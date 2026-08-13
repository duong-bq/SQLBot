"""077_ai_user_permissions_queries

Thay 2 cột `postgres_query`/`clickhouse_query` bằng 1 cột JSONB `queries` trên
`ai_user_permissions`: SW đổi payload sang mảng `tableInfo.queries[]` tổng quát theo
`datasourceId`/`datasourceType`, không còn cố định 2 loại datasource. Breaking change — cắt hẳn,
không backfill dữ liệu cũ (không đủ thông tin `datasourceId`/`datasourceType` để suy ra).

Revision ID: 077a1b2c3d4e
Revises: 076a1b2c3d4e
Create Date: 2026-08-13 10:00:00.000000

"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '077a1b2c3d4e'
down_revision = '076a1b2c3d4e'
branch_labels = None
depends_on = None


def upgrade():
    """Thêm cột `queries` (JSONB, not null, default `[]`), drop hẳn 2 cột cũ.

    Dữ liệu cũ dù sao cũng bị ghi đè ở lần AUTHORIZATION_SYNC kế tiếp của từng user (full snapshot)
    nên không cần giữ lại cột cũ ở dạng "mồ côi".
    """
    op.add_column('ai_user_permissions', sa.Column(
        'queries', postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'[]'::jsonb"), nullable=False,
    ))
    op.drop_column('ai_user_permissions', 'postgres_query')
    op.drop_column('ai_user_permissions', 'clickhouse_query')


def downgrade():
    op.add_column('ai_user_permissions', sa.Column('clickhouse_query', sa.Text(), nullable=True))
    op.add_column('ai_user_permissions', sa.Column('postgres_query', sa.Text(), nullable=True))
    op.drop_column('ai_user_permissions', 'queries')
