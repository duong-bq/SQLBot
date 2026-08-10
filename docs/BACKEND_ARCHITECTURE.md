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
| `apps/chat/` | Hội thoại + **toàn bộ pipeline** | `task/llm.py` (2.4K dòng, trái tim hệ thống), `api/chat.py`, `curd/chat.py`, `models/chat_model.py` |
| `apps/datasource/` | Datasource, bảng, cột, quan hệ, M-Schema | `crud/datasource.py`, `api/datasource.py`, `api/table_relation.py`, `embedding/table_embedding.py`, `crud/permission.py` |
| `apps/db/` | Tầng truy cập DB nghiệp vụ đa engine | `db.py` (1.3K dòng), `constant.py` (enum `DB`) |
| `apps/ai_model/` | Nạp và cache LLM / embedding model | `model_factory.py`, `embedding.py` |
| `apps/terminology/` | Từ điển thuật ngữ nghiệp vụ + vector | `curd/terminology.py` |
| `apps/data_training/` | Ví dụ SQL mẫu + vector | `curd/data_training.py` |
| `apps/template/` | Nạp prompt từ YAML (có `@cache`) | `template.py` |
| `apps/system/` | User, workspace, auth, model config, assistant, apikey | `middleware/auth.py`, `schemas/permission.py`, `api/login.py` |
| `apps/mcp/` | MCP server | `mcp.py` |
| `apps/hooks/` | Cổng nhận bản tin đồng bộ từ hệ ngoài (SW) | `api/ai_sync.py`, `handlers/authorization.py`, `crud/ai_user_permission.py` |
| `apps/dashboard/`, `apps/settings/`, `apps/swagger/` | Dashboard, tham số hệ thống, i18n cho OpenAPI | |
| `common/core/` | Config, session DB, response middleware, cache | `config.py`, `db.py`, `response_middleware/` |
| `common/utils/` | Tiện ích: crypto, log, lock phân tán, embedding thread | `crypto.py`, `distributed_lock.py`, `embedding_threads.py` |

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

### `lifespan` — chạy lúc khởi động

[main.py:66](../backend/main.py#L66), theo đúng thứ tự:

1. `run_migrations()` — **alembic upgrade head tự động**. Không có bước migrate thủ công nào.
2. `init_sqlbot_cache()`, `init_dynamic_cors(app)`
3. Lấp embedding còn thiếu cho terminology / data_training / bảng+datasource. Bọc trong
   `SingleWorkerGuard.once` để nhiều worker uvicorn không cùng chạy.
4. `async_model_info()` — mã hóa lại key/địa chỉ của model đã lưu.

Migration mới nhất: `074_ai_sync_hook.py` (tạo `ai_sync_hook_logs` + `ai_user_permissions`). Trước
đó: `073_merge_heads_071.py` (gộp hai head `071` phát sinh khi merge nhánh), `072_add_chat_external_id.py`,
`071_add_chat_record_answer.py`, `070_add_table_column_comments.py`.

---

## 4. Muốn sửa X thì vào đâu

| Muốn | Mở |
|---|---|
| Sửa cách sinh SQL / thứ tự message | [llm.py:354](../backend/apps/chat/task/llm.py#L354) `init_messages` + [llm.py:1056](../backend/apps/chat/task/llm.py#L1056) `generate_sql` |
| Sửa nội dung prompt (bất kỳ pha nào) | [templates/template.yaml](../backend/templates/template.yaml) — bản đồ khối ở [TEXT2SQL_PIPELINE.md §10](TEXT2SQL_PIPELINE.md) |
| Sửa prompt riêng cho một loại DB | [templates/sql_examples/](../backend/templates/sql_examples/) |
| Sửa pha trả lời bằng lời | [llm.py:611](../backend/apps/chat/task/llm.py#L611) + khối `answer` ở [template.yaml:630](../backend/templates/template.yaml#L630) |
| Sửa vòng retry / hạ cấp | [llm.py:1613-1780](../backend/apps/chat/task/llm.py#L1613) |
| Thêm/sửa event SSE | `run_task` ([llm.py:1538](../backend/apps/chat/task/llm.py#L1538)) — **rồi cập nhật `API_SPEC.md` §6.3** |
| Đổi điểm dừng pipeline | [chat_model.py:54](../backend/apps/chat/models/chat_model.py#L54) `ChatFinishStep` |
| Sửa M-Schema đưa vào prompt | [datasource.py:638](../backend/apps/datasource/crud/datasource.py#L638) `get_table_schema` |
| Đổi cách chọn bảng theo embedding | [table_embedding.py:43](../backend/apps/datasource/embedding/table_embedding.py#L43) |
| Sửa kiểm tra an toàn SQL | [llm.py:102](../backend/apps/chat/task/llm.py#L102) (whitelist bảng) + [db.py:1084](../backend/apps/db/db.py#L1084) (`check_sql_read`) |
| Thêm một loại database | [db/constant.py](../backend/apps/db/constant.py) enum `DB` + [db.py](../backend/apps/db/db.py) + một file `templates/sql_examples/*.yaml` |
| Sửa endpoint hỏi đáp | [chat.py:461](../backend/apps/chat/api/chat.py#L461) |
| Sửa quản trị datasource | [apps/datasource/api/datasource.py](../backend/apps/datasource/api/datasource.py) |
| Sửa auth / token | [apps/system/middleware/auth.py](../backend/apps/system/middleware/auth.py) + [apps/system/api/login.py](../backend/apps/system/api/login.py) |
| Đổi cấu hình mặc định | [common/core/config.py](../backend/common/core/config.py) — bảng đầy đủ ở [OPERATIONS.md §2](OPERATIONS.md) |
| Thêm bảng DB mới | model trong `apps/<domain>/models/` + migration trong `alembic/versions/` |
| Sửa cách gọi LLM | [model_factory.py](../backend/apps/ai_model/model_factory.py) |

---

## 5. Data model

### Cụm hội thoại — [chat_model.py](../backend/apps/chat/models/chat_model.py)

| Bảng | Vai trò | Cột đáng chú ý |
|---|---|---|
| `chat` | Một hội thoại | `external_id`, `oid`, `datasource`, `brief`, `origin` (0 web / 1 mcp / 2 assistant) |
| `chat_record` | Một **lượt hỏi** | `question`, `sql`, `data`, `answer`, `chart`, `analysis`, `predict`, `error`, `finish` |
| `chat_log` | Một **thao tác** trong lượt | `operate` (enum 14 giá trị), `messages` (JSONB), `token_usage`, `local_operation`, `error` |

Quan hệ: `chat` 1─n `chat_record` 1─n `chat_log`.

`chat_record.answer` do migration `071_add_chat_record_answer.py` thêm — cột này là của fork, upstream
không có.

`OperationEnum` có 14 giá trị; giá trị `ANSWER='14'` là của fork. `chat_log` là thứ dùng để debug —
xem [OPERATIONS.md §6](OPERATIONS.md).

### `chat.external_id` — vì sao tồn tại

[chat_model.py:104](../backend/apps/chat/models/chat_model.py#L104). Khóa chính `chat.id` khai
`Identity(always=True)`, tức Postgres **luôn tự sinh**, client không chèn giá trị riêng vào được.
Nhưng đối tác tích hợp cần tự đặt định danh hội thoại phía họ. `external_id` là cột UUID riêng,
`UNIQUE` (để hai lần hỏi cùng UUID chắc chắn rơi vào đúng một hội thoại) và `NULL` với hội thoại tạo
từ web (Postgres cho phép nhiều NULL trong ràng buộc UNIQUE).

Việc quy đổi `external_id` → id nội bộ nằm ở `resolve_chat_id`
([curd/chat.py:898](../backend/apps/chat/curd/chat.py#L898)), gọi qua dependency
`resolve_chat_for_question` ([chat.py:436](../backend/apps/chat/api/chat.py#L436)). **Phải là
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

Phase hiện tại **chưa** đọc `ai_user_permissions` trong pipeline Text2SQL — đó là việc của spec sau.

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

`/hooks/*` nằm trong danh sách trắng này ([whitelist.py](../backend/common/utils/whitelist.py)):
`POST /hooks/ai-sync` tự xác thực bằng static token riêng (`Authorization: Bearer
<AI_SYNC_HOOK_TOKEN>`), không dùng JWT `X-SQLBOT-TOKEN`.

### Ba lớp phân quyền

1. **Workspace (`oid`)** — mọi tài nguyên (datasource, terminology, chat) gắn `oid`. Truy vấn luôn
   lọc theo `oid` của user hiện tại.
2. **Permission code** — `require_permissions(SqlbotPermission)`
   ([permission.py:49](../backend/apps/system/schemas/permission.py#L49)) dùng làm FastAPI dependency
   trên route.
3. **Row/column permission** — do `sqlbot_xpack` cung cấp, sinh điều kiện `WHERE` bổ sung chèn vào
   SQL của LLM (`generate_filter`, [llm.py:1238](../backend/apps/chat/task/llm.py#L1238)).

**Admin `id=1` bỏ qua lớp 3.** `is_normal_user` chỉ là `current_user.id != 1`
([permission.py:89](../backend/apps/datasource/crud/permission.py#L89)). Test bằng tài khoản admin
sẽ **không** thấy row-permission chạy — đây là nguồn nhầm lẫn kinh điển.

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
([llm.py:477](../backend/apps/chat/task/llm.py#L477)). Không có license hợp lệ thì các nhánh đó im
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
- [DATASOURCE_API_SPEC.md](../backend/scripts/chat_stream_demo/DATASOURCE_API_SPEC.md) — 23 endpoint
  quản trị datasource, cho cả database quan hệ (§3–§7) lẫn file Excel/CSV (§8).
- [AI_SYNC_HOOK_API_SPEC.md](../backend/scripts/ai_sync_hook/AI_SYNC_HOOK_API_SPEC.md) — cổng nhận
  bản tin đồng bộ từ hệ thống SW (`POST /hooks/ai-sync`).

Swagger: `GET /docs` (bật/tắt bằng `SQLBOT_DOC_ENABLED`), có i18n qua `?lang=zh|en`.
