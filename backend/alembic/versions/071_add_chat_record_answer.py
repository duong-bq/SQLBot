"""071_add_chat_record_answer

Thêm cột `answer` vào bảng chat_record để lưu câu trả lời chữ do LLM sinh ở pha cuối pipeline.

Revision ID: 071b2c3d4e5f6
Revises: 070a1b2c3d4e5
Create Date: 2026-07-28 07:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '071b2c3d4e5f6'
down_revision = '070a1b2c3d4e5'
branch_labels = None
depends_on = None


def upgrade():
    """Thêm cột answer (nullable) — record cũ giữ NULL, không cần backfill."""
    op.add_column('chat_record', sa.Column('answer', sa.Text(), nullable=True))
    op.execute("COMMENT ON COLUMN chat_record.answer IS '大模型生成的最终文字回答'")


def downgrade():
    op.drop_column('chat_record', 'answer')
