from fastapi import APIRouter

from apps.datasource.crud.datasource import update_ds_recommended_config
from apps.datasource.crud.recommended_problem import get_datasource_recommended, \
    save_recommended_problem, get_datasource_recommended_base
from apps.datasource.models.datasource import RecommendedProblemBase
from apps.swagger.i18n import PLACEHOLDER_PREFIX
from common.audit.models.log_model import OperationType, OperationModules
from common.audit.schemas.logger_decorator import LogConfig, system_log
from common.core.deps import SessionDep, CurrentUser

router = APIRouter(tags=["recommended problem"], prefix="/recommended_problem")


@router.get("/get_datasource_recommended/{ds_id}", response_model=None, summary=f"{PLACEHOLDER_PREFIX}rp_get")
async def datasource_recommended(session: SessionDep, ds_id: int):
    """
    Lấy danh sách câu hỏi gợi ý (recommended problem) đã bật cho nguồn dữ liệu ``ds_id``.

    Dùng để hiển thị các câu hỏi mẫu người dùng có thể bấm nhanh khi bắt đầu hỏi đáp trên nguồn này.
    """
    return get_datasource_recommended(session, ds_id)


@router.get("/get_datasource_recommended_base/{ds_id}", response_model=None, summary=f"{PLACEHOLDER_PREFIX}rp_base")
async def datasource_recommended(session: SessionDep, ds_id: int):
    """
    Lấy cấu hình gốc câu hỏi gợi ý của nguồn dữ liệu ``ds_id`` (gồm cả trạng thái bật/tắt, nội dung).

    Khác với endpoint /get_datasource_recommended: bản này trả về cấu hình đầy đủ dùng cho màn hình
    quản trị/chỉnh sửa, không chỉ danh sách đã lọc để hiển thị.
    """
    return get_datasource_recommended_base(session, ds_id)


@router.post("/save_recommended_problem", response_model=None, summary=f"{PLACEHOLDER_PREFIX}rp_save")
@system_log(
    LogConfig(operation_type=OperationType.UPDATE, module=OperationModules.DATASOURCE,
              resource_id_expr="data_info.datasource_id"))
async def datasource_recommended(session: SessionDep, user: CurrentUser, data_info: RecommendedProblemBase):
    """
    Lưu cấu hình câu hỏi gợi ý cho một nguồn dữ liệu.

    Body ``RecommendedProblemBase`` gồm ``datasource_id``, cấu hình bật/tắt (``recommended_config``) và
    danh sách câu hỏi gợi ý. Cập nhật cấu hình nguồn dữ liệu rồi lưu các câu hỏi. Có ghi log thao tác.
    """
    update_ds_recommended_config(session, data_info.datasource_id, data_info.recommended_config)
    return save_recommended_problem(session, user, data_info)
