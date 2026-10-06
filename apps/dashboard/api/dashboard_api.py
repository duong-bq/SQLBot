from typing import List

from fastapi import APIRouter, File, UploadFile, HTTPException

from apps.dashboard.crud.dashboard_service import list_resource, load_resource, \
    create_resource, create_canvas, validate_name, delete_resource, update_resource, update_canvas
from apps.dashboard.models.dashboard_model import CreateDashboard, BaseDashboard, QueryDashboard
from apps.swagger.i18n import PLACEHOLDER_PREFIX
from common.audit.models.log_model import OperationType, OperationModules
from common.audit.schemas.logger_decorator import LogConfig, system_log
from common.core.deps import SessionDep, CurrentUser

router = APIRouter(tags=["Dashboard"], prefix="/dashboard")


@router.post("/list_resource", summary=f"{PLACEHOLDER_PREFIX}list_resource_api")
async def list_resource_api(session: SessionDep, dashboard: QueryDashboard, current_user: CurrentUser):
    """
    Lấy danh sách tài nguyên dashboard (thư mục/dashboard) của người dùng hiện tại.

    Body ``QueryDashboard`` chứa điều kiện lọc (ví dụ thư mục cha, tên). Chỉ trả về tài nguyên thuộc
    người dùng hiện tại. Dùng để hiển thị cây/danh sách dashboard.
    """
    return list_resource(session=session, dashboard=dashboard, current_user=current_user)


@router.post("/load_resource", summary=f"{PLACEHOLDER_PREFIX}load_resource_api")
async def load_resource_api(session: SessionDep, current_user: CurrentUser, dashboard: QueryDashboard):
    """
    Tải chi tiết một dashboard (nội dung canvas) theo ``QueryDashboard`` (thường có id).

    Kiểm tra quyền sở hữu: nếu tài nguyên không thuộc người dùng hiện tại thì trả 403. Trả về toàn bộ
    dữ liệu canvas để render dashboard.
    """
    resource_dict = load_resource(session=session, dashboard=dashboard)
    if resource_dict and resource_dict.get("create_by") != str(current_user.id):
        raise HTTPException(
            status_code=403,
            detail="You do not have permission to access this resource"
        )

    return resource_dict


@router.post("/create_resource", response_model=BaseDashboard, summary=f"{PLACEHOLDER_PREFIX}create_resource_api")
async def create_resource_api(session: SessionDep, user: CurrentUser, dashboard: CreateDashboard):
    """
    Tạo mới một tài nguyên dashboard (thường là thư mục hoặc bản ghi dashboard rỗng).

    Body ``CreateDashboard`` gồm tên, loại, thư mục cha... Trả về ``BaseDashboard`` vừa tạo.
    """
    return create_resource(session, user, dashboard)


@router.post("/update_resource", response_model=BaseDashboard, summary=f"{PLACEHOLDER_PREFIX}update_resource")
@system_log(LogConfig(
    operation_type=OperationType.UPDATE,
    module=OperationModules.DASHBOARD,
    resource_id_expr="dashboard.id"
))
async def update_resource_api(session: SessionDep, user: CurrentUser, dashboard: QueryDashboard):
    """
    Cập nhật thông tin một tài nguyên dashboard (ví dụ đổi tên, di chuyển thư mục).

    Body ``QueryDashboard`` chứa id và các trường cần sửa. Trả về ``BaseDashboard`` sau cập nhật. Có ghi log.
    """
    return update_resource(session=session, user=user, dashboard=dashboard)


@router.delete("/delete_resource/{resource_id}/{name}", summary=f"{PLACEHOLDER_PREFIX}delete_resource_api")
@system_log(LogConfig(
    operation_type=OperationType.DELETE,
    module=OperationModules.DASHBOARD,
    resource_id_expr="resource_id",
    remark_expr="name"
))
async def delete_resource_api(session: SessionDep, current_user: CurrentUser, resource_id: str, name: str):
    """
    Xóa một tài nguyên dashboard theo ``resource_id`` (``name`` chỉ dùng để ghi log).

    Nếu là thư mục sẽ xóa cả nội dung bên trong. Có ghi log thao tác kèm tên tài nguyên.
    """
    return delete_resource(session, current_user, resource_id)


@router.post("/create_canvas", response_model=BaseDashboard, summary=f"{PLACEHOLDER_PREFIX}create_canvas_api")
@system_log(LogConfig(
    operation_type=OperationType.CREATE,
    module=OperationModules.DASHBOARD,
    result_id_expr="id"
))
async def create_canvas_api(session: SessionDep, user: CurrentUser, dashboard: CreateDashboard):
    """
    Tạo mới một dashboard kèm nội dung canvas (biểu đồ/bố cục).

    Body ``CreateDashboard`` chứa tên và dữ liệu canvas. Thường dùng khi lưu một biểu đồ từ kết quả hỏi
    đáp thành dashboard. Trả về ``BaseDashboard`` vừa tạo. Có ghi log.
    """
    return create_canvas(session, user, dashboard)


@router.post("/update_canvas", response_model=BaseDashboard, summary=f"{PLACEHOLDER_PREFIX}update_canvas_api")
@system_log(LogConfig(
    operation_type=OperationType.UPDATE,
    module=OperationModules.DASHBOARD,
    resource_id_expr="dashboard.id"
))
async def update_canvas_api(session: SessionDep, user: CurrentUser, dashboard: CreateDashboard):
    """
    Cập nhật nội dung canvas của một dashboard đã có.

    Body ``CreateDashboard`` gồm id và dữ liệu canvas mới (biểu đồ, bố cục). Trả về ``BaseDashboard`` sau
    cập nhật. Có ghi log.
    """
    return update_canvas(session, user, dashboard)


@router.post("/check_name", summary=f"{PLACEHOLDER_PREFIX}check_name_api")
async def check_name_api(session: SessionDep, user: CurrentUser, dashboard: QueryDashboard):
    """
    Kiểm tra tên dashboard có bị trùng hay không (trong cùng thư mục của người dùng).

    Dùng để validate trước khi tạo/đổi tên. Trả về kết quả cho biết tên có hợp lệ/còn trống không.
    """
    return validate_name(session, user, dashboard)
