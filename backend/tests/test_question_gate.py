"""Test cho cổng định tuyến câu hỏi.

Trọng tâm KHÔNG phải chất lượng phân loại — thứ đó chỉ đo được bằng lưu lượng thật ở chế độ chỉ
đo — mà là **hợp đồng an toàn** của cổng, đúng những thứ mà một lần sửa sau có thể phá mà nhìn
diff không thấy:

- mọi đường hỏng đều rơi về ``'sql'``, tức hành vi y như trước khi có cổng;
- cổng không bao giờ ném exception ra ngoài, kể cả khi LLM nổ hay khi ghi log nổ;
- ``category``/``reason`` vẫn được giữ nguyên khi ``action`` bị ép, vì đó mới là số đo;
- chế độ chỉ đo ép được ``'answer'`` về ``'sql'`` mà không làm mất thông tin phân loại.

``LLMService`` được giả bằng một namespace phẳng thay vì ``unittest.mock``: cổng chỉ đụng vào bốn
thuộc tính, mà mock tự sinh thuộc tính nên sẽ nuốt mất lỗi gõ nhầm tên.
"""

# Import sqlbot_xpack TRƯỚC: apps.chat.* nằm trong một vòng import lẫn nhau, chỉ gỡ được khi xpack
# là module khởi động vòng đó.
import sqlbot_xpack  # noqa: F401  isort:skip

import types

import pytest

from apps.chat.models.chat_model import ChatQuestion
from apps.chat.task import llm as llm_mod
from apps.chat.task import question_gate as gate_mod
from apps.chat.task.question_gate import (
    ACTION_ANSWER,
    ACTION_SQL,
    RouteDecision,
    decide_route,
    parse_route_answer,
)


# --- phần thuần: đọc câu trả lời của LLM ---------------------------------------------------

@pytest.mark.parametrize('text, action, category, forced', [
    ('{"action":"answer","category":"backref","reason":"da co o luot truoc"}',
     ACTION_ANSWER, 'backref', ''),
    ('{"action":"sql","category":"need_data","reason":"can so lieu moi"}',
     ACTION_SQL, 'need_data', ''),
    ('{"action":"answer","category":"chitchat","reason":"chao hoi"}',
     ACTION_ANSWER, 'chitchat', ''),
    # action và category mâu thuẫn: LLM đang lưỡng lự nên nghiêng về phía an toàn
    ('{"action":"answer","category":"need_data","reason":"x"}', ACTION_SQL, 'need_data', 'mismatch'),
    ('{"action":"sql","category":"chitchat","reason":"x"}', ACTION_SQL, 'chitchat', 'mismatch'),
    # category lạ
    ('{"action":"answer","category":"khong_ton_tai"}', ACTION_SQL, 'need_data', 'mismatch'),
    # action lạ / không có JSON / rỗng
    ('{"action":"maybe","category":"backref"}', ACTION_SQL, 'need_data', 'unparsed'),
    ('xin chao, toi khong tra ve JSON', ACTION_SQL, 'need_data', 'unparsed'),
    ('', ACTION_SQL, 'need_data', 'unparsed'),
])
def test_parse_moi_duong_hong_deu_ve_sql(text, action, category, forced):
    d = parse_route_answer(text)
    assert (d.action, d.category, d.forced) == (action, category, forced)


def test_parse_bo_qua_json_nhap_trong_khoi_think():
    """Model không tắt được thinking thì phần suy luận rơi vào content kèm JSON nháp.

    Lọc theo khoá `action` chứ không theo vị trí, nếu không sẽ bốc nhầm object nháp đứng trước.
    """
    text = ('<think>can nhac: {"tables":["a","b"]} co ve la hoi ve du lieu</think>\n'
            '{"action":"answer","category":"meta","reason":"hoi ve datasource"}')
    d = parse_route_answer(text)
    assert (d.action, d.category, d.forced) == (ACTION_ANSWER, 'meta', '')


def test_skip_sql_chi_dung_khi_action_la_answer():
    assert RouteDecision(ACTION_ANSWER, 'backref').skip_sql is True
    assert RouteDecision(ACTION_SQL, 'need_data').skip_sql is False


# --- decide_route: vòng đời đầy đủ ---------------------------------------------------------

@pytest.fixture
def service():
    """``LLMService`` giả, chỉ đủ những gì cổng đụng tới."""
    return types.SimpleNamespace(
        chat_question=ChatQuestion(chat_id=1, question='trong do cai nao lon nhat'),
        table_name_list=['bang_a', 'bang_b'],
        record=types.SimpleNamespace(id=7, chat_id=1),
        route_config=types.SimpleNamespace(model_id=3, model_name='fake-mini'),
        route_llm=types.SimpleNamespace(stream=lambda msg: iter(())),
    )


@pytest.fixture
def stub_io(monkeypatch):
    """Chặn mọi lối ra ngoài của cổng (DB, LLM) và ghi lại những gì nó định làm."""
    calls = {'start': 0, 'end': 0, 'error': 0}
    monkeypatch.setattr(gate_mod, 'get_recent_qa_history', lambda **kw: 'LICH-SU')
    monkeypatch.setattr(gate_mod, 'start_log',
                        lambda **kw: calls.__setitem__('start', calls['start'] + 1) or 'LOG')
    monkeypatch.setattr(gate_mod, 'end_log',
                        lambda **kw: calls.__setitem__('end', calls['end'] + 1))
    monkeypatch.setattr(gate_mod, 'trigger_log_error',
                        lambda **kw: calls.__setitem__('error', calls['error'] + 1))
    monkeypatch.setattr(llm_mod, 'process_stream', lambda res, usage=None, **kw: res)
    monkeypatch.setattr(gate_mod.settings, 'LLM_ROUTE_ENABLED', True)
    monkeypatch.setattr(gate_mod.settings, 'LLM_ROUTE_SHADOW', False)
    return calls


def answer_with(service, text):
    """Cho LLM giả trả về `text` theo đúng hình dạng chunk mà process_stream sinh ra."""
    service.route_llm.stream = lambda msg: iter([{'content': text, 'reasoning_content': ''}])


def test_tat_co_thi_khong_cham_toi_db_lan_llm(service, stub_io, monkeypatch):
    """Cờ tắt phải chặn từ dòng đầu: không gọi LLM, không sinh thêm bản ghi chat_log nào."""
    monkeypatch.setattr(gate_mod.settings, 'LLM_ROUTE_ENABLED', False)
    service.route_llm.stream = lambda msg: pytest.fail('cờ tắt mà vẫn gọi LLM')
    d = decide_route(service, None)
    assert (d.action, d.forced) == (ACTION_SQL, 'disabled')
    assert stub_io['start'] == 0


def test_duong_thanh_cong_co_dong_log(service, stub_io):
    answer_with(service, '{"action":"answer","category":"backref","reason":"da co"}')
    d = decide_route(service, None)
    assert (d.action, d.category, d.reason) == (ACTION_ANSWER, 'backref', 'da co')
    assert (stub_io['start'], stub_io['end'], stub_io['error']) == (1, 1, 0)


def test_che_do_chi_do_ep_ve_sql_nhung_giu_nguyen_phan_loai(service, stub_io, monkeypatch):
    """Chế độ chỉ đo phải đổi ĐÚNG `action`. Mất `category` là mất luôn lý do nó tồn tại."""
    monkeypatch.setattr(gate_mod.settings, 'LLM_ROUTE_SHADOW', True)
    answer_with(service, '{"action":"answer","category":"chitchat","reason":"chao hoi"}')
    d = decide_route(service, None)
    assert (d.action, d.forced) == (ACTION_SQL, 'shadow')
    assert (d.category, d.reason) == ('chitchat', 'chao hoi')


def test_che_do_chi_do_khong_dong_vao_quyet_dinh_sql(service, stub_io, monkeypatch):
    monkeypatch.setattr(gate_mod.settings, 'LLM_ROUTE_SHADOW', True)
    answer_with(service, '{"action":"sql","category":"need_data","reason":"can so lieu"}')
    assert decide_route(service, None).forced == ''


def test_llm_no_thi_hong_mo_va_danh_dau_log_loi(service, stub_io):
    def boom(msg):
        raise RuntimeError('model 500')

    service.route_llm.stream = boom
    d = decide_route(service, None)
    assert (d.action, d.forced) == (ACTION_SQL, 'error')
    assert stub_io['error'] == 1


def test_ghi_log_no_cung_khong_giet_luot_hoi(service, stub_io, monkeypatch):
    """Cổng chèn vào giữa một đường đang chạy tốt, nên hỏng ở đây phải im lặng thành hành vi cũ."""
    def boom(**kw):
        raise RuntimeError('db down')

    monkeypatch.setattr(gate_mod, 'start_log', boom)
    monkeypatch.setattr(gate_mod, 'trigger_log_error', boom)
    d = decide_route(service, None)
    assert (d.action, d.forced) == (ACTION_SQL, 'error')
