"""Test cho bước explore và verify của pha SQL (plan 2026-11-16-explore-verify-sql.md).

Ba nhóm:

- phần thuần của ``sql_review``: đọc JSON, ép LIMIT và chặn cột ngoài quyền cho probe, hồ sơ cột;
- đường chạy probe trong ``LLMService``: đi đúng đường kiểm, áp quyền và thực thi của SQL chính;
- cách verify gắn vào ``_sql_phase``: số liệu chỉ phát sau verify, ``revise`` đi đường retry,
  hết vòng sửa thì kết quả đi tiếp, lần sửa hỏng thì quay về kết quả đã chạy được.

Dựng ``LLMService`` bằng ``object.__new__`` như ``test_sql_phase.py``; LLM, DB và ghi log đều bị
chặn, verify và explore được thay bằng hàm giả có kịch bản.
"""

# Import sqlbot_xpack TRƯỚC: apps.chat.* nằm trong một vòng import lẫn nhau.
import sqlbot_xpack  # noqa: F401  isort:skip

import types

import httpx
import openai
import orjson
import pytest
import sqlparse
from langchain_core.messages import AIMessage, AIMessageChunk

from apps.ai_model.model_factory import LLMConfig, bind_reasoning_effort
from apps.chat.models.chat_model import AiModelQuestion, ChatFinishStep, ChatQuestion
from apps.chat.models.chat_model import HumanPromptMessage, OperationEnum
from apps.chat.task import llm as llm_mod
from apps.chat.task import sql_review
from apps.chat.task.llm import LLMService
from apps.chat.task.prompt_budget import SchemaPrompt
from apps.chat.task.sql_review import (
    Probe,
    ProbeResult,
    VerifyDecision,
    limit_probe_sql,
    parse_explore_answer,
    parse_verify_answer,
    profile_columns,
    render_exploration,
    result_hints,
)
from apps.datasource.crud import datasource as ds_mod
from apps.datasource.m_schema import MSchema, MSchemaField, MSchemaTable
from common.error import SingleMessageError

def context_error():
    """400 vượt context, kiểu vLLM qua LiteLLM."""
    request = httpx.Request("POST", "http://litellm/v1/chat/completions")
    return openai.BadRequestError("maximum context length is 32768 tokens",
                                  response=httpx.Response(400, request=request), body=None)


PERMITTED_COLUMNS = {'ho': {'id', 'xa', 'nam'}, 'ra_soat': {'ho_id', 'phan_loai'}}


def parse_events(chunks):
    """Bóc các dòng SSE ``data:{...}`` thành list dict."""
    return [orjson.loads(c[len('data:'):].strip()) for c in chunks
            if isinstance(c, str) and c.startswith('data:')]


def event_types(chunks):
    """Danh sách ``type`` của các event SSE theo thứ tự."""
    return [e.get('type') for e in parse_events(chunks)]


def drive(gen):
    """Chạy cạn generator, trả về ``(chunk đã yield, giá trị return)``."""
    chunks = []
    while True:
        try:
            chunks.append(next(gen))
        except StopIteration as stop:
            return chunks, stop.value


class TestDocJson:
    """Đọc JSON của explore và verify; đường hỏng không bao giờ ném."""

    def test_explore_doc_bang_va_probe(self):
        text = ('suy nghĩ...\n{"tables": ["ra_soat"], "probes": '
                '[{"id": "p1", "purpose": "giá trị xã", "sql": "SELECT DISTINCT xa FROM ho"},'
                ' {"purpose": "thiếu sql"}]}')
        req = parse_explore_answer(text)
        assert req.tables == ('ra_soat',)
        assert req.probes == (Probe(id='p1', purpose='giá trị xã', sql='SELECT DISTINCT xa FROM ho'),)

    def test_explore_cat_so_probe_theo_cau_hinh(self, monkeypatch):
        monkeypatch.setattr(sql_review.settings, 'LLM_SQL_PROBE_MAX', 2)
        probes = [{'sql': f'SELECT {i}'} for i in range(5)]
        req = parse_explore_answer(orjson.dumps({'tables': [], 'probes': probes}).decode())
        assert [p.id for p in req.probes] == ['p1', 'p2']

    def test_explore_khong_co_json_thi_none(self):
        assert parse_explore_answer('không có gì') is None

    def test_verify_revise(self):
        d = parse_verify_answer('{"verdict": "Revise", "issues": ["đếm trùng"], "probes": []}')
        assert d.revise and d.issues == ('đếm trùng',) and d.forced == ''

    @pytest.mark.parametrize('text', ['', 'xin chào', '{"verdict": "maybe"}', '{"verdict": 1'])
    def test_verify_hong_thi_accept_co_danh_dau(self, text):
        d = parse_verify_answer(text)
        assert d.verdict == 'accept' and d.forced == 'unparsed'


class TestProbeSql:
    """Ràng buộc riêng của probe: ép LIMIT trên AST, chặn cột ngoài quyền."""

    def test_them_limit_khi_chua_co(self):
        assert limit_probe_sql('SELECT xa FROM ho', 'pg', 20) == 'SELECT xa FROM ho LIMIT 20'

    def test_giu_limit_nho_hon_va_ha_limit_lon_hon(self):
        assert limit_probe_sql('SELECT xa FROM ho LIMIT 5', 'pg', 20).endswith('LIMIT 5')
        assert limit_probe_sql('SELECT xa FROM ho LIMIT 500', 'pg', 20).endswith('LIMIT 20')

    def test_sql_server_dung_top(self):
        assert limit_probe_sql('SELECT xa FROM ho', 'sqlServer', 20) == 'SELECT TOP 20 xa FROM ho'

    def test_oracle_khong_dung_limit(self):
        assert 'LIMIT' not in limit_probe_sql('SELECT xa FROM ho', 'oracle', 20)

    @pytest.mark.parametrize('sql', ['SELECT 1; SELECT 2', 'DELETE FROM ho', 'UPDATE ho SET xa = 1'])
    def test_chi_nhan_mot_cau_select(self, sql):
        with pytest.raises(Exception):
            limit_probe_sql(sql, 'pg', 20)

    @pytest.mark.parametrize('sql', ['SELECT * FROM ho', 'SELECT h.* FROM ho h'])
    def test_chan_select_sao(self, sql):
        with pytest.raises(ValueError):
            limit_probe_sql(sql, 'pg', 20, PERMITTED_COLUMNS)

    def test_chan_cot_ngoai_quyen(self):
        with pytest.raises(ValueError, match='cccd'):
            limit_probe_sql('SELECT cccd FROM ho', 'pg', 20, PERMITTED_COLUMNS)

    def test_cho_phep_bi_danh_va_count_sao(self):
        sql = ('SELECT r.phan_loai, COUNT(*) AS so_luong FROM ra_soat r '
               'GROUP BY r.phan_loai ORDER BY so_luong DESC')
        assert limit_probe_sql(sql, 'pg', 20, PERMITTED_COLUMNS).endswith('LIMIT 20')

    def test_so_ten_cot_khong_phan_biet_hoa_thuong(self):
        limit_probe_sql('SELECT XA FROM ho', 'pg', 20, PERMITTED_COLUMNS)


class TestHoSoKetQua:
    """Hồ sơ cột và gợi ý tất định đưa vào verify."""

    def test_ho_so_tinh_tren_toan_bo_dong(self):
        rows = [{'a': 1, 'b': None}, {'a': 3, 'b': None}, {'a': 1, 'b': 'x'}]
        prof = {p['column']: p for p in profile_columns(['a', 'b'], rows)}
        assert prof['a'] == {'column': 'a', 'nulls': 0, 'distinct': 2, 'min': 1, 'max': 3}
        assert prof['b']['nulls'] == 2 and prof['b']['distinct'] == 1

    def test_kieu_lan_lon_thi_bo_min_max(self):
        prof = profile_columns(['a'], [{'a': 1}, {'a': 'x'}])[0]
        assert 'min' not in prof and prof['distinct'] == 2

    def test_goi_y(self):
        assert result_hints(['a'], []) == ['Truy vấn trả về 0 dòng.']
        assert 'mọi cột đều bằng 0' in result_hints(['a', 'b'], [{'a': 0, 'b': '0'}])[0]
        assert result_hints(['a', 'b'], [{'a': 1, 'b': None}]) == ['Cột b toàn NULL.']

    def test_exploration_rong_thi_chuoi_rong(self):
        assert render_exploration('', '', []) == ''
        block = render_exploration('# Table: ra_soat\n', '', [
            ProbeResult(probe=Probe('p1', 'xem', 'SELECT 1'), error='bị chặn')])
        assert block.startswith('<exploration>') and '<error>bị chặn</error>' in block


class TestReasoningEffort:
    """Mọi lời gọi gửi `reasoning_effort`; cờ enable_thinking có sẵn thì đi cùng chiều."""

    class FakeLLM:
        def bind(self, **kwargs):
            return kwargs

    def test_duong_gateway_chi_gui_reasoning_effort(self):
        config = LLMConfig(model_type='openai', model_name='m')
        assert bind_reasoning_effort(self.FakeLLM(), config, 'none') == {
            'extra_body': {'reasoning_effort': 'none'}}

    def test_duong_mac_dinh_giu_cau_hinh_va_lat_enable_thinking(self):
        extra = {'chat_template_kwargs': {'enable_thinking': False, 'k': 1}, 'top_k': 5}
        config = LLMConfig(model_type='openai', model_name='m',
                           additional_params={'extra_body': extra})
        bound = bind_reasoning_effort(self.FakeLLM(), config, 'low')
        assert bound['extra_body'] == {'reasoning_effort': 'low', 'top_k': 5,
                                       'chat_template_kwargs': {'enable_thinking': True, 'k': 1}}
        # Không sửa cấu hình gốc: LLMConfig được cache theo hash.
        assert extra['chat_template_kwargs']['enable_thinking'] is False


class TestCatalog:
    """Bảng được phép ngoài prompt thành catalog; render của M-Schema không đổi."""

    def test_catalog_khong_vao_render(self):
        orders = MSchemaTable('orders', 'orders', fields=(MSchemaField('id', 'int'),))
        items = MSchemaTable('items', 'items', comment='Mặt hàng')
        m = MSchema('db', (orders,))
        with_catalog = MSchema('db', (orders,), catalog=(items,))
        assert with_catalog.render() == m.render()
        assert with_catalog.permitted_table_names == ['orders', 'items']
        assert with_catalog.render_catalog() == 'items: Mặt hàng\n'

    def test_get_table_m_schema_dua_bang_ngoai_top_vao_catalog(self, monkeypatch):
        def table(tid, name):
            return types.SimpleNamespace(
                schema='public',
                fields=[types.SimpleNamespace(id=tid * 10, field_name='id', field_type='int',
                                              custom_comment=None)],
                table=types.SimpleNamespace(id=tid, table_name=name, custom_comment=None,
                                            embedding=None))

        objs = [table(1, 'orders'), table(2, 'customers')]
        monkeypatch.setattr(ds_mod, 'get_table_obj_by_ds', lambda session, current_user, ds: objs)
        monkeypatch.setattr(ds_mod, 'calc_table_embedding',
                            lambda tables, question, ds_id=None: [dict(tables[0])])
        monkeypatch.setattr(ds_mod.settings, 'TABLE_EMBEDDING_ENABLED', True)
        ds = types.SimpleNamespace(id=9, type='pg', table_relation=None)
        m_schema, names = ds_mod.get_table_m_schema(None, None, ds, 'q')
        assert names == ['orders']
        assert [t.name for t in m_schema.catalog] == ['customers']


@pytest.fixture
def service(monkeypatch):
    """``LLMService`` đủ sống cho pha SQL, explore, verify và probe; mọi I/O bị chặn."""
    s = object.__new__(LLMService)
    s.ds = types.SimpleNamespace(id=1, type='pg', name='ds', oid=1)
    s.record = types.SimpleNamespace(id=7, chat_id=3)
    s.current_user = types.SimpleNamespace(id=5)
    s.current_assistant = None
    s.sw_permission = None
    s.change_title = False
    s.table_name_list = ['ho']
    s.permitted_tables = ['ho', 'ra_soat']
    s.permitted_columns = PERMITTED_COLUMNS
    s.current_logs = {}
    s.sql_message = []
    s.chat_question = types.SimpleNamespace(
        question='câu hỏi', sql='',
        sql_revise_question=lambda issues, probes: f'REVISE[{issues}]{probes}',
        sql_exploration_info=lambda exploration: f'EXPLORATION:{exploration}')
    s.out_ds_instance = None

    s.calls = []
    s.answers = iter([f'SELECT {i} FROM ho' for i in range(1, 10)])

    def generate_sql(_session, retry_reason=None, revise_prompt=None):
        s.calls.append(('generate_sql', retry_reason, revise_prompt))
        sql = next(s.answers)
        s.sql_message.append(AIMessage(content=sql))
        yield {'content': orjson.dumps({'success': True, 'sql': sql, 'tables': ['ho']}).decode(),
               'reasoning_content': ''}

    def check_sql(session, res, operate):
        data = orjson.loads(res)
        return data['sql'], data['tables']

    def execute_sql(sql):
        s.calls.append(('execute', sql))
        return {'fields': ['n'], 'data': [{'n': sql}]}

    s.generate_sql = generate_sql
    s.get_chart_type_from_sql_answer = lambda text: 'bar'
    s.check_sql = check_sql
    s.check_save_sql = lambda session, res, operate: check_sql(session, res, operate)[0]
    s.execute_sql = execute_sql
    s.saved = []
    s.save_sql_data = lambda session, data_obj: s.saved.append(data_obj)
    s.build_sql_data_payload = lambda result: {'data': result['data']}

    monkeypatch.setattr(llm_mod, 'is_normal_user', lambda user: False)
    monkeypatch.setattr(llm_mod, 'start_log', lambda **kw: types.SimpleNamespace(**kw))
    monkeypatch.setattr(llm_mod, 'end_log', lambda **kw: kw.get('log'))
    monkeypatch.setattr(llm_mod, 'trigger_log_error', lambda **kw: None)
    monkeypatch.setattr(llm_mod, 'save_sql', lambda **kw: s.calls.append(('save_sql', kw['sql'])))
    monkeypatch.setattr(llm_mod, 'get_chat_chart_data', lambda _session, record_id: {'saved': True})
    monkeypatch.setattr(llm_mod.settings, 'LLM_SQL_MAX_RETRY', 1)
    monkeypatch.setattr(llm_mod.settings, 'LLM_ANSWER_ON_FAILURE', True)
    monkeypatch.setattr(llm_mod.settings, 'LLM_SQL_VERIFY_ENABLED', True)
    monkeypatch.setattr(llm_mod.settings, 'LLM_SQL_EXPLORE_ENABLED', False)
    monkeypatch.setattr(llm_mod.settings, 'LLM_SQL_VERIFY_MAX_REVISE', 1)
    return s


def script_verify(monkeypatch, service, decisions):
    """Thay verify bằng kịch bản; ghi lại thứ tự gọi xen với các bước khác."""
    decisions = iter(decisions)

    def fake_verify(llm_service, _session, sql, result, tables, exploration):
        service.calls.append(('verify', sql, tuple(tables)))
        return next(decisions)

    monkeypatch.setattr(sql_review, 'verify', fake_verify)


ACCEPT = VerifyDecision(verdict='accept')
REVISE = VerifyDecision(verdict='revise', issues=('đếm trùng',),
                        probes=(Probe('v1', 'đếm lặp', 'SELECT ho_id FROM ra_soat'),))


def run_phase(service, in_chat=True, stream=True):
    return drive(service._sql_phase(None, {}, in_chat, stream, ChatFinishStep.GENERATE_ANSWER))


class TestVerifyTrongPhaSql:
    """Verify là chốt chặn cuối: số liệu chỉ ra khỏi pha sau khi verify kết thúc."""

    def test_accept_thi_so_lieu_phat_sau_verify(self, service, monkeypatch):
        script_verify(monkeypatch, service, [ACCEPT])
        chunks, res = run_phase(service)
        assert [c[0] for c in service.calls] == ['generate_sql', 'execute', 'verify']
        assert event_types(chunks) == ['sql-result', 'info', 'sql', 'sql-data']
        assert res.verify_issues == () and res.result['data'] == [{'n': 'SELECT 1 FROM ho'}]
        assert len(service.saved) == 1

    def test_revise_sinh_lai_qua_duong_retry_kem_probe(self, service, monkeypatch):
        script_verify(monkeypatch, service, [REVISE, ACCEPT])
        probes = []
        service.run_probes = lambda _s, ps: probes.append(ps) or [
            ProbeResult(probe=ps[0], fields=('ho_id',), rows=({'ho_id': 1},))]
        chunks, res = run_phase(service)

        gen_calls = [c for c in service.calls if c[0] == 'generate_sql']
        assert gen_calls[0] == ('generate_sql', None, None)
        assert gen_calls[1][1] is None and gen_calls[1][2].startswith('REVISE[- đếm trùng]')
        assert '<probe id="v1">' in gen_calls[1][2]
        assert probes == [[REVISE.probes[0]]]
        # Hai event `sql` như vòng retry, nhưng chỉ một `sql-data` và là của câu đã được accept.
        assert event_types(chunks).count('sql') == 2
        data_events = [e for e in parse_events(chunks) if e['type'] == 'sql-data']
        assert data_events == [{'content': 'execute-success', 'type': 'sql-data',
                                'data': {'data': [{'n': 'SELECT 2 FROM ho'}]}}]
        assert res.verify_issues == ()

    def test_het_vong_sua_thi_ket_qua_cuoi_di_tiep_kem_van_de(self, service, monkeypatch):
        script_verify(monkeypatch, service, [REVISE, REVISE])
        service.run_probes = lambda _s, ps: []
        _, res = run_phase(service, in_chat=False, stream=False)
        assert res.result['data'] == [{'n': 'SELECT 2 FROM ho'}]
        assert res.verify_issues == ('đếm trùng',)
        assert [c[0] for c in service.calls].count('verify') == 2

    def test_lan_sua_hong_han_thi_quay_ve_ket_qua_da_chay(self, service, monkeypatch):
        script_verify(monkeypatch, service, [REVISE])
        service.run_probes = lambda _s, ps: []
        goc = service.execute_sql

        def execute_sql(sql):
            if sql != 'SELECT 1 FROM ho':
                raise SingleMessageError('lỗi chạy')
            return goc(sql)

        service.execute_sql = execute_sql
        json_result = {}
        chunks, res = drive(service._sql_phase(None, json_result, True, True,
                                               ChatFinishStep.GENERATE_ANSWER))
        assert res.degraded_reason is None
        assert res.result['data'] == [{'n': 'SELECT 1 FROM ho'}]
        assert res.verify_issues == ('đếm trùng',)
        assert ('save_sql', 'SELECT 1 FROM ho') in service.calls
        # Lịch sử hội thoại SQL quay về câu trả lời đã chạy được.
        assert service.sql_message[-1].content == 'SELECT 1 FROM ho'
        sql_events = [e['content'] for e in parse_events(chunks) if e['type'] == 'sql']
        assert sql_events[-1] == sqlparse.format('SELECT 1 FROM ho', reindent=True)
        assert event_types(chunks)[-2:] == ['sql', 'sql-data']

    def test_loi_llm_khi_sua_thi_quay_ve_ket_qua_da_chay(self, service, monkeypatch):
        script_verify(monkeypatch, service, [REVISE])
        service.run_probes = lambda _s, ps: []
        goc = service.generate_sql

        def generate_sql(_session, retry_reason=None, revise_prompt=None):
            if revise_prompt:
                raise RuntimeError('LLM timeout')
            yield from goc(_session, retry_reason)

        service.generate_sql = generate_sql
        json_result = {}
        _, res = drive(service._sql_phase(None, json_result, False, False,
                                          ChatFinishStep.GENERATE_ANSWER))
        assert res.result['data'] == [{'n': 'SELECT 1 FROM ho'}]
        assert json_result['sql'] == 'SELECT 1 FROM ho'

    def test_lan_sua_khong_dat_lai_tieu_de(self, service, monkeypatch):
        script_verify(monkeypatch, service, [REVISE, ACCEPT])
        service.run_probes = lambda _s, ps: []
        service.change_title = True
        service.get_brief_from_sql_answer = lambda text: 'tiêu đề'
        service.get_record = lambda: types.SimpleNamespace(chat_id=3)
        monkeypatch.setattr(llm_mod, 'rename_chat', lambda session, rename_object: 'tiêu đề')
        chunks, _ = run_phase(service)
        assert event_types(chunks).count('brief') == 1

    def test_tat_verify_thi_khong_goi(self, service, monkeypatch):
        monkeypatch.setattr(llm_mod.settings, 'LLM_SQL_VERIFY_ENABLED', False)
        script_verify(monkeypatch, service, [])
        chunks, _ = run_phase(service)
        assert 'verify' not in [c[0] for c in service.calls]
        assert event_types(chunks) == ['sql-result', 'info', 'sql', 'sql-data']

    def test_tro_ly_datasource_dong_luon_tat(self, service, monkeypatch):
        monkeypatch.setattr(llm_mod.settings, 'LLM_SQL_EXPLORE_ENABLED', True)
        service.current_assistant = types.SimpleNamespace(type=1)
        assert not service.verify_enabled() and not service.explore_enabled()


class TestExploreTrongPhaSql:
    """Khối exploration vào hội thoại SQL dưới dạng message hệ thống, trước lần sinh đầu."""

    def test_exploration_dung_truoc_lan_sinh_dau(self, service, monkeypatch):
        monkeypatch.setattr(llm_mod.settings, 'LLM_SQL_EXPLORE_ENABLED', True)
        monkeypatch.setattr(llm_mod.settings, 'LLM_SQL_VERIFY_ENABLED', False)
        seen = []
        monkeypatch.setattr(sql_review, 'explore', lambda svc, _s: '<exploration>x</exploration>')
        goc = service.generate_sql

        def generate_sql(_session, retry_reason=None, revise_prompt=None):
            seen.extend(m.content for m in service.sql_message)
            yield from goc(_session, retry_reason)

        service.generate_sql = generate_sql
        run_phase(service)
        assert seen[0] == 'EXPLORATION:<exploration>x</exploration>'
        assert all(getattr(m, 'sqlbot_system', False) for m in service.sql_message[:2])


class TestDuongChayProbe:
    """Probe đi đúng đường kiểm, áp quyền và thực thi của SQL chính."""

    def test_probe_hop_le_duoc_ep_limit_va_chay(self, service):
        results = service.run_probes(None, [Probe('p1', 'xem', 'SELECT xa FROM ho')])
        assert results[0].error == ''
        assert ('execute', 'SELECT xa FROM ho LIMIT 20') in service.calls

    def test_bang_ngoai_quyen_bi_chan_truoc_khi_chay(self, service):
        results = service.run_probes(None, [Probe('p1', 'xem', 'SELECT id FROM bi_mat')])
        assert 'unauthorized tables' in results[0].error
        assert not any(c[0] == 'execute' for c in service.calls)

    def test_mot_probe_hong_khong_keo_probe_khac(self, service):
        results = service.run_probes(None, [Probe('p1', 'a', 'SELECT cccd FROM ho'),
                                            Probe('p2', 'b', 'SELECT id FROM ho')])
        assert results[0].error and results[1].error == ''
        assert [r.probe.id for r in results] == ['p1', 'p2']

    def test_ap_scope_sw_len_ban_thuc_thi(self, service, monkeypatch):
        service.sw_permission = types.SimpleNamespace(restricted=True,
                                                      scope_queries={'ho': 'SELECT * FROM ho'})
        monkeypatch.setattr(llm_mod, 'apply_scope_to_sql', lambda sql, scopes, ds_type: 'SCOPED')
        service.run_probes(None, [Probe('p1', 'xem', 'SELECT xa FROM ho')])
        assert ('execute', 'SCOPED') in service.calls

    def test_luat_dong_built_in_duoc_ap_va_ep_limit_lai(self, service, monkeypatch):
        monkeypatch.setattr(llm_mod, 'is_normal_user', lambda user: True)
        service.generate_filter = lambda _s, sql, tables: orjson.dumps(
            {'success': True, 'sql': 'SELECT xa FROM ho WHERE xa = 1', 'tables': tables}).decode()
        service.run_probes(None, [Probe('p1', 'xem', 'SELECT xa FROM ho')])
        assert ('execute', 'SELECT xa FROM ho WHERE xa = 1 LIMIT 20') in service.calls

    def test_ghi_chat_log_rieng_cho_probe(self, service, monkeypatch):
        logs = []
        monkeypatch.setattr(llm_mod, 'start_log',
                            lambda **kw: logs.append(kw['operate']) or types.SimpleNamespace())
        service.run_probes(None, [Probe('p1', 'xem', 'SELECT xa FROM ho')])
        assert logs == [OperationEnum.EXECUTE_PROBE]


class TestPromptAnswer:
    """Vấn đề của verify chỉ vào prompt answer khi có; không có thì prompt giữ nguyên."""

    def test_khong_co_van_de_thi_prompt_nhu_cu(self):
        q = AiModelQuestion(question='q', sql='SELECT 1', fields='[]', data='[]', data_scope='s')
        assert '</sql>\n\n<fields>' in q.answer_user_question()

    def test_co_van_de_thi_them_khoi_canh_bao(self):
        q = AiModelQuestion(question='q', sql='SELECT 1', fields='[]', data='[]', data_scope='s',
                            verify_note='<verification-issues>\n- x\n</verification-issues>\n')
        assert '</sql>\n<verification-issues>\n- x\n</verification-issues>\n\n<fields>' in \
            q.answer_user_question()


class FakeLLM:
    """LLM giả cho explore/verify: lần lượt ném lỗi hoặc trả câu trả lời theo kịch bản."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.prompts = []

    def stream(self, messages):
        self.prompts.append([m.content for m in messages])
        outcome = self.outcomes.pop(0)

        def gen():
            if isinstance(outcome, Exception):
                raise outcome
            yield AIMessageChunk(content=outcome)

        return gen()


@pytest.fixture
def review_service(monkeypatch):
    """``LLMService`` tối thiểu cho ``sql_review.explore``/``verify`` với log bị chặn."""
    s = object.__new__(LLMService)
    s.ds = types.SimpleNamespace(id=1, type='pg')
    s.record = types.SimpleNamespace(id=7)
    s.chat_question = ChatQuestion(chat_id=1, question='Bao nhiêu hộ nghèo?', engine='PostgreSQL')
    orders = MSchemaTable('ho', 'ho', fields=(MSchemaField('id', 'int'),))
    extra = MSchemaTable('ra_soat', 'ra_soat', comment='Kết quả rà soát',
                         fields=(MSchemaField('phan_loai', 'text'),))
    s.schema_prompt = SchemaPrompt(MSchema('db', (orders,), catalog=(extra,)), 'mau')
    s.schema_reduction_index = 0
    s.schema_reductions = ()
    s.sql_schema_message = HumanPromptMessage(content='<m-schema>')
    s.sql_message = [s.sql_schema_message]
    s.load_sample_data = lambda _s, tables: f'mẫu của {tables}'
    s.run_probes = lambda _s, probes: [ProbeResult(probe=p, rows=({'x': 1},)) for p in probes]
    s.used_tables_schema = lambda _s, tables: f'schema {tables}'
    for name in ('start_log', 'end_log', 'trigger_log_error'):
        monkeypatch.setattr(sql_review, name, lambda **kw: types.SimpleNamespace())
    return s


class TestDieuPhoiExplore:
    """Explore không bao giờ ném; xong thì catalog bị bỏ để pha SQL không tốn nấc rút gọn."""

    def test_nap_schema_bang_xin_xem_va_ket_qua_probe(self, review_service):
        review_service.llm = FakeLLM(
            '{"tables": ["ra_soat", "khong_co"], "probes": [{"id": "p1", "sql": "SELECT 1"}]}')
        block = sql_review.explore(review_service, None)
        assert '# Table: ra_soat, Kết quả rà soát' in block
        assert "mẫu của ['ra_soat']" in block and '<probe id="p1">' in block
        assert 'khong_co' not in block
        assert 'ra_soat: Kết quả rà soát' in review_service.llm.prompts[0][-1]
        assert review_service.schema_prompt.m_schema.catalog == ()

    def test_bi_tu_choi_prompt_thi_bo_catalog_truoc(self, review_service):
        review_service.llm = FakeLLM(context_error(), '{"tables": [], "probes": []}')
        assert sql_review.explore(review_service, None) == ''
        assert review_service.schema_reductions == ('drop_catalog',)
        assert 'ra_soat' not in review_service.llm.prompts[1][-1]

    def test_loi_llm_thi_tra_rong(self, review_service):
        review_service.llm = FakeLLM(RuntimeError('timeout'))
        assert sql_review.explore(review_service, None) == ''
        assert review_service.schema_prompt.m_schema.catalog == ()


class TestDieuPhoiVerify:
    """Verify dùng model bật thinking; hỏng thì thành accept có đánh dấu."""

    def test_dung_thinking_llm_va_dua_ho_so_vao_prompt(self, review_service):
        review_service.llm = FakeLLM()
        review_service.thinking_llm = FakeLLM('{"verdict": "accept", "issues": [], "probes": []}')
        result = {'fields': ['n'], 'data': [{'n': 0}]}
        decision = sql_review.verify(review_service, None, 'SELECT 0', result, ['ho'], '')
        assert decision.verdict == 'accept' and decision.forced == ''
        user_prompt = review_service.thinking_llm.prompts[0][1]
        assert "schema ['ho']" in user_prompt and '"nulls": 0' in user_prompt
        assert 'mọi cột đều bằng 0' in user_prompt

    def test_loi_thi_accept(self, review_service):
        review_service.thinking_llm = FakeLLM(RuntimeError('timeout'))
        decision = sql_review.verify(review_service, None, 'SELECT 1', {'fields': [], 'data': []},
                                     [], '')
        assert decision.verdict == 'accept' and decision.forced == 'error'
