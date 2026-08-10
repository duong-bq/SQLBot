"""074_ai_sync_hook

Tạo 2 bảng của AI Sync Hook: `ai_sync_hook_logs` (audit raw request từ SW) và
`ai_user_permissions` (trạng thái quyền hiện tại của user sau khi parse).

Revision ID: 074a1b2c3d4e
Revises: 19d092e65217
Create Date: 2026-08-10 10:00:00.000000

"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '074a1b2c3d4e'
down_revision = '19d092e65217'
branch_labels = None
depends_on = None


def upgrade():
    """Tạo bảng + index.

    Khoá chính UUID sinh phía DB bằng `gen_random_uuid()` — hàm built-in từ Postgres 13, image của
    SQLBot là pg16/pg17 nên không cần CREATE EXTENSION.

    Unique index trên `idempotency_key` là **partial** (chỉ khi NOT NULL): đó vừa là cơ chế chống
    xử lý trùng khi SW retry, vừa cho phép dòng log không có idempotency key cùng tồn tại.
    """
    op.create_table(
        'ai_sync_hook_logs',
        sa.Column('id', postgresql.UUID(as_uuid=True), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('request_id', sa.String(length=100), nullable=True),
        sa.Column('idempotency_key', sa.String(length=100), nullable=True),
        sa.Column('action_type', sa.Integer(), nullable=False),
        sa.Column('schema_version', sa.String(length=20), nullable=True),
        sa.Column('source', sa.String(length=50), server_default=sa.text("'SW'"), nullable=False),
        sa.Column('user_id', sa.String(length=100), nullable=True),
        sa.Column('request_payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('status', sa.String(length=30), nullable=False),
        sa.Column('sync_version', sa.BigInteger(), nullable=True),
        sa.Column('error_code', sa.String(length=100), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('received_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('processed_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('idx_ai_sync_hook_logs_user_id', 'ai_sync_hook_logs', ['user_id'])
    op.create_index('idx_ai_sync_hook_logs_action_type', 'ai_sync_hook_logs', ['action_type'])
    op.create_index('idx_ai_sync_hook_logs_received_at', 'ai_sync_hook_logs', ['received_at'])
    op.create_index(
        'uq_ai_sync_hook_logs_idempotency', 'ai_sync_hook_logs', ['idempotency_key'],
        unique=True, postgresql_where=sa.text('idempotency_key IS NOT NULL'),
    )
    op.execute("COMMENT ON TABLE ai_sync_hook_logs IS 'Audit các bản tin đồng bộ SW gửi vào AI Sync Hook'")
    op.execute("COMMENT ON COLUMN ai_sync_hook_logs.sync_version IS 'epoch millis từ timestamp của SW, dùng làm mốc chống bản tin lùi'")

    op.create_table(
        'ai_user_permissions',
        sa.Column('id', postgresql.UUID(as_uuid=True), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('user_id', sa.String(length=100), nullable=False),
        sa.Column('full_name', sa.String(length=255), nullable=True),
        sa.Column('is_admin', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('form_uuid', sa.String(length=100), nullable=False),
        sa.Column('database_table_name', sa.String(length=255), nullable=False),
        sa.Column('table_display_name', sa.String(length=255), nullable=True),
        sa.Column('table_description', sa.Text(), nullable=True),
        sa.Column('fields', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column('postgres_query', sa.Text(), nullable=True),
        sa.Column('clickhouse_query', sa.Text(), nullable=True),
        sa.Column('sync_version', sa.BigInteger(), nullable=False),
        sa.Column('synced_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'form_uuid', name='uq_ai_user_permissions_user_form'),
    )
    op.create_index('idx_ai_user_permissions_user_id', 'ai_user_permissions', ['user_id'])
    op.create_index('idx_ai_user_permissions_user_table', 'ai_user_permissions', ['user_id', 'database_table_name'])
    op.execute("COMMENT ON TABLE ai_user_permissions IS 'Quyền hiện tại của user theo form, parse từ bản tin AUTHORIZATION_SYNC'")


def downgrade():
    op.drop_index('idx_ai_user_permissions_user_table', table_name='ai_user_permissions')
    op.drop_index('idx_ai_user_permissions_user_id', table_name='ai_user_permissions')
    op.drop_table('ai_user_permissions')
    op.drop_index('uq_ai_sync_hook_logs_idempotency', table_name='ai_sync_hook_logs')
    op.drop_index('idx_ai_sync_hook_logs_received_at', table_name='ai_sync_hook_logs')
    op.drop_index('idx_ai_sync_hook_logs_action_type', table_name='ai_sync_hook_logs')
    op.drop_index('idx_ai_sync_hook_logs_user_id', table_name='ai_sync_hook_logs')
    op.drop_table('ai_sync_hook_logs')
