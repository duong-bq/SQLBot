"""081_chat_attachment

Tạo bảng `chat_attachment`: text đã trích từ file .docx đính kèm một lượt hỏi của hội thoại
(`POST /chat/question` nhận thêm `fileUrl` là presigned URL).

Phải lưu bản trích ra DB vì hai lý do, không lý do nào tránh được:

- Presigned URL hết hạn sau ít phút; các lượt hỏi sau muốn dùng lại tài liệu thì không còn đường
  tải lại — bản text trong DB là bản gốc duy nhất.
- Cột `chat_record.question` phải giữ sạch (UI, `/regenerate`, sinh brief, khối lịch sử pha answer
  đều tiêu thụ nó), nên nội dung tài liệu không được ghép vào đó.

Tài liệu được đối xử như MESSAGE chứ không phải hạ tầng kiểu schema: nó đi theo dòng chảy hội
thoại, chịu cắt trần ký tự và trôi theo cửa sổ lịch sử. Bảng này chỉ là nơi giữ bản gốc để các
chính sách quản lý hội thoại (hiện tại và tương lai) tự quyết định đưa bao nhiêu vào prompt.

Revision ID: 081a1b2c3d4e
Revises: 080a1b2c3d4e
Create Date: 2026-08-14 14:00:00.000000

"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '081a1b2c3d4e'
down_revision = '080a1b2c3d4e'
branch_labels = None
depends_on = None


def upgrade():
    """Tạo bảng mới, không đụng bảng nào sẵn có.

    `record_id` là lượt hỏi ĐÍNH file — mỗi lượt tối đa một tài liệu theo hợp đồng API hiện tại,
    nhưng cố ý KHÔNG đặt unique để sau này nhận nhiều file một lượt thì không phải migration lại.

    `truncated` ghi việc bản trích đã bị cắt ngay từ lúc đọc file (trần ký tự chống zip bomb).
    Phải lưu thành cột vì mọi lần render sau này đều phải nhắc LLM "tài liệu không đầy đủ" — quy
    tắc chung của repo: cắt dữ liệu đưa vào prompt thì phải nói cho LLM biết đã cắt.
    """
    op.create_table(
        'chat_attachment',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column('chat_id', sa.BigInteger(), nullable=False),
        sa.Column('record_id', sa.BigInteger(), nullable=False),
        sa.Column('filename', sa.Text(), nullable=True),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('truncated', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('create_time', postgresql.TIMESTAMP(timezone=False), server_default=sa.func.now(),
                  nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    # Lịch sử pha answer join theo record_id của các lượt trong cửa sổ; danh sách hội thoại có thể
    # cần gom theo chat_id. Cả hai đều là phép tra điểm nên index thường là đủ.
    op.create_index('idx_chat_attachment_record', 'chat_attachment', ['record_id'])
    op.create_index('idx_chat_attachment_chat', 'chat_attachment', ['chat_id'])


def downgrade():
    op.drop_index('idx_chat_attachment_chat', table_name='chat_attachment')
    op.drop_index('idx_chat_attachment_record', table_name='chat_attachment')
    op.drop_table('chat_attachment')
