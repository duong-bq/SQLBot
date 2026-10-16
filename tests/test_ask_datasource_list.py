"""Test cho ``POST /chat/ask`` nhận danh sách datasource và hai cờ ``include_answer`` / ``include_chart``.

Ba tầng, mỗi tầng một nhóm test:

- ``resolve_chat_for_ask`` / ``validate_ask_datasource_ids`` / ``resolve_chat_id``: kiểm danh sách
  và tạo hội thoại chưa gắn datasource;
- ``LLMService.select_datasource``: chỉ chọn trong danh sách, kể cả khi LLM trả id lạ;
- ``LLMService.run_task`` nhánh JSON đồng bộ: bốn tổ hợp cờ, answer/chart hỏng cho ``null``, không
  chuỗi SSE nào lọt vào response, MCP không bị đổi hành vi.

Dựng ``LLMService`` bằng ``object.__new__`` như ``test_sql_phase.py``: stub trả về đúng hình dạng dữ
liệu thật để test bắt được lỗi hình dạng.
"""

# Import sqlbot_xpack TRƯỚC: apps.chat.* nằm trong một vòng import lẫn nhau.
import sqlbot_xpack  # noqa: F401  isort:skip

import queue
import types

import orjson
import pytest
from fastapi import FastAPI, HTTPException

from apps.chat.api import chat as chat_api
from apps.chat.curd import chat as chat_curd
from apps.chat.models.chat_model import Chat, ChatAskQuestion, ChatFinishStep, ChatQuestion
from apps.chat.task import llm as llm_mod
from apps.chat.task.llm import LLMService, SqlPhaseResult
from apps.datasource.models.datasource import CoreDatasource
from common.error import SingleMessageError


def make_ds(ds_id, oid=1, status='Success', name=None):
    """Một ``CoreDatasource`` đủ dùng cho test (không chạm DB)."""
    return CoreDatasource(id=ds_id, name=name or f'ds{ds_id}', description=f'mô tả {ds_id}',
                          type='mysql', type_name='MySQL', oid=oid, status=status)


class FakeSession:
    """Session giả: ``get`` theo id, ``query(Chat)`` theo ``external_id``, ghi nhận ``add``."""

    def __init__(self, datasources=(), chats=()):
        self.datasources = {d.id: d for d in datasources}
        self.chats = {c.id: c for c in chats}
        self.added = []

    def get(self, model, key):
        if model is CoreDatasource:
            return self.datasources.get(key)
        if model is Chat:
            return self.chats.get(key)
        return None

    def query(self, model):
        outer = self

        class Q:
            def filter(self, *a, **k):
                return self

            def first(self):
                return None

        return Q()

    def add(self, obj):
        obj.id = 100
        self.added.append(obj)

    def commit(self):
        pass

    def refresh(self, obj):
        pass


USER = types.SimpleNamespace(id=1, oid=1)


class TestValidateAskDatasourceIds:
    def test_bo_trung_giu_thu_tu(self):
        session = FakeSession([make_ds(1), make_ds(2)])
        assert chat_curd.validate_ask_datasource_ids(session, USER, [2, 1, 2]) == [2, 1]

    def test_id_khong_ton_tai(self):
        with pytest.raises(ValueError, match='9'):
            chat_curd.validate_ask_datasource_ids(FakeSession([make_ds(1)]), USER, [1, 9])

    def test_khac_workspace(self):
        session = FakeSession([make_ds(1), make_ds(2, oid=2)])
        with pytest.raises(ValueError, match='workspace'):
            chat_curd.validate_ask_datasource_ids(session, USER, [1, 2])

    def test_chua_dung_duoc(self):
        session = FakeSession([make_ds(1, status='Failed')])
        with pytest.raises(ValueError, match='not ready'):
            chat_curd.validate_ask_datasource_ids(session, USER, [1])


class TestResolveChatId:
    def test_tao_hoi_thoai_khong_gan_datasource(self):
        session = FakeSession()
        chat_id = chat_curd.resolve_chat_id(session, USER, 'uuid-1', None, allow_no_datasource=True)
        assert chat_id == 100
        chat = session.added[0]
        assert chat.datasource is None and chat.engine_type == '' and chat.external_id == 'uuid-1'

    def test_van_doi_datasource_khi_khong_cho_phep_bo_trong(self):
        with pytest.raises(Exception, match="Provide 'datasource'"):
            chat_curd.resolve_chat_id(FakeSession(), USER, 'uuid-1', None)

    def test_duong_cu_gan_datasource(self):
        session = FakeSession([make_ds(5)])
        chat_curd.resolve_chat_id(session, USER, 'uuid-1', 5)
        assert session.added[0].datasource == 5 and session.added[0].engine_type == 'MySQL'


class TestResolveChatForAsk:
    @pytest.fixture
    def captured(self, monkeypatch):
        got = {}

        def fake_resolve(session, current_user, chat_id, datasource_id=None, allow_no_datasource=False):
            got.update(datasource_id=datasource_id, allow=allow_no_datasource)
            return 42

        monkeypatch.setattr(chat_api, 'resolve_chat_id', fake_resolve)
        return got

    def run(self, session, **body):
        import asyncio
        question = ChatAskQuestion(question='q', chat_id='uuid-1', **body)
        return asyncio.run(chat_api.resolve_chat_for_ask(session, USER, question))

    def test_khong_co_danh_sach_di_duong_cu(self, captured):
        assert self.run(FakeSession(), datasource=3) == 42
        assert captured == {'datasource_id': 3, 'allow': False}

    def test_datasource_va_danh_sach_cung_co_thi_400(self, captured):
        with pytest.raises(HTTPException) as e:
            self.run(FakeSession([make_ds(1)]), datasource=1, datasource_ids=[1])
        assert e.value.status_code == 400 and e.value.detail['code'] == 'invalid_datasource_ids'

    def test_danh_sach_rong_thi_400(self, captured):
        with pytest.raises(HTTPException) as e:
            self.run(FakeSession(), datasource_ids=[])
        assert e.value.status_code == 400

    def test_id_khong_hop_le_thi_400_nem_ro_id(self, captured):
        with pytest.raises(HTTPException) as e:
            self.run(FakeSession([make_ds(1)]), datasource_ids=[1, 9])
        assert e.value.status_code == 400 and '9' in e.value.detail['message']

    def test_mot_phan_tu_tuong_duong_datasource(self, captured):
        self.run(FakeSession([make_ds(1)]), datasource_ids=[1, 1])
        assert captured == {'datasource_id': 1, 'allow': True}

    def test_nhieu_phan_tu_tao_hoi_thoai_chua_gan(self, captured):
        self.run(FakeSession([make_ds(1), make_ds(2)]), datasource_ids=[1, 2])
        assert captured == {'datasource_id': None, 'allow': True}

    def test_hoi_thoai_co_san_datasource_ngoai_danh_sach_thi_400(self, captured):
        chat = Chat(id=42, oid=1, datasource=7)
        with pytest.raises(HTTPException) as e:
            self.run(FakeSession([make_ds(1), make_ds(2)], chats=[chat]), datasource_ids=[1, 2])
        assert e.value.status_code == 400

    def test_hoi_thoai_co_san_datasource_trong_danh_sach_thi_qua(self, captured):
        chat = Chat(id=42, oid=1, datasource=2)
        assert self.run(FakeSession([make_ds(1), make_ds(2)], chats=[chat]), datasource_ids=[1, 2]) == 42

    def test_hoi_thoai_workspace_khac_khong_bi_ho_thong_tin(self, captured):
        """Hội thoại workspace khác để ``require_permissions`` từ chối, không phải thông điệp ở đây."""
        chat = Chat(id=42, oid=2, datasource=7)
        assert self.run(FakeSession([make_ds(1), make_ds(2)], chats=[chat]), datasource_ids=[1, 2]) == 42


class TestHopDongRoute:
    def test_body_ask_la_mot_khoi_chua_danh_sach_va_co(self):
        """Dependency và route dùng chung một body: không bị FastAPI tách thành hai tham số body."""
        app = FastAPI()
        app.include_router(chat_api.router)
        schema = app.openapi()
        body = schema['paths']['/chat/ask']['post']['requestBody']['content']['application/json']['schema']
        assert body['$ref'].endswith('/ChatAskQuestion')
        props = schema['components']['schemas']['ChatAskQuestion']['properties']
        assert {'datasource_ids', 'include_answer', 'include_chart'} <= set(props)

    def test_question_khong_nhan_truong_moi(self):
        """``/chat/question`` giữ body cũ."""
        app = FastAPI()
        app.include_router(chat_api.router)
        props = app.openapi()['components']['schemas']['ChatQuestionBase']['properties']
        assert not {'datasource_ids', 'include_answer', 'include_chart'} & set(props)


# --- select_datasource ----------------------------------------------------------------------


class FakeSelectSession:
    """Session cho ``select_datasource``: trả danh sách ứng viên và các đối tượng được ``get``."""

    def __init__(self, rows, chat):
        self.rows = rows
        self.chat = chat
        self.datasources = {r.id: make_ds(r.id) for r in rows}

    def exec(self, stmt):
        return list(self.rows)

    def get(self, model, key):
        return self.chat if model is Chat else self.datasources.get(key)

    def begin_nested(self):
        class Ctx:
            def __enter__(self_):
                return self_

            def __exit__(self_, *a):
                return False

        return Ctx()

    def add(self, obj):
        pass

    def flush(self):
        pass

    def refresh(self, obj):
        pass

    def commit(self):
        pass


@pytest.fixture
def selector(monkeypatch):
    """``LLMService`` chỉ đủ sống cho ``select_datasource``; ``llm_answer['text']`` là câu LLM trả."""
    s = object.__new__(LLMService)
    s.oid = 1
    s.current_assistant = None
    s.out_ds_instance = None
    s.current_logs = {}
    s.ds = None
    s.record = types.SimpleNamespace(id=7, chat_id=3)
    s.chat_question = types.SimpleNamespace(
        question='doanh thu', ai_modal_id=1, ai_modal_name='m', datasource_ids=None,
        datasource_sys_question=lambda: 'sys',
        datasource_user_question=lambda lst: lst,
    )
    s.filter_terminology_template = lambda *a, **k: None
    s.filter_training_template = lambda *a, **k: None
    s.filter_custom_prompts = lambda *a, **k: None
    s.init_messages = lambda *a, **k: None

    state = types.SimpleNamespace(text='{"id": 2}', llm_calls=0, shown=None)

    def fake_stream(msgs):
        state.llm_calls += 1
        state.shown = msgs[-1].content
        return iter([{'content': state.text}])

    s.llm = types.SimpleNamespace(stream=fake_stream)
    s.state = state

    monkeypatch.setattr(llm_mod, 'process_stream', lambda res, usage: res)
    monkeypatch.setattr(llm_mod, 'start_log', lambda **kw: None)
    monkeypatch.setattr(llm_mod, 'end_log', lambda **kw: None)
    monkeypatch.setattr(llm_mod, 'get_version', lambda ds: '')
    monkeypatch.setattr(llm_mod, 'save_select_datasource_answer', lambda **kw: None)
    monkeypatch.setattr(llm_mod.settings, 'TABLE_EMBEDDING_ENABLED', False)
    return s


def rows(*ids):
    return [types.SimpleNamespace(id=i, name=f'ds{i}', description=f'mô tả {i}') for i in ids]


def run_select(service, ids_in_workspace):
    chat = Chat(id=3, oid=1)
    session = FakeSelectSession(rows(*ids_in_workspace), chat)
    for _ in service.select_datasource(session):
        pass
    return chat


class TestSelectDatasourceTheoDanhSach:
    def test_khong_co_danh_sach_thay_ca_workspace(self, selector):
        selector.state.text = '{"id": 3}'
        chat = run_select(selector, [1, 2, 3])
        assert chat.datasource == 3
        shown = orjson.loads(selector.state.shown)
        assert [d['id'] for d in shown] == [1, 2, 3]

    def test_chi_thay_va_chon_trong_danh_sach(self, selector):
        selector.chat_question.datasource_ids = [2, 3]
        selector.state.text = '{"id": 3}'
        chat = run_select(selector, [1, 2, 3])
        assert chat.datasource == 3
        assert [d['id'] for d in orjson.loads(selector.state.shown)] == [2, 3]

    def test_mot_ung_vien_khong_goi_llm(self, selector):
        selector.chat_question.datasource_ids = [2]
        chat = run_select(selector, [1, 2, 3])
        assert chat.datasource == 2
        assert selector.state.llm_calls == 0

    def test_llm_tra_id_ngoai_danh_sach_la_loi(self, selector):
        selector.chat_question.datasource_ids = [2, 3]
        selector.state.text = '{"id": 1}'
        chat = Chat(id=3, oid=1)
        session = FakeSelectSession(rows(1, 2, 3), chat)
        with pytest.raises(SingleMessageError, match='not in the given datasource list'):
            for _ in selector.select_datasource(session):
                pass
        assert chat.datasource is None

    def test_llm_noi_khong_co_cai_nao_phu_hop_van_la_loi_fail(self, selector):
        selector.chat_question.datasource_ids = [2, 3]
        selector.state.text = '{"id": 0, "fail": "không có datasource phù hợp"}'
        with pytest.raises(SingleMessageError, match='không có datasource phù hợp'):
            run_select(selector, [1, 2, 3])

    def test_giao_rong_la_loi(self, selector):
        selector.chat_question.datasource_ids = [8, 9]
        with pytest.raises(SingleMessageError, match='No available datasource'):
            run_select(selector, [1, 2, 3])
        assert selector.state.llm_calls == 0


# --- run_task: hai cờ -----------------------------------------------------------------------

RESULT = {'fields': ['a'], 'data': [{'a': 1}]}


class SyncExecutor:
    """Chạy tác vụ ngay trong luồng gọi: đủ cho test vì answer chỉ cần đã nằm trong queue khi vét."""

    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)


@pytest.fixture
def ask_service(monkeypatch):
    """``LLMService`` chạy ``run_task`` nhánh ``in_chat=False, stream=False`` với pha SQL giả."""
    s = object.__new__(LLMService)
    s.ds = types.SimpleNamespace(id=5, type='mysql', name='bán hàng', oid=1)
    s.record = types.SimpleNamespace(id=7, chat_id=3)
    s.current_user = types.SimpleNamespace(id=1)
    s.current_assistant = None
    s.sw_permission = None
    s.out_ds_instance = None
    s.current_logs = {}
    s.chat_question = types.SimpleNamespace(
        question='q', datasource_ids=None, include_answer=False, include_chart=False,
        extended_response=True, db_schema=None)
    s.filter_terminology_template = lambda *a, **k: None
    s.filter_training_template = lambda *a, **k: None
    s.filter_custom_prompts = lambda *a, **k: None
    s.init_messages = lambda *a, **k: None
    s.validate_history_ds = lambda *a, **k: None
    s.get_record = lambda: types.SimpleNamespace(id=7, chat_id=3, question='q', regenerate_record_id=None)
    s.finish = lambda *a, **k: None
    s.trans = lambda key: key

    s.build_answer_prompts = lambda *a: ('sys', 'user')
    s.answer_behavior = 'ok'

    def answer_worker(sys_prompt, user_prompt, out_queue):
        if s.answer_behavior == 'ok':
            out_queue.put(('chunk', {'content': ' Doanh thu là 1. ', 'reasoning_content': None}))
        else:
            out_queue.put(('error', RuntimeError('answer hỏng')))
        out_queue.put(('done', None))

    s.run_answer_worker = answer_worker

    s.chart_behavior = 'ok'

    def generate_chart(_session, chart_type, schema):
        if s.chart_behavior == 'ok':
            yield {'content': '{"type":"bar"}', 'reasoning_content': ''}
        else:
            raise RuntimeError('chart hỏng')

    s.generate_chart = generate_chart
    s.check_save_chart = lambda session, res: {'type': 'bar'}

    def fake_phase(_session, json_result, in_chat, stream, finish_step):
        json_result['sql'] = 'SELECT 1'
        json_result['data'] = {'fields': ['a'], 'data': [{'a': 1}]}
        return SqlPhaseResult(result=dict(RESULT), data=[{'a': 1}], tables=['t1'], chart_type='bar')
        yield  # noqa: unreachable — giữ hàm là generator

    s._sql_phase = fake_phase

    class FakeSessionMaker:
        def __call__(self):
            return None

        def remove(self):
            return None

    def no_picture(*a, **k):
        raise AssertionError('/ask không được render ảnh')

    monkeypatch.setattr(llm_mod, 'check_connection', lambda ds, trans=None: True)
    monkeypatch.setattr(llm_mod, 'session_maker', FakeSessionMaker())
    monkeypatch.setattr(llm_mod, 'executor', SyncExecutor())
    monkeypatch.setattr(llm_mod, 'get_table_schema', lambda **kw: ('schema', ['t1']))
    monkeypatch.setattr(llm_mod, 'request_picture', no_picture)
    return s


def ask(service, answer=False, chart=False):
    """Chạy ``run_task`` như ``/chat/ask`` và trả ``(mọi chunk, response cuối)`` như ``stream_sql``."""
    service.chat_question.include_answer = answer
    service.chat_question.include_chart = chart
    chunks = list(service.run_task(in_chat=False, stream=False, finish_step=ChatFinishStep.QUERY_DATA))
    final = {}
    for chunk in chunks:
        if chunk:
            final = chunk
    return chunks, final


class TestRunTaskHaiCo:
    def test_khong_co_thi_chi_co_du_lieu_va_datasource(self, ask_service):
        chunks, final = ask(ask_service)
        assert final['success'] is True and final['sql'] == 'SELECT 1'
        assert final['datasource'] == {'id': 5, 'name': 'bán hàng'}
        assert 'answer' not in final and 'chart' not in final

    def test_request_kieu_cu_khong_them_khoa_datasource(self, ask_service):
        ask_service.chat_question.extended_response = False
        _, final = ask(ask_service)
        assert 'datasource' not in final

    def test_chi_answer(self, ask_service):
        chunks, final = ask(ask_service, answer=True)
        assert final['answer'] == 'Doanh thu là 1.'
        assert 'chart' not in final

    def test_chi_chart(self, ask_service):
        _, final = ask(ask_service, chart=True)
        assert final['chart'] == {'type': 'bar'}
        assert 'answer' not in final

    def test_ca_hai(self, ask_service):
        _, final = ask(ask_service, answer=True, chart=True)
        assert final['answer'] == 'Doanh thu là 1.'
        assert final['chart'] == {'type': 'bar'}
        assert final['sql'] == 'SELECT 1'

    @pytest.mark.parametrize('answer,chart', [(False, False), (True, False), (False, True), (True, True)])
    def test_khong_chuoi_sse_lot_vao_response(self, ask_service, answer, chart):
        chunks, _ = ask(ask_service, answer=answer, chart=chart)
        assert chunks and all(isinstance(c, dict) for c in chunks)

    def test_chart_hong_thi_null_con_lai_van_thanh_cong(self, ask_service):
        ask_service.chart_behavior = 'fail'
        _, final = ask(ask_service, answer=True, chart=True)
        assert final['success'] is True
        assert final['chart'] is None
        assert final['answer'] == 'Doanh thu là 1.'
        assert final['sql'] == 'SELECT 1'

    def test_answer_hong_thi_null_con_lai_van_thanh_cong(self, ask_service):
        ask_service.answer_behavior = 'fail'
        _, final = ask(ask_service, answer=True, chart=True)
        assert final['success'] is True
        assert final['answer'] is None
        assert final['chart'] == {'type': 'bar'}

    def test_chi_answer_hong(self, ask_service):
        ask_service.answer_behavior = 'fail'
        _, final = ask(ask_service, answer=True)
        assert final['success'] is True and final['answer'] is None

    def test_du_lieu_hong_thi_ca_loi_goi_loi(self, ask_service):
        def bad_phase(_session, json_result, in_chat, stream, finish_step):
            raise SingleMessageError('SQL hỏng')
            yield  # noqa: unreachable

        ask_service._sql_phase = bad_phase
        ask_service.save_error = lambda **kw: None
        _, final = ask(ask_service, answer=True, chart=True)
        assert final['success'] is False and 'SQL hỏng' in final['message']
        assert 'answer' not in final and 'chart' not in final


class TestMcpKhongDoi:
    """MCP không-stream cũng là ``in_chat=False, stream=False`` nhưng không bao giờ đặt cờ."""

    def test_mcp_van_nem_loi_khi_chart_hong(self, ask_service):
        ask_service.chart_behavior = 'fail'
        ask_service.chat_question.extended_response = False
        ask_service.save_error = lambda **kw: None
        chunks = list(ask_service.run_task(in_chat=False, stream=False,
                                           finish_step=ChatFinishStep.GENERATE_CHART))
        final = chunks[-1]
        assert final['success'] is False and 'chart hỏng' in final['message']

    def test_mcp_ket_qua_khong_co_answer_hay_datasource(self, ask_service, monkeypatch):
        ask_service.chat_question.extended_response = False
        monkeypatch.setattr(llm_mod, 'request_picture', lambda *a, **k: ('http://img', None))
        monkeypatch.setattr(llm_mod, 'start_log', lambda **kw: None)
        monkeypatch.setattr(llm_mod, 'end_log', lambda **kw: None)
        chunks = list(ask_service.run_task(in_chat=False, stream=False,
                                           finish_step=ChatFinishStep.GENERATE_CHART))
        final = chunks[-1]
        assert final['success'] is True and final['chart'] == {'type': 'bar'}
        assert final['image_url'] == 'http://img'
        assert 'answer' not in final and 'datasource' not in final


class TestChatQuestionMoRong:
    def test_extended_response(self):
        assert ChatQuestion(chat_id=1, question='q').extended_response is False
        assert ChatQuestion(chat_id=1, question='q', datasource_ids=[1]).extended_response is True
        assert ChatQuestion(chat_id=1, question='q', include_answer=True).extended_response is True
        assert ChatQuestion(chat_id=1, question='q', include_chart=True).extended_response is True
