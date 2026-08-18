"""Route duy nhất của AI Sync Hook: `POST /api/v1/hooks/ai-sync`.

Route chỉ lo giao thức: đọc body thô, ghi audit, tra handler theo actionType, dịch lỗi thành HTTP
status. Nghiệp vụ nằm trong `apps/hooks/handlers/`.

Xác thực dùng chung cơ chế JWT của SQLBot (`X-SQLBOT-TOKEN`, cấp bởi `POST /login/access-token`)
và yêu cầu quyền `ws_admin` qua `require_permissions` — giống mọi API quản trị khác trong repo
(terminology, datasource…), không có static token riêng. Vì vậy `/hooks/*` **không** nằm trong
whitelist của `TokenMiddleware`.

Ba điều dễ hỏng nếu sửa file này:

1. **Path `/api/v1/hooks/ai-sync` phải nằm trong `direct_paths` của `ResponseMiddleware`**
   (response_middleware.py:30) thì body mới không bị bọc envelope `{code,data,msg}` — đó là hợp đồng
   đã thống nhất với SW. Việc route trả `JSONResponse` **không** đủ: nhánh
   `isinstance(response, JSONResponse)` trong middleware đó không bao giờ đúng khi có nhiều
   `BaseHTTPMiddleware` xếp chồng, vì `call_next()` dựng lại response thành `_StreamingResponse`.
   Đổi prefix/path của route mà quên sửa `direct_paths` là âm thầm phá hợp đồng, và chỉ các nhánh
   HTTP 200 mới lộ ra (nhánh lỗi thoát envelope sẵn nhờ điều kiện `status_code != 200`).
2. **Thứ tự các bước.** Ghi audit log TRƯỚC khi báo lỗi actionType/envelope, để mọi request đã xác
   thực đều để lại vết; nhưng KHÔNG ghi log cho request thiếu idempotency key/JSON hỏng — không
   được để những request chưa đọc được nội dung bơm dữ liệu rác vào bảng audit.
3. **Body được đọc thô bằng `request.json()`,** không khai Pydantic model ở signature: FastAPI sẽ
   tự trả 422 với format riêng của nó, mất quyền phân biệt 400 (envelope sai) với 422 (actionType
   chưa hỗ trợ) và mất luôn dòng audit.

Lưu ý: lỗi xác thực (401, thiếu/sai JWT) và lỗi thiếu quyền (`ws_admin`) xảy ra ở tầng
`TokenMiddleware`/`require_permissions` **trước khi vào hàm route**, nên không theo format
`SyncHookResponse` — chúng trả theo đúng quy ước lỗi chung của SQLBot (xem
`BACKEND_ARCHITECTURE.md` §7), không phải hợp đồng riêng của hook.
"""

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
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
    SyncResultItem,
    to_sync_version,
)
from apps.system.schemas.permission import SqlbotPermission, require_permissions
from common.core.deps import SessionDep
from common.utils.utils import SQLBotLogUtil

router = APIRouter(tags=["AI Sync Hook"], prefix="/hooks")


def _respond(
    *,
    http_status: int,
    action_type: int,
    status: SyncStatus,
    request_id: str | None = None,
    idempotency_key: str | None = None,
    log_id: str | None = None,
    results: list[SyncResultItem] | None = None,
    error_code: SyncErrorCode | None = None,
    error_message: str | None = None,
) -> JSONResponse:
    """Dựng JSONResponse thống nhất cho mọi nhánh trả về của hook.

    `applied` cấp request là tổng cộng dồn `upserted`/`deleted` của tất cả phần tử trong `results`
    — tiện cho SW không phải tự cộng, chi tiết từng user vẫn nằm trong `results`.
    """
    results = results or []
    body = SyncHookResponse(
        requestId=request_id,
        idempotencyKey=idempotency_key,
        actionType=action_type,
        status=status.value,
        logId=log_id,
        results=results,
        applied=SyncAppliedCounts(
            upserted=sum(r.applied.upserted for r in results),
            deleted=sum(r.applied.deleted for r in results),
        ),
        errorCode=error_code.value if error_code else None,
        errorMessage=error_message,
    )
    return JSONResponse(status_code=http_status, content=body.model_dump())


def _duplicate_response(
    log: AiSyncHookLog, *, request_id: str | None, idempotency_key: str
) -> JSONResponse:
    """Trả kết quả của lần xử lý đầu tiên cho request trùng idempotency key.

    Cố ý trả 200 chứ không 409: SW retry vì timeout mạng (dù lần đầu đã thành công) không nên nhận
    lỗi. `errorCode`/`errorMessage` là của lần đầu, để SW biết lần đầu FAILED hay SUCCESS. `results`
    chi tiết từng user của lần đầu KHÔNG được lưu lại (audit log không có cột đó) nên trả rỗng.
    """
    return _respond(
        http_status=200,
        action_type=log.action_type,
        status=SyncStatus.DUPLICATE,
        request_id=request_id,
        idempotency_key=idempotency_key,
        log_id=str(log.id),
        error_code=SyncErrorCode(log.error_code) if log.error_code else None,
        error_message=log.error_message,
    )


def _best_effort_single_user_id(raw_data: Any) -> str | None:
    """Đoán `user_id` để ghi vào audit log RECEIVED trước khi parse đầy đủ.

    Chỉ ghi được khi batch đúng 1 user — nhiều user thì cột `user_id` (đơn) không còn đủ diễn tả,
    để `None` (xem spec `2026-08-11-ai-sync-hook-batch-users-design.md` §3).
    """
    if not isinstance(raw_data, dict):
        return None
    users = raw_data.get("users")
    if not isinstance(users, list) or len(users) != 1:
        return None
    first = users[0]
    if not isinstance(first, dict):
        return None
    user_id = first.get("userId")
    return user_id if isinstance(user_id, str) else None


@router.post("/ai-sync", summary="Nhận bản tin đồng bộ từ hệ thống SW")
@require_permissions(permission=SqlbotPermission(role=['ws_admin']))
async def ai_sync(request: Request, session: SessionDep) -> JSONResponse:
    """Cổng tiếp nhận bản tin đồng bộ: `actionType = 1` (AUTHORIZATION_SYNC) và `4` (DATASOURCE_SYNC).

    Xác thực (JWT `X-SQLBOT-TOKEN`) và phân quyền (`ws_admin`) đã được `TokenMiddleware` +
    `require_permissions` xử lý trước khi vào đây — hàm này chỉ chạy khi request đã hợp lệ.

    Trình tự: kiểm idempotency key → parse JSON thô → ghi audit RECEIVED (commit) → kiểm
    actionType → validate envelope → gọi handler → chốt audit (commit). Ba lần commit là có chủ
    ý: dòng audit phải sống sót khi pha nghiệp vụ rollback.
    """
    request_id = request.headers.get("X-Request-ID")
    idempotency_key = request.headers.get("X-Idempotency-Key")

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
    best_effort_user_id = _best_effort_single_user_id(raw_data)
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
        results=result.results,
    )
