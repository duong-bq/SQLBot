"""075_ai_user_permissions_domain_info

Thêm 4 cột metadata lĩnh vực (domain) của bảng nghiệp vụ vào `ai_user_permissions`: SW đã bổ sung
`linhVucMa`/`linhVucUuid`/`linhVucName`/`linhVucDescription` vào `tableInfo` của bản tin
AUTHORIZATION_SYNC. Chỉ để đọc kèm khi truy vấn quyền, không chuẩn hoá bảng riêng.

Revision ID: 075a1b2c3d4e
Revises: 074a1b2c3d4e
Create Date: 2026-08-12 10:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '075a1b2c3d4e'
down_revision = '074a1b2c3d4e'
branch_labels = None
depends_on = None


def upgrade():
    """Thêm 4 cột domain (nullable, không default) + index thường trên `domain_code`.

    Không backfill: dữ liệu cũ (ghi trước khi SW gửi domain info) giữ NULL — tương thích ngược vì
    cả 4 cột đều optional ở tầng schema input.
    """
    op.add_column('ai_user_permissions', sa.Column('domain_code', sa.String(length=100), nullable=True))
    op.add_column('ai_user_permissions', sa.Column('domain_uuid', sa.String(length=100), nullable=True))
    op.add_column('ai_user_permissions', sa.Column('domain_name', sa.String(length=255), nullable=True))
    op.add_column('ai_user_permissions', sa.Column('domain_description', sa.Text(), nullable=True))
    op.create_index('idx_ai_user_permissions_domain_code', 'ai_user_permissions', ['domain_code'])


def downgrade():
    op.drop_index('idx_ai_user_permissions_domain_code', table_name='ai_user_permissions')
    op.drop_column('ai_user_permissions', 'domain_description')
    op.drop_column('ai_user_permissions', 'domain_name')
    op.drop_column('ai_user_permissions', 'domain_uuid')
    op.drop_column('ai_user_permissions', 'domain_code')
