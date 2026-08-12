# AI Sync Hook — API đọc quyền user (dev/test)

> Bổ sung cho domain `apps/hooks/` (xem [2026-08-10-ai-sync-hook-design.md](2026-08-10-ai-sync-hook-design.md)
> và [2026-08-11-ai-sync-hook-batch-users-design.md](2026-08-11-ai-sync-hook-batch-users-design.md)).
> Chỉ thêm đường **đọc** dữ liệu `ai_user_permissions` — không đụng route ghi (`/hooks/ai-sync`),
> không đổi schema DB, không đọc pipeline Text2SQL.

## 1. Mục tiêu

Sau khi `AUTHORIZATION_SYNC` ghi quyền vào `ai_user_permissions`, dev/tester hiện không có cách nào
xem lại dữ liệu đã lưu ngoài việc query DB trực tiếp. Thêm 2 endpoint GET nội bộ để kiểm tra nhanh
khi debug SW gửi hook, hoặc khi phát triển pha tích hợp pipeline (đọc quyền) sau này.

## 2. Quyết định đã chốt

| Vấn đề | Quyết định | Lý do |
|---|---|---|
| Phạm vi dữ liệu | Chỉ `ai_user_permissions` (quyền hiện tại). Không đọc `ai_sync_hook_logs` (audit) | Đã đủ cho mục tiêu dev/test; audit log để dành cho nhu cầu debug sâu hơn sau này nếu phát sinh |
| Xác thực | Giống hook: JWT `X-SQLBOT-TOKEN` + `require_permissions(role=['ws_admin'])` | Dữ liệu quyền của user khác nhạy cảm; không thêm cơ chế xác thực riêng, nhất quán với mọi API admin trong repo |
| Response envelope | Trả Pydantic model thường (để `ResponseMiddleware` bọc `{code,data,msg}`) | Khác hook (route ghi phải trả raw JSON theo hợp đồng máy-máy với SW) — đây là API đọc nội bộ, theo convention chuẩn của SQLBot như `table_relation.py` |
| Audit log thao tác | Không gắn `system_log` | Convention hiện có chỉ audit hành động ghi/sửa, không audit GET |
| Response 1-user | Gom lại giống payload SW gửi (`{userId, fullName, isAdmin, formQueries}`), cộng `syncVersion`/`syncedAt` | Dễ đối chiếu "SW gửi gì — SQLBot lưu được gì" khi debug |
| User không có dòng nào trong `ai_user_permissions` | Trả **200**, `fullName/isAdmin/syncVersion/syncedAt = null`, `formQueries: []` — không trả 404 | "Hiện không có quyền nào" là trạng thái hợp lệ (có thể do chưa từng đồng bộ, hoặc đã bị thu hồi hết), không phải lỗi |
| Response tóm tắt tất cả user | `{userId, fullName, isAdmin, formCount, syncVersion, syncedAt}`, không kèm `formQueries` chi tiết | Gọn cho việc chọn `userId` rồi gọi tiếp endpoint chi tiết; tránh payload lớn khi nhiều user/form |
| Phân trang | Không có | Công cụ dev/test, quy mô dữ liệu nhỏ (YAGNI) |
| Sắp xếp | Theo `user_id` | Ổn định thứ tự, khớp `get_user_permissions` hiện có |

## 3. Giới hạn đã biết

Vì không đọc `ai_sync_hook_logs`, endpoint **không phân biệt được** "user chưa từng đồng bộ" với
"user đã bị SW thu hồi hết quyền" (`formQueries: []` trong lần AUTHORIZATION_SYNC gần nhất) — cả hai
trường hợp đều cho `ai_user_permissions` rỗng với `user_id` đó. Muốn phân biệt phải mở rộng đọc thêm
audit log — ngoài phạm vi thiết kế này.

## 4. Endpoint

```
GET /api/v1/hooks/ai-sync/permissions            -> tóm tắt tất cả user đang có quyền
GET /api/v1/hooks/ai-sync/permissions/{user_id}  -> chi tiết quyền của 1 user
```

### 4.1. `GET /hooks/ai-sync/permissions/{user_id}`

```json
{
  "userId": "usr-1",
  "fullName": "Nguyễn Văn A",
  "isAdmin": false,
  "syncVersion": 1786356000000,
  "syncedAt": "2026-08-11T10:00:00Z",
  "formQueries": [
    {
      "formUuid": "form-abcd-1234",
      "tableInfo": {
        "databaseTableName": "kdl_nhan_khau_row_values",
        "tableDisplayName": "Dữ liệu Nhân khẩu",
        "tableDescription": "Bảng lưu trữ thông tin cư trú của công dân",
        "fields": [
          {"id": "province_id", "name": "Mã Tỉnh/Thành", "description": "Mã định danh của Tỉnh/Thành phố"}
        ]
      },
      "postgresQuery": "SELECT * FROM kdl_nhan_khau_row_values WHERE province_id = '01'",
      "clickHouseQuery": "SELECT * FROM kdl_nhan_khau_row_values WHERE province_id = '01'"
    }
  ]
}
```

- `syncVersion`/`syncedAt` cấp cao nhất lấy từ dòng bất kỳ trong kết quả — mọi form của cùng 1 user
  luôn cùng `sync_version` tại một thời điểm (`replace_user_permissions` ghi đồng loạt giá trị này
  cho toàn bộ form trong lần áp dụng full-snapshot gần nhất).
- Không có dòng nào cho `user_id` này → HTTP 200, `fullName`/`isAdmin`/`syncVersion`/`syncedAt` đều
  `null`, `formQueries: []` (xem §3).

### 4.2. `GET /hooks/ai-sync/permissions`

```json
[
  {
    "userId": "usr-1", "fullName": "Nguyễn Văn A", "isAdmin": false,
    "formCount": 2, "syncVersion": 1786356000000, "syncedAt": "2026-08-11T10:00:00Z"
  },
  {
    "userId": "usr-2", "fullName": null, "isAdmin": true,
    "formCount": 0, "syncVersion": 1786356100000, "syncedAt": "2026-08-11T10:05:00Z"
  }
]
```

Mảng rỗng nếu chưa có user nào từng được đồng bộ.

## 5. Thay đổi code (không đổi schema DB, không đụng hook ghi)

| File | Thay đổi |
|---|---|
| `apps/hooks/crud/ai_user_permission.py` | Thêm `list_users_with_permission_summary(session) -> list[...]`: `GROUP BY user_id`, đếm số dòng, lấy `MAX(sync_version)`/`MAX(synced_at)`, `full_name`/`is_admin` của dòng bất kỳ (đại diện, vì cùng user thì các dòng luôn cùng giá trị — do `replace_user_permissions` ghi đồng loạt). Sắp theo `user_id`. `get_user_permissions` giữ nguyên, tái dùng cho endpoint chi tiết. |
| `apps/hooks/schemas/permission_query_schema.py` (**mới**) | `PermissionFormQueryOut` (formUuid, tableInfo lồng nhau, postgresQuery, clickHouseQuery), `UserPermissionDetailOut` (userId, fullName, isAdmin, syncVersion, syncedAt, formQueries), `UserPermissionSummaryOut` (userId, fullName, isAdmin, formCount, syncVersion, syncedAt). Hàm thuần `to_permission_detail(user_id, rows: list[AiUserPermission]) -> UserPermissionDetailOut` xử lý ca rỗng (trả toàn `null`/`[]`). |
| `apps/hooks/api/permission_query.py` (**mới**) | Router `prefix="/hooks/ai-sync/permissions"`, 2 route GET, cả hai `@require_permissions(permission=SqlbotPermission(role=['ws_admin']))`. Gọi crud rồi map sang schema output; không dùng `JSONResponse` thô (khác `ai_sync.py`) — trả thẳng Pydantic model/list để `ResponseMiddleware` tự bọc envelope chuẩn. |
| `apps/api.py` | Thêm `from apps.hooks.api import permission_query` và `api_router.include_router(permission_query.router)`. |

## 6. Kiểm thử

- `tests/test_ai_permission_query_schema.py` (mới, không cần DB): test `to_permission_detail` với
  danh sách rows rỗng (trả `null`/`[]` đúng) và với ≥1 row (map đúng field, `syncVersion`/`syncedAt`
  lấy đúng từ row).
- `tests/test_ai_permission_query_crud.py` (mới, cần Postgres — `skipif` thiếu `SQLBOT_TEST_DB_URL`
  giống các test crud khác của domain này): test `list_users_with_permission_summary` — nhiều user,
  `formCount` đúng, sắp theo `user_id`, user không có dòng nào không xuất hiện trong kết quả.
- `tests/test_ai_permission_query_api.py` (mới, không cần DB — patch tầng crud giống
  `test_ai_sync_api.py`): test xác thực (401 chưa đăng nhập, 500 thiếu quyền `ws_admin` — theo đúng
  quy ước chung đã ghi ở `BACKEND_ARCHITECTURE.md` §7), test response được bọc envelope
  `{code,data,msg}` (khác hẳn `/hooks/ai-sync` — đây là điểm cần test rõ vì dễ nhầm lẫn với hook ghi),
  test ca user không có dòng nào trả `200` với `formQueries: []`.

## 7. Tài liệu cần rà lại sau khi implement

Theo bảng ánh xạ trong `CLAUDE.md` (thêm endpoint mới) — **đề xuất, chưa tự sửa**:

- `backend/scripts/ai_sync_hook/AI_SYNC_HOOK_API_SPEC.md`: cân nhắc thêm mục mô tả 2 endpoint đọc
  này (tài liệu hiện tại chỉ nói về hook ghi `/hooks/ai-sync`), hoặc tách file spec riêng nếu tài
  liệu ghi/đọc lẫn lộn sẽ khó đọc — để user quyết định khi rà.
- `docs/BACKEND_ARCHITECTURE.md`: dòng mô tả `apps/hooks/` (hiện liệt kê `api/ai_sync.py`,
  `handlers/authorization.py`, `crud/ai_user_permission.py`) cần thêm `api/permission_query.py`.
