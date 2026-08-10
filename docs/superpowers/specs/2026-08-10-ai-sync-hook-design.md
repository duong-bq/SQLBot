# AI Sync Hook — Thiết kế

> Phase 1: chỉ cổng tiếp nhận + 2 bảng + hàm đọc quyền nội bộ. **Không** đụng pipeline Text2SQL.
> Việc bơm quyền vào M-Schema / chèn `WHERE` là spec riêng ở phase sau.

## 1. Mục tiêu

Một endpoint duy nhất nhận bản tin đồng bộ từ hệ thống SW, thiết kế generic theo `actionType` để
sau này mở rộng, phase này chỉ triển khai `actionType = 1` (AUTHORIZATION_SYNC).

```
SW ──POST /api/v1/hooks/ai-sync──▶ [authn → idempotency → audit → validate → dispatch]
                                                                              │
                                                              handler AUTHORIZATION_SYNC
                                                                              │
                                                          replace snapshot ai_user_permissions
                                                                              │
                                                     (phase sau) SQLBot đọc theo user_id
```

## 2. Quyết định đã chốt

| Vấn đề | Quyết định | Lý do |
|---|---|---|
| Phạm vi | Hook + 2 bảng + hàm CRUD đọc quyền. Không có HTTP endpoint đọc quyền | Chưa có người tiêu thụ thật; không mở bề mặt HTTP lộ dữ liệu quyền khi chưa cần |
| Ngữ nghĩa sync | **Full snapshot**: `formQueries` là toàn bộ quyền hiện tại. Form không có trong payload bị **xoá cứng**. `formQueries: []` = thu hồi hết | Schema payload không có trường báo xoá, nên replace-toàn-bộ là cách duy nhất thu hồi được quyền |
| Xử lý | **Đồng bộ trong request** | Payload một user thì nhỏ; SW biết ngay kết quả thật và retry được |
| Response | **Raw JSON + HTTP status chuẩn**, bỏ qua envelope `{code,data,msg}` | Cổng máy-máy với hệ ngoài; SW retry theo status code |
| `sync_version` | epoch millis của `timestamp` trong envelope; bản tin có version **≤** version đã áp dụng thì bỏ qua (`STALE`) | Chống ca SW retry bản cũ sau khi bản mới đã vào. Đánh đổi: phụ thuộc đồng hồ SW |
| Idempotency | `X-Idempotency-Key` **bắt buộc**; trùng key → 200 + kết quả lần đầu, không xử lý lại | SW retry an toàn, không bao giờ xử lý hai lần |
| Code placement | Domain mới `apps/hooks/`, dispatch bằng dict `{actionType: handler}` | Ngang hàng các domain hiện có; thêm actionType không phải sửa route. Không dựng base-class registry khi mới có 1 handler (YAGNI) |
| Khoá chính 2 bảng | `UUID` + `gen_random_uuid()` | Theo DDL do bên tích hợp đưa. Postgres 16/17 của SQLBot có sẵn hàm này, không cần extension. Lệch quy ước snowflake của repo nhưng 2 bảng này không join vào cụm id snowflake |
| `user_id` | Lưu nguyên string SW gửi, **không** FK sang `sys_user` | Quy đổi sang user nội bộ (có thể qua `sys_user_platform`) là việc của phase tích hợp pipeline |

## 3. Lệch khỏi DDL gốc — cần biết

1. **Thêm cột `sync_version BIGINT` vào `ai_sync_hook_logs`.** Bắt buộc để chống bản tin lùi: sau
   một lần thu hồi hết quyền (`formQueries: []`), bảng `ai_user_permissions` không còn dòng nào của
   user nên không suy ra được version đã áp dụng. Mốc đó phải đọc từ log.
2. **Thêm status `STALE`** vào tập trạng thái (`RECEIVED`, `PROCESSING`, `SUCCESS`, `FAILED`,
   `DUPLICATE`, `STALE`). `PROCESSING` được khai để giữ nguyên tập trạng thái của DDL gốc nhưng
   **luồng đồng bộ không bao giờ set nó**: một request đi thẳng từ `RECEIVED` sang trạng thái cuối.
   Nó chỉ có ý nghĩa nếu sau này chuyển sang xử lý nền.
3. **Thêm `actionType = 0` (`UNKNOWN`)** làm giá trị sentinel để ghi audit được cả những request có
   `actionType` thiếu hoặc không phải integer (cột `action_type` là `NOT NULL`).

## 4. Hợp đồng API

`POST /api/v1/hooks/ai-sync`

Headers: `Content-Type: application/json`, `Authorization: Bearer <AI_SYNC_HOOK_TOKEN>`,
`X-Request-ID: <UUID>` (tuỳ chọn), `X-Idempotency-Key: <UUID>` (bắt buộc).

Envelope: `actionType` (int, bắt buộc), `version` (string, bắt buộc), `timestamp` (ISO 8601, bắt
buộc), `data` (object, bắt buộc).

Payload `actionType=1`: `userId` (bắt buộc), `isAdmin` (bắt buộc), `formQueries` (bắt buộc, có thể
rỗng), `fullName` (tuỳ chọn). Mỗi form: `formUuid` + `tableInfo` bắt buộc, `postgresQuery` /
`clickHouseQuery` tuỳ chọn. `tableInfo`: `databaseTableName` + `fields` bắt buộc (`fields` có thể
là mảng rỗng), `tableDisplayName` / `tableDescription` tuỳ chọn. Mỗi field: `id` bắt buộc, `name` /
`description` tuỳ chọn.

Response body luôn có: `requestId`, `idempotencyKey`, `actionType`, `status`, `logId`, `userId`,
`applied` (`{upserted, deleted}`), `errorCode`, `errorMessage`.

### Bảng lỗi

| HTTP | status | errorCode | Có ghi audit log? |
|---|---|---|---|
| 200 | SUCCESS | — | có |
| 200 | DUPLICATE | — | không ghi mới, trả lại log cũ |
| 200 | STALE | — | có |
| 400 | FAILED | `MALFORMED_JSON` | không (không parse được thì không có gì để ghi) |
| 400 | FAILED | `MISSING_IDEMPOTENCY_KEY` | không (không có khoá để ghi) |
| 400 | FAILED | `INVALID_ENVELOPE` | có (`action_type` = 0 nếu không đọc được) |
| 400 | FAILED | `INVALID_PAYLOAD` | có |
| 400 | FAILED | `DUPLICATE_FORM_UUID` | có |
| 401 | FAILED | `UNAUTHORIZED` | **không** — request chưa xác thực không được phép ghi vào bảng audit (chống flood) |
| 422 | FAILED | `UNSUPPORTED_ACTION_TYPE` | có (actionType 2–6: đã đặt tên, chưa triển khai) |
| 422 | FAILED | `UNKNOWN_ACTION_TYPE` | có (giá trị ngoài 0–6) |
| 500 | FAILED | `INTERNAL_ERROR` | có |
| 503 | FAILED | `HOOK_DISABLED` | không |

`actionType` chưa triển khai **phải** trả lỗi, không được im lặng bỏ qua.

## 5. Ranh giới transaction

`get_session` của repo commit ở cuối request, nhưng hook cần commit thủ công theo ba pha để dòng
audit sống sót cả khi pha nghiệp vụ rollback:

1. Insert log `RECEIVED` → **commit**. Unique index trên `idempotency_key` chính là cơ chế chống
   trùng: `IntegrityError` ở đây nghĩa là có request song song cùng key → đọc lại log và trả
   `DUPLICATE`.
2. Parse + replace snapshot quyền → **commit**. Lỗi thì `rollback` (chỉ mất pha 2, pha 1 đã commit).
3. Update log sang `SUCCESS` / `FAILED` / `STALE` kèm `processed_at` → **commit**.

## 6. Bảo mật

- So sánh token bằng `secrets.compare_digest` (chống timing attack).
- `AI_SYNC_HOOK_TOKEN` rỗng → hook trả 503, **không** chấp nhận request không token. Mặc định an
  toàn cho ca deploy quên đặt biến.
- `AI_SYNC_HOOK_ENABLED` mặc định `False`.
- Path `/hooks/*` vào whitelist của `TokenMiddleware` (hook tự xác thực, không dùng JWT của SQLBot).
- `postgresQuery` / `clickHouseQuery` được lưu **nguyên văn dạng text**, phase này không parse,
  không chạy, không kiểm tra an toàn SQL. Việc kiểm tra là của phase tích hợp pipeline.

## 7. Kiểm thử

Repo không có fixture DB cho test (`tests/` chạy standalone, style `unittest` + `mock`). Nên:

- Test schema/validation và các hàm thuần: không cần DB.
- Test route: `TestClient` trên một app tối giản chỉ mount router hook, patch tầng crud.
- Test tích hợp thật (2 bảng, unique index, replace snapshot): module riêng, `skipif` khi không có
  biến môi trường `SQLBOT_TEST_DB_URL`.
