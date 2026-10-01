"""085_ai_model_config

Bảng `ai_model_config`: model (llm, embedding, rerank) của từng workspace và từng datasource do AI
Gateway đẩy sang. Dòng workspace là cấu hình mặc định; dòng datasource là bản chép lúc tạo
datasource. Bảng mới hoàn toàn, không đụng bảng upstream, không backfill: không có dòng nào thì
SQLBot chạy như cũ với model chung.

Revision ID: 085a1b2c3d4e
Revises: 084a1b2c3d4e
Create Date: 2026-10-01 09:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '085a1b2c3d4e'
down_revision = '084a1b2c3d4e'
branch_labels = None
depends_on = None


def upgrade():
    """Tạo bảng `ai_model_config` cùng unique theo phạm vi và index theo workspace."""
    op.create_table(
        'ai_model_config',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('scope', sa.String(length=16), nullable=False),
        sa.Column('scope_id', sa.BigInteger(), nullable=False),
        sa.Column('oid', sa.BigInteger(), nullable=False),
        sa.Column('model_type', sa.String(length=16), nullable=False),
        sa.Column('base_url', sa.Text(), nullable=False),
        sa.Column('model', sa.String(length=255), nullable=False),
        sa.Column('api_key_enc', sa.Text(), nullable=False),
        sa.Column('api_key_hint', sa.String(length=16), nullable=False),
        sa.Column('dim', sa.Integer(), nullable=True),
        sa.Column('origin', sa.String(length=16), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'scope', 'scope_id', 'model_type', name='uq_ai_model_config_scope'
        ),
    )
    op.create_index(op.f('ix_ai_model_config_id'), 'ai_model_config', ['id'])
    op.create_index('idx_ai_model_config_oid', 'ai_model_config', ['oid'])


def downgrade():
    """Xoá bảng `ai_model_config` (mất mọi cấu hình model đã đẩy sang)."""
    op.drop_index('idx_ai_model_config_oid', table_name='ai_model_config')
    op.drop_index(op.f('ix_ai_model_config_id'), table_name='ai_model_config')
    op.drop_table('ai_model_config')
