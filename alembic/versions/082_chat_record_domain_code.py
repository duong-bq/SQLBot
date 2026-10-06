"""082_chat_record_domain_code

Thêm cột `domain_code` (nullable) vào `chat_record`: mã lĩnh vực (linhVucMa phía SW) mà lượt hỏi
bị giới hạn theo, đến từ trường `domainCode` của `POST /chat/question`.

Phải lưu theo từng record chứ không suy ra từ request vì `/regenerate` chạy lại một lượt cũ bằng
một request mới không mang `domainCode` — ràng buộc lĩnh vực của lượt gốc chỉ còn đọc được từ đây.
NULL nghĩa là lượt hỏi không giới hạn lĩnh vực (hoặc user ngoài hệ quyền SW).

Revision ID: 082a1b2c3d4e
Revises: 081a1b2c3d4e
Create Date: 2026-08-17 10:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '082a1b2c3d4e'
down_revision = '081a1b2c3d4e'
branch_labels = None
depends_on = None


def upgrade():
    """Thêm một cột nullable — không cần backfill, record cũ mang nghĩa "không giới hạn"."""
    op.add_column('chat_record', sa.Column('domain_code', sa.String(length=100), nullable=True))


def downgrade():
    op.drop_column('chat_record', 'domain_code')
