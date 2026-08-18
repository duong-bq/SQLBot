"""Handler cho `actionType = 1` (AUTHORIZATION_SYNC).

Handler nhận BATCH nhiều user (`data.users`). Validate cấu trúc TOÀN BỘ batch trước khi áp dụng bất
kỳ ai (all-or-nothing với lỗi cấu trúc: 1 user sai thì cả batch FAILED). Sau khi qua được bước đó,
lặp áp dụng — mỗi user tự so `sync_version` với mốc đã áp của CHÍNH user đó, nên `STALE` tính riêng
theo từng user chứ không theo cả batch. Không ghi audit log và không biết gì về HTTP — route lo hai
việc đó.
"""

from dataclasses import dataclass, field

from pydantic import ValidationError
from sqlmodel import Session

from apps.hooks.constants import SyncErrorCode, SyncStatus
from apps.hooks.crud.ai_sync_log import get_last_applied_version
from apps.hooks.crud.ai_user_permission import replace_user_permissions
from apps.hooks.errors import SyncHookError
from apps.hooks.schemas.ai_sync_schema import (
    AuthorizationSyncBatch,
    SyncAppliedCounts,
    SyncEnvelope,
    SyncResultItem,
    UserSyncResult,
    find_duplicate_datasource_id,
    find_duplicate_form_uuid,
    find_duplicate_user_id,
)


@dataclass
class HandlerResult:
    """Kết quả một lần xử lý bản tin batch, để route dựng response và chốt audit log.

    `status` luôn là `SUCCESS` khi hàm trả về bình thường (không raise) — batch đã qua validate cấu
    trúc và được xử lý xong, bất kể có phần tử nào bên trong bị `STALE`/`FAILED`. Chi tiết từng
    phần tử (user hoặc datasource, tùy actionType) nằm trong `results`.
    """

    status: SyncStatus
    results: list[SyncResultItem] = field(default_factory=list)


def handle_authorization_sync(
    session: Session, envelope: SyncEnvelope, sync_version: int
) -> HandlerResult:
    """Parse payload batch rồi áp FULL SNAPSHOT cho từng user.

    Validate cấu trúc CẢ BATCH trước (schema, `userId` trùng, `formUuid` trùng trong từng user) —
    bất kỳ lỗi nào ở bước này thì raise ngay, KHÔNG áp dụng cho ai. Qua được bước đó mới lặp áp dụng:
    user nào `STALE` (`sync_version` không mới hơn mốc đã áp của CHÍNH user đó) bị bỏ qua, không phải
    lỗi. Toàn bộ vòng lặp áp dụng nằm trong một transaction, commit đúng 1 lần ở cuối — lỗi bất ngờ
    giữa batch phải để caller rollback sạch, không để nửa batch commit dở dang.
    """
    try:
        batch = AuthorizationSyncBatch.model_validate(envelope.data)
    except ValidationError as e:
        raise SyncHookError(400, SyncErrorCode.INVALID_PAYLOAD, str(e)) from e

    if not batch.users:
        raise SyncHookError(400, SyncErrorCode.EMPTY_USER_LIST, "data.users không được rỗng")

    duplicated_user = find_duplicate_user_id(batch.users)
    if duplicated_user:
        raise SyncHookError(
            400,
            SyncErrorCode.DUPLICATE_USER_ID,
            f"userId bị lặp trong payload: {duplicated_user}",
        )

    for user_data in batch.users:
        duplicated_form = find_duplicate_form_uuid(user_data.form_queries)
        if duplicated_form:
            raise SyncHookError(
                400,
                SyncErrorCode.DUPLICATE_FORM_UUID,
                f"formUuid bị lặp trong payload của user {user_data.user_id}: {duplicated_form}",
            )
        for form in user_data.form_queries:
            duplicated_datasource = find_duplicate_datasource_id(form.table_info.queries)
            if duplicated_datasource:
                raise SyncHookError(
                    400,
                    SyncErrorCode.DUPLICATE_DATASOURCE_ID,
                    f"datasourceId bị lặp trong payload của user {user_data.user_id}, "
                    f"form {form.form_uuid}: {duplicated_datasource}",
                )

    results: list[UserSyncResult] = []
    for user_data in batch.users:
        last_applied = get_last_applied_version(session, user_data.user_id)
        if sync_version <= last_applied:
            results.append(UserSyncResult(userId=user_data.user_id, status=SyncStatus.STALE.value))
            continue
        upserted, deleted = replace_user_permissions(
            session,
            user_id=user_data.user_id,
            full_name=user_data.full_name,
            is_admin=user_data.is_admin,
            form_queries=user_data.form_queries,
            sync_version=sync_version,
        )
        results.append(
            UserSyncResult(
                userId=user_data.user_id,
                status=SyncStatus.SUCCESS.value,
                applied=SyncAppliedCounts(upserted=upserted, deleted=deleted),
            )
        )

    session.commit()
    return HandlerResult(status=SyncStatus.SUCCESS, results=results)
