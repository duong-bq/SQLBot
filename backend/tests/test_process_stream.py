"""Test cho ``process_stream`` — chỗ bóc phần suy luận của LLM ra khỏi câu trả lời chính thức.

Bất biến được canh ở đây: **mỗi mẩu suy luận chỉ đi qua dây đúng một lần**. Trước đây khối
``<think>`` được phát hai lần (một lần theo token, rồi một lần nguyên khối ngay tại thẻ đóng), nên
client nào cộng dồn ``reasoning_content`` cũng thấy nội dung nhân đôi. Lỗi này vô hình với backend
vì pipeline chỉ đọc ``content``, nên phải có test giữ, không trông vào việc đọc code.

Không dùng DB: ``process_stream`` là hàm thuần trên iterator chunk.
"""

# Import sqlbot_xpack TRƯỚC: apps.chat.curd.chat và apps.datasource.crud.datasource nằm trong một
# vòng import lẫn nhau, chỉ gỡ được khi xpack là module khởi động vòng đó.
import sqlbot_xpack  # noqa: F401  isort:skip

from apps.chat.task.llm import process_stream


class FakeChunk:
    """Bản tối giản của ``BaseMessageChunk``: chỉ ba thuộc tính mà ``process_stream`` đụng tới."""

    def __init__(self, content: str, reasoning: str = None):
        self.content = content
        self.additional_kwargs = {} if reasoning is None else {'reasoning_content': reasoning}
        self.usage_metadata = None


def run(chunks):
    """Chạy stream, trả về cặp (toàn bộ content, toàn bộ reasoning) đã cộng dồn."""
    out = list(process_stream(iter(chunks)))
    return (''.join(o['content'] for o in out),
            ''.join(o['reasoning_content'] for o in out))


def test_khoi_think_chi_phat_mot_lan():
    """Khối ``<think>`` chảy theo token: reasoning ra đúng một bản, content chỉ còn phần sau thẻ."""
    content, reasoning = run([FakeChunk('<think>'), FakeChunk('suy '), FakeChunk('luan'),
                              FakeChunk('</think>'), FakeChunk('{"success"'), FakeChunk(':true}')])
    assert reasoning == 'suy luan'
    assert content == '{"success":true}'


def test_the_dong_nam_chung_chunk_voi_noi_dung():
    """Thẻ đóng nằm giữa chunk: phần trước nó là suy luận, phần sau là câu trả lời."""
    content, reasoning = run([FakeChunk('<think>x1 '), FakeChunk('x2</think>tail1'), FakeChunk('tail2')])
    assert reasoning == 'x1 x2'
    assert content == 'tail1tail2'


def test_the_mo_bi_cat_doi_giua_hai_chunk():
    """Thẻ mở bị tokenizer cắt đôi vẫn phải nhận ra — nếu không, cả khối suy luận lọt vào content."""
    content, reasoning = run([FakeChunk('<thi'), FakeChunk('nk>y1'), FakeChunk('y2</think>z')])
    assert reasoning == 'y1y2'
    assert content == 'z'


def test_truong_reasoning_native_di_thang():
    """Model trả suy luận qua trường riêng của provider thì chuyển tiếp nguyên trạng, không bóc thẻ."""
    content, reasoning = run([FakeChunk('', reasoning='r1'), FakeChunk('json', reasoning='r2'),
                              FakeChunk('rest')])
    assert reasoning == 'r1r2'
    assert content == 'jsonrest'


def test_khong_co_the_thi_khong_dong_gi_toi_content():
    """Câu trả lời không có khối suy luận đi qua nguyên vẹn."""
    content, reasoning = run([FakeChunk('SELECT '), FakeChunk('1')])
    assert reasoning == ''
    assert content == 'SELECT 1'


def test_the_dong_bi_cat_doi_giua_hai_chunk():
    """Đối xứng với thẻ mở: ``</th`` + ``ink>`` phải khớp được.

    Không đệm thì thẻ đóng vĩnh viễn không khớp, toàn bộ câu trả lời chui vào ``reasoning_content``
    và ``content`` rỗng — pha sinh SQL sẽ báo "LLM returned empty content" rồi retry.
    """
    content, reasoning = run([FakeChunk('<think>y1'), FakeChunk('y2</th'), FakeChunk('ink>z')])
    assert reasoning == 'y1y2'
    assert content == 'z'


def test_the_dong_bi_cat_thanh_nhieu_manh():
    """Tokenizer cắt vụn ``</think>`` thành từng ký tự vẫn phải ghép lại được."""
    content, reasoning = run([FakeChunk('<think>a'), FakeChunk('</'), FakeChunk('thi'),
                              FakeChunk('nk'), FakeChunk('>'), FakeChunk('b')])
    assert reasoning == 'a'
    assert content == 'b'


def test_manh_giong_the_dong_nhung_khong_phai():
    """Giữ nhầm rồi thì phải trả lại: ``</the`` không thành thẻ nên vẫn là suy luận."""
    content, reasoning = run([FakeChunk('<think>a</the'), FakeChunk('ory</think>b')])
    assert reasoning == 'a</theory'
    assert content == 'b'


def test_stream_dut_khi_dang_giu_manh_nghi_la_the():
    """Stream hết mà mẩu đang giữ chưa thành thẻ thì phải xả ra, không được nuốt mất."""
    content, reasoning = run([FakeChunk('<think>a</th')])
    assert reasoning == 'a</th'
    assert content == ''
