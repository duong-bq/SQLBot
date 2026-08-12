"""Router đọc quyền user đã lưu trong `ai_user_permissions` — phục vụ dev/test.

Khác `api/ai_sync.py` (route ghi, phải trả raw JSON theo hợp đồng máy-máy với SW), đây là API đọc
nội bộ nên trả thẳng Pydantic model/list theo convention chuẩn của SQLBot: `ResponseMiddleware` tự
bọc envelope `{code,data,msg}` như mọi API admin khác trong repo (xem `table_relation.py`).
"""

from fastapi import APIRouter, Path

from apps.hooks.crud.ai_user_permission import (
    get_user_permissions,
    list_users_with_permission_summary,
)
from apps.hooks.schemas.permission_query_schema import (
    UserPermissionDetailOut,
    UserPermissionSummaryOut,
    to_permission_detail,
)
from apps.system.schemas.permission import SqlbotPermission, require_permissions
from common.core.deps import SessionDep

router = APIRouter(tags=["AI Sync Hook"], prefix="/hooks/ai-sync/permissions")


@router.get(
    "", response_model=list[UserPermissionSummaryOut],
    summary="Tóm tắt quyền tất cả user (dev/test)",
)
@require_permissions(permission=SqlbotPermission(role=['ws_admin']))
async def list_permission_summary(session: SessionDep) -> list[UserPermissionSummaryOut]:
    """Liệt kê tóm tắt quyền hiện tại của mọi user đang có ≥1 dòng trong `ai_user_permissions`.

    Dùng để dev/test chọn nhanh `userId` cần xem chi tiết mà không cần biết trước userId.
    """
    rows = list_users_with_permission_summary(session)
    return [
        UserPermissionSummaryOut(
            userId=row["user_id"],
            fullName=row["full_name"],
            isAdmin=row["is_admin"],
            formCount=row["form_count"],
            syncVersion=row["sync_version"],
            syncedAt=row["synced_at"],
        )
        for row in rows
    ]


@router.get(
    "/{user_id}", response_model=UserPermissionDetailOut,
    summary="Chi tiết quyền của 1 user (dev/test)",
)
@require_permissions(permission=SqlbotPermission(role=['ws_admin']))
async def get_permission_detail(
    session: SessionDep, user_id: str = Path(...)
) -> UserPermissionDetailOut:
    """Trả chi tiết quyền hiện tại của 1 user, gom lại giống payload SW gửi vào hook.

    User không có dòng nào trong `ai_user_permissions` (chưa từng đồng bộ, hoặc đã bị thu hồi hết
    quyền — hai ca không phân biệt được vì không đọc audit log) trả HTTP 200 với field liên quan để
    `null`/`formQueries: []` — không phải lỗi.
    """
    rows = get_user_permissions(session, user_id)
    return to_permission_detail(user_id, rows)
