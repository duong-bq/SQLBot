"""Schema output cho API đọc quyền user (dev/test) — GET /hooks/ai-sync/permissions*.

Tách khỏi `ai_sync_schema.py` vì khác trách nhiệm: file đó là input do SW gửi vào hook, file này là
output đọc lại từ `ai_user_permissions`. Field đặt tên camelCase để serialize thẳng, giống response
của hook, cho dễ đối chiếu "SW gửi gì — SQLBot lưu được gì".
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from apps.hooks.models.ai_sync_model import AiUserPermission


class PermissionTableInfoOut(BaseModel):
    """Bảng nghiệp vụ, đọc lại từ 1 dòng `ai_user_permissions`.

    Không còn `fields`/`tableDescription` — field-level metadata và mô tả bảng đã chuyển sang
    endpoint edit field/table riêng.
    """

    model_config = ConfigDict(populate_by_name=True)

    databaseTableName: str
    tableDisplayName: str | None = None
    linhVucMa: str | None = None
    linhVucUuid: str | None = None
    linhVucName: str | None = None
    linhVucDescription: str | None = None
    queries: list[dict[str, Any]]


class PermissionFormQueryOut(BaseModel):
    """Quyền trên 1 form, đọc lại từ 1 dòng `ai_user_permissions`."""

    model_config = ConfigDict(populate_by_name=True)

    formUuid: str
    tableInfo: PermissionTableInfoOut


class UserPermissionDetailOut(BaseModel):
    """Response của `GET /hooks/ai-sync/permissions/{user_id}`."""

    model_config = ConfigDict(populate_by_name=True)

    userId: str
    fullName: str | None = None
    isAdmin: bool | None = None
    syncVersion: int | None = None
    syncedAt: datetime | None = None
    formQueries: list[PermissionFormQueryOut] = []


class UserPermissionSummaryOut(BaseModel):
    """1 phần tử trong response của `GET /hooks/ai-sync/permissions`."""

    model_config = ConfigDict(populate_by_name=True)

    userId: str
    fullName: str | None = None
    isAdmin: bool | None = None
    formCount: int
    syncVersion: int | None = None
    syncedAt: datetime | None = None


def to_permission_detail(user_id: str, rows: list[AiUserPermission]) -> UserPermissionDetailOut:
    """Map danh sách dòng `ai_user_permissions` của 1 user thành response chi tiết.

    `rows` rỗng (chưa từng đồng bộ, hoặc đã bị thu hồi hết quyền — hai ca không phân biệt được vì
    hàm này không đọc audit log) trả về object toàn `null`/`formQueries: []`, KHÔNG raise lỗi — "hiện
    không có quyền nào" là trạng thái hợp lệ. `syncVersion`/`syncedAt` lấy từ dòng đầu tiên vì mọi
    dòng của cùng user luôn cùng giá trị (`replace_user_permissions` ghi đồng loạt cho cả batch).
    """
    if not rows:
        return UserPermissionDetailOut(userId=user_id, formQueries=[])

    first = rows[0]
    return UserPermissionDetailOut(
        userId=user_id,
        fullName=first.full_name,
        isAdmin=first.is_admin,
        syncVersion=first.sync_version,
        syncedAt=first.synced_at,
        formQueries=[
            PermissionFormQueryOut(
                formUuid=row.form_uuid,
                tableInfo=PermissionTableInfoOut(
                    databaseTableName=row.database_table_name,
                    tableDisplayName=row.table_display_name,
                    linhVucMa=row.domain_code,
                    linhVucUuid=row.domain_uuid,
                    linhVucName=row.domain_name,
                    linhVucDescription=row.domain_description,
                    queries=row.queries,
                ),
            )
            for row in rows
        ],
    )
