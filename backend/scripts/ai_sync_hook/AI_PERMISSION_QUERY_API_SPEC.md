# Đặc tả API đọc quyền user (dev/test)

Dành cho dev/tester cần xem lại quyền đã được `POST /hooks/ai-sync` (`AUTHORIZATION_SYNC`) ghi vào
`ai_user_permissions`, không cần query DB trực tiếp. Xem
[AI_SYNC_HOOK_API_SPEC.md](AI_SYNC_HOOK_API_SPEC.md) để biết hợp đồng ghi (SW gửi bản tin vào hook).

Đây là API **đọc nội bộ**, khác hẳn hook: response bọc envelope `{code,data,msg}` theo convention
chuẩn của SQLBot, không phải raw JSON như hook.

---

## 1. Quy ước chung

**Base URL:** `https://<host>/api/v1`

**Xác thực:** giống hệt hook — JWT `X-SQLBOT-TOKEN: Bearer <access_token>` (lấy từ
`POST /login/access-token`), tài khoản phải có quyền `ws_admin` (hoặc admin toàn cục). Xem
[AI_SYNC_HOOK_API_SPEC.md §1](AI_SYNC_HOOK_API_SPEC.md#1-quy-ước-chung) để biết chi tiết luồng đăng
nhập — không lặp lại ở đây.

**Response:** bọc envelope chuẩn `{"code": 0, "data": ..., "msg": null}` — **khác** hook (hook trả
raw JSON không bọc envelope). `data` là nội dung mô tả ở §2/§3 dưới đây.

---

## 2. `GET /hooks/ai-sync/permissions` — tóm tắt tất cả user

Liệt kê tóm tắt quyền hiện tại của mọi user đang có ≥1 dòng trong `ai_user_permissions`. Dùng để
chọn nhanh `userId` cần xem chi tiết mà không cần biết trước.

**Response `data`:**

```json
[
  {
    "userId": "usr-1",
    "fullName": "Nguyễn Văn A",
    "isAdmin": false,
    "formCount": 2,
    "syncVersion": 1786356000000,
    "syncedAt": "2026-08-11T10:00:00Z"
  },
  {
    "userId": "usr-2",
    "fullName": null,
    "isAdmin": true,
    "formCount": 0,
    "syncVersion": 1786356100000,
    "syncedAt": "2026-08-11T10:05:00Z"
  }
]
```

Mảng rỗng nếu chưa có user nào từng được đồng bộ. Không phân trang, không sắp xếp theo tham số —
luôn sắp theo `userId`.

| Trường | Ý nghĩa |
|---|---|
| `userId` | Định danh user do SW gửi, lưu nguyên văn |
| `fullName` | Tên hiển thị, từ lần đồng bộ gần nhất |
| `isAdmin` | Cờ admin, từ lần đồng bộ gần nhất |
| `formCount` | Số form user đang có quyền |
| `syncVersion` | `sync_version` (epoch millis) của lần đồng bộ gần nhất |
| `syncedAt` | Thời điểm ghi dòng quyền gần nhất |

---

## 3. `GET /hooks/ai-sync/permissions/{user_id}` — chi tiết 1 user

Trả toàn bộ quyền hiện tại của 1 user, gom lại **giống hệt cấu trúc payload SW gửi vào hook**
(`AuthorizationSyncData` — xem [AI_SYNC_HOOK_API_SPEC.md §4](AI_SYNC_HOOK_API_SPEC.md#4-payload-authorization_sync-actiontype--1)),
cộng thêm `syncVersion`/`syncedAt` để dễ đối chiếu.

**Response `data`:**

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
        "linhVucMa": "LV_DAN_CU",
        "linhVucUuid": "lv-uuid-5678",
        "linhVucName": "Lĩnh vực Dân cư",
        "linhVucDescription": "Lĩnh vực quản lý các thông tin liên quan đến dân cư",
        "queries": [
          {"datasourceId": "ds-001", "datasourceType": "postgresql", "query": "SELECT * FROM kdl_nhan_khau_row_values WHERE province_id = '01'"},
          {"datasourceId": "ds-002", "datasourceType": "clickhouse", "query": "SELECT * FROM kdl_nhan_khau_row_values WHERE province_id = '01'"}
        ]
      }
    }
  ]
}
```

⚠ **`user_id` không có dòng nào trong `ai_user_permissions` → HTTP 200**, không phải 404:

```json
{
  "userId": "usr-khong-ton-tai",
  "fullName": null,
  "isAdmin": null,
  "syncVersion": null,
  "syncedAt": null,
  "formQueries": []
}
```

"Hiện không có quyền nào" là trạng thái hợp lệ — không phải lỗi. Trạng thái này xảy ra ở **hai** ca
mà endpoint **không phân biệt được** (vì không đọc `ai_sync_hook_logs`):

1. `user_id` chưa từng được SW đồng bộ lần nào.
2. `user_id` đã từng có quyền, nhưng lần `AUTHORIZATION_SYNC` gần nhất gửi `formQueries: []` (thu
   hồi hết).

Muốn phân biệt hai ca này phải tự query `ai_sync_hook_logs` theo `user_id` — ngoài phạm vi API này.

---

## 4. Bảng lỗi

| HTTP | Ý nghĩa |
|---|---|
| 200 | Thành công — kể cả khi user không có quyền nào (xem §3) |
| 401 | Thiếu/sai/hết hạn `X-SQLBOT-TOKEN` (xảy ra trước khi vào logic của route) |
| 500 | Tài khoản không có quyền `ws_admin` — quy ước lỗi chung toàn hệ thống, không phải riêng API này |

---

## 5. Ví dụ `curl`

```bash
TOKEN=$(curl -s -X POST "https://<host>/api/v1/login/access-token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=<tai-khoan>&password=<mat-khau>" | jq -r .access_token)

# Tóm tắt tất cả user
curl -s "https://<host>/api/v1/hooks/ai-sync/permissions" \
  -H "X-SQLBOT-TOKEN: Bearer $TOKEN" | jq .

# Chi tiết 1 user
curl -s "https://<host>/api/v1/hooks/ai-sync/permissions/usr-12345-67890" \
  -H "X-SQLBOT-TOKEN: Bearer $TOKEN" | jq .
```
