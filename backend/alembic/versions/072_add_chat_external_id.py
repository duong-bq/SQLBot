"""072_add_chat_external_id

Thêm cột `external_id` vào bảng chat: khóa đối chiếu dạng UUID do hệ thống tích hợp bên ngoài tự
sinh, thay cho việc để họ đặt thẳng `chat.id` (cột Identity always).

Revision ID: 072c3d4e5f6a7
Revises: 071b2c3d4e5f6
Create Date: 2026-07-29 09:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '072c3d4e5f6a7'
down_revision = '071b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade():
    """Thêm cột nullable + unique index.

    Nullable vì hội thoại tạo từ UI web không có external_id; Postgres cho phép nhiều NULL cùng tồn
    tại trong ràng buộc UNIQUE nên không cần backfill. Unique để hai request cùng một UUID chắc chắn
    rơi vào đúng một hội thoại, kể cả khi chạy song song.
    """
    op.add_column('chat', sa.Column('external_id', sa.String(length=64), nullable=True))
    op.create_index('ix_chat_external_id', 'chat', ['external_id'], unique=True)
    op.execute("COMMENT ON COLUMN chat.external_id IS '外部系统自生成的会话标识（UUID），Web端为空'")


def downgrade():
    op.drop_index('ix_chat_external_id', table_name='chat')
    op.drop_column('chat', 'external_id')
