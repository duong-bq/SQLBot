from typing import Optional
from fastapi import APIRouter, HTTPException, Path, Query
from sqlmodel import exists, or_, select, delete as sqlmodel_delete, update as sqlmodel_update   
from apps.swagger.i18n import PLACEHOLDER_PREFIX
from apps.system.crud.user import clean_user_cache
from apps.system.crud.workspace import reset_single_user_oid, reset_user_oid
from apps.system.models.system_model import UserWsModel, WorkspaceBase, WorkspaceEditor, WorkspaceModel
from apps.system.models.user import UserModel
from apps.system.schemas.permission import SqlbotPermission, require_permissions
from apps.system.schemas.system_schema import UserWsBase, UserWsDTO, UserWsEditor, UserWsOption, WorkspaceUser
from common.audit.models.log_model import OperationType, OperationModules
from common.audit.schemas.logger_decorator import system_log, LogConfig
from common.core.deps import CurrentUser, SessionDep, Trans
from common.core.pagination import Paginator
from common.core.schemas import PaginatedResponse, PaginationParams
from common.utils.time import get_timestamp

router = APIRouter(tags=["system_ws"], prefix="/system/workspace")

@router.get("/uws/option/pager/{pageNum}/{pageSize}", response_model=PaginatedResponse[UserWsOption], summary=f"{PLACEHOLDER_PREFIX}ws_user_grid_api")
@require_permissions(permission=SqlbotPermission(role=['ws_admin']))
async def option_pager(
    session: SessionDep,
    current_user: CurrentUser,
    trans: Trans,
    pageNum: int = Path(description=f"{PLACEHOLDER_PREFIX}page_num"),
    pageSize: int = Path(description=f"{PLACEHOLDER_PREFIX}page_size"),
    oid: int = Query(description=f"{PLACEHOLDER_PREFIX}oid"),
    keyword: Optional[str] = Query(None, description=f"{PLACEHOLDER_PREFIX}keyword"),
):
    """
    Danh sách người dùng **chưa** thuộc workspace ``oid``, có phân trang (dùng khi thêm thành viên).

    Chỉ admin. Lọc theo ``keyword`` (tài khoản/tên). Loại trừ người dùng đã là thành viên của ``oid``
    và tài khoản hệ thống (id=1). Kết quả để chọn người bổ sung vào workspace.
    """
    if not current_user.isAdmin:
        raise Exception(trans('i18n_permission.no_permission', url = ", ", msg = trans('i18n_permission.only_admin')))
    if not oid:
        raise Exception(trans('i18n_miss_args', key = '[oid]'))
    pagination = PaginationParams(page=pageNum, size=pageSize)
    paginator = Paginator(session)
    stmt = select(UserModel.id, UserModel.account, UserModel.name).where(
        ~exists().where(UserWsModel.uid == UserModel.id, UserWsModel.oid == oid),
        UserModel.id != 1
    ).order_by(UserModel.account, UserModel.create_time)
    
    if keyword:
        keyword_pattern = f"%{keyword}%"
        stmt = stmt.where(
            or_(
                UserModel.account.ilike(keyword_pattern),
                UserModel.name.ilike(keyword_pattern),
            )
        )
    return await paginator.get_paginated_response(
        stmt=stmt,
        pagination=pagination,
    )
    
@router.get("/uws/option", response_model=UserWsOption | None, include_in_schema=False)
@require_permissions(permission=SqlbotPermission(role=['ws_admin'])) 
async def option_user(
    session: SessionDep, 
    current_user: CurrentUser,
    trans: Trans,
    keyword: str = Query(description="搜索关键字")
    ):
    """
    Tìm một người dùng chưa thuộc workspace hiện tại theo ``keyword`` khớp chính xác (tài khoản hoặc tên).

    Dùng để tra nhanh một người trước khi thêm vào workspace. Ẩn khỏi tài liệu Swagger.
    """
    if not keyword:
        raise Exception(trans('i18n_miss_args', key = '[keyword]'))
    if (not current_user.isAdmin) and current_user.weight == 0:
        raise Exception(trans('i18n_permission.no_permission', url = '', msg = ''))
    oid = current_user.oid
    
    stmt = select(UserModel.id, UserModel.account, UserModel.name).where(
        ~exists().where(UserWsModel.uid == UserModel.id, UserWsModel.oid == oid),
        UserModel.id != 1
    )
    
    if keyword:
        stmt = stmt.where(
            or_(
                UserModel.account == keyword,
                UserModel.name == keyword,
            )
        )
    return session.exec(stmt).first()


@router.get("/uws/pager/{pageNum}/{pageSize}", response_model=PaginatedResponse[WorkspaceUser], include_in_schema=False)
@require_permissions(permission=SqlbotPermission(role=['ws_admin'])) 
async def pager(
    session: SessionDep,
    current_user: CurrentUser,
    trans: Trans,
    pageNum: int,
    pageSize: int,
    keyword: Optional[str] = Query(None, description="搜索关键字(可选)"),
    oid: Optional[int] = Query(None, description="空间ID(仅admin用户生效)"),
):
    """
    Danh sách thành viên của một workspace, có phân trang. Ẩn khỏi tài liệu Swagger.

    Admin có thể chỉ định ``oid`` bất kỳ; người dùng thường chỉ xem workspace hiện tại của mình.
    Lọc theo ``keyword`` (tài khoản/tên/email). Kèm ``weight`` (vai trò trong workspace).
    """
    if not current_user.isAdmin and current_user.weight == 0:
        raise Exception(trans('i18n_permission.no_permission', url = '', msg = ''))
    if current_user.isAdmin:
        workspace_id = oid if oid else current_user.oid
    else:
        workspace_id = current_user.oid
    pagination = PaginationParams(page=pageNum, size=pageSize)
    paginator = Paginator(session)
    stmt = select(UserModel.id, UserModel.account, UserModel.name, UserModel.email, UserModel.status, UserModel.create_time, UserModel.oid, UserWsModel.weight).join(
        UserWsModel, UserModel.id == UserWsModel.uid
    ).where(
        UserWsModel.oid == workspace_id,
        UserModel.id != 1
    ).order_by(UserModel.account, UserModel.create_time)
    
    if keyword:
        keyword_pattern = f"%{keyword}%"
        stmt = stmt.where(
            or_(
                UserModel.account.ilike(keyword_pattern),
                UserModel.name.ilike(keyword_pattern),
                UserModel.email.ilike(keyword_pattern)
            )
        )
    return await paginator.get_paginated_response(
        stmt=stmt,
        pagination=pagination,
    )
    

@router.post("/uws", summary=f"{PLACEHOLDER_PREFIX}ws_user_bind_api")
@require_permissions(permission=SqlbotPermission(role=['ws_admin']))
@system_log(LogConfig(operation_type=OperationType.ADD, module=OperationModules.MEMBER, resource_id_expr="creator.uid_list",
                      ))
async def create(session: SessionDep, current_user: CurrentUser, trans: Trans, creator: UserWsDTO):
    """
    Thêm (gán) một hoặc nhiều người dùng vào workspace.

    Body ``UserWsDTO`` gồm ``uid_list``, ``oid`` và ``weight`` (vai trò). Chỉ admin mới được chỉ định
    ``oid``/``weight`` tùy ý; người dùng thường mặc định gán vào workspace hiện tại với weight 0.
    Có reset workspace mặc định và xóa cache cho từng người được thêm.
    """
    if not current_user.isAdmin and current_user.weight == 0:
        raise Exception(trans('i18n_permission.no_permission', url = '', msg = ''))
    oid: int = creator.oid if (current_user.isAdmin and creator.oid) else current_user.oid
    weight = creator.weight if (current_user.isAdmin and creator.weight) else 0
    # 判断uid_list以及oid合法性
    db_model_list = [
        UserWsModel.model_validate({
            "oid": oid,
            "uid": uid,
            "weight": weight
        })
        for uid in creator.uid_list
    ]
    for uid in creator.uid_list:
        await reset_single_user_oid(session, uid, oid)
        await clean_user_cache(uid)
        
    session.add_all(db_model_list)

@router.put("/uws", summary=f"{PLACEHOLDER_PREFIX}ws_user_status_api")
@require_permissions(permission=SqlbotPermission(role=['admin']))
@system_log(LogConfig(operation_type=OperationType.UPDATE, module=OperationModules.MEMBER, resource_id_expr="editor.uid_list",
                      ))
async def uws_edit(session: SessionDep, trans: Trans, editor: UserWsEditor):
    """
    Cập nhật vai trò (``weight``) của một thành viên trong workspace (chỉ admin).

    Body ``UserWsEditor`` gồm ``uid``, ``oid`` và ``weight`` mới. Xem logic ở hàm ``edit``;
    có xóa cache người dùng để vai trò mới có hiệu lực.
    """
    await edit(session, trans, editor)
    
async def edit(session: SessionDep, trans: Trans, editor: UserWsEditor):
    if not editor.oid or not editor.uid:
        raise Exception(trans('i18n_miss_args', key = '[oid, uid]'))
    db_model = session.exec(select(UserWsModel).where(UserWsModel.uid == editor.uid, UserWsModel.oid == editor.oid)).first()
    if not db_model:
        raise HTTPException("uws not exist")
    if editor.weight == db_model.weight:
        return
    
    db_model.weight = editor.weight
    session.add(db_model)
    
    await clean_user_cache(editor.uid)

@router.delete("/uws", summary=f"{PLACEHOLDER_PREFIX}ws_user_unbind_api")
@require_permissions(permission=SqlbotPermission(role=['ws_admin']))
@system_log(LogConfig(operation_type=OperationType.DELETE, module=OperationModules.MEMBER, resource_id_expr="dto.uid_list",
                      ))
async def delete(session: SessionDep, current_user: CurrentUser, trans: Trans, dto: UserWsBase):
    """
    Gỡ (unbind) một hoặc nhiều người dùng khỏi workspace.

    Body ``UserWsBase`` gồm ``uid_list`` và ``oid``. Xóa liên kết ``UserWsModel`` tương ứng, reset
    workspace mặc định của các người bị gỡ và xóa cache của họ.
    """
    if not current_user.isAdmin and current_user.weight == 0:
        raise Exception(trans('i18n_permission.no_permission', url = '', msg = ''))
    oid: int = dto.oid if (current_user.isAdmin and dto.oid) else current_user.oid
    db_model_list: list[UserWsModel] = session.exec(select(UserWsModel).where(UserWsModel.uid.in_(dto.uid_list), UserWsModel.oid == oid)).all()
    if not db_model_list:
        raise HTTPException(f"UserWsModel not found")
    for db_model in db_model_list:
        session.delete(db_model)
    
    for uid in dto.uid_list:
        await reset_single_user_oid(session, uid, oid, False)
        await clean_user_cache(uid)
        
@router.get("", response_model=list[WorkspaceModel], summary=f"{PLACEHOLDER_PREFIX}ws_all_api")
@require_permissions(permission=SqlbotPermission(role=['admin'])) 
async def query(session: SessionDep, trans: Trans):
    """
    Lấy danh sách tất cả workspace của hệ thống (chỉ admin).

    Tên workspace bắt đầu bằng ``i18n`` sẽ được dịch qua ``trans``. Kết quả sắp xếp theo tên.
    """
    list_result = session.exec(select(WorkspaceModel)).all()
    for ws in list_result:
        if ws.name.startswith('i18n'):
            ws.name = trans(ws.name)
    list_result.sort(key=lambda x: x.name)
    return list_result

@router.post("", summary=f"{PLACEHOLDER_PREFIX}ws_create_api")
@require_permissions(permission=SqlbotPermission(role=['admin']))
@system_log(LogConfig(operation_type=OperationType.CREATE, module=OperationModules.WORKSPACE, result_id_expr="id",
                      ))
async def add(session: SessionDep, creator: WorkspaceBase):
    """
    Tạo workspace mới (chỉ admin).

    Body ``WorkspaceBase`` gồm tên workspace. Trả về bản ghi vừa tạo.
    """
    db_model = WorkspaceModel.model_validate(creator)
    db_model.create_time = get_timestamp()
    session.add(db_model)
    return db_model
    
@router.put("", summary=f"{PLACEHOLDER_PREFIX}ws_update_api")
@require_permissions(permission=SqlbotPermission(role=['admin']))
@system_log(LogConfig(operation_type=OperationType.UPDATE, module=OperationModules.WORKSPACE, resource_id_expr="editor.id",
                      ))
async def update(session: SessionDep, editor: WorkspaceEditor):
    """
    Đổi tên một workspace (chỉ admin).

    Body ``WorkspaceEditor`` gồm ``id`` và ``name`` mới.
    """
    id = editor.id
    db_model = session.get(WorkspaceModel, id)
    if not db_model:
        raise HTTPException(f"WorkspaceModel with id {id} not found")
    db_model.name = editor.name
    session.add(db_model)

@router.get("/{id}", response_model=WorkspaceModel, summary=f"{PLACEHOLDER_PREFIX}ws_query_api")
@require_permissions(permission=SqlbotPermission(role=['admin']))    
async def get_one(session: SessionDep, trans: Trans, id: int = Path(description=f"{PLACEHOLDER_PREFIX}oid")):
    """
    Lấy chi tiết một workspace theo ``id`` (chỉ admin).

    Tên bắt đầu bằng ``i18n`` sẽ được dịch qua ``trans``.
    """
    db_model = session.get(WorkspaceModel, id)
    if not db_model:
        raise HTTPException(f"WorkspaceModel with id {id} not found")
    if db_model.name.startswith('i18n'):
        db_model.name = trans(db_model.name)
    return db_model

@router.delete("/{id}", summary=f"{PLACEHOLDER_PREFIX}ws_del_api")
@require_permissions(permission=SqlbotPermission(role=['admin']))
@system_log(LogConfig(operation_type=OperationType.DELETE, module=OperationModules.WORKSPACE, resource_id_expr="id",
                      ))
async def single_delete(session: SessionDep, current_user: CurrentUser, id: int = Path(description=f"{PLACEHOLDER_PREFIX}oid")):
    """
    Xóa một workspace theo ``id`` (chỉ admin).

    Không cho xóa workspace mặc định (id=1). Nếu người gọi đang ở workspace bị xóa thì đưa về mặc định.
    Reset workspace mặc định cho mọi thành viên, xóa các liên kết ``UserWsModel`` và làm sạch cache liên quan.
    """
    if not current_user.isAdmin:
        raise HTTPException("only admin can delete workspace")
    if id == 1:
        raise HTTPException(f"Can not delete default workspace")
    db_model = session.get(WorkspaceModel, id)
    if not db_model:
        raise HTTPException(f"WorkspaceModel with id {id} not found")
    
    if current_user.oid == id:
            current_user.oid = 1  # reset to default workspace
            update_stmt = sqlmodel_update(UserModel).where(UserModel.id == current_user.id).values(oid=1)
            session.exec(update_stmt)
            await clean_user_cache(current_user.id)
    
    user_ws_list = session.exec(select(UserWsModel).where(UserWsModel.oid == id)).all()
    if user_ws_list:
        # clean user cache
        for user_ws in user_ws_list:
            await clean_user_cache(user_ws.uid)
        # reset user default oid
        await reset_user_oid(session, id)
        # delete user_ws
        session.exec(sqlmodel_delete(UserWsModel).where(UserWsModel.oid == id))
        
    session.delete(db_model)


