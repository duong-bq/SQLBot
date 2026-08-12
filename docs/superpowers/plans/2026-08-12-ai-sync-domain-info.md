# AI Sync Hook — lưu thông tin lĩnh vực (domain) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Lưu 4 trường lĩnh vực (`linhVucMa`/`linhVucUuid`/`linhVucName`/`linhVucDescription`) mà SW
gửi trong `tableInfo` của `AUTHORIZATION_SYNC` vào `ai_user_permissions`, và phơi lại đúng 4 trường
đó qua API đọc quyền đã có (`GET /hooks/ai-sync/permissions/{user_id}`).

**Architecture:** Thêm 4 cột nullable (`domain_code`, `domain_uuid`, `domain_name`,
`domain_description`) trực tiếp vào bảng/model `AiUserPermission` — không tách bảng riêng. Domain
info đi theo đúng đường dữ liệu đã có: parse ở `TableInfo` (input) → ghi ở
`replace_user_permissions` → đọc lại ở `to_permission_detail` (output). Không đổi route, không đổi
ngữ nghĩa FULL SNAPSHOT, không đụng pipeline Text2SQL.

**Tech Stack:** FastAPI, SQLModel/SQLAlchemy, Alembic, Pydantic, pytest (Postgres thật cho test crud
qua fixture `db_session`, không cần DB cho test schema/model/API nhờ patch).

## Global Constraints

- Tên cột/tên field tiếng Anh: `domain_code`, `domain_uuid`, `domain_name`, `domain_description` —
  không dùng `linh_vuc_*`.
- Cả 4 field/cột đều optional (nullable), không validate liên trường.
- Field output đặt tên alias giống hệt input SW gửi (`linhVucMa`, `linhVucUuid`, `linhVucName`,
  `linhVucDescription`) — đúng convention hiện có của `permission_query_schema.py`.
- Mọi hàm mới/sửa phải có docstring tiếng Việt (theo `CLAUDE.md`).
- Commit message 1 dòng, tiếng Việt, tiền tố Conventional Commits.
- Spec tham chiếu: `docs/superpowers/specs/2026-08-12-ai-sync-domain-info-design.md`.

---

## Task 1: Thêm cột domain vào model + migration DB

**Files:**
- Modify: `backend/apps/hooks/models/ai_sync_model.py` (class `AiUserPermission`, sau field
  `table_description` ở dòng 102; thêm index vào `__table_args__` ở dòng 84-88)
- Create: `backend/alembic/versions/075_ai_user_permissions_domain_info.py`
- Test: `tests/test_ai_sync_models.py`

**Interfaces:**
- Produces: `AiUserPermission.domain_code: str | None`, `.domain_uuid: str | None`,
  `.domain_name: str | None`, `.domain_description: str | None` — Task 2, 3, 5 dùng các tên này.
  Index tên `idx_ai_user_permissions_domain_code`.

- [ ] **Step 1: Viết test kiểm tra model có đủ cột và index mới**

  Trong `tests/test_ai_sync_models.py`, sửa `test_permission_co_du_cot_va_unique_user_form`: thêm
  `"domain_code"`, `"domain_uuid"`, `"domain_name"`, `"domain_description"` vào tuple tên cột đang
  duyệt ở dòng 38-41. Thêm test mới `test_permission_co_index_domain_code`: đọc
  `AiUserPermission.__table__.indexes`, assert `"idx_ai_user_permissions_domain_code"` có trong tập
  tên index (theo mẫu `test_permission_co_index_user_id_va_user_table` ở dòng 53-56).

- [ ] **Step 2: Chạy test, xác nhận FAIL**

  Run: `pytest tests/test_ai_sync_models.py -v`
  Expected: FAIL — thiếu cột `domain_code` (KeyError hoặc AssertionError) và thiếu index.

- [ ] **Step 3: Thêm 4 field vào model `AiUserPermission`**

  Thêm ngay sau `table_description` (dòng 102): 4 field kiểu `str | None`, `default=None`, cột
  SQLAlchemy tương ứng — `domain_code`/`domain_uuid` dùng `String(100)`, `domain_name` dùng
  `String(255)`, `domain_description` dùng `Text`; cả 4 `nullable=True`. Giữ đúng phong cách khai
  báo `sa_column=Column(...)` như các field `String`/`Text` khác trong cùng class.

  Thêm vào tuple `__table_args__` (dòng 84-88) một `Index("idx_ai_user_permissions_domain_code",
  "domain_code")`, đặt cạnh 2 index hiện có.

  Cập nhật docstring của class `AiUserPermission` (dòng 76-81) nếu cần nhắc thêm domain info là
  metadata đọc kèm.

- [ ] **Step 4: Chạy test, xác nhận PASS**

  Run: `pytest tests/test_ai_sync_models.py -v`
  Expected: PASS toàn bộ.

- [ ] **Step 5: Viết migration Alembic**

  Tạo `backend/alembic/versions/075_ai_user_permissions_domain_info.py` theo đúng khuôn của
  `074_ai_sync_hook.py` (đọc file đó để lấy đúng format `revision`/`down_revision`/`branch_labels`).
  `down_revision` trỏ về revision id của `074_ai_sync_hook.py`. Trong `upgrade()`:
  `op.add_column("ai_user_permissions", ...)` cho cả 4 cột (kiểu khớp Step 3, nullable, không
  default), sau đó `op.create_index("idx_ai_user_permissions_domain_code", "ai_user_permissions",
  ["domain_code"])`. `downgrade()` làm ngược lại: drop index rồi drop 4 cột.

- [ ] **Step 6: Chạy migration trên DB test, xác nhận không lỗi**

  Run: `cd backend && alembic upgrade head`
  Expected: chạy thành công, không lỗi; kiểm tra bằng `alembic current` thấy revision 075.

- [ ] **Step 7: Commit**

  ```bash
  git add backend/apps/hooks/models/ai_sync_model.py backend/alembic/versions/075_ai_user_permissions_domain_info.py tests/test_ai_sync_models.py
  git commit -m "feat: thêm cột lưu thông tin lĩnh vực vào ai_user_permissions"
  ```

---

## Task 2: Parse 4 field domain trong input schema (`TableInfo`)

**Files:**
- Modify: `backend/apps/hooks/schemas/ai_sync_schema.py` (class `TableInfo`, dòng 41-56)
- Test: `tests/test_ai_sync_schema.py`

**Interfaces:**
- Consumes: không phụ thuộc Task 1.
- Produces: `TableInfo.domain_code: str | None` (alias `linhVucMa`), `.domain_uuid: str | None`
  (alias `linhVucUuid`), `.domain_name: str | None` (alias `linhVucName`), `.domain_description:
  str | None` (alias `linhVucDescription`) — Task 3 dùng các tên thuộc tính này.

- [ ] **Step 1: Viết test parse domain fields**

  Trong `tests/test_ai_sync_schema.py`: thêm 4 khoá `linhVucMa`, `linhVucUuid`, `linhVucName`,
  `linhVucDescription` vào `tableInfo` của `VALID_DATA` (dòng 27-35), giá trị mẫu bám theo payload
  trong yêu cầu gốc (`"LV_DAN_CU"`, `"lv-uuid-5678"`, `"Lĩnh vực Dân cư"`, mô tả tương ứng).

  Thêm assertion vào `test_payload_hop_le_va_parse_dung_alias` (dòng 61-71): sau dòng kiểm
  `form.table_info.database_table_name`, assert `form.table_info.domain_code == "LV_DAN_CU"`,
  `.domain_uuid == "lv-uuid-5678"`, `.domain_name == "Lĩnh vực Dân cư"`, `.domain_description` đúng
  giá trị mẫu.

  Thêm test mới `test_table_info_thieu_domain_van_hop_le`: parse một `tableInfo` không có 4 khoá
  domain (dùng lại payload cũ trước khi sửa, ví dụ chỉ có `databaseTableName` + `fields` rỗng), assert
  cả 4 thuộc tính domain đều `None` — xác nhận tương thích ngược với SW cũ.

- [ ] **Step 2: Chạy test, xác nhận FAIL**

  Run: `pytest tests/test_ai_sync_schema.py -v`
  Expected: FAIL — `AttributeError` vì `TableInfo` chưa có `domain_code`.

- [ ] **Step 3: Thêm 4 field vào `TableInfo`**

  Trong `TableInfo` (dòng 41-56), thêm 4 field ngay sau `table_description` (dòng 55): kiểu
  `str | None`, `default=None`, alias lần lượt `"linhVucMa"`, `"linhVucUuid"`, `"linhVucName"`,
  `"linhVucDescription"`, đặt tên thuộc tính `domain_code`/`domain_uuid`/`domain_name`/
  `domain_description`. Cập nhật docstring của `TableInfo` (dòng 42-49) nhắc thêm ý domain là
  metadata optional, không ảnh hưởng ngữ nghĩa `fields` rỗng.

- [ ] **Step 4: Chạy test, xác nhận PASS**

  Run: `pytest tests/test_ai_sync_schema.py -v`
  Expected: PASS toàn bộ.

- [ ] **Step 5: Commit**

  ```bash
  git add backend/apps/hooks/schemas/ai_sync_schema.py tests/test_ai_sync_schema.py
  git commit -m "feat: parse thông tin lĩnh vực trong payload AUTHORIZATION_SYNC"
  ```

---

## Task 3: Ghi domain fields khi upsert quyền (`replace_user_permissions`)

**Files:**
- Modify: `backend/apps/hooks/crud/ai_user_permission.py` (hàm `replace_user_permissions`, dict
  `values` dòng 52-65)
- Test: `tests/test_ai_sync_crud.py`

**Interfaces:**
- Consumes: `AiUserPermission.domain_code/.domain_uuid/.domain_name/.domain_description` (Task 1),
  `FormQuery.table_info.domain_code/.domain_uuid/.domain_name/.domain_description` (Task 2).
- Produces: `replace_user_permissions(...)` ghi đủ 4 cột domain — Task 4 (API test) gián tiếp phụ
  thuộc dữ liệu này qua `get_user_permissions`.

- [ ] **Step 1: Viết test crud ghi và cập nhật domain fields**

  `tests/test_ai_sync_crud.py` hiện chỉ test `ai_sync_log.py` (không test
  `replace_user_permissions`) — thêm import `replace_user_permissions` từ
  `apps.hooks.crud.ai_user_permission` và `FormQuery`/`TableInfo`/`FieldItem` từ
  `apps.hooks.schemas.ai_sync_schema` (hoặc dựng `FormQuery.model_validate(...)` từ dict, theo mẫu
  `VALID_DATA` ở `test_ai_sync_schema.py`).

  Test mới `test_replace_user_permissions_ghi_du_domain_fields(db_session)`: gọi
  `replace_user_permissions` với 1 `form_queries` có đủ 4 field domain, `sync_version=100`. Sau đó
  `session.execute(select(AiUserPermission).where(...)).scalar_one()` (hoặc dùng
  `get_user_permissions` đã có sẵn trong `apps.hooks.crud.ai_user_permission`), assert
  `domain_code`/`domain_uuid`/`domain_name`/`domain_description` đúng giá trị đã gửi.

  Test mới `test_replace_user_permissions_upsert_cap_nhat_domain_khac`: gọi
  `replace_user_permissions` lần 1 với domain A, lần 2 (cùng `user_id`+`form_uuid`, `sync_version`
  lớn hơn) với domain B — assert dòng trong DB có domain B (không giữ giá trị cũ), xác nhận
  `on_conflict_do_update` cập nhật đúng cột mới.

- [ ] **Step 2: Chạy test, xác nhận FAIL**

  Run: `pytest tests/test_ai_sync_crud.py -v`
  Expected: FAIL — cột domain trong DB toàn `NULL` dù đã gửi giá trị.

- [ ] **Step 3: Thêm 4 khoá domain vào dict `values`**

  Trong `replace_user_permissions` (dòng 52-65), thêm vào dict `values`:
  `"domain_code": form.table_info.domain_code`, `"domain_uuid": form.table_info.domain_uuid`,
  `"domain_name": form.table_info.domain_name`, `"domain_description":
  form.table_info.domain_description`. Vì `on_conflict_do_update` ở dòng 68-71 dùng
  `{k: v for k, v in values.items() if k not in (...)}`, 4 khoá mới tự động được đưa vào `set_=`,
  không cần sửa gì thêm ở đó.

- [ ] **Step 4: Chạy test, xác nhận PASS**

  Run: `pytest tests/test_ai_sync_crud.py -v`
  Expected: PASS toàn bộ (bao gồm cả test cũ của `ai_sync_log.py` không bị ảnh hưởng).

- [ ] **Step 5: Commit**

  ```bash
  git add backend/apps/hooks/crud/ai_user_permission.py tests/test_ai_sync_crud.py
  git commit -m "feat: ghi thông tin lĩnh vực khi upsert quyền user"
  ```

---

## Task 4: Phơi domain fields qua API đọc quyền (`permission_query_schema.py`)

**Files:**
- Modify: `backend/apps/hooks/schemas/permission_query_schema.py` (class
  `PermissionTableInfoOut` dòng 16-24; hàm `to_permission_detail` dòng 64-96)
- Test: `tests/test_ai_permission_query_schema.py`, `tests/test_ai_permission_query_api.py`

**Interfaces:**
- Consumes: `AiUserPermission.domain_code/.domain_uuid/.domain_name/.domain_description` (Task 1).
- Produces: `PermissionTableInfoOut.linhVucMa/.linhVucUuid/.linhVucName/.linhVucDescription`
  (đều `str | None`).

- [ ] **Step 1: Viết test schema output có domain fields**

  Trong `tests/test_ai_permission_query_schema.py`: thêm `domain_code="LV_DAN_CU"`,
  `domain_uuid="lv-uuid-5678"`, `domain_name="Lĩnh vực Dân cư"`, `domain_description="..."` vào
  `defaults` của hàm `_row()` (dòng 13-27).

  Sửa `test_co_du_lieu_map_dung_field` (dòng 43-58): thêm assertion sau dòng kiểm
  `form.tableInfo.databaseTableName` — `form.tableInfo.linhVucMa == "LV_DAN_CU"`,
  `.linhVucUuid == "lv-uuid-5678"`, `.linhVucName == "Lĩnh vực Dân cư"`, `.linhVucDescription` đúng
  giá trị.

  Thêm test mới `test_domain_none_khong_loi`: gọi `_row()` với `domain_code=None` (và 3 field domain
  còn lại `None`), assert `to_permission_detail` chạy không lỗi và `linhVucMa` v.v. đều `None`.

- [ ] **Step 2: Chạy test, xác nhận FAIL**

  Run: `pytest tests/test_ai_permission_query_schema.py -v`
  Expected: FAIL — `PermissionTableInfoOut` chưa có `linhVucMa`.

- [ ] **Step 3: Thêm 4 field vào `PermissionTableInfoOut` và map trong `to_permission_detail`**

  Trong `PermissionTableInfoOut` (dòng 16-24), thêm 4 field `str | None = None`: `linhVucMa`,
  `linhVucUuid`, `linhVucName`, `linhVucDescription` — đặt sau `tableDescription`.

  Trong `to_permission_detail` (dòng 64-96), ở khối dựng `PermissionTableInfoOut(...)` (dòng 85-90),
  thêm 4 keyword argument map từ `row.domain_code`/`row.domain_uuid`/`row.domain_name`/
  `row.domain_description`.

- [ ] **Step 4: Chạy test, xác nhận PASS**

  Run: `pytest tests/test_ai_permission_query_schema.py -v`
  Expected: PASS toàn bộ.

- [ ] **Step 5: Viết/sửa test API xác nhận response có domain fields**

  Trong `tests/test_ai_permission_query_api.py`: tìm test đang mock
  `apps.hooks.api.permission_query.get_user_permissions` trả về danh sách row có dữ liệu mẫu (đọc
  toàn bộ file nếu cần để thấy fixture `crud_patches`/dữ liệu mock hiện dùng). Thêm domain fields vào
  object row mock (dùng `SimpleNamespace` hoặc instance `AiUserPermission` tuỳ theo file đang dùng
  gì), rồi assert response JSON của `GET /hooks/ai-sync/permissions/{user_id}` có
  `data.formQueries[0].tableInfo.linhVucMa` đúng giá trị mock.

- [ ] **Step 6: Chạy test, xác nhận PASS**

  Run: `pytest tests/test_ai_permission_query_api.py -v`
  Expected: PASS toàn bộ.

- [ ] **Step 7: Commit**

  ```bash
  git add backend/apps/hooks/schemas/permission_query_schema.py tests/test_ai_permission_query_schema.py tests/test_ai_permission_query_api.py
  git commit -m "feat: phơi thông tin lĩnh vực qua API đọc quyền user"
  ```

---

## Task 5: Chạy toàn bộ test suite domain hook + đề xuất cập nhật tài liệu

**Files:**
- Không sửa code — chỉ chạy test tổng hợp và chuẩn bị đề xuất tài liệu.

**Interfaces:**
- Consumes: toàn bộ Task 1-4.
- Produces: xác nhận không có test nào trong domain `ai_sync`/`ai_permission_query` bị vỡ; danh sách
  đề xuất sửa tài liệu để trình user duyệt (không tự sửa).

- [ ] **Step 1: Chạy toàn bộ test liên quan**

  Run: `pytest tests/test_ai_sync_schema.py tests/test_ai_sync_models.py tests/test_ai_sync_crud.py tests/test_ai_sync_api.py tests/test_ai_sync_e2e.py tests/test_ai_sync_handler.py tests/test_ai_sync_permission_crud.py tests/test_ai_sync_constants.py tests/test_ai_permission_query_schema.py tests/test_ai_permission_query_crud.py tests/test_ai_permission_query_api.py -v`
  Expected: PASS toàn bộ.

- [ ] **Step 2: Nêu đề xuất cập nhật tài liệu cho user duyệt (không tự sửa)**

  Theo `CLAUDE.md` và spec §7: đề xuất thêm 4 field domain vào bảng mô tả payload `tableInfo` trong
  `backend/scripts/ai_sync_hook/AI_SYNC_HOOK_API_SPEC.md` §4, kèm ví dụ JSON; và thêm 4 field domain
  vào ví dụ response + bảng mô tả `tableInfo` output trong
  `backend/scripts/ai_sync_hook/AI_PERMISSION_QUERY_API_SPEC.md`. Trình bày rõ "sửa mục nào, thành
  gì" rồi chờ user quyết định trước khi đụng vào các file này.
