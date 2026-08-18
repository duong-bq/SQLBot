"""083_core_field_lookup_index

Index cho cặp cột mà mọi luồng đồng bộ metadata dùng để tra một cột: `(table_id, field_name)`.

`sync_fields` tra từng cột một để quyết định INSERT hay UPDATE, nên một lần đồng bộ sinh ra đúng
bằng số cột lời gọi SELECT. Không có index, mỗi lời gọi là seq scan toàn bộ `core_field` — chi phí
tỉ lệ với tổng số cột của TOÀN hệ thống chứ không phải của nguồn đang đồng bộ, nên càng thêm
datasource thì đồng bộ nguồn nào cũng chậm đi.

Đo trên nguồn 537 bảng / 10450 cột: 0.93 ms -> 0.26 ms mỗi lời tra, tổng một lượt resync
92.9s -> 79.7s.

Không đặt UNIQUE: cùng một `field_name` trong cùng `table_id` lẽ ra không trùng, nhưng dữ liệu
cũ chưa chắc sạch và migration này không được phép fail vì chuyện đó.

Revision ID: 083a1b2c3d4e
Revises: 082a1b2c3d4e
Create Date: 2026-08-18 12:00:00.000000

"""
from alembic import op

# revision identifiers, used by Alembic.
revision = '083a1b2c3d4e'
down_revision = '082a1b2c3d4e'
branch_labels = None
depends_on = None


def upgrade():
    """Thêm index tra cột. `if_not_exists` vì index có thể đã được tạo tay khi đo hiệu năng."""
    op.create_index(
        'ix_core_field_table_id_field_name',
        'core_field',
        ['table_id', 'field_name'],
        if_not_exists=True,
    )


def downgrade():
    op.drop_index('ix_core_field_table_id_field_name', table_name='core_field', if_exists=True)
