"""078_ai_user_permissions_drop_fields

Drop cột `fields` trên `ai_user_permissions`: field-level metadata (tên/mô tả cột) đã chuyển sang
endpoint edit field/table riêng của SW, không còn đi qua AUTHORIZATION_SYNC nữa. Không backfill —
dữ liệu cũ trong cột này không còn nguồn cập nhật.

Revision ID: 078a1b2c3d4e
Revises: 077a1b2c3d4e
Create Date: 2026-08-14 09:00:00.000000

"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '078a1b2c3d4e'
down_revision = '077a1b2c3d4e'
branch_labels = None
depends_on = None


def upgrade():
    op.drop_column('ai_user_permissions', 'fields')


def downgrade():
    op.add_column('ai_user_permissions', sa.Column(
        'fields', postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'[]'::jsonb"), nullable=False,
    ))
