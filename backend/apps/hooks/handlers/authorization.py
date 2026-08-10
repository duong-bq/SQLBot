"""Handler cho `actionType = 1` (AUTHORIZATION_SYNC).

Handler chỉ lo nghiệp vụ: validate payload, so mốc version, áp snapshot. Nó KHÔNG ghi audit log và
không biết gì về HTTP — route lo hai việc đó. Nhờ vậy handler test được mà không cần dựng HTTP.
"""

from dataclasses import dataclass

from pydantic import ValidationError
from sqlmodel import Session

from apps.hooks.constants import SyncErrorCode, SyncStatus
from apps.hooks.crud.ai_sync_log import get_last_applied_version
from apps.hooks.crud.ai_user_permission import replace_user_permissions
from apps.hooks.errors import SyncHookError
from apps.hooks.schemas.ai_sync_schema import (
    AuthorizationSyncData,
    SyncEnvelope,
    find_duplicate_form_uuid,
)


@dataclass
class HandlerResult:
    """Kết quả một lần xử lý bản tin, để route dựng response và chốt audit log."""

    user_id: str | None
    status: SyncStatus
    upserted: int = 0
    deleted: int = 0


def handle_authorization_sync(
    session: Session, envelope: SyncEnvelope, sync_version: int
) -> HandlerResult:
    """Parse payload quyền rồi áp FULL SNAPSHOT cho user.

    Trả `STALE` (không phải lỗi) khi `sync_version` không mới hơn mốc đã áp dụng thành công: SW
    retry một bản tin cũ sau khi bản mới đã vào thì không được phép đè ngược.
    """
    try:
        data = AuthorizationSyncData.model_validate(envelope.data)
    except ValidationError as e:
        raise SyncHookError(400, SyncErrorCode.INVALID_PAYLOAD, str(e)) from e

    duplicated = find_duplicate_form_uuid(data.form_queries)
    if duplicated:
        raise SyncHookError(
            400,
            SyncErrorCode.DUPLICATE_FORM_UUID,
            f"formUuid bị lặp trong payload: {duplicated}",
        )

    last_applied = get_last_applied_version(session, data.user_id)
    if sync_version <= last_applied:
        return HandlerResult(user_id=data.user_id, status=SyncStatus.STALE)

    upserted, deleted = replace_user_permissions(
        session,
        user_id=data.user_id,
        full_name=data.full_name,
        is_admin=data.is_admin,
        form_queries=data.form_queries,
        sync_version=sync_version,
    )
    return HandlerResult(
        user_id=data.user_id, status=SyncStatus.SUCCESS, upserted=upserted, deleted=deleted
    )
