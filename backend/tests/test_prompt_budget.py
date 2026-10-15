"""Test cho thang rút gọn khối thông tin CSDL khi model từ chối prompt sinh SQL vì vượt context.

Hai lớp:

- các nấc rút gọn và ``MSchema.render`` — hàm thuần, dữ liệu dựng tay;
- bộ điều phối trong ``LLMService.generate_sql`` — ``LLMService`` dựng bằng ``object.__new__``
  như ``test_sql_phase.py``, ``ChatQuestion`` và template prompt là thật, LLM là đồ giả từ chối
  prompt N lần đầu rồi mới trả lời, đồng thời chụp lại khối schema của từng lần gọi.
"""

# Import sqlbot_xpack TRƯỚC: apps.chat.* nằm trong một vòng import lẫn nhau, chỉ gỡ được khi xpack
# là module khởi động vòng đó.
import sqlbot_xpack  # noqa: F401  isort:skip

import types

import httpx
import openai
import pytest
from langchain_core.messages import AIMessageChunk

from apps.chat.models.chat_model import (
    ChatFinishStep,
    ChatQuestion,
    HumanPromptMessage,
    SystemPromptMessage,
)
from apps.chat.task import llm as llm_mod
from apps.chat.task.llm import LLMService
from apps.chat.task.prompt_budget import (
    SchemaPrompt,
    SchemaReductionExhausted,
    drop_pulled_comments,
    drop_pulled_tables,
    drop_sample_data,
    drop_selected_comments,
    is_prompt_rejection,
    next_reduction,
)
from apps.datasource.crud import datasource as ds_mod
from apps.datasource.m_schema import (
    MSchema,
    MSchemaField,
    MSchemaForeignKey,
    MSchemaTable,
)

REQUEST = httpx.Request("POST", "http://litellm/v1/chat/completions")


def http_error(cls, status, message):
    """Dựng lỗi HTTP của openai SDK như LiteLLM trả về."""
    return cls(message, response=httpx.Response(status, request=REQUEST), body=None)


def context_error():
    """400 vượt context, kiểu vLLM qua LiteLLM."""
    return http_error(openai.BadRequestError, 400,
                      "ContextWindowExceededError: maximum context length is 32768 tokens")


ORDERS = MSchemaTable(
    name='orders', title='public.orders', comment='Đơn hàng',
    fields=(MSchemaField('id', 'int'), MSchemaField('customer_id', 'int', 'Mã khách')))
CUSTOMERS = MSchemaTable(
    name='customers', title='public.customers',
    fields=(MSchemaField('id', 'int', 'Mã'), MSchemaField('name', 'text', 'Tên')), pulled=True)
ITEMS = MSchemaTable(
    name='items', title='public.items', comment='Dòng hàng',
    fields=(MSchemaField('order_id', 'int', 'Đơn'),))
FK_ORDER_CUSTOMER = MSchemaForeignKey('orders.customer_id=customers.id', ('orders', 'customers'))
FK_ITEM_ORDER = MSchemaForeignKey('items.order_id=orders.id', ('items', 'orders'))
M_SCHEMA = MSchema(db_name='public', tables=(ORDERS, ITEMS, CUSTOMERS),
                   foreign_keys=(FK_ORDER_CUSTOMER, FK_ITEM_ORDER))
PROMPT = SchemaPrompt(M_SCHEMA, '# Table: orders\n[{"id": 1}]')


class TestRender:
    """``MSchema.render`` sinh đúng định dạng chuỗi M-Schema cũ."""

    def test_render_day_du(self):
        assert M_SCHEMA.render() == (
            "【DB_ID】 public\n【Schema】\n"
            "# Table: public.orders, Đơn hàng\n[\n(id:int),\n(customer_id:int, Mã khách)\n]\n"
            "# Table: public.items, Dòng hàng\n[\n(order_id:int, Đơn)\n]\n"
            "# Table: public.customers\n[\n(id:int, Mã),\n(name:text, Tên)\n]\n"
            "【Foreign keys】\norders.customer_id=customers.id\nitems.order_id=orders.id\n"
        )

    def test_bang_khong_cot_va_schema_rong(self):
        empty = MSchemaTable(name='t', title='t')
        assert MSchema(db_name=None, tables=(empty,)).render() == \
            "【DB_ID】 None\n【Schema】\n# Table: t\n[\n\n]\n"
        assert MSchema(db_name='x').render() == "【DB_ID】 x\n【Schema】\n"


class TestGetTableSchema:
    """``get_table_schema`` (lớp bọc chuỗi) giữ nguyên đầu ra; bảng kéo theo được đánh dấu."""

    @staticmethod
    def _objs():
        def field(fid, name, comment=None):
            return types.SimpleNamespace(id=fid, field_name=name, field_type='int',
                                         custom_comment=comment)

        def table(tid, name, fields, comment=None):
            return types.SimpleNamespace(
                schema='public', fields=fields,
                table=types.SimpleNamespace(id=tid, table_name=name, custom_comment=comment,
                                            embedding=None))

        return [table(1, 'orders', [field(11, 'id'), field(12, 'customer_id', ' Mã khách ')],
                      ' Đơn hàng '),
                table(2, 'customers', [field(21, 'id', 'Mã'), field(22, 'secret', 'Bí mật')])]

    @pytest.fixture
    def patched(self, monkeypatch):
        objs = self._objs()
        monkeypatch.setattr(ds_mod, 'get_table_obj_by_ds', lambda session, current_user, ds: objs)
        # embedding chỉ chọn bảng đầu tiên
        monkeypatch.setattr(ds_mod, 'calc_table_embedding',
                            lambda tables, question, ds_id=None: [dict(tables[0])])
        monkeypatch.setattr(ds_mod.settings, 'TABLE_EMBEDDING_ENABLED', True)
        relation = [{'shape': 'edge', 'source': {'cell': 1, 'port': '12'},
                     'target': {'cell': '2', 'port': 21}}]
        return types.SimpleNamespace(id=9, type='pg', table_relation=relation)

    def test_chuoi_giong_ban_cu_va_bang_keo_theo_co_co(self, patched):
        schema, names = ds_mod.get_table_schema(None, None, patched, 'q',
                                                column_filter={'customers': {'id'}})
        assert schema == (
            "【DB_ID】 public\n【Schema】\n"
            "# Table: public.orders, Đơn hàng\n[\n(id:int),\n(customer_id:int, Mã khách)\n]\n"
            "# Table: public.customers\n[\n(id:int, Mã)\n]\n"
            "【Foreign keys】\norders.customer_id=customers.id\n"
        )
        assert names == ['orders', 'customers']

        m_schema, _ = ds_mod.get_table_m_schema(None, None, patched, 'q')
        assert [(t.name, t.pulled) for t in m_schema.tables] == [('orders', False),
                                                                  ('customers', True)]
        assert m_schema.foreign_keys[0].tables == ('orders', 'customers')

    def test_khong_co_bang_nao(self, monkeypatch, patched):
        monkeypatch.setattr(ds_mod, 'get_table_obj_by_ds', lambda session, current_user, ds: [])
        assert ds_mod.get_table_schema(None, None, patched, 'q') == ("", [])


class TestCacNac:
    """Từng nấc là hàm thuần; nấc không có gì để bỏ trả None."""

    def test_bo_sample_data(self):
        assert drop_sample_data(PROMPT).sample_data == ''
        assert drop_sample_data(SchemaPrompt(M_SCHEMA, '')) is None

    def test_bo_chu_thich_cot_bang_keo_theo_giu_chu_thich_bang(self):
        reduced = drop_pulled_comments(PROMPT).m_schema
        assert reduced.tables[2].fields == (MSchemaField('id', 'int'), MSchemaField('name', 'text'))
        assert reduced.tables[:2] == (ORDERS, ITEMS)
        pulled_with_comment = MSchemaTable(name='c', title='c', comment='Bảng C',
                                           fields=(MSchemaField('id', 'int'),), pulled=True)
        assert drop_pulled_comments(SchemaPrompt(MSchema('x', (ORDERS, pulled_with_comment)))) is None

    def test_bo_bang_keo_theo_cung_fk_cham_toi(self):
        reduced = drop_pulled_tables(PROMPT).m_schema
        assert reduced.tables == (ORDERS, ITEMS)
        assert reduced.foreign_keys == (FK_ITEM_ORDER,)
        assert drop_pulled_tables(SchemaPrompt(MSchema('x', (ORDERS, ITEMS)))) is None

    def test_bo_chu_thich_cot_bang_duoc_chon(self):
        reduced = drop_selected_comments(PROMPT).m_schema
        assert reduced.tables[0] == MSchemaTable(
            name='orders', title='public.orders', comment='Đơn hàng',
            fields=(MSchemaField('id', 'int'), MSchemaField('customer_id', 'int')))
        assert reduced.tables[2] == CUSTOMERS
        assert drop_selected_comments(SchemaPrompt(MSchema('x', (CUSTOMERS,)))) is None

    def test_next_reduction_nhay_qua_nac_khong_tac_dung(self):
        no_pulled = SchemaPrompt(MSchema('x', (ORDERS,)), '')
        index, name, reduced = next_reduction(no_pulled, 0)
        assert (index, name) == (4, 'drop_selected_comments')
        assert next_reduction(reduced, index) is None


class TestPhanLoaiLoi:
    def test_400_chua_phan_loai_moi_la_tu_choi_prompt(self):
        assert is_prompt_rejection(context_error())
        wrapped = RuntimeError('langchain')
        wrapped.__cause__ = context_error()
        assert is_prompt_rejection(wrapped)

    def test_het_ngan_sach_va_loi_khac_khong_phai(self):
        assert not is_prompt_rejection(
            http_error(openai.BadRequestError, 400, 'Budget has been exceeded'))
        assert not is_prompt_rejection(http_error(openai.RateLimitError, 429, 'slow down'))
        assert not is_prompt_rejection(ValueError('x'))


class FakeLLM:
    """LLM giả: ``failures`` lần gọi đầu ném lỗi, sau đó stream câu trả lời.

    ``schemas`` chụp nội dung message schema ở mỗi lần gọi; ``fail_after_chunk`` ném lỗi sau khi
    đã stream một chunk để kiểm nhánh "đã gửi đi thì không rút gọn".
    """

    def __init__(self, failures, error_factory=context_error, fail_after_chunk=False):
        self.failures = failures
        self.error_factory = error_factory
        self.fail_after_chunk = fail_after_chunk
        self.schemas = []

    def stream(self, messages):
        self.schemas.append(next(m.content for m in messages if isinstance(m, HumanPromptMessage)
                                 and '<m-schema>' in m.content))
        call = len(self.schemas)

        def gen():
            if self.fail_after_chunk:
                yield AIMessageChunk(content='{"success"')
                raise self.error_factory()
            if call <= self.failures:
                raise self.error_factory()
            yield AIMessageChunk(content='{"success":true,"sql":"SELECT 1","tables":["orders"]}')

        return gen()


@pytest.fixture
def service(monkeypatch):
    """``LLMService`` chỉ đủ sống cho ``generate_sql``, với prompt đã dựng như ``init_messages``."""
    s = object.__new__(LLMService)
    s.ds = types.SimpleNamespace(id=159, type='pg')
    s.record = types.SimpleNamespace(id=7)
    s.change_title = False
    s.current_logs = {}
    s.chat_question = ChatQuestion(chat_id=1, question='Có bao nhiêu đơn hàng?', engine='PostgreSQL',
                                   db_schema=M_SCHEMA.render(), sample_data=PROMPT.sample_data)
    s.schema_prompt = PROMPT
    s.schema_reduction_index = 0
    s.schema_reductions = ()
    templates = s.chat_question.sql_sys_question(s.ds.type, s.enable_sql_row_limit)
    s.sql_schema_message = HumanPromptMessage(content=templates['schema'])
    s.sql_message = [SystemPromptMessage(content=templates['system']), s.sql_schema_message]

    monkeypatch.setattr(llm_mod, 'start_log', lambda **kw: None)
    monkeypatch.setattr(llm_mod, 'end_log', lambda **kw: None)
    monkeypatch.setattr(llm_mod, 'save_sql_answer', lambda **kw: s.record)
    return s


def run(service, retry_reason=None):
    return list(service.generate_sql(None, retry_reason))


class TestDieuPhoi:
    """Bộ điều phối quanh lời gọi stream trong ``generate_sql``."""

    def test_khong_loi_thi_prompt_giu_nguyen(self, service):
        service.llm = FakeLLM(failures=0)
        before = service.sql_schema_message.content
        run(service)
        assert service.llm.schemas == [before]
        assert service.schema_reductions == ()

    def test_len_tung_nac_dung_thu_tu_den_khi_vua(self, service):
        service.llm = FakeLLM(failures=3)
        chunks = run(service)

        assert chunks[-1]['content'].startswith('{"success":true')
        assert service.schema_reductions == ('drop_sample_data', 'drop_pulled_comments',
                                             'drop_pulled_tables')
        first, after_sample, after_comments, after_tables = service.llm.schemas
        assert '# Table: orders' in first and '# Table: orders' not in after_sample
        assert '(id:int, Mã)' in after_sample and '(id:int, Mã)' not in after_comments
        assert 'public.customers' in after_comments and 'public.customers' not in after_tables
        assert 'orders.customer_id=customers.id' not in after_tables
        assert '(customer_id:int, Mã khách)' in after_tables
        # các prompt chạy sau trong lượt (chart, answer, gợi ý) thấy bản đã rút gọn
        assert service.chat_question.sample_data == ''
        assert 'customers' not in service.chat_question.db_schema
        assert service.sql_message[1] is service.sql_schema_message

    def test_het_nac_thi_bao_loi_rieng_khong_traceback(self, service):
        service.llm = FakeLLM(failures=99)
        with pytest.raises(SchemaReductionExhausted) as info:
            run(service)
        assert len(service.llm.schemas) == 5
        assert 'bỏ chú thích cột của bảng được chọn' in str(info.value)
        assert isinstance(info.value.__cause__, openai.BadRequestError)

    def test_thu_lai_sql_cung_luot_dung_nac_da_dat(self, service):
        service.llm = FakeLLM(failures=1)
        run(service)
        service.llm = FakeLLM(failures=0)
        run(service, retry_reason='SQL sai')
        assert '# Table: orders' not in service.llm.schemas[0]
        assert service.schema_reductions == ('drop_sample_data',)

    @pytest.mark.parametrize('llm', [
        FakeLLM(failures=1, error_factory=lambda: http_error(
            openai.BadRequestError, 400, 'Budget has been exceeded')),
        FakeLLM(failures=1, error_factory=lambda: http_error(
            openai.InternalServerError, 500, 'down')),
        FakeLLM(failures=0, fail_after_chunk=True),
    ])
    def test_loi_khong_do_prompt_hoac_da_stream_thi_khong_rut_gon(self, service, llm):
        service.llm = llm
        with pytest.raises(openai.APIStatusError):
            run(service)
        assert len(llm.schemas) == 1
        assert service.schema_reductions == ()

    def test_khong_rut_gon_duoc_thi_nem_loi_goc(self, service):
        service.schema_prompt = None  # datasource của assistant bên ngoài
        service.llm = FakeLLM(failures=1)
        with pytest.raises(openai.BadRequestError):
            run(service)


@pytest.fixture
def task_service(monkeypatch):
    """``LLMService`` đủ để chạy ``run_task`` tới khối except tổng."""
    s = object.__new__(LLMService)
    s.ds = types.SimpleNamespace(id=1, type='pg', name='ds', oid=1)
    s.record = types.SimpleNamespace(id=7, chat_id=3)
    s.current_user = types.SimpleNamespace(id=1)
    s.current_assistant = None
    s.sw_permission = None
    s.change_title = False
    s.current_logs = {}
    s.chat_question = types.SimpleNamespace(question='q', datasource_ids=None, include_answer=False,
                                            include_chart=False, extended_response=False)
    s.out_ds_instance = None
    for name in ('filter_terminology_template', 'filter_training_template',
                 'filter_custom_prompts', 'init_messages', 'validate_history_ds', 'finish'):
        setattr(s, name, lambda *a, **k: None)
    s.get_record = lambda: types.SimpleNamespace(id=7, chat_id=3, question='q',
                                                 regenerate_record_id=None)
    s.trans = lambda key: key
    s.calls = 0

    def generate_sql(_session, **_kw):
        s.calls += 1
        raise SchemaReductionExhausted(['drop_sample_data'])
        yield  # không bao giờ tới: giữ hàm là generator

    s.generate_sql = generate_sql
    s.saved_errors = []
    s.save_error = lambda session, message: s.saved_errors.append(message)

    class FakeSessionMaker:
        def __call__(self):
            return None

        def remove(self):
            return None

    monkeypatch.setattr(llm_mod, 'check_connection', lambda ds, trans=None: True)
    monkeypatch.setattr(llm_mod, 'session_maker', FakeSessionMaker())
    monkeypatch.setattr(llm_mod, 'start_log', lambda **kw: None)
    monkeypatch.setattr(llm_mod, 'end_log', lambda **kw: None)
    monkeypatch.setattr(llm_mod.settings, 'LLM_SQL_MAX_RETRY', 1)
    return s


def test_het_nac_khong_thu_lai_va_tra_thong_diep_tran(task_service):
    """Pha SQL không thử lại; ``/chat/ask`` nhận thông điệp thuần, không traceback, không code."""
    chunks = list(task_service.run_task(in_chat=False, stream=False,
                                        finish_step=ChatFinishStep.QUERY_DATA))
    final = chunks[-1]
    assert task_service.calls == 1
    assert final['success'] is False
    assert final['message'].startswith('Thông tin CSDL vượt quá context của model')
    assert 'traceback' not in final['message']
    assert 'code' not in final
