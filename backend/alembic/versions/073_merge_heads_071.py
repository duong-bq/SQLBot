"""merge_heads_071

Revision ID: 19d092e65217
Revises: a2e2ecfa5a9c, 072c3d4e5f6a7
Create Date: 2026-08-07 17:10:52.709649

"""
from alembic import op
import sqlalchemy as sa
import sqlmodel.sql.sqltypes


# revision identifiers, used by Alembic.
revision = '19d092e65217'
down_revision = ('a2e2ecfa5a9c', '072c3d4e5f6a7')
branch_labels = None
depends_on = None


def upgrade():
    """Gộp 2 nhánh alembic 071 (071_modify_permission_jsonb và 071_add_chat_record_answer)
    phát sinh do hai branch git thêm migration độc lập từ cùng một revision cha; không đổi schema."""
    pass


def downgrade():
    """Không cần tách lại nhánh khi downgrade — hai revision cha vẫn độc lập với nhau."""
    pass
