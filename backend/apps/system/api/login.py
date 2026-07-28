from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import OAuth2PasswordRequestForm
from apps.system.schemas.logout_schema import LogoutSchema
from apps.system.schemas.system_schema import BaseUserDTO
from common.core.deps import SessionDep, Trans
from ..crud.user import authenticate
from common.core.security import create_access_token
from datetime import timedelta
from common.core.config import settings
from common.core.schemas import Token
from sqlbot_xpack.authentication.manage import logout as xpack_logout

from common.audit.models.log_model import OperationType, OperationModules
from common.audit.schemas.logger_decorator import system_log, LogConfig

router = APIRouter(tags=["login"], prefix="/login")

@router.post("/access-token")
@system_log(LogConfig(
    operation_type=OperationType.LOGIN,
    module=OperationModules.USER,
    result_id_expr="id"
))
async def local_login(
    session: SessionDep,
    trans: Trans,
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()]
) -> Token:
    """
    Đăng nhập nội bộ, cấp access token (JWT).

    Nhận ``username``/``password`` dạng plaintext theo chuẩn OAuth2 password flow (không còn
    qua bước mã hóa/giải mã phía xpack — deploy nội bộ công ty đã có reverse proxy terminate
    HTTPS trước khi traffic chạm tới đây). Xác thực tài khoản rồi kiểm tra tuần tự: có gắn
    workspace không, trạng thái kích hoạt, đúng nguồn đăng nhập nội bộ. Thất bại ở bước nào trả
    về lỗi 400 tương ứng. Thành công trả về ``Token`` chứa access_token có hạn
    ``ACCESS_TOKEN_EXPIRE_MINUTES``.
    """
    user: BaseUserDTO = authenticate(session=session, account=form_data.username, password=form_data.password)
    if not user:
        raise HTTPException(status_code=400, detail=trans('i18n_login.account_pwd_error'))
    if not user.oid or user.oid == 0:
        raise HTTPException(status_code=400, detail=trans('i18n_login.no_associated_ws', msg = trans('i18n_concat_admin')))
    if user.status != 1:
        raise HTTPException(status_code=400, detail=trans('i18n_login.user_disable', msg = trans('i18n_concat_admin')))
    if user.origin is not None and user.origin != 0:
        raise HTTPException(status_code=400, detail=trans('i18n_login.origin_error'))
    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    user_dict = user.to_dict()
    return Token(access_token=create_access_token(
        user_dict, expires_delta=access_token_expires
    ))

@router.post("/logout")    
async def logout(session: SessionDep, request: Request, dto: LogoutSchema):
    """
    Đăng xuất người dùng.

    Với đăng nhập nội bộ (``dto.origin == 0``) không cần xử lý phía server (token JWT stateless) nên
    trả về ``None``. Với các nguồn đăng nhập khác (SSO/OIDC...) ủy quyền cho module xpack xử lý logout.
    """
    if dto.origin != 0:
        return await xpack_logout(session, request, dto)
    return None