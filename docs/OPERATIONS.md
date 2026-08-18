# Vận hành: chạy, cấu hình, deploy, đo, debug

> Tài liệu này gom mọi thứ nằm ngoài luồng code: chạy local, biến môi trường, deploy, harness đánh
> giá, cách debug một lượt hỏi, và **danh sách bẫy đã biết** (§7 — đọc trước khi làm việc với API
> datasource).

---

## 1. Chạy local

### Yêu cầu

- **Python 3.11** đúng minor version (`requires-python = "==3.11.*"` trong `backend/pyproject.toml`).
- `uv` để quản lý dependency.
- PostgreSQL có extension **pgvector** + Redis (tùy chọn). `docker-compose.service.yml` dựng sẵn cả
  hai: Postgres `pgvector/pgvector:pg16` ở cổng **5434**, Redis ở **6380**.

### `.env` nằm ở **repo root**, không phải trong `backend/`

`config.py` khai `env_file="../.env"` ([config.py:27](../backend/common/core/config.py#L27)) — đường
dẫn tương đối so với thư mục làm việc `backend/`. Đặt `.env` trong `backend/` thì nó bị bỏ qua im
lặng. File này nằm trong `.gitignore`.

Ba nhóm giá trị **bắt buộc sửa khi chạy local**:

| Nhóm | Vì sao |
|---|---|
| `BASE_DIR`, `UPLOAD_DIR`, `EXCEL_PATH`, `MCP_IMAGE_PATH`, `LOCAL_MODEL_PATH` | Mặc định là `/opt/sqlbot/...` (dành cho container chạy root). User thường không ghi được, và `sqlbot_xpack` gọi `mkdir(UPLOAD_DIR)` **ngay lúc import** → `PermissionError` trước cả khi app chạy |
| `EMBEDDING_PROVIDER=api` + `EMBEDDING_API_*` | Mặc định `local` nạp `shibing624/text2vec-base-chinese` từ `LOCAL_MODEL_PATH`; model đó chỉ có trong image Docker. Chạy local mà để `local` thì mỗi lần lưu/sync datasource ném `FileNotFoundError` |
| `POSTGRES_PASSWORD` | Ghi mật khẩu **thô, không URL-encode sẵn**: `config.py` đã bọc `urllib.parse.quote()`. Điền sẵn `%40` là mã hóa hai lần và Postgres từ chối đăng nhập |

### Lệnh

```bash
# hạ tầng
docker compose -f docker-compose.service.yml up -d

# backend
cd backend
uv sync --extra cpu          # KHÔNG chạy `uv sync` trần — xem ghi chú dưới
uv run uvicorn main:app --host 0.0.0.0 --port 8000 --reload

# frontend
cd frontend && npm install && npm run dev     # cổng 5173
```

### `--extra cpu` là **bắt buộc**

Upstream đã chuyển `torch` sang `[project.optional-dependencies]`
([pyproject.toml:62](../backend/pyproject.toml#L62)), chia thành hai extra `cpu` và `cu128` — và khai
chúng là **conflicting** trong `[tool.uv]`, nên `uv` không tự chọn hộ. `uv sync` trần **không cài
torch**, mà `apps/ai_model/embedding.py` import `langchain_huggingface` ngay đầu file và chính lệnh
import đó nạp torch → chết lúc khởi động, **bất kể `EMBEDDING_PROVIDER` đặt là gì**.

Dùng `--extra cu128` thay cho `cpu` nếu thật sự cần chạy embedding trên GPU — wheel CUDA nặng thêm
vài GB. Không dùng cả hai cùng lúc.

`torch` cũng là lý do `uv sync` lần đầu tải ~1,7GB. `Dockerfile.backend` vì thế tách làm hai lần sync
để layer dependency chỉ rebuild khi `pyproject.toml`/`uv.lock` đổi.

**Không có bước migrate thủ công**: `lifespan` gọi `alembic upgrade head` mỗi lần khởi động
([main.py:66](../backend/main.py#L66)).

---

## 2. Biến cấu hình

Nguồn duy nhất: [common/core/config.py](../backend/common/core/config.py). Bảng dưới nhóm theo **tác
dụng thật**, không theo thứ tự trong file.

### 2.1. Retry và hạ cấp (fork thêm)

| Biến | Mặc định | Tác dụng |
|---|---|---|
| `LLM_SQL_MAX_RETRY` | `1` | Số lần sinh lại SQL trong **cùng một lượt** khi pha SQL hỏng. Mỗi lần tốn thêm một vòng gọi LLM. `0` để tắt |
| `LLM_ANSWER_ON_FAILURE` | `True` | Hết retry vẫn hỏng thì trả lời bằng lời dựa trên lịch sử thay vì phát event `error` |
| `LLM_ANSWER_FALLBACK_ROUNDS` | `3` | Số lượt hỏi-đáp cũ đưa vào prompt fallback |
| `LLM_ANSWER_FALLBACK_ROWS` | `20` | Số dòng dữ liệu tối đa của **mỗi** lượt cũ trong prompt fallback |

### 2.2. Pha sinh biểu đồ

| Biến | Mặc định | Tác dụng |
|---|---|---|
| `GENERATE_CHART_ENABLED` | `True` | Chạy pha sinh biểu đồ sau pha answer trên `POST /chat/question`. Thực chất là chọn `finish_step` mặc định của endpoint đó: `True` → `GENERATE_CHART`, `False` → `GENERATE_ANSWER` |

Cái giá khi bật: thêm **một** lượt gọi LLM cho mỗi câu hỏi (2 → 3). Lượt này chạy song song với pha
answer nên độ trễ tới `finish` tăng ít hơn nhiều so với chi phí token.

Không đụng tới nhánh MCP (tự truyền `QUERY_DATA`) lẫn nhánh hạ cấp — không có dữ liệu thì biểu đồ vô
nghĩa, `run_task` bỏ qua bất kể biến này.

### 2.3. Lịch sử hội thoại

| Biến | Mặc định | Tác dụng |
|---|---|---|
| `GENERATE_SQL_QUERY_HISTORY_ROUND_COUNT` | `5` | Số lượt phát lại vào prompt sinh SQL |
| `LLM_SQL_HISTORY_ANSWER_MAX_CHARS` | `4000` | Trần ký tự cho **một** câu trả lời khi nhập vào lịch sử. Chặn ca model lặp vô hạn (đã gặp 94.731 ký tự) làm vỡ context window vĩnh viễn |
| `LLM_SQL_HISTORY_TOTAL_MAX_CHARS` | `24000` | Trần ký tự cho **toàn bộ** phần lịch sử. Chặn bằng ký tự chứ không chỉ bằng số lượt, vì m-schema đã ăn sẵn ~24K |
| `LLM_SQL_HISTORY_JSON_ONLY` | `False` | Chỉ giữ khối JSON của pha SQL trong lịch sử, bỏ văn xuôi suy luận. **Đang được đo bằng bộ test HĐND — đừng đổi mặc định mà không kèm số liệu** |
| `LLM_ANSWER_HISTORY_ENABLED` | `True` | Đưa lịch sử vào cả nhánh answer **bình thường**, không chỉ fallback |
| `LLM_ANSWER_HISTORY_ROUNDS` / `_ROWS` | `3` / `20` | Ngân sách lịch sử cho nhánh answer thường (tách riêng khỏi `FALLBACK_*` để A/B được) |

### 2.4. Thinking

| Biến | Mặc định | Tác dụng |
|---|---|---|
| `LLM_DISABLE_THINKING` | `True` | Tắt pha reasoning. Model như Qwen3 có thể kẹt trong vòng lặp suy luận, đốt hết token mà không xuất `content` nào → pipeline nhận chuỗi rỗng rồi chết |
| `LLM_DISABLE_THINKING_EXTRA_BODY` | `{"chat_template_kwargs": {"enable_thinking": false}}` | Payload tiêm vào `extra_body`. Chuẩn vLLM/SGLang. API kiểu DashScope cần `{"enable_thinking": false}` — **đổi biến này, đừng sửa code** |
| `PARSE_REASONING_BLOCK_ENABLED` | `True` | Tách khối `<think>…</think>` khỏi `content` |

### 2.5. Embedding và top-K

| Biến | Mặc định | Tác dụng |
|---|---|---|
| `EMBEDDING_ENABLED` | `True` | Công tắc tổng |
| `EMBEDDING_PROVIDER` | `local` | `local` = HuggingFace tại `LOCAL_MODEL_PATH`; `api` = `OpenAIEmbeddings` |
| `EMBEDDING_API_BASE_URL` / `_KEY` / `_MODEL` | — | Cấu hình cho `provider=api` |
| `TABLE_EMBEDDING_ENABLED` / `TABLE_EMBEDDING_COUNT` | `True` / `10` | Top-K bảng đưa vào M-Schema |
| `DS_EMBEDDING_COUNT` | `10` | Top-K datasource khi LLM tự chọn |
| `EMBEDDING_DEFAULT_SIMILARITY` | `0.4` | Ngưỡng cosine; override bằng `EMBEDDING_TERMINOLOGY_SIMILARITY` / `EMBEDDING_DATA_TRAINING_SIMILARITY` |
| `EMBEDDING_DEFAULT_TOP_COUNT` | `5` | Top-K thuật ngữ / ví dụ SQL; override tương tự |

**Đổi model embedding thì phải re-embed toàn bộ** (`scripts/eval_text2sql/21_reembed.py`), nếu không
vector cũ và mới không cùng không gian.

### 2.6. Giới hạn dòng

| Nơi | Giá trị | Ghi chú |
|---|---|---|
| `GENERATE_SQL_QUERY_LIMIT_ENABLED` | `True` | Ép LLM thêm `LIMIT` vào SQL |
| `ANSWER_MAX_ROWS` (**hằng số trong code**, [llm.py:75](../backend/apps/chat/task/llm.py#L75)) | `100` | Trần số dòng nhồi vào prompt answer. Sửa thì **bắt buộc** giữ nguyên cặp với `build_data_scope_note` |

### 2.7. Bảo mật và mạng

| Biến | Mặc định | Tác dụng |
|---|---|---|
| `SECRET_KEY` | `secrets.token_urlsafe(32)` | Khóa ký JWT. **Không đặt cố định thì mọi token chết sau mỗi lần restart process** |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `11520` (8 ngày) | Không có refresh endpoint |
| `SQLBOT_ALLOW_METADATA_QUERIES` | `False` | Cho phép `SHOW/DESCRIBE/DESC/EXPLAIN`. Bật là lộ cấu trúc DB |
| `CORS_ALLOW_ALL_ORIGINS` | `False` | Không khai được bằng `BACKEND_CORS_ORIGINS='*'` vì kiểu là `AnyUrl`. Bật cờ này thì `main.py` dùng `allow_origin_regex='.*'`, vẫn hoạt động với credentials |
| `BACKEND_CORS_ORIGINS` | `[]` | Danh sách origin, ngăn bằng dấu phẩy. Starlette so khớp **chuỗi chính xác** — khác scheme/host/port là bị chặn, không có wildcard theo domain |
| `SQLBOT_DOC_ENABLED` | `True` | Bật `/docs` và `/openapi.json` |

### 2.8. Nạp Excel bất đồng bộ và callback

Chỉ ảnh hưởng `POST /datasource/createFromExcelAsync` và ba vòng nền đi kèm (worker nạp, vòng gửi
callback, vòng quét phục hồi) — xem [BACKEND_ARCHITECTURE.md §5](BACKEND_ARCHITECTURE.md).

| Biến | Mặc định | Tác dụng |
|---|---|---|
| `AI_CALLBACK_URL` | `''` | URL nhận callback báo kết quả. **Để rỗng là tắt hẳn việc gửi** — job vẫn nạp, thư vẫn nằm chờ trong bảng, hệ ngoài không bao giờ biết kết quả |
| `AI_CALLBACK_ACTION_TYPE` | `999` | Mã sự kiện trong phong bì `{actionType, payload}`. Do đối tác quy định |
| `AI_CALLBACK_AUTH_HEADER` | `''` | Header xác thực dạng `"Tên: giá trị"`. Đối tác hiện không yêu cầu xác thực |
| `AI_CALLBACK_TIMEOUT` | `15` | Timeout mỗi lần gửi (giây). **Không được bỏ**: thư viện HTTP mặc định chờ vô hạn |
| `AI_CALLBACK_MAX_ATTEMPTS` | `8` | Thử đủ số lần này mà vẫn hỏng thì thư chuyển `dead` và **dừng hẳn** |
| `AI_CALLBACK_BACKOFF_BASE_SECONDS` / `_CAP_SECONDS` | `10` / `1800` | Giãn cách thử lại nhân đôi mỗi lần hỏng, chặn trên 30 phút |
| `AI_CALLBACK_POLL_SECONDS` | `10` | Chu kỳ vòng gửi quét hàng đợi |
| `AI_CALLBACK_WORKERS` / `AI_CALLBACK_BATCH_SIZE` | `4` / `20` | Số thư gửi song song và số thư nhặt mỗi vòng |
| `EXCEL_IMPORT_WORKERS` | `4` | Số job nạp chạy song song. Pool **riêng**, không dùng chung 200 thread của embedding: mỗi job giữ cả dataframe trong RAM |
| `EXCEL_IMPORT_HEARTBEAT_SECONDS` / `EXCEL_IMPORT_STALE_SECONDS` | `30` / `180` | Chu kỳ worker báo còn sống, và ngưỡng coi job là đã chết. Ngưỡng phải là **bội số vài lần** của chu kỳ |
| `EXCEL_IMPORT_LOCK_TIMEOUT_MS` | `5000` | Tổng thời gian chờ khóa chống trùng tên, gom từ nhiều lần thử-rồi-ngủ. Hết hạn trả `409 DATASOURCE_NAME_LOCKED` thay vì xếp hàng vô hạn |
| `EXCEL_IMPORT_RECOVERY_SECONDS` | `60` | Chu kỳ vòng quét phục hồi |
| `EXCEL_IMPORT_TEMP_TTL_HOURS` | `24` | File tạm quá hạn này mà không job nào đang dùng thì xóa. Đặt dài vì thư mục dùng chung với luồng `/parseExcel` → `/importToDb` của web, nơi người dùng để giữa chừng khá lâu |

**Tải file nguồn từ URL.** `createFromExcelAsync` không nhận bytes nữa: hệ ngoài đưa một presigned
URL, worker tự tải. Nhóm biến này chi phối bước tải đó
([remote_file.py](../backend/apps/datasource/utils/remote_file.py)).

| Biến | Mặc định | Tác dụng |
|---|---|---|
| `EXCEL_DOWNLOAD_ALLOWED_HOSTS` | `''` | Danh sách host được phép tải về, ngăn cách bằng dấu phẩy. **Rỗng = chặn tất cả**, không phải cho phép tất cả — xem bẫy ở §7.3. Ghi được cả `host`, `host:port` lẫn URL đầy đủ |
| `EXCEL_DOWNLOAD_MAX_MB` | `100` | Trần dung lượng file nguồn. Ép **hai lần**: theo `Content-Length` khai báo, rồi theo số byte thật đếm trong lúc tải |
| `EXCEL_DOWNLOAD_MIN_TTL_SECONDS` | `300` | Hạn chữ ký còn lại tối thiểu để nhận việc. Job có thể xếp hàng sau job khác nên URL sắp hết hạn phải bị từ chối ngay chứ không để hỏng ở nền. **Đối tác được yêu cầu ký ≥ 1 giờ** |
| `EXCEL_DOWNLOAD_CONNECT_TIMEOUT` / `_READ_TIMEOUT` | `5` / `30` | Timeout bắt tay và timeout giữa hai lần đọc |
| `EXCEL_DOWNLOAD_TOTAL_TIMEOUT` | `1800` | Trần cho **cả** lượt tải. Không thừa: timeout đọc chỉ bắt được kết nối đứng im, một nguồn nhỏ giọt đều đặn sẽ giữ worker vô hạn mà không lần đọc nào quá hạn |
| `EXCEL_DOWNLOAD_RETRIES` | `2` | Số lần tải lại, **chỉ** với lỗi mạng và `5xx`. Mọi `4xx` là câu trả lời dứt khoát, thử lại vô nghĩa và chỉ đẩy job tới gần hạn chữ ký hơn |
| `EXCEL_DOWNLOAD_PROBE_ENABLED` / `_PROBE_TIMEOUT` | `true` / `3` | Hỏi nguồn 1 byte ngay trong pha đồng bộ để bắt sớm `403`/`404`/quá cỡ. Tắt đi thì các lỗi đó lùi xuống callback chứ không mất |

**Bật callback ở deployment Docker**: `docker-compose.backend.yml` truyền `AI_CALLBACK_URL` xuống
container, giá trị lấy từ file `.env` **cạnh compose file**. Chạy backend thẳng trên host thì đặt
trong `.env` ở repo root như mọi biến khác. Điền URL rồi restart là các thư đang chờ tự đi — không
cần bắn lại tay, và không lần thử nào bị đốt trong lúc URL còn rỗng.

Chỉ `AI_CALLBACK_URL` là bắt buộc; các biến còn lại có mặc định dùng được ngay.

### 2.9. Tài liệu `.docx` đính kèm câu hỏi

Chi phối trường `fileUrl` của `POST /chat/question` — xem
[TEXT2SQL_PIPELINE.md §10](TEXT2SQL_PIPELINE.md).

| Biến | Mặc định | Tác dụng |
|---|---|---|
| `CHAT_DOC_MAX_MB` | `15` | Trần dung lượng file. Ép trên **số byte thật** đếm trong lúc tải, không tin `Content-Length`. Thấp hơn hẳn trần Excel vì file tải thẳng vào **RAM** (client đang chờ trên connection, không có worker nền để ghi đĩa) — trần này nhân với số request đồng thời chính là chi phí RAM |
| `CHAT_DOC_DOWNLOAD_TIMEOUT` | `30` | Trần cho cả lượt tải. Người dùng đang ngồi đợi, không có chỗ nào để lùi lại làm sau |
| `CHAT_DOC_EXTRACT_MAX_CHARS` | `200000` | Trần ký tự lúc trích, ép **trong lúc duyệt** chứ không cắt sau — docx là file zip, không chặn sớm thì một file 1 MB bung ra hàng trăm MB text. Đây cũng là bản được lưu xuống `chat_attachment` |
| `CHAT_DOC_PROMPT_MAX_CHARS` | `30000` | Phần tài liệu đưa vào prompt của **chính lượt đính kèm** |
| `CHAT_DOC_HISTORY_MAX_CHARS` | `10000` | Phần tài liệu đưa vào prompt của các **lượt sau**, qua lịch sử answer |

**Không có biến allowlist riêng**: dùng chung `EXCEL_DOWNLOAD_ALLOWED_HOSTS` ở §2.8 vì cùng trỏ về
một MinIO. Rỗng vẫn là **chặn tất cả** — chưa khai host thì tính năng đính kèm coi như chưa mở, mọi
`fileUrl` trả `400 URL_HOST_NOT_ALLOWED`. Timeout bắt tay và timeout đọc cũng lấy từ nhóm
`EXCEL_DOWNLOAD_*`; riêng `EXCEL_DOWNLOAD_RETRIES` **không** áp dụng — luồng này không thử lại, việc
người dùng bấm gửi lại chính là vòng retry.

Hai trần cuối ảnh hưởng trực tiếp tới prompt nhưng **chưa được đo bằng harness** (§4) — đổi thì phải
đo lại chứ đừng tin cảm giác.

### 2.10. Khác

`CACHE_TYPE` (`memory`/`redis`/`None`), `CACHE_REDIS_URL`, `LOG_LEVEL`, `LOG_DIR`, `SQL_DEBUG`,
`PG_POOL_SIZE`/`PG_MAX_OVERFLOW`/`PG_POOL_RECYCLE`/`PG_POOL_PRE_PING` (pool của **DB metadata**),
`ORACLE_CLIENT_PATH`, `CONTEXT_PATH` (prefix URL, mặc định rỗng).

Biến bool nhận cả `"true"`/`"True"`/`"false"` nhờ validator `lowercase_bool`
([config.py:277](../backend/common/core/config.py#L277)).

---

## 3. Deployment

Ba đường, chọn theo mục đích:

### 3.1. `docker-compose.backend.yml` — đường đang dùng cho đối tác

Chỉ container backend; Postgres/Redis là service bên ngoài.

```bash
docker compose -f docker-compose.backend.yml up -d --build
```

Build từ `Dockerfile.backend`, khác `Dockerfile` (all-in-one của upstream) ở bốn điểm, tất cả đều
cố ý:

- Base `python:3.11-slim`, không nhúng Postgres 17 vào cùng container.
- **Không build frontend và g2-ssr** — container này chỉ phục vụ REST/SSE. Pha sinh biểu đồ đã bỏ
  nên g2-ssr thành vô dụng.
- **Không nhúng model embedding ~2GB**; dùng `EMBEDDING_PROVIDER=api`.
- Không tải Oracle instant client / thư viện DM: `oracledb` chạy thin mode, wheel `dmpython` đã
  bundle sẵn `libdmdpi`.

Build cần ra được mạng tới ba host mà `uv.lock` ghim URL tuyệt đối: `mirrors.aliyun.com`,
`test.pypi.org` (sqlbot-xpack), `download.pytorch.org` (torch CPU). Muốn đổi index thì phải sửa
`[[tool.uv.index]]` trong `pyproject.toml` rồi `uv lock` lại — `--frozen` đọc URL trong lock.

Hai điều bắt buộc trong compose file:

**`SECRET_KEY` phải đặt cố định.** Compose khai `${SECRET_KEY:?...}` để fail sớm. Quote cả giá trị:
dấu `:` trong thông báo lỗi của `${VAR:?msg}` làm YAML parser hiểu thành mapping.

**Dùng named volume, KHÔNG bind-mount** cho `/opt/sqlbot/data` và `/opt/sqlbot/images`. Container
chạy uid 1000 (không phải root):

- Named volume: lần đầu Docker copy nội dung từ image vào volume, **giữ nguyên owner uid 1000** →
  ghi được ngay, và có sẵn cả `file/` lẫn `excel/`.
- Bind-mount: Docker **không** copy nội dung image vào; thư mục host thiếu thì daemon tự tạo, trên
  Docker Engine thuần thành `root:root` → uid 1000 hết ghi được. Ngoài ra `excel/` sẽ không tồn tại
  vì chỉ `UPLOAD_DIR` được `mkdir` lúc import.

Vẫn muốn bind-mount thì phải tự tạo và `chown 1000:1000` **trước khi** `up`.

**Trước khi deploy bản có migration `076`**, đếm số dòng nó sẽ backfill:

```sql
SELECT count(*) FROM core_datasource WHERE status IS NULL OR status = '';
```

Migration đặt các dòng đó thành `Success`. Trên máy dev con số là 0 nên bước này chưa bao giờ được
kiểm chứng với dữ liệu thật; production trả về số lớn thì phải xem qua danh sách trước — nguồn dữ
liệu nào thực sự hỏng mà bị đánh dấu `Success` sẽ quay lại danh sách hỏi đáp.

`docker/backend-entrypoint.sh` chờ Postgres (và Redis nếu `CACHE_TYPE=redis`) mở cổng trước khi gọi
uvicorn — vì `lifespan` chạy `alembic upgrade head` ngay lúc start, DB chưa sẵn sàng thì process
chết luôn. Timeout `SQLBOT_WAIT_TIMEOUT` (mặc định 120s). Uvicorn chạy với `--proxy-headers
--forwarded-allow-ips='*'` để sau reverse proxy vẫn lấy đúng IP client và scheme.

### 3.2. Image all-in-one của upstream

`Dockerfile` + `docker-compose.yaml`, hoặc lệnh `docker run` một dòng trong `README.md`. Gộp
Postgres + backend + frontend + g2-ssr vào một container. Dùng khi cần bản đầy đủ có web UI.
Thư mục `installer/` là bộ cài offline của upstream.

### 3.3. Dev

`docker-compose.service.yml` (Postgres + Redis) + chạy uvicorn/vite trên host — xem §1.
`docker-compose.data.yml` dựng các Postgres chứa **dữ liệu nghiệp vụ để test** (Lào Cai), cổng
5436/5437 — không liên quan gì tới DB metadata của SQLBot.

---

## 4. Harness đánh giá

Hướng dẫn đầy đủ: [scripts/eval_text2sql/README.md](../backend/scripts/eval_text2sql/README.md).
Phần dưới chỉ là những gì cần biết để quyết định *khi nào phải chạy*.

### Hai bộ test độc lập, dùng chung phần chấm điểm

| | **DataLaoCai** (v1/v2) | **HĐND Hà Nội** (v3) |
|---|---|---|
| Driver | `run_eval.py` | `run_eval_http.py` |
| Cách gọi | in-process, import thẳng `LLMService` | **HTTP** `POST /chat/question` (SSE) |
| Datasource | id=5, 8 bảng, có JOIN | id=1, 2 bảng, không JOIN |
| Multi-turn | ✗ | ✓ tối đa 5 lượt chung `chat_id` |

Chọn bộ nào: cần đo **đúng bề mặt đối tác gọi** (kèm lỗi tầng API, hội thoại nhiều lượt) → HĐND;
cần đo sâu vào pipeline hoặc dừng sớm theo `finish_step` → DataLaoCai.

```bash
cd backend
.venv/bin/python scripts/eval_text2sql/run_eval_http.py              # bộ HĐND, toàn bộ
.venv/bin/python scripts/eval_text2sql/run_eval_http.py --only S1,M1
.venv/bin/python scripts/eval_text2sql/run_eval.py --finish-step GENERATE_SQL   # rẻ nhất
```

Harness **chỉ đọc**, không tự tạo datasource/model. Cần sẵn trong DB metadata: datasource đã sync
bảng và có embedding, một `AiModelDetail` với `default_model=true`, endpoint embedding chạy được.

### Ba chỉ số riêng của bộ HĐND

- **Hội thoại đúng trọn vẹn** — sai một lượt là cả mạch hỏng với người dùng, sát thực tế hơn EX
  trung bình theo lượt.
- **Nhiễm ngữ cảnh** — mỗi lượt khai `context_dependency: independent` chạy **hai lần**: trong hội
  thoại (có lịch sử nhiễu) và trong `chat_id` trắng. Lượt đứng riêng thì đúng mà trong hội thoại lại
  sai chính là bằng chứng lịch sử làm hỏng câu hỏi vốn tự đủ nghĩa. Không có lần đối chứng thì không
  tách được "model dốt" khỏi "model bị lịch sử kéo lệch".
- **Câu trả lời không bịa số** — chỉ số **chẩn đoán**, số model tự tính (tỷ lệ %, hiệu hai dòng)
  cũng bị gắn cờ nên phải nhìn danh sách rồi tự phán xét. Cần vì theo `API_SPEC.md` §7.5 bên thứ ba
  **chỉ** nhận `answer` và `sql`, không nhận bảng số liệu — SQL đúng mà câu chữ bịa số thì với họ
  vẫn là trả lời sai.

### Khi nào **bắt buộc** chạy lại

- Đổi bất kỳ biến `LLM_*` nào.
- Sửa `templates/template.yaml` hoặc `templates/sql_examples/*.yaml`.
- Sửa `init_messages`, `build_answer_prompts`, `generate_sql`, hoặc vòng retry.
- Đổi model LLM hoặc model embedding.

### Kết quả đo gần nhất

Đợt `2026-07-30`, bộ HĐND qua HTTP (32 câu chính + 3 lượt đối chứng), chi tiết ở
`BaoCao_DanhGia_Text2SQL_HDND.md`:

| Chỉ số | Kết quả |
|---|---:|
| Execution Accuracy | 71.9% |
| Table usage recall | 100% |
| SQL sạch (lint) | 100% |
| Câu trả lời không bịa số | 65.6% |
| EX lượt 1 / lượt ≥2 | 66.7% / 77.8% |
| Latency p50 / p95 | 5,92s / 8,68s |
| Lỗi tầng API · phải retry · SQL chạy không nổi | 0 · 0 · 0 |

Đánh giá theo góc nhìn người dùng (`BaoCao_ChatLuongTraLoi_HDND_BA.md`, đợt 2026-07-29): 24/32 câu
(75%) trả lời dùng được ngay.

Các báo cáo cũ hơn cho bộ DataLaoCai: `BaoCao_DanhGia_Text2SQL_SQLBot.md` (64 câu) và `..._v2.md`.

---

## 5. Log

File log ở `backend/logs/`: `debug.log` (có xoay vòng `.1`…`.4`), `info.log`, `warn.log`,
`error.log`. Cấu hình bằng `LOG_LEVEL`, `LOG_DIR`, `LOG_FORMAT`.

Những gì `run_task` ghi ra log và **không** phát thành event SSE:

- Toàn văn SQL model sinh ra, trước và sau khi qua kiểm duyệt.
- `SQL phase failed (attempt N/M), retrying: <lý do>` — dấu vết **duy nhất** của lần retry.
- `SQL phase failed after N retry, falling back to answer-only: <lý do>` — dấu vết duy nhất của
  lượt hạ cấp (cố ý không ghi vào `chat_record.error`).

`SQL_DEBUG=True` bật echo SQL của SQLAlchemy cho DB metadata.

---

## 6. Debug một lượt hỏi

### Lần từ `record_id`

Client nhận event `{"type":"id", "id": <record_id>}` ngay đầu stream. Từ đó:

```
GET /api/v1/chat/record/{record_id}/log      # toàn bộ bước xử lý
GET /api/v1/chat/record/{record_id}/usage    # chỉ token usage
```

([chat.py:195](../backend/apps/chat/api/chat.py#L195) và [chat.py:213](../backend/apps/chat/api/chat.py#L213))

### Đọc thẳng bảng `chat_log`

Một dòng cho mỗi thao tác, phân biệt bằng cột `operate` (`OperationEnum`). Cột quan trọng:

| Cột | Nội dung |
|---|---|
| `messages` (JSONB) | **Đây là chỗ có giá trị nhất.** Xem §"hai bản messages" bên dưới |
| `reasoning_content` | Phần thinking (thường rỗng vì đã tắt) |
| `token_usage` | Số token vào/ra |
| `local_operation` | `True` = bước không gọi LLM (chọn bảng, lọc thuật ngữ, chạy SQL) |
| `error` | Bước này có hỏng không |
| `pid` | Trỏ về `record_id` |

### Hai bản `messages` — đừng nhầm

| Bản | Ghi lúc | Nội dung |
|---|---|---|
| `start_log` | Trước khi gọi LLM | **Prompt thật** đã gửi đi, gồm cả m-schema đầy đủ và — nếu là lần retry — cả câu trả lời hỏng lẫn lời nhắc sửa |
| `end_log` | Sau khi LLM trả lời | **Lịch sử sạch** (`self.sql_message`): mỗi lượt đúng một cặp (câu hỏi, đáp án dùng được), đã cắt theo `LLM_SQL_HISTORY_*` |

Bản `end_log` chính là thứ lượt sau đọc để phát lại. Muốn biết *"LLM thật sự nhìn thấy gì"* → đọc
`start_log`. Muốn biết *"lượt sau sẽ nhớ gì"* → đọc `end_log`.

### `chat_record`

Kết quả cuối của lượt: `sql`, `data`, `answer`, `error`, `finish`. Lượt **hạ cấp** có `answer` khác
rỗng nhưng `sql` và `data` rỗng, và `error` **cũng rỗng** — đó là dấu nhận biết.

### Debug một lần đồng bộ của AI Sync Hook

Dựng lại một lần gọi `POST /hooks/ai-sync` bằng cách truy vấn `ai_sync_hook_logs` theo
`idempotency_key` (client gửi trong header, log lại phía SW) hoặc theo `user_id`:

```sql
SELECT status, action_type, sync_version, error_code, error_message, received_at, processed_at
FROM ai_sync_hook_logs
WHERE idempotency_key = '...' OR user_id = '...'
ORDER BY received_at DESC;
```

`sync_version` là mốc **server tự sinh** tại thời điểm xử lý (epoch millis), không còn phản ánh
`timestamp` do SW khai báo — SW không còn gửi trường đó nữa. Chỉ dùng để biết thứ tự áp dụng thật
trên SQLBot, không dùng để suy ra bản tin phía SW gửi lúc nào.

`request_payload` (JSONB) là body thô SW đã gửi — dùng để tái hiện request khi cần. Trạng thái quyền
hiện tại (sau khi đã áp snapshot) nằm ở `ai_user_permissions`, lọc theo `user_id`.

Bản tin `DATASOURCE_SYNC` (`action_type = 4`) **không có `user_id`** — cột đó NULL, nên chỉ lần được
theo `idempotency_key` hoặc quét theo loại bản tin:

```sql
SELECT status, error_code, request_payload->'payload'->'datasourceIds' AS ds_ids, received_at, processed_at
FROM ai_sync_hook_logs WHERE action_type = 4 ORDER BY received_at DESC LIMIT 20;
```

Dòng log chỉ nói request đã chạy xong hay hỏng ở tầng cấu trúc; kết cục của **từng nguồn** nằm trong
body trả về chứ không lưu lại, nên nguồn nào đồng bộ trượt thì phải tra tiếp `warn.log`/`error.log`
theo chuỗi `[ai-sync] DATASOURCE_SYNC`.

### Vì sao người dùng này không thấy bảng đó

Quyền dữ liệu theo user (xem [TEXT2SQL_PIPELINE.md §13](TEXT2SQL_PIPELINE.md)) không phát event
riêng nào, nên chẩn đoán đi theo ba bước dưới đây. `user_id` chính là `account` của tài khoản
SQLBot đang đăng nhập.

```sql
-- 1. User có dòng quyền nào không? Không có dòng nào = KHÔNG bị siết (chạy như trước).
SELECT database_table_name, domain_code, is_admin, form_uuid, queries
FROM ai_user_permissions WHERE user_id = '<account>';

-- 2. Lượt hỏi đó chạy với lĩnh vực nào (NULL = hỏi trên toàn bộ bảng được cấp).
SELECT id, question, domain_code, error FROM chat_record WHERE id = <record_id>;

-- 3. Tên bảng được cấp có khớp CHÍNH XÁC (phân biệt hoa-thường) tên trong schema không.
SELECT table_name FROM core_table WHERE ds_id = <datasource_id>;
```

Bước 3 là chỗ hay hỏng nhất. Lệch hoa-thường thì bảng **không** được cấp quyền, nhưng log có dòng
`[sw_permission] Bảng '<tên SW gửi>' … khớp khi bỏ hoa-thường ('<tên thật>')` nêu đúng cặp tên gần
nhau. `grep sw_permission backend/logs/warn.log` là ra ngay mọi thứ đã bị bỏ qua âm thầm: tên lệch
hoa-thường, scope query hỏng cú pháp, bảng có nhiều scope mâu thuẫn.

---

## 7. Bẫy đã biết

### 7.1. API datasource

| Bẫy | Chi tiết |
|---|---|
| **`source.cell` / `target.cell` phải là số nguyên** | Trong `POST /table_relation/save/{ds_id}`. Truyền chuỗi thì API trả **200 OK, không lỗi, không log**, nhưng quan hệ bị vô hiệu hoàn toàn → mọi câu hỏi cần JOIN sẽ sai. Xem `DATASOURCE_API_SPEC.md` §6.2 |
| **`editTable` / `editField` thiếu `custom_comment` là xóa mất chú thích** | Hai endpoint này ghi đè toàn bộ record, không phải PATCH. Chú thích là đầu vào trực tiếp của M-Schema nên mất là chất lượng SQL tụt ngay |
| **`chooseTables` thay thế toàn bộ danh sách** | Không phải "thêm bảng". Gửi thiếu một bảng đang dùng là bỏ chọn nó |
| **`chooseTables` bỏ chọn bảng nhưng KHÔNG dọn đồ thị quan hệ** | `sync_table` chỉ gọi `seed_table_relation` ([datasource.py:640](../backend/apps/datasource/crud/datasource.py#L640)), mà hàm seed **no-op khi đồ thị đã khác rỗng**. Hệ quả: bỏ chọn bảng để lại node/port/edge trỏ vào id đã xoá, và chọn lại thì bảng mang **id mới** trong khi seed vẫn từ chối chạy. Triệu chứng ở tầng trên là khối `Foreign keys` của M-Schema có dòng `None.None=...` — LLM đọc phải rác đó khi cần JOIN. Không có đường phục hồi từ UI. Hiện chỉ `DATASOURCE_SYNC` (hook actionType 4) là dọn, vì nó gọi thêm `reconcile_table_relation` ([datasource.py:454](../backend/apps/datasource/crud/datasource.py#L454)). Kiểm tồn đọng bằng cách so `table_relation` với `core_table`/`core_field` của cùng `ds_id` |
| **Lỗi thường trả 500 thay vì 403/404** | Không suy ra được nguyên nhân từ status code; phải đọc body |

### 7.2. Rò rỉ thông tin

**`core_datasource.configuration` chứa mật khẩu DB nghiệp vụ** và được trả về nguyên vẹn bởi
`GET /datasource/list` và `GET /datasource/get/{id}`. Bất kỳ tài khoản nào đọc được datasource là
đọc được credential. Cân nhắc trước khi cấp token cho bên thứ ba.

**`.mcp.json` đang được git track và chứa credential thật** của hai server MCP (một DB nghiệp vụ
remote và DB metadata local). File này đã nằm trong lịch sử commit. Đừng chép giá trị của nó vào bất
kỳ tài liệu, log hay issue nào; nếu cần xoay vòng mật khẩu thì phải xử lý cả lịch sử git.

### 7.3. Cấu hình

| Bẫy | Chi tiết |
|---|---|
| `.env` đặt nhầm chỗ | Phải ở **repo root**, không phải `backend/`. Đặt sai thì bị bỏ qua im lặng, app chạy bằng toàn giá trị mặc định |
| `POSTGRES_PASSWORD` bị encode hai lần | Ghi thô, `config.py` tự `quote()` |
| `SECRET_KEY` không đặt | Mọi JWT chết sau mỗi lần restart |
| `BACKEND_CORS_ORIGINS='*'` không dùng được | Kiểu là `AnyUrl`. Muốn mở hết thì đặt `CORS_ALLOW_ALL_ORIGINS=True` |
| Sửa `template.yaml` mà không restart | `apps/template/template.py` có `@cache` |
| Đổi cấu hình model trong DB mà không restart | `LLMFactory.create_llm` có `@lru_cache(maxsize=32)` |
| `EXCEL_DOWNLOAD_ALLOWED_HOSTS` rỗng làm chết **cả** đính kèm `.docx` của chat | Một biến chi phối hai tính năng (§2.9). Triệu chứng phía client là `400 URL_HOST_NOT_ALLOWED` chứ không phải lỗi cấu hình — dễ đi tìm nhầm phía đối tác |

### 7.4. Hành vi pipeline

| Bẫy | Chi tiết |
|---|---|
| Test bằng tài khoản admin thì **không thấy row-permission** | `is_normal_user` chỉ là `current_user.id != 1` |
| Không có license xpack thì các nhánh custom prompt **im lặng không chạy**, không báo lỗi | |
| Một lượt có thể phát **nhiều** event `info: sql generated` | Retry cố ý không phát event riêng |
| Event `sql` và `sql-data` **có thể không xuất hiện** | Lượt hạ cấp không có SQL. Client không được coi chúng là bắt buộc |
| Số liệu nằm ở trường `data` của `sql-data`, **không** phải `content` | `content` vẫn là chuỗi `"execute-success"` — giữ nguyên để client cũ không vỡ |
| Dựng payload `sql-data` **trước** `save_sql_data` → client nhận nhiều dòng hơn bản lưu DB | Bước lưu mới là chỗ cắt xuống 1000 dòng và gắn `limit`. Xem `build_sql_data_payload` |
| Tài liệu `.docx` đính kèm **biến mất** khỏi ngữ cảnh sau vài lượt | Cố ý: nó là message, không phải hạ tầng, nên trôi theo cửa sổ lịch sử như câu hỏi thường. Người dùng báo "hỏi lại thì bot quên file" là đúng thiết kế, không phải bug — xem TEXT2SQL_PIPELINE.md §10 |
| Pha biểu đồ hỏng **không** giết lượt hỏi | Phát `info: chart failed` rồi đi tiếp; vẫn có `answer` + `finish`. Cố ý không gọi `save_error` vì web UI hiện khối lỗi đỏ với mọi record có `error` khác rỗng |
| Lỗi giữa stream vẫn là **HTTP 200** | Phải đọc event `error`, không dựa vào status code |
| Cắt dòng dữ liệu mà không kèm `build_data_scope_note` → LLM bịa số tổng | Xem [TEXT2SQL_PIPELINE.md §6](TEXT2SQL_PIPELINE.md) |
| Không có cơ chế hủy | `AbortController` phía client chỉ ngắt kết nối; backend vẫn chạy tới hết và vẫn tính token |
| Không chạy song song nhiều câu hỏi trên cùng `chat_id` | Xem `API_SPEC.md` §7.4 |

### 7.5. AI Sync Hook

| Bẫy | Chi tiết |
|---|---|
| `sync_version` không còn chống bản tin lùi | Cột này (và cơ chế `STALE` cũ) đã bỏ hẳn — SW không còn gửi `timestamp`, mọi bản tin `AUTHORIZATION_SYNC` hợp lệ về cấu trúc đều ghi đè bản trước bất kể thứ tự thời gian thật. Đừng đi tìm lại hành vi chống lùi trong code hiện tại |
| `AUTHORIZATION_SYNC` là **full snapshot**, không phải patch | Gửi thiếu một bảng (`databaseTableName`) đang có quyền là **thu hồi** quyền trên bảng đó, không phải "giữ nguyên". `formQueries: []` thu hồi hết. Khoá so khớp là `databaseTableName`, không phải `formUuid` (optional, chỉ tham khảo). Xem `AI_SYNC_HOOK_API_SPEC.md` §4-§5 |
| Gọi hook mà không đăng nhập trước | Không có static token riêng — SW phải `POST /login/access-token` lấy `X-SQLBOT-TOKEN` như mọi client khác, và tài khoản đó phải có quyền `ws_admin` |
| Thiếu quyền `ws_admin` trả **500**, không phải 403 | Quy ước chung của `require_permissions` toàn hệ thống (xem §7.4 / `BACKEND_ARCHITECTURE.md` §7), không riêng gì hook |
| `DATASOURCE_SYNC` trả **200 + `status: SUCCESS`** ngay cả khi **mọi** nguồn trong batch đều hỏng | Ngữ nghĩa per-item, không all-or-nothing như `AUTHORIZATION_SYNC`: `status` cấp request chỉ nói "batch đã xử lý xong". Kết cục thật nằm ở `results[]`. SW dựng monitor theo HTTP status sẽ không bao giờ báo động |
| `DATASOURCE_SYNC` **mở rộng** tập bảng, không giữ nguyên lựa chọn thủ công | Trạng thái đích là toàn bộ schema nguồn, trong khi `chooseTables` là tập do người dùng tick. Một nguồn đang `91/91` có thể thành `102/102` sau đồng bộ — đúng thiết kế, nhưng ai quen nhìn số đó sẽ tưởng có người vào bấm chọn lại |
| Chạy **đồng bộ trong request**, không có hàng đợi nền | Đo được ~15s cho một nguồn PostgreSQL hơn 100 bảng. Batch nhiều nguồn giữ kết nối HTTP rất lâu; timeout phía SW/reverse proxy là thứ hỏng trước tiên |
| Nguồn Excel và nguồn đang `Importing` bị **từ chối per-item** | Excel không có DB nguồn ngoài để đọc lại (đọc "catalog" của nó sẽ trúng PG nội bộ của SQLBot), còn `Importing` nghĩa là worker nền đang ghi metadata song song — chen vào là race |
| DB nguồn trả **0 bảng** thì FAILED chứ không xoá sạch | Hỏng theo hướng đóng, cố ý: 0 bảng gần như luôn là sai schema hoặc mất quyền DB, trong khi hạ tầng bên dưới lại hiểu "danh sách bảng rỗng" là lệnh xoá hết. Muốn xoá thật thì đi đường `chooseTables` |

### 7.6. Nạp Excel bất đồng bộ và các vòng nền

| Bẫy | Chi tiết |
|---|---|
| **Thread nền không có request context** | `require_permissions` lấy user qua `RequestContext.get_request()`, thứ này là contextvar và `executor.submit` **không** truyền contextvar sang thread mới (khác `asyncio.to_thread`). Gọi bất kỳ route handler nào từ worker sẽ ném `RuntimeError` ngay dòng đầu. Vì vậy phần việc nặng phải nằm ở hàm thuần trong `utils/`, worker chỉ gọi hàm thuần |
| **`submit` phải đứng sau `commit`** | Worker mở session riêng; ở mức cô lập READ COMMITTED nó không thấy dòng job của transaction chưa chốt. Submit trước commit thì worker báo "job not found" một cách ngẫu nhiên theo thời điểm |
| **Exception trong `Future` biến mất không dấu vết** | `executor.submit` trả về `Future`; không ai gọi `.result()` thì lỗi bị nuốt hoàn toàn, job đứng im ở `running` cho tới khi vòng phục hồi chôn nó. Mọi điểm vào của worker phải có try/except bọc ngoài cùng |
| **Đừng chờ trong `async def` bằng lời gọi đồng bộ** | Route là coroutine chạy trên event loop dùng chung; một câu lệnh DB đứng chờ vài giây (khóa, truy vấn nặng) chặn **toàn bộ** tiến trình. Đã đo được: bản đầu dùng `pg_advisory_xact_lock` (chờ tới 5s) làm client redis async trong cùng tiến trình văng `TimeoutError` và request trả 500 khi có vài lời gọi cùng tên bắn ra một lúc. Cách chữa: `pg_try_advisory_xact_lock` không chờ, lặp lại với `await asyncio.sleep` — lúc ngủ thì event loop được trả về |
| **`NULL < x` cho UNKNOWN, không phải TRUE** | Mọi điều kiện lọc theo ngưỡng trên cột nullable (`heartbeat_at`, `callback_claimed_at`, `next_attempt_at`, `status`) phải có nhánh `is_(None)` đi kèm. Thiếu nhánh đó thì đúng những dòng cần cứu nhất — dòng chưa kịp ghi mốc thời gian — lại vĩnh viễn không được nhặt. Đã dính hai lần: `usable_ds_condition` và `requeue_stale_callbacks` |
| **`AI_CALLBACK_URL` rỗng thì im lặng không gửi** | Không có log lỗi, không có lần thử nào bị đốt. Job vẫn `success`, thư vẫn nằm chờ trong `excel_import_jobs`, và hệ ngoài chờ mãi. Triệu chứng: `SELECT count(*) FROM excel_import_jobs WHERE callback_status='pending'` tăng dần |
| **Callback là "ít nhất một lần"** | Bên nhận trả 2xx chậm hơn timeout thì lá thư đó vẫn được gửi lại. Không có cách nào biến thành "đúng một lần" — bên nhận phải idempotent theo `externalId` |
| **Nguồn dữ liệu `Failed` tồn đọng, và tự nhân lên** | Nạp hỏng để lại một dòng `core_datasource` trạng thái `Failed`. Nó **không** chiếm tên (`find_conflicting_ds` loại trừ `FAILED`), nên gọi lại cùng tên vẫn ra `202` — chính vì thế mỗi lần thử lại đẻ thêm một dòng nữa, và không có lỗi nào báo cho ai biết. Chúng vẫn hiện trong `GET /datasource/list` vì hàm đó không lọc theo status. Kiểm tra tồn đọng: `SELECT id, name FROM core_datasource WHERE status = 'Failed'` |
| **`EXCEL_DOWNLOAD_ALLOWED_HOSTS` rỗng chặn sạch, không phải mở sạch** | Hỏng theo hướng đóng là cố ý — đây là allowlist chống SSRF, mà mặc định "mở" của một biến chưa ai điền thì nguy hiểm hơn hẳn một endpoint chết. Triệu chứng khi quên điền: **mọi** lời gọi `createFromExcelAsync` trả `400 URL_HOST_NOT_ALLOWED` với thông điệp nói thẳng là chưa cấu hình |
| **URL ký sẵn là credential, không phải đường dẫn** | Chữ ký nằm trong query string và nó được ghi nguyên văn xuống cột `excel_import_jobs.file_url`. Không bao giờ log cả URL, không dán vào issue hay chat: dùng `RemoteSource.safe_label` (host + object key). Hệ quả vận hành: ai đọc được bảng đó là đọc được file nguồn cho tới khi chữ ký hết hạn |
| **Hạn chữ ký tính từ lúc ký, không phải từ lúc worker chạy** | Job xếp hàng sau các job khác, hoặc được vòng phục hồi nhặt lại sau khi tiến trình chết, đều có thể tới lượt tải hàng chục phút sau khi nhận việc. Vì vậy URL được kiểm hạn **lại lần nữa** ngay trước khi tải, và đối tác được yêu cầu ký ≥ 1 giờ. Triệu chứng khi ký quá ngắn: `400 URL_EXPIRED` ở pha đồng bộ, hoặc `DOWNLOAD_FORBIDDEN` trong callback |
| **`status` thành `Failed` trước khi `error_code` được ghi** | `fail_import_job` đổi trạng thái nguồn dữ liệu rồi mới ghi dòng job. Hệ ngoài hỏi `excelImportStatus` đúng khe giữa hai bước sẽ thấy `Failed` kèm `errorCode: null`. Khe này rất hẹp và tự khỏi ở lần hỏi sau, nhưng đủ để một client hỏi liên tục gặp phải |
| **Khối `COPY` chết ở `uploadExcel`** | [datasource.py:563](../backend/apps/datasource/api/datasource.py#L563) có một khối `COPY FROM STDIN` chạy sau `to_sql` nhưng thiếu `seek(0)`, nên nó đọc chuỗi rỗng và nạp 0 dòng. Vô hại đúng vì lỗi đó — ai "sửa" bằng cách thêm `seek(0)` sẽ làm **mọi bảng nhân đôi dữ liệu**. Đường mới (`utils/excel_import.py`) đã bỏ hẳn khối này |
| **`sessionmaker.close()` không đóng pool** | `ConnectionPoolManager` lưu `sessionmaker`, và `sessionmaker` không có `close()` thật — engine bên dưới sống mãi cùng toàn bộ connection. Muốn giải phóng phải `engine.dispose()`. Cùng lớp lỗi với `get_engine_conn()` gọi xong không `dispose` |

Đọc trạng thái hàng đợi khi cần chẩn đoán:

```sql
SELECT status, callback_status, count(*) FROM excel_import_jobs GROUP BY 1, 2;
SELECT ds_id, error_code, callback_attempts, callback_last_error
FROM excel_import_jobs WHERE callback_status = 'dead';
```

Thư đã `dead` đưa lại vào hàng đợi bằng `POST /datasource/resendExcelCallback/{ds_id}` (ẩn khỏi
swagger, cần `ws_admin`) — không cần sửa DB bằng tay.

### 7.7. Script chạy ngoài app: phải `import sqlbot_xpack` trước

`apps/datasource/crud/datasource.py` và `apps/system/schemas/permission.py` import lẫn nhau qua
`sqlbot_xpack` (nó kéo theo `apps/system/api/user.py`). Vòng này vô hại khi chạy bằng `main.py` vì
thứ tự import của app tình cờ đúng, nhưng script tự viết import thẳng module datasource sẽ chết
ngay dòng đầu:

```
ImportError: cannot import name 'get_ws_ds' from partially initialized module
apps.datasource.crud.datasource (most likely due to a circular import)
```

Cách chữa: đặt `import sqlbot_xpack` lên **trước mọi import `apps.*`** trong script. `import
apps.api` **không** chữa được — nó chết cùng kiểu.

Vòng import này cũng chi phối **code trong app**: module nào không nằm sẵn trong cụm datasource mà
cần gọi hàm của `crud/datasource.py` thì phải import **lazy trong thân hàm**, không import ở mức
module. Ví dụ đang có: handler `DATASOURCE_SYNC` gọi `resync_ds_metadata`
([datasource_sync.py:57](../backend/apps/hooks/handlers/datasource_sync.py#L57)). Chạy bằng `main.py`
thì import ở mức module vẫn sống nhờ thứ tự nạp tình cờ đúng, nhưng test nạp riêng cụm `hooks` sẽ vỡ
— tức là lỗi chỉ xuất hiện ở nơi khó liên hệ nhất với nguyên nhân.

### 7.8. Envelope response

**Trả `JSONResponse` trong route KHÔNG giúp thoát envelope `{code, data, msg}`.**

`ResponseMiddleware` có một nhánh `isinstance(response, JSONResponse)` trông như lối thoát
([response_middleware.py:43](../backend/common/core/response_middleware.py#L43)), nhưng nhánh đó
**không bao giờ đúng** trên app thật: mỗi `BaseHTTPMiddleware` gọi `call_next()` sẽ dựng lại response
thành `_StreamingResponse` của Starlette, nên tới lượt `ResponseMiddleware` chạy thì kiểu gốc đã mất.
App có 4 middleware xếp chồng nên điều này luôn xảy ra.

Cách duy nhất trả JSON thô là khai path pattern của route vào `direct_paths`
([response_middleware.py:30](../backend/common/core/response_middleware.py#L30)). Lưu ý so khớp bằng
`route.path_format`, tức phải ghi đúng dạng có tham số (`/foo/{id}`), không phải URL đã điền giá trị.

Hai lý do bẫy này khó phát hiện:

| | |
|---|---|
| Lỗi vẫn "đúng" format | `ResponseMiddleware` bỏ qua mọi response **ngoài dải 2xx**, nên 4xx/5xx thoát envelope sẵn. Test một vài ca lỗi rồi kết luận "không bị bọc" là sai — chỉ nhánh thành công mới lộ vấn đề |
| Test không bắt được | Fixture e2e ở [tests/test_ai_sync_e2e.py:56](../tests/test_ai_sync_e2e.py#L56) chỉ gắn `RequestContextMiddleware` + auth giả, **không** có `ResponseMiddleware` — về cấu trúc không thể phát hiện lớp lỗi này. Thêm endpoint đối tác mới thì phải kiểm bằng request thật vào app đã chạy |

### 7.9. Quản trị người dùng và định danh theo `account`

| Bẫy | Chi tiết |
|---|---|
| **`unique=True` trong model SQLModel không tới được DB** | `sys_user.account` và `sys_user.name` đều khai `unique=True` ([models/user.py:11-13](../backend/apps/system/models/user.py#L11)), nhưng bảng được tạo bằng DDL viết tay ở [001_ddl.py:23](../backend/alembic/versions/001_ddl.py#L23) và **không có ràng buộc duy nhất nào**. Đọc model rồi kết luận DB có ràng buộc là sai — đã sai một lần khi soạn spec |
| `account` duy nhất chỉ nhờ kiểm ở tầng ứng dụng | `check_account_exists` ([crud/user.py:120](../backend/apps/system/crud/user.py#L120)) chạy lúc tạo, không có index duy nhất đỡ phía sau — một check-then-act kinh điển, hai request đồng thời vẫn tạo được hai `account` trùng. Hệ quả nặng hơn bình thường vì `resolve_uid_by_account` dùng `.first()`: có trùng thì nó **im lặng chọn một trong hai**. `name` thì không duy nhất, và không cần duy nhất |
| Route `/user/by-account/*` phải đăng ký **trước** `/user/{id}` | Starlette khớp route theo thứ tự đăng ký, `{param}` ăn trọn một đoạn đường dẫn. Đặt sau thì `DELETE /user/by-account` rơi vào `DELETE /user/{id}` rồi chết ở bước ép kiểu int với thông báo không liên quan tới nguyên nhân. Thêm route `/user/...` mới thì kiểm lại vị trí |
| `status = 0` giết luôn token **đang sống**, không chỉ chặn lần đăng nhập sau | `TokenMiddleware` đọc lại `status` ở **mỗi** request ([auth.py:155](../backend/apps/system/middleware/auth.py#L155), và 125 / 236 cho API key / token nhúng), còn handler đổi trạng thái xoá cache `USER_INFO` nên hiệu lực tức thì. Ngoại lệ duy nhất: luồng SSE đang chạy dở vẫn chạy tới hết, vì middleware chỉ chạy lúc mở request |
| Thừa field trong body thì **bị bỏ qua im lặng**, không 422 | Mặc định `extra='ignore'` của pydantic; không schema nào trong repo bật `extra='forbid'`. Gửi kèm `id` vào `POST /user` cũng bị bỏ, hệ thống vẫn tự sinh id. Ngoại lệ cố ý: object `configuration` của datasource có kiểm tên key và trả 422 ([utils.py:26](../backend/apps/datasource/utils/utils.py#L26)) |

### 7.10. Quyền dữ liệu theo user (đồng bộ từ SW)

Cơ chế đầy đủ ở [TEXT2SQL_PIPELINE.md §13](TEXT2SQL_PIPELINE.md). **Không có biến cấu hình nào**
bật/tắt phần này — quyền có hay không hoàn toàn do dữ liệu trong `ai_user_permissions` quyết định.

| Bẫy | Chi tiết |
|---|---|
| **Không có dòng quyền nào = KHÔNG bị siết** | Đây là mặc định "mở", ngược với allowlist tải file ở §7.6. Cố ý: mọi tài khoản nội bộ SQLBot (web UI, admin, harness eval) đều không có dòng nào và phải chạy như trước. Hệ quả cần nhớ khi test: **thu hồi hết quyền của một user bằng `formQueries: []` là biến user đó thành không bị siết**, không phải cấm sạch |
| **`is_admin=true` phía SW bỏ qua toàn bộ phép sàng** | Một dòng `is_admin` là đủ, không cần dòng nào khác. Test phân quyền bằng tài khoản admin SW sẽ không thấy gì bị chặn |
| **Tên bảng khớp phân biệt hoa-thường tuyệt đối** | Lệch một chữ hoa là bảng không được cấp, và triệu chứng phía người dùng là "chưa được cấp quyền bảng nào" chứ không phải lỗi cấu hình. Log có warning nêu tên gần đúng (xem §6) — cố ý không tự khớp lỏng vì Postgres cho phép hai bảng cùng tên khác hoa-thường tồn tại song song |
| **Scope query hỏng cú pháp thì bảng bị LOẠI, không phải "cho xem tất cả"** | Hỏng theo hướng đóng. Sai cú pháp ở phía SW biểu hiện thành mất bảng chứ không thành lỗi — chỉ log warning mới nói ra nguyên nhân |
| **`SELECT *` trong scope query nghĩa là cho xem MỌI cột** | Muốn giới hạn cột thì scope query phải liệt kê tường minh. Đây là lựa chọn có chủ ý (giữ đúng nghĩa của `*`), nhưng dễ bị hiểu là "đã có phân quyền cột" trong khi thực tế chưa |
| **`queries[].datasourceId` phải là id datasource của SQLBot** | So khớp chuỗi chính xác với `core_datasource.id`. Gửi định danh nội bộ của SW (`"ds-001"`) thì không bảng nào được cấp quyền trên datasource nào — mà không có lỗi nào ở phía hook, vì hook không biết id đó dùng để làm gì |
| **`domainCode` gửi `null` trả 400, không gửi thì không sao** | Ba trạng thái phân biệt bằng `model_fields_set` của pydantic. Client dựng body bằng cách gán biến chưa khởi tạo sẽ nhận `400 invalid_domain_code` chứ không âm thầm hỏi trên toàn bộ lĩnh vực |
| **`/regenerate` kế thừa `domain_code` của record gốc** | Cột `chat_record.domain_code` (migration `082`). Sinh lại một lượt cũ mà đổi phạm vi thì hai kết quả không so sánh được với nhau |
| **Nhánh assistant với datasource động không đi qua phép sàng** | Không có datasource cụ thể thì không có `queries[]` nào để đối chiếu. Hiện chưa dùng trong triển khai HĐND, nhưng đừng cho rằng mọi đường vào đều được siết |

### 7.11. SQLAlchemy `expire_on_commit`: mỗi `commit()` là một lần nạp lại cả dòng

Mặc định của SQLAlchemy là `expire_on_commit=True`: sau mỗi `commit()`, **mọi** object đang gắn
session bị đánh dấu hết hạn, và lần chạm thuộc tính kế tiếp âm thầm bắn một `SELECT` nạp lại
**nguyên dòng**. Không có log, không có exception — nhìn code chỉ thấy `ds.id`.

| Bẫy | Chi tiết |
|---|---|
| **Commit trong vòng lặp + đọc lại object = N+1 vô hình** | `sync_table` commit theo từng bảng/cột rồi vòng nào cũng đọc `ds.type`/`ds.configuration`, nên mỗi vòng kéo về cả cột `table_relation`. Với nguồn 537 bảng, cột đó ~1,5 MB → một lượt resync tải ~1,7 GB qua dây mà không ai dùng tới. Cách chữa là `snapshot_ds` ([datasource.py:568](../backend/apps/datasource/crud/datasource.py#L568)): bản sao transient, không gắn session nên không bao giờ hết hạn |
| **`model_dump()` trên object đã hết hạn trả về toàn `None`** | Thuộc tính hết hạn bị **gỡ khỏi `__dict__`**, mà `model_dump()` đọc thẳng `__dict__` — nên nó trả bản sao rỗng, không exception, không cảnh báo. `getattr` mới kích hoạt cơ chế nạp lại của ORM. Bẫy này có thật: `create_ds` và luồng nhập Excel đều commit ngay trước khi gọi `sync_table`, nên bản sao hỏng sẽ khiến mọi bảng được ghi với `ds_id=None` |
| **`table_relation` nặng hơn nhiều so với tưởng tượng** | Nó **không phải** danh sách cạnh: nó là ảnh chụp canvas của sơ đồ ER (node, port, layout, style theo định dạng AntV/X6). Nguồn 537 bảng có 936 cell, trong đó riêng phần `ports` chiếm 86% dung lượng, còn cạnh chỉ 5%. Bảng không có quan hệ nào vẫn tốn node. `pg_column_size` báo số **đã nén** (TOAST) nên nhìn qua tưởng nhẹ, nhưng đọc ra là bản giải nén |

### 7.12. `SQLBOT_TEST_DB_URL` — fixture test **TRUNCATE** dữ liệu

Các test tích hợp cần Postgres thật (JSONB, partial unique index, `ON CONFLICT` — SQLite không mô
phỏng được). Biến `SQLBOT_TEST_DB_URL` chọn DB cho chúng; thiếu biến thì test **skip** chứ không
fail, nên `pytest tests/` vẫn xanh trên máy không dựng Postgres.

⚠ **Fixture `db_session` chạy `TRUNCATE ai_sync_hook_logs, ai_user_permissions` trước MỖI test**
([conftest.py:53](../tests/conftest.py#L53)) — cần vậy vì các test dùng chung định danh cố định
(`u1`, `f1`, …) và assert số dòng chính xác, còn handler thì tự commit bên trong nên không bọc
rollback được.

Nghĩa là **biến này chỉ được trỏ vào một DB rỗng dùng xong vứt**, không bao giờ trỏ vào DB dev hay
DB thật. Trỏ nhầm là mất sạch audit trail của hook và mất sạch bảng phân quyền — mà hậu quả nặng
nhất không phải "mất dữ liệu": `ai_user_permissions` rỗng nghĩa là **mọi user SW thành không bị
siết** (mặc định mở, §7.10), chỉ khôi phục được khi SW đẩy lại snapshot actionType 1.

```bash
docker run -d --name sqlbot-test-pg -p 55432:5432 \
  -e POSTGRES_USER=test -e POSTGRES_PASSWORD=test -e POSTGRES_DB=sqlbot_test \
  pgvector/pgvector:pg16
export SQLBOT_TEST_DB_URL="postgresql+psycopg://test:test@127.0.0.1:55432/sqlbot_test"
```

Chạy test không có DB (48 test skip): `set -a && . ./env.txt && set +a` rồi
`backend/.venv/bin/python -m pytest tests/ -q`. Cần `env.txt` vì `sqlbot_xpack` `mkdir` `UPLOAD_DIR`
ngay lúc import, mặc định là `/opt/sqlbot` — user thường không ghi được vào đó (§1).
