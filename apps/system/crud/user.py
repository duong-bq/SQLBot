from typing import Optional

from sqlmodel import Session, func, select, delete as sqlmodel_delete

from apps.system.models.system_model import UserWsModel, WorkspaceModel
from apps.system.schemas.auth import CacheName, CacheNamespace
from apps.system.schemas.system_schema import EMAIL_REGEX, PWD_REGEX, BaseUserDTO, UserInfoDTO, UserWs
from common.core.deps import SessionDep
from common.core.security import verify_md5pwd
from common.core.sqlbot_cache import cache, clear_cache
from common.utils.locale import I18n, I18nHelper
from common.utils.utils import SQLBotLogUtil
from ..models.user import UserModel, UserPlatformModel


def get_db_user(*, session: Session, user_id: int) -> UserModel:
    db_user = session.get(UserModel, user_id)
    return db_user


def get_user_by_account(*, session: Session, account: str) -> BaseUserDTO | None:
    statement = select(UserModel).where(UserModel.account == account)
    db_user = session.exec(statement).first()
    if not db_user:
        return None
    return BaseUserDTO.model_validate(db_user.model_dump())


def resolve_uid_by_account(*, session: Session, account: str,
                           trans: Optional[I18n | I18nHelper] = None) -> int:
    """
    Đổi ``account`` (tên đăng nhập, duy nhất toàn hệ thống) thành ``id`` nội bộ.

    Cầu nối giữa hai không gian định danh: hệ thống tích hợp bên ngoài đặt ``account`` bằng đúng
    định danh người dùng bên họ nên không giữ ``id`` snowflake của SQLBot, trong khi mọi handler
    quản trị lại nhận ``id``. Nhóm route ``/user/by-account/*`` dùng hàm này làm bước đầu tiên.

    Chặn luôn tài khoản quản trị hệ thống (``id == 1``). Các handler quản trị không tự chặn vì giao
    diện SQLBot không bao giờ hiển thị tài khoản này, nên nếu để lọt thì bên tích hợp có thể xoá
    hoặc khoá nhầm nó và mất luôn đường vào hệ thống.

    Ném lỗi ngay tại đây khi không tìm thấy thay vì trả ``None`` cho handler: ``None`` đi tiếp sẽ nổ
    ở một tầng khác với thông báo không liên quan gì tới nguyên nhân thật.
    """
    db_user = get_user_by_account(session=session, account=account)
    if not db_user:
        if trans:
            raise Exception(trans('i18n_not_exist', msg=f"{trans('i18n_user.account')} [{account}]"))
        raise Exception(f"Account [{account}] not found!")
    if db_user.id == 1:
        raise Exception(f"System admin account [{account}] cannot be managed through this API!")
    return db_user.id


@cache(namespace=CacheNamespace.AUTH_INFO, cacheName=CacheName.USER_INFO, keyExpression="user_id")
async def get_user_info(*, session: Session, user_id: int) -> UserInfoDTO | None:
    db_user: UserModel = get_db_user(session=session, user_id=user_id)
    if not db_user:
        return None
    userInfo = UserInfoDTO.model_validate(db_user.model_dump())
    userInfo.isAdmin = userInfo.id == 1 and userInfo.account == 'admin'
    if userInfo.isAdmin:
        return userInfo
    ws_model: UserWsModel = session.exec(
        select(UserWsModel).where(UserWsModel.uid == userInfo.id, UserWsModel.oid == userInfo.oid)).first()
    userInfo.weight = ws_model.weight if ws_model else -1
    return userInfo


def authenticate(*, session: Session, account: str, password: str) -> BaseUserDTO | None:
    db_user = get_user_by_account(session=session, account=account)
    if not db_user:
        return None
    if not verify_md5pwd(password, db_user.password):
        return None
    return db_user


def user_ws_list(session: Session, uid: int, trans: Optional[I18n | I18nHelper] = None) -> list[UserWs]:
    if uid == 1:
        stmt = select(WorkspaceModel.id, WorkspaceModel.name).order_by(WorkspaceModel.name, WorkspaceModel.create_time)
    else:
        stmt = select(WorkspaceModel.id, WorkspaceModel.name).join(
            UserWsModel, UserWsModel.oid == WorkspaceModel.id
        ).where(
            UserWsModel.uid == uid,
        ).order_by(WorkspaceModel.name, WorkspaceModel.create_time)
    result = session.exec(stmt)
    if not trans:
        return result.all()
    list_result = [
        UserWs(id=id, name=trans(name) if name.startswith('i18n') else name)
        for id, name in result.all()
    ]
    if list_result:
        list_result.sort(key=lambda x: x.name)
    return list_result

async def user_ws_options(session: Session, uid: int, trans: Optional[I18n | I18nHelper] = None) -> list[UserWs]:
    return user_ws_list(session, uid, trans)


@clear_cache(namespace=CacheNamespace.AUTH_INFO, cacheName=CacheName.USER_INFO, keyExpression="id")
async def single_delete(session: SessionDep, id: int):
    user_model: UserModel = get_db_user(session=session, user_id=id)
    del_stmt = sqlmodel_delete(UserWsModel).where(UserWsModel.uid == id)
    session.exec(del_stmt)
    if user_model and user_model.origin and user_model.origin != 0:
        platform_del_stmt = sqlmodel_delete(UserPlatformModel).where(UserPlatformModel.uid == id)
        session.exec(platform_del_stmt)
    session.delete(user_model)
    session.commit()


@clear_cache(namespace=CacheNamespace.AUTH_INFO, cacheName=CacheName.USER_INFO, keyExpression="id")
async def clean_user_cache(id: int):
    SQLBotLogUtil.info(f"User cache for [{id}] has been cleaned")


def check_account_exists(*, session: Session, account: str) -> bool:
    return session.exec(select(func.count()).select_from(UserModel).where(UserModel.account == account)).one() > 0


def check_email_exists(*, session: Session, email: str) -> bool:
    return session.exec(select(func.count()).select_from(UserModel).where(UserModel.email == email)).one() > 0


def check_email_format(email: str) -> bool:
    return bool(EMAIL_REGEX.fullmatch(email))


def check_pwd_format(pwd: str) -> bool:
    return bool(PWD_REGEX.fullmatch(pwd))
