"""Endpoint cấu hình model cho AI Gateway: đẩy cấu hình workspace và xem lại để đối soát."""

from fastapi import APIRouter, Path, Request

from apps.ai_model_config.crud import list_rows, put_workspace_models
from apps.ai_model_config.errors import (
    CODE_WORKSPACE_NOT_FOUND,
    ModelConfigError,
    invalid,
)
from apps.ai_model_config.models import MODEL_TYPES, SCOPE_DATASOURCE, SCOPE_WORKSPACE
from apps.ai_model_config.schemas import parse_model_set, serialize_rows
from apps.system.models.system_model import WorkspaceModel
from apps.system.schemas.permission import SqlbotPermission, require_permissions
from common.core.deps import SessionDep

router = APIRouter(tags=["ai_model_config"], prefix="/ai-model-config")


def _require_workspace(session, oid: int) -> None:
    """Báo 404 ``WORKSPACE_NOT_FOUND`` khi workspace không tồn tại."""
    if session.get(WorkspaceModel, oid) is None:
        raise ModelConfigError(
            CODE_WORKSPACE_NOT_FOUND, 404, "Workspace không tồn tại."
        ).to_http()


@router.put("/workspace/{oid}", include_in_schema=False)
@require_permissions(permission=SqlbotPermission(role=['admin']))
async def put_workspace(session: SessionDep, request: Request, oid: int = Path()):
    """Thay toàn bộ cấu hình model của workspace; response có ``changed``.

    Đọc body thô thay vì khai schema cho FastAPI: lỗi 422 mặc định dội lại ``input`` có
    ``api_key``. Không gọi mạng, nên Gateway gọi lại bao nhiêu lần cũng rẻ.
    """
    _require_workspace(session, oid)
    try:
        raw = await request.json()
    except ValueError:
        raise invalid("body", "Body phải là JSON.").to_http() from None
    try:
        model_set = parse_model_set(raw, root="body")
        changed = put_workspace_models(session, oid, model_set)
    except ModelConfigError as exc:
        raise exc.to_http() from None
    return {
        "models": serialize_rows(list_rows(session, SCOPE_WORKSPACE, oid)),
        "changed": changed,
    }


@router.get("/workspace/{oid}", include_in_schema=False)
@require_permissions(permission=SqlbotPermission(role=['admin']))
async def get_workspace(session: SessionDep, oid: int = Path()):
    """Cấu hình model của workspace, khoá đã che."""
    _require_workspace(session, oid)
    return {"models": serialize_rows(list_rows(session, SCOPE_WORKSPACE, oid))}


@router.get("/datasource/{ds_id}", include_in_schema=False)
@require_permissions(
    permission=SqlbotPermission(role=['ws_admin'], type='ds', keyExpression="ds_id")
)
async def get_datasource(session: SessionDep, ds_id: int = Path()):
    """Cấu hình đã chốt của datasource, kèm loại nào đang chạy model riêng hay model chung."""
    rows = list_rows(session, SCOPE_DATASOURCE, ds_id)
    pinned = {row.model_type for row in rows}
    return {
        "models": serialize_rows(rows),
        "effective": {
            model_type: SCOPE_DATASOURCE if model_type in pinned else "global"
            for model_type in MODEL_TYPES
        },
    }
