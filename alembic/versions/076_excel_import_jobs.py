"""076_excel_import_jobs

Tạo bảng `excel_import_jobs` cho luồng import excel bất đồng bộ (kiêm outbox callback), và làm cột
`core_datasource.status` trở nên dùng được.

Revision ID: 076a1b2c3d4e
Revises: 075a1b2c3d4e
Create Date: 2026-08-12 09:00:00.000000

"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '076a1b2c3d4e'
down_revision = '075a1b2c3d4e'
branch_labels = None
depends_on = None


def upgrade():
    """Tạo bảng job + backfill `core_datasource.status`.

    Backfill là bắt buộc, không phải dọn dẹp cho đẹp. Cột `status` khai nullable và không có server
    default (migration 003), lại chỉ được ghi bởi `create_ds` và `update_ds` — nên mọi nguồn dữ
    liệu tạo bằng script, chèn tay, hoặc bằng bản cũ hơn hai hàm đó đang mang NULL. Từ bản này trở
    đi cột được đem ra lọc, nếu không backfill thì các dòng NULL biến mất khỏi danh sách và khỏi
    hỏi đáp ngay khi deploy — kể cả nguồn dữ liệu đang chạy thật.

    Backfill lo cho dữ liệu hiện có; phép lọc theo lối loại trừ (`usable_ds_condition`) lo cho dữ
    liệu tương lai. Bỏ lớp nào cũng để hở.

    KHÔNG tạo unique index trên (name, oid) ở bản này: `check_name` đang thủng với user có oid rỗng
    nên dữ liệu hiện tại rất có thể đã có tên trùng, và migration sẽ hỏng giữa chừng. Cơ chế chống
    trùng là advisory lock ở tầng ứng dụng. Muốn thêm index thì phải kiểm dữ liệu trước bằng một
    câu GROUP BY, và làm ở một migration riêng.
    """
    op.create_table(
        'excel_import_jobs',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column('ds_id', sa.BigInteger(), nullable=False),
        sa.Column('oid', sa.BigInteger(), nullable=False),
        sa.Column('create_by', sa.BigInteger(), nullable=True),
        sa.Column('file_path', sa.Text(), nullable=False),
        sa.Column('sheet_names', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('ds_name', sa.String(length=128), nullable=False),
        sa.Column('status', sa.String(length=30), server_default='pending', nullable=False),
        sa.Column('worker_id', sa.String(length=128), nullable=True),
        sa.Column('heartbeat_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('started_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column('finished_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column('created_tables', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('error_code', sa.String(length=100), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('callback_status', sa.String(length=30), server_default='none', nullable=False),
        sa.Column('callback_attempts', sa.Integer(), server_default='0', nullable=False),
        sa.Column('next_attempt_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column('callback_claimed_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column('callback_last_error', sa.Text(), nullable=True),
        sa.Column('callback_payload', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('ds_id', name='uq_excel_import_jobs_ds_id'),
    )
    op.create_index(
        'idx_excel_import_jobs_callback', 'excel_import_jobs', ['callback_status', 'next_attempt_at']
    )
    op.create_index(
        'idx_excel_import_jobs_status_heartbeat', 'excel_import_jobs', ['status', 'heartbeat_at']
    )

    op.execute("UPDATE core_datasource SET status = 'Success' WHERE status IS NULL OR status = ''")


def downgrade():
    """Chỉ bỏ bảng job.

    Không hoàn tác backfill: không có cách nào biết dòng nào vốn là NULL, và đưa về NULL cũng không
    khôi phục được trạng thái nào có ý nghĩa.
    """
    op.drop_index('idx_excel_import_jobs_status_heartbeat', table_name='excel_import_jobs')
    op.drop_index('idx_excel_import_jobs_callback', table_name='excel_import_jobs')
    op.drop_table('excel_import_jobs')
