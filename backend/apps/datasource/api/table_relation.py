# Author: Junjun
# Date: 2025/9/24
from typing import List

from fastapi import APIRouter, Path

from apps.datasource.models.datasource import CoreDatasource
from apps.swagger.i18n import PLACEHOLDER_PREFIX
from apps.system.schemas.permission import SqlbotPermission, require_permissions
from common.core.deps import SessionDep
from common.audit.models.log_model import OperationType, OperationModules
from common.audit.schemas.logger_decorator import LogConfig, system_log
router = APIRouter(tags=["Table Relation"], prefix="/table_relation")


@router.post("/save/{ds_id}", response_model=None, summary=f"{PLACEHOLDER_PREFIX}tr_save")
@require_permissions(permission=SqlbotPermission(role=['ws_admin'], keyExpression="ds_id", type='ds'))
@system_log(LogConfig(operation_type=OperationType.UPDATE_TABLE_RELATION,module=OperationModules.DATASOURCE,resource_id_expr="ds_id"))
async def save_relation(session: SessionDep, relation: List[dict],
                        ds_id: int = Path(..., description=f"{PLACEHOLDER_PREFIX}ds_id")):
    """
    Lưu quan hệ giữa các bảng (table relation) của một nguồn dữ liệu ``ds_id`` (quyền ws_admin trên nguồn đó).

    Body ``relation`` là danh sách mô tả các liên kết (join) giữa bảng; ghi đè toàn bộ cấu hình quan hệ
    hiện có. Quan hệ này giúp LLM biết cách JOIN đúng khi sinh SQL. Có ghi log thao tác.
    """
    ds = session.get(CoreDatasource, ds_id)
    if ds:
        ds.table_relation = relation
        session.commit()
    else:
        raise Exception("no datasource")
    return True


@router.post("/get/{ds_id}", response_model=List, summary=f"{PLACEHOLDER_PREFIX}tr_get")
@require_permissions(permission=SqlbotPermission(role=['ws_admin'], keyExpression="ds_id", type='ds'))
async def get_relation(session: SessionDep, ds_id: int = Path(..., description=f"{PLACEHOLDER_PREFIX}ds_id")):
    """
    Lấy cấu hình quan hệ giữa các bảng của nguồn dữ liệu ``ds_id`` (quyền ws_admin trên nguồn đó).

    Trả về danh sách quan hệ đã lưu, hoặc mảng rỗng nếu chưa cấu hình.
    """
    ds = session.get(CoreDatasource, ds_id)
    if ds:
        return ds.table_relation if ds.table_relation else []
    return []
