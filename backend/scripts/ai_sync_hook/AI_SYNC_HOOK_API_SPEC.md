# Đặc tả API AI Sync Hook

Dành cho hệ thống SW cần đồng bộ quyền user sang SQLBot.

Phạm vi phase hiện tại: **1 endpoint**, **1 actionType** (`AUTHORIZATION_SYNC`). Đọc phần quyền này
trong pipeline Text2SQL (bơm vào M-Schema, chèn `WHERE`) **chưa** được triển khai — endpoint dưới
đây chỉ nhận, xác thực và lưu.

---

## 1. Quy ước chung

**Base URL:** `https://<host>/api/v1`

**Endpoint:** `POST /hooks/ai-sync`

**Xác thực:** header `Authorization: Bearer <AI_SYNC_HOOK_TOKEN>` — token tĩnh cấu hình qua biến
môi trường, **khác** JWT `X-SQLBOT-TOKEN` dùng cho các API khác của SQLBot. Không có refresh, không
hết hạn.

**Headers bắt buộc:**

| Header | Bắt buộc | Ý nghĩa |
|---|---|---|
| `Content-Type: application/json` | Có | |
| `Authorization: Bearer <token>` | Có | Xác thực |
| `X-Idempotency-Key: <UUID>` | **Có** | Chống xử lý trùng khi retry — xem §5 |
| `X-Request-ID: <UUID>` | Không | Chỉ để trace log, vọng lại nguyên văn trong response |

**Response:** JSON thô, **không** bọc theo envelope `{code,data,msg}` mà các API SQLBot khác dùng.
HTTP status phản ánh đúng kết quả (2xx thành công, 4xx lỗi phía SW, 5xx lỗi phía SQLBot).

```json
{
  "requestId": "req-001",
  "idempotencyKey": "550e8400-e29b-41d4-a716-446655440000",
  "actionType": 1,
  "status": "SUCCESS",
  "logId": "b3f1c2a0-...-uuid",
  "userId": "usr-12345-67890",
  "applied": {"upserted": 2, "deleted": 0},
  "errorCode": null,
  "errorMessage": null
}
```

---

## 2. Action Type

`actionType` là **integer**, không phải string.

| actionType | Constant | Ý nghĩa | Trạng thái |
|---|---|---|---|
| 1 | `AUTHORIZATION_SYNC` | Đồng bộ quyền user | **Đã triển khai** |
| 2 | `USER_SYNC` | Đồng bộ thông tin user | Chưa triển khai |
| 3 | `ORGANIZATION_SYNC` | Đồng bộ tổ chức | Chưa triển khai |
| 4 | `DATASOURCE_SYNC` | Đồng bộ datasource | Chưa triển khai |
| 5 | `KNOWLEDGE_SYNC` | Đồng bộ knowledge | Chưa triển khai |
| 6 | `DOCUMENT_SYNC` | Đồng bộ document | Chưa triển khai |

Gửi actionType 2–6 nhận **422 `UNSUPPORTED_ACTION_TYPE`** — không bị bỏ qua âm thầm. Gửi actionType
ngoài 1–6 (kể cả 0, string, thiếu trường) nhận **422 `UNKNOWN_ACTION_TYPE`**.

Một actionType đã release thì không được đổi ý nghĩa.

---

## 3. Envelope request

```json
{
  "actionType": 1,
  "version": "1.0",
  "timestamp": "2026-08-10T10:00:00Z",
  "data": { "...": "..." }
}
```

| Trường | Kiểu | Bắt buộc | Ghi chú |
|---|---|---|---|
| `actionType` | integer | Có | Xem §2 |
| `version` | string | Có | Version schema, hiện tại `"1.0"` |
| `timestamp` | ISO 8601 datetime | Có | **Dùng làm mốc chống bản tin đến sai thứ tự — xem §6.** Đồng hồ SW phải đồng bộ NTP |
| `data` | object | Có | Payload theo `actionType`, xem §4 |

---

## 4. Payload AUTHORIZATION_SYNC (`actionType = 1`)

```json
{
  "userId": "usr-12345-67890",
  "fullName": "Nguyễn Văn A",
  "isAdmin": false,
  "formQueries": [
    {
      "formUuid": "form-abcd-1234",
      "tableInfo": {
        "databaseTableName": "kdl_nhan_khau_row_values",
        "tableDisplayName": "Dữ liệu Nhân khẩu",
        "tableDescription": "Bảng lưu trữ thông tin cư trú của công dân",
        "fields": [
          {
            "id": "province_id",
            "name": "Mã Tỉnh/Thành",
            "description": "Mã định danh của Tỉnh/Thành phố"
          },
          {
            "id": "full_name",
            "name": "Họ và tên",
            "description": "Tên đầy đủ của công dân"
          }
        ]
      },
      "postgresQuery": "SELECT * FROM kdl_nhan_khau_row_values WHERE province_id = '01'",
      "clickHouseQuery": "SELECT * FROM kdl_nhan_khau_row_values WHERE province_id = '01'"
    }
  ]
}
```

⚠ Alias trong JSON là `clickHouseQuery` (chữ **H** hoa) — gõ sai thành `clickhouseQuery` sẽ khiến
trường đó bị bỏ qua (coi như không có) chứ không báo lỗi.

| Trường | Kiểu | Bắt buộc | Ghi chú |
|---|---|---|---|
| `userId` | string | Có | Không được rỗng |
| `fullName` | string | Không | |
| `isAdmin` | boolean | Có | |
| `formQueries` | array | Có | **Có thể rỗng** — xem §5 |
| `formQueries[].formUuid` | string | Có | Không được rỗng, không được lặp trong cùng request |
| `formQueries[].tableInfo` | object | Có | |
| `formQueries[].postgresQuery` | string | Không | |
| `formQueries[].clickHouseQuery` | string | Không | |
| `tableInfo.databaseTableName` | string | Có | |
| `tableInfo.tableDisplayName` | string | Không | |
| `tableInfo.tableDescription` | string | Không | |
| `tableInfo.fields` | array | Có | **Có thể rỗng** |
| `fields[].id` | string | Có | |
| `fields[].name` | string | Không | |
| `fields[].description` | string | Không | |

Trùng `formUuid` trong cùng một request nhận **400 `DUPLICATE_FORM_UUID`**.

---

## 5. Ngữ nghĩa FULL SNAPSHOT — đọc kỹ trước khi tích hợp

**`formQueries` là toàn bộ quyền hiện tại của user, không phải phần thay đổi.**

- Mỗi lần gọi thành công, SQLBot **thay thế toàn bộ** danh sách quyền của `userId` bằng đúng những
  gì có trong `formQueries`.
- Form nào **không có mặt** trong `formQueries` sẽ **bị xoá quyền** — kể cả khi trước đó user đã có
  quyền trên form đó. Đây không phải "giữ nguyên", mà là "thu hồi".
- `formQueries: []` nghĩa là **thu hồi toàn bộ quyền** của user.

Nói cách khác: mỗi lần gọi hook, SW phải gửi đủ **toàn bộ** danh sách form user được phép truy cập
tại thời điểm đó, không chỉ phần vừa thêm/sửa.

---

## 6. Idempotency và chống bản tin đến sai thứ tự

**`X-Idempotency-Key` là bắt buộc.** Gửi lại đúng key đã dùng trước đó (ví dụ do timeout mạng và
SW tự động retry) sẽ **không xử lý lại** — trả 200 với `status: "DUPLICATE"` kèm kết quả của lần
xử lý đầu tiên (kể cả khi lần đầu đó FAILED). Client an toàn khi retry vô hạn với cùng key.

**Chống bản tin lùi:** SQLBot dùng `timestamp` trong envelope để suy ra mốc thời gian của bản tin.
Nếu một bản tin đến muộn (do mạng, do retry với `X-Idempotency-Key` **mới**) nhưng `timestamp` của
nó **không mới hơn** bản tin gần nhất đã xử lý thành công cho cùng `userId`, SQLBot **bỏ qua** bản
tin đó — trả 200 với `status: "STALE"`, **không** áp dụng vào dữ liệu.

⚠ Hệ quả vận hành: nếu đồng hồ hệ thống SW lệch, bản tin mới có thể bị coi là cũ và bị bỏ qua trong
khi HTTP status vẫn là 200. Phải kiểm `status` trong response, không chỉ HTTP status code, để biết
bản tin có thực sự được áp dụng hay không.

---

## 7. Bảng lỗi

| HTTP | `status` | `errorCode` | Ý nghĩa |
|---|---|---|---|
| 200 | `SUCCESS` | — | Áp dụng thành công |
| 200 | `DUPLICATE` | (của lần đầu) | Trùng `X-Idempotency-Key`, trả lại kết quả cũ |
| 200 | `STALE` | — | Bản tin cũ hơn mốc đã áp dụng, bị bỏ qua có chủ ý |
| 400 | `FAILED` | `MALFORMED_JSON` | Body không phải JSON hợp lệ hoặc không phải object |
| 400 | `FAILED` | `MISSING_IDEMPOTENCY_KEY` | Thiếu header `X-Idempotency-Key` |
| 400 | `FAILED` | `INVALID_ENVELOPE` | Envelope thiếu trường hoặc sai kiểu |
| 400 | `FAILED` | `INVALID_PAYLOAD` | `data` sai theo schema ở §4 |
| 400 | `FAILED` | `DUPLICATE_FORM_UUID` | `formUuid` bị lặp trong cùng request |
| 401 | `FAILED` | `UNAUTHORIZED` | Thiếu/sai token |
| 422 | `FAILED` | `UNSUPPORTED_ACTION_TYPE` | actionType đã đặt tên nhưng chưa triển khai (2–6) |
| 422 | `FAILED` | `UNKNOWN_ACTION_TYPE` | actionType không hợp lệ hoặc ngoài phạm vi |
| 500 | `FAILED` | `INTERNAL_ERROR` | Lỗi phía SQLBot |
| 503 | `FAILED` | `HOOK_DISABLED` | Hook đang tắt hoặc chưa cấu hình token |

---

## 8. Ví dụ `curl`

```bash
curl -X POST "https://<host>/api/v1/hooks/ai-sync" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <AI_SYNC_HOOK_TOKEN>" \
  -H "X-Request-ID: $(uuidgen)" \
  -H "X-Idempotency-Key: $(uuidgen)" \
  -d '{
    "actionType": 1,
    "version": "1.0",
    "timestamp": "2026-08-10T10:00:00Z",
    "data": {
      "userId": "usr-12345-67890",
      "fullName": "Nguyễn Văn A",
      "isAdmin": false,
      "formQueries": [
        {
          "formUuid": "form-abcd-1234",
          "tableInfo": {
            "databaseTableName": "kdl_nhan_khau_row_values",
            "fields": [{"id": "province_id", "name": "Mã Tỉnh/Thành"}]
          },
          "postgresQuery": "SELECT * FROM kdl_nhan_khau_row_values WHERE province_id = '\''01'\''"
        }
      ]
    }
  }'
```

Response mong đợi:

```json
{
  "requestId": "...",
  "idempotencyKey": "...",
  "actionType": 1,
  "status": "SUCCESS",
  "logId": "...",
  "userId": "usr-12345-67890",
  "applied": {"upserted": 1, "deleted": 0},
  "errorCode": null,
  "errorMessage": null
}
```
