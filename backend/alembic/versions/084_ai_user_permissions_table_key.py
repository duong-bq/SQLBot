"""084_ai_user_permissions_table_key

Đổi khoá định danh dòng quyền trong `ai_user_permissions` từ `(user_id, form_uuid)` sang
`(user_id, database_table_name)`. `formUuid` optional ở phía SW — thứ SQLBot thực sự cần là "user
này truy cập được bảng nào", không phải "form nào" — nên khoá so khớp full-snapshot chuyển hẳn sang
tên bảng, `form_uuid` chỉ còn lưu tham khảo (nullable).

Trước khi thêm unique constraint mới phải dọn trùng dữ liệu cũ: ở thiết kế trước, một user có thể có
2 dòng cùng bảng nhưng khác `form_uuid` (2 form khác nhau cùng trỏ 1 bảng) — hợp lệ theo khoá cũ,
nhưng vi phạm khoá mới. Giữ lại dòng có `updated_at` mới nhất (tie-break bằng `id`), xoá các dòng
còn lại — hành vi này khớp đúng "bản snapshot gần nhất thắng" mà route AUTHORIZATION_SYNC luôn áp
dụng.

Revision ID: 084a1b2c3d4e
Revises: 083a1b2c3d4e
Create Date: 2026-08-19 09:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '084a1b2c3d4e'
down_revision = '083a1b2c3d4e'
branch_labels = None
depends_on = None


def upgrade():
    """Dọn trùng theo khoá mới, đổi unique constraint, và nới `form_uuid` thành nullable."""
    op.execute(
        """
        DELETE FROM ai_user_permissions a
        USING ai_user_permissions b
        WHERE a.user_id = b.user_id
          AND a.database_table_name = b.database_table_name
          AND (a.updated_at, a.id) < (b.updated_at, b.id)
        """
    )
    op.drop_constraint(
        'uq_ai_user_permissions_user_form', 'ai_user_permissions', type_='unique'
    )
    op.drop_index('idx_ai_user_permissions_user_table', table_name='ai_user_permissions')
    op.alter_column(
        'ai_user_permissions', 'form_uuid',
        existing_type=sa.String(length=100), nullable=True,
    )
    op.create_unique_constraint(
        'uq_ai_user_permissions_user_table', 'ai_user_permissions',
        ['user_id', 'database_table_name'],
    )


def downgrade():
    """Đảo ngược — CHÚ Ý: sẽ fail nếu dữ liệu hiện tại có `form_uuid` NULL hoặc trùng theo
    `(user_id, form_uuid)`, vì đó là trạng thái hợp lệ SAU migration này nhưng không hợp lệ TRƯỚC."""
    op.drop_constraint(
        'uq_ai_user_permissions_user_table', 'ai_user_permissions', type_='unique'
    )
    op.alter_column(
        'ai_user_permissions', 'form_uuid',
        existing_type=sa.String(length=100), nullable=False,
    )
    op.create_index(
        'idx_ai_user_permissions_user_table', 'ai_user_permissions',
        ['user_id', 'database_table_name'],
    )
    op.create_unique_constraint(
        'uq_ai_user_permissions_user_form', 'ai_user_permissions', ['user_id', 'form_uuid'],
    )
