"""Truy cập bảng audit `ai_sync_hook_logs`.

Các hàm ở đây **tự commit** thay vì để `get_session` commit cuối request: hook chia xử lý thành ba
pha (ghi RECEIVED → xử lý nghiệp vụ → cập nhật kết quả) và dòng audit phải sống sót cả khi pha
nghiệp vụ rollback.
"""

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlmodel import Session

from apps.hooks.constants import SyncStatus
from apps.hooks.models.ai_sync_model import AiSyncHookLog


def get_log_by_idempotency_key(session: Session, idempotency_key: str) -> AiSyncHookLog | None:
    """Tìm dòng log đã ghi cho một idempotency key, None nếu chưa có."""
    statement = select(AiSyncHookLog).where(AiSyncHookLog.idempotency_key == idempotency_key)
    return session.execute(statement).scalars().first()


def create_received_log(
    session: Session,
    *,
    request_id: str | None,
    idempotency_key: str | None,
    action_type: int,
    schema_version: str | None,
    user_id: str | None,
    request_payload: dict[str, Any],
    source: str = "SW",
) -> AiSyncHookLog:
    """Ghi dòng audit trạng thái RECEIVED và commit ngay.

    Raise `IntegrityError` khi idempotency key đã tồn tại — đó là **cơ chế chống trùng chính** cho
    ca hai request song song cùng key (kiểm tra trước bằng SELECT không đủ). Caller phải rollback
    rồi đọc lại dòng cũ.
    """
    log = AiSyncHookLog(
        request_id=request_id,
        idempotency_key=idempotency_key,
        action_type=action_type,
        schema_version=schema_version,
        source=source,
        user_id=user_id,
        request_payload=request_payload,
        status=SyncStatus.RECEIVED.value,
    )
    session.add(log)
    session.commit()
    session.refresh(log)
    return log


def finish_log(
    session: Session,
    log: AiSyncHookLog,
    *,
    status: str,
    sync_version: int | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> AiSyncHookLog:
    """Chốt kết quả xử lý vào dòng audit và commit.

    `sync_version` chỉ được ghi khi biết (envelope đã parse được). Với bản SUCCESS thì trường này
    chính là mốc để `get_last_applied_version` chống bản tin lùi, nên không được bỏ.
    """
    log.status = status
    log.error_code = error_code
    log.error_message = error_message
    if sync_version is not None:
        log.sync_version = sync_version
    log.processed_at = datetime.now(timezone.utc)
    session.add(log)
    session.commit()
    session.refresh(log)
    return log


def get_last_applied_version(session: Session, user_id: str) -> int:
    """`sync_version` lớn nhất đã áp dụng THÀNH CÔNG cho user, 0 nếu chưa có.

    Cố ý đọc từ bảng log chứ không từ `ai_user_permissions`: sau một bản tin thu hồi hết quyền,
    bảng permission không còn dòng nào của user nên mốc version sẽ mất, và một bản tin cũ gửi lại
    sau đó sẽ được áp dụng ngược — đúng cái ca cần chống.
    """
    statement = select(func.max(AiSyncHookLog.sync_version)).where(
        AiSyncHookLog.user_id == user_id,
        AiSyncHookLog.status == SyncStatus.SUCCESS.value,
    )
    return session.execute(statement).scalar() or 0
