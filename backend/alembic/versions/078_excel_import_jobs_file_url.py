"""078_excel_import_jobs_file_url

Thêm cột `file_url` vào `excel_import_jobs`: hệ ngoài chuyển từ đẩy bytes lên bằng multipart sang
đưa presigned URL để SQLBot tự tải, và việc tải nằm ở worker nền chứ không ở pha đồng bộ. Worker
chạy ở tiến trình khác — có thể ở lần khởi động khác của ứng dụng — nên URL bắt buộc phải nằm
trong DB chứ không thể chỉ tồn tại trong RAM của request.

Revision ID: 078a1b2c3d4e
Revises: 077a1b2c3d4e
Create Date: 2026-08-14 09:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '078a1b2c3d4e'
down_revision = '077a1b2c3d4e'
branch_labels = None
depends_on = None


def upgrade():
    """Thêm một cột nullable. Không backfill, không index, không đụng ràng buộc cũ.

    Nullable là quyết định có chủ ý chứ không phải để tránh phiền: NULL mang nghĩa "job theo đường
    multipart cũ", và nhánh đó vẫn phải chạy được. Lúc deploy hoàn toàn có thể còn job đang
    `pending` do bản trước tạo ra, với file đã nằm sẵn trên đĩa và không có URL nào để tải.

    Không đánh index: không truy vấn nào lọc theo cột này.

    Cột `file_path` giữ nguyên NOT NULL. Ở luồng mới pha đồng bộ chưa có file, nhưng nó vẫn đặt
    trước tên file mà worker sẽ ghi vào — nên ràng buộc cũ không phải nới, và mọi chỗ đang đọc
    `file_path` không phải thêm phép kiểm rỗng.
    """
    op.add_column('excel_import_jobs', sa.Column('file_url', sa.Text(), nullable=True))


def downgrade():
    op.drop_column('excel_import_jobs', 'file_url')
