"""Hằng số của AI Sync Hook.

Cố ý không import gì từ `common/` hay `apps/` khác: file này phải nạp được mà không kéo theo
settings hay engine DB, để test và các module khác dùng lại không bị tác dụng phụ lúc import.
"""

from enum import Enum, IntEnum


class SyncActionType(IntEnum):
    """Loại bản tin đồng bộ. Một giá trị đã release thì KHÔNG được đổi ý nghĩa.

    `UNKNOWN = 0` là sentinel nội bộ, SW không bao giờ gửi: cột `ai_sync_hook_logs.action_type` là
    NOT NULL nên phải có giá trị để ghi audit cho request thiếu/sai `actionType`.
    """

    UNKNOWN = 0
    AUTHORIZATION_SYNC = 1
    USER_SYNC = 2
    ORGANIZATION_SYNC = 3
    DATASOURCE_SYNC = 4
    KNOWLEDGE_SYNC = 5
    DOCUMENT_SYNC = 6


IMPLEMENTED_ACTION_TYPES = frozenset({SyncActionType.AUTHORIZATION_SYNC, SyncActionType.DATASOURCE_SYNC})


class SyncStatus(str, Enum):
    """Trạng thái xử lý một bản tin, ghi vào `ai_sync_hook_logs.status`.

    `STALE` là trạng thái riêng của fork: bản tin có sync_version không mới hơn version đã áp dụng
    thì bị bỏ qua có chủ ý — khác FAILED (lỗi) và khác DUPLICATE (trùng idempotency key).

    `PROCESSING` được giữ để khớp tập trạng thái của DDL gốc nhưng luồng xử lý ĐỒNG BỘ hiện tại
    không bao giờ set nó — một request đi thẳng từ RECEIVED sang trạng thái cuối cùng. Nó chỉ có ý
    nghĩa nếu sau này chuyển sang xử lý nền.
    """

    RECEIVED = "RECEIVED"
    PROCESSING = "PROCESSING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    DUPLICATE = "DUPLICATE"
    STALE = "STALE"


class SyncErrorCode(str, Enum):
    """Mã lỗi trả cho SW. Giá trị là hợp đồng tích hợp, đổi tên là breaking change."""

    MALFORMED_JSON = "MALFORMED_JSON"
    MISSING_IDEMPOTENCY_KEY = "MISSING_IDEMPOTENCY_KEY"
    INVALID_ENVELOPE = "INVALID_ENVELOPE"
    INVALID_PAYLOAD = "INVALID_PAYLOAD"
    DUPLICATE_FORM_UUID = "DUPLICATE_FORM_UUID"
    DUPLICATE_DATASOURCE_ID = "DUPLICATE_DATASOURCE_ID"
    EMPTY_USER_LIST = "EMPTY_USER_LIST"
    DUPLICATE_USER_ID = "DUPLICATE_USER_ID"
    EMPTY_DATASOURCE_LIST = "EMPTY_DATASOURCE_LIST"
    UNSUPPORTED_ACTION_TYPE = "UNSUPPORTED_ACTION_TYPE"
    UNKNOWN_ACTION_TYPE = "UNKNOWN_ACTION_TYPE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


def resolve_action_type(raw: object) -> SyncActionType | None:
    """Quy đổi giá trị `actionType` thô trong JSON sang enum, trả None nếu không hợp lệ.

    Hai bẫy được xử lý tường minh: `bool` là subclass của `int` trong Python (`True == 1`) nên phải
    loại trước; và `SyncActionType.UNKNOWN` (0) là sentinel nội bộ nên coi giá trị 0 do SW gửi là
    không hợp lệ. String dạng "1" cũng bị loại vì spec bắt buộc kiểu integer.
    """
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    try:
        action = SyncActionType(raw)
    except ValueError:
        return None
    return None if action is SyncActionType.UNKNOWN else action
