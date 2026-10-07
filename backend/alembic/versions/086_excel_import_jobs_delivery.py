"""086_excel_import_jobs_delivery

Thêm cột `delivery` vào `excel_import_jobs`: cách báo kết quả của job. `callback` là luồng bất đồng
bộ có sẵn; `response` là `/createFromExcel` đồng bộ, trả kết quả ngay trong HTTP response và không
bao giờ sinh callback.

Revision ID: 086a1b2c3d4e
Revises: 085a1b2c3d4e
Create Date: 2026-10-07 09:00:00.000000

"""
import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = '086a1b2c3d4e'
down_revision = '085a1b2c3d4e'
branch_labels = None
depends_on = None


def upgrade():
    """Thêm cột NOT NULL có server default `callback`.

    Server default làm mọi dòng cũ thành `callback`, đúng với cách chúng được tạo ra, nên hành vi
    của job đang chạy dở lúc deploy không đổi. Không index: không truy vấn nào lọc theo cột này.
    """
    op.add_column(
        'excel_import_jobs',
        sa.Column('delivery', sa.String(length=20), server_default='callback', nullable=False),
    )


def downgrade():
    """Bỏ cột `delivery`."""
    op.drop_column('excel_import_jobs', 'delivery')
