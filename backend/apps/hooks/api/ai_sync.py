"""Route duy nhất của AI Sync Hook: `POST /api/v1/hooks/ai-sync`.

Route chỉ lo giao thức: xác thực bằng static token, đọc body thô, ghi audit, tra handler theo
actionType, dịch lỗi thành HTTP status. Nghiệp vụ nằm trong `apps/hooks/handlers/`.

Ba điều dễ hỏng nếu sửa file này:

1. **Luôn trả `JSONResponse`.** `ResponseMiddleware` bỏ qua JSONResponse (response_middleware.py:42)
   nên body không bị bọc envelope `{code,data,msg}` — đó là hợp đồng đã thống nhất với SW.
2. **Thứ tự các bước.** Ghi audit log TRƯỚC khi báo lỗi actionType/envelope, để mọi request đã xác
   thực đều để lại vết; nhưng KHÔNG ghi log cho request 401/503/thiếu idempotency key/JSON hỏng —
   không được để caller chưa xác thực bơm dữ liệu vào bảng audit.
3. **Body được đọc thô bằng `request.json()`,** không khai Pydantic model ở signature: FastAPI sẽ
   tự trả 422 với format riêng của nó, mất quyền phân biệt 400 (envelope sai) với 422 (actionType
   chưa hỗ trợ) và mất luôn dòng audit.
"""

import secrets
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from fastapi.security.utils import get_authorization_scheme_param
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from apps.hooks.constants import (
    IMPLEMENTED_ACTION_TYPES,
    SyncActionType,
    SyncErrorCode,
    SyncStatus,
    resolve_action_type,
)
from apps.hooks.crud.ai_sync_log import (
    create_received_log,
    finish_log,
    get_log_by_idempotency_key,
)
from apps.hooks.errors import SyncHookError
from apps.hooks.handlers import ACTION_HANDLERS
from apps.hooks.models.ai_sync_model import AiSyncHookLog
from apps.hooks.schemas.ai_sync_schema import (
    SyncAppliedCounts,
    SyncEnvelope,
    SyncHookResponse,
    to_sync_version,
)
from common.core.config import settings
from common.core.deps import SessionDep
from common.utils.utils import SQLBotLogUtil

router = APIRouter(tags=["AI Sync Hook"], prefix="/hooks")


def authenticate_hook_request(request: Request) -> None:
    """Xác thực request bằng `Authorization: Bearer <AI_SYNC_HOOK_TOKEN>`.

    Hook không dùng JWT của SQLBot (header `X-SQLBOT-TOKEN`) mà dùng static token riêng, nên path
    `/hooks/*` phải nằm trong whitelist của `TokenMiddleware`.

    So sánh bằng `secrets.compare_digest` để thời gian so sánh không phụ thuộc nội dung token
    (chống timing attack). Hook tắt hoặc token cấu hình rỗng thì trả 503 chứ KHÔNG cho qua.
    """
    if not settings.AI_SYNC_HOOK_ENABLED or not settings.AI_SYNC_HOOK_TOKEN:
        raise SyncHookError(503, SyncErrorCode.HOOK_DISABLED, "AI Sync Hook đang tắt")

    scheme, token = get_authorization_scheme_param(request.headers.get("Authorization", ""))
    if scheme.lower() != "bearer" or not token:
        raise SyncHookError(401, SyncErrorCode.UNAUTHORIZED, "Thiếu Authorization: Bearer <token>")
    if not secrets.compare_digest(token, settings.AI_SYNC_HOOK_TOKEN):
        raise SyncHookError(401, SyncErrorCode.UNAUTHORIZED, "Token không hợp lệ")


def _respond(
    *,
    http_status: int,
    action_type: int,
    status: SyncStatus,
    request_id: str | None = None,
    idempotency_key: str | None = None,
    log_id: str | None = None,
    user_id: str | None = None,
    upserted: int = 0,
    deleted: int = 0,
    error_code: SyncErrorCode | None = None,
    error_message: str | None = None,
) -> JSONResponse:
    """Dựng JSONResponse thống nhất cho mọi nhánh trả về của hook."""
    body = SyncHookResponse(
        requestId=request_id,
        idempotencyKey=idempotency_key,
        actionType=action_type,
        status=status.value,
        logId=log_id,
        userId=user_id,
        applied=SyncAppliedCounts(upserted=upserted, deleted=deleted),
        errorCode=error_code.value if error_code else None,
        errorMessage=error_message,
    )
    return JSONResponse(status_code=http_status, content=body.model_dump())


def _duplicate_response(
    log: AiSyncHookLog, *, request_id: str | None, idempotency_key: str
) -> JSONResponse:
    """Trả kết quả của lần xử lý đầu tiên cho request trùng idempotency key.

    Cố ý trả 200 chứ không 409: SW retry vì timeout mạng (dù lần đầu đã thành công) không nên nhận
    lỗi. `errorCode`/`errorMessage` là của lần đầu, để SW biết lần đầu FAILED hay SUCCESS.
    """
    return _respond(
        http_status=200,
        action_type=log.action_type,
        status=SyncStatus.DUPLICATE,
        request_id=request_id,
        idempotency_key=idempotency_key,
        log_id=str(log.id),
        user_id=log.user_id,
        error_code=SyncErrorCode(log.error_code) if log.error_code else None,
        error_message=log.error_message,
    )


@router.post("/ai-sync", summary="Nhận bản tin đồng bộ từ hệ thống SW")
async def ai_sync(request: Request, session: SessionDep) -> JSONResponse:
    """Cổng tiếp nhận bản tin đồng bộ, hiện chỉ xử lý `actionType = 1` (AUTHORIZATION_SYNC).

    Trình tự: xác thực → kiểm idempotency key → parse JSON thô → ghi audit RECEIVED (commit) →
    kiểm actionType → validate envelope → gọi handler → chốt audit (commit). Ba lần commit là có
    chủ ý: dòng audit phải sống sót khi pha nghiệp vụ rollback.
    """
    request_id = request.headers.get("X-Request-ID")
    idempotency_key = request.headers.get("X-Idempotency-Key")

    try:
        authenticate_hook_request(request)
    except SyncHookError as e:
        # Không ghi audit: request chưa xác thực không được phép ghi vào bảng log.
        SQLBotLogUtil.warning(f"[ai-sync] từ chối request: {e.error_code.value} - {e.message}")
        return _respond(
            http_status=e.http_status,
            action_type=int(SyncActionType.UNKNOWN),
            status=SyncStatus.FAILED,
            request_id=request_id,
            idempotency_key=idempotency_key,
            error_code=e.error_code,
            error_message=e.message,
        )

    if not idempotency_key:
        return _respond(
            http_status=400,
            action_type=int(SyncActionType.UNKNOWN),
            status=SyncStatus.FAILED,
            request_id=request_id,
            error_code=SyncErrorCode.MISSING_IDEMPOTENCY_KEY,
            error_message="Thiếu header X-Idempotency-Key",
        )

    try:
        raw: Any = await request.json()
    except Exception:
        raw = None
    if not isinstance(raw, dict):
        return _respond(
            http_status=400,
            action_type=int(SyncActionType.UNKNOWN),
            status=SyncStatus.FAILED,
            request_id=request_id,
            idempotency_key=idempotency_key,
            error_code=SyncErrorCode.MALFORMED_JSON,
            error_message="Body phải là một JSON object",
        )

    existing = get_log_by_idempotency_key(session, idempotency_key)
    if existing:
        return _duplicate_response(existing, request_id=request_id, idempotency_key=idempotency_key)

    action_type = resolve_action_type(raw.get("actionType"))
    logged_action_type = (
        int(action_type) if action_type else int(SyncActionType.UNKNOWN)
    )
    raw_data = raw.get("data")
    best_effort_user_id = raw_data.get("userId") if isinstance(raw_data, dict) else None
    schema_version = raw.get("version") if isinstance(raw.get("version"), str) else None

    try:
        log = create_received_log(
            session,
            request_id=request_id,
            idempotency_key=idempotency_key,
            action_type=logged_action_type,
            schema_version=schema_version,
            user_id=best_effort_user_id if isinstance(best_effort_user_id, str) else None,
            request_payload=raw,
        )
    except IntegrityError:
        # Hai request song song cùng idempotency key: unique index chặn, đọc lại dòng của bên thắng.
        session.rollback()
        winner = get_log_by_idempotency_key(session, idempotency_key)
        if winner:
            return _duplicate_response(
                winner, request_id=request_id, idempotency_key=idempotency_key
            )
        raise

    raw_action_type = raw.get("actionType")
    reported_action_type = (
        raw_action_type
        if isinstance(raw_action_type, int) and not isinstance(raw_action_type, bool)
        else logged_action_type
    )

    def fail(http_status: int, error_code: SyncErrorCode, message: str) -> JSONResponse:
        """Chốt audit FAILED rồi dựng response lỗi (dùng cho mọi lỗi sau khi đã có dòng log)."""
        finish_log(
            session, log, status=SyncStatus.FAILED.value,
            error_code=error_code.value, error_message=message,
        )
        return _respond(
            http_status=http_status,
            action_type=reported_action_type,
            status=SyncStatus.FAILED,
            request_id=request_id,
            idempotency_key=idempotency_key,
            log_id=str(log.id),
            user_id=log.user_id,
            error_code=error_code,
            error_message=message,
        )

    if action_type is None:
        return fail(
            422, SyncErrorCode.UNKNOWN_ACTION_TYPE,
            f"actionType không hợp lệ: {raw.get('actionType')!r} (phải là integer trong 1..6)",
        )
    if action_type not in IMPLEMENTED_ACTION_TYPES:
        return fail(
            422, SyncErrorCode.UNSUPPORTED_ACTION_TYPE,
            f"actionType {int(action_type)} ({action_type.name}) chưa được triển khai",
        )

    try:
        envelope = SyncEnvelope.model_validate(raw)
    except ValidationError as e:
        return fail(400, SyncErrorCode.INVALID_ENVELOPE, str(e))

    sync_version = to_sync_version(envelope.timestamp)
    handler = ACTION_HANDLERS[action_type]
    try:
        result = handler(session, envelope, sync_version)
    except SyncHookError as e:
        session.rollback()
        return fail(e.http_status, e.error_code, e.message)
    except Exception as e:  # noqa: BLE001 — mọi lỗi ngoài dự kiến là bug, vẫn phải để lại vết
        session.rollback()
        SQLBotLogUtil.error(f"[ai-sync] lỗi không lường trước: {e}", exc_info=True)
        return fail(500, SyncErrorCode.INTERNAL_ERROR, str(e))

    finish_log(session, log, status=result.status.value, sync_version=sync_version)
    return _respond(
        http_status=200,
        action_type=int(action_type),
        status=result.status,
        request_id=request_id,
        idempotency_key=idempotency_key,
        log_id=str(log.id),
        user_id=result.user_id,
        upserted=result.upserted,
        deleted=result.deleted,
    )
