from collections import defaultdict
from typing import Optional
from fastapi import APIRouter, File, Path, Query, UploadFile
from sqlmodel import SQLModel, or_, select, delete as sqlmodel_delete
from apps.system.crud.user import check_account_exists, check_email_exists, check_email_format, check_pwd_format, get_db_user, single_delete, user_ws_options
from apps.system.crud.user_excel import batchUpload, downTemplate, download_error_file
from apps.system.models.system_model import UserWsModel, WorkspaceModel
from apps.system.models.user import UserModel
from apps.system.schemas.auth import CacheName, CacheNamespace
from apps.system.schemas.permission import SqlbotPermission, require_permissions
from apps.system.schemas.system_schema import PwdEditor, UserCreator, UserEditor, UserGrid, UserInfoDTO, UserLanguage, UserStatus, UserWs
from common.audit.models.log_model import OperationType, OperationModules
from common.audit.schemas.logger_decorator import LogConfig, system_log
from common.core.deps import CurrentUser, SessionDep, Trans
from common.core.pagination import Paginator
from common.core.schemas import PaginatedResponse, PaginationParams
from common.core.security import default_md5_pwd, md5pwd, verify_md5pwd
from common.core.sqlbot_cache import clear_cache
from common.core.config import settings
from apps.swagger.i18n import PLACEHOLDER_PREFIX

router = APIRouter(tags=["system_user"], prefix="/user")


@router.get("/template", include_in_schema=False)
@require_permissions(permission=SqlbotPermission(role=['admin']))
async def templateExcel(trans: Trans):
    """
    Tải file Excel mẫu để import người dùng hàng loạt (chỉ admin).

    Ẩn khỏi tài liệu Swagger (``include_in_schema=False``). File mẫu có sẵn các cột yêu cầu để điền.
    """
    return await downTemplate(trans)

@router.post("/batchImport", include_in_schema=False)
@require_permissions(permission=SqlbotPermission(role=['admin']))
async def upload_excel(session: SessionDep, trans: Trans, current_user: CurrentUser, file: UploadFile = File(...)):
    """
    Import người dùng hàng loạt từ file Excel đã upload (chỉ admin).

    Đọc và tạo người dùng theo từng dòng; dòng lỗi được ghi lại để tải về qua ``/errorRecord/{file_id}``.
    Ẩn khỏi tài liệu Swagger.
    """
    return await batchUpload(session, trans, file)


@router.get("/errorRecord/{file_id}", include_in_schema=False)
@require_permissions(permission=SqlbotPermission(role=['admin']))
async def download_error(file_id: str):
    """
    Tải file báo cáo các dòng bị lỗi của một lần import Excel (chỉ admin).

    ``file_id`` là mã sinh ra sau khi ``/batchImport`` chạy xong. Ẩn khỏi tài liệu Swagger.
    """
    return download_error_file(file_id)

@router.get("/info", summary=f"{PLACEHOLDER_PREFIX}system_user_current_user")
async def user_info(current_user: CurrentUser) -> UserInfoDTO:
    """
    Lấy thông tin người dùng đang đăng nhập (từ token).

    Trả về ``UserInfoDTO`` gồm tài khoản, quyền, workspace hiện tại... Dùng để khởi tạo phiên ở frontend.
    """
    return current_user

 
@router.get("/defaultPwd", include_in_schema=False)
@require_permissions(permission=SqlbotPermission(role=['admin']))
async def default_pwd() -> str:
    """
    Lấy mật khẩu mặc định của hệ thống (chỉ admin).

    Dùng để hiển thị cho admin khi tạo/đặt lại mật khẩu người dùng. Ẩn khỏi tài liệu Swagger.
    """
    return settings.DEFAULT_PWD

@router.get("/pager/{pageNum}/{pageSize}", response_model=PaginatedResponse[UserGrid], summary=f"{PLACEHOLDER_PREFIX}system_user_grid")
@require_permissions(permission=SqlbotPermission(role=['admin']))
async def pager(
    session: SessionDep,
    pageNum: int = Path(..., title=f"{PLACEHOLDER_PREFIX}page_num", description=f"{PLACEHOLDER_PREFIX}page_num"),
    pageSize: int = Path(..., title=f"{PLACEHOLDER_PREFIX}page_size", description=f"{PLACEHOLDER_PREFIX}page_size"),
    keyword: Optional[str] = Query(None, description=f"{PLACEHOLDER_PREFIX}keyword"),
    status: Optional[int] = Query(None, description=f"{PLACEHOLDER_PREFIX}status"),
    origins: Optional[list[int]] = Query(None, description=f"{PLACEHOLDER_PREFIX}origin"),
    oidlist: Optional[list[int]] = Query(None, description=f"{PLACEHOLDER_PREFIX}oid"),
):
    """
    Danh sách người dùng có phân trang, kèm bộ lọc (chỉ admin).

    Phân trang theo ``pageNum``/``pageSize``. Lọc theo ``keyword`` (tài khoản/tên/email), ``status``
    (trạng thái kích hoạt), ``origins`` (nguồn đăng nhập) và ``oidlist`` (workspace). Mỗi bản ghi kết quả
    được gộp thêm ``oid_list`` — danh sách workspace mà người dùng đó thuộc về.
    """
    pagination = PaginationParams(page=pageNum, size=pageSize)
    paginator = Paginator(session)
    filters = {}
    
    origin_stmt = (
        select(UserModel.id, UserModel.account)
        .join(UserWsModel, UserModel.id == UserWsModel.uid, isouter=True)
        .where(UserModel.id != 1)
        .distinct()
        .order_by(UserModel.account)
    )
    
    if oidlist:
        origin_stmt = origin_stmt.where(UserWsModel.oid.in_(oidlist))
    if origins:
        origin_stmt = origin_stmt.where(UserModel.origin.in_(origins))
    if status is not None:
        origin_stmt = origin_stmt.where(UserModel.status == status)        
    if keyword:
        keyword_pattern = f"%{keyword}%"
        origin_stmt = origin_stmt.where(
            or_(
                UserModel.account.ilike(keyword_pattern),
                UserModel.name.ilike(keyword_pattern),
                UserModel.email.ilike(keyword_pattern)
            )
        )
        
    user_page = await paginator.get_paginated_response(
        stmt=origin_stmt,
        pagination=pagination,
        **filters)
    uid_list = [item.get('id') for item in user_page.items]
    if not uid_list:
        return user_page
    stmt = (
        select(UserModel, UserWsModel.oid.label('ws_oid'))
        .join(UserWsModel, UserModel.id == UserWsModel.uid, isouter=True)
        .where(UserModel.id.in_(uid_list))
        .order_by(UserModel.account, UserModel.create_time)
    )
    user_workspaces = session.exec(stmt).all()
    merged = defaultdict(list)
    extra_attrs = {}

    for (user, ws_oid) in user_workspaces:
        item = {}
        item.update(user.model_dump())
        user_id = item['id']
        merged[user_id].append(ws_oid)
        if user_id not in extra_attrs:
            extra_attrs[user_id] = {k: v for k, v in item.items() if k != "ws_oid"}

    # 组合结果
    result = [
        {**extra_attrs[user_id], "oid_list": list(filter(None, oid_list))} 
        for user_id, oid_list in merged.items()
    ]
    user_page.items = result
    return user_page

def format_user_dict(row) -> dict:
    result_dict = {}
    for item, key in zip(row, row._fields):
        if isinstance(item, SQLModel):
            result_dict.update(item.model_dump())
        else:
            result_dict[key] = item
    
    return result_dict

@router.get("/ws", include_in_schema=False)
async def ws_options(session: SessionDep, current_user: CurrentUser, trans: Trans) -> list[UserWs]:
    """
    Lấy danh sách workspace mà người dùng hiện tại thuộc về.

    Dùng cho dropdown chuyển workspace. Ẩn khỏi tài liệu Swagger.
    """
    return await user_ws_options(session, current_user.id, trans)

@router.put("/ws/{oid}", summary=f"{PLACEHOLDER_PREFIX}switch_oid_api")
@clear_cache(namespace=CacheNamespace.AUTH_INFO, cacheName=CacheName.USER_INFO, keyExpression="current_user.id")
@system_log(LogConfig(
    operation_type=OperationType.UPDATE,
    module=OperationModules.USER,
    resource_id_expr="editor.id"
))
async def ws_change(session: SessionDep, current_user: CurrentUser, trans:Trans, oid: int = Path(description=f"{PLACEHOLDER_PREFIX}oid")):
    """
    Chuyển workspace đang hoạt động của người dùng hiện tại sang ``oid``.

    Kiểm tra người dùng có thuộc workspace ``oid`` không (nếu không, báo lỗi thiếu quyền/không tồn tại)
    rồi cập nhật ``oid`` mặc định. Có xóa cache thông tin người dùng để phiên nhận workspace mới.
    """
    ws_list: list[UserWs] = await user_ws_options(session, current_user.id)
    if not any(x.id == oid for x in ws_list):
        db_ws = session.get(WorkspaceModel, oid)
        if db_ws:
            raise Exception(trans('i18n_user.ws_miss', ws = db_ws.name))
        raise Exception(trans('i18n_not_exist', msg = f"{trans('i18n_ws.title')}[{oid}]"))
    user_model: UserModel = get_db_user(session = session, user_id = current_user.id)
    user_model.oid = oid
    session.add(user_model)

@router.get("/{id}", response_model=UserEditor, summary=f"{PLACEHOLDER_PREFIX}user_detail_api")
@require_permissions(permission=SqlbotPermission(role=['admin']))
async def query(session: SessionDep, trans: Trans, id: int = Path(description=f"{PLACEHOLDER_PREFIX}uid")) -> UserEditor:
    """
    Lấy chi tiết một người dùng theo ``id`` để hiển thị form chỉnh sửa (chỉ admin).

    Trả về ``UserEditor`` kèm ``oid_list`` là danh sách workspace mà người dùng thuộc về.
    """
    db_user: UserModel = get_db_user(session = session, user_id = id)
    u_ws_options = await user_ws_options(session, id, trans)
    result = UserEditor.model_validate(db_user.model_dump())
    if u_ws_options:
        result.oid_list = [item.id for item in u_ws_options]
    return result


@router.post("", summary=f"{PLACEHOLDER_PREFIX}user_create_api")
@require_permissions(permission=SqlbotPermission(role=['admin']))
@system_log(LogConfig(
    operation_type=OperationType.CREATE,
    module=OperationModules.USER,
    result_id_expr="id"
))
async def user_create(session: SessionDep, creator: UserCreator, trans: Trans):
    """
    Tạo người dùng mới (chỉ admin).

    Body ``UserCreator`` gồm tài khoản, email, ``oid_list`` (workspace được gán)... Kiểm tra trùng tài khoản
    và định dạng email trước khi tạo (xem hàm ``create``). Workspace mặc định lấy phần tử đầu của ``oid_list``.
    """
    return await create(session=session, creator=creator, trans=trans)
    
async def create(session: SessionDep, creator: UserCreator, trans: Trans):
    if check_account_exists(session=session, account=creator.account):
        raise Exception(trans('i18n_exist', msg = f"{trans('i18n_user.account')} [{creator.account}]"))
    """ if check_email_exists(session=session, email=creator.email):
        raise Exception(trans('i18n_exist', msg = f"{trans('i18n_user.email')} [{creator.email}]")) """
    if not check_email_format(creator.email):
        raise Exception(trans('i18n_format_invalid', key = f"{trans('i18n_user.email')} [{creator.email}]"))
    #data = creator.model_dump(exclude_unset=True)
    data = creator.model_dump()
    user_model = UserModel.model_validate(data)
    #user_model.create_time = get_timestamp()
    user_model.language = "zh-CN"
    user_model.oid = 0
    if creator.oid_list:
        # need to validate oid_list
        db_model_list = [
            UserWsModel.model_validate({
                "oid": oid,
                "uid": user_model.id,
                "weight": 0
            })
            for oid in creator.oid_list
        ]
        session.add_all(db_model_list)
        user_model.oid = creator.oid_list[0]   
    session.add(user_model)
    return user_model

    
@router.put("", summary=f"{PLACEHOLDER_PREFIX}user_update_api")
@require_permissions(permission=SqlbotPermission(role=['admin']))
@clear_cache(namespace=CacheNamespace.AUTH_INFO, cacheName=CacheName.USER_INFO, keyExpression="editor.id")
@system_log(LogConfig(
    operation_type=OperationType.UPDATE,
    module=OperationModules.USER,
    resource_id_expr="editor.id"
))
async def update(session: SessionDep, editor: UserEditor, trans: Trans):
    """
    Cập nhật thông tin người dùng (chỉ admin).

    Không cho đổi ``account``. Đồng bộ lại danh sách workspace: tính phần chênh giữa ``editor.oid_list``
    và các workspace hiện có để thêm/xóa liên kết ``UserWsModel`` tương ứng. Có xóa cache thông tin người dùng.
    """
    user_model: UserModel = get_db_user(session = session, user_id = editor.id)
    if not user_model:
        raise Exception(f"User with id [{editor.id}] not found!")
    if editor.account != user_model.account:
        raise Exception(f"account cannot be changed!")
    """ if editor.email != user_model.email and check_email_exists(session=session, email=editor.email):
        raise Exception(trans('i18n_exist', msg = f"{trans('i18n_user.email')} [{editor.email}]")) """
    if not check_email_format(editor.email):
        raise Exception(trans('i18n_format_invalid', key = f"{trans('i18n_user.email')} [{editor.email}]"))
    origin_oid: int = user_model.oid
    
    uws_list_stmt = select(UserWsModel).where(UserWsModel.uid == editor.id)
    uws_list = session.exec(uws_list_stmt).all()
    
    existing_oids = {uws.oid for uws in uws_list}
    new_oid_set = set(editor.oid_list) if editor.oid_list else set()
    oids_to_remove = existing_oids - new_oid_set
    oids_to_add = new_oid_set - existing_oids
    
    if oids_to_remove:
        del_stmt = sqlmodel_delete(UserWsModel).where(UserWsModel.uid == editor.id, UserWsModel.oid.in_(oids_to_remove))
        session.exec(del_stmt)
    
    data = editor.model_dump(exclude_unset=True)
    user_model.sqlmodel_update(data)
    
    user_model.oid = 0
    if editor.oid_list:
        user_model.oid = origin_oid if origin_oid in editor.oid_list else  editor.oid_list[0]
        if oids_to_add:
            db_uws_model_list = [
                UserWsModel.model_validate({
                    "oid": oid,
                    "uid": user_model.id,
                    "weight": 0
                })
                for oid in oids_to_add
            ]
            session.add_all(db_uws_model_list)
    session.add(user_model)

@router.delete("/{id}", summary=f"{PLACEHOLDER_PREFIX}user_del_api")
@require_permissions(permission=SqlbotPermission(role=['admin']))
@system_log(LogConfig(
    operation_type=OperationType.DELETE,
    module=OperationModules.USER,
    resource_id_expr="id"
))
async def delete(session: SessionDep, id: int = Path(description=f"{PLACEHOLDER_PREFIX}uid")):
    """
    Xóa một người dùng theo ``id`` (chỉ admin).

    Xóa cả các liên kết workspace của người dùng (xem ``single_delete``).
    """
    await single_delete(session, id)

@router.delete("", summary=f"{PLACEHOLDER_PREFIX}user_batchdel_api")
@require_permissions(permission=SqlbotPermission(role=['admin']))
@system_log(LogConfig(operation_type=OperationType.DELETE,module=OperationModules.USER,resource_id_expr="id_list"))
async def batch_del(session: SessionDep, id_list: list[int]):
    """
    Xóa nhiều người dùng cùng lúc theo danh sách ``id_list`` (chỉ admin).

    Lặp qua từng id và gọi ``single_delete``.
    """
    for id in id_list:
        await single_delete(session, id)
    
@router.put("/language", summary=f"{PLACEHOLDER_PREFIX}language_change")
@clear_cache(namespace=CacheNamespace.AUTH_INFO, cacheName=CacheName.USER_INFO, keyExpression="current_user.id")
async def langChange(session: SessionDep, current_user: CurrentUser, trans: Trans, language: UserLanguage):
    """
    Đổi ngôn ngữ giao diện của người dùng hiện tại.

    Chỉ chấp nhận các mã: ``zh-CN``, ``zh-TW``, ``en``, ``ko-KR``. Có xóa cache thông tin người dùng.
    """
    lang = language.language
    if lang not in ["zh-CN", "zh-TW", "en", "ko-KR"]:
        raise Exception(trans('i18n_user.language_not_support', key = lang))
    db_user: UserModel = get_db_user(session=session, user_id=current_user.id)
    db_user.language = lang
    session.add(db_user)

   
@router.patch("/pwd/{id}", summary=f"{PLACEHOLDER_PREFIX}reset_pwd")
@require_permissions(permission=SqlbotPermission(role=['admin'])) 
@clear_cache(namespace=CacheNamespace.AUTH_INFO, cacheName=CacheName.USER_INFO, keyExpression="id")
@system_log(LogConfig(operation_type=OperationType.RESET_PWD,module=OperationModules.USER,resource_id_expr="id"))
async def pwdReset(session: SessionDep, current_user: CurrentUser, trans: Trans, id: int = Path(description=f"{PLACEHOLDER_PREFIX}uid")):
    """
    Đặt lại mật khẩu của một người dùng về mật khẩu mặc định (chỉ admin).

    Kiểm tra người gọi là admin, rồi gán mật khẩu mặc định (đã băm) cho người dùng ``id``.
    Có xóa cache thông tin người dùng đó.
    """
    if not current_user.isAdmin:
        raise Exception(trans('i18n_permission.no_permission', url = " patch[/user/pwd/id],", msg = trans('i18n_permission.only_admin')))
    db_user: UserModel = get_db_user(session=session, user_id=id)
    db_user.password = default_md5_pwd()
    session.add(db_user)

@router.put("/pwd", summary=f"{PLACEHOLDER_PREFIX}update_pwd")
@clear_cache(namespace=CacheNamespace.AUTH_INFO, cacheName=CacheName.USER_INFO, keyExpression="current_user.id")
@system_log(LogConfig(operation_type=OperationType.UPDATE_PWD,module=OperationModules.USER,result_id_expr="id"))
async def pwdUpdate(session: SessionDep, current_user: CurrentUser, trans: Trans, editor: PwdEditor):
    """
    Người dùng hiện tại tự đổi mật khẩu.

    Body ``PwdEditor`` gồm ``pwd`` (mật khẩu cũ) và ``new_pwd``. Kiểm tra định dạng mật khẩu mới và
    xác minh mật khẩu cũ khớp trước khi cập nhật. Có xóa cache thông tin người dùng.
    """
    new_pwd = editor.new_pwd
    if not check_pwd_format(new_pwd):
        raise Exception(trans('i18n_format_invalid', key = trans('i18n_user.password')))
    db_user: UserModel = get_db_user(session=session, user_id=current_user.id)
    if not verify_md5pwd(editor.pwd, db_user.password):
        raise Exception(trans('i18n_error', key = trans('i18n_user.password')))
    db_user.password = md5pwd(new_pwd)
    session.add(db_user)
    return db_user

    
@router.patch("/status", summary=f"{PLACEHOLDER_PREFIX}update_status")
@require_permissions(permission=SqlbotPermission(role=['admin']))
@clear_cache(namespace=CacheNamespace.AUTH_INFO, cacheName=CacheName.USER_INFO, keyExpression="statusDto.id")
@system_log(LogConfig(operation_type=OperationType.UPDATE_STATUS,module=OperationModules.USER, resource_id_expr="statusDto.id"))
async def statusChange(session: SessionDep, current_user: CurrentUser, trans: Trans, statusDto: UserStatus):
    """
    Bật/tắt trạng thái kích hoạt của một người dùng (chỉ admin).

    Body ``UserStatus`` gồm ``id`` và ``status`` (0 = vô hiệu hóa, 1 = kích hoạt). Có xóa cache
    thông tin người dùng để việc khóa tài khoản có hiệu lực ngay.
    """
    if not current_user.isAdmin:
        raise Exception(trans('i18n_permission.no_permission', url = ", ", msg = trans('i18n_permission.only_admin')))
    status = statusDto.status
    if status not in [0, 1]:
        return {"message": "status not supported"}
    db_user: UserModel = get_db_user(session=session, user_id=statusDto.id)
    db_user.status = status
    session.add(db_user)
