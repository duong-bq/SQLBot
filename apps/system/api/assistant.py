import json
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import List, Optional

from fastapi import APIRouter, Form, HTTPException, Path, Query, Request, Response, UploadFile
from fastapi.responses import StreamingResponse
from sqlbot_xpack.file_utils import SQLBotFileUtils
from sqlmodel import select

from apps.datasource.models.datasource import CoreDatasource
from apps.db.constant import DB
from apps.swagger.i18n import PLACEHOLDER_PREFIX
from apps.system.crud.assistant import AssistantOutDs, AssistantOutDsFactory, get_assistant_info
from apps.system.crud.assistant_manage import dynamic_upgrade_cors, save
from apps.system.models.system_model import AssistantModel
from apps.system.schemas.auth import CacheName, CacheNamespace
from apps.system.schemas.permission import SqlbotPermission, require_permissions
from apps.system.schemas.system_schema import AssistantBase, AssistantDTO, AssistantUiSchema, AssistantValidator
from common.core.config import settings
from common.core.deps import CurrentAssistant, SessionDep, Trans, CurrentUser
from common.core.security import create_access_token
from common.core.sqlbot_cache import clear_cache
from common.utils.utils import get_origin_from_referer, origin_match_domain
router = APIRouter(tags=["system_assistant"], prefix="/system/assistant")
from common.audit.models.log_model import OperationType, OperationModules
from common.audit.schemas.logger_decorator import LogConfig, system_log
from sqlbot_xpack.core import decrypt_embedded_sign

@router.get("/info/{id}", include_in_schema=False)
async def info(request: Request, response: Response, session: SessionDep, trans: Trans, id: int, virtual: Optional[int] = Query(None)):
    """
    Lấy thông tin cấu hình của một trợ lý nhúng theo ``id`` (dùng cho trang/iframe nhúng, ẩn khỏi Swagger).

    Xác thực nguồn nhúng: đọc ``origin`` từ header/referer và kiểm tra khớp với ``domain`` đã cấu hình
    của assistant; nếu không khớp sẽ báo lỗi origin không hợp lệ. Thành công thì set header CORS cho origin đó.

    Không khai báo ``-> AssistantModel``: hàm trả về ``model_dump()`` kèm thêm khóa ``token`` (access
    token của tài khoản ảo ``sqlbot-inner-assistant``), khai báo response model sẽ lọc mất khóa đó và
    phía nhúng không đăng nhập được.

    ``virtual`` là id người dùng ảo do phía nhúng truyền vào, được ghi thẳng vào payload token để phân
    biệt phiên của từng người dùng cuối. Client tự chọn giá trị này nên nó KHÔNG phải danh tính đã
    xác thực — chỉ dùng để tách lịch sử hội thoại, đừng dùng làm căn cứ phân quyền.
    """
    if not id:
        raise Exception('miss assistant id')
    db_model = await get_assistant_info(session=session, assistant_id=id)
    if not db_model:
        raise RuntimeError(f"assistant application not exist")
    db_model = AssistantModel.model_validate(db_model)

    # 校验 SQLBOT-EMBEDDED-SIGN 请求头
    sign_header = request.headers.get("SQLBOT-EMBEDDED-SIGN")
    if not sign_header:
        raise RuntimeError(trans('i18n_embedded.invalid_origin', origin=''))

    sign_data = await decrypt_embedded_sign(sign_header)

    # 校验 assistant_id 与 id 参数一致
    if str(sign_data.get("assistant_id")) != str(id):
        raise RuntimeError(trans('i18n_embedded.invalid_origin', origin=''))

    # 校验 target（来源域名）是否合法
    target = sign_data.get("target", "")
    
    request_origin = request.headers.get("origin") or get_origin_from_referer(request)
    if not request_origin:
        raise RuntimeError(trans('i18n_embedded.invalid_origin', origin=request_origin or ''))
    request_origin = request_origin.rstrip('/')
    if not target or target == "null":
        target = request_origin
    elif target != request_origin:
        raise RuntimeError(trans('i18n_embedded.invalid_origin', origin=target or ''))
    
    if not origin_match_domain(target, db_model.domain):
        raise RuntimeError(trans('i18n_embedded.invalid_origin', origin=target or ''))

    # 校验 sign_time 是否在 10 秒内
    sign_time_str = sign_data.get("sign_time", "")
    sign_time = datetime.fromisoformat(sign_time_str)
    now_utc = datetime.now(timezone.utc)
    sign_time_utc = sign_time.astimezone(timezone.utc)
    if abs((now_utc - sign_time_utc).total_seconds()) > 10:
        raise RuntimeError(trans('i18n_embedded.invalid_origin', origin=target or ''))

    # 校验是否为真实浏览器请求（非自动化工具）
    if sign_data.get("webdriver", False):
        raise RuntimeError(trans('i18n_embedded.invalid_origin', origin=target or ''))

    # 校验 User-Agent 一致性（签名中的 navigator.userAgent 与请求头一致）
    sign_user_agent = sign_data.get("user_agent", "")
    request_user_agent = request.headers.get("User-Agent", "")
    if sign_user_agent != request_user_agent:
        raise RuntimeError(trans('i18n_embedded.invalid_origin', origin=target or ''))

    # 校验 timezone 与 sign_time 偏移一致性（防时区伪造）
    tz_name = sign_data.get("timezone", "")
    tz = ZoneInfo(tz_name)
    sign_time_naive = sign_time.replace(tzinfo=None)
    if tz.utcoffset(sign_time_naive) != sign_time.utcoffset():
        raise RuntimeError(trans('i18n_embedded.invalid_origin', origin=target or ''))

    origin = target.rstrip('/')

    response.headers["Access-Control-Allow-Origin"] = origin


    assistant_oid = 1
    if (db_model.type == 0):
        configuration = db_model.configuration
        config_obj = json.loads(configuration) if configuration else {}
        assistant_oid = config_obj.get('oid', 1)

    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    assistantDict = {
        "id": virtual, "account": 'sqlbot-inner-assistant', "oid": assistant_oid, "assistant_id": id
    }
    access_token = create_access_token(
        assistantDict, expires_delta=access_token_expires
    )

    result = db_model.model_dump()
    result["token"] = access_token
    return result


@router.get("/app/{appId}", include_in_schema=False)
async def getApp(request: Request, response: Response, session: SessionDep, trans: Trans, appId: str) -> AssistantModel:
    """
    Lấy thông tin trợ lý nhúng theo ``appId`` (mã ứng dụng công khai), ẩn khỏi Swagger.

    Giống ``/info/{id}`` nhưng tra theo ``app_id`` thay vì id nội bộ; cũng kiểm tra origin nhúng khớp domain.
    """
    if not appId:
        raise Exception('miss assistant appId')
    db_model = session.exec(select(AssistantModel).where(AssistantModel.app_id == appId)).first()
    if not db_model:
        raise RuntimeError(f"assistant application not exist")
    db_model = AssistantModel.model_validate(db_model)
    origin = request.headers.get("origin") or get_origin_from_referer(request)
    if not origin:
        raise RuntimeError(trans('i18n_embedded.invalid_origin', origin=origin or ''))
    origin = origin.rstrip('/')
    if not origin_match_domain(origin, db_model.domain):
        raise RuntimeError(trans('i18n_embedded.invalid_origin', origin=origin or ''))
    
    response.headers["Access-Control-Allow-Origin"] = origin
    return db_model


# Endpoint bị vô hiệu hóa (upstream a70eb1b30/0b4878199), thay bằng GET /validate/{id} bên dưới: route
# này cấp access token chỉ dựa trên ``id`` assistant, không kiểm tra origin nhúng, nên ai biết id là lấy
# được token vào workspace tương ứng.
#
# Upstream vô hiệu hóa bằng cách bọc cả hàm trong chuỗi ba nháy. Cách đó vỡ khi thân hàm có docstring —
# ``"""`` của docstring đóng luôn chuỗi bọc ngoài và phần còn lại thành code rác. Git merge sạch nhưng
# file không parse được. Đã chuyển sang comment bằng '#'.
# @router.get("/validator", response_model=AssistantValidator, include_in_schema=False)
# async def validator(session: SessionDep, id: int, virtual: Optional[int] = Query(None)):
#     """
#     Cấp access token cho phiên trợ lý nhúng theo ``id`` (ẩn khỏi Swagger).
#
#     Sinh token cho một "người dùng ảo" (``sqlbot-inner-assistant``) gắn với workspace của assistant và
#     ``virtual`` id, để phía nhúng gọi các API chat mà không cần đăng nhập thật. Trả về ``AssistantValidator``
#     chứa token; nếu assistant không tồn tại trả về validator rỗng.
#     """
#     if not id:
#         raise Exception('miss assistant id')
#
#     db_model = await get_assistant_info(session=session, assistant_id=id)
#     if not db_model:
#         return AssistantValidator()
#     db_model = AssistantModel.model_validate(db_model)
#     assistant_oid = 1
#     if (db_model.type == 0):
#         configuration = db_model.configuration
#         config_obj = json.loads(configuration) if configuration else {}
#         assistant_oid = config_obj.get('oid', 1)
#
#     access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
#     assistantDict = {
#         "id": virtual, "account": 'sqlbot-inner-assistant', "oid": assistant_oid, "assistant_id": id
#     }
#     access_token = create_access_token(
#         assistantDict, expires_delta=access_token_expires
#     )
#     return AssistantValidator(True, True, True, access_token)


@router.get('/picture/{file_id}', summary=f"{PLACEHOLDER_PREFIX}assistant_picture_api")
async def picture(file_id: str = Path(description="file_id")):
    """
    Trả về file ảnh (logo / icon nổi) của trợ lý theo ``file_id``.

    Suy ra media type từ đuôi file (svg → image/svg+xml, còn lại → image/jpeg) và stream nội dung file.
    Trả 404 nếu file không tồn tại.
    """
    file_path = SQLBotFileUtils.get_file_path(file_id=file_id)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found")

    if file_id.lower().endswith(".svg"):
        media_type = "image/svg+xml"
    else:
        media_type = "image/jpeg"

    def iterfile():
        with open(file_path, mode="rb") as f:
            yield from f

    return StreamingResponse(iterfile(), media_type=media_type)


@router.patch('/ui', summary=f"{PLACEHOLDER_PREFIX}assistant_ui_api", description=f"{PLACEHOLDER_PREFIX}assistant_ui_api")
@require_permissions(permission=SqlbotPermission(role=['ws_admin']))
@system_log(LogConfig(operation_type=OperationType.UPDATE, module=OperationModules.APPLICATION, result_id_expr="id"))
async def ui(session: SessionDep, data: str = Form(), files: List[UploadFile] = []):
    """
    Cập nhật cấu hình giao diện (UI) của một trợ lý, kèm upload logo/icon.

    Nhận ``data`` (JSON ``AssistantUiSchema``) qua form và danh sách ``files``. File có cờ ``logo`` hoặc
    ``float_icon`` được kiểm tra định dạng (jpg/png ≤ 10MB) và lưu; file cũ tương ứng bị xóa. Các trường
    cấu hình được gộp vào ``configuration`` (JSON) của assistant. Có xóa cache thông tin nhúng.
    """
    json_data = json.loads(data)
    uiSchema = AssistantUiSchema(**json_data)
    id = uiSchema.id
    db_model = session.get(AssistantModel, id)
    if not db_model:
        raise ValueError(f"AssistantModel with id {id} not found")
    configuration = db_model.configuration
    config_obj = json.loads(configuration) if configuration else {}

    ui_schema_dict = uiSchema.model_dump(exclude_none=True, exclude_unset=True)
    if files:
        for file in files:
            origin_file_name = file.filename
            file_name, flag_name = SQLBotFileUtils.split_filename_and_flag(origin_file_name)
            file.filename = file_name
            if flag_name == 'logo' or flag_name == 'float_icon':
                try:
                    SQLBotFileUtils.check_file(file=file, file_types=[".jpg", ".jpeg", ".png"],
                                               limit_file_size=(10 * 1024 * 1024))
                except ValueError as e:
                    error_msg = str(e)
                    if '文件大小超过限制' in error_msg:
                        raise ValueError(f"文件大小超过限制（最大 10 M）")
                    else:
                        raise e
                if config_obj.get(flag_name):
                    SQLBotFileUtils.delete_file(config_obj.get(flag_name))
                file_id = await SQLBotFileUtils.upload(file)
                ui_schema_dict[flag_name] = file_id
            else:
                raise ValueError(f"Unsupported file flag: {flag_name}")

    for flag_name in ['logo', 'float_icon']:
        file_val = config_obj.get(flag_name)
        if file_val and not ui_schema_dict.get(flag_name):
            config_obj[flag_name] = None
            SQLBotFileUtils.delete_file(file_val)

    for attr, value in ui_schema_dict.items():
        if attr != 'id' and not attr.startswith("__"):
            config_obj[attr] = value

    db_model.configuration = json.dumps(config_obj, ensure_ascii=False)
    session.add(db_model)
    session.commit()
    await clear_ui_cache(db_model.id)
    return db_model


@clear_cache(namespace=CacheNamespace.EMBEDDED_INFO, cacheName=CacheName.ASSISTANT_INFO, keyExpression="id")
async def clear_ui_cache(id: int):
    pass

@router.get("/validate/{id}", include_in_schema=False)
async def validate(request: Request, response: Response, session: SessionDep, trans: Trans, id: int):
    if not id:
        raise Exception('miss assistant id')
    db_model = session.get(AssistantModel, id)
    if not db_model:
        raise RuntimeError(f"assistant application not exist")

    origin = request.headers.get("origin") or get_origin_from_referer(request)
    if not origin:
        raise RuntimeError(trans('i18n_embedded.invalid_origin', origin=origin or ''))
    origin = origin.rstrip('/')

    if not origin_match_domain(origin, db_model.domain):
        raise RuntimeError(trans('i18n_embedded.invalid_origin', origin=origin or ''))

    response.headers["Access-Control-Allow-Origin"] = origin
    return {"valid": True, "origin": origin}


@router.get("/ds", include_in_schema=False, response_model=list[dict])
async def ds(session: SessionDep, current_assistant: CurrentAssistant):
    """
    Danh sách nguồn dữ liệu khả dụng cho trợ lý nhúng hiện tại (ẩn khỏi Swagger).

    - Trợ lý nội bộ (``type == 0``): lấy datasource theo workspace của assistant; nếu không ở chế độ
      online thì chỉ trả về các datasource trong ``public_list``.
    - Trợ lý dùng nguồn ngoài (``type == 1``): lấy danh sách từ ``AssistantOutDs`` tương ứng.
    Mỗi phần tử gồm id, tên, mô tả, loại DB và số bảng.
    """
    if current_assistant.type == 0:
        online = current_assistant.online
        configuration = current_assistant.configuration
        config: dict[any] = json.loads(configuration)
        oid: int = int(config['oid'])
        stmt = select(CoreDatasource.id, CoreDatasource.name, CoreDatasource.description, CoreDatasource.type, CoreDatasource.type_name, CoreDatasource.num).where(
            CoreDatasource.oid == oid)
        if not online:
            public_list: list[int] = config.get('public_list') or None
            if public_list:
                stmt = stmt.where(CoreDatasource.id.in_(public_list))
            else:
                return []
        db_ds_list = session.exec(stmt)
        return [
            {
                "id": ds.id,
                "name": ds.name,
                "description": ds.description,
                "type": ds.type,
                "type_name": ds.type_name,
                "num": ds.num,
            }
            for ds in db_ds_list]
    if current_assistant.type == 1:
        out_ds_instance: AssistantOutDs = AssistantOutDsFactory.get_instance(current_assistant)
        return [
            {
                "id": str(ds.id),
                "name": ds.name,
                "description": ds.description or ds.comment,
                "type": ds.type,
                "type_name": get_db_type(ds.type),
                "num": len(ds.tables) if ds.tables else 0,
            }
            for ds in out_ds_instance.ds_list
            if get_db_type(ds.type)
        ]
        
    return None

def get_db_type(type):
    try:
        db = DB.get_db(type)
        return db.db_name
    except Exception:
        return None


@router.get("", response_model=list[AssistantModel], summary=f"{PLACEHOLDER_PREFIX}assistant_grid_api")
@require_permissions(permission=SqlbotPermission(role=['ws_admin']))
async def query(session: SessionDep, current_user: CurrentUser):
    """
    Danh sách trợ lý (assistant) trong workspace hiện tại (quyền ws_admin).

    Loại trừ loại nhúng trang (``type == 4``). Sắp xếp theo tên và thời gian tạo.
    """
    list_result = session.exec(select(AssistantModel).where(AssistantModel.oid == current_user.oid, AssistantModel.type != 4).order_by(AssistantModel.name,
                                                                                               AssistantModel.create_time)).all()
    for model in list_result:
        model.enable_custom_model = model.enable_custom_model or False
    return list_result


@router.get("/advanced_application", response_model=list[AssistantModel], include_in_schema=False)
@require_permissions(permission=SqlbotPermission(role=['ws_admin']))
async def query_advanced_application(session: SessionDep, current_user: CurrentUser):
    """
    Danh sách "ứng dụng nâng cao" (assistant dùng nguồn dữ liệu ngoài, ``type == 1``) trong workspace hiện tại.

    Quyền ws_admin; ẩn khỏi Swagger. Sắp xếp theo tên và thời gian tạo.
    """
    list_result = session.exec(select(AssistantModel).where(AssistantModel.type == 1, AssistantModel.oid == current_user.oid).order_by(AssistantModel.name,
                                                                                               AssistantModel.create_time)).all()
    return list_result


@router.post("", summary=f"{PLACEHOLDER_PREFIX}assistant_create_api")
@require_permissions(permission=SqlbotPermission(role=['ws_admin']))
@system_log(LogConfig(operation_type=OperationType.CREATE, module=OperationModules.APPLICATION, result_id_expr="id"))
async def add(request: Request, session: SessionDep, current_user: CurrentUser, creator: AssistantBase):
    """
    Tạo mới một trợ lý/ứng dụng nhúng (quyền ws_admin).

    Gán vào workspace hiện tại, trừ loại nhúng trang (``type == 4``) thì gán workspace mặc định (oid=1).
    Việc lưu và cập nhật CORS động do hàm ``save`` xử lý.
    """
    oid = current_user.oid if creator.type != 4 else 1
    return await save(request, session, creator, oid)


@router.put("", summary=f"{PLACEHOLDER_PREFIX}assistant_update_api")
@require_permissions(permission=SqlbotPermission(role=['ws_admin']))
@clear_cache(namespace=CacheNamespace.EMBEDDED_INFO, cacheName=CacheName.ASSISTANT_INFO, keyExpression="editor.id")
@system_log(LogConfig(operation_type=OperationType.UPDATE, module=OperationModules.APPLICATION, resource_id_expr="editor.id"))
async def update(request: Request, session: SessionDep, editor: AssistantDTO):
    """
    Cập nhật một trợ lý/ứng dụng nhúng (quyền ws_admin).

    Body ``AssistantDTO`` gồm ``id`` và các trường cần sửa. Sau khi lưu, cập nhật lại danh sách CORS động
    (vì domain nhúng có thể thay đổi). Có xóa cache thông tin nhúng.
    """
    id = editor.id
    db_model = session.get(AssistantModel, id)
    if not db_model:
        raise ValueError(f"AssistantModel with id {id} not found")
    update_data = AssistantModel.model_validate(editor)
    db_model.sqlmodel_update(update_data)
    session.add(db_model)
    session.commit()
    dynamic_upgrade_cors(request=request, session=session)


# Endpoint bị vô hiệu hóa (upstream a70eb1b30/0b4878199): route không có require_permissions nên bất kỳ
# ai đăng nhập cũng đọc được cấu hình assistant của workspace khác, gồm cả domain nhúng và app_id.
# Frontend đã bỏ lời gọi tương ứng (api/assistant.ts, api/embedded.ts). Giữ nguyên dạng comment thay vì
# xóa hẳn để còn đối chiếu khi merge upstream lần sau.
# @router.get("/{id}", response_model=AssistantModel, summary=f"{PLACEHOLDER_PREFIX}assistant_query_api", description=f"{PLACEHOLDER_PREFIX}assistant_query_api")
# async def get_one(session: SessionDep, id: int = Path(description="ID")):
#     """
#     Lấy chi tiết một trợ lý theo ``id`` (dùng cho trang quản trị).
#     """
#     db_model = await get_assistant_info(session=session, assistant_id=id)
#     if not db_model:
#         raise ValueError(f"AssistantModel with id {id} not found")
#     db_model = AssistantModel.model_validate(db_model)
#     return db_model


@router.delete("/{id}", summary=f"{PLACEHOLDER_PREFIX}assistant_del_api")
@require_permissions(permission=SqlbotPermission(role=['ws_admin']))
@clear_cache(namespace=CacheNamespace.EMBEDDED_INFO, cacheName=CacheName.ASSISTANT_INFO, keyExpression="id")
@system_log(LogConfig(operation_type=OperationType.DELETE, module=OperationModules.APPLICATION, resource_id_expr="id"))
async def delete(request: Request, session: SessionDep, id: int = Path(description="ID")):
    """
    Xóa một trợ lý/ứng dụng nhúng theo ``id`` (quyền ws_admin).

    Sau khi xóa, cập nhật lại danh sách CORS động và xóa cache thông tin nhúng.
    """
    db_model = session.get(AssistantModel, id)
    if not db_model:
        raise ValueError(f"AssistantModel with id {id} not found")
    session.delete(db_model)
    session.commit()
    dynamic_upgrade_cors(request=request, session=session)

