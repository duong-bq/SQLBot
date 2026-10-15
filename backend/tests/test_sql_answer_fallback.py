"""Test lưới an toàn khi JSON đáp án sinh SQL lạc sang phần reasoning.

Ca gốc (ds159, 15/10/2026): prompt bảo Qwen tắt thinking suy luận trong ``<think>``, model mở thẻ
nhưng không bao giờ đóng, nên cả câu trả lời — kể cả JSON — rơi vào reasoning, content rỗng và pha
SQL báo "LLM returned empty content". Trên server thật, reasoning parser của vLLM bóc thẻ và đẩy
nội dung qua trường ``reasoning_content``; nếu server không bóc thì ``process_stream`` tự bóc thẻ
trong content. Test ở đây chạy qua ``process_stream`` thật cho cả hai đường.
"""

# Import sqlbot_xpack TRƯỚC: apps.chat.* nằm trong một vòng import lẫn nhau, chỉ gỡ được khi xpack
# là module khởi động vòng đó.
import sqlbot_xpack  # noqa: F401  isort:skip

import types

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk

from apps.chat.task import llm as llm_mod
from apps.chat.task.llm import LLMService, _pick_sql_answer

ANSWER = '{"success":true,"sql":"SELECT 1","tables":["t1"],"chart-type":"table"}'
DRAFT = '{"success":false,"message":"nháp"}'


class TestPickSqlAnswer:
    """Quy tắc chọn giữa content và reasoning."""

    def test_content_co_json_thi_giu_nguyen_content(self):
        content = 'Suy luận ngắn.\n' + ANSWER
        assert _pick_sql_answer(content, 'reasoning ' + DRAFT) == content

    def test_content_rong_thi_lay_json_trong_reasoning(self):
        assert _pick_sql_answer('', 'Cần bảng t1.\n\n\n' + ANSWER) == ANSWER

    def test_content_chi_co_van_xuoi_thi_van_lay_tu_reasoning(self):
        assert _pick_sql_answer('Xin lỗi.', 'suy luận ' + ANSWER) == ANSWER

    def test_reasoning_co_nhieu_json_thi_lay_khoi_cuoi_co_success(self):
        reasoning = DRAFT + ' rồi sửa lại ["t1"] ' + ANSWER
        assert _pick_sql_answer('', reasoning) == ANSWER

    def test_khong_co_o_dau_ca_thi_tra_content_de_bao_loi_cu(self):
        assert _pick_sql_answer('', 'chỉ có văn xuôi') == ''
        assert _pick_sql_answer('văn xuôi', '') == 'văn xuôi'


class FakeLLM:
    """LLM giả stream sẵn một danh sách mẩu content, hoặc mẩu reasoning qua trường riêng."""

    def __init__(self, pieces, native_reasoning=False):
        self.pieces = pieces
        self.native_reasoning = native_reasoning

    def stream(self, messages):
        if self.native_reasoning:
            return iter(AIMessageChunk(content='', additional_kwargs={'reasoning_content': p})
                        for p in self.pieces)
        return iter(AIMessageChunk(content=p) for p in self.pieces)


@pytest.fixture
def service(monkeypatch):
    """``LLMService`` chỉ đủ sống cho ``generate_sql`` ở đường không lỗi."""
    s = object.__new__(LLMService)
    s.record = types.SimpleNamespace(id=7)
    s.change_title = False
    s.current_logs = {}
    s.chat_question = types.SimpleNamespace(
        ai_modal_id=1, ai_modal_name='qwen',
        sql_user_question=lambda current_time, change_title: 'câu hỏi')
    s.sql_message = []
    s.saved = []

    monkeypatch.setattr(llm_mod, 'start_log', lambda **kw: None)
    monkeypatch.setattr(llm_mod, 'end_log', lambda **kw: None)

    def save_sql_answer(**kw):
        s.saved.append(kw['answer'])
        return s.record

    monkeypatch.setattr(llm_mod, 'save_sql_answer', save_sql_answer)
    return s


class TestGenerateSqlLichSu:
    """Lịch sử hội thoại nhận đáp án đã chọn, bản lưu record giữ nguyên content."""

    def test_the_think_khong_dong_thi_lich_su_van_co_json(self, service):
        service.llm = FakeLLM(['<think>', 'Cần bảng t1.\n\n\n', ANSWER])

        chunks = list(service.generate_sql(None))

        assert ''.join(c['content'] for c in chunks) == ''
        assert ANSWER in ''.join(c['reasoning_content'] for c in chunks)
        assert isinstance(service.sql_message[-1], AIMessage)
        assert service.sql_message[-1].content == ANSWER
        assert service.saved == ['{"content":""}']

    def test_vllm_boc_the_dua_json_qua_truong_reasoning(self, service):
        """Đường gặp thật trên server: thẻ đã bị vLLM bóc, content của mọi chunk rỗng."""
        service.llm = FakeLLM(['Cần bảng t1.\n\n\n', ANSWER], native_reasoning=True)

        chunks = list(service.generate_sql(None))

        assert ''.join(c['content'] for c in chunks) == ''
        assert service.sql_message[-1].content == ANSWER

    def test_van_xuoi_khong_the_thi_khong_doi_gi(self, service):
        text = 'Cần bảng t1, không JOIN.\n' + ANSWER
        service.llm = FakeLLM([text])

        chunks = list(service.generate_sql(None))

        assert ''.join(c['content'] for c in chunks) == text
        assert service.sql_message[-1].content == text
