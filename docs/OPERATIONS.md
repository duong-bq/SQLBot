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

### 2.2. Lịch sử hội thoại

| Biến | Mặc định | Tác dụng |
|---|---|---|
| `GENERATE_SQL_QUERY_HISTORY_ROUND_COUNT` | `5` | Số lượt phát lại vào prompt sinh SQL |
| `LLM_SQL_HISTORY_ANSWER_MAX_CHARS` | `4000` | Trần ký tự cho **một** câu trả lời khi nhập vào lịch sử. Chặn ca model lặp vô hạn (đã gặp 94.731 ký tự) làm vỡ context window vĩnh viễn |
| `LLM_SQL_HISTORY_TOTAL_MAX_CHARS` | `24000` | Trần ký tự cho **toàn bộ** phần lịch sử. Chặn bằng ký tự chứ không chỉ bằng số lượt, vì m-schema đã ăn sẵn ~24K |
| `LLM_SQL_HISTORY_JSON_ONLY` | `False` | Chỉ giữ khối JSON của pha SQL trong lịch sử, bỏ văn xuôi suy luận. **Đang được đo bằng bộ test HĐND — đừng đổi mặc định mà không kèm số liệu** |
| `LLM_ANSWER_HISTORY_ENABLED` | `True` | Đưa lịch sử vào cả nhánh answer **bình thường**, không chỉ fallback |
| `LLM_ANSWER_HISTORY_ROUNDS` / `_ROWS` | `3` / `20` | Ngân sách lịch sử cho nhánh answer thường (tách riêng khỏi `FALLBACK_*` để A/B được) |

### 2.3. Thinking

| Biến | Mặc định | Tác dụng |
|---|---|---|
| `LLM_DISABLE_THINKING` | `True` | Tắt pha reasoning. Model như Qwen3 có thể kẹt trong vòng lặp suy luận, đốt hết token mà không xuất `content` nào → pipeline nhận chuỗi rỗng rồi chết |
| `LLM_DISABLE_THINKING_EXTRA_BODY` | `{"chat_template_kwargs": {"enable_thinking": false}}` | Payload tiêm vào `extra_body`. Chuẩn vLLM/SGLang. API kiểu DashScope cần `{"enable_thinking": false}` — **đổi biến này, đừng sửa code** |
| `PARSE_REASONING_BLOCK_ENABLED` | `True` | Tách khối `<think>…</think>` khỏi `content` |

### 2.4. Embedding và top-K

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

### 2.5. Giới hạn dòng

| Nơi | Giá trị | Ghi chú |
|---|---|---|
| `GENERATE_SQL_QUERY_LIMIT_ENABLED` | `True` | Ép LLM thêm `LIMIT` vào SQL |
| `ANSWER_MAX_ROWS` (**hằng số trong code**, [llm.py:74](../backend/apps/chat/task/llm.py#L74)) | `100` | Trần số dòng nhồi vào prompt answer. Sửa thì **bắt buộc** giữ nguyên cặp với `build_data_scope_note` |

### 2.6. Bảo mật và mạng

| Biến | Mặc định | Tác dụng |
|---|---|---|
| `SECRET_KEY` | `secrets.token_urlsafe(32)` | Khóa ký JWT. **Không đặt cố định thì mọi token chết sau mỗi lần restart process** |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `11520` (8 ngày) | Không có refresh endpoint |
| `SQLBOT_ALLOW_METADATA_QUERIES` | `False` | Cho phép `SHOW/DESCRIBE/DESC/EXPLAIN`. Bật là lộ cấu trúc DB |
| `CORS_ALLOW_ALL_ORIGINS` | `False` | Không khai được bằng `BACKEND_CORS_ORIGINS='*'` vì kiểu là `AnyUrl`. Bật cờ này thì `main.py` dùng `allow_origin_regex='.*'`, vẫn hoạt động với credentials |
| `BACKEND_CORS_ORIGINS` | `[]` | Danh sách origin, ngăn bằng dấu phẩy. Starlette so khớp **chuỗi chính xác** — khác scheme/host/port là bị chặn, không có wildcard theo domain |
| `SQLBOT_DOC_ENABLED` | `True` | Bật `/docs` và `/openapi.json` |

### 2.7. Nạp Excel bất đồng bộ và callback

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

**Bật callback ở deployment Docker**: `docker-compose.backend.yml` truyền `AI_CALLBACK_URL` xuống
container, giá trị lấy từ file `.env` **cạnh compose file**. Chạy backend thẳng trên host thì đặt
trong `.env` ở repo root như mọi biến khác. Điền URL rồi restart là các thư đang chờ tự đi — không
cần bắn lại tay, và không lần thử nào bị đốt trong lúc URL còn rỗng.

Chỉ `AI_CALLBACK_URL` là bắt buộc; các biến còn lại có mặc định dùng được ngay.

### 2.8. Khác

`CACHE_TYPE` (`memory`/`redis`/`None`), `CACHE_REDIS_URL`, `LOG_LEVEL`, `LOG_DIR`, `SQL_DEBUG`,
`PG_POOL_SIZE`/`PG_MAX_OVERFLOW`/`PG_POOL_RECYCLE`/`PG_POOL_PRE_PING` (pool của **DB metadata**),
`ORACLE_CLIENT_PATH`, `CONTEXT_PATH` (prefix URL, mặc định rỗng).

Biến bool nhận cả `"true"`/`"True"`/`"false"` nhờ validator `lowercase_bool`
([config.py:189](../backend/common/core/config.py#L189)).

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

([chat.py:194](../backend/apps/chat/api/chat.py#L194) và [chat.py:212](../backend/apps/chat/api/chat.py#L212))

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

`request_payload` (JSONB) là body thô SW đã gửi — dùng để tái hiện request khi cần. Trạng thái quyền
hiện tại (sau khi đã áp snapshot) nằm ở `ai_user_permissions`, lọc theo `user_id`.

---

## 7. Bẫy đã biết

### 7.1. API datasource

| Bẫy | Chi tiết |
|---|---|
| **`source.cell` / `target.cell` phải là số nguyên** | Trong `POST /table_relation/save/{ds_id}`. Truyền chuỗi thì API trả **200 OK, không lỗi, không log**, nhưng quan hệ bị vô hiệu hoàn toàn → mọi câu hỏi cần JOIN sẽ sai. Xem `DATASOURCE_API_SPEC.md` §6.2 |
| **`editTable` / `editField` thiếu `custom_comment` là xóa mất chú thích** | Hai endpoint này ghi đè toàn bộ record, không phải PATCH. Chú thích là đầu vào trực tiếp của M-Schema nên mất là chất lượng SQL tụt ngay |
| **`chooseTables` thay thế toàn bộ danh sách** | Không phải "thêm bảng". Gửi thiếu một bảng đang dùng là bỏ chọn nó |
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

### 7.4. Hành vi pipeline

| Bẫy | Chi tiết |
|---|---|
| Test bằng tài khoản admin thì **không thấy row-permission** | `is_normal_user` chỉ là `current_user.id != 1` |
| Không có license xpack thì các nhánh custom prompt **im lặng không chạy**, không báo lỗi | |
| Một lượt có thể phát **nhiều** event `info: sql generated` | Retry cố ý không phát event riêng |
| Event `sql` và `sql-data` **có thể không xuất hiện** | Lượt hạ cấp không có SQL. Client không được coi chúng là bắt buộc |
| Lỗi giữa stream vẫn là **HTTP 200** | Phải đọc event `error`, không dựa vào status code |
| Cắt dòng dữ liệu mà không kèm `build_data_scope_note` → LLM bịa số tổng | Xem [TEXT2SQL_PIPELINE.md §6](TEXT2SQL_PIPELINE.md) |
| Không có cơ chế hủy | `AbortController` phía client chỉ ngắt kết nối; backend vẫn chạy tới hết và vẫn tính token |
| Không chạy song song nhiều câu hỏi trên cùng `chat_id` | Xem `API_SPEC.md` §7.4 |

### 7.5. AI Sync Hook

| Bẫy | Chi tiết |
|---|---|
| Bản tin bị coi là `STALE` mà SW vẫn thấy **HTTP 200** | Chống lùi dựa trên `timestamp` của SW, không phải sequence phía DB. Đồng hồ SW lệch về quá khứ (hoặc lệch múi giờ, thiếu offset) khiến bản tin mới bị bỏ qua trong khi request vẫn "thành công" theo status code — phải đọc trường `status` trong body, không chỉ HTTP status |
| `AUTHORIZATION_SYNC` là **full snapshot**, không phải patch | Gửi thiếu một `formUuid` đang có quyền là **thu hồi** form đó, không phải "giữ nguyên". `formQueries: []` thu hồi hết. Xem `AI_SYNC_HOOK_API_SPEC.md` §5 |
| Gọi hook mà không đăng nhập trước | Không có static token riêng — SW phải `POST /login/access-token` lấy `X-SQLBOT-TOKEN` như mọi client khác, và tài khoản đó phải có quyền `ws_admin` |
| Thiếu quyền `ws_admin` trả **500**, không phải 403 | Quy ước chung của `require_permissions` toàn hệ thống (xem §7.4 / `BACKEND_ARCHITECTURE.md` §7), không riêng gì hook |

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
| **Nguồn dữ liệu `Failed` vẫn chiếm tên** | Nạp hỏng để lại một dòng `core_datasource` trạng thái `Failed`: không vào hỏi đáp nhưng tạo lại đúng tên đó sẽ bị `409`. Bên tích hợp phải gọi endpoint xóa. Kiểm tra tồn đọng: `SELECT id, name FROM core_datasource WHERE status = 'Failed'` |
| **Khối `COPY` chết ở `uploadExcel`** | [datasource.py:558](../backend/apps/datasource/api/datasource.py#L558) có một khối `COPY FROM STDIN` chạy sau `to_sql` nhưng thiếu `seek(0)`, nên nó đọc chuỗi rỗng và nạp 0 dòng. Vô hại đúng vì lỗi đó — ai "sửa" bằng cách thêm `seek(0)` sẽ làm **mọi bảng nhân đôi dữ liệu**. Đường mới (`utils/excel_import.py`) đã bỏ hẳn khối này |
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
