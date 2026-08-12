# AI Sync Hook Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Dựng endpoint `POST /api/v1/hooks/ai-sync` làm cổng tiếp nhận bản tin đồng bộ từ hệ thống SW, phase này chỉ triển khai `actionType = 1` (AUTHORIZATION_SYNC), ghi audit vào `ai_sync_hook_logs` và replace snapshot quyền vào `ai_user_permissions`.

**Architecture:** Domain mới `backend/apps/hooks/` ngang hàng với `apps/chat`, `apps/datasource`. Route chỉ lo giao thức (xác thực static token, đọc body thô, ghi audit, dịch lỗi thành HTTP status); nghiệp vụ của từng `actionType` nằm trong handler riêng, tra qua dict `ACTION_HANDLERS`. Ba pha commit thủ công để dòng audit sống sót khi pha nghiệp vụ rollback.

**Tech Stack:** FastAPI, SQLModel/SQLAlchemy 2.x, Pydantic v2, Alembic, PostgreSQL 16/17, pytest + unittest.mock.

**Spec:** [docs/superpowers/specs/2026-08-10-ai-sync-hook-design.md](../specs/2026-08-10-ai-sync-hook-design.md) — mọi quyết định thiết kế (full snapshot, idempotency, bảng lỗi, ranh giới transaction) nằm ở đó, plan này không lặp lại.

## Global Constraints

- Mọi hàm viết mới **phải có docstring tiếng Việt**, nêu mục đích + giả định/bẫy, không mô tả lại từng dòng. Thuật ngữ IT giữ nguyên tiếng Anh.
- Commit message: **một dòng**, tiếng Việt, tiền tố Conventional Commits.
- Python `==3.11.*`, chạy mọi lệnh qua `uv run --directory backend`.
- Ruff select `E,W,F,I,B,C4,UP,ARG001` (`E501` được ignore).
- **Không** đụng pipeline Text2SQL (`apps/chat/task/llm.py`).
- Response của hook luôn là `JSONResponse` trả trực tiếp, để `ResponseMiddleware` bỏ qua ở [response_middleware.py:43](../../../backend/common/core/response_middleware.py#L43) — không bọc envelope `{code,data,msg}`.
  > **Giả định này SAI, đã sửa sau khi test trên app thật.** `isinstance(response, JSONResponse)` không bao giờ đúng khi có nhiều `BaseHTTPMiddleware` xếp chồng. Phải khai path vào `direct_paths` — xem [OPERATIONS.md §7.8](../../OPERATIONS.md).
- `actionType` chưa triển khai **phải** trả lỗi, tuyệt đối không im lặng bỏ qua.
- Không log giá trị `AI_SYNC_HOOK_TOKEN` ở bất cứ đâu; so sánh token bằng `secrets.compare_digest`.
- Test đặt ở `tests/` (root repo), chạy `pytest ../tests` từ `backend/`.
- TDD: mỗi task viết test trước, chạy cho fail, rồi mới implement. Test fail thì sửa code, không sửa test cho khớp.

## File Structure

| File | Trách nhiệm |
|---|---|
| `backend/apps/hooks/constants.py` | `SyncActionType`, `IMPLEMENTED_ACTION_TYPES`, `SyncStatus`, `SyncErrorCode`, `resolve_action_type()`. **Không import gì từ `common/`** để nạp được độc lập |
| `backend/apps/hooks/errors.py` | `SyncHookError` mang sẵn HTTP status + errorCode |
| `backend/apps/hooks/models/ai_sync_model.py` | 2 SQLModel table, index khai trong `__table_args__` |
| `backend/apps/hooks/schemas/ai_sync_schema.py` | Pydantic envelope/payload/response + 3 hàm thuần |
| `backend/apps/hooks/crud/ai_sync_log.py` | Ghi/đọc audit log, kiểm idempotency, đọc mốc version |
| `backend/apps/hooks/crud/ai_user_permission.py` | Replace snapshot quyền, đọc quyền theo `user_id` |
| `backend/apps/hooks/handlers/authorization.py` | Handler `actionType=1` + `HandlerResult` |
| `backend/apps/hooks/handlers/__init__.py` | Dict `ACTION_HANDLERS` |
| `backend/apps/hooks/api/ai_sync.py` | Route + xác thực + dịch lỗi |
| `backend/alembic/versions/074_ai_sync_hook.py` | Migration tạo 2 bảng |
| `tests/conftest.py` | `sys.path` → `backend/`, fixture `pg_url` / `db_session` |
| `tests/test_ai_sync_*.py` | 7 module test (chi tiết trong từng task) |

Mỗi thư mục con cần `__init__.py` rỗng. Sửa file có sẵn: `common/core/config.py` (2 biến), `apps/api.py` (đăng ký router), `common/utils/whitelist.py` (thêm `/hooks/*`), `alembic/env.py` (import model).

## Chuẩn bị môi trường (một lần, trước Task 4)

Task 4/5/6/8 cần Postgres thật: code dùng JSONB, `gen_random_uuid()`, partial unique index và `ON CONFLICT` — SQLite không mô phỏng được.

```bash
docker run -d --name sqlbot-test-pg -p 55432:5432 \
  -e POSTGRES_PASSWORD=test -e POSTGRES_USER=test -e POSTGRES_DB=sqlbot_test \
  pgvector/pgvector:pg16
export SQLBOT_TEST_DB_URL="postgresql+psycopg://test:test@127.0.0.1:55432/sqlbot_test"
```

Thiếu biến này thì các test đó **skip** chứ không fail — nhưng task chỉ được coi là xong khi đã chạy chúng với DB thật và thấy PASS (không phải SKIP).

---

### Task 1: Constants, error type, config

**Files:**
- Create: `backend/apps/hooks/__init__.py`, `constants.py`, `errors.py`
- Modify: `backend/common/core/config.py` (chèn sau `SQLBOT_DB_URL`, ~dòng 66)
- Test: `tests/conftest.py`, `tests/test_ai_sync_constants.py`

**Interfaces — Produces:**
- `SyncActionType(IntEnum)`: `UNKNOWN=0, AUTHORIZATION_SYNC=1, USER_SYNC=2, ORGANIZATION_SYNC=3, DATASOURCE_SYNC=4, KNOWLEDGE_SYNC=5, DOCUMENT_SYNC=6`. `UNKNOWN` là sentinel nội bộ (cột `action_type` là NOT NULL nên request thiếu `actionType` vẫn phải ghi được audit).
- `IMPLEMENTED_ACTION_TYPES: frozenset` = `{AUTHORIZATION_SYNC}`
- `SyncStatus(str, Enum)`: `RECEIVED, PROCESSING, SUCCESS, FAILED, DUPLICATE, STALE`
- `SyncErrorCode(str, Enum)`: `MALFORMED_JSON, MISSING_IDEMPOTENCY_KEY, INVALID_ENVELOPE, INVALID_PAYLOAD, DUPLICATE_FORM_UUID, UNAUTHORIZED, UNSUPPORTED_ACTION_TYPE, UNKNOWN_ACTION_TYPE, INTERNAL_ERROR, HOOK_DISABLED`
- `resolve_action_type(raw: object) -> SyncActionType | None` — trả `None` với: không phải `int`, là `bool` (bẫy: `bool` là subclass của `int`, `True == 1`), giá trị `0`, giá trị ngoài enum.
- `SyncHookError(Exception)` với `.http_status: int`, `.error_code: SyncErrorCode`, `.message: str`
- `settings.AI_SYNC_HOOK_ENABLED: bool = False`, `settings.AI_SYNC_HOOK_TOKEN: str = ""`

- [ ] **Step 1: Tạo `tests/conftest.py`**

Ba thứ: đưa `backend/` vào `sys.path` (repo không có package install, các test hiện có tự chèn thủ công); fixture `pg_url` scope session đọc `SQLBOT_TEST_DB_URL`, `pytest.skip` nếu thiếu; fixture `db_session` tạo 2 bảng hook bằng `Model.__table__.create(engine, checkfirst=True)` rồi `TRUNCATE` cả hai, yield `sqlmodel.Session`, `engine.dispose()` khi xong.

Lý do tạo bảng bằng metadata thay vì chạy alembic: test không phụ thuộc toàn bộ lịch sử migration. Điều này chỉ đúng khi index được khai trong `__table_args__` của model (Task 2).

- [ ] **Step 2: Viết test thất bại `tests/test_ai_sync_constants.py`**

Các ca:
- mapping 7 giá trị `SyncActionType` đúng số (0–6);
- chỉ `AUTHORIZATION_SYNC` nằm trong `IMPLEMENTED_ACTION_TYPES`, 5 cái còn lại không;
- `resolve_action_type(1)` và `(6)` trả đúng enum;
- parametrize trả `None` với `["1", 1.0, True, False, None, 0, 7, 99, -1, [], {}]` (kèm comment giải thích vì sao `True` và `0` phải bị loại).

- [ ] **Step 3: Chạy test cho fail**

Run: `uv run --directory backend pytest ../tests/test_ai_sync_constants.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'apps.hooks'`

- [ ] **Step 4: Implement `constants.py` và `errors.py`**

- [ ] **Step 5: Thêm 2 biến vào `config.py`**

Kèm comment giải thích: mặc định tắt và token rỗng → hook trả 503 chứ không chấp nhận request không token, để ca deploy quên đặt biến không biến cổng thành lỗ hổng.

- [ ] **Step 6: Chạy test, phải PASS**

Run: `uv run --directory backend pytest ../tests/test_ai_sync_constants.py -v`

- [ ] **Step 7: Kiểm config nạp được**

Run: `uv run --directory backend python -c "from common.core.config import settings; print(settings.AI_SYNC_HOOK_ENABLED, repr(settings.AI_SYNC_HOOK_TOKEN))"`
Expected: `False ''`

- [ ] **Step 8: Commit**

```bash
git add backend/apps/hooks backend/common/core/config.py tests/conftest.py tests/test_ai_sync_constants.py
git commit -m "feat: hằng số và cấu hình cho AI Sync Hook"
```

---

### Task 2: Hai bảng dữ liệu + migration

**Files:**
- Create: `backend/apps/hooks/models/__init__.py`, `models/ai_sync_model.py`, `backend/alembic/versions/074_ai_sync_hook.py`
- Modify: `backend/alembic/env.py` (thêm import vào khối import model, ~dòng 34)
- Test: `tests/test_ai_sync_models.py`

**Interfaces — Produces:**

`AiSyncHookLog` → `ai_sync_hook_logs`:

| Cột | Kiểu | Ghi chú |
|---|---|---|
| `id` | `UUID(as_uuid=True)` PK | `server_default=text('gen_random_uuid()')` — built-in từ PG13, không cần extension |
| `request_id`, `idempotency_key` | `String(100)` null | |
| `action_type` | `Integer` NOT NULL | |
| `schema_version` | `String(20)` null | |
| `source` | `String(50)` NOT NULL | default `'SW'` |
| `user_id` | `String(100)` null | best-effort, có thể null khi payload sai |
| `request_payload` | `JSONB` NOT NULL | raw body |
| `status` | `String(30)` NOT NULL | |
| `sync_version` | `BigInteger` null | **thêm ngoài DDL gốc**, xem spec §3 |
| `error_code` | `String(100)` null | |
| `error_message` | `Text` null | |
| `received_at` | `TIMESTAMP(timezone=True)` NOT NULL | `server_default=func.now()` |
| `processed_at` | `TIMESTAMP(timezone=True)` null | |

Index: `idx_ai_sync_hook_logs_user_id`, `idx_ai_sync_hook_logs_action_type`, `idx_ai_sync_hook_logs_received_at`, và `uq_ai_sync_hook_logs_idempotency` — **unique, partial** (`postgresql_where=text('idempotency_key IS NOT NULL')`).

`AiUserPermission` → `ai_user_permissions`: `id` (UUID PK như trên), `user_id String(100)` NOT NULL, `full_name String(255)`, `is_admin Boolean` NOT NULL default false, `form_uuid String(100)` NOT NULL, `database_table_name String(255)` NOT NULL, `table_display_name String(255)`, `table_description Text`, `fields JSONB` NOT NULL default `'[]'::jsonb`, `postgres_query Text`, `clickhouse_query Text`, `sync_version BigInteger` NOT NULL, `synced_at`/`created_at`/`updated_at` (`TIMESTAMPTZ` NOT NULL, default now). Ràng buộc `UniqueConstraint('user_id','form_uuid', name='uq_ai_user_permissions_user_form')`; index `idx_ai_user_permissions_user_id`, `idx_ai_user_permissions_user_table(user_id, database_table_name)`.

Migration: `revision = '074a1b2c3d4e'`, `down_revision = '19d092e65217'`, có `downgrade()` drop đủ index + 2 bảng, kèm `COMMENT ON` cho 2 bảng và cho cột `sync_version`.

- [ ] **Step 1: Viết test thất bại `tests/test_ai_sync_models.py`**

Đọc metadata SQLAlchemy nên **không cần DB**. Các ca: đúng 2 `__tablename__`; `AiSyncHookLog` có đủ 14 cột và `action_type`/`request_payload`/`status` là NOT NULL; index `uq_ai_sync_hook_logs_idempotency` có `unique is True` và `dialect_options["postgresql"]["where"]` không None; `AiUserPermission` có đủ 15 cột, `sync_version` NOT NULL, có `UniqueConstraint` trên `(user_id, form_uuid)`, có 2 index đúng tên.

- [ ] **Step 2: Chạy test cho fail**

Run: `uv run --directory backend pytest ../tests/test_ai_sync_models.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'apps.hooks.models'`

- [ ] **Step 3: Implement `models/ai_sync_model.py`**

Khai index trong `__table_args__` (không chỉ trong migration) để `db_session` fixture tạo bảng bằng metadata vẫn có đủ ràng buộc thật — nhất là partial unique index chống trùng idempotency. Docstring nói rõ vì sao 2 bảng này dùng UUID thay vì snowflake BigInt của repo.

- [ ] **Step 4: Chạy test, phải PASS**

Run: `uv run --directory backend pytest ../tests/test_ai_sync_models.py -v`

- [ ] **Step 5: Viết migration `074_ai_sync_hook.py`**

- [ ] **Step 6: Import model vào `alembic/env.py`**

Theo idiom sẵn có của file: `from apps.hooks.models.ai_sync_model import SQLModel  # noqa`

- [ ] **Step 7: Kiểm alembic chỉ có một head**

Run: `uv run --directory backend alembic heads`
Expected: một dòng duy nhất, `074a1b2c3d4e (head)`

- [ ] **Step 8: Chạy migration lên/xuống/lên trên Postgres test**

```bash
cd backend
SQLBOT_DB_URL="$SQLBOT_TEST_DB_URL" uv run alembic upgrade head
SQLBOT_DB_URL="$SQLBOT_TEST_DB_URL" uv run alembic downgrade -1
SQLBOT_DB_URL="$SQLBOT_TEST_DB_URL" uv run alembic upgrade head
```
Expected: cả 3 exit 0. Chưa dựng Postgres test thì bỏ step này và **ghi rõ là chưa verify**.

- [ ] **Step 9: Commit**

```bash
git add backend/apps/hooks/models backend/alembic tests/test_ai_sync_models.py
git commit -m "feat: bảng ai_sync_hook_logs và ai_user_permissions"
```

---

### Task 3: Schema Pydantic và các hàm thuần

**Files:**
- Create: `backend/apps/hooks/schemas/__init__.py`, `schemas/ai_sync_schema.py`
- Test: `tests/test_ai_sync_schema.py`

**Interfaces — Produces** (mọi model dùng `ConfigDict(populate_by_name=True, extra="ignore")`):

| Model | Thuộc tính (alias JSON) |
|---|---|
| `SyncEnvelope` | `action_type` (`actionType`, int), `version` (str, min_length 1), `timestamp` (datetime), `data` (dict) |
| `FieldItem` | `id` (min_length 1), `name` (None), `description` (None) |
| `TableInfo` | `database_table_name` (`databaseTableName`, min_length 1), `table_display_name` (`tableDisplayName`), `table_description` (`tableDescription`), `field_list` (`fields`, list[FieldItem]) |
| `FormQuery` | `form_uuid` (`formUuid`, min_length 1), `table_info` (`tableInfo`), `postgres_query` (`postgresQuery`), `clickhouse_query` (`clickHouseQuery`) |
| `AuthorizationSyncData` | `user_id` (`userId`, min_length 1), `full_name` (`fullName`), `is_admin` (`isAdmin`, bắt buộc), `form_queries` (`formQueries`, bắt buộc, được rỗng) |
| `SyncAppliedCounts` | `upserted: int = 0`, `deleted: int = 0` |
| `SyncHookResponse` | camelCase trực tiếp (không alias): `requestId, idempotencyKey, actionType, status, logId, userId, applied, errorCode, errorMessage` |

Hàm thuần:
- `to_sync_version(ts: datetime) -> int` — epoch millis; datetime naive coi là UTC (nếu không thì `timestamp()` hiểu theo timezone máy chạy backend → mốc chống-lùi lệch theo nơi deploy).
- `normalize_fields(items: list[FieldItem]) -> list[dict]` — luôn đủ 3 khoá `id/name/description`, thiếu thì `None`.
- `find_duplicate_form_uuid(form_queries: list[FormQuery]) -> str | None`.

Hai điểm phải ghi vào docstring: alias là `clickHouseQuery` (**chữ H hoa**) — sai alias là mất query âm thầm; thuộc tính đặt `field_list` chứ không `fields` để tránh đụng không gian tên của Pydantic BaseModel.

`SyncEnvelope.action_type` chỉ khai `int` cho đủ hình — việc phân biệt "không phải integer" với "integer nhưng chưa hỗ trợ" do `resolve_action_type` làm **trước** khi validate envelope, vì hai ca đó khác HTTP status (400 vs 422) và khác cách ghi audit.

- [ ] **Step 1: Viết test thất bại `tests/test_ai_sync_schema.py`**

Không cần DB. Dùng một constant `VALID_DATA` đúng payload mẫu trong spec (form `form-abcd-1234`, bảng `kdl_nhan_khau_row_values`, 2 field). Các ca:

*Envelope:* hợp lệ parse đúng 4 trường và `timestamp` ra `datetime(2026,8,10,10,0,tzinfo=utc)`; parametrize thiếu từng trường `version`/`timestamp`/`data` → `ValidationError`.

*Payload:* hợp lệ parse đúng alias, đặc biệt `clickHouseQuery` và thứ tự `field_list`; `formQueries: []` hợp lệ và `full_name is None`; parametrize thiếu `userId`/`isAdmin`/`formQueries` → lỗi; `userId: ""` → lỗi; form thiếu `tableInfo` → lỗi; `tableInfo` thiếu `fields` → lỗi; `fields: []` được chấp nhận và các trường optional ra `None`; field thiếu `id` → lỗi.

*Hàm thuần:* `to_sync_version` ra `1786356000000` cho `2026-08-10T10:00:00Z`; naive == aware cùng giờ; tôn trọng offset `+07:00`; `normalize_fields` điền `None` đủ 3 khoá; `find_duplicate_form_uuid` bắt được `f1` lặp và trả `None` khi không lặp.

*Response:* `model_dump()` ra khoá camelCase, `applied == {"upserted": 0, "deleted": 0}`, `errorCode is None`.

- [ ] **Step 2: Chạy test cho fail**

Run: `uv run --directory backend pytest ../tests/test_ai_sync_schema.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'apps.hooks.schemas'`

- [ ] **Step 3: Implement `schemas/ai_sync_schema.py`**

- [ ] **Step 4: Chạy test, phải PASS**

Run: `uv run --directory backend pytest ../tests/test_ai_sync_schema.py -v`

- [ ] **Step 5: Commit**

```bash
git add backend/apps/hooks/schemas tests/test_ai_sync_schema.py
git commit -m "feat: schema envelope và payload authorization sync"
```

---

### Task 4: CRUD audit log

**Files:**
- Create: `backend/apps/hooks/crud/__init__.py`, `crud/ai_sync_log.py`
- Test: `tests/test_ai_sync_crud.py`

**Interfaces:**
- Consumes: `AiSyncHookLog` (Task 2), `SyncStatus` (Task 1)
- Produces:
  - `get_log_by_idempotency_key(session, idempotency_key: str) -> AiSyncHookLog | None`
  - `create_received_log(session, *, request_id, idempotency_key, action_type: int, schema_version, user_id, request_payload: dict, source: str = "SW") -> AiSyncHookLog` — **tự commit**; raise `IntegrityError` khi trùng key
  - `finish_log(session, log, *, status: str, sync_version: int | None = None, error_code: str | None = None, error_message: str | None = None) -> AiSyncHookLog` — **tự commit**, set `processed_at = now(utc)`
  - `get_last_applied_version(session, user_id: str) -> int` — `max(sync_version)` của các log `status='SUCCESS'` cùng `user_id`, trả `0` nếu chưa có

Hai điều phải ghi docstring: các hàm **tự commit** (không để `get_session` commit cuối request) vì hook chia ba pha và dòng audit phải sống sót khi pha nghiệp vụ rollback; `get_last_applied_version` cố ý đọc từ bảng **log** chứ không từ `ai_user_permissions` — sau bản tin thu hồi hết quyền, bảng permission không còn dòng nào của user nên mốc version sẽ mất và bản tin cũ gửi lại sau đó sẽ được áp ngược.

- [ ] **Step 1: Viết test thất bại `tests/test_ai_sync_crud.py`**

Dùng fixture `db_session`. Các ca:
- `create_received_log` ghi `status=RECEIVED`, `source='SW'`, `request_payload` round-trip đúng dict, `received_at` không None, `processed_at`/`sync_version` là None;
- `get_log_by_idempotency_key` tìm đúng dòng và trả None với key lạ;
- tạo hai log cùng `idempotency_key` → `pytest.raises(IntegrityError)` (rồi `db_session.rollback()`);
- `finish_log` SUCCESS cập nhật `status`/`sync_version`/`processed_at`, `error_code` vẫn None;
- `finish_log` FAILED ghi đúng `error_code`/`error_message`;
- `get_last_applied_version` trả 0 khi user chưa có gì;
- chỉ tính bản SUCCESS: dựng 3 log SUCCESS(100)/FAILED(999)/STALE(888) cùng user → kết quả 100;
- tách theo user: user khác trả 0.

- [ ] **Step 2: Chạy test cho fail**

Run: `uv run --directory backend pytest ../tests/test_ai_sync_crud.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'apps.hooks.crud'`. Nếu ra SKIP thì chưa dựng Postgres test — xem mục "Chuẩn bị môi trường".

- [ ] **Step 3: Implement `crud/ai_sync_log.py`**

- [ ] **Step 4: Chạy test, phải PASS (không SKIP)**

Run: `SQLBOT_TEST_DB_URL="postgresql+psycopg://test:test@127.0.0.1:55432/sqlbot_test" uv run --directory backend pytest ../tests/test_ai_sync_crud.py -v`

- [ ] **Step 5: Commit**

```bash
git add backend/apps/hooks/crud tests/test_ai_sync_crud.py
git commit -m "feat: crud audit log cho AI Sync Hook"
```

---

### Task 5: CRUD quyền user (replace snapshot + đọc)

**Files:**
- Create: `backend/apps/hooks/crud/ai_user_permission.py`
- Test: `tests/test_ai_sync_permission_crud.py`

**Interfaces:**
- Consumes: `AiUserPermission` (Task 2), `FormQuery` + `normalize_fields` (Task 3)
- Produces:
  - `replace_user_permissions(session, *, user_id: str, full_name: str | None, is_admin: bool, form_queries: list[FormQuery], sync_version: int) -> tuple[int, int]` trả `(upserted, deleted)` — **tự commit**
  - `get_user_permissions(session, user_id: str) -> list[AiUserPermission]` — sắp theo `form_uuid` cho ổn định thứ tự

Thuật toán: DELETE các dòng của user có `form_uuid NOT IN` danh sách đến (danh sách rỗng → xoá hết), lấy `rowcount` làm `deleted`; rồi với từng form chạy `INSERT ... ON CONFLICT (user_id, form_uuid) DO UPDATE` (`sqlalchemy.dialects.postgresql.insert`), set lại mọi cột trừ 2 cột khoá, `synced_at`/`updated_at` = now. Chọn upsert thay vì "xoá hết rồi chèn lại" để giữ `id` và `created_at` của dòng cũ, và để không có khoảng thời gian user mất sạch quyền giữa hai câu lệnh.

`get_user_permissions` là **điểm vào duy nhất** mà phase tích hợp pipeline sẽ dùng: chỉ cần `user_id`.

- [ ] **Step 1: Viết test thất bại `tests/test_ai_sync_permission_crud.py`**

Helper `_forms(*specs)` dựng `list[FormQuery]` từ tuple `(form_uuid, table_name, field_ids)` bằng cách validate qua `AuthorizationSyncData`. Các ca:
- ghi mới 2 form → `(2, 0)`, và kiểm đủ mọi cột đã map đúng (`table_display_name`, `table_description`, `fields` đúng list dict 3 khoá, `postgres_query`, `clickhouse_query`, `sync_version`, `full_name`, `is_admin`);
- snapshot mới thiếu `f1` → `(1, 1)`, còn lại đúng `{f2}`;
- `form_queries=[]` → `(0, 1)` và bảng rỗng cho user đó;
- upsert cùng `form_uuid`: `id` và `created_at` **không đổi**, còn `database_table_name`/`fields`/`sync_version`/`full_name`/`is_admin` đổi theo bản mới;
- không đụng quyền user khác: thu hồi hết của `u2` không ảnh hưởng `u1`;
- `get_user_permissions` với user lạ trả `[]`.

- [ ] **Step 2: Chạy test cho fail**

Run: `SQLBOT_TEST_DB_URL=... uv run --directory backend pytest ../tests/test_ai_sync_permission_crud.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'apps.hooks.crud.ai_user_permission'`

- [ ] **Step 3: Implement `crud/ai_user_permission.py`**

- [ ] **Step 4: Chạy test, phải PASS**

Run: `SQLBOT_TEST_DB_URL="postgresql+psycopg://test:test@127.0.0.1:55432/sqlbot_test" uv run --directory backend pytest ../tests/test_ai_sync_permission_crud.py -v`

- [ ] **Step 5: Commit**

```bash
git add backend/apps/hooks/crud/ai_user_permission.py tests/test_ai_sync_permission_crud.py
git commit -m "feat: replace snapshot quyền user theo bản tin authorization sync"
```

---

### Task 6: Handler AUTHORIZATION_SYNC + bảng dispatch

**Files:**
- Create: `backend/apps/hooks/handlers/__init__.py`, `handlers/authorization.py`
- Test: `tests/test_ai_sync_handler.py`

**Interfaces:**
- Consumes: `SyncEnvelope`/`AuthorizationSyncData`/`find_duplicate_form_uuid` (Task 3), `replace_user_permissions` (Task 5), `get_last_applied_version` (Task 4), `SyncHookError`/`SyncStatus`/`SyncErrorCode` (Task 1)
- Produces:
  - `HandlerResult` (dataclass): `user_id: str | None`, `status: SyncStatus`, `upserted: int = 0`, `deleted: int = 0`
  - `handle_authorization_sync(session, envelope: SyncEnvelope, sync_version: int) -> HandlerResult`
  - `ACTION_HANDLERS: dict[SyncActionType, Callable[[Session, SyncEnvelope, int], HandlerResult]]`

Luồng handler: validate `envelope.data` bằng `AuthorizationSyncData` → `ValidationError` thì raise `SyncHookError(400, INVALID_PAYLOAD, str(e))`; `find_duplicate_form_uuid` khác None → raise `SyncHookError(400, DUPLICATE_FORM_UUID, ...)` có kèm form_uuid trong message; so `sync_version <= get_last_applied_version(...)` → trả `HandlerResult(status=STALE)` (**không** phải lỗi); còn lại gọi `replace_user_permissions` và trả `SUCCESS` kèm 2 số đếm.

Handler **không** ghi audit log và không biết gì về HTTP — route lo hai việc đó. Nhờ vậy handler test được mà không cần dựng HTTP. `ACTION_HANDLERS` cố ý là dict thay vì registry/base-class: mới có một handler nên chưa đủ dữ kiện để trừu tượng hoá.

- [ ] **Step 1: Viết test thất bại `tests/test_ai_sync_handler.py`**

Dùng `db_session`. Helper `_envelope(data)`, `_data(user_id, forms)`, và `_mark_applied(session, user_id, version)` — hàm cuối dựng mốc version bằng cách ghi một log SUCCESS qua `create_received_log` + `finish_log` (handler đọc mốc từ bảng log nên không thể dựng mốc bằng cách gọi handler hai lần). Các ca:
- `ACTION_HANDLERS` có đúng một key và trỏ đúng hàm;
- áp snapshot thành công: `status=SUCCESS`, `user_id`, `(1, 0)`, bảng quyền có 1 dòng;
- payload thiếu trường → `SyncHookError` 400 `INVALID_PAYLOAD`;
- trùng `formUuid` → `SyncHookError` 400 `DUPLICATE_FORM_UUID`, message chứa `f1`;
- mốc 2000, bản tin 1500 → `STALE`, `(0,0)`, bảng quyền vẫn rỗng;
- mốc 2000, bản tin **đúng** 2000 → `STALE` (chống lùi dùng `<=`);
- mốc 2000, bản tin 2001 → `SUCCESS`, có 1 dòng;
- áp snapshot rồi gửi `formQueries: []` version mới hơn → `SUCCESS`, `(0, 1)`, bảng rỗng.

- [ ] **Step 2: Chạy test cho fail**

Run: `SQLBOT_TEST_DB_URL=... uv run --directory backend pytest ../tests/test_ai_sync_handler.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'apps.hooks.handlers'`

- [ ] **Step 3: Implement `handlers/authorization.py` và `handlers/__init__.py`**

- [ ] **Step 4: Chạy test, phải PASS**

Run: `SQLBOT_TEST_DB_URL="postgresql+psycopg://test:test@127.0.0.1:55432/sqlbot_test" uv run --directory backend pytest ../tests/test_ai_sync_handler.py -v`

- [ ] **Step 5: Commit**

```bash
git add backend/apps/hooks/handlers tests/test_ai_sync_handler.py
git commit -m "feat: handler authorization sync và bảng dispatch actionType"
```

---

### Task 7: Route, xác thực, đăng ký vào app

**Files:**
- Create: `backend/apps/hooks/api/__init__.py`, `api/ai_sync.py`
- Modify: `backend/apps/api.py`, `backend/common/utils/whitelist.py`
- Test: `tests/test_ai_sync_api.py`

**Interfaces:**
- Consumes: toàn bộ Task 1–6
- Produces: `router: APIRouter(tags=["AI Sync Hook"], prefix="/hooks")` với `POST /ai-sync`; `authenticate_hook_request(request) -> None` (raise `SyncHookError` 503 `HOOK_DISABLED` / 401 `UNAUTHORIZED`). Endpoint đầy đủ: `POST {API_V1_STR}/hooks/ai-sync`.

**Trình tự trong route** (thứ tự là một phần của hợp đồng, xem spec §4–§5):

1. Đọc `X-Request-ID`, `X-Idempotency-Key`.
2. `authenticate_hook_request` → lỗi thì trả ngay, **không ghi audit** (caller chưa xác thực không được bơm dữ liệu vào bảng log), chỉ ghi app log.
3. Thiếu `X-Idempotency-Key` → 400 `MISSING_IDEMPOTENCY_KEY`, không ghi audit (không có khoá để ghi).
4. `await request.json()`; hỏng hoặc không phải dict → 400 `MALFORMED_JSON`, không ghi audit. Cố ý **không** khai Pydantic model ở signature: nếu khai, FastAPI tự trả 422 theo format riêng của nó, mất quyền phân biệt 400/422 và mất luôn dòng audit.
5. `get_log_by_idempotency_key` → có rồi thì trả 200 `DUPLICATE` kèm `logId`/`errorCode`/`errorMessage` của lần đầu, **không** gọi handler.
6. `resolve_action_type(raw.get("actionType"))`; lấy best-effort `user_id` từ `raw["data"]["userId"]` và `schema_version` từ `raw["version"]` (chỉ khi là str).
7. `create_received_log` (commit). `IntegrityError` → `session.rollback()`, đọc lại dòng của bên thắng → trả 200 `DUPLICATE`; không đọc được thì re-raise.
8. `action_type is None` → 422 `UNKNOWN_ACTION_TYPE`; không nằm trong `IMPLEMENTED_ACTION_TYPES` → 422 `UNSUPPORTED_ACTION_TYPE` (message nêu cả số và tên enum). Cả hai đều `finish_log(FAILED)` trước khi trả.
9. `SyncEnvelope.model_validate(raw)` → `ValidationError` thì 400 `INVALID_ENVELOPE` + `finish_log(FAILED)`.
10. `sync_version = to_sync_version(envelope.timestamp)`; gọi `ACTION_HANDLERS[action_type]`. `SyncHookError` → rollback + `finish_log(FAILED)` + trả đúng status/code của exception. `Exception` khác → rollback + ghi app log `exc_info=True` + 500 `INTERNAL_ERROR` + `finish_log(FAILED)`.
11. Thành công/STALE → `finish_log(status=result.status, sync_version=...)` rồi trả 200.

Trong file nên có 2 helper nhỏ: `_respond(...)` dựng `JSONResponse` từ `SyncHookResponse` cho mọi nhánh, và `_duplicate_response(log, ...)` cho nhánh trùng key. Docstring module phải nêu 3 điều dễ hỏng: luôn trả `JSONResponse`; thứ tự ghi audit; và lý do đọc body thô.

- [ ] **Step 1: Viết test thất bại `tests/test_ai_sync_api.py`**

Không cần DB: fixture `client` dựng `FastAPI()` tối giản chỉ `include_router(ai_sync.router, prefix="/api/v1")`, override `get_session` bằng `MagicMock`, set `settings.AI_SYNC_HOOK_ENABLED=True` + `AI_SYNC_HOOK_TOKEN` (nhớ restore giá trị gốc khi teardown). Fixture `crud_patches` patch 3 hàm crud log **tại module route** (`apps.hooks.api.ai_sync.get_log_by_idempotency_key`, `.create_received_log`, `.finish_log`). Handler patch qua `patch.dict(ACTION_HANDLERS, {...})`.

Các ca:
- thành công: 200, `status=SUCCESS`, `applied` đúng, `requestId`/`idempotencyKey` vọng lại, `errorCode is None`, `sync_version` truyền vào handler đúng `1786356000000`, và body **không** có khoá `code`/`data` (chứng minh không bị bọc envelope);
- không có header `Authorization` → 401 `UNAUTHORIZED`, `create_received_log` **không** được gọi;
- token sai → 401, không ghi log;
- `AI_SYNC_HOOK_ENABLED=False` → 503 `HOOK_DISABLED`;
- `AI_SYNC_HOOK_TOKEN=""` → 503 `HOOK_DISABLED`;
- thiếu `X-Idempotency-Key` → 400 `MISSING_IDEMPOTENCY_KEY`, không ghi log;
- body không phải JSON → 400 `MALFORMED_JSON`, không ghi log;
- body là JSON array → 400 `MALFORMED_JSON`;
- parametrize `actionType` ∈ `[2,3,4,5,6]` → 422 `UNSUPPORTED_ACTION_TYPE`, `create_received_log` được gọi đúng 1 lần, `finish_log` nhận `status=FAILED`, và `actionType` trong response vọng lại đúng giá trị SW gửi;
- parametrize `actionType` ∈ `["1", 99, None, 0, True]` → 422 `UNKNOWN_ACTION_TYPE`, có ghi log;
- envelope thiếu `version`/`timestamp` → 400 `INVALID_ENVELOPE`, có ghi log;
- trùng idempotency key (mock `get_log_by_idempotency_key` trả log cũ) → 200 `DUPLICATE`, `logId` đúng, handler **không** được gọi, `create_received_log` **không** được gọi;
- race: `create_received_log` raise `IntegrityError`, `get_log_by_idempotency_key` `side_effect=[None, log_cũ]` → 200 `DUPLICATE`, handler không được gọi;
- handler raise `SyncHookError(400, INVALID_PAYLOAD, ...)` → 400, `errorCode`/`errorMessage` đúng, `finish_log` nhận `FAILED`;
- handler raise `RuntimeError` → 500 `INTERNAL_ERROR`, `finish_log` nhận `FAILED`;
- handler trả `STALE` → 200 `status=STALE`, `applied` toàn 0;
- `whiteUtils.is_whitelisted("/api/v1/hooks/ai-sync") is True`.

- [ ] **Step 2: Chạy test cho fail**

Run: `uv run --directory backend pytest ../tests/test_ai_sync_api.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'apps.hooks.api'`

- [ ] **Step 3: Implement `api/ai_sync.py`**

- [ ] **Step 4: Đăng ký router trong `backend/apps/api.py`**

Thêm `from apps.hooks.api import ai_sync` và `api_router.include_router(ai_sync.router)`.

- [ ] **Step 5: Thêm `"/hooks/*"` vào `wlist` trong `backend/common/utils/whitelist.py`**

Kèm comment: hook tự xác thực bằng static token riêng, không dùng JWT `X-SQLBOT-TOKEN`.

- [ ] **Step 6: Chạy test, phải PASS**

Run: `uv run --directory backend pytest ../tests/test_ai_sync_api.py -v`

- [ ] **Step 7: Kiểm route có mặt trong app thật**

Run: `uv run --directory backend python -c "from apps.api import api_router; print([r.path for r in api_router.routes if 'hooks' in r.path])"`
Expected: `['/hooks/ai-sync']`

- [ ] **Step 8: Chạy toàn bộ suite để chắc không phá gì**

Run: `uv run --directory backend pytest ../tests -v`
Expected: PASS hết (test cần DB thì PASS nếu có `SQLBOT_TEST_DB_URL`, không thì SKIP)

- [ ] **Step 9: Commit**

```bash
git add backend/apps/hooks/api backend/apps/api.py backend/common/utils/whitelist.py tests/test_ai_sync_api.py
git commit -m "feat: endpoint POST /hooks/ai-sync nhận bản tin đồng bộ từ SW"
```

---

### Task 8: Kiểm thử đầu-cuối trên Postgres thật + tài liệu

**Files:**
- Create: `tests/test_ai_sync_e2e.py`, `backend/scripts/ai_sync_hook/AI_SYNC_HOOK_API_SPEC.md`
- Modify: `docs/BACKEND_ARCHITECTURE.md`, `docs/OPERATIONS.md`, `docs/AI_ONBOARDING.md`

**Interfaces:** không có interface code mới.

- [ ] **Step 1: Viết test đầu-cuối `tests/test_ai_sync_e2e.py`**

HTTP thật + Postgres thật, **không patch gì**: fixture `e2e_client` dựng app tối giản như Task 7 nhưng override `get_session` trả đúng `db_session` của fixture. Đây là test duy nhất chứng minh cả chuỗi route → audit log → bảng quyền.

Ca chính (một test, sáu bước tuần tự, mỗi bước một `idempotency_key` khác trừ bước retry):
1. bản tin `10:00Z` cấp 2 form → 200 SUCCESS, `applied={2,0}`, bảng quyền có `{form-1, form-2}`, log có `status=SUCCESS`, `sync_version=1786356000000`, `processed_at` không None, `request_payload` round-trip đúng;
2. gửi lại y nguyên cùng key → `DUPLICATE`;
3. bản tin `11:00Z` chỉ còn `form-2` → `applied={1,1}`, bảng quyền còn `{form-2}`;
4. gửi lại bản **cũ** `09:00Z` với key khác → 200 `STALE`, bảng quyền không đổi;
5. bản tin `12:00Z` với `formQueries: []` → SUCCESS, bảng quyền rỗng;
6. bản tin `11:30Z` (cũ hơn bước 5) → `STALE` — chứng minh mốc version đọc từ bảng log nên không mất khi bảng quyền trống.

Hai ca phụ: `actionType=3` → 422 `UNSUPPORTED_ACTION_TYPE` nhưng log vẫn có `status=FAILED`, `action_type=3`, `error_code` đúng; payload thiếu trường → 400 `INVALID_PAYLOAD`, log `FAILED` với `user_id` best-effort đã ghi được, và bảng quyền của user đó rỗng.

- [ ] **Step 2: Chạy test e2e**

Run: `SQLBOT_TEST_DB_URL="postgresql+psycopg://test:test@127.0.0.1:55432/sqlbot_test" uv run --directory backend pytest ../tests/test_ai_sync_e2e.py -v`
Expected: PASS. FAIL thì sửa code, không sửa test cho khớp.

- [ ] **Step 3: Chạy toàn bộ suite + lint**

```bash
SQLBOT_TEST_DB_URL="postgresql+psycopg://test:test@127.0.0.1:55432/sqlbot_test" uv run --directory backend pytest ../tests -v
uv run --directory backend ruff check apps/hooks
```
Expected: pytest PASS hết, ruff sạch trên `apps/hooks`.

- [ ] **Step 4: Viết spec API cho bên tích hợp `backend/scripts/ai_sync_hook/AI_SYNC_HOOK_API_SPEC.md`**

Phải có: endpoint + headers; ví dụ JSON đầy đủ của envelope và payload `actionType=1`; bảng mapping 6 actionType kèm cột trạng thái triển khai; bảng lỗi đúng như spec §4; ngữ nghĩa **full snapshot** nói rõ `formQueries: []` = thu hồi hết quyền và gửi thiếu form = thu hồi form đó; quy tắc idempotency (bắt buộc, trùng → 200 DUPLICATE kèm kết quả lần đầu); quy tắc chống bản tin lùi (`STALE`, dựa trên `timestamp` nên đồng hồ SW phải đồng bộ NTP); ví dụ `curl` một request thành công.

- [ ] **Step 5: Cập nhật `docs/BACKEND_ARCHITECTURE.md`**

- §2 bảng thư mục: thêm `apps/hooks/` — "Cổng nhận bản tin đồng bộ từ hệ ngoài (SW)" kèm file cần biết.
- §3: đổi "Migration mới nhất" thành `074_ai_sync_hook.py`.
- §5: thêm mục "Cụm AI Sync Hook" — 2 bảng và quan hệ log → permission qua `user_id`.
- §7: ghi chú `/hooks/*` nằm trong whitelist `TokenMiddleware` và tự xác thực bằng `AI_SYNC_HOOK_TOKEN`.
- §11: thêm `AI_SYNC_HOOK_API_SPEC.md` vào danh sách spec "đừng viết lại".

- [ ] **Step 6: Cập nhật `docs/OPERATIONS.md`**

- Bảng biến cấu hình: `AI_SYNC_HOOK_ENABLED` (mặc định `False`), `AI_SYNC_HOOK_TOKEN` (rỗng → hook trả 503).
- Mục "Bẫy đã biết", hai bẫy mới: (1) `sync_version` lấy từ `timestamp` của SW nên đồng hồ SW lệch về quá khứ sẽ khiến bản tin mới bị coi là `STALE` — phía SW thấy HTTP 200 nên **tưởng đã thành công**, chỉ `ai_sync_hook_logs.status` mới nói thật; (2) `AUTHORIZATION_SYNC` là full snapshot, gửi thiếu form nào là **thu hồi quyền form đó**, không phải "không thay đổi".
- Cách debug: truy vấn `ai_sync_hook_logs` theo `idempotency_key` / `user_id` để dựng lại một lần đồng bộ.

- [ ] **Step 7: Cập nhật `docs/AI_ONBOARDING.md`**

Thêm dòng trỏ `AI_SYNC_HOOK_API_SPEC.md` vào bảng "Bản đồ tài liệu", và một câu ở §3 rằng đã có cổng đồng bộ quyền từ SW nhưng **chưa** nối vào pipeline Text2SQL.

- [ ] **Step 8: Commit**

```bash
git add tests/test_ai_sync_e2e.py backend/scripts/ai_sync_hook docs/
git commit -m "test: kiểm thử đầu-cuối AI Sync Hook và cập nhật tài liệu"
```

---

## Ngoài phạm vi (phase sau, cần spec riêng)

- Đọc `ai_user_permissions` trong pipeline Text2SQL: bơm bảng/field vào M-Schema, chèn điều kiện giới hạn quyền, xử lý tương tác với row/column permission của `sqlbot_xpack`.
- Quy đổi `user_id` của SW sang user nội bộ SQLBot (có thể qua `sys_user_platform`) và gắn `oid` workspace.
- Kiểm tra an toàn `postgres_query` / `clickhouse_query` trước khi dùng.
- 5 actionType còn lại (2–6).
- Endpoint HTTP tra cứu quyền cho DevOps/SW.
- Retention/dọn `ai_sync_hook_logs`.
