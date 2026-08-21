"""Test cho ``trim_chat_brief`` — hàm cắt tiêu đề hội thoại trước khi ghi vào cột ``chat.brief``.

Trần phải bám đúng ``max_length`` của cột (64). Trước đây mọi chỗ đặt tiêu đề đều cắt cứng ở 20 ký
tự nên tiêu đề do LLM đặt thường xuyên mất chữ cuối; test này giữ cả trần lẫn quy tắc không đứt
giữa một từ.
"""

# Import sqlbot_xpack TRƯỚC: apps.chat.curd.chat nằm trong một vòng import lẫn nhau với
# apps.datasource.crud.datasource, chỉ gỡ được khi xpack là module khởi động vòng đó.
import sqlbot_xpack  # noqa: F401  isort:skip

from apps.chat.curd.chat import CHAT_BRIEF_MAX_CHARS, trim_chat_brief
from apps.chat.models.chat_model import Chat


def test_tran_bam_theo_cot_db():
    """Trần của hàm phải đúng bằng sức chứa của cột, không được lệch."""
    assert CHAT_BRIEF_MAX_CHARS == Chat.__table__.c.brief.type.length


def test_tieu_de_ngan_giu_nguyen():
    """Dưới trần thì chỉ trim khoảng trắng, không cắt chữ nào — đây là ca thường gặp nhất."""
    assert trim_chat_brief('  Số lượng đại biểu họ X  ') == 'Số lượng đại biểu họ X'


def test_rong_va_none():
    assert trim_chat_brief('') == ''
    assert trim_chat_brief(None) == ''


def test_vuot_tran_thi_cat_o_ranh_gioi_tu():
    """Vượt trần thì lùi về khoảng trắng gần cuối nhất, không để lại từ cụt."""
    brief = trim_chat_brief('Có bao nhiêu nghị quyết được ban hành trong năm 2024 theo từng lĩnh vực')
    assert len(brief) <= CHAT_BRIEF_MAX_CHARS
    assert brief == 'Có bao nhiêu nghị quyết được ban hành trong năm 2024 theo từng'


def test_chuoi_dinh_lien_thi_cat_cung():
    """Không có khoảng trắng nào hợp lý để lùi thì đành cắt cứng ở trần."""
    assert trim_chat_brief('x' * 100) == 'x' * CHAT_BRIEF_MAX_CHARS
