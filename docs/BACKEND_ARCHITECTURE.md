# Kiến trúc backend — bản đồ code

> Mục đích: trả lời câu hỏi *"muốn sửa X thì mở file nào"* mà không phải grep cả repo.
> Logic pipeline Text2SQL nằm ở [TEXT2SQL_PIPELINE.md](TEXT2SQL_PIPELINE.md), không lặp lại ở đây.

---

## 1. Stack

| Lớp | Công nghệ |
|---|---|
| Web framework | FastAPI (async), Starlette middleware |
| ORM | SQLModel trên SQLAlchemy 2.x |
| DB metadata | PostgreSQL + extension **pgvector** |
| Migration | Alembic, **chạy tự động lúc khởi động** |
| LLM | LangChain `BaseChatModel`, gọi kiểu streaming |
| Embedding | HuggingFace (local) hoặc `OpenAIEmbeddings` (API) |
| Runtime | Python **3.11** (`requires-python = "==3.11.*"`), dependency bằng `uv` |
| MCP | `fastapi-mcp` |
| Frontend | Vue 3 + TypeScript + Vite + Element Plus |

Lưu ý về **hai database khác nhau**, rất dễ nhầm:

- **DB metadata của SQLBot** — Postgres, chứa `chat`, `core_datasource`, `terminology`… Cấu hình qua
  `POSTGRES_*` / `SQLBOT_DB_URL`.
- **DB nghiệp vụ** — cái mà người dùng hỏi. Người dùng khai báo qua UI/API datasource, có thể là 12
  loại DB khác nhau, SQLBot chỉ đọc.

---

## 2. Bố cục thư mục

```
backend/
├── main.py              # entry point, 2 app, middleware, lifespan
├── alembic/versions/    # migration
├── templates/           # prompt (template.yaml + sql_examples/*.yaml)
├── scripts/             # tool ngoài luồng chạy: eval, demo tích hợp
├── common/              # hạ tầng dùng chung
└── apps/                # chia theo domain
```

| Thư mục | Trách nhiệm | File cần biết |
|---|---|---|
| `apps/chat/` | Hội thoại + **toàn bộ pipeline** | `task/llm.py` (2.5K dòng, trái tim hệ thống), `api/chat.py`, `curd/chat.py`, `models/chat_model.py`, `utils/attachment.py` (đọc file `.docx` đính kèm), `permission/sw_permission.py` (quyền dữ liệu theo user, đồng bộ từ SW) |
| `apps/datasource/` | Datasource, bảng, cột, quan hệ, M-Schema | `crud/datasource.py`, `api/datasource.py`, `api/table_relation.py`, `embedding/table_embedding.py`, `crud/permission.py` |
| `apps/datasource/task/` | Ba vòng chạy nền của luồng nạp Excel bất đồng bộ (xem §5) | `excel_import_task.py`, `callback_sender.py`, `excel_recovery.py` |
| `apps/db/` | Tầng truy cập DB nghiệp vụ đa engine | `db.py` (1.3K dòng), `constant.py` (enum `DB`) |
| `apps/ai_model/` | Nạp và cache LLM / embedding model | `model_factory.py`, `embedding.py` |
| `apps/terminology/` | Từ điển thuật ngữ nghiệp vụ + vector | `curd/terminology.py` |
| `apps/data_training/` | Ví dụ SQL mẫu + vector | `curd/data_training.py` |
| `apps/template/` | Nạp prompt từ YAML (có `@cache`) | `template.py` |
| `apps/system/` | User, workspace, auth, model config, assistant, apikey | `middleware/auth.py`, `schemas/permission.py`, `api/login.py` |
| `apps/mcp/` | MCP server | `mcp.py` |
| `apps/hooks/` | Cổng nhận bản tin đồng bộ từ hệ ngoài (SW) + API đọc quyền (dev/test) | `api/ai_sync.py`, `api/permission_query.py`, `handlers/authorization.py`, `crud/ai_user_permission.py` |
| `apps/dashboard/`, `apps/settings/`, `apps/swagger/` | Dashboard, tham số hệ thống, i18n cho OpenAPI | |
| `common/core/` | Config, session DB, response middleware, cache | `config.py`, `db.py`, `response_middleware.py` |
| `common/utils/` | Tiện ích: crypto, log, lock phân tán, embedding thread, vòng lặp định kỳ | `crypto.py`, `distributed_lock.py`, `embedding_threads.py`, `periodic.py` |

Quy ước đặt tên: mỗi domain có `api/` (route), `crud/` hoặc `curd/` (truy cập DB — **cả hai cách
viết đều tồn tại**, `apps/chat/curd/` là lỗi chính tả của upstream đã ăn sâu), `models/` (SQLModel +
schema Pydantic).

---

## 3. Entry point

[main.py](../backend/main.py) tạo **hai** FastAPI app:

| App | Port | Việc |
|---|---|---|
| `app` | 8000 | API chính + phục vụ frontend |
| `mcp_app` | 8001 | MCP server (`FastApiMCP` mount vào), phục vụ `/images` tĩnh |

`mcp` chỉ expose 7 operation: `mcp_datasource_list`, `get_model_list`, `mcp_question`, `mcp_start`,
`mcp_assistant`, `mcp_ws_list`, `access_token` ([main.py:222](../backend/main.py#L222)).

### Chuỗi middleware

Thêm theo thứ tự `TokenMiddleware` → `ResponseMiddleware` → `RequestContextMiddleware` →
`RequestContextMiddlewareCommon` ([main.py:246](../backend/main.py#L246)). Starlette chạy middleware
theo thứ tự **ngược** với thứ tự thêm, nên `RequestContextMiddlewareCommon` chạm request trước.

`ResponseMiddleware` bọc mọi response JSON có HTTP **2xx** vào envelope `{code, data, msg}` — không
riêng 200, để endpoint trả `202` (`createFromExcelAsync`) không sinh ra hình dạng body thứ hai. Muốn
một route trả **JSON thô** thì cách duy nhất là khai path pattern của nó vào danh sách `direct_paths`
([response_middleware.py:30](../backend/common/core/response_middleware.py#L30)) — hiện có
`mcp_question`, `mcp_assistant`, `/hooks/ai-sync` và bộ `openapi.json`/`docs`/`redoc`. Xem
[OPERATIONS.md §7.8](OPERATIONS.md) để biết vì sao trả về `JSONResponse` trong route **không** đủ.

### `lifespan` — chạy lúc khởi động

[main.py:66](../backend/main.py#L66), theo đúng thứ tự:

1. `run_migrations()` — **alembic upgrade head tự động**. Không có bước migrate thủ công nào.
2. `init_sqlbot_cache()`, `init_dynamic_cors(app)`
3. Lấp embedding còn thiếu cho terminology / data_training / bảng+datasource. Bọc trong
   `SingleWorkerGuard.once` để nhiều worker uvicorn không cùng chạy.
4. `start_datasource_background_tasks()` — bật vòng gửi callback và vòng quét phục hồi của luồng nạp
   Excel (§5). **Cố ý không** bọc `SingleWorkerGuard`: hai vòng này giành việc bằng
   `FOR UPDATE SKIP LOCKED` và `UPDATE` có điều kiện nên chạy ở mọi worker vừa an toàn vừa có lợi.
5. `async_model_info()` — mã hóa lại key/địa chỉ của model đã lưu.

`shutdown_resources()` gọi `stop_datasource_background_tasks()` **trước** `SingleWorkerGuard.release()`
— các vòng nền còn đang giữ session DB.

Migration mới nhất: `082_chat_record_domain_code.py` (thêm cột `chat_record.domain_code` — mã lĩnh
vực giới hạn phạm vi của lượt hỏi). Trước đó: `081_chat_attachment.py` (tạo `chat_attachment` — text
trích từ file `.docx` đính kèm câu hỏi), `080_excel_import_jobs_file_url.py` (thêm cột `file_url` — nguồn file
chuyển từ multipart sang presigned URL; đánh số 080 vì revision id của bản 079 cũ trùng với một
migration đã có trên `main`), `079_ai_user_permissions_drop_table_description.py`,
`078_ai_user_permissions_drop_fields.py`, `077_ai_user_permissions_queries.py`,
`076_excel_import_jobs.py` (tạo `excel_import_jobs` + backfill
`core_datasource.status`), `075_ai_user_permissions_domain_info.py`, `074_ai_sync_hook.py`
(tạo `ai_sync_hook_logs` + `ai_user_permissions`), `073_merge_heads_071.py` (gộp hai head `071` phát
sinh khi merge nhánh), `072_add_chat_external_id.py`.

---

## 4. Muốn sửa X thì vào đâu

| Muốn | Mở |
|---|---|
| Sửa cách sinh SQL / thứ tự message | [llm.py:358](../backend/apps/chat/task/llm.py#L358) `init_messages` + [llm.py:1116](../backend/apps/chat/task/llm.py#L1116) `generate_sql` |
| Sửa nội dung prompt (bất kỳ pha nào) | [templates/template.yaml](../backend/templates/template.yaml) — bản đồ khối ở [TEXT2SQL_PIPELINE.md §10](TEXT2SQL_PIPELINE.md) |
| Sửa prompt riêng cho một loại DB | [templates/sql_examples/](../backend/templates/sql_examples/) |
| Sửa pha trả lời bằng lời | [llm.py:662](../backend/apps/chat/task/llm.py#L662) + khối `answer` ở [template.yaml:630](../backend/templates/template.yaml#L630) |
| Sửa vòng retry / hạ cấp | [llm.py:1697-1870](../backend/apps/chat/task/llm.py#L1697) |
| Thêm/sửa event SSE | `run_task` ([llm.py:1622](../backend/apps/chat/task/llm.py#L1622)) — **rồi cập nhật `API_SPEC.md` §6.3** |
| Đổi điểm dừng pipeline | [chat_model.py:54](../backend/apps/chat/models/chat_model.py#L54) `ChatFinishStep` |
| Sửa M-Schema đưa vào prompt | [datasource.py:859](../backend/apps/datasource/crud/datasource.py#L859) `get_table_schema` |
| Đổi cách chọn bảng theo embedding | [table_embedding.py:43](../backend/apps/datasource/embedding/table_embedding.py#L43) |
| Sửa kiểm tra an toàn SQL | [llm.py:103](../backend/apps/chat/task/llm.py#L103) (whitelist bảng) + [db.py:1084](../backend/apps/db/db.py#L1084) (`check_sql_read`) |
| Sửa quyền dữ liệu theo user (bảng / cột / hàng) | [apps/chat/permission/sw_permission.py](../backend/apps/chat/permission/sw_permission.py) — suy diễn quyền và viết lại SQL; hai điểm cưỡng chế nằm ở `choose_table_schema` và ngay trước `execute_sql` trong `llm.py`. **Rồi cập nhật `TEXT2SQL_PIPELINE.md` §13** |
| Thêm một loại database | [db/constant.py](../backend/apps/db/constant.py) enum `DB` + [db.py](../backend/apps/db/db.py) + một file `templates/sql_examples/*.yaml` |
| Sửa endpoint hỏi đáp | [chat.py:485](../backend/apps/chat/api/chat.py#L485) |
| Sửa xử lý file `.docx` đính kèm | `apps/chat/utils/attachment.py` (tải + trích + render khối prompt) + `apps/chat/curd/attachment.py` (lưu/đọc bảng) — **rồi cập nhật `API_SPEC.md` §6.10** |
| Sửa quản trị datasource | [apps/datasource/api/datasource.py](../backend/apps/datasource/api/datasource.py) |
| Sửa luồng nạp Excel bất đồng bộ | `apps/datasource/task/` (3 vòng nền, §5) + `crud/excel_job.py` (mọi câu SQL của hàng đợi) + `utils/excel_import.py` (hàm thuần nạp file) + `utils/remote_file.py` (kiểm URL nguồn và tải file) |
| Sửa hợp đồng callback với hệ ngoài | [callback_sender.py](../backend/apps/datasource/task/callback_sender.py) `send_callback` — **rồi cập nhật `DATASOURCE_API_SPEC.md` §9.3** |
| Sửa auth / token | [apps/system/middleware/auth.py](../backend/apps/system/middleware/auth.py) + [apps/system/api/login.py](../backend/apps/system/api/login.py) |
| Sửa quản trị tài khoản người dùng | [apps/system/api/user.py](../backend/apps/system/api/user.py) — nhóm `/user/by-account/*` ở đầu file, handler gốc theo `id` ở dưới. **Rồi cập nhật `USER_ADMIN_API_SPEC.md` và bản `_LITE`** |
| Đổi cấu hình mặc định | [common/core/config.py](../backend/common/core/config.py) — bảng đầy đủ ở [OPERATIONS.md §2](OPERATIONS.md) |
| Thêm bảng DB mới | model trong `apps/<domain>/models/` + migration trong `alembic/versions/` |
| Sửa cách gọi LLM | [model_factory.py](../backend/apps/ai_model/model_factory.py) |

---

## 5. Data model

### Cụm hội thoại — [chat_model.py](../backend/apps/chat/models/chat_model.py)

| Bảng | Vai trò | Cột đáng chú ý |
|---|---|---|
| `chat` | Một hội thoại | `external_id`, `oid`, `datasource`, `brief`, `origin` (0 web / 1 mcp / 2 assistant) |
| `chat_record` | Một **lượt hỏi** | `question`, `sql`, `data`, `answer`, `chart`, `analysis`, `predict`, `error`, `finish`, `domain_code` |
| `chat_log` | Một **thao tác** trong lượt | `operate` (enum 14 giá trị), `messages` (JSONB), `token_usage`, `local_operation`, `error` |
| `chat_attachment` | Text đã trích từ file `.docx` gửi kèm một lượt hỏi | `chat_id`, `record_id`, `filename`, `content`, `truncated` |

Quan hệ: `chat` 1─n `chat_record` 1─n `chat_log`; `chat_record` 1─n `chat_attachment`.

`chat_attachment` (migration `081_chat_attachment.py`) **cố ý không đặt UNIQUE trên `record_id`** —
hiện mỗi lượt chỉ đính một file, nhưng để mở đường cho nhiều file mà không phải sửa schema; khi đọc
thì bản mới nhất của record thắng. `content` là bản đã bị áp trần `CHAT_DOC_EXTRACT_MAX_CHARS`, `truncated`
cho biết bản gốc có dài hơn thế không. Bảng này chỉ được đọc ở một chỗ duy nhất khi dựng lịch sử pha
answer — xem [TEXT2SQL_PIPELINE.md §10](TEXT2SQL_PIPELINE.md).

`chat_record.answer` do migration `071_add_chat_record_answer.py` thêm — cột này là của fork, upstream
không có. `chat_record.domain_code` (migration `082_chat_record_domain_code.py`) ghi lại mã lĩnh vực
mà lượt hỏi bị giới hạn theo; `NULL` nghĩa là không giới hạn. Lưu xuống DB thay vì chỉ giữ trong
runtime vì `/regenerate` phải dựng lại đúng phạm vi dữ liệu của lượt gốc — xem
[TEXT2SQL_PIPELINE.md §13](TEXT2SQL_PIPELINE.md).

`OperationEnum` có 14 giá trị; giá trị `ANSWER='14'` là của fork. `chat_log` là thứ dùng để debug —
xem [OPERATIONS.md §6](OPERATIONS.md).

### `chat.external_id` — vì sao tồn tại

[chat_model.py:104](../backend/apps/chat/models/chat_model.py#L104). Khóa chính `chat.id` khai
`Identity(always=True)`, tức Postgres **luôn tự sinh**, client không chèn giá trị riêng vào được.
Nhưng đối tác tích hợp cần tự đặt định danh hội thoại phía họ. `external_id` là cột UUID riêng,
`UNIQUE` (để hai lần hỏi cùng UUID chắc chắn rơi vào đúng một hội thoại) và `NULL` với hội thoại tạo
từ web (Postgres cho phép nhiều NULL trong ràng buộc UNIQUE).

Việc quy đổi `external_id` → id nội bộ nằm ở `resolve_chat_id`
([curd/chat.py:923](../backend/apps/chat/curd/chat.py#L923)), gọi qua dependency
`resolve_chat_for_question` ([chat.py:440](../backend/apps/chat/api/chat.py#L440)). **Phải là
dependency**, không được gọi trong thân route, vì `require_permissions` cần thấy id nội bộ.

### Cụm datasource — [datasource.py](../backend/apps/datasource/models/datasource.py)

| Bảng | Vai trò |
|---|---|
| `core_datasource` | Kết nối: `type`, `configuration` (**đã mã hóa, chứa password**), `table_relation` (JSON sơ đồ quan hệ), `embedding` |
| `core_table` | Một bảng đã đồng bộ: `table_name`, `custom_comment`, `embedding`, `checked` |
| `core_field` | Một cột: `field_name`, `field_type`, `custom_comment`, `field_index` |
| `ds_recommended_problem` | Câu hỏi gợi ý cho datasource |

`custom_comment` của bảng/cột là thứ đi thẳng vào M-Schema — đòn bẩy chất lượng SQL lớn nhất không
cần đụng code.

### Cụm nạp Excel bất đồng bộ — [excel_job.py](../backend/apps/datasource/models/excel_job.py)

Một bảng duy nhất, `excel_import_jobs` (migration `076`), giữ **hai** vai trò: hàng đợi việc nạp và
hộp thư đi (outbox) của callback. Ràng buộc `UNIQUE(ds_id)` nên mỗi nguồn dữ liệu có đúng một job.

| Nhóm cột | Dùng để |
|---|---|
| `ds_id`, `oid`, `create_by`, `ds_name`, `file_path`, `file_url`, `sheet_names` | Đủ để worker làm việc mà không cần request context |
| `status`, `worker_id`, `heartbeat_at`, `created_at`/`started_at`/`finished_at` | Vòng đời job và phát hiện worker chết |
| `created_tables` | Danh sách bảng đã tạo, để dọn khi job hỏng giữa chừng |
| `error_code`, `error_message` | Trả lại cho `GET /datasource/excelImportStatus/{ds_id}` |
| `callback_status`, `callback_attempts`, `next_attempt_at`, `callback_claimed_at`, `callback_last_error`, `callback_payload` | Phần outbox |

**Vì sao job row chính là outbox**, không tách bảng riêng: kết quả nạp và lá thư báo kết quả phải
cùng sinh ra trong một transaction. Tách bảng thì có thể nạp xong mà thư không được ghi (hoặc ngược
lại), và không có transaction phân tán nào ở đây để cứu.

**Hai cột file, đọc kỹ kẻo hiểu ngược.** `file_url` là nơi file **đến từ**, `file_path` là nơi nó
**sẽ nằm**. Từ khi endpoint nhận presigned URL thay vì bytes, pha đồng bộ chỉ đặt trước cái tên ở
`file_path` chứ không tạo ra file nào — worker mới là bên ghi. Trong khoảng giữa hai việc đó,
`file_path` trỏ tới một file chưa tồn tại. Đặt tên sẵn (thay vì để cột rỗng) giữ được ràng buộc
`NOT NULL` và giữ cho nhánh dọn file của `excel_recovery` không phải thêm phép kiểm rỗng.
`file_url` rỗng nghĩa là job cũ theo đường multipart — nhánh đó vẫn chạy được.

Ba vòng nền, mỗi vòng một file trong `apps/datasource/task/`:

| Vòng | File | Việc |
|---|---|---|
| Worker nạp | `excel_import_task.py` | `ThreadPoolExecutor` riêng (`EXCEL_IMPORT_WORKERS`). Endpoint `submit` một job **sau khi commit**, worker mở session riêng, đập heartbeat theo chu kỳ, kết thúc bằng `finish_job` (ghi kết quả + xếp thư vào outbox trong cùng transaction). Hai bước đầu của worker là **tải file** từ `file_url` và **đối chiếu tên sheet** — cả hai nằm trong khối heartbeat vì tải một file lớn có thể lâu hơn ngưỡng phát hiện job chết |
| Vòng gửi | `callback_sender.py` | Ba pha: nhặt thư bằng `FOR UPDATE SKIP LOCKED` (transaction ngắn) → gọi HTTP **ngoài mọi transaction** → ghi kết quả (transaction ngắn). Không giữ transaction mở trong lúc chờ mạng — đó là lý do tách ba pha |
| Vòng phục hồi | `excel_recovery.py` | Bốn việc độc lập, mỗi việc try/except riêng: nhặt job mồ côi (`pending` quá lâu), chôn job chết (heartbeat hết hạn → drop bảng dở, ds `Failed`, xếp thư báo hỏng), gỡ thư kẹt ở `sending`, dọn file tạm quá hạn không ai dùng |

Giành việc giữa nhiều tiến trình dựa vào `SKIP LOCKED` và `UPDATE ... WHERE status = 'pending'` kiểm
`rowcount` — không dùng khóa toàn cục, nên chạy bao nhiêu worker uvicorn cũng đúng.

Chống trùng tên nguồn dữ liệu dùng **advisory lock** (`pg_advisory_xact_lock`, khóa dẫn từ chuỗi
`sqlbot:ds:name:<oid>:<name>`) chứ không phải unique index: dữ liệu hiện có rất có thể đã trùng tên
do `check_name` thủng với `oid` rỗng, thêm index là migration hỏng giữa chừng.

### Cụm RAG

`terminology` và `data_training`, mỗi bảng có cột `embedding` kiểu vector của pgvector, truy vấn
bằng toán tử `<=>`.

### Cụm AI Sync Hook — [ai_sync_model.py](../backend/apps/hooks/models/ai_sync_model.py)

| Bảng | Vai trò |
|---|---|
| `ai_sync_hook_logs` | Audit toàn bộ request SW gửi vào `/hooks/ai-sync`: raw payload, trạng thái xử lý, `sync_version` |
| `ai_user_permissions` | Trạng thái quyền **hiện tại** của user theo form, ghi từ bản tin `AUTHORIZATION_SYNC` (full snapshot — xem `AI_SYNC_HOOK_API_SPEC.md` §5) |

Quan hệ: hai bảng liên kết lỏng qua `user_id` (không FK) — `ai_sync_hook_logs` là audit trail,
`ai_user_permissions` là state hiện tại được `replace_user_permissions` ghi đè theo snapshot mỗi
lần đồng bộ thành công. Khoá chính hai bảng này là UUID sinh phía DB (`gen_random_uuid()`), khác
quy ước snowflake BigInt của phần còn lại trong repo, vì chúng do hệ ngoài ghi vào.

Pipeline Text2SQL **chỉ đọc** `ai_user_permissions`, mỗi lượt hỏi một lần, qua
`apps/chat/permission/sw_permission.py` — nó lọc bảng/cột đưa vào M-Schema và bọc phạm vi dòng vào
SQL trước khi chạy. Ghi vào bảng này là độc quyền của hook. Chi tiết:
[TEXT2SQL_PIPELINE.md §13](TEXT2SQL_PIPELINE.md).

---

## 6. Tầng DB đa engine — `apps/db/db.py`

13 loại DB, khai trong enum `DB` ([db/constant.py:18](../backend/apps/db/constant.py#L18)): `excel`
(thực chất là PostgreSQL nội bộ), `redshift`, `ck`, `dm` (达梦), `doris`, `es`, `kingbase`,
`sqlServer`, `mysql`, `oracle`, `pg`, `starrocks`, `hive`.

Mỗi entry mang 6 thông tin: mã, tên hiển thị, ký tự **mở** và **đóng** định danh (`"` hay `` ` ``),
kiểu kết nối (`sqlalchemy` hay `py_driver`), tên file template trong `sql_examples/`, danh sách tham
số bị cấm trong `extraParams`.

| Hàm | Dòng | Việc |
|---|---|---|
| `get_engine` | [139](../backend/apps/db/db.py#L139) | Engine SQLAlchemy, tùy chọn dùng pool |
| `get_driver_connection` | [191](../backend/apps/db/db.py#L191) | Nhánh `py_driver` cho DB không có dialect SQLAlchemy tốt |
| `check_connection` | [294](../backend/apps/db/db.py#L294) | Kiểm tra kết nối trước khi chạy pipeline |
| `get_tables` / `get_fields` / `get_relations` | [497](../backend/apps/db/db.py#L497) / [547](../backend/apps/db/db.py#L547) / [597](../backend/apps/db/db.py#L597) | Đồng bộ metadata |
| `exec_sql` | [775](../backend/apps/db/db.py#L775) | Chạy SQL, chuẩn hóa kiểu dữ liệu về JSON-safe |
| `get_sqlglot_dialect` | [1026](../backend/apps/db/db.py#L1026) | Ánh xạ loại DB → dialect sqlglot, dùng ở cả bảo mật lẫn parse |
| `check_sql_read` | [1084](../backend/apps/db/db.py#L1084) | Tầng bảo mật thứ 3 |

Hai pool manager riêng: `ConnectionPoolManager` (SQLAlchemy) và `DriverConnectionPoolManager`
(py_driver), [db.py:1165](../backend/apps/db/db.py#L1165) và [1222](../backend/apps/db/db.py#L1222).
Pool của **DB metadata** thì cấu hình bằng `PG_POOL_SIZE` / `PG_MAX_OVERFLOW` / `PG_POOL_RECYCLE` /
`PG_POOL_PRE_PING`.

---

## 7. Auth và phân quyền

### Token

- Header: **`X-SQLBOT-TOKEN: Bearer <jwt>`** (`settings.TOKEN_KEY`), không phải `Authorization`.
- Cấp bởi `POST /login/access-token`, body **`application/x-www-form-urlencoded`**.
- Hạn 8 ngày (`ACCESS_TOKEN_EXPIRE_MINUTES = 60*24*8`). **Không có refresh endpoint** — hết hạn thì
  đăng nhập lại.
- Ký bằng `SECRET_KEY`. Mặc định trong code là `secrets.token_urlsafe(32)`, sinh mới mỗi lần process
  start → **không đặt cố định thì mọi JWT chết sau mỗi lần restart**.
- Assistant dùng header riêng `X-SQLBOT-ASSISTANT-TOKEN`.

`TokenMiddleware` ([auth.py:26](../backend/apps/system/middleware/auth.py#L26)) chặn mọi request trừ
danh sách trắng và preflight OPTIONS.

`POST /hooks/ai-sync` **không** nằm trong danh sách trắng — dùng chung JWT `X-SQLBOT-TOKEN` và
`require_permissions(role=['ws_admin'])` như các API quản trị khác, không có static token riêng.

### Hai không gian định danh người dùng

Khoá nội bộ là `sys_user.id` (snowflake 19 chữ số), nhưng hệ ngoài SW không giữ nó. Cầu nối là cột
**`account`**: SW đặt `account` bằng đúng `userId` bên họ, nên một định danh duy nhất chạy suốt cả
API quản trị lẫn `POST /hooks/ai-sync`.

Nhóm route `/user/by-account/*` ([user.py:215](../backend/apps/system/api/user.py#L215)) phục vụ
việc đó — sáu route ánh xạ 1-1 với các handler theo `id`. Mỗi route chỉ gọi `resolve_uid_by_account`
([crud/user.py:29](../backend/apps/system/crud/user.py#L29)) rồi **gọi lại chính handler cũ** thay vì
chép thân hàm, nên ba lớp decorator của handler gốc (kiểm quyền, xoá cache, ghi log kiểm toán) vẫn
chạy đúng cấu hình gốc. `resolve_uid_by_account` chặn sẵn admin `id == 1` vì các handler gốc không tự
chặn.

Hai cái bẫy đi kèm nằm ở [OPERATIONS.md §7.9](OPERATIONS.md).

### Bốn lớp phân quyền

1. **Workspace (`oid`)** — mọi tài nguyên (datasource, terminology, chat) gắn `oid`. Truy vấn luôn
   lọc theo `oid` của user hiện tại.
2. **Permission code** — `require_permissions(SqlbotPermission)`
   ([permission.py:49](../backend/apps/system/schemas/permission.py#L49)) dùng làm FastAPI dependency
   trên route.
3. **Row/column permission của xpack** — cấu hình bằng tay trên UI SQLBot, sinh điều kiện `WHERE` bổ
   sung chèn vào SQL của LLM (`generate_filter`, [llm.py:1298](../backend/apps/chat/task/llm.py#L1298)).
4. **Quyền dữ liệu đồng bộ từ SW** — lọc bảng/cột khỏi M-Schema và bọc phạm vi dòng vào SQL, dữ liệu
   lấy từ `ai_user_permissions` (`apps/chat/permission/sw_permission.py`).

Lớp 3 và lớp 4 **độc lập với nhau**, không biết tới nhau, và trong triển khai HĐND chỉ lớp 4 có dữ
liệu. Đừng đọc code của lớp 3 để suy ra hành vi của lớp 4: lớp 3 nhờ LLM viết lại điều kiện lọc,
lớp 4 viết lại SQL bằng `sqlglot` một cách tất định.

**Admin `id=1` bỏ qua lớp 3.** `is_normal_user` chỉ là `current_user.id != 1`
([permission.py:89](../backend/apps/datasource/crud/permission.py#L89)). Test bằng tài khoản admin
sẽ **không** thấy row-permission chạy — đây là nguồn nhầm lẫn kinh điển. Lớp 4 có cơ chế bỏ qua
riêng: user không có dòng quyền nào, hoặc có cờ `is_admin` từ SW.

---

## 8. Tầng AI model — `apps/ai_model/`

### LLM

`LLMFactory.create_llm` ([model_factory.py](../backend/apps/ai_model/model_factory.py)) trả về
`BaseChatModel` của LangChain, bọc `@lru_cache(maxsize=32)` — cùng một cấu hình chỉ khởi tạo một
lần. Hệ quả: **đổi cấu hình model trong DB thì phải xóa cache hoặc restart** mới có hiệu lực.

`get_default_config()` đọc model mặc định của workspace và **giải mã** `api_domain` / `api_key`
(mã hóa bằng `common/utils/crypto.py`, thực chất gọi xuống `sqlbot_xpack.core`).

`apply_disable_thinking()` merge `LLM_DISABLE_THINKING_EXTRA_BODY` vào `extra_body` **mà không ghi
đè** phần admin đã cấu hình. Mặc định payload theo chuẩn vLLM/SGLang
(`{"chat_template_kwargs": {"enable_thinking": false}}`); API kiểu DashScope cần
`{"enable_thinking": false}` — đổi **biến môi trường**, đừng sửa code.

Mọi nhà cung cấp đều đi qua giao diện OpenAI-compatible.

### Embedding

`EmbeddingModelCache` ([embedding.py](../backend/apps/ai_model/embedding.py)) dùng double-checked
locking để nhiều thread không cùng nạp model. `EMBEDDING_PROVIDER='api'` → `OpenAIEmbeddings`; khác
đi → `HuggingFaceEmbeddings` tải model về `LOCAL_MODEL_PATH`.

---

## 9. `sqlbot_xpack` — package ngoài repo

**Không có source trong repo này.** Cài từ wheel/index riêng, nằm ở
`backend/.venv/lib/python3.11/site-packages/sqlbot_xpack`. Version bị ghim trong `pyproject.toml`.
Đừng đi tìm code của nó trong `apps/`.

Nó cung cấp:

| Module | Dùng ở |
|---|---|
| `sqlbot_xpack.core` | Mã hóa/giải mã (`common/utils/crypto.py`), license, `monitor_app` |
| `sqlbot_xpack.permissions` | Model `DsPermission` / `DsRules`, row & column permission |
| `sqlbot_xpack.custom_prompt` | Prompt tùy chỉnh theo workspace/datasource |
| `sqlbot_xpack.config` | Tham số hệ thống (`SysArgModel`) |
| `sqlbot_xpack.authentication` | Logout |
| `sqlbot_xpack.file_utils` | Upload file |

Nhiều tính năng bị chặn sau `SQLBotLicenseUtil.valid()` — ví dụ custom prompt
([llm.py:483](../backend/apps/chat/task/llm.py#L483)). Không có license hợp lệ thì các nhánh đó im
lặng không chạy, **không báo lỗi**.

---

## 10. Frontend (tóm tắt)

Vue 3 + TypeScript + Vite, thư mục `frontend/src/`.

| Thư mục | Nội dung |
|---|---|
| `views/chat/` | Giao diện hỏi đáp — `index.vue`, `ChatRow.vue`, `answer/`, `chat-block/`, `execution-component/` |
| `views/ds/` | Quản trị datasource, chọn bảng, sơ đồ quan hệ |
| `views/system/` | User, workspace, cấu hình model |
| `views/embedded/` | Nhúng vào trang khác |
| `api/` | Wrapper gọi backend |
| `stores/` | Pinia store |

Frontend đọc SSE qua `request.fetchStream('/chat/question', ...)`
([api/chat.ts:17](../frontend/src/api/chat.ts#L17)) — tự đọc `ReadableStream` và parse chuỗi `data:`,
**không** dùng `EventSource`: `EventSource` chỉ làm được GET và không gửi được custom header
`X-SQLBOT-TOKEN`. `AbortController` truyền vào chỉ ngắt kết nối phía client; backend vẫn chạy tiếp
tới hết (không có cơ chế hủy — xem `API_SPEC.md` §7.1).

Dev server: `npm run dev` ở `frontend/`, mặc định lắng nghe mọi interface (đã sửa trong nhánh này).

---

## 11. Bề mặt API

Router đăng ký ở [apps/api.py](../backend/apps/api.py), prefix `settings.API_V1_STR` = `/api/v1`
(`CONTEXT_PATH` mặc định rỗng):

`login` · `user` · `workspace` · `assistant` · `aimodel` · `base` (settings) · `terminology` ·
`data_training` · `datasource` · `chat` · `dashboard` · `mcp` · `table_relation` · `parameter` ·
`apikey` · `recommended_problem` · `variable_api` · `hooks`

Đặc tả chi tiết đã có, **đừng viết lại**:

- [API_SPEC.md](../backend/scripts/chat_stream_demo/API_SPEC.md) — luồng tích hợp chat (login,
  datasource list, `POST /chat/question` SSE).
- [DATASOURCE_API_SPEC.md](../backend/scripts/chat_stream_demo/DATASOURCE_API_SPEC.md) — 26 endpoint
  quản trị datasource, cho cả database quan hệ (§3–§7) lẫn file Excel/CSV (§8 ba bước, §9 một lời
  gọi + callback).
- [AI_SYNC_HOOK_API_SPEC.md](../backend/scripts/ai_sync_hook/AI_SYNC_HOOK_API_SPEC.md) — cổng nhận
  bản tin đồng bộ từ hệ thống SW (`POST /hooks/ai-sync`).
- [AI_PERMISSION_QUERY_API_SPEC.md](../backend/scripts/ai_sync_hook/AI_PERMISSION_QUERY_API_SPEC.md)
  — 2 endpoint GET đọc lại quyền đã đồng bộ (dev/test), tách riêng khỏi spec ghi ở trên.
- [USER_ADMIN_API_SPEC.md](../backend/scripts/ai_sync_hook/USER_ADMIN_API_SPEC.md) — quản trị tài
  khoản cho SW (đăng nhập, tạo/sửa/khoá/xoá user, mật khẩu), định danh bằng `account`. Bản
  `USER_ADMIN_API_SPEC_LITE.md` là bản rút gọn gửi đối tác; sửa thì sửa cả hai.

Swagger: `GET /docs` (bật/tắt bằng `SQLBOT_DOC_ENABLED`), có i18n qua `?lang=zh|en`.
