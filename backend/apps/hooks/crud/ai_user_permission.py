"""Truy cập bảng `ai_user_permissions` — trạng thái quyền hiện tại của user."""

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import Session

from apps.hooks.models.ai_sync_model import AiUserPermission
from apps.hooks.schemas.ai_sync_schema import FormQuery, normalize_fields


def replace_user_permissions(
    session: Session,
    *,
    user_id: str,
    full_name: str | None,
    is_admin: bool,
    form_queries: list[FormQuery],
    sync_version: int,
) -> tuple[int, int]:
    """Áp dụng FULL SNAPSHOT quyền của một user, trả `(số dòng upsert, số dòng xoá)`.

    Ngữ nghĩa: `form_queries` là toàn bộ quyền hiện tại. Form nào không có trong đó thì bị xoá cứng
    — đây là cách duy nhất thu hồi được quyền vì payload của SW không có trường báo xoá. Danh sách
    rỗng nghĩa là thu hồi hết.

    Dùng `INSERT ... ON CONFLICT (user_id, form_uuid) DO UPDATE` thay vì "xoá hết rồi chèn lại":
    giữ được `id` và `created_at` của dòng cũ, và không có khoảng thời gian user mất sạch quyền
    giữa hai câu lệnh.
    """
    now = datetime.now(timezone.utc)
    incoming_uuids = [form.form_uuid for form in form_queries]

    # Xoá trước: form không còn trong snapshot thì mất quyền ngay trong cùng transaction.
    delete_statement = AiUserPermission.__table__.delete().where(
        AiUserPermission.__table__.c.user_id == user_id
    )
    if incoming_uuids:
        delete_statement = delete_statement.where(
            AiUserPermission.__table__.c.form_uuid.notin_(incoming_uuids)
        )
    result = session.execute(delete_statement)
    deleted = result.rowcount or 0

    upserted = 0
    for form in form_queries:
        values = {
            "user_id": user_id,
            "full_name": full_name,
            "is_admin": is_admin,
            "form_uuid": form.form_uuid,
            "database_table_name": form.table_info.database_table_name,
            "table_display_name": form.table_info.table_display_name,
            "table_description": form.table_info.table_description,
            "fields": normalize_fields(form.table_info.field_list),
            "postgres_query": form.postgres_query,
            "clickhouse_query": form.clickhouse_query,
            "sync_version": sync_version,
            "synced_at": now,
            "updated_at": now,
        }
        statement = pg_insert(AiUserPermission.__table__).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=["user_id", "form_uuid"],
            set_={k: v for k, v in values.items() if k not in ("user_id", "form_uuid")},
        )
        session.execute(statement)
        upserted += 1

    session.commit()
    return upserted, deleted


def get_user_permissions(session: Session, user_id: str) -> list[AiUserPermission]:
    """Toàn bộ quyền hiện tại của một user, sắp theo `form_uuid` cho ổn định thứ tự.

    Đây là điểm vào duy nhất mà phase tích hợp pipeline sẽ dùng: chỉ cần `user_id`, không cần biết
    bản tin đến từ đâu.
    """
    statement = (
        select(AiUserPermission)
        .where(AiUserPermission.user_id == user_id)
        .order_by(AiUserPermission.form_uuid)
    )
    return list(session.execute(statement).scalars().all())
