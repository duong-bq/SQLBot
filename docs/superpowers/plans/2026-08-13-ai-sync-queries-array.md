# AI Sync Hook — mảng `queries[]` tổng quát Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Thay 2 field cố định `postgresQuery`/`clickHouseQuery` (cấp `FormQuery`) bằng mảng
`tableInfo.queries[]` tổng quát theo `datasourceId`/`datasourceType`, xuyên suốt input schema, DB,
CRUD và output API đọc quyền — breaking change, cắt hẳn, không giữ tương thích ngược.

**Architecture:** Parse `queries[]` ở `TableInfo` (input) → chuẩn hoá + ghi vào 1 cột JSONB
`queries` trên `ai_user_permissions` (thay 2 cột `postgres_query`/`clickhouse_query`) → đọc lại
nguyên văn ở API đọc quyền. Kiểm trùng `datasourceId` trong cùng 1 form theo đúng khuôn
`find_duplicate_form_uuid` đã có.

**Tech Stack:** FastAPI, SQLModel/SQLAlchemy, Alembic, Pydantic, pytest (Postgres thật cho test CRUD
qua fixture `db_session`, không cần DB cho test schema/model/API nhờ patch).

**Spec:** `docs/superpowers/specs/2026-08-13-ai-sync-queries-array-design.md`

## Global Constraints

- Cắt hẳn sang `queries[]` — xoá `postgresQuery`/`clickHouseQuery` khỏi input schema, không hỗ trợ
  song song 2 format.
- `queries[]` nằm **trong `tableInfo`**, không phải ngang cấp `tableInfo` như 2 field cũ.
- `queries` bắt buộc, tối thiểu 1 phần tử.
- `datasourceType` là free string, không validate theo enum.
- JSONB lưu `queries` giữ nguyên khoá **camelCase** (`datasourceId`, `datasourceType`, `query`).
- Trùng `datasourceId` trong cùng 1 form → `400 DUPLICATE_DATASOURCE_ID`.
- Drop hẳn cột `postgres_query`/`clickhouse_query` trong migration, không backfill sang `queries`.
- Mọi hàm mới/sửa phải có docstring tiếng Việt (theo `CLAUDE.md`).
- Commit message 1 dòng, tiếng Việt, tiền tố Conventional Commits.

---

## Task 1: Input schema — `QueryItem`, `TableInfo.queries`, xoá field cũ ở `FormQuery`

**Files:**
- Modify: `backend/apps/hooks/schemas/ai_sync_schema.py` (class `FieldItem` dòng 31-38 — thêm
  `QueryItem` ngay sau; class `TableInfo` dòng 41-63; class `FormQuery` dòng 66-74; thêm hàm
  `normalize_queries` cạnh `normalize_fields` dòng 148-154; thêm hàm `find_duplicate_datasource_id`
  cạnh `find_duplicate_form_uuid` dòng 157-168)
- Test: `tests/test_ai_sync_schema.py`

**Interfaces:**
- Produces: `QueryItem(datasource_id: str, datasource_type: str, query: str)` — alias
  `datasourceId`/`datasourceType`/`query`. `TableInfo.queries: list[QueryItem]` (alias `queries`,
  `min_length=1`). `normalize_queries(items: list[QueryItem]) -> list[dict[str, Any]]` trả
  `[{"datasourceId": ..., "datasourceType": ..., "query": ...}, ...]`.
  `find_duplicate_datasource_id(queries: list[QueryItem]) -> str | None`. `FormQuery` không còn
  `postgres_query`/`clickhouse_query`. Task 2, 4 dùng các tên này.

- [ ] **Step 1: Viết test cho `QueryItem`/`TableInfo.queries`/`normalize_queries`/`find_duplicate_datasource_id`**

  Trong `VALID_DATA["formQueries"][0]["tableInfo"]` (dòng 27-35): xoá 2 khoá `postgresQuery`/
  `clickHouseQuery` ở cấp `formQueries[0]` (dòng 40-41 — chúng đang nằm SAI cấp so với thiết kế mới,
  cần dời), thêm khoá `queries` bên trong `tableInfo` với 2 phần tử:
  `{"datasourceId": "ds-001", "datasourceType": "postgresql", "query": "SELECT * FROM
  kdl_nhan_khau_row_values WHERE province_id = '01'"}` và
  `{"datasourceId": "ds-002", "datasourceType": "clickhouse", "query": "SELECT * FROM
  kdl_nhan_khau_row_values WHERE province_id = '01'"}`.

  Sửa `test_payload_hop_le_va_parse_dung_alias` (dòng 61-71): bỏ dòng 69
  (`assert form.clickhouse_query.startswith("SELECT")`) — field không còn tồn tại; thay bằng
  assert `len(form.table_info.queries) == 2`, `form.table_info.queries[0].datasource_id ==
  "ds-001"`, `form.table_info.queries[0].datasource_type == "postgresql"`,
  `form.table_info.queries[1].datasource_id == "ds-002"`.

  Sửa `test_table_info_fields_rong_duoc_chap_nhan` (dòng 110-118): payload test này hiện không có
  `queries` — thêm `"queries": [{"datasourceId": "d1", "datasourceType": "postgresql", "query":
  "SELECT 1"}]` vào `tableInfo` của payload (nếu không, test sẽ fail vì `queries` giờ bắt buộc);
  bỏ dòng 119 (`assert data.form_queries[0].postgres_query is None` — field không còn tồn tại).

  Rà toàn bộ các payload test khác trong file có `tableInfo` mà thiếu `queries` (`test_form_thieu_table_info_bi_loai`
  dòng 93-97, `test_table_info_thieu_fields_bi_loai` dòng 100-107, `test_field_thieu_id_bi_loai` dòng
  122-131, `test_normalize_fields_giu_du_3_khoa_va_dien_none` dòng 149-160, `test_find_duplicate_form_uuid`
  dòng 163-172, `test_find_duplicate_form_uuid_tra_none_khi_khong_trung` dòng 175-185): với các test
  đang kỳ vọng lỗi validate vì lý do KHÁC (thiếu `tableInfo`, thiếu `fields`, thiếu `id` của field) —
  giữ nguyên, không cần thêm `queries` vì payload đã lỗi từ trước đó rồi. Với các test kỳ vọng
  **parse thành công** (`test_normalize_fields_giu_du_3_khoa_va_dien_none`,
  `test_find_duplicate_form_uuid`, `test_find_duplicate_form_uuid_tra_none_khi_khong_trung`) — bắt
  buộc thêm `"queries": [{"datasourceId": "d1", "datasourceType": "postgresql", "query": "SELECT
  1"}]` vào mỗi `tableInfo`, nếu không sẽ fail vì `queries` rỗng/thiếu bị từ chối.

  Thêm test mới `test_table_info_queries_rong_bi_loai`: payload có `tableInfo.queries: []` →
  `pytest.raises(ValidationError)`.

  Thêm test mới `test_table_info_thieu_queries_bi_loai`: payload có `tableInfo` không có khoá
  `queries` → `pytest.raises(ValidationError)`.

  Thêm test mới `test_normalize_queries_tra_dung_3_khoa_camel_case`: dựng `TableInfo` (qua
  `AuthorizationSyncData.model_validate`) với `queries` có 1 phần tử, gọi
  `normalize_queries(data.form_queries[0].table_info.queries)`, assert kết quả `==
  [{"datasourceId": "d1", "datasourceType": "postgresql", "query": "SELECT 1"}]`.

  Thêm test mới `test_find_duplicate_datasource_id`: 2 phần tử `queries` cùng `datasourceId="d1"` →
  `find_duplicate_datasource_id(...) == "d1"`.

  Thêm test mới `test_find_duplicate_datasource_id_tra_none_khi_khong_trung`: 2 phần tử
  `datasourceId` khác nhau → trả `None`.

- [ ] **Step 2: Chạy test, xác nhận FAIL**

  Run: `pytest tests/test_ai_sync_schema.py -v`
  Expected: FAIL — `QueryItem` chưa tồn tại, `TableInfo` chưa có `queries`, `FormQuery` vẫn còn
  `postgres_query`/`clickhouse_query` nên các assert mới không khớp; nhiều test cũ FAIL vì thiếu
  `queries` bắt buộc.

- [ ] **Step 3: Thêm `QueryItem`, sửa `TableInfo`, sửa `FormQuery`, thêm 2 hàm thuần**

  Thêm class `QueryItem(BaseModel)` ngay sau `FieldItem` (dòng 38): `model_config =
  ConfigDict(populate_by_name=True, extra="ignore")`, 3 field `datasource_id` (alias
  `datasourceId`, `min_length=1`), `datasource_type` (alias `datasourceType`, `min_length=1`),
  `query` (`min_length=1`, không alias vì tên trùng cả 2 phía). Docstring tiếng Việt: 1 phần tử
  trong `tableInfo.queries[]`, gắn 1 datasource cụ thể với query giới hạn phạm vi dữ liệu.

  Trong `TableInfo` (dòng 41-63): thêm field `queries: list[QueryItem] = Field(alias="queries",
  min_length=1)`, đặt sau `domain_description` (dòng 62), trước `field_list` (dòng 63). Cập nhật
  docstring class (dòng 42-52) nhắc thêm: `queries` bắt buộc tối thiểu 1 phần tử — khác `fields` được
  phép rỗng, vì một form không biết lấy dữ liệu từ nguồn nào thì vô nghĩa.

  Trong `FormQuery` (dòng 66-74): xoá 2 dòng field `postgres_query`/`clickhouse_query` (dòng 73-74).
  Cập nhật docstring (dòng 67) bỏ phần nhắc tới query — giờ nằm trong `table_info.queries`.

  Thêm hàm `normalize_queries(items: list[QueryItem]) -> list[dict[str, Any]]` ngay sau
  `normalize_fields` (dòng 154): trả `[{"datasourceId": item.datasource_id, "datasourceType":
  item.datasource_type, "query": item.query} for item in items]`. Docstring tiếng Việt: chuẩn hoá
  về list dict camelCase để ghi vào cột JSONB — giữ camelCase (khác `normalize_fields` dùng khoá
  đã sẵn cùng tên) để output API đọc lại không phải remap khoá.

  Thêm hàm `find_duplicate_datasource_id(queries: list[QueryItem]) -> str | None` ngay sau
  `find_duplicate_form_uuid` (dòng 168), cùng khuôn (dùng `set` tích luỹ, trả phần tử trùng đầu
  tiên). Docstring tiếng Việt tương tự `find_duplicate_form_uuid`, nhắc rõ đây là kiểm trong PHẠM VI
  1 form, không phải toàn batch.

- [ ] **Step 4: Chạy test, xác nhận PASS**

  Run: `pytest tests/test_ai_sync_schema.py -v`
  Expected: PASS toàn bộ.

- [ ] **Step 5: Commit**

  ```bash
  git add backend/apps/hooks/schemas/ai_sync_schema.py tests/test_ai_sync_schema.py
  git commit -m "feat: thay postgresQuery/clickHouseQuery bằng mảng queries tổng quát trong input schema"
  ```

---

## Task 2: Mã lỗi `DUPLICATE_DATASOURCE_ID` + kiểm trùng ở handler

**Files:**
- Modify: `backend/apps/hooks/constants.py` (class `SyncErrorCode` dòng 48-60)
- Modify: `backend/apps/hooks/handlers/authorization.py` (import dòng 19-26; vòng lặp validate cấu
  trúc dòng 69-76)
- Test: `tests/test_ai_sync_handler.py`

**Interfaces:**
- Consumes: `find_duplicate_datasource_id` (Task 1).
- Produces: `SyncErrorCode.DUPLICATE_DATASOURCE_ID`. Handler raise `SyncHookError(400,
  DUPLICATE_DATASOURCE_ID, ...)` khi có form trùng `datasourceId` — không thay đổi interface hàm
  `handle_authorization_sync`.

- [ ] **Step 1: Cập nhật helper `_user()` sang `queries[]` và viết test trùng `datasourceId`**

  Trong `tests/test_ai_sync_handler.py`, hàm `_user()` (dòng 20-33) đang dựng `tableInfo` với
  `"postgresQuery": f"SELECT * FROM {table}"` (dòng 29) ở SAI cấp (cấp `formQueries[i]`, không phải
  trong `tableInfo`) — sửa thành: bỏ khoá `postgresQuery` khỏi dict `formQueries[i]`, thêm khoá
  `"queries": [{"datasourceId": f"ds-{form_uuid}", "datasourceType": "postgresql", "query": f"SELECT
  * FROM {table}"}]` vào bên trong dict `"tableInfo"`.

  Thêm test mới `test_trung_datasource_id_trong_1_form_raise_duplicate_datasource_id`: dựng batch 1
  user với 1 form có `tableInfo.queries` gồm 2 phần tử cùng `datasourceId="d1"` (dùng dict thô, không
  qua `_user()` vì cần kiểm soát trùng lặp thủ công) — gọi `handle_authorization_sync`, assert raise
  `SyncHookError` với `exc.value.http_status == 400`, `exc.value.error_code is
  SyncErrorCode.DUPLICATE_DATASOURCE_ID`, `"d1" in exc.value.message`, và
  `get_user_permissions(db_session, <user_id>) == []` (không ai trong batch được áp dụng — cùng
  khuôn `test_trung_form_uuid_trong_1_user_raise_duplicate_form_uuid` dòng 105-111).

- [ ] **Step 2: Chạy test, xác nhận FAIL**

  Run: `pytest tests/test_ai_sync_handler.py -v`
  Expected: FAIL — hầu hết test FAIL vì `_user()` sinh payload thiếu `queries` bắt buộc (Task 1) hoặc
  còn field cũ đã bị xoá khỏi schema; test mới FAIL vì `SyncErrorCode.DUPLICATE_DATASOURCE_ID` chưa
  tồn tại và handler chưa kiểm trùng.

- [ ] **Step 3: Thêm mã lỗi + kiểm trùng trong handler**

  Trong `constants.py`, thêm `DUPLICATE_DATASOURCE_ID = "DUPLICATE_DATASOURCE_ID"` vào
  `SyncErrorCode` (dòng 48-60), đặt sau `DUPLICATE_FORM_UUID`.

  Trong `authorization.py`, thêm `find_duplicate_datasource_id` vào import từ `ai_sync_schema` (dòng
  19-26). Trong vòng lặp validate cấu trúc theo từng user (dòng 69-76, đang kiểm `find_duplicate_form_uuid`),
  lồng thêm vòng lặp theo từng `form` trong `user_data.form_queries`: gọi
  `find_duplicate_datasource_id(form.table_info.queries)`, trùng thì raise `SyncHookError(400,
  SyncErrorCode.DUPLICATE_DATASOURCE_ID, f"datasourceId bị lặp trong payload của user
  {user_data.user_id}, form {form.form_uuid}: {dup}")`. Đặt SAU bước kiểm `duplicated_form` của cùng
  user (kiểm form trùng trước, kiểm datasource trùng sau — cùng nguyên tắc all-or-nothing, raise
  sớm nhất có thể).

- [ ] **Step 4: Chạy test, xác nhận PASS**

  Run: `pytest tests/test_ai_sync_handler.py -v`
  Expected: PASS toàn bộ.

- [ ] **Step 5: Commit**

  ```bash
  git add backend/apps/hooks/constants.py backend/apps/hooks/handlers/authorization.py tests/test_ai_sync_handler.py
  git commit -m "feat: thêm kiểm trùng datasourceId trong cùng 1 form"
  ```

---

## Task 3: DB model + migration — cột `queries` thay `postgres_query`/`clickhouse_query`

**Files:**
- Modify: `backend/apps/hooks/models/ai_sync_model.py` (class `AiUserPermission` dòng 76-126)
- Create: `backend/alembic/versions/076_ai_user_permissions_queries.py`
- Test: `tests/test_ai_sync_models.py`

**Interfaces:**
- Produces: `AiUserPermission.queries: list[dict[str, Any]]`. Không còn `.postgres_query`/
  `.clickhouse_query`. Task 4 dùng tên cột này.

- [ ] **Step 1: Sửa test model**

  Trong `tests/test_ai_sync_models.py`, sửa `test_permission_co_du_cot_va_unique_user_form` (dòng
  36-51): trong tuple tên cột đang duyệt (dòng 38-43), xoá `"postgres_query"`, `"clickhouse_query"`,
  thêm `"queries"`.

- [ ] **Step 2: Chạy test, xác nhận FAIL**

  Run: `pytest tests/test_ai_sync_models.py -v`
  Expected: FAIL — thiếu cột `queries` (`postgres_query`/`clickhouse_query` vẫn còn nên không lộ ra
  qua assert "thiếu cột", nhưng assert mới về `queries` sẽ FAIL).

- [ ] **Step 3: Sửa model**

  Trong `AiUserPermission` (dòng 76-126): xoá 2 dòng field `postgres_query`/`clickhouse_query` (dòng
  115-116), thêm ngay tại đúng vị trí đó:

  ```python
  queries: list[dict[str, Any]] = Field(
      default_factory=list, sa_column=Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
  )
  ```

  (cùng khuôn cột `fields` dòng 112-114). Cập nhật docstring class (dòng 77-85), câu "`postgres_query`
  / `clickhouse_query` lưu nguyên văn..." (dòng 80) sửa thành: `queries` lưu nguyên mảng
  `{datasourceId, datasourceType, query}` theo từng datasource của form, giữ khoá camelCase để output
  API đọc thẳng không cần remap.

- [ ] **Step 4: Chạy test, xác nhận PASS**

  Run: `pytest tests/test_ai_sync_models.py -v`
  Expected: PASS toàn bộ.

- [ ] **Step 5: Viết migration Alembic**

  Tạo `backend/alembic/versions/076_ai_user_permissions_queries.py` theo đúng khuôn
  `075_ai_user_permissions_domain_info.py` (đọc file đó lấy format `revision`/`down_revision`).
  `down_revision = '075a1b2c3d4e'`. `upgrade()`: `op.add_column('ai_user_permissions',
  sa.Column('queries', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"),
  nullable=False))`, sau đó `op.drop_column('ai_user_permissions', 'postgres_query')`,
  `op.drop_column('ai_user_permissions', 'clickhouse_query')`. `downgrade()` làm ngược lại theo đúng
  thứ tự an toàn: thêm lại `clickhouse_query`/`postgres_query` (nullable, kiểu `Text`) trước, rồi mới
  `op.drop_column('ai_user_permissions', 'queries')`.

- [ ] **Step 6: Chạy migration trên DB dev, xác nhận không lỗi**

  DB dev dùng chung (`192.168.9.89:5434/sqlbot`) đã quen thuộc từ lần trước — hỏi user xác nhận
  trước khi chạy nếu chưa có `SQLBOT_TEST_DB_URL`/biến `POSTGRES_*` sẵn trong session, vì lần trước
  từng phát hiện lệch `alembic_version` phải xử lý thủ công.

  Run: `cd backend && alembic upgrade head`
  Expected: chạy thành công, `alembic current` cho thấy revision `076a1b2c3d4e` (hoặc id đã đặt ở
  Step 5).

  Xác minh trực tiếp: kết nối Postgres, `SELECT column_name FROM information_schema.columns WHERE
  table_name='ai_user_permissions'` — có `queries`, không còn `postgres_query`/`clickhouse_query`.

- [ ] **Step 7: Commit**

  ```bash
  git add backend/apps/hooks/models/ai_sync_model.py backend/alembic/versions/076_ai_user_permissions_queries.py tests/test_ai_sync_models.py
  git commit -m "feat: thay postgres_query/clickhouse_query bằng cột queries trên ai_user_permissions"
  ```

---

## Task 4: Ghi `queries` khi upsert quyền (`replace_user_permissions`)

**Files:**
- Modify: `backend/apps/hooks/crud/ai_user_permission.py` (import dòng 11; dict `values` dòng 52-70)
- Test: `tests/test_ai_sync_permission_crud.py`

**Interfaces:**
- Consumes: `AiUserPermission.queries` (Task 3), `TableInfo.queries`, `normalize_queries` (Task 1).
- Produces: `replace_user_permissions(...)` ghi đúng cột `queries`.

**Lưu ý khi chạy test:** fixture `db_session` TRUNCATE cả `ai_sync_hook_logs` lẫn `ai_user_permissions`
trên DB dev dùng chung mỗi lần chạy — đã được user chấp nhận ở lần trước, nhắc lại để chắc chắn còn
hiệu lực trước khi chạy `pytest` với `SQLBOT_TEST_DB_URL` trỏ vào DB dev.

- [ ] **Step 1: Sửa helper `_forms()` và assertion trong test crud**

  Trong `tests/test_ai_sync_permission_crud.py`, hàm `_forms()` (dòng 7-27): xoá 2 dòng
  `"postgresQuery": f"SELECT * FROM {table}"` / `"clickHouseQuery": f"SELECT * FROM {table} SETTINGS
  x=1"` (dòng 21-22, đang SAI cấp — ngang `tableInfo` thay vì bên trong), thêm khoá `"queries":
  [{"datasourceId": f"ds-{form_uuid}", "datasourceType": "postgresql", "query": f"SELECT * FROM
  {table}"}, {"datasourceId": f"ds-{form_uuid}-ch", "datasourceType": "clickhouse", "query": f"SELECT
  * FROM {table} SETTINGS x=1"}]` vào bên trong dict `"tableInfo"`.

  Sửa `test_ghi_moi_snapshot` (dòng 60-81): xoá dòng 77-78 (`assert r1.postgres_query ==
  "SELECT * FROM t1"` / `assert r1.clickhouse_query == "SELECT * FROM t1 SETTINGS x=1"`), thay
  bằng assert `r1.queries == [{"datasourceId": "ds-f1", "datasourceType": "postgresql", "query":
  "SELECT * FROM t1"}, {"datasourceId": "ds-f1-ch", "datasourceType": "clickhouse", "query": "SELECT
  * FROM t1 SETTINGS x=1"}]`.

  Thêm test mới `test_upsert_cap_nhat_queries_khac`: gọi `replace_user_permissions` lần 1 với
  `_forms(("f1", "t1", ["a"]))` (có `queries` mặc định của helper), `sync_version=100`, commit; gọi
  lần 2 cùng `user_id`/`form_uuid` nhưng dựng `form_queries` thủ công (không qua `_forms()`) với
  `tableInfo.queries` chỉ 1 phần tử `{"datasourceId": "ds-moi", "datasourceType": "clickhouse",
  "query": "SELECT 2"}`, `sync_version=200`, commit; assert dòng kết quả có `queries == [{"datasourceId":
  "ds-moi", "datasourceType": "clickhouse", "query": "SELECT 2"}]` (ghi đè hoàn toàn, không giữ lại
  phần tử cũ).

- [ ] **Step 2: Chạy test, xác nhận FAIL**

  Run: `pytest tests/test_ai_sync_permission_crud.py -v`
  Expected: FAIL — `replace_user_permissions` vẫn ghi `postgres_query`/`clickhouse_query` (cột đã bị
  xoá ở Task 3 nên thực tế sẽ lỗi ở tầng SQL/attribute trước khi assert kịp chạy).

- [ ] **Step 3: Sửa `replace_user_permissions`**

  Thêm `normalize_queries` vào import từ `ai_sync_schema` (dòng 11, cạnh `normalize_fields`). Trong
  dict `values` (dòng 52-70): xoá 2 dòng `"postgres_query": form.postgres_query` / `"clickhouse_query":
  form.clickhouse_query` (dòng 65-66), thêm `"queries": normalize_queries(form.table_info.queries)`
  ngay sau dòng `"fields": normalize_fields(form.table_info.field_list)` (dòng 64).

- [ ] **Step 4: Chạy test, xác nhận PASS**

  Run: `pytest tests/test_ai_sync_permission_crud.py -v`
  Expected: PASS toàn bộ.

- [ ] **Step 5: Commit**

  ```bash
  git add backend/apps/hooks/crud/ai_user_permission.py tests/test_ai_sync_permission_crud.py
  git commit -m "feat: ghi mảng queries khi upsert quyền user"
  ```

---

## Task 5: Output API — `permission_query_schema.py`

**Files:**
- Modify: `backend/apps/hooks/schemas/permission_query_schema.py` (class `PermissionTableInfoOut`
  dòng 16-28; class `PermissionFormQueryOut` dòng 31-39; hàm `to_permission_detail` dòng 68-104)
- Test: `tests/test_ai_permission_query_schema.py`, `tests/test_ai_permission_query_api.py`

**Interfaces:**
- Consumes: `AiUserPermission.queries` (Task 3).
- Produces: `PermissionTableInfoOut.queries: list[dict[str, Any]]`. `PermissionFormQueryOut` không
  còn `postgresQuery`/`clickHouseQuery`.

- [ ] **Step 1: Sửa fixture và assertion trong test schema output**

  Trong `tests/test_ai_permission_query_schema.py`, hàm `_row()` (dòng 12-28): xoá 2 dòng
  `postgres_query="SELECT 1"` / `clickhouse_query="SELECT 1"` (dòng 26-27), thêm
  `queries=[{"datasourceId": "d1", "datasourceType": "postgresql", "query": "SELECT 1"}]`.

  Sửa `test_co_du_lieu_map_dung_field` (dòng 43-58): xoá dòng 57-58 (`assert form.postgresQuery ==
  "SELECT 1"` / `assert form.clickHouseQuery == "SELECT 1"`), thay bằng assert
  `form.tableInfo.queries == [{"datasourceId": "d1", "datasourceType": "postgresql", "query": "SELECT
  1"}]`.

  Rà `test_rong_tra_toan_null_va_form_queries_rong` và `test_nhieu_form_lay_sync_version_tu_dong_dau`
  — không đụng vào `postgresQuery`/`clickHouseQuery` nên không cần sửa, chỉ chạy lại để xác nhận vẫn
  PASS.

- [ ] **Step 2: Chạy test, xác nhận FAIL**

  Run: `pytest tests/test_ai_permission_query_schema.py -v`
  Expected: FAIL — `_row()` truyền `queries=...` vào `AiUserPermission(**defaults)` nhưng model chưa
  chắc đồng bộ nếu Task 3 chưa merge (giả định Task 3 đã xong trước đó trong cùng nhánh); `to_permission_detail`
  chưa map `queries` nên assert mới FAIL, đồng thời `postgres_query`/`clickhouse_query` không còn là
  keyword hợp lệ của `AiUserPermission` nên `_row()` với `postgres_query=...` (bản cũ) sẽ lỗi ngay
  khi khởi tạo.

- [ ] **Step 3: Sửa `PermissionTableInfoOut`, `PermissionFormQueryOut`, `to_permission_detail`**

  Trong `PermissionTableInfoOut` (dòng 16-28): thêm `queries: list[dict[str, Any]]`, đặt sau `fields`
  (dòng 28).

  Trong `PermissionFormQueryOut` (dòng 31-39): xoá 2 dòng `postgresQuery`/`clickHouseQuery` (dòng
  38-39).

  Trong `to_permission_detail` (dòng 68-104), khối dựng `PermissionTableInfoOut(...)` (dòng 89-98):
  thêm `queries=row.queries` ngay sau `fields=row.fields` (dòng 97). Xoá 2 dòng
  `postgresQuery=row.postgres_query` / `clickHouseQuery=row.clickhouse_query` (dòng 99-100) khỏi
  khối dựng `PermissionFormQueryOut(...)`.

- [ ] **Step 4: Chạy test, xác nhận PASS**

  Run: `pytest tests/test_ai_permission_query_schema.py -v`
  Expected: PASS toàn bộ.

- [ ] **Step 5: Sửa fixture trong test API đọc quyền**

  Trong `tests/test_ai_permission_query_api.py`, tìm chỗ dựng `AiUserPermission(...)` mock (dùng
  `postgres_query=None, clickhouse_query=None` — xem quanh dòng 133-134): xoá 2 keyword đó, thêm
  `queries=[{"datasourceId": "d1", "datasourceType": "postgresql", "query": "SELECT 1"}]`. Thêm assert
  `resp.json()["data"]["formQueries"][0]["tableInfo"]["queries"] == [{"datasourceId": "d1",
  "datasourceType": "postgresql", "query": "SELECT 1"}]` vào đúng test đang kiểm response chi tiết
  (test đang có mặt `linhVucMa` v.v. — thêm assert `queries` cạnh đó).

- [ ] **Step 6: Chạy test, xác nhận PASS**

  Run: `pytest tests/test_ai_permission_query_api.py -v`
  Expected: PASS toàn bộ.

- [ ] **Step 7: Commit**

  ```bash
  git add backend/apps/hooks/schemas/permission_query_schema.py tests/test_ai_permission_query_schema.py tests/test_ai_permission_query_api.py
  git commit -m "feat: phơi mảng queries qua API đọc quyền user"
  ```

---

## Task 6: Cập nhật test e2e sang `queries[]` + chạy toàn bộ test suite

**Files:**
- Modify: `tests/test_ai_sync_e2e.py` (hàm `_body()` dòng 27-57)
- Không sửa code chính — task này chỉ cập nhật fixture cuối cùng còn dùng format cũ và xác nhận
  toàn hệ thống (route → handler → CRUD → DB → API đọc) hoạt động nhất quán đầu-cuối với `queries[]`.

**Interfaces:**
- Consumes: toàn bộ Task 1-5.
- Produces: không có interface mới — đây là test tích hợp xác nhận lại các interface đã có hoạt
  động đúng khi ghép chung.

- [ ] **Step 1: Sửa `_body()` sang `queries[]`**

  Trong `_body()` (dòng 27-57): xoá 2 dòng `"postgresQuery": "SELECT * FROM
  kdl_nhan_khau_row_values WHERE province_id = '01'"` / `"clickHouseQuery": "SELECT * FROM
  kdl_nhan_khau_row_values WHERE province_id = '01'"` (dòng 49-50, đang ở cấp `formQueries[i]`),
  thêm vào bên trong `"tableInfo"` khoá `"queries": [{"datasourceId": "ds-pg", "datasourceType":
  "postgresql", "query": "SELECT * FROM kdl_nhan_khau_row_values WHERE province_id = '01'"}]`.

  Test `test_e2e_batch_1_user_loi_thi_khong_ai_duoc_ap_dung` (dòng 211-232) và
  `test_e2e_batch_nhieu_user_1_thanh_cong_1_stale` (dòng 170-208) dựng `tableInfo` thủ công riêng
  (không qua `_body()`) — các payload đó hiện dùng `"fields": [{"id": "a"}]` mà không có `"queries"`,
  cần thêm `"queries": [{"datasourceId": "d1", "datasourceType": "postgresql", "query": "SELECT
  1"}]` vào mỗi `tableInfo` trong 2 test này, nếu không sẽ FAIL vì `queries` bắt buộc (Task 1).

- [ ] **Step 2: Chạy riêng file e2e, xác nhận PASS**

  Run: `pytest tests/test_ai_sync_e2e.py -v`
  Expected: PASS toàn bộ (5 test, giữ nguyên số lượng — không thêm/bớt test case ở đây, chỉ đổi
  fixture).

- [ ] **Step 3: Commit**

  ```bash
  git add tests/test_ai_sync_e2e.py
  git commit -m "test: cập nhật test e2e sang mảng queries tổng quát"
  ```

- [ ] **Step 4: Chạy toàn bộ test suite của domain hook**

  Run: `pytest tests/test_ai_sync_schema.py tests/test_ai_sync_models.py tests/test_ai_sync_crud.py tests/test_ai_sync_permission_crud.py tests/test_ai_sync_handler.py tests/test_ai_sync_api.py tests/test_ai_sync_e2e.py tests/test_ai_sync_constants.py tests/test_ai_permission_query_schema.py tests/test_ai_permission_query_crud.py tests/test_ai_permission_query_api.py -v`
  Expected: PASS toàn bộ, không skip nào chuyển thành fail so với trước khi bắt đầu Task 1.

- [ ] **Step 5: Nêu đề xuất cập nhật tài liệu cho user duyệt (không tự sửa)**

  Theo `CLAUDE.md` và spec §11: đề xuất sửa `backend/scripts/ai_sync_hook/AI_SYNC_HOOK_API_SPEC.md`
  §4 — thay ví dụ JSON và bảng mô tả field: bỏ `formQueries[].postgresQuery`/
  `formQueries[].clickHouseQuery`, thêm `tableInfo.queries[]` và 3 field con
  `datasourceId`/`datasourceType`/`query`; §7 thêm dòng `DUPLICATE_DATASOURCE_ID` vào bảng lỗi. Và
  `backend/scripts/ai_sync_hook/AI_PERMISSION_QUERY_API_SPEC.md` — cập nhật ví dụ response, bỏ
  `postgresQuery`/`clickHouseQuery` ở cấp `formQueries[]`, thêm `queries[]` trong `tableInfo`. Trình
  bày rõ "sửa mục nào, thành gì" rồi chờ user quyết định trước khi đụng vào các file này.
