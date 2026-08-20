"""Test cho pha gợi ý câu hỏi chạy ngay trong `POST /chat/question`.

Trọng tâm là hai thứ mà đọc code không đủ để yên tâm: câu hỏi cũ đưa vào prompt có thực sự bị
khoá theo từng user hay không (đây là ranh giới lộ dữ liệu, không phải chuyện chất lượng gợi ý),
và việc ghi kết quả có đúng là chỉ đụng tới `chat_record` chứ không lan sang bản ghi hội thoại.

Dùng SQLite trong RAM: hai hàm được test là SQL thuần, không phụ thuộc phương ngữ PostgreSQL. Phải
cấp `id` tường minh vì cột id khai báo `Identity(always=True)` — SQLite không sinh giúp.
"""

# Import sqlbot_xpack TRƯỚC: apps.chat.curd.chat và apps.datasource.crud.datasource nằm trong một
# vòng import lẫn nhau, chỉ gỡ được khi xpack là module khởi động vòng đó.
import sqlbot_xpack  # noqa: F401  isort:skip

import queue
from datetime import datetime, timedelta

import orjson
import pytest
from sqlmodel import Session, SQLModel, create_engine

from apps.chat.curd.chat import get_user_old_questions, save_record_recommend_questions
from apps.chat.models.chat_model import Chat, ChatRecord
from apps.chat.task.llm import LLMService

BASE_TIME = datetime(2026, 1, 1, 8, 0, 0)


@pytest.fixture
def session():
    """Session SQLite trong RAM, chỉ dựng hai bảng mà test này đụng tới."""
    engine = create_engine("sqlite://")
    Chat.__table__.create(engine)
    ChatRecord.__table__.create(engine)
    with Session(engine) as s:
        yield s


def add_record(session, rid, *, create_by, datasource=1, question='q', domain_code=None,
               error=None, minutes=0, chat_id=1):
    session.add(ChatRecord(id=rid, chat_id=chat_id, create_by=create_by, datasource=datasource,
                           question=question, domain_code=domain_code, error=error,
                           create_time=BASE_TIME + timedelta(minutes=minutes)))
    session.commit()


class TestGetUserOldQuestions:
    def test_khong_lay_cau_hoi_cua_user_khac(self, session):
        """Ranh giới chính: cùng datasource nhưng khác người tạo thì tuyệt đối không lọt."""
        add_record(session, 1, create_by=10, question='câu của tôi')
        add_record(session, 2, create_by=99, question='câu của người khác')
        assert get_user_old_questions(session, datasource=1, create_by=10) == ['câu của tôi']

    def test_khong_lay_cau_hoi_o_datasource_khac(self, session):
        add_record(session, 1, create_by=10, datasource=1, question='nguồn 1')
        add_record(session, 2, create_by=10, datasource=2, question='nguồn 2')
        assert get_user_old_questions(session, datasource=1, create_by=10) == ['nguồn 1']

    def test_co_domain_code_thi_siet_dung_linh_vuc(self, session):
        add_record(session, 1, create_by=10, question='thu ngân sách', domain_code='THU')
        add_record(session, 2, create_by=10, question='chi ngân sách', domain_code='CHI')
        add_record(session, 3, create_by=10, question='không lĩnh vực', domain_code=None)
        assert get_user_old_questions(session, 1, 10, domain_code='THU') == ['thu ngân sách']

    def test_khong_co_domain_code_thi_lay_moi_linh_vuc(self, session):
        """Quyết định thiết kế: không nêu lĩnh vực thì gom câu hỏi cũ ở mọi lĩnh vực của chính user.

        An toàn vì mọi dòng lấy ra đều do user này tạo, tức đã nằm trong quyền của họ lúc hỏi.
        """
        add_record(session, 1, create_by=10, question='thu', domain_code='THU', minutes=0)
        add_record(session, 2, create_by=10, question='chi', domain_code='CHI', minutes=1)
        assert set(get_user_old_questions(session, 1, 10)) == {'thu', 'chi'}

    def test_bo_qua_luot_hong_va_cau_hoi_rong(self, session):
        add_record(session, 1, create_by=10, question='ok')
        add_record(session, 2, create_by=10, question='hỏng', error='boom')
        add_record(session, 3, create_by=10, question=None)
        assert get_user_old_questions(session, 1, 10) == ['ok']

    def test_moi_nhat_truoc_va_ton_trong_limit(self, session):
        for i in range(5):
            add_record(session, i + 1, create_by=10, question=f'q{i}', minutes=i)
        assert get_user_old_questions(session, 1, 10, limit=2) == ['q4', 'q3']

    @pytest.mark.parametrize('datasource,create_by', [(None, 10), (1, None), (0, 10)])
    def test_thieu_dinh_danh_thi_tra_rong(self, session, datasource, create_by):
        add_record(session, 1, create_by=10, question='q')
        assert get_user_old_questions(session, datasource, create_by) == []


class TestSaveRecordRecommendQuestions:
    def _chat_and_record(self, session):
        session.add(Chat(id=1, create_by=10, engine_type='pg', recommended_generate=False))
        add_record(session, 1, create_by=10)
        session.commit()

    def test_ghi_vao_record_va_khong_dung_toi_hoi_thoai(self, session):
        """Khác `save_recommend_question_answer`: không có nhánh ghi đè gợi ý cấp hội thoại."""
        self._chat_and_record(session)
        saved = save_record_recommend_questions(
            session, 1, {'content': '["câu 1", "câu 2"]'})
        assert orjson.loads(saved) == ['câu 1', 'câu 2']

        session.expire_all()
        assert orjson.loads(session.get(ChatRecord, 1).recommended_question) == ['câu 1', 'câu 2']
        chat = session.get(Chat, 1)
        assert not chat.recommended_generate
        assert chat.recommended_question is None

    def test_json_hong_thi_luu_mang_rong_chu_khong_nem(self, session):
        """Gợi ý là pha phụ: LLM trả rác cũng không được làm gãy lượt hỏi đã có câu trả lời."""
        self._chat_and_record(session)
        assert save_record_recommend_questions(session, 1, {'content': 'xin lỗi, tôi không thể'}) == '[]'

    def test_content_rong_thi_luu_mang_rong(self, session):
        self._chat_and_record(session)
        assert save_record_recommend_questions(session, 1, {'content': ''}) == '[]'

    def test_thieu_record_id_thi_nem(self, session):
        with pytest.raises(Exception):
            save_record_recommend_questions(session, None, {'content': '[]'})


class TestDrainRecommendQueue:
    """Hợp đồng SSE của pha gợi ý. `self` không được dùng trong thân hàm nên gọi kiểu unbound."""

    def _drain(self, q, state=None, block=True):
        return list(LLMService.drain_recommend_queue(None, q, state if state is not None else {}, block))

    def test_thanh_cong_phat_info_roi_recommended_question(self):
        q = queue.Queue()
        q.put(('result', '["a", "b"]'))
        q.put(('done', None))
        events = [orjson.loads(e[len('data:'):]) for e in self._drain(q)]
        assert events[0] == {'type': 'info', 'msg': 'recommended question generated'}
        assert events[1] == {'type': 'recommended_question', 'content': '["a", "b"]'}

    def test_hong_thi_chi_bao_info_khong_phat_error(self):
        q = queue.Queue()
        q.put(('error', RuntimeError('LLM chết')))
        q.put(('done', None))
        events = [orjson.loads(e[len('data:'):]) for e in self._drain(q)]
        assert events == [{'type': 'info', 'msg': 'recommended question failed'}]

    def test_queue_none_thi_khong_phat_gi(self):
        assert self._drain(None) == []

    def test_da_vet_xong_thi_khong_phat_lai(self):
        q = queue.Queue()
        q.put(('result', '[]'))
        q.put(('done', None))
        state = {}
        self._drain(q, state)
        assert state['done'] is True
        assert self._drain(q, state) == []
