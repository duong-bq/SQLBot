"""Endpoint cấu hình model cho AI Gateway: đẩy cấu hình và xem lại để đối soát.

Từ plan-workspace-danh-sach-model.md Gateway cấp model theo kho: đổi llm/rerank cho cả kho bằng
``PUT /datasources``, đổi embedding một datasource bằng ``PUT /datasource/{ds_id}/embedding``.
``PUT /workspace/{oid}`` còn nhận cho Gateway cũ, gỡ ở đợt dọn.
"""

from fastapi import APIRouter, Path, Request

from sqlmodel import select

from apps.ai_model_config.crud import (
    list_rows,
    lock_workspace,
    put_datasources_models,
    put_workspace_models,
    replace_datasource_embedding,
)
from apps.ai_model_config.errors import (
    CODE_DATASOURCE_NOT_FOUND,
    CODE_WORKSPACE_NOT_FOUND,
    ModelConfigError,
    invalid,
)
from apps.ai_model_config.models import (
    MODEL_EMBEDDING,
    MODEL_TYPES,
    SCOPE_DATASOURCE,
    SCOPE_WORKSPACE,
)
from apps.ai_model_config.schemas import parse_model_set, serialize_rows
from apps.datasource.models.datasource import CoreDatasource, CoreTable
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


async def _json_body(request: Request):
    """Body JSON thô; xem ``put_workspace`` vì sao không khai schema cho FastAPI."""
    try:
        return await request.json()
    except ValueError:
        raise invalid("body", "Body phải là JSON.").to_http() from None


def _ds_ids(raw) -> list[int]:
    """Danh sách ``ds_ids`` không rỗng, toàn số nguyên dương; bỏ phần tử trùng, sắp tăng."""
    if not isinstance(raw, list) or not raw:
        raise invalid("ds_ids", "ds_ids phải là mảng không rỗng.")
    if not all(isinstance(v, int) and not isinstance(v, bool) and v > 0 for v in raw):
        raise invalid("ds_ids", "Phần tử của ds_ids phải là số nguyên dương.")
    return sorted(set(raw))


def _lock_datasources(session, ds_ids: list[int]) -> dict[int, int]:
    """Khoá các dòng datasource theo thứ tự id tới hết transaction; trả ``{ds_id: oid}``.

    Khoá theo thứ tự tăng để hai lời gọi chồng nhau không deadlock. Thiếu datasource nào thì
    404, không ghi gì.
    """
    rows = session.exec(
        select(CoreDatasource.id, CoreDatasource.oid)
        .where(CoreDatasource.id.in_(ds_ids))
        .order_by(CoreDatasource.id)
        .with_for_update()
    ).all()
    oids = {ds_id: oid for ds_id, oid in rows}
    missing = [ds_id for ds_id in ds_ids if ds_id not in oids]
    if missing:
        raise ModelConfigError(
            CODE_DATASOURCE_NOT_FOUND, 404, f"Datasource không tồn tại: {missing}."
        )
    return oids


@router.put("/datasources", include_in_schema=False)
@require_permissions(permission=SqlbotPermission(role=['admin']))
async def put_datasources(session: SessionDep, request: Request):
    """Đổi llm/rerank cho nhiều datasource trong một transaction.

    Body ``{"ds_ids": [...], "models": {"llm": ..., "rerank": ...}}``. Gateway gọi với mọi
    datasource của một kho khi kho đổi model. Gửi lại cùng giá trị là no-op, nên Gateway gọi lại
    sau lỗi là hai bên khớp. Không nhận embedding (xem ``put_datasource_embedding``).
    """
    raw = await _json_body(request)
    try:
        if not isinstance(raw, dict):
            raise invalid("body", "Body phải là object.")
        ds_ids = _ds_ids(raw.get("ds_ids"))
        model_set = parse_model_set(raw.get("models"))
        if model_set.embedding is not None:
            raise invalid(
                "models.embedding", "Không đổi embedding ở đây: vector đã lưu do model cũ tính."
            )
        if model_set.is_empty():
            raise invalid("models", "Cần ít nhất một khối llm hoặc rerank.")
        oids = _lock_datasources(session, ds_ids)
        written = put_datasources_models(session, oids, model_set)
    except ModelConfigError as exc:
        raise exc.to_http() from None
    return {"ds_ids": ds_ids, "written": written}


@router.put("/datasource/{ds_id}/embedding", include_in_schema=False)
@require_permissions(permission=SqlbotPermission(role=['admin']))
async def put_datasource_embedding(session: SessionDep, request: Request, ds_id: int = Path()):
    """Thay model embedding của một datasource và tính lại vector của nó (Q2 của plan).

    Body là một khối embedding ``{base_url, api_key, model}``. Commit trước rồi mới giao việc
    tính lại cho thread nền, để thread đọc được dòng mới. Tính lại chạy nền: response về khi
    việc đã được giao, không đợi xong; vector nào embed hỏng thì giữ vector cũ.
    """
    from common.utils.embedding_threads import run_save_ds_embeddings, run_save_table_embeddings

    raw = await _json_body(request)
    try:
        model_set = parse_model_set({MODEL_EMBEDDING: raw}, root="body")
        oid = _lock_datasources(session, [ds_id])[ds_id]
        changed = replace_datasource_embedding(session, ds_id, oid, model_set)
    except ModelConfigError as exc:
        raise exc.to_http() from None
    table_ids = list(session.exec(select(CoreTable.id).where(CoreTable.ds_id == ds_id)).all())
    session.commit()
    run_save_table_embeddings(table_ids)
    run_save_ds_embeddings([ds_id])
    return {"ds_id": ds_id, "changed": changed, "tables": len(table_ids)}


@router.put("/workspace/{oid}", include_in_schema=False)
@require_permissions(permission=SqlbotPermission(role=['admin']))
async def put_workspace(session: SessionDep, request: Request, oid: int = Path()):
    """Thay toàn bộ cấu hình model của workspace; response có ``changed`` và ``synced_datasources``.

    Datasource đi theo workspace (llm/rerank) được đồng bộ trong cùng transaction.

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
        lock_workspace(session, oid)
        changed, synced = put_workspace_models(session, oid, model_set)
    except ModelConfigError as exc:
        raise exc.to_http() from None
    return {
        "models": serialize_rows(list_rows(session, SCOPE_WORKSPACE, oid)),
        "changed": changed,
        "synced_datasources": synced,
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
