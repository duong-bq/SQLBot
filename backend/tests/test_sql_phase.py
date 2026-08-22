"""Test cho pha SQL sau khi bóc khỏi ``run_task`` thành generator ``_sql_phase``.

Trọng tâm KHÔNG phải chất lượng SQL mà là **hợp đồng ghép nối** — đúng thứ mà việc bóc có thể làm
gãy mà đọc diff không thấy:

- generator con có hai kênh ra (``yield`` chở event SSE, ``return`` chở kết quả), và ``run_task``
  phải nối đúng cả hai;
- ``return`` trong generator con không còn kết thúc request, nên cờ ``stopped`` là thứ duy nhất
  giữ cho pha chart không chạy sau khi ``finish`` đã phát;
- ``json_result`` truyền vào và sửa tại chỗ, nên bản mà ``run_task`` trả về ở nhánh không-stream
  phải thấy được những gì pha SQL đã ghi;
- vòng thử lại và nhánh hạ cấp vẫn nằm gọn bên trong pha, không rò event nào ra dây.

Cách dựng: ``LLMService`` được tạo bằng ``object.__new__`` để bỏ qua ``__init__`` (vốn cần DB,
model và datasource thật), rồi gắn tay đúng những thuộc tính mà pha SQL đụng tới. Cố tình không
dùng ``unittest.mock`` cho phần thân: các stub ở đây phải trả về hình dạng dữ liệu thật (generator
chunk, tuple ``(sql, tables)``, dict ``fields``/``data``) thì test mới bắt được lỗi hình dạng.
"""

# Import sqlbot_xpack TRƯỚC: apps.chat.* nằm trong một vòng import lẫn nhau, chỉ gỡ được khi xpack
# là module khởi động vòng đó.
import sqlbot_xpack  # noqa: F401  isort:skip

import types

import orjson
import pytest

from apps.chat.models.chat_model import ChatFinishStep
from apps.chat.task import llm as llm_mod
from apps.chat.task.llm import LLMService, SqlPhaseResult
from apps.chat.task.question_gate import RouteDecision, fail_open
from common.error import SingleMessageError, SQLBotDBConnectionError

SQL_ANSWER = '{"success":true,"sql":"SELECT 1","tables":["t1"],"chart-type":"bar"}'


def parse_events(chunks):
    """Bóc các dòng SSE ``data:{...}`` thành list dict, bỏ qua thứ không phải chuỗi SSE.

    Nhánh không-stream của ``run_task`` yield thẳng dict ``json_result`` chứ không phải chuỗi, nên
    hàm này phải chịu được stream hỗn hợp thay vì giả định mọi phần tử đều là SSE.
    """
    events = []
    for c in chunks:
        if isinstance(c, str) and c.startswith('data:'):
            events.append(orjson.loads(c[len('data:'):].strip()))
    return events


def event_types(chunks):
    return [e.get('type') for e in parse_events(chunks)]


@pytest.fixture
def service(monkeypatch):
    """Một ``LLMService`` chỉ đủ sống cho pha SQL đi hết đường thành công.

    Mọi lời gọi ra ngoài (LLM, DB, ghi log vận hành) đều bị chặn. ``is_normal_user`` trả False để
    pha đi nhánh không lọc quyền hàng — nhánh quyền có test riêng ở ``test_sw_permission.py``,
    kéo nó vào đây chỉ làm test giòn thêm mà không kiểm được gì về việc bóc.
    """
    s = object.__new__(LLMService)

    s.ds = types.SimpleNamespace(id=1, type='mysql', name='ds', oid=1)
    s.record = types.SimpleNamespace(id=7, chat_id=3)
    s.current_user = types.SimpleNamespace(id=1)
    s.current_assistant = None
    s.sw_permission = None
    s.change_title = False
    s.table_name_list = ['t1']
    s.current_logs = {}
    s.chat_question = types.SimpleNamespace(question='câu hỏi')
    s.out_ds_instance = None

    s.calls = []

    def generate_sql(_session, retry_reason=None):
        s.calls.append(('generate_sql', retry_reason))
        yield {'content': SQL_ANSWER, 'reasoning_content': ''}

    s.generate_sql = generate_sql
    s.get_chart_type_from_sql_answer = lambda text: 'bar'
    s.check_sql = lambda session, res, operate: ('SELECT 1', ['t1'])
    s.check_save_sql = lambda session, res, operate: 'SELECT 1'
    s.execute_sql = lambda sql: {'fields': ['a'], 'data': [{'a': 1}]}
    s.save_sql_data = lambda session, data_obj: None
    s.build_sql_data_payload = lambda result: {'fields': result['fields'], 'data': result['data']}

    monkeypatch.setattr(llm_mod, 'extract_tables_from_sql', lambda sql, ds_type=None: set())
    monkeypatch.setattr(llm_mod, 'is_normal_user', lambda user: False)
    monkeypatch.setattr(llm_mod, 'start_log', lambda **kw: None)
    monkeypatch.setattr(llm_mod, 'end_log', lambda **kw: None)
    monkeypatch.setattr(llm_mod.settings, 'LLM_SQL_MAX_RETRY', 1)
    monkeypatch.setattr(llm_mod.settings, 'LLM_ANSWER_ON_FAILURE', True)
    return s


def drive(gen):
    """Chạy cạn generator, trả về ``(danh sách chunk đã yield, giá trị return)``.

    Giá trị return của generator chỉ lấy được qua ``StopIteration.value`` — đây chính là kênh thứ
    hai mà ``yield from`` dùng, nên test phải đọc nó theo đúng cách đó.
    """
    chunks = []
    while True:
        try:
            chunks.append(next(gen))
        except StopIteration as stop:
            return chunks, stop.value


class TestSqlPhaseKenhRa:
    """Hai kênh ra của generator con."""

    def test_duong_thanh_cong_tra_ve_du_nam_gia_tri(self, service):
        chunks, res = drive(service._sql_phase(None, {}, True, True, ChatFinishStep.GENERATE_ANSWER))

        assert isinstance(res, SqlPhaseResult)
        assert res.stopped is False
        assert res.degraded_reason is None
        assert res.chart_type == 'bar'
        assert res.tables == ['t1']
        assert res.result == {'fields': ['a'], 'data': [{'a': 1}]}
        assert res.data == [{'a': 1}]
        assert event_types(chunks) == ['sql-result', 'info', 'sql', 'sql-data']

    def test_json_result_duoc_sua_tai_cho(self, service, monkeypatch):
        """Kênh phụ cho nhánh không-stream: pha ghi vào dict của người gọi chứ không trả về."""
        monkeypatch.setattr(llm_mod, 'get_chat_chart_data', lambda session, rid: [{'a': 1}])
        json_result = {}
        drive(service._sql_phase(None, json_result, False, False, ChatFinishStep.GENERATE_ANSWER))
        assert json_result['sql'] == 'SELECT 1'
        assert 'data' in json_result


class TestSqlPhaseDungSom:
    """``finish_step`` dừng ở GENERATE_SQL — chỗ ``return`` đổi nghĩa sau khi bóc."""

    def test_phat_finish_roi_bao_stopped(self, service):
        chunks, res = drive(service._sql_phase(None, {}, True, True, ChatFinishStep.GENERATE_SQL))

        assert res.stopped is True
        assert event_types(chunks)[-1] == 'finish'
        # SQL chưa chạy nên không có dữ liệu, nhưng chart_type/tables đã có thì vẫn phải mang về.
        assert res.result is None
        assert res.chart_type == 'bar'

    def test_khong_chay_sql_khi_dung_som(self, service):
        def no_exec(sql):
            raise AssertionError('không được chạy SQL khi finish_step dừng ở GENERATE_SQL')

        service.execute_sql = no_exec
        drive(service._sql_phase(None, {}, True, True, ChatFinishStep.GENERATE_SQL))


class TestSqlPhaseThuLai:
    """Vòng thử lại phải nằm trọn trong pha, không rò ra hợp đồng SSE."""

    def test_thu_lai_roi_thanh_cong(self, service):
        lan = {'n': 0}
        goc = service.execute_sql

        def execute_sql(sql):
            lan['n'] += 1
            if lan['n'] == 1:
                raise SingleMessageError('lỗi giả lần đầu')
            return goc(sql)

        service.execute_sql = execute_sql
        chunks, res = drive(service._sql_phase(None, {}, True, True, ChatFinishStep.GENERATE_ANSWER))

        assert res.degraded_reason is None
        assert res.result is not None
        # Lần sinh lại phải nhận được lý do hỏng của lần trước.
        assert [c[1] for c in service.calls] == [None, 'lỗi giả lần đầu']
        # Hành vi có sẵn từ trước khi bóc, ghi lại ở đây để lần sau đổi thì test biết: lần thử lại
        # phát LẠI trọn bộ event của pha, nên client nhận hai `sql` và bản sau đè bản trước. Cái
        # mà vòng retry cố ý không phát là event RIÊNG báo "đang thử lại", không phải các event này.
        assert event_types(chunks) == ['sql-result', 'info', 'sql',
                                       'sql-result', 'info', 'sql', 'sql-data']

    def test_het_luot_thu_thi_ha_cap_chu_khong_nem(self, service):
        def execute_sql(sql):
            raise SingleMessageError('hỏng mãi')

        service.execute_sql = execute_sql
        chunks, res = drive(service._sql_phase(None, {}, True, True, ChatFinishStep.GENERATE_ANSWER))

        assert res.degraded_reason == 'hỏng mãi'
        assert res.result is None
        assert 'sql-data' not in event_types(chunks)

    def test_mat_ket_noi_thi_khong_thu_lai(self, service):
        def execute_sql(sql):
            raise SQLBotDBConnectionError('mất kết nối')

        service.execute_sql = execute_sql
        _, res = drive(service._sql_phase(None, {}, True, True, ChatFinishStep.GENERATE_ANSWER))

        assert res.degraded_reason is not None
        assert len(service.calls) == 1

    def test_nhanh_mcp_van_nem_loi_that(self, service):
        """``in_chat=False`` giữ nguyên hợp đồng cũ: ngoại lệ phải xuyên qua ``yield from``."""

        def execute_sql(sql):
            raise SingleMessageError('hỏng mãi')

        service.execute_sql = execute_sql
        with pytest.raises(SingleMessageError):
            drive(service._sql_phase(None, {}, False, True, ChatFinishStep.GENERATE_ANSWER))


@pytest.fixture
def run_task_service(service, monkeypatch):
    """Bổ sung phần tiền đề của ``run_task`` để chạy được từ đầu request."""
    service.filter_terminology_template = lambda *a, **k: None
    service.filter_training_template = lambda *a, **k: None
    service.filter_custom_prompts = lambda *a, **k: None
    service.init_messages = lambda *a, **k: None
    service.validate_history_ds = lambda *a, **k: None
    service.get_record = lambda: types.SimpleNamespace(id=7, chat_id=3, question='q',
                                                       regenerate_record_id=None)
    service.finish = lambda *a, **k: None
    service.trans = lambda key: key

    class FakeSessionMaker:
        """``session_maker`` được dùng vừa như factory vừa như registry (``.remove()``)."""

        def __call__(self):
            return None

        def remove(self):
            return None

    monkeypatch.setattr(llm_mod, 'check_connection', lambda ds, trans=None: True)
    monkeypatch.setattr(llm_mod, 'session_maker', FakeSessionMaker())
    return service


class TestRunTaskGhepNoi:
    """``run_task`` nối generator con: event đi lên, kết quả đi về."""

    def test_chuyen_tiep_nguyen_ven_event_cua_pha_con(self, run_task_service, monkeypatch):
        def fake_phase(_session, json_result, in_chat, stream, finish_step):
            yield 'data:' + orjson.dumps({'type': 'sql', 'content': 'SELECT 1'}).decode() + '\n\n'
            yield 'data:' + orjson.dumps({'type': 'sql-data'}).decode() + '\n\n'
            return SqlPhaseResult(result={'fields': ['a'], 'data': [{'a': 1}]}, tables=['t1'],
                                  chart_type='bar', data=[{'a': 1}])

        run_task_service._sql_phase = fake_phase
        chunks = list(run_task_service.run_task(in_chat=True, stream=True,
                                                finish_step=ChatFinishStep.QUERY_DATA))
        types_ = event_types(chunks)
        assert types_[-3:] == ['sql', 'sql-data', 'finish']

    def test_stopped_thi_dung_ngay_khong_phat_them_finish(self, run_task_service):
        """Bẫy chính của việc bóc: bỏ qua cờ này thì `finish` bị phát hai lần."""

        def fake_phase(_session, json_result, in_chat, stream, finish_step):
            yield 'data:' + orjson.dumps({'type': 'finish'}).decode() + '\n\n'
            return SqlPhaseResult(stopped=True)

        run_task_service._sql_phase = fake_phase
        chunks = list(run_task_service.run_task(in_chat=True, stream=True,
                                                finish_step=ChatFinishStep.GENERATE_SQL))
        assert event_types(chunks).count('finish') == 1

    def test_json_result_pha_con_ghi_ra_toi_duoc_ket_qua_cuoi(self, run_task_service):
        def fake_phase(_session, json_result, in_chat, stream, finish_step):
            json_result['sql'] = 'SELECT 1'
            return SqlPhaseResult(result={'fields': ['a'], 'data': [{'a': 1}]}, data=[{'a': 1}])
            yield  # noqa: unreachable — giữ hàm là generator

        run_task_service._sql_phase = fake_phase
        chunks = list(run_task_service.run_task(in_chat=False, stream=False,
                                                finish_step=ChatFinishStep.QUERY_DATA))
        dicts = [c for c in chunks if isinstance(c, dict)]
        assert dicts and dicts[-1]['sql'] == 'SELECT 1'

    def test_degraded_reason_dan_toi_prompt_fallback(self, run_task_service, monkeypatch):
        """Kiểm cờ hạ cấp đi qua được kênh ``return`` chứ không rơi mất khi bóc."""
        chon = {}

        def fake_phase(_session, json_result, in_chat, stream, finish_step):
            return SqlPhaseResult(degraded_reason='hỏng mãi')
            yield  # noqa: unreachable

        run_task_service._sql_phase = fake_phase
        run_task_service.build_answer_fallback_prompts = lambda s, reason, kind: (
            chon.setdefault('nhanh', 'fallback'), chon.setdefault('kind', kind))
        run_task_service.build_answer_prompts = lambda *a: (chon.setdefault('nhanh', 'thuong'),
                                                            'user')
        run_task_service.run_answer_worker = lambda *a: None
        run_task_service.drain_answer_queue = lambda q, st, block: iter(())
        run_task_service.drain_recommend_queue = lambda q, st, block: iter(())
        monkeypatch.setattr(llm_mod, 'executor',
                            types.SimpleNamespace(submit=lambda *a, **k: None))
        monkeypatch.setattr(llm_mod.settings, 'CHAT_INLINE_RECOMMEND_ENABLED', False)

        list(run_task_service.run_task(in_chat=True, stream=True,
                                       finish_step=ChatFinishStep.GENERATE_ANSWER))
        assert chon['nhanh'] == 'fallback'
        # Pha SQL hỏng thật thì phải là biến thể "failed"; nhánh cổng định tuyến bỏ qua truy vấn
        # mới dùng "no_query". Lẫn hai cái là báo sự cố cho một lượt hoàn toàn bình thường.
        assert chon['kind'] == 'failed'

    def test_data_di_toi_duoc_bang_markdown_cua_nhanh_mcp(self, run_task_service, monkeypatch):
        """``_data`` chỉ được đọc ở nhánh MCP, nên nhánh SSE không bảo vệ được nó."""

        def fake_phase(_session, json_result, in_chat, stream, finish_step):
            return SqlPhaseResult(result={'fields': ['a'], 'data': [{'a': 1}]}, data=[{'a': 7}])
            yield  # noqa: unreachable

        run_task_service._sql_phase = fake_phase
        chunks = list(run_task_service.run_task(in_chat=False, stream=True,
                                                finish_step=ChatFinishStep.QUERY_DATA))
        markdown = ''.join(c for c in chunks if isinstance(c, str))
        # Khẳng định trên NỘI DUNG bảng, và chặn cả nhánh lỗi: chuỗi traceback có số dòng nên một
        # phép kiểm kiểu `'7' in markdown` sẽ đúng ngay cả khi run_task ném lỗi — bẫy này đã sập
        # một lần lúc viết test.
        assert 'ERROR' not in markdown
        assert 'The SQL execution result is empty' not in markdown
        assert '| a' in markdown and '7' in markdown

    def test_tables_va_chart_type_di_toi_duoc_pha_chart(self, run_task_service, monkeypatch):
        """Hai giá trị này chỉ có người đọc ở pha chart — nơi nằm ngoài vùng mổ."""
        nhan = {}

        def fake_phase(_session, json_result, in_chat, stream, finish_step):
            return SqlPhaseResult(result={'fields': ['a'], 'data': [{'a': 1}]}, data=[{'a': 1}],
                                  tables=['t1'], chart_type='bar')
            yield  # noqa: unreachable

        def fake_generate_chart(_session, chart_type, schema):
            nhan['chart_type'] = chart_type
            yield {'content': '{"type":"bar"}', 'reasoning_content': ''}

        run_task_service._sql_phase = fake_phase
        run_task_service.generate_chart = fake_generate_chart
        run_task_service.check_save_chart = lambda session, res: {'type': 'bar'}
        run_task_service.build_answer_prompts = lambda *a: ('sys', 'user')
        run_task_service.run_answer_worker = lambda *a: None
        run_task_service.drain_answer_queue = lambda q, st, block: iter(())
        run_task_service.drain_recommend_queue = lambda q, st, block: iter(())

        def fake_get_table_schema(**kw):
            nhan['table_list'] = kw.get('table_list')
            return 'schema', ['t1']

        monkeypatch.setattr(llm_mod, 'get_table_schema', fake_get_table_schema)
        monkeypatch.setattr(llm_mod, 'executor', types.SimpleNamespace(submit=lambda *a, **k: None))
        monkeypatch.setattr(llm_mod.settings, 'CHAT_INLINE_RECOMMEND_ENABLED', False)

        list(run_task_service.run_task(in_chat=True, stream=True,
                                       finish_step=ChatFinishStep.GENERATE_CHART))
        assert nhan['chart_type'] == 'bar'
        assert nhan['table_list'] == ['t1']


class TestCongDinhTuyenGhepNoi:
    """Cổng định tuyến nối vào ``run_task``: ai được rẽ, ai không, và rẽ rồi thì đi đâu.

    Cổng là thứ chèn vào giữa một đường đang chạy tốt, nên tiêu chí nghiệm thu nặng nhất không
    phải "rẽ đúng" mà là "tắt thì không đổi gì". Bản thân chất lượng phân loại chỉ đo được bằng
    lưu lượng thật ở chế độ chỉ đo, không đo được ở đây.
    """

    @staticmethod
    def _stub_answer(service, monkeypatch, chon):
        """Chặn hai pha chạy sau cổng và ghi lại nhánh prompt mà run_task đã chọn."""
        service.build_answer_fallback_prompts = lambda s, reason, kind: (
            chon.setdefault('nhanh', 'fallback'), chon.setdefault('kind', kind))
        service.build_answer_prompts = lambda *a: (chon.setdefault('nhanh', 'thuong'), 'user')
        service.run_answer_worker = lambda *a: None
        service.drain_answer_queue = lambda q, st, block: iter(())
        service.drain_recommend_queue = lambda q, st, block: iter(())
        monkeypatch.setattr(llm_mod, 'executor',
                            types.SimpleNamespace(submit=lambda *a, **k: None))
        monkeypatch.setattr(llm_mod.settings, 'CHAT_INLINE_RECOMMEND_ENABLED', False)

    @staticmethod
    def _fake_phase(goi):
        def fake_phase(_session, json_result, in_chat, stream, finish_step):
            goi.append(True)
            return SqlPhaseResult(result={'fields': ['a'], 'data': [{'a': 1}]}, tables=['t1'],
                                  chart_type='bar', data=[{'a': 1}])
            yield  # noqa: unreachable — giữ hàm là generator

        return fake_phase

    def _chay(self, service, monkeypatch, decision, **kwargs):
        goi, chon = [], {}
        service._sql_phase = self._fake_phase(goi)
        self._stub_answer(service, monkeypatch, chon)
        if decision == 'real':
            pass  # dùng `decide_route` thật, để kiểm chính cái chốt bên trong nó
        elif decision is not None:
            monkeypatch.setattr(llm_mod, 'decide_route', lambda svc, sess: decision)
        else:
            monkeypatch.setattr(llm_mod, 'decide_route',
                                lambda svc, sess: pytest.fail('cổng chạy ở nhánh không được rẽ'))
        opts = {'in_chat': True, 'stream': True, 'finish_step': ChatFinishStep.GENERATE_ANSWER}
        opts.update(kwargs)
        chunks = list(service.run_task(**opts))
        return goi, chon, chunks

    def test_tat_co_thi_khong_them_event_nao_len_day(self, run_task_service, monkeypatch):
        """Tiêu chí nghiệm thu: cờ tắt thì dây SSE giống hệt lúc chưa có cổng."""
        goi, chon, chunks = self._chay(run_task_service, monkeypatch, fail_open('disabled'))
        assert goi == [True]
        assert chon['nhanh'] == 'thuong'
        assert not any('route' in str(e.get('msg', '')) for e in parse_events(chunks))

    def test_co_tat_thi_cong_that_khong_cham_toi_llm(self, run_task_service, monkeypatch):
        """Như trên nhưng dùng `decide_route` THẬT, vì đó mới là đường chạy trên máy thật.

        Test trên chặn cổng bằng monkeypatch nên chỉ kiểm được `run_task`; test này kiểm nốt nửa
        còn lại: cổng tự chặn từ dòng đầu khi cờ tắt, không gọi LLM và không sinh chat_log.
        """
        monkeypatch.setattr(llm_mod.settings, 'LLM_ROUTE_ENABLED', False)
        run_task_service.route_llm = types.SimpleNamespace(
            stream=lambda msg: pytest.fail('cờ tắt mà cổng vẫn gọi LLM'))
        goi, chon, chunks = self._chay(run_task_service, monkeypatch, 'real')
        assert goi == [True]
        assert chon['nhanh'] == 'thuong'
        assert not any(e.get('type') == 'info' and 'route' in str(e.get('msg', ''))
                       for e in parse_events(chunks))

    def test_ret_sang_answer_thi_khong_chay_pha_sql(self, run_task_service, monkeypatch):
        goi, chon, chunks = self._chay(
            run_task_service, monkeypatch,
            RouteDecision(action='answer', category='chitchat', reason='chào hỏi'))
        assert goi == []
        assert chon['nhanh'] == 'fallback'
        # Biến thể prompt phải là "no_query": lượt này không có sự cố nào để kể với người dùng.
        assert chon['kind'] == 'no_query'
        # Không có SQL nên cũng không được có event `sql` / `sql-data` nào trên dây.
        assert not ({'sql', 'sql-data'} & set(event_types(chunks)))

    def test_chon_sql_thi_van_chay_pha_sql_va_co_dau_vet(self, run_task_service, monkeypatch):
        goi, chon, chunks = self._chay(
            run_task_service, monkeypatch,
            RouteDecision(action='sql', category='need_data', reason='cần số liệu'))
        assert goi == [True]
        assert chon['nhanh'] == 'thuong'
        msgs = [e.get('msg', '') for e in parse_events(chunks) if e.get('type') == 'info']
        assert any('route sql/need_data' in m for m in msgs)

    def test_nhanh_mcp_khong_bao_gio_bi_re(self, run_task_service, monkeypatch):
        """Cả pha answer nằm trong `if in_chat`, nên rẽ MCP sang answer là trả về rỗng lặng lẽ."""
        goi, _, _ = self._chay(run_task_service, monkeypatch, None,
                               in_chat=False, finish_step=ChatFinishStep.GENERATE_ANSWER)
        assert goi == [True]

    def test_finish_step_dung_som_khong_bi_re(self, run_task_service, monkeypatch):
        """Người gọi đã nói rõ họ chỉ cần SQL hoặc dữ liệu thì không có gì để định tuyến."""
        goi, _, _ = self._chay(run_task_service, monkeypatch, None,
                               finish_step=ChatFinishStep.QUERY_DATA)
        assert goi == [True]
