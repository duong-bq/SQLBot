# AI Sync Hook — bỏ version/timestamp, bọc payload

## Bối cảnh

Bản tin AI Sync Hook (`POST /api/v1/hooks/ai-sync`) hiện có vỏ (`SyncEnvelope`) gồm 4 trường:
`actionType`, `version`, `timestamp`, `data` (hoặc `payload`, nhận cả hai qua `AliasChoices`).

Ở commit `92aaaff11` (2026-08-18), `version`/`timestamp` đã được hạ xuống optional ở mức model để
`actionType = 4` (DATASOURCE_SYNC) có thể gửi bản tin gọn chỉ còn `{actionType, payload}` — nhưng
route vẫn bắt buộc `version`/`timestamp` cho `actionType = 1` (AUTHORIZATION_SYNC), vì `timestamp`
đang là mốc chống bản tin lùi (STALE): một bản tin quyền cũ tới muộn hơn bản tin mới sẽ bị so sánh
`sync_version` (epoch millis quy đổi từ `timestamp`) và bỏ qua nếu không mới hơn mốc đã áp dụng gần
nhất của user đó.

Yêu cầu lần này: áp dụng đúng khuôn gọn (`{actionType, payload}`) cho **mọi** actionType, không
riêng actionType 4 — bỏ hẳn `version`, bỏ hẳn `timestamp`, và không còn nhận key `data` (tên gốc
trước khi đổi sang `payload`).

## Quyết định

Ba câu hỏi đã chốt qua trao đổi trực tiếp với user:

1. **Phạm vi:** áp dụng cho TẤT CẢ actionType, kể cả actionType 1 — không chỉ riêng actionType 4.
2. **Cơ chế STALE:** bỏ hẳn, không cần chống bản tin lùi nữa. SW không còn cam kết gì về thứ tự gửi;
   bản tin nào tới sau (bất kể `timestamp` thật của nó) đều ghi đè full snapshot của bản tin trước.
3. **Key `data`:** bỏ luôn, envelope chỉ còn nhận key `payload`. Không giữ `data` như alias phụ để
   tương thích ngược.

## Thiết kế

### 1. `SyncEnvelope` (`backend/apps/hooks/schemas/ai_sync_schema.py`)

- Xoá field `version`, xoá field `timestamp`.
- Đổi tên field `data` → `payload` (cả tên Python lẫn key JSON, không còn `AliasChoices`) — tránh
  việc `populate_by_name=True` vô tình cho phép gửi lại key `data` nếu chỉ đổi alias mà giữ tên field
  Python là `data`.
- `extra="ignore"` giữ nguyên: SW gửi thừa `version`/`timestamp` (chưa kịp bỏ ở phía họ) thì hook bỏ
  qua âm thầm, không lỗi — tránh breaking change đột ngột cho bên tích hợp.

### 2. Cơ chế STALE

Bỏ hoàn toàn khỏi 3 chỗ:

- `handlers/authorization.py`: xoá nhánh so `sync_version <= last_applied` → mọi user trong batch
  hợp lệ về cấu trúc đều được áp dụng full snapshot, không còn kết quả `"STALE"`.
- `crud/ai_sync_log.py`: xoá hàm `get_last_applied_version` (hết chỗ dùng).
- `constants.py`: xoá `SyncStatus.STALE` khỏi enum (không còn ai set trạng thái này).

### 3. Nguồn giá trị `sync_version`

Cột `sync_version` trên `ai_sync_hook_logs` và `ai_user_permissions` **giữ nguyên schema** (không
migration DB) — vẫn được ghi ở mỗi lần xử lý thành công, nhưng đổi từ "quy đổi `timestamp` SW khai
báo" sang "quy đổi thời điểm SERVER xử lý request" (`datetime.now(timezone.utc)` tại route). Ý nghĩa
đổi từ "mốc chống trùng/lùi" sang thuần thông tin: dùng để tra cứu "lần đồng bộ gần nhất" qua API
dev/test `GET /hooks/ai-sync/permissions` (`permission_query.py`), không còn ảnh hưởng gì đến việc
bản tin có được áp dụng hay không.

Hệ quả: `sync_version` truyền vào handler không còn có thể là `None` — luôn là một epoch millis hợp
lệ, nên lệnh gọi handler bỏ `or 0` fallback.

### 4. Cột `schema_version` (audit log)

Cột `schema_version` trên `ai_sync_hook_logs` (lưu giá trị `version` SW gửi, thuần audit) mất nguồn
vì `version` không còn tồn tại trong envelope. Giữ nguyên cột (đã nullable, không migration), route
luôn ghi `None`.

## Không thuộc phạm vi

- Không đổi schema/migration DB — mọi cột liên quan (`sync_version`, `schema_version`) giữ nguyên
  kiểu và tên.
- Không đổi hợp đồng response (`SyncHookResponse`) — SW vẫn nhận đúng shape response như trước.
- Không đổi cơ chế idempotency (`X-Idempotency-Key`) — không liên quan đến `version`/`timestamp`.
- Không cập nhật tài liệu (`AI_SYNC_HOOK_API_SPEC.md`, các spec cũ, `OPERATIONS.md`,
  `BACKEND_ARCHITECTURE.md`) trong phạm vi spec/plan này — theo quy ước `CLAUDE.md` của repo, việc
  đó chỉ được **đề xuất** với user sau khi code xong, không tự sửa.

## Kế hoạch triển khai

Xem `docs/superpowers/plans/2026-08-18-ai-sync-hook-drop-version-timestamp.md`.
