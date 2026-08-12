# AI Permission Query API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Thêm 2 endpoint GET nội bộ (`/hooks/ai-sync/permissions`, `/hooks/ai-sync/permissions/{user_id}`) để dev/test xem lại quyền đã lưu trong `ai_user_permissions` sau khi nhận hook từ SW.

**Architecture:** Thêm 1 hàm CRUD đọc mới (GROUP BY user), 1 file schema output riêng cho phía đọc, 1 router mới dùng chung xác thực `ws_admin` với hook nhưng trả response theo convention chuẩn của SQLBot (có envelope `{code,data,msg}`), khác với route ghi `/hooks/ai-sync` vốn trả raw JSON.

**Tech Stack:** Python, FastAPI, Pydantic v2, SQLModel/SQLAlchemy, Postgres.

## Global Constraints

- Không đổi schema DB, không đụng route ghi `/hooks/ai-sync` hay handler `authorization.py`.
- Xác thực: JWT `X-SQLBOT-TOKEN` + `require_permissions(role=['ws_admin'])` — giống hệt hook, không thêm cơ chế riêng.
- Không phân trang (dev/test, quy mô nhỏ — YAGNI).
- User không có dòng nào trong `ai_user_permissions` → HTTP 200 (không 404), field liên quan để `null`/`[]`.
- Docstring hàm mới bằng tiếng Việt (quy ước CLAUDE.md).
- Tham chiếu đầy đủ: [`docs/superpowers/specs/2026-08-12-ai-permission-query-api-design.md`](../specs/2026-08-12-ai-permission-query-api-design.md).

---

### Task 1: CRUD — tóm tắt quyền theo user

**Files:**
- Modify: `backend/apps/hooks/crud/ai_user_permission.py`
- Test: `tests/test_ai_permission_query_crud.py` (mới, cần Postgres — skip nếu thiếu `SQLBOT_TEST_DB_URL`)

**Đầu ra:** Hàm `list_users_with_permission_summary(session) -> list[dict]`, mỗi phần tử gồm `user_id`, `full_name`, `is_admin`, `form_count`, `sync_version`, `synced_at` — 1 dòng cho mỗi `user_id` phân biệt đang có ≥1 dòng trong `ai_user_permissions`, sắp theo `user_id`. Dùng `GROUP BY user_id`, `COUNT(*)`, `MAX(sync_version)`, `MAX(synced_at)`.

**Test cần phủ:** nhiều user cho ra đúng `form_count`/số dòng kết quả; user không có dòng nào không xuất hiện; thứ tự theo `user_id`.

- [ ] Viết test trước (assert trên dữ liệu đã seed qua `replace_user_permissions` có sẵn), chạy xác nhận FAIL (hàm chưa tồn tại).
- [ ] Implement hàm trong `ai_user_permission.py`, đặt ngay dưới `get_user_permissions`.
- [ ] Chạy lại test xác nhận PASS.
- [ ] Commit: `feat: thêm list_users_with_permission_summary`.

---

### Task 2: Schema output cho phía đọc

**Files:**
- Create: `backend/apps/hooks/schemas/permission_query_schema.py`
- Test: `tests/test_ai_permission_query_schema.py` (mới, không cần DB)

**Interfaces:**
- Consumes: `AiUserPermission` (model, đã có ở `apps/hooks/models/ai_sync_model.py`), `normalize_fields`-style dữ liệu `fields` (đã là `list[dict]` sẵn trong model, không cần convert lại).
- Produces:
  - `PermissionFormQueryOut` — `formUuid`, `tableInfo` (lồng `databaseTableName`/`tableDisplayName`/`tableDescription`/`fields`), `postgresQuery`, `clickHouseQuery`.
  - `UserPermissionDetailOut` — `userId`, `fullName`, `isAdmin`, `syncVersion`, `syncedAt`, `formQueries: list[PermissionFormQueryOut]`.
  - `UserPermissionSummaryOut` — `userId`, `fullName`, `isAdmin`, `formCount`, `syncVersion`, `syncedAt`.
  - Hàm thuần `to_permission_detail(user_id: str, rows: list[AiUserPermission]) -> UserPermissionDetailOut` — Task 3 gọi hàm này để map kết quả `get_user_permissions` sang response.

**Đầu ra:** `to_permission_detail(user_id, [])` trả object toàn `null`/`formQueries=[]`; với `rows` không rỗng, map đúng field từng dòng thành `PermissionFormQueryOut`, lấy `syncVersion`/`syncedAt` từ dòng đầu tiên (mọi dòng cùng user luôn cùng giá trị).

- [ ] Viết test trước cho `to_permission_detail` (ca rỗng + ca có dữ liệu), chạy xác nhận FAIL.
- [ ] Implement 3 class Pydantic + hàm `to_permission_detail` trong file mới.
- [ ] Chạy lại test xác nhận PASS.
- [ ] Commit: `feat: thêm schema output cho API đọc quyền`.

---

### Task 3: Router 2 endpoint + đăng ký vào app

**Files:**
- Create: `backend/apps/hooks/api/permission_query.py`
- Modify: `backend/apps/api.py` (thêm import + `include_router`)
- Test: `tests/test_ai_permission_query_api.py` (mới, không cần DB — patch tầng crud giống `test_ai_sync_api.py`)

**Interfaces:**
- Consumes: `list_users_with_permission_summary` (Task 1), `get_user_permissions` (đã có), `to_permission_detail`, `UserPermissionSummaryOut` (Task 2).
- Produces: router `permission_query.router`, prefix `/hooks/ai-sync/permissions`, 2 route:
  - `GET ""` → `list[UserPermissionSummaryOut]`
  - `GET "/{user_id}"` → `UserPermissionDetailOut`

**Đầu ra:** Cả 2 route gắn `@require_permissions(permission=SqlbotPermission(role=['ws_admin']))`. Trả thẳng Pydantic model/list (KHÔNG dùng `JSONResponse` thô như `ai_sync.py`) để `ResponseMiddleware` tự bọc `{code,data,msg}`.

**Test cần phủ:** 401 khi chưa đăng nhập; 500 khi thiếu quyền `ws_admin` (đúng quy ước chung của repo); response 2 endpoint được bọc envelope `{code,data,msg}` (khác hẳn `/hooks/ai-sync`); ca user không có dòng nào trả 200 với `formQueries: []`.

- [ ] Viết test trước cho router (patch crud), chạy xác nhận FAIL (router/hàm chưa tồn tại).
- [ ] Implement router trong file mới, đăng ký vào `apps/api.py`.
- [ ] Chạy lại test xác nhận PASS.
- [ ] Commit: `feat: thêm router đọc quyền user AI Sync Hook`.

---

### Task 4: Chạy toàn bộ test + rà tài liệu

**Files:**
- Modify (đề xuất, chờ duyệt trước khi sửa): `backend/scripts/ai_sync_hook/AI_SYNC_HOOK_API_SPEC.md`, `docs/BACKEND_ARCHITECTURE.md`

**Đầu ra:**
- Chạy toàn bộ `tests/` (không riêng domain hook) để xác nhận không hồi quy.
- Đề xuất với user: thêm mô tả 2 endpoint đọc mới vào `AI_SYNC_HOOK_API_SPEC.md` (hoặc tách file riêng nếu ghi/đọc lẫn lộn khó đọc), và cập nhật dòng liệt kê file trong `BACKEND_ARCHITECTURE.md` (`apps/hooks/` cần thêm `api/permission_query.py`) — theo đúng quy ước "đề xuất, không tự sửa" của CLAUDE.md. Chỉ sửa khi user đồng ý.

- [ ] Chạy `pytest tests/ -v`, xác nhận PASS/SKIPPED, không FAIL.
- [ ] Trình bày đề xuất sửa tài liệu cho user, chờ quyết định.

---

## Self-Review

**Spec coverage:** §2 (quyết định) → Task 1–3 (crud/schema/router theo đúng lựa chọn). §3 (giới hạn đã biết) → không cần task riêng, đã phản ánh trong hành vi "200 khi rỗng" ở Task 2/3. §4 (2 endpoint + response) → Task 3. §6 (test) → phủ đủ 3 file test tương ứng Task 1–3. §7 (tài liệu) → Task 4.

**Placeholder scan:** không còn "TBD"/mô tả mơ hồ — mỗi task nêu rõ tên hàm, field, hành vi kỳ vọng.

**Type consistency:** `to_permission_detail(user_id, rows) -> UserPermissionDetailOut` định nghĩa ở Task 2, dùng đúng tên này ở Task 3. `list_users_with_permission_summary` định nghĩa ở Task 1, dùng đúng tên ở Task 3.
