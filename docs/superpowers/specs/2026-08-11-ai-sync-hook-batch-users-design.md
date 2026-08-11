# AI Sync Hook — Đồng bộ quyền theo batch nhiều user

> Sửa đổi phase 1 (xem [2026-08-10-ai-sync-hook-design.md](2026-08-10-ai-sync-hook-design.md)). Chỉ
> đổi payload/response và cách áp dụng của `actionType = 1` (AUTHORIZATION_SYNC). Không đụng
> `actionType` khác, không đổi cơ chế xác thực, không đổi schema DB.

## 1. Vấn đề

Thiết kế cũ nhận đúng 1 user mỗi request (`data = {userId, fullName, isAdmin, formQueries}`). Khi
SW đổi quyền của 1 **role**, thay đổi đó ảnh hưởng tới cả danh sách user thuộc role đó — SW cần gửi
một request cho từng user, không đúng với cách phía SW phát sinh sự kiện (1 sự kiện đổi role = 1
danh sách user bị ảnh hưởng).

Mục tiêu: đổi payload `AUTHORIZATION_SYNC` để nhận **một mảng user** trong 1 request, giữ nguyên
mọi cơ chế khác của hook (idempotency, chống bản tin lùi, audit log, xác thực JWT + `ws_admin`).

## 2. Quyết định đã chốt

| Vấn đề | Quyết định | Lý do |
|---|---|---|
| Tương thích ngược | **Breaking change**, bỏ hẳn payload 1-user dạng object cũ | Endpoint mới release, chưa có consumer thật tích hợp |
| Vị trí mảng user | `data.users` (object bọc mảng), không để `data` là mảng thẳng | `SyncEnvelope.data` giữ nguyên kiểu `dict[str, Any]`; còn chỗ thêm field batch-level sau này |
| Lỗi cấu trúc (payload sai schema, `userId`/`formUuid` trùng) | **All-or-nothing**: cả request `FAILED`, không áp dụng cho bất kỳ user nào | Đơn giản, dễ audit, khớp tinh thần "idempotency key = 1 lần xử lý" hiện tại |
| `STALE` (bản tin cũ hơn mốc đã áp) | **Tính riêng theo từng user**, không phải theo cả request | User A hợp lệ vẫn phải được áp dụng dù user B trong cùng batch bị stale — đúng bản chất STALE là chống-lùi-theo-user |
| Audit log (`ai_sync_hook_logs`) | **1 dòng cho cả request** (không tách theo user) | Giữ nguyên cơ chế `X-Idempotency-Key` hiện tại (unique theo request); `request_payload` đã chứa đủ danh sách user nên không mất thông tin |
| Cột `user_id` trong audit log khi batch > 1 user | Để `NULL`; batch đúng 1 user thì vẫn ghi như cũ | Cột này vốn `nullable=True` sẵn, không cần migration; giữ ích lợi debug khi batch nhỏ |
| Transaction | Một transaction cho toàn bộ vòng lặp áp dụng, **commit một lần** ở cuối | Tránh nửa batch commit rồi nửa sau rollback khi lỗi bất ngờ giữa chừng |
| Response | Thêm `results: list[{userId, status, applied}]`; `status`/`applied` cấp request là tổng hợp | SW cần biết kết quả riêng từng user (SUCCESS hay STALE) |

## 3. Payload mới

```json
{
  "actionType": 1,
  "version": "1.0",
  "timestamp": "2026-08-11T10:00:00Z",
  "data": {
    "users": [
      {
        "userId": "usr-1",
        "fullName": "Nguyễn Văn A",
        "isAdmin": false,
        "formQueries": [ { "...": "giữ nguyên cấu trúc formQueries hiện tại" } ]
      },
      {
        "userId": "usr-2",
        "fullName": "Trần Thị B",
        "isAdmin": false,
        "formQueries": []
      }
    ]
  }
}
```

- `data.users`: mảng bắt buộc, **không được rỗng** (rỗng → lỗi `EMPTY_USER_LIST`).
- Mỗi phần tử giữ nguyên cấu trúc `userId`/`fullName`/`isAdmin`/`formQueries` như payload đơn-user
  cũ (xem [AI_SYNC_HOOK_API_SPEC.md §4](../../../backend/scripts/ai_sync_hook/AI_SYNC_HOOK_API_SPEC.md)
  cho chi tiết `formQueries`/`tableInfo`/`fields` — phần này không đổi).
- `userId` không được trùng giữa các phần tử trong cùng `data.users` (lỗi `DUPLICATE_USER_ID`).
- `formUuid` không được trùng **trong `formQueries` của cùng một user** — không liên quan tới user
  khác trong batch (giữ nguyên `DUPLICATE_FORM_UUID` như hiện tại, chỉ đổi phạm vi kiểm từ "cả
  request" thành "trong từng user").
- Ngữ nghĩa FULL SNAPSHOT của `formQueries` giữ nguyên: với từng user, `formQueries` là toàn bộ
  quyền hiện tại của **user đó**; form không có mặt bị xoá cứng.

## 4. Luồng xử lý

```
POST /hooks/ai-sync
  │
  ├─ idempotency key trùng? ──▶ trả DUPLICATE (kết quả lần đầu, không xử lý lại)
  │
  ├─ parse JSON, ghi audit RECEIVED (commit #1, user_id = NULL nếu batch > 1)
  │
  ├─ validate envelope + validate CẤU TRÚC toàn bộ batch:
  │     - data.users không rỗng
  │     - userId không trùng nhau trong batch
  │     - formUuid không trùng trong formQueries của mỗi user
  │     - từng user đúng schema AuthorizationSyncData
  │   → BẤT KỲ lỗi nào ở bước này: cả request FAILED, không áp dụng ai (audit FAILED, commit #2)
  │
  ├─ qua được validate cấu trúc, mở 1 transaction áp dụng CHO CẢ BATCH:
  │     với mỗi user trong data.users (theo thứ tự trong mảng):
  │       - last_applied = get_last_applied_version(user_id)
  │       - sync_version (mốc thời gian của envelope) ≤ last_applied?
  │            có  → user này STALE, không ghi gì, results += {userId, STALE, applied: {0,0}}
  │            không → replace_user_permissions(user_id, ...) (KHÔNG tự commit)
  │                     results += {userId, SUCCESS, applied: {upserted, deleted}}
  │     hết vòng lặp → session.commit() một lần (commit #2 cho batch thành công)
  │
  └─ finish_log(status=SUCCESS, sync_version) (commit #3) → trả response
```

Lỗi ngoài dự kiến (exception) giữa vòng lặp áp dụng → `session.rollback()` toàn bộ batch (không có
user nào được áp dụng dở dang), audit ghi `FAILED` / `INTERNAL_ERROR` như cơ chế hiện tại.

## 5. Response mới

```json
{
  "requestId": "req-001",
  "idempotencyKey": "550e8400-...",
  "actionType": 1,
  "status": "SUCCESS",
  "logId": "b3f1c2a0-...",
  "results": [
    {"userId": "usr-1", "status": "SUCCESS", "applied": {"upserted": 2, "deleted": 0}},
    {"userId": "usr-2", "status": "STALE",   "applied": {"upserted": 0, "deleted": 0}}
  ],
  "applied": {"upserted": 2, "deleted": 0},
  "errorCode": null,
  "errorMessage": null
}
```

- `status` cấp request: `SUCCESS` (đã xử lý xong batch, bất kể có user nào STALE bên trong),
  `FAILED` (lỗi cấu trúc hoặc lỗi hệ thống, không ai được áp dụng), `DUPLICATE` (trùng idempotency
  key, trả lại kết quả lần xử lý đầu — kể cả `results` của lần đầu). **`STALE` không còn là giá trị
  hợp lệ ở `status` cấp request** — nó chỉ xuất hiện trong `results[].status`.
- `applied` cấp request = tổng cộng dồn `upserted`/`deleted` của tất cả phần tử trong `results`.
- `results[].applied` = delta của riêng user đó trong lần gọi này (không phải tổng số quyền hiện có
  của user — muốn biết tổng phải tự query, xem `get_user_permissions`).
- Khi cả request `FAILED` (lỗi cấu trúc): `results` là mảng rỗng, `errorCode`/`errorMessage` mô tả
  lỗi (có thể nêu rõ phần tử nào trong `users` gây lỗi trong `errorMessage`).

## 6. Mã lỗi mới

| HTTP | `errorCode` | Ý nghĩa |
|---|---|---|
| 400 | `EMPTY_USER_LIST` | `data.users` rỗng hoặc thiếu |
| 400 | `DUPLICATE_USER_ID` | `userId` bị lặp giữa các phần tử trong cùng `data.users` |

Các mã lỗi khác (`MALFORMED_JSON`, `MISSING_IDEMPOTENCY_KEY`, `INVALID_ENVELOPE`,
`INVALID_PAYLOAD`, `DUPLICATE_FORM_UUID`, `UNSUPPORTED_ACTION_TYPE`, `UNKNOWN_ACTION_TYPE`,
`INTERNAL_ERROR`) giữ nguyên ý nghĩa, chỉ đổi phạm vi áp dụng theo mô tả ở §3–§4.

## 7. Thay đổi code (không đổi schema DB)

| File | Thay đổi |
|---|---|
| `apps/hooks/schemas/ai_sync_schema.py` | `AuthorizationSyncData` giữ nguyên làm schema 1 user. Thêm `AuthorizationSyncBatch { users: list[AuthorizationSyncData] }`. Thêm `find_duplicate_user_id()`. Thêm `UserSyncResult`. `SyncHookResponse` bỏ `userId` đơn, thêm `results: list[UserSyncResult]`. |
| `apps/hooks/handlers/authorization.py` | `handle_authorization_sync` nhận `AuthorizationSyncBatch`: validate `userId` trùng trước, sau đó lặp từng user (tách helper riêng cho logic 1 user: so `sync_version`, apply hoặc đánh STALE), gom `results`. Không tự commit — trả kết quả để route commit. |
| `apps/hooks/crud/ai_user_permission.py` | Bỏ `session.commit()` trong `replace_user_permissions`; caller chịu trách nhiệm commit. |
| `apps/hooks/api/ai_sync.py` | `_respond` nhận `results`, tự tính `applied` tổng. `best_effort_user_id` cho log RECEIVED: lấy từ `data.users` — đúng 1 phần tử thì ghi `user_id`, nhiều phần tử để `None`. Đảm bảo đúng 1 lần `session.commit()` cho pha nghiệp vụ của cả batch (sau vòng lặp áp dụng, trước khi `finish_log`). |

**Không cần migration**: `ai_sync_hook_logs.user_id` đã `nullable=True` sẵn; `ai_user_permissions`
không đổi cấu trúc — batch chỉ là nhiều lần ghi độc lập theo `user_id` trong cùng transaction.

## 8. Tài liệu cần rà lại sau khi implement

Theo bảng ánh xạ trong `CLAUDE.md` (đổi payload hook) — **đề xuất, chưa tự sửa**:

- `backend/scripts/ai_sync_hook/AI_SYNC_HOOK_API_SPEC.md`: sửa §1 (ví dụ response), §4 (payload
  batch), §7 (thêm `EMPTY_USER_LIST`, `DUPLICATE_USER_ID`), §8 (ví dụ curl).
- `docs/OPERATIONS.md`, `docs/BACKEND_ARCHITECTURE.md`: rà nếu có mô tả payload/luồng hook.
- `docs/superpowers/specs/2026-08-10-ai-sync-hook-design.md`: **giữ nguyên**, đây là lịch sử quyết
  định phase 1 (single-user); spec này (2026-08-11) là quyết định đè lên, không sửa ngược spec cũ.

## 9. Kiểm thử

- `tests/test_ai_sync_schema.py`: test `AuthorizationSyncBatch`, `find_duplicate_user_id`,
  `UserSyncResult`, response schema mới.
- `tests/test_ai_sync_handler.py`: `handle_authorization_sync` nhận batch; case mixed SUCCESS/STALE
  trong cùng 1 batch; case `userId` trùng bị chặn trước khi apply user nào.
- `tests/test_ai_sync_api.py`: sửa mọi test đang gọi route với payload đơn-user cũ sang
  `data.users`; thêm case 1 user lỗi cấu trúc → cả batch `FAILED`, không ai được áp dụng; case
  batch 2 user, 1 SUCCESS + 1 STALE, response `results` đúng từng phần tử.
- `tests/test_ai_sync_e2e.py`, `tests/test_ai_sync_permission_crud.py`, `tests/test_ai_sync_crud.py`:
  rà lại các chỗ gọi `replace_user_permissions` có đang kỳ vọng nó tự `commit()` không (đã bỏ ở §7),
  sửa thành commit tường minh trong test nếu cần.
- `tests/test_ai_sync_models.py`: không đổi (model DB không đổi).
