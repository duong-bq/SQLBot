"""Bảng tra handler theo actionType.

Thêm một actionType mới = thêm một handler + một dòng ở đây, KHÔNG sửa route. Cố ý dùng dict thay
vì registry/base-class: hiện chỉ có một handler nên chưa có đủ dữ kiện để trừu tượng hoá.
"""

from collections.abc import Callable

from sqlmodel import Session

from apps.hooks.constants import SyncActionType
from apps.hooks.handlers.authorization import HandlerResult, handle_authorization_sync
from apps.hooks.schemas.ai_sync_schema import SyncEnvelope

ACTION_HANDLERS: dict[SyncActionType, Callable[[Session, SyncEnvelope, int], HandlerResult]] = {
    SyncActionType.AUTHORIZATION_SYNC: handle_authorization_sync,
}

__all__ = ["ACTION_HANDLERS", "HandlerResult", "handle_authorization_sync"]
