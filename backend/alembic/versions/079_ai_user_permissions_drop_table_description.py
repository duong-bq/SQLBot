"""079_ai_user_permissions_drop_table_description

Drop cột `table_description` trên `ai_user_permissions`: mô tả bảng đã chuyển sang endpoint edit
field/table riêng của SW, không còn đi qua AUTHORIZATION_SYNC nữa. Không backfill — dữ liệu cũ
trong cột này không còn nguồn cập nhật.

Revision ID: 079a1b2c3d4e
Revises: 078a1b2c3d4e
Create Date: 2026-08-14 10:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '079a1b2c3d4e'
down_revision = '078a1b2c3d4e'
branch_labels = None
depends_on = None


def upgrade():
    op.drop_column('ai_user_permissions', 'table_description')


def downgrade():
    op.add_column('ai_user_permissions', sa.Column('table_description', sa.Text(), nullable=True))
