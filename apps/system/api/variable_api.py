# Author: Junjun
# Date: 2026/1/26
from typing import List
from fastapi import APIRouter

from apps.swagger.i18n import PLACEHOLDER_PREFIX
from apps.system.crud.system_variable import save, delete, list_all, list_page
from apps.system.models.system_variable_model import SystemVariable
from common.core.config import settings
from common.core.deps import SessionDep, CurrentUser, Trans
from apps.system.schemas.permission import SqlbotPermission, require_permissions

router = APIRouter(tags=["System_variable"], prefix="/sys_variable")
path = settings.EXCEL_PATH


@router.post("/save", response_model=None, summary=f"{PLACEHOLDER_PREFIX}variable_save")
@require_permissions(permission=SqlbotPermission(role=['admin']))
async def save_variable(session: SessionDep, user: CurrentUser, trans: Trans, variable: SystemVariable):
    """
    Tạo mới hoặc cập nhật một biến hệ thống (system variable) — chỉ admin.

    Biến hệ thống dùng cho lọc quyền dữ liệu/tham số hóa. Body ``SystemVariable`` chứa tên, giá trị...
    """
    return save(session, user, trans, variable)


@router.post("/delete",response_model=None, summary=f"{PLACEHOLDER_PREFIX}variable_delete")
@require_permissions(permission=SqlbotPermission(role=['admin']))
async def delete_variable(session: SessionDep, ids: List[int]):
    """
    Xóa một hoặc nhiều biến hệ thống theo danh sách ``ids`` (chỉ admin).
    """
    return delete(session, ids)


@router.post("/listAll",response_model=None, summary=f"{PLACEHOLDER_PREFIX}variable_list")
@require_permissions(permission=SqlbotPermission(role=['ws_admin']))
async def list_all_data(session: SessionDep, trans: Trans, variable: SystemVariable = None):
    """
    Lấy toàn bộ biến hệ thống, có thể lọc theo điều kiện trong ``variable`` (quyền ws_admin).
    """
    return list_all(session, trans, variable)


@router.post("/listPage/{pageNum}/{pageSize}",response_model=None, summary=f"{PLACEHOLDER_PREFIX}variable_page")
@require_permissions(permission=SqlbotPermission(role=['admin']))
async def pager(session: SessionDep, trans: Trans, pageNum: int, pageSize: int,
                        variable: SystemVariable = None):
    """
    Danh sách biến hệ thống có phân trang (chỉ admin).

    Phân trang theo ``pageNum``/``pageSize``; ``variable`` (tùy chọn) dùng làm điều kiện lọc.
    """
    return await list_page(session, trans, pageNum, pageSize, variable)
