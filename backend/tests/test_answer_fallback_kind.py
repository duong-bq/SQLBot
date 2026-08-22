"""Test cho việc tham số hoá prompt hạ cấp theo ``fallback_kind``.

Nhánh answer hạ cấp trước đây chỉ phục vụ đúng một tình huống: pha SQL hỏng. Nay nó phải phục vụ
thêm tình huống hệ thống CHỦ ĐỘNG bỏ qua truy vấn, nên mấy dòng khung kể chuyện được tách ra
``answer.fallback_variants`` còn bộ rule thì dùng chung.

Hai rủi ro mà test này canh:

- **Rò giọng sự cố.** Nhánh ``no_query`` mà còn chữ "thất bại"/"lỗi" trong prompt thì LLM sẽ báo
  cho người dùng một sự cố không hề xảy ra. Đây là lỗi nhìn diff không thấy vì prompt là dữ liệu.
- **Hai bộ rule trôi khỏi nhau.** Lý do duy nhất để dùng chung một template thay vì hai bản sao là
  bộ rule phải y hệt nhau; test khẳng định điều đó thay vì tin vào thiện chí của lần sửa sau.

Không cần ``LLMService``: prompt được render thẳng từ ``ChatQuestion``, tầng dưới cùng của chuỗi.
"""

# Import sqlbot_xpack TRƯỚC: apps.chat.* nằm trong một vòng import lẫn nhau, chỉ gỡ được khi xpack
# là module khởi động vòng đó.
import sqlbot_xpack  # noqa: F401  isort:skip

import pytest

from apps.chat.models.chat_model import ChatQuestion

# Câu chữ chỉ được có ở nhánh hỏng thật. Để lọt sang nhánh no_query là bug.
FAILURE_WORDS = ['thất bại', 'thông báo lỗi kỹ thuật', 'chưa lấy được dữ liệu']

# Rule dùng chung. Mất một dòng nào ở đây nghĩa là template đã bị tách đôi ngầm.
SHARED_RULES = [
    'trả lời bằng đúng ngôn ngữ đó',
    'THỨ TỰ ƯU TIÊN',
    'VỀ CHÍNH DATASOURCE',
    'không bịa số liệu',
    'tối đa khoảng 3 đến 5 câu',
]


def build(kind: str) -> tuple[str, str]:
    """Render cặp prompt hạ cấp cho một ``kind``, với nội dung giả đủ nhận ra trong output."""
    q = ChatQuestion(chat_id=1, question='trong đó cái nào lớn nhất')
    q.fallback_kind = kind
    q.fallback_reason = 'LY-DO'
    q.fallback_schema = 'BANG-A\nBANG-B'
    q.fallback_history = 'LICH-SU'
    return q.answer_fallback_sys_question(), q.answer_fallback_user_question()


def test_nhanh_failed_van_ke_dung_su_co():
    sys_prompt, _ = build('failed')
    assert 'thất bại' in sys_prompt
    assert 'chưa lấy được dữ liệu' in sys_prompt


def test_nhanh_no_query_khong_ke_su_co():
    sys_prompt, _ = build('no_query')
    assert 'CHỦ ĐỘNG bỏ qua' in sys_prompt
    assert 'cần truy vấn dữ liệu' in sys_prompt
    for word in FAILURE_WORDS:
        assert word not in sys_prompt, f'nhánh no_query rò chữ báo sự cố: {word!r}'


@pytest.mark.parametrize('kind', ['failed', 'no_query'])
def test_bo_rule_dung_chung_giua_hai_nhanh(kind):
    sys_prompt, _ = build(kind)
    for rule in SHARED_RULES:
        assert rule in sys_prompt, f'nhánh {kind} thiếu rule dùng chung: {rule!r}'


def test_kind_la_roi_ve_nhanh_failed():
    """Giá trị lạ không được ném KeyError giữa lúc đang dựng prompt cho nhánh cứu hộ."""
    assert build('khong-ton-tai')[0] == build('failed')[0]


def test_user_prompt_dung_the_trung_lap_va_co_du_bon_khoi():
    _, user_prompt = build('no_query')
    assert '<no-data-reason>' in user_prompt
    assert '<failure>' not in user_prompt
    for token in ['LY-DO', 'BANG-A', 'LICH-SU', 'trong đó cái nào lớn nhất']:
        assert token in user_prompt
