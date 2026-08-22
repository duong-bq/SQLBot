import concurrent
import json
import os
import queue
import traceback
import urllib.parse
import warnings
from concurrent.futures import ThreadPoolExecutor, Future
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Generator, List, Optional, Union, Dict, Iterator

import orjson
import pandas as pd
import requests
import sqlglot
import sqlparse
from langchain.chat_models.base import BaseChatModel
from langchain_community.utilities import SQLDatabase
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, BaseMessageChunk
from sqlalchemy import and_, select
from sqlalchemy.orm import sessionmaker, scoped_session
from sqlbot_xpack.config.model import SysArgModel
from sqlbot_xpack.custom_prompt.curd.custom_prompt import find_custom_prompts
from sqlbot_xpack.custom_prompt.models.custom_prompt_model import CustomPromptTypeEnum
from sqlbot_xpack.license.license_manage import SQLBotLicenseUtil
from sqlglot import exp
from sqlmodel import Session

from apps.ai_model.model_factory import LLMConfig, LLMFactory, get_default_config
from apps.chat.curd.chat import save_question, save_sql_answer, save_sql, \
    save_error_message, save_sql_exec_data, save_chart_answer, save_chart, \
    finish_record, save_analysis_answer, save_predict_answer, save_predict_data, \
    save_select_datasource_answer, save_recommend_question_answer, \
    get_old_questions, save_analysis_predict_record, rename_chat, get_chart_config, \
    get_chat_chart_data, list_generate_sql_logs, list_generate_chart_logs, start_log, end_log, \
    get_last_execute_sql_error, format_json_data, format_chart_fields, get_chat_brief_generate, get_chat_predict_data, \
    trim_chat_brief, \
    get_chat_chart_config, trigger_log_error, save_answer, get_recent_qa_history, \
    get_user_old_questions, save_record_recommend_questions
from apps.chat.models.chat_model import ChatQuestion, ChatRecord, Chat, RenameChat, ChatLog, OperationEnum, \
    ChatFinishStep, AxisObj, SystemPromptMessage, HumanPromptMessage, AIPromptMessage
from apps.chat.permission.sw_permission import resolve_sw_permission, apply_scope_to_sql
from apps.chat.task.question_gate import decide_route, fail_open
from apps.data_training.curd.data_training import get_training_template
from apps.datasource.crud.datasource import get_table_schema, get_tables_sample_data, usable_ds_condition
from apps.datasource.crud.permission import get_row_permission_filters, is_normal_user
from apps.datasource.embedding.ds_embedding import get_ds_embedding
from apps.datasource.models.datasource import CoreDatasource
from apps.db.db import exec_sql, get_version, check_connection, get_sqlglot_dialect
from apps.system.crud.aimodel_manage import get_ai_model_list_by_workspace
from apps.system.crud.assistant import AssistantOutDs, AssistantOutDsFactory, get_assistant_ds
from apps.system.crud.parameter_manage import get_groups
from apps.system.crud.user import user_ws_list
from apps.system.schemas.system_schema import AssistantOutDsSchema
from apps.terminology.curd.terminology import get_terminology_template
from common.core.config import settings
from common.core.db import engine
from common.core.deps import CurrentAssistant, CurrentUser
from common.error import SingleMessageError, SQLBotDBError, ParseSQLResultError, SQLBotDBConnectionError
from common.utils.data_format import DataFormat
from common.utils.locale import I18n, I18nHelper
from common.utils.utils import SQLBotLogUtil, extract_nested_json, extract_json_object, prepare_for_orjson

warnings.filterwarnings("ignore")

executor = ThreadPoolExecutor(max_workers=200)

dynamic_ds_types = [1, 3]
dynamic_subsql_prefix = 'select * from sqlbot_dynamic_temp_table_'

# Số dòng dữ liệu tối đa nhồi vào prompt của pha answer. SQL đã bị chặn ở 1000 dòng, nhưng nhồi cả
# 1000 dòng vào prompt chỉ làm chậm và tốn token: câu trả lời chữ chỉ cần nêu vài điểm nổi bật chứ
# không đọc hết bảng. Cắt bớt ở đây không ảnh hưởng bảng dữ liệu và biểu đồ mà client nhận được.
#
# CẢNH BÁO: cắt dòng mà không nói cho LLM biết đã cắt thì nó đếm số dòng nhận được rồi báo đó là
# tổng — đo bằng bộ test HĐND: SQL trả 136 dòng, câu trả lời khẳng định "tổng cộng 72 nghị quyết".
# Vì vậy mọi chỗ cắt dòng phải đi kèm `build_data_scope_note`.
ANSWER_MAX_ROWS = 100


def build_data_scope_note(total_rows: int, sent_rows: int) -> str:
    """Dựng câu mô tả phạm vi dữ liệu để LLM biết khối <data> có phải toàn bộ kết quả hay không.

    Lý do phải có: pha answer chỉ nhận ANSWER_MAX_ROWS dòng đầu, nhưng prompt lại yêu cầu "chỉ dùng
    con số có trong <data>". LLM không có cách nào biết mình đang xem bản cắt, nên khi cần một con số
    tổng nó tự đếm số dòng nhận được — ra đúng bằng số dòng bị cắt còn lại. Đây là lỗi nguy hiểm nhất
    của pha answer vì SQL hoàn toàn đúng, người dùng không có dấu hiệu nào để nghi ngờ.

    Nêu tổng thật kể cả khi KHÔNG cắt: có tổng cho sẵn thì LLM khỏi phải tự đếm, tránh luôn lỗi cộng
    sai khi liệt kê (đo được: liệt kê đúng đủ 15 tên nhưng mở đầu viết "gồm 16 cán bộ").
    """
    if total_rows <= 0:
        return 'Truy vấn không trả về dòng dữ liệu nào.'
    if sent_rows >= total_rows:
        return (f'Truy vấn trả về tổng cộng {total_rows} dòng. Khối <data> chứa ĐỦ toàn bộ {total_rows} '
                f'dòng, không bị cắt bớt.')
    return (f'Truy vấn trả về tổng cộng {total_rows} dòng. Khối <data> CHỈ chứa {sent_rows} dòng đầu '
            f'tiên, {total_rows - sent_rows} dòng còn lại đã bị cắt bỏ để tiết kiệm ngữ cảnh và KHÔNG '
            f'được gửi cho bạn.')

session_maker = scoped_session(sessionmaker(bind=engine, class_=Session))

i18n = I18n()


def extract_tables_from_sql(sql: str, ds_type: str = None) -> set:
    """Trích xuất tên bảng thật từ SQL (parse bằng sqlglot, đáng tin cậy).

    Loại bỏ tên CTE (WITH ... AS): đó là tập kết quả tạm do chính câu SQL này định nghĩa, không
    phải bảng trong database. sqlglot parse tham chiếu tới CTE cũng thành exp.Table; nếu không
    loại ra, bước kiểm tra an toàn sẽ coi tên CTE là "bảng chưa được cấp phép" và từ chối MỌI câu
    SQL có WITH. Bảng thật nằm trong thân CTE là các node exp.Table riêng biệt nên vẫn được trích
    xuất — kể cả khi một CTE được build từ bảng chưa cấp phép, bảng đó vẫn bị kiểm tra.

    Dùng ``cte.alias_or_name`` chứ không phải ``cte.alias``: bản vá upstream 7118b401a sửa cùng lỗi này
    nhưng lọc bằng ``if cte.alias``, nên CTE nào sqlglot không gán được alias sẽ lọt qua và lại bị coi
    là bảng.

    **NÉM exception khi không parse được**, thay vì nuốt lỗi rồi trả set rỗng như trước. Set rỗng
    phải mang đúng một nghĩa: "câu SQL này không tham chiếu bảng nào". Nếu gộp cả ca parse hỏng vào
    đó thì phía gọi buộc phải coi set rỗng là đáng ngờ, và mọi câu SQL hợp lệ nhưng không đọc bảng
    (vd `SELECT COUNT(*) FROM (SELECT 'a' UNION ALL SELECT 'b') t`) đều bị từ chối oan.

    Cố ý GIỮ nhánh này khi merge từ upstream: bản upstream bọc toàn bộ thân hàm trong
    ``try/except Exception: pass``. Nuốt lỗi parse ở đây là lỗ hổng — set rỗng được phía gọi hiểu là
    "không tham chiếu bảng nào" và cho chạy, nên mọi câu SQL không parse được sẽ đi vòng qua bước
    kiểm tra danh sách bảng được phép.
    """
    dialect = get_sqlglot_dialect(ds_type)
    statements = [stmt for stmt in sqlglot.parse(sql, dialect=dialect) if stmt]
    if not statements:
        raise ValueError('SQL is empty or contains no statement')
    tables = set()
    for stmt in statements:
        cte_names = {cte.alias_or_name for cte in stmt.find_all(exp.CTE)}
        for table in stmt.find_all(exp.Table):
            if table.name and table.name not in cte_names:
                tables.add(table.name)
    return tables


def _cap_history_answer(text: str) -> str:
    """Chặn trần độ dài câu trả lời của LLM trước khi đưa vào lịch sử hội thoại.

    Từ khi prompt yêu cầu model suy luận, phần suy luận chỉ nằm ngoài `content` khi model bọc nó
    trong <think>. Không bọc thì toàn bộ suy luận rơi vào `content`, và đã gặp ca model lặp vô hạn
    xuất 94.731 ký tự. Nhét nguyên chuỗi đó vào `sql_message` thì lượt kế tiếp phát lại là vỡ
    context window — và hỏng vĩnh viễn, vì message độc đã được `end_log` lưu vào chuỗi log.

    Mặc định CHỈ cắt theo độ dài. Bật `LLM_SQL_HISTORY_JSON_ONLY` thì rút về riêng khối JSON, bỏ
    phần văn xuôi suy luận — xem chú thích của biến đó về lý do. Trần độ dài vẫn áp cho cả hai
    nhánh: khi không tìm được JSON (model chưa xuất ra khối nào thì đã chết giữa stream) thì phải
    còn đường lùi, và bản thân câu SQL cũng có thể dài bất thường.

    Đừng chọn nhánh nào dựa trên cảm giác: đây là biến phải đo bằng `backend/scripts/eval_text2sql`.
    Từng thử đo tay 6 lượt hỏi và kết luận sai — khác biệt không có ý nghĩa thống kê (Fisher p≈0.5),
    chạy lại đúng cấu hình đó còn cho kết quả khác.
    """
    stripped = (text or '').strip()
    if settings.LLM_SQL_HISTORY_JSON_ONLY:
        json_str = extract_json_object(stripped, required_keys=('success',), prefer_last=True)
        if json_str:
            stripped = json_str
    limit = settings.LLM_SQL_HISTORY_ANSWER_MAX_CHARS
    if len(stripped) <= limit:
        return stripped
    return stripped[:limit] + '\n...[nội dung quá dài, đã cắt bớt]'


def _humanize_error(message: str) -> str:
    """Rút chuỗi lỗi nội bộ về đúng phần người đọc hiểu được, bỏ traceback.

    Các `SingleMessageError` trong file này không thống nhất định dạng: chỗ raise chuỗi thuần, chỗ
    raise JSON `{"message": ..., "traceback": ...}`. Hàm này gom cả hai về một dạng để prompt
    fallback không bị dính traceback — LLM đọc thấy traceback là chép nguyên thuật ngữ kỹ thuật
    vào câu trả lời cho người dùng cuối.
    """
    if not message:
        return ''
    text = message.strip()
    if text.startswith('{'):
        try:
            obj = orjson.loads(text)
            if isinstance(obj, dict) and obj.get('message'):
                return str(obj['message'])
        except Exception:
            pass
    return text


@dataclass
class SqlPhaseResult:
    """Kết quả pha SQL, trả về qua kênh ``return`` của ``_sql_phase``.

    Gói đúng những giá trị mà các pha sau (answer, chart) đọc, thay cho cách
    cũ là pha SQL ghi thẳng vào biến khai báo sẵn ở scope ngoài của
    ``run_task``.

    ``stopped`` báo pha đã tự kết thúc request vì ``finish_step`` dừng sớm ở
    ``GENERATE_SQL``: nó đã phát nốt các event cuối, người gọi phải dừng ngay
    chứ không chạy tiếp pha nào. ``degraded_reason`` khác None nghĩa là hết
    lượt thử vẫn hỏng — lượt này không có dữ liệu mới, pha answer phải dùng
    prompt fallback.
    """

    result: Optional[Dict[str, Any]] = None
    tables: Optional[List[str]] = None
    chart_type: Optional[str] = None
    data: Any = None
    degraded_reason: Optional[str] = None
    stopped: bool = False


class LLMService:
    ds: CoreDatasource
    chat_question: ChatQuestion
    oid: int
    record: ChatRecord
    config: LLMConfig
    llm: BaseChatModel
    # Model của cổng định tuyến. Trỏ về chính `config`/`llm` khi LLM_ROUTE_MODEL_ID = 0.
    route_config: LLMConfig
    route_llm: BaseChatModel
    sql_message: List[Union[BaseMessage, dict[str, Any]]]
    chart_message: List[Union[BaseMessage, dict[str, Any]]]

    # session: Session = db_session
    current_user: CurrentUser
    current_assistant: Optional[CurrentAssistant] = None
    out_ds_instance: Optional[AssistantOutDs] = None
    change_title: bool = False

    generate_sql_logs: List[ChatLog]
    generate_chart_logs: List[ChatLog]
    current_logs: dict[OperationEnum, ChatLog]
    chunk_list: List[str]
    future: Future

    trans: I18nHelper = None

    last_execute_sql_error: str = None
    articles_number: int = 4

    enable_sql_row_limit: bool = settings.GENERATE_SQL_QUERY_LIMIT_ENABLED
    base_message_round_count_limit: int = settings.GENERATE_SQL_QUERY_HISTORY_ROUND_COUNT

    def __init__(self, session: Session, current_user: CurrentUser, chat_question: ChatQuestion,
                 current_assistant: Optional[CurrentAssistant] = None, no_reasoning: bool = False,
                 embedding: bool = False, config: LLMConfig = None,
                 route_config: LLMConfig = None):
        self.sql_message = []
        self.chart_message = []
        self.generate_sql_logs = []
        self.generate_chart_logs = []
        self.current_logs = {}
        self.chunk_list = []
        self.current_user = current_user
        self.current_assistant = current_assistant

        self.table_name_list = []
        # Quyết định quyền SW của lượt này (resolve lười trong get_sw_permission). None = chưa
        # resolve; sau khi resolve luôn là SwPermission (restricted hoặc không).
        self.sw_permission = None

        chat_question.lang = get_lang_name(current_user.language)
        self.trans = i18n(lang=current_user.language)

        chat_id = chat_question.chat_id
        chat: Chat | None = session.get(Chat, chat_id)
        if not chat:
            raise SingleMessageError(f"Chat with id {chat_id} not found")
        self.oid = chat.oid

        if self.oid and not current_assistant:
            w_list = user_ws_list(session, self.current_user.id)
            oid_list = [item.id for item in w_list]
            if int(self.oid) not in oid_list:
                raise SingleMessageError("Current user cannot not access this chat")
        if self.oid and current_assistant:
            if self.oid != self.current_user.oid:
                raise SingleMessageError("Current assistant user cannot not access this chat")

        self.current_user.oid = chat.oid
        ds: CoreDatasource | AssistantOutDsSchema | None = None
        if not chat.datasource and chat_question.datasource_id:
            _ds = session.get(CoreDatasource, chat_question.datasource_id)
            if _ds:
                if _ds.oid != self.oid:
                    raise SingleMessageError(
                        f"Datasource with id {chat_question.datasource_id} does not belong to current workspace")
                chat.datasource = _ds.id
                chat.engine_type = _ds.type_name
                # save chat
                session.add(chat)
                session.flush()
                session.refresh(chat)
                session.commit()

        if chat.datasource:
            # Get available datasource
            if current_assistant and current_assistant.type in dynamic_ds_types:
                self.out_ds_instance = AssistantOutDsFactory.get_instance(current_assistant)
                ds = self.out_ds_instance.get_ds(chat.datasource)
                if not ds:
                    raise SingleMessageError("No available datasource configuration found")
                chat_question.engine = ds.type + get_version(ds)
            else:
                ds = session.get(CoreDatasource, chat.datasource)
                if not ds:
                    raise SingleMessageError("No available datasource configuration found")
                chat_question.engine = (ds.type_name if ds.type != 'excel' else 'PostgreSQL') + get_version(ds)

        self.generate_sql_logs = list_generate_sql_logs(session=session, chart_id=chat_id)
        self.generate_chart_logs = list_generate_chart_logs(session=session, chart_id=chat_id)

        self.change_title = not get_chat_brief_generate(session=session, chat_id=chat_id)

        self.ds = (
            ds if isinstance(ds, AssistantOutDsSchema) else CoreDatasource(**ds.model_dump())) if ds else None
        self.chat_question = chat_question
        self.config = config
        # Việc tắt thinking đã chuyển về apply_disable_thinking() trong model_factory (áp dụng cho
        # mọi lời gọi LLM, điều khiển bằng settings.LLM_DISABLE_THINKING). Tham số no_reasoning giữ
        # lại cho tương thích chữ ký nhưng không còn tác dụng.

        self.chat_question.ai_modal_id = self.config.model_id
        self.chat_question.ai_modal_name = self.config.model_name

        # Create LLM instance through factory
        llm_instance = LLMFactory.create_llm(self.config)
        self.llm = llm_instance.llm

        # Cổng định tuyến chỉ phân loại một câu hỏi ngắn nên chạy được trên model nhỏ. Không tách
        # thành LLMService riêng: cổng dùng chung câu hỏi, danh sách bảng và record của lượt này,
        # chỉ khác đúng cái model.
        self.route_config = route_config or self.config
        self.route_llm = LLMFactory.create_llm(route_config).llm if route_config else self.llm

        # get last_execute_sql_error
        last_execute_sql_error = get_last_execute_sql_error(session, self.chat_question.chat_id)
        if last_execute_sql_error:
            self.chat_question.error_msg = f'''<error-msg>
{last_execute_sql_error}
</error-msg>'''
        else:
            self.chat_question.error_msg = ''

    @classmethod
    async def create(cls, *args, **kwargs):
        specialized_model_id = None
        _ai_model_list = []
        if args[3]:
            if args[1]:
                ws_id = args[1].oid
                _ai_model_list = get_ai_model_list_by_workspace(args[0], ws_id)
            if args[3].enable_custom_model:
                if args[3].custom_model:
                    if any(str(model.id) == str(args[3].custom_model) for model in _ai_model_list):
                        specialized_model_id = args[3].custom_model
                        print("use custom model: id[" + specialized_model_id + "]")
        config: LLMConfig = await get_default_config(specialized_model_id)
        # Phải resolve ở đây chứ không trong __init__: get_default_config là async còn __init__ thì
        # không, và run_task lại là generator đồng bộ nên không await được ở bất kỳ chỗ nào sau này.
        route_config: Optional[LLMConfig] = None
        if settings.LLM_ROUTE_ENABLED and settings.LLM_ROUTE_MODEL_ID:
            try:
                route_config = await get_default_config(settings.LLM_ROUTE_MODEL_ID)
            except Exception as e:
                # Cấu hình sai model cho cổng không được giết lượt hỏi: rơi về model chính.
                SQLBotLogUtil.error(f'[route] load route model failed, use main model: {e}')
        instance = cls(*args, **kwargs, config=config, route_config=route_config)

        chat_params: list[SysArgModel] = await get_groups(args[0], "chat")
        for config in chat_params:
            if config.pkey == 'chat.sqlbot_name':
                if config.pval.strip():
                    instance.chat_question.sqlbot_name = config.pval
            if config.pkey == 'chat.limit_rows':
                if config.pval.lower().strip() == 'true':
                    instance.enable_sql_row_limit = True
                else:
                    instance.enable_sql_row_limit = False
            if config.pkey == 'chat.context_record_count':
                count_value = config.pval
                if count_value is None:
                    count_value = settings.GENERATE_SQL_QUERY_HISTORY_ROUND_COUNT
                count_value = int(count_value)
                if count_value < 0:
                    count_value = 0
                instance.base_message_round_count_limit = count_value
        return instance

    def is_running(self, timeout=0.5):
        try:
            r = concurrent.futures.wait([self.future], timeout)
            if len(r.not_done) > 0:
                return True
            else:
                return False
        except Exception as e:
            return True

    def init_messages(self, session: Session):

        self.table_name_list = self.choose_table_schema(session)

        last_sql_messages: List[dict[str, Any]] = self.generate_sql_logs[-1].messages if len(
            self.generate_sql_logs) > 0 else []
        if self.chat_question.regenerate_record_id:
            # filter record before regenerate_record_id
            _temp_log = next(
                filter(lambda obj: obj.pid == self.chat_question.regenerate_record_id, self.generate_sql_logs), None)
            last_sql_messages: List[dict[str, Any]] = _temp_log.messages if _temp_log else []

        # 排除所有的系统提示词
        last_sql_messages = [obj for obj in last_sql_messages if obj.get("sqlbot_system") != True]

        count_limit = self.base_message_round_count_limit

        self.sql_message = []
        # add sys prompt
        _system_templates = self.chat_question.sql_sys_question(self.ds.type, self.enable_sql_row_limit)
        self.sql_message.append(SystemPromptMessage(content=_system_templates['system']))
        self.sql_message.append(HumanPromptMessage(content=_system_templates['rules']))
        self.sql_message.append(
            AIPromptMessage(content='Tôi đã nắm rõ tất cả quy tắc, bao gồm cấu trúc bảng, chuẩn SQL, giới hạn bảo mật và định dạng đầu ra, tôi sẽ tuân thủ nghiêm ngặt các quy tắc này.'))
        self.sql_message.append(HumanPromptMessage(content=_system_templates['schema']))
        self.sql_message.append(
            AIPromptMessage(content='Tôi đã xác nhận thông tin cơ sở dữ liệu và schema cấu trúc bảng mà bạn cung cấp, SQL tôi sinh ra sẽ không vượt ra ngoài phạm vi bạn cung cấp.'))
        if _system_templates.get('custom_prompt'):
            self.sql_message.append(HumanPromptMessage(content=_system_templates['custom_prompt']))
            self.sql_message.append(AIPromptMessage(content='Tôi đã xác nhận thông tin bổ sung bạn cung cấp, tôi sẽ tham khảo.'))
        if _system_templates.get('terminologies'):
            self.sql_message.append(HumanPromptMessage(content=_system_templates['terminologies']))
            self.sql_message.append(AIPromptMessage(content='Tôi đã xác nhận thông tin thuật ngữ bạn cung cấp, tôi sẽ tham khảo.'))
        if _system_templates.get('data_training'):
            self.sql_message.append(HumanPromptMessage(content=_system_templates['data_training']))
            self.sql_message.append(AIPromptMessage(content='Tôi đã xác nhận các ví dụ SQL bạn cung cấp, tôi sẽ tham khảo.'))

        if last_sql_messages is not None and len(last_sql_messages) > 0:
            last_rounds = get_last_conversation_rounds(last_sql_messages, rounds=count_limit)
            last_rounds = trim_history_by_size(last_rounds, settings.LLM_SQL_HISTORY_TOTAL_MAX_CHARS)

            for _msg_dict in last_rounds:
                _msg: BaseMessage
                if _msg_dict.get('type') == 'human':
                    _msg = HumanMessage(content=_msg_dict.get('content'))
                    self.sql_message.append(_msg)
                elif _msg_dict.get('type') == 'ai':
                    _msg = AIMessage(content=_msg_dict.get('content'))
                    self.sql_message.append(_msg)

        last_chart_messages: List[dict[str, Any]] = self.generate_chart_logs[-1].messages if len(
            self.generate_chart_logs) > 0 else []
        if self.chat_question.regenerate_record_id:
            # filter record before regenerate_record_id
            _temp_log = next(
                filter(lambda obj: obj.pid == self.chat_question.regenerate_record_id, self.generate_chart_logs), None)
            last_chart_messages: List[dict[str, Any]] = _temp_log.messages if _temp_log else []

        # 排除所有的系统提示词
        last_chart_messages = [obj for obj in last_chart_messages if obj.get("sqlbot_system") != True]

        count_chart_limit = self.base_message_round_count_limit

        self.chart_message = []
        # add sys prompt
        _chart_system_templates = self.chat_question.chart_sys_question()
        self.chart_message.append(SystemPromptMessage(content=_chart_system_templates['system']))
        self.chart_message.append(HumanPromptMessage(content=_chart_system_templates['rules']))
        self.chart_message.append(AIPromptMessage(content='Tôi đã nắm rõ tất cả quy tắc, tôi sẽ tuân thủ nghiêm ngặt các quy tắc này để sinh ra JSON đúng yêu cầu.'))
        if last_chart_messages is not None and len(last_chart_messages) > 0:
            last_rounds = get_last_conversation_rounds(last_chart_messages, rounds=count_chart_limit)

            for _msg_dict in last_rounds:
                _msg: BaseMessage
                if _msg_dict.get('type') == 'human':
                    _msg = HumanMessage(content=_msg_dict.get('content'))
                    self.chart_message.append(_msg)
                elif _msg_dict.get('type') == 'ai':
                    _msg = AIMessage(content=_msg_dict.get('content'))
                    self.chart_message.append(_msg)

    def init_record(self, session: Session) -> ChatRecord:
        self.record = save_question(session=session, current_user=self.current_user, question=self.chat_question)
        return self.record

    def get_record(self):
        return self.record

    def set_record(self, record: ChatRecord):
        self.record = record

    def set_articles_number(self, articles_number: int):
        self.articles_number = articles_number

    def get_fields_from_chart(self, _session: Session):
        chart_info = get_chart_config(_session, self.record.id)
        return format_chart_fields(chart_info)

    def filter_terminology_template(self, _session: Session, oid: int = None, ds_id: int = None):
        self.current_logs[OperationEnum.FILTER_TERMS] = start_log(session=_session,
                                                                  operate=OperationEnum.FILTER_TERMS,
                                                                  record_id=self.record.id, local_operation=True)
        calculate_oid = oid
        calculate_ds_id = ds_id
        if self.current_assistant:
            calculate_oid = self.current_assistant.oid if self.current_assistant.type != 4 else self.oid
            if self.current_assistant.type == 1:
                calculate_ds_id = None
        if self.current_assistant and self.current_assistant.type == 1:
            self.chat_question.terminologies, term_list = get_terminology_template(_session,
                                                                                   self.chat_question.question,
                                                                                   calculate_oid,
                                                                                   None, self.current_assistant.id)
        else:
            self.chat_question.terminologies, term_list = get_terminology_template(_session,
                                                                                   self.chat_question.question,
                                                                                   calculate_oid,
                                                                                   calculate_ds_id)

        self.current_logs[OperationEnum.FILTER_TERMS] = end_log(session=_session,
                                                                log=self.current_logs[OperationEnum.FILTER_TERMS],
                                                                full_message=term_list)

    def filter_custom_prompts(self, _session: Session, custom_prompt_type: CustomPromptTypeEnum, oid: int = None,
                              ds_id: int = None):
        if SQLBotLicenseUtil.valid():
            self.current_logs[OperationEnum.FILTER_CUSTOM_PROMPT] = start_log(session=_session,
                                                                              operate=OperationEnum.FILTER_CUSTOM_PROMPT,
                                                                              record_id=self.record.id,
                                                                              local_operation=True)
            calculate_oid = oid
            calculate_ds_id = ds_id
            if self.current_assistant:
                calculate_oid = self.current_assistant.oid if self.current_assistant.type != 4 else self.oid
                if self.current_assistant.type == 1:
                    calculate_ds_id = None
            if self.current_assistant and self.current_assistant.type == 1:
                self.chat_question.custom_prompt, prompt_list = find_custom_prompts(_session,
                                                                                    custom_prompt_type,
                                                                                    calculate_oid,
                                                                                    None, self.current_assistant.id)
            else:
                self.chat_question.custom_prompt, prompt_list = find_custom_prompts(_session,
                                                                                    custom_prompt_type,
                                                                                    calculate_oid,
                                                                                    calculate_ds_id)
            self.current_logs[OperationEnum.FILTER_CUSTOM_PROMPT] = end_log(session=_session,
                                                                            log=self.current_logs[
                                                                                OperationEnum.FILTER_CUSTOM_PROMPT],
                                                                            full_message=prompt_list)

    def filter_training_template(self, _session: Session, oid: int = None, ds_id: int = None):
        self.current_logs[OperationEnum.FILTER_SQL_EXAMPLE] = start_log(session=_session,
                                                                        operate=OperationEnum.FILTER_SQL_EXAMPLE,
                                                                        record_id=self.record.id,
                                                                        local_operation=True)
        calculate_oid = oid
        calculate_ds_id = ds_id
        if self.current_assistant:
            calculate_oid = self.current_assistant.oid if self.current_assistant.type != 4 else self.oid
            if self.current_assistant.type == 1:
                calculate_ds_id = None
        if self.current_assistant and self.current_assistant.type == 1:
            self.chat_question.data_training, example_list = get_training_template(_session,
                                                                                   self.chat_question.question,
                                                                                   calculate_oid,
                                                                                   None, self.current_assistant.id)
        else:
            self.chat_question.data_training, example_list = get_training_template(_session,
                                                                                   self.chat_question.question,
                                                                                   calculate_oid,
                                                                                   calculate_ds_id)
        self.current_logs[OperationEnum.FILTER_SQL_EXAMPLE] = end_log(session=_session,
                                                                      log=self.current_logs[
                                                                          OperationEnum.FILTER_SQL_EXAMPLE],
                                                                      full_message=example_list)

    def get_sw_permission(self, _session: Session):
        """Resolve (lười, cache theo lượt) quyết định quyền SW của user hiện tại trên datasource này.

        Trả None khi không áp dụng được: datasource ngoài (assistant động) không nằm trong hệ quyền
        SW, hoặc hội thoại chưa gắn datasource. `account` của user chính là `user_id` phía SW.
        """
        if self.out_ds_instance or not self.ds:
            return None
        if self.sw_permission is None:
            self.sw_permission = resolve_sw_permission(
                _session, self.current_user.account, self.chat_question.domain_code, self.ds)
        return self.sw_permission

    def choose_table_schema(self, _session: Session):
        """Dựng M-Schema + sample data cho prompt, đồng thời là chỗ ÁP quyền SW lên lượt hỏi.

        Thu hẹp danh sách bảng ngay tại đây là thu hẹp cả ba tầng cùng lúc: schema trong prompt,
        whitelist AST (dùng chính danh sách trả về của hàm này), và sample data. Phễu bảng rỗng
        thì chặn sớm bằng SingleMessageError — trước mọi lời gọi LLM — kèm danh sách lĩnh vực
        user thực có để client sửa `domainCode`.
        """
        self.current_logs[OperationEnum.CHOOSE_TABLE] = start_log(session=_session,
                                                                  operate=OperationEnum.CHOOSE_TABLE,
                                                                  record_id=self.record.id,
                                                                  local_operation=True)
        sw_permission = self.get_sw_permission(_session)
        restricted = sw_permission is not None and sw_permission.restricted
        if restricted:
            domain_list = ', '.join(
                f"{d['code']} ({d['name']})" if d.get('name') else d['code']
                for d in sw_permission.available_domains) or 'không có'
            requested_domain = self.chat_question.domain_code
            if requested_domain and requested_domain not in {d['code'] for d in
                                                             sw_permission.available_domains}:
                raise SingleMessageError(
                    f"Mã lĩnh vực '{requested_domain}' không có trong quyền của bạn trên nguồn dữ "
                    f"liệu này. Lĩnh vực hợp lệ: {domain_list}.")
            if not sw_permission.allowed_tables:
                raise SingleMessageError(
                    f"Bạn chưa được cấp quyền khai thác bảng nào trên nguồn dữ liệu này. "
                    f"Lĩnh vực bạn được cấp: {domain_list}.")

        self.chat_question.db_schema, tables = self.out_ds_instance.get_db_schema(
            self.ds.id, self.chat_question.question) if self.out_ds_instance else get_table_schema(
            session=_session,
            current_user=self.current_user,
            ds=self.ds,
            question=self.chat_question.question,
            table_list=sw_permission.allowed_tables if restricted else None,
            column_filter=sw_permission.allowed_columns if restricted else None)

        if restricted and not tables:
            # Tên bảng trong quyền không khớp exact với core_table (đã có warning chi tiết trong
            # log) — với user bị siết thì schema rỗng phải là lỗi to, không phải prompt rỗng.
            raise SingleMessageError(
                "Danh sách bảng bạn được cấp quyền không khớp với schema hiện tại của nguồn dữ "
                "liệu. Liên hệ quản trị để đồng bộ lại quyền.")

        # Get sample data for all tables
        if not self.out_ds_instance:
            self.chat_question.sample_data = get_tables_sample_data(
                session=_session,
                current_user=self.current_user,
                ds=self.ds,
                table_list=tables,
                scope_queries=sw_permission.scope_queries if restricted else None)

        self.current_logs[OperationEnum.CHOOSE_TABLE] = end_log(session=_session,
                                                                log=self.current_logs[OperationEnum.CHOOSE_TABLE],
                                                                full_message=self.chat_question.db_schema)
        return tables

    def generate_analysis(self, _session: Session):
        fields = self.get_fields_from_chart(_session)
        self.chat_question.fields = orjson.dumps(fields).decode()
        data = get_chat_chart_data(_session, self.record.id)
        self.chat_question.data = orjson.dumps(data.get('data')).decode()
        analysis_msg: List[Union[BaseMessage, dict[str, Any]]] = []

        ds_id = self.ds.id if isinstance(self.ds, CoreDatasource) else None

        self.filter_terminology_template(_session, self.oid, ds_id)

        self.filter_custom_prompts(_session, CustomPromptTypeEnum.ANALYSIS, self.oid, ds_id)

        analysis_msg.append(SystemPromptMessage(content=self.chat_question.analysis_sys_question()))
        analysis_msg.append(HumanMessage(content=self.chat_question.analysis_user_question()))

        self.current_logs[OperationEnum.ANALYSIS] = start_log(session=_session,
                                                              ai_modal_id=self.chat_question.ai_modal_id,
                                                              ai_modal_name=self.chat_question.ai_modal_name,
                                                              operate=OperationEnum.ANALYSIS,
                                                              record_id=self.record.id,
                                                              full_message=[
                                                                  {'type': msg.type,
                                                                   'sqlbot_system': getattr(msg, 'sqlbot_system',
                                                                                            False) is True,
                                                                   'content': msg.content} for
                                                                  msg
                                                                  in analysis_msg])
        full_thinking_text = ''
        full_analysis_text = ''
        token_usage = {}
        res = process_stream(self.llm.stream(analysis_msg), token_usage)
        for chunk in res:
            if chunk.get('content'):
                full_analysis_text += chunk.get('content')
            if chunk.get('reasoning_content'):
                full_thinking_text += chunk.get('reasoning_content')
            yield chunk

        analysis_msg.append(AIMessage(full_analysis_text))

        self.current_logs[OperationEnum.ANALYSIS] = end_log(session=_session,
                                                            log=self.current_logs[
                                                                OperationEnum.ANALYSIS],
                                                            full_message=[
                                                                {'type': msg.type,
                                                                 'sqlbot_system': getattr(msg, 'sqlbot_system',
                                                                                          False) is True,
                                                                 'content': msg.content}
                                                                for msg in analysis_msg],
                                                            reasoning_content=full_thinking_text,
                                                            token_usage=token_usage)
        self.record = save_analysis_answer(session=_session, record_id=self.record.id,
                                           answer=orjson.dumps({'content': full_analysis_text}).decode())

    def build_answer_prompts(self, _session: Session, fields: Any,
                             data: Optional[List[Dict[str, Any]]]) -> tuple[str, str]:
        """Dựng sẵn cặp (system, user) prompt cho pha answer, chạy TRONG luồng chính.

        Phải dựng trước khi spawn thread: pha answer chạy song song với pha sinh chart mà cả hai
        cùng đọc self.chat_question. Dựng trên một bản model_copy() rồi chỉ đưa hai chuỗi đã render
        sang thread, nên thread không còn chạm vào state dùng chung — hết đường tranh chấp. Việc đọc
        lịch sử từ DB cũng phải nằm ở đây vì lý do đó: `_session` không được đi qua thread.

        Lấy fields/data từ kết quả thực thi SQL chứ không từ chart config như generate_analysis:
        lúc này chart chưa sinh xong, và chính việc bỏ phụ thuộc vào chart mới cho phép chạy song song.

        Tổng số dòng phải đếm TRƯỚC khi cắt và truyền riêng qua `data_scope` — xem
        `build_data_scope_note` về lý do.

        Lịch sử hỏi-đáp dùng chung `get_recent_qa_history` với nhánh fallback, tức là mang theo cả
        SQL lẫn dữ liệu cũ. Đây là ngữ cảnh đầy đủ nhưng cũng là rủi ro: prompt answer vốn cấm bịa
        số, giờ trong prompt lại có sẵn những con số KHÔNG thuộc lượt này. Bộ rule ở
        `answer.system` phải giữ được ranh giới đó, và mức độ nhiễm phải đo bằng chỉ số
        "Câu trả lời không bịa số" của `scripts/eval_text2sql/run_eval_http.py` chứ không đoán bằng
        cảm giác. Tắt bằng `LLM_ANSWER_HISTORY_ENABLED=false` để đối chứng.

        Bọc sẵn cặp thẻ `<history>` tại đây thay vì để trong template: lượt đầu hội thoại không có
        lịch sử, để template tự bọc thì mọi lượt đầu đều dính một khối XML rỗng vô nghĩa.
        """
        answer_question = self.chat_question.model_copy()
        rows = data if data else []
        total_rows = len(rows)
        if total_rows > ANSWER_MAX_ROWS:
            rows = rows[:ANSWER_MAX_ROWS]
        answer_question.fields = orjson.dumps(fields if fields else []).decode()
        answer_question.data = orjson.dumps(prepare_for_orjson(rows)).decode()
        answer_question.data_scope = build_data_scope_note(total_rows, len(rows))
        answer_question.answer_history = ''
        if settings.LLM_ANSWER_HISTORY_ENABLED:
            _history = get_recent_qa_history(
                session=_session,
                chat_id=self.record.chat_id,
                exclude_record_id=self.record.id,
                rounds=settings.LLM_ANSWER_HISTORY_ROUNDS,
                max_rows=settings.LLM_ANSWER_HISTORY_ROWS,
            )
            if _history:
                answer_question.answer_history = f'<history>\n{_history}\n</history>\n'
        return answer_question.answer_sys_question(), answer_question.answer_user_question()

    def build_answer_fallback_prompts(self, _session: Session, reason: str,
                                      kind: str = 'failed') -> tuple[str, str]:
        """Dựng cặp prompt cho pha answer khi lượt này KHÔNG có dữ liệu truy vấn mới.

        `kind` phân biệt VÌ SAO không có dữ liệu, và chỉ chi phối mấy dòng khung của prompt
        (xem `answer.fallback_variants`): `'failed'` là pha SQL hỏng thật, `'no_query'` là hệ
        thống chủ động bỏ qua truy vấn. Hai trường hợp dùng chung bộ rule vì bộ rule không phụ
        thuộc nguyên nhân; chỉ có giọng kể là phải khác, nếu không thì một lượt bình thường lại
        được thông báo cho người dùng như một sự cố.

        Thay dữ liệu vừa truy vấn (không có) bằng tóm tắt các lượt hỏi-đáp cũ. Phần lớn câu hỏi làm
        pha SQL gãy là câu tham chiếu ngược ("trong đó bảng nào dài nhất") — dữ liệu để trả lời đã
        nằm sẵn ở lượt trước, chỉ là không diễn đạt được thành SQL mới.

        `reason` được rút gọn về đúng thông điệp cho người đọc: chuỗi lỗi nội bộ hay là JSON bọc
        traceback, nhét nguyên vào prompt chỉ làm LLM chép lại thuật ngữ kỹ thuật vào câu trả lời.

        Kèm luôn danh sách bảng: nhóm câu hỏi về chính datasource ("có bao nhiêu bảng") gần như luôn
        làm pha SQL gãy vì không bảng nghiệp vụ nào chứa thông tin đó, trong khi câu trả lời nằm sẵn
        trong schema. Chỉ đưa TÊN bảng chứ không đưa cả m-schema — m-schema nặng vài chục KB và
        phần cột không giúp gì cho loại câu hỏi này.
        """
        answer_question = self.chat_question.model_copy()
        answer_question.fallback_kind = kind
        answer_question.fallback_reason = _humanize_error(reason)
        answer_question.fallback_schema = '\n'.join(self.table_name_list) if self.table_name_list else ''
        answer_question.fallback_history = get_recent_qa_history(
            session=_session,
            chat_id=self.record.chat_id,
            exclude_record_id=self.record.id,
            rounds=settings.LLM_ANSWER_FALLBACK_ROUNDS,
            max_rows=settings.LLM_ANSWER_FALLBACK_ROWS,
        )
        return answer_question.answer_fallback_sys_question(), answer_question.answer_fallback_user_question()

    def generate_answer(self, _session: Session, sys_prompt: str, user_prompt: str):
        """Gọi LLM sinh câu trả lời chữ cuối cùng, yield từng chunk như các pha khác.

        Nhận prompt đã render sẵn thay vì tự dựng (xem build_answer_prompts). Không gán lại
        self.record sau khi lưu — luồng chart cũng đang gán self.record, ghi đè lẫn nhau sẽ hỏng.
        """
        answer_msg: List[Union[BaseMessage, dict[str, Any]]] = [
            SystemPromptMessage(content=sys_prompt),
            HumanMessage(content=user_prompt),
        ]

        self.current_logs[OperationEnum.ANSWER] = start_log(session=_session,
                                                            ai_modal_id=self.chat_question.ai_modal_id,
                                                            ai_modal_name=self.chat_question.ai_modal_name,
                                                            operate=OperationEnum.ANSWER,
                                                            record_id=self.record.id,
                                                            full_message=[
                                                                {'type': msg.type,
                                                                 'sqlbot_system': getattr(msg, 'sqlbot_system',
                                                                                          False) is True,
                                                                 'content': msg.content} for
                                                                msg
                                                                in answer_msg])
        full_thinking_text = ''
        full_answer_text = ''
        token_usage = {}
        res = process_stream(self.llm.stream(answer_msg), token_usage)
        for chunk in res:
            if chunk.get('content'):
                full_answer_text += chunk.get('content')
            if chunk.get('reasoning_content'):
                full_thinking_text += chunk.get('reasoning_content')
            yield chunk

        answer_msg.append(AIMessage(full_answer_text))

        # strip(): LLM hay mở đầu bằng vài dòng trống sau khối reasoning, để nguyên thì client nào
        # render thẳng cũng bị thụt một khoảng trắng đầu câu trả lời.
        save_answer(session=_session, record_id=self.record.id,
                    answer=orjson.dumps({'content': full_answer_text.strip()}).decode())
        self.current_logs[OperationEnum.ANSWER] = end_log(session=_session,
                                                          log=self.current_logs[OperationEnum.ANSWER],
                                                          full_message=[
                                                              {'type': msg.type,
                                                               'sqlbot_system': getattr(msg, 'sqlbot_system',
                                                                                        False) is True,
                                                               'content': msg.content}
                                                              for msg in answer_msg],
                                                          reasoning_content=full_thinking_text,
                                                          token_usage=token_usage)

    def run_answer_worker(self, sys_prompt: str, user_prompt: str, out_queue: queue.Queue):
        """Thân thread của pha answer: đẩy từng chunk vào queue cho luồng chính vét ra.

        Mở session riêng vì session_maker là scoped_session (thread-local) — dùng lại session của
        luồng chính từ thread khác sẽ hỏng. Luôn đặt sentinel 'done' trong finally, nếu không luồng
        chính sẽ chờ vô hạn ở lần vét cuối.
        """
        _session = None
        try:
            _session = session_maker()
            for chunk in self.generate_answer(_session, sys_prompt, user_prompt):
                out_queue.put(('chunk', chunk))
        except Exception as e:
            traceback.print_exc()
            out_queue.put(('error', e))
        finally:
            out_queue.put(('done', None))
            if _session is not None:
                session_maker.remove()

    def drain_answer_queue(self, answer_queue: Optional[queue.Queue], state: Dict[str, Any], block: bool):
        """Vét chunk answer từ queue và phát ra event SSE, xen vào giữa dòng event của pha chart.

        block=False dùng khi đang stream chart: chỉ lấy những gì đã sẵn, không được chặn luồng chart.
        block=True dùng sau khi chart xong: chờ tới sentinel 'done'.

        Answer lỗi thì chỉ báo bằng event info rồi đi tiếp, KHÔNG phát event 'error': SQL và chart
        đã chạy xong và vẫn dùng được, giết cả lượt hỏi chỉ vì pha phụ hỏng là mất dữ liệu vô ích.
        """
        if answer_queue is None or state.get('done'):
            return
        while True:
            try:
                kind, payload = answer_queue.get(block=block, timeout=None if block else 0.01)
            except queue.Empty:
                return
            if kind == 'chunk':
                if payload.get('content'):
                    state['text'] = state.get('text', '') + payload.get('content')
                yield 'data:' + orjson.dumps(
                    {'content': payload.get('content'), 'reasoning_content': payload.get('reasoning_content'),
                     'type': 'answer-result'}).decode() + '\n\n'
            elif kind == 'error':
                SQLBotLogUtil.error(f'Generate answer failed: {payload}')
                state['failed'] = True
            else:
                state['done'] = True
                if state.get('failed'):
                    yield 'data:' + orjson.dumps({'type': 'info', 'msg': 'answer failed'}).decode() + '\n\n'
                else:
                    yield 'data:' + orjson.dumps({'type': 'info', 'msg': 'answer generated'}).decode() + '\n\n'
                    yield 'data:' + orjson.dumps(
                        {'content': state.get('text', '').strip(), 'type': 'answer'}).decode() + '\n\n'
                return

    def generate_inline_recommend(self, _session: Session):
        """Sinh gợi ý câu hỏi tiếp theo cho CHÍNH lượt hỏi này, chạy trong thread riêng.

        Dùng lại prompt `guess` của endpoint rời nhưng thay hai đầu vào:
        - schema: lấy thẳng `chat_question.db_schema` mà `init_messages` đã dựng, tức là bản đã qua
          phễu quyền SW của lượt này — không tự gọi lại `get_table_schema` như task rời;
        - câu hỏi cũ: chỉ của chính user (xem `get_user_old_questions`).

        Không yield chunk ra ngoài: kết quả là MỘT khối JSON, mảnh dở dang thì client không parse
        được nên stream theo token vô nghĩa (cùng lý do với pha biểu đồ). Trả về chuỗi JSON đã lưu.
        """
        articles_number = max(1, settings.CHAT_INLINE_RECOMMEND_COUNT)
        guess_msg: List[Union[BaseMessage, dict[str, Any]]] = []
        guess_msg.append(SystemPromptMessage(content=self.chat_question.guess_sys_question(articles_number)))

        old_questions = [q.strip() for q in get_user_old_questions(
            session=_session,
            datasource=self.record.datasource,
            create_by=self.record.create_by,
            domain_code=self.chat_question.domain_code,
            limit=max(0, settings.CHAT_INLINE_RECOMMEND_HISTORY))]
        guess_msg.append(
            HumanMessage(content=self.chat_question.guess_user_question(orjson.dumps(old_questions).decode())))

        self.current_logs[OperationEnum.GENERATE_RECOMMENDED_QUESTIONS] = start_log(
            session=_session,
            ai_modal_id=self.chat_question.ai_modal_id,
            ai_modal_name=self.chat_question.ai_modal_name,
            operate=OperationEnum.GENERATE_RECOMMENDED_QUESTIONS,
            record_id=self.record.id,
            full_message=[{'type': msg.type,
                           'sqlbot_system': getattr(msg, 'sqlbot_system', False) is True,
                           'content': msg.content} for msg in guess_msg])

        full_thinking_text = ''
        full_guess_text = ''
        token_usage = {}
        for chunk in process_stream(self.llm.stream(guess_msg), token_usage):
            if chunk.get('content'):
                full_guess_text += chunk.get('content')
            if chunk.get('reasoning_content'):
                full_thinking_text += chunk.get('reasoning_content')

        guess_msg.append(AIMessage(full_guess_text))
        self.current_logs[OperationEnum.GENERATE_RECOMMENDED_QUESTIONS] = end_log(
            session=_session,
            log=self.current_logs[OperationEnum.GENERATE_RECOMMENDED_QUESTIONS],
            full_message=[{'type': msg.type,
                           'sqlbot_system': getattr(msg, 'sqlbot_system', False) is True,
                           'content': msg.content} for msg in guess_msg],
            reasoning_content=full_thinking_text,
            token_usage=token_usage)

        return save_record_recommend_questions(session=_session, record_id=self.record.id,
                                              answer={'content': full_guess_text})

    def run_recommend_worker(self, out_queue: queue.Queue):
        """Thân thread của pha gợi ý. Cùng khuôn với `run_answer_worker`.

        Session riêng vì session_maker là scoped_session (thread-local), và sentinel 'done' đặt
        trong finally để luồng chính không chờ vô hạn khi thread chết giữa chừng.
        """
        _session = None
        try:
            _session = session_maker()
            out_queue.put(('result', self.generate_inline_recommend(_session)))
        except Exception as e:
            traceback.print_exc()
            out_queue.put(('error', e))
        finally:
            out_queue.put(('done', None))
            if _session is not None:
                session_maker.remove()

    def drain_recommend_queue(self, recommend_queue: Optional[queue.Queue], state: Dict[str, Any], block: bool):
        """Vét kết quả pha gợi ý và phát event SSE.

        Pha gợi ý hỏng thì chỉ báo bằng event `info` rồi đi tiếp, KHÔNG phát `error` và KHÔNG gọi
        save_error: câu trả lời chính đã sinh xong, giết cả lượt hỏi vì một pha phụ là mất dữ liệu
        vô ích (giống hệt cách xử lý pha answer và pha chart).
        """
        if recommend_queue is None or state.get('done'):
            return
        while True:
            try:
                kind, payload = recommend_queue.get(block=block, timeout=None if block else 0.01)
            except queue.Empty:
                return
            if kind == 'result':
                state['text'] = payload
            elif kind == 'error':
                SQLBotLogUtil.error(f'Generate inline recommended questions failed: {payload}')
                state['failed'] = True
            else:
                state['done'] = True
                if state.get('failed'):
                    yield 'data:' + orjson.dumps(
                        {'type': 'info', 'msg': 'recommended question failed'}).decode() + '\n\n'
                else:
                    yield 'data:' + orjson.dumps(
                        {'type': 'info', 'msg': 'recommended question generated'}).decode() + '\n\n'
                    yield 'data:' + orjson.dumps(
                        {'content': state.get('text', '[]'), 'type': 'recommended_question'}).decode() + '\n\n'
                return

    def generate_predict(self, _session: Session):
        fields = self.get_fields_from_chart(_session)
        self.chat_question.fields = orjson.dumps(fields).decode()
        data = get_chat_chart_data(_session, self.record.id)
        self.chat_question.data = orjson.dumps(data.get('data')).decode()

        ds_id = self.ds.id if isinstance(self.ds, CoreDatasource) else None
        self.filter_custom_prompts(_session, CustomPromptTypeEnum.PREDICT_DATA, self.oid, ds_id)

        predict_msg: List[Union[BaseMessage, dict[str, Any]]] = []
        predict_msg.append(SystemPromptMessage(content=self.chat_question.predict_sys_question()))
        predict_msg.append(HumanMessage(content=self.chat_question.predict_user_question()))

        self.current_logs[OperationEnum.PREDICT_DATA] = start_log(session=_session,
                                                                  ai_modal_id=self.chat_question.ai_modal_id,
                                                                  ai_modal_name=self.chat_question.ai_modal_name,
                                                                  operate=OperationEnum.PREDICT_DATA,
                                                                  record_id=self.record.id,
                                                                  full_message=[
                                                                      {'type': msg.type,
                                                                       'sqlbot_system': getattr(msg, 'sqlbot_system',
                                                                                                False) is True,
                                                                       'content': msg.content} for
                                                                      msg
                                                                      in predict_msg])
        full_thinking_text = ''
        full_predict_text = ''
        token_usage = {}
        res = process_stream(self.llm.stream(predict_msg), token_usage)
        for chunk in res:
            if chunk.get('content'):
                full_predict_text += chunk.get('content')
            if chunk.get('reasoning_content'):
                full_thinking_text += chunk.get('reasoning_content')
            yield chunk

        predict_msg.append(AIMessage(full_predict_text))
        self.record = save_predict_answer(session=_session, record_id=self.record.id,
                                          answer=orjson.dumps({'content': full_predict_text}).decode())
        self.current_logs[OperationEnum.PREDICT_DATA] = end_log(session=_session,
                                                                log=self.current_logs[
                                                                    OperationEnum.PREDICT_DATA],
                                                                full_message=[
                                                                    {'type': msg.type,
                                                                     'sqlbot_system': getattr(msg, 'sqlbot_system',
                                                                                              False) is True,
                                                                     'content': msg.content}
                                                                    for msg in predict_msg],
                                                                reasoning_content=full_thinking_text,
                                                                token_usage=token_usage)

    def generate_recommend_questions_task(self, _session: Session):

        # get schema
        if self.ds and not self.chat_question.db_schema:
            # Nhánh này chạy khi task gợi ý câu hỏi được gọi rời (không qua choose_table_schema),
            # nên vẫn phải tự áp quyền SW: schema đầy đủ lọt vào prompt gợi ý là rò tên bảng/cột
            # ngoài quyền của user bị siết.
            sw_permission = self.get_sw_permission(_session)
            restricted = sw_permission is not None and sw_permission.restricted
            self.chat_question.db_schema, tables = self.out_ds_instance.get_db_schema(
                self.ds.id, self.chat_question.question) if self.out_ds_instance else get_table_schema(
                session=_session,
                current_user=self.current_user, ds=self.ds,
                question=self.chat_question.question,
                embedding=True,
                table_list=sw_permission.allowed_tables if restricted else None,
                column_filter=sw_permission.allowed_columns if restricted else None)

            # Get sample data for all tables
            # if not self.out_ds_instance:
            #     self.chat_question.sample_data = get_tables_sample_data(
            #         session=_session,
            #         current_user=self.current_user,
            #         ds=self.ds)

        guess_msg: List[Union[BaseMessage, dict[str, Any]]] = []
        guess_msg.append(SystemPromptMessage(content=self.chat_question.guess_sys_question(self.articles_number)))

        old_questions = list(map(lambda q: q.strip(), get_old_questions(_session, self.record.datasource)))
        guess_msg.append(
            HumanMessage(content=self.chat_question.guess_user_question(orjson.dumps(old_questions).decode())))

        self.current_logs[OperationEnum.GENERATE_RECOMMENDED_QUESTIONS] = start_log(session=_session,
                                                                                    ai_modal_id=self.chat_question.ai_modal_id,
                                                                                    ai_modal_name=self.chat_question.ai_modal_name,
                                                                                    operate=OperationEnum.GENERATE_RECOMMENDED_QUESTIONS,
                                                                                    record_id=self.record.id,
                                                                                    full_message=[
                                                                                        {'type': msg.type,
                                                                                         'sqlbot_system': getattr(msg,
                                                                                                                  'sqlbot_system',
                                                                                                                  False) is True,
                                                                                         'content': msg.content} for
                                                                                        msg
                                                                                        in guess_msg])
        full_thinking_text = ''
        full_guess_text = ''
        token_usage = {}
        res = process_stream(self.llm.stream(guess_msg), token_usage)
        for chunk in res:
            if chunk.get('content'):
                full_guess_text += chunk.get('content')
            if chunk.get('reasoning_content'):
                full_thinking_text += chunk.get('reasoning_content')
            yield chunk

        guess_msg.append(AIMessage(full_guess_text))

        self.current_logs[OperationEnum.GENERATE_RECOMMENDED_QUESTIONS] = end_log(session=_session,
                                                                                  log=self.current_logs[
                                                                                      OperationEnum.GENERATE_RECOMMENDED_QUESTIONS],
                                                                                  full_message=[
                                                                                      {'type': msg.type,
                                                                                       'sqlbot_system': getattr(msg,
                                                                                                                'sqlbot_system',
                                                                                                                False) is True,
                                                                                       'content': msg.content}
                                                                                      for msg in guess_msg],
                                                                                  reasoning_content=full_thinking_text,
                                                                                  token_usage=token_usage)
        self.record = save_recommend_question_answer(session=_session, record_id=self.record.id,
                                                     answer={'content': full_guess_text},
                                                     articles_number=self.articles_number)

        yield {'recommended_question': self.record.recommended_question}

    def select_datasource(self, _session: Session):
        datasource_msg: List[Union[BaseMessage, dict[str, Any]]] = []
        datasource_msg.append(SystemPromptMessage(self.chat_question.datasource_sys_question()))
        if self.current_assistant and self.current_assistant.type != 4:
            _ds_list = get_assistant_ds(session=_session, llm_service=self)
        else:
            # Lọc nguồn dữ liệu chưa dùng được: đây là đường LLM tự chọn nguồn, chặn cửa gắn nguồn
            # thủ công mà để ngỏ cửa này thì hậu quả y hệt — phiên hỏi đáp trên một nguồn 0 bảng.
            stmt = select(CoreDatasource.id, CoreDatasource.name, CoreDatasource.description).where(
                and_(CoreDatasource.oid == self.oid, usable_ds_condition()))
            _ds_list = [
                {
                    "id": ds.id,
                    "name": ds.name,
                    "description": ds.description
                }
                for ds in _session.exec(stmt)
            ]
        if not _ds_list:
            raise SingleMessageError('No available datasource configuration found')
        ignore_auto_select = _ds_list and len(_ds_list) == 1
        # ignore auto select ds

        full_thinking_text = ''
        full_text = ''
        if not ignore_auto_select:
            if settings.TABLE_EMBEDDING_ENABLED and (
                    not self.current_assistant or (self.current_assistant and self.current_assistant.type != 1)):
                _ds_list = get_ds_embedding(_session, _ds_list, self.out_ds_instance,
                                            self.chat_question.question, self.current_assistant)
                # yield {'content': '{"id":' + str(ds.get('id')) + '}'}

            _ds_list_dict = []
            for _ds in _ds_list:
                _ds_list_dict.append(_ds)
            datasource_msg.append(
                HumanMessage(self.chat_question.datasource_user_question(orjson.dumps(_ds_list_dict).decode())))

            self.current_logs[OperationEnum.CHOOSE_DATASOURCE] = start_log(session=_session,
                                                                           ai_modal_id=self.chat_question.ai_modal_id,
                                                                           ai_modal_name=self.chat_question.ai_modal_name,
                                                                           operate=OperationEnum.CHOOSE_DATASOURCE,
                                                                           record_id=self.record.id,
                                                                           full_message=[{'type': msg.type,
                                                                                          'sqlbot_system': getattr(msg,
                                                                                                                   'sqlbot_system',
                                                                                                                   False) is True,
                                                                                          'content': msg.content}
                                                                                         for
                                                                                         msg in datasource_msg])

            token_usage = {}
            res = process_stream(self.llm.stream(datasource_msg), token_usage)
            for chunk in res:
                if chunk.get('content'):
                    full_text += chunk.get('content')
                if chunk.get('reasoning_content'):
                    full_thinking_text += chunk.get('reasoning_content')
                yield chunk
            datasource_msg.append(AIMessage(full_text))

            self.current_logs[OperationEnum.CHOOSE_DATASOURCE] = end_log(session=_session,
                                                                         log=self.current_logs[
                                                                             OperationEnum.CHOOSE_DATASOURCE],
                                                                         full_message=[
                                                                             {'type': msg.type,
                                                                              'sqlbot_system': getattr(msg,
                                                                                                       'sqlbot_system',
                                                                                                       False) is True,
                                                                              'content': msg.content}
                                                                             for msg in datasource_msg],
                                                                         reasoning_content=full_thinking_text,
                                                                         token_usage=token_usage)

            # Đáp án chọn datasource luôn có khóa 'id'; lọc theo khóa để không bốc nhầm JSON nháp.
            json_str = extract_json_object(full_text, required_keys=('id',))
            if json_str is None:
                raise SingleMessageError(f'Cannot parse datasource from answer: {full_text}')
            ds = orjson.loads(json_str)

        _error: Exception | None = None
        _datasource: int | None = None
        _engine_type: str | None = None
        try:
            data: dict = _ds_list[0] if ignore_auto_select else ds

            if data.get('id') and data.get('id') != 0:
                _datasource = data['id']
                _chat = _session.get(Chat, self.record.chat_id)
                _chat.datasource = _datasource
                if self.current_assistant and self.current_assistant.type in dynamic_ds_types:
                    _ds = self.out_ds_instance.get_ds(data['id'])
                    self.ds = _ds
                    self.chat_question.engine = _ds.type + get_version(self.ds)

                    _engine_type = self.chat_question.engine
                    _chat.engine_type = _ds.type
                else:
                    _ds = _session.get(CoreDatasource, _datasource)
                    if not _ds:
                        _datasource = None
                        raise SingleMessageError(f"Datasource configuration with id {_datasource} not found")
                    self.ds = CoreDatasource(**_ds.model_dump())
                    self.chat_question.engine = (_ds.type_name if _ds.type != 'excel' else 'PostgreSQL') + get_version(
                        self.ds)

                    _engine_type = self.chat_question.engine
                    _chat.engine_type = _ds.type_name
                # save chat
                with _session.begin_nested():
                    # 为了能继续记日志，先单独处理下事务
                    try:
                        _session.add(_chat)
                        _session.flush()
                        _session.refresh(_chat)
                        _session.commit()
                    except Exception as e:
                        _session.rollback()
                        raise e

            elif data['fail']:
                raise SingleMessageError(data['fail'])
            else:
                raise SingleMessageError('No available datasource configuration found')

        except Exception as e:
            _error = e

        if not ignore_auto_select and not settings.TABLE_EMBEDDING_ENABLED:
            self.record = save_select_datasource_answer(session=_session, record_id=self.record.id,
                                                        answer=orjson.dumps({'content': full_text}).decode(),
                                                        datasource=_datasource,
                                                        engine_type=_engine_type)
        if self.ds:
            oid = self.ds.oid if isinstance(self.ds, CoreDatasource) else 1
            ds_id = self.ds.id if isinstance(self.ds, CoreDatasource) else None

            self.filter_terminology_template(_session, oid, ds_id)

            self.filter_training_template(_session, oid, ds_id)

            self.filter_custom_prompts(_session, CustomPromptTypeEnum.GENERATE_SQL, oid, ds_id)

            self.init_messages(_session)

        if _error:
            raise _error

    def generate_sql(self, _session: Session, retry_reason: Optional[str] = None):
        """Sinh SQL, yield từng chunk. `retry_reason` khác None nghĩa là đang sinh lại trong cùng lượt.

        Lần retry chỉ thêm một message ngắn nêu lý do hỏng chứ không dựng lại prompt đầy đủ: câu hỏi
        gốc vẫn nằm trong `self.sql_message`, nên model có đủ ngữ cảnh để tự sửa. Dựng lại từ đầu vừa
        tốn token vừa khiến model lặp lại đúng lỗi cũ.

        Câu trả lời hỏng và lời nhắc sửa CHỈ nằm trong danh sách `messages` tạm của lần gọi này,
        không nhập vào `self.sql_message`. Nhờ vậy mỗi lượt hỏi luôn để lại đúng một cặp (câu hỏi,
        đáp án dùng được) cho các lượt sau — trước đây một lượt có retry để lại bốn message, trong
        đó có cả lời khiển trách "câu trả lời vừa rồi KHÔNG dùng được" mà lượt sau đọc thấy sẽ hiểu
        sai là đang nói về chính nó.
        """
        # append current question
        if retry_reason:
            # Chỉ nhấc ra khi phần tử cuối đúng là câu trả lời của model: nếu lần trước chết giữa
            # stream thì cuối danh sách là câu hỏi, nhấc đi là mất luôn đề bài.
            failed_answer = self.sql_message.pop() if self.sql_message and isinstance(
                self.sql_message[-1], AIMessage) else None
            messages = list(self.sql_message)
            if failed_answer is not None:
                messages.append(failed_answer)
            messages.append(HumanMessage(self.chat_question.sql_retry_question(retry_reason)))
        else:
            self.sql_message.append(HumanMessage(
                self.chat_question.sql_user_question(current_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                                                     change_title=self.change_title)))
            messages = self.sql_message

        self.current_logs[OperationEnum.GENERATE_SQL] = start_log(session=_session,
                                                                  ai_modal_id=self.chat_question.ai_modal_id,
                                                                  ai_modal_name=self.chat_question.ai_modal_name,
                                                                  operate=OperationEnum.GENERATE_SQL,
                                                                  record_id=self.record.id,
                                                                  full_message=[
                                                                      {'type': msg.type,
                                                                       'sqlbot_system': getattr(msg, 'sqlbot_system',
                                                                                                False) is True,
                                                                       'content': msg.content} for msg
                                                                      in messages])
        full_thinking_text = ''
        full_sql_text = ''
        token_usage = {}
        res = process_stream(self.llm.stream(messages), token_usage)
        for chunk in res:
            if chunk.get('content'):
                full_sql_text += chunk.get('content')
            if chunk.get('reasoning_content'):
                full_thinking_text += chunk.get('reasoning_content')
            yield chunk

        # Lịch sử chỉ nhận bản đã rút gọn. `end_log` bên dưới cũng lưu `self.sql_message` chứ không
        # lưu `messages`: chính field này là thứ `init_messages` của lượt sau đọc để phát lại, nên
        # nó phải là lịch sử sạch. Prompt thật của lần gọi (kèm lần thử hỏng) đã nằm ở `start_log`.
        self.sql_message.append(AIMessage(_cap_history_answer(full_sql_text)))

        self.current_logs[OperationEnum.GENERATE_SQL] = end_log(session=_session,
                                                                log=self.current_logs[OperationEnum.GENERATE_SQL],
                                                                full_message=[{'type': msg.type,
                                                                               'sqlbot_system': getattr(msg,
                                                                                                        'sqlbot_system',
                                                                                                        False) is True,
                                                                               'content': msg.content}
                                                                              for msg in self.sql_message],
                                                                reasoning_content=full_thinking_text,
                                                                token_usage=token_usage)
        self.record = save_sql_answer(session=_session, record_id=self.record.id,
                                      answer=orjson.dumps({'content': full_sql_text}).decode())

    def generate_with_sub_sql(self, session: Session, sql, sub_mappings: list):
        sub_query = json.dumps(sub_mappings, ensure_ascii=False)
        self.chat_question.sql = sql
        self.chat_question.sub_query = sub_query
        dynamic_sql_msg: List[Union[BaseMessage, dict[str, Any]]] = []
        dynamic_sql_msg.append(SystemPromptMessage(content=self.chat_question.dynamic_sys_question()))
        dynamic_sql_msg.append(HumanMessage(content=self.chat_question.dynamic_user_question()))

        self.current_logs[OperationEnum.GENERATE_DYNAMIC_SQL] = start_log(session=session,
                                                                          ai_modal_id=self.chat_question.ai_modal_id,
                                                                          ai_modal_name=self.chat_question.ai_modal_name,
                                                                          operate=OperationEnum.GENERATE_DYNAMIC_SQL,
                                                                          record_id=self.record.id,
                                                                          full_message=[{'type': msg.type,
                                                                                         'sqlbot_system': getattr(msg,
                                                                                                                  'sqlbot_system',
                                                                                                                  False) is True,
                                                                                         'content': msg.content}
                                                                                        for
                                                                                        msg in dynamic_sql_msg])

        full_thinking_text = ''
        full_dynamic_text = ''
        token_usage = {}
        res = process_stream(self.llm.stream(dynamic_sql_msg), token_usage)
        for chunk in res:
            if chunk.get('content'):
                full_dynamic_text += chunk.get('content')
            if chunk.get('reasoning_content'):
                full_thinking_text += chunk.get('reasoning_content')

        dynamic_sql_msg.append(AIMessage(full_dynamic_text))

        self.current_logs[OperationEnum.GENERATE_DYNAMIC_SQL] = end_log(session=session,
                                                                        log=self.current_logs[
                                                                            OperationEnum.GENERATE_DYNAMIC_SQL],
                                                                        full_message=[
                                                                            {'type': msg.type,
                                                                             'sqlbot_system': getattr(msg,
                                                                                                      'sqlbot_system',
                                                                                                      False) is True,
                                                                             'content': msg.content}
                                                                            for msg in dynamic_sql_msg],
                                                                        reasoning_content=full_thinking_text,
                                                                        token_usage=token_usage)

        SQLBotLogUtil.info(full_dynamic_text)
        return full_dynamic_text

    def generate_assistant_dynamic_sql(self, _session: Session, sql, tables: List):
        ds: AssistantOutDsSchema = self.ds
        sub_query = []
        result_dict = {}
        for table in ds.tables:
            if table.name in tables and table.sql:
                # sub_query.append({"table": table.name, "query": table.sql})
                result_dict[table.name] = table.sql
                sub_query.append({"table": table.name, "query": f'{dynamic_subsql_prefix}{table.name}'})
        if not sub_query:
            return None
        temp_sql_text = self.generate_with_sub_sql(session=_session, sql=sql, sub_mappings=sub_query)
        result_dict['sqlbot_temp_sql_text'] = temp_sql_text
        return result_dict

    def build_table_filter(self, session: Session, sql: str, filters: list):
        filter = json.dumps(filters, ensure_ascii=False)
        self.chat_question.sql = sql
        self.chat_question.filter = filter
        permission_sql_msg: List[Union[BaseMessage, dict[str, Any]]] = []
        permission_sql_msg.append(SystemPromptMessage(content=self.chat_question.filter_sys_question()))
        permission_sql_msg.append(HumanMessage(content=self.chat_question.filter_user_question()))

        self.current_logs[OperationEnum.GENERATE_SQL_WITH_PERMISSIONS] = start_log(session=session,
                                                                                   ai_modal_id=self.chat_question.ai_modal_id,
                                                                                   ai_modal_name=self.chat_question.ai_modal_name,
                                                                                   operate=OperationEnum.GENERATE_SQL_WITH_PERMISSIONS,
                                                                                   record_id=self.record.id,
                                                                                   full_message=[
                                                                                       {'type': msg.type,
                                                                                        'sqlbot_system': getattr(msg,
                                                                                                                 'sqlbot_system',
                                                                                                                 False) is True,
                                                                                        'content': msg.content} for
                                                                                       msg
                                                                                       in permission_sql_msg])
        full_thinking_text = ''
        full_filter_text = ''
        token_usage = {}
        res = process_stream(self.llm.stream(permission_sql_msg), token_usage)
        for chunk in res:
            if chunk.get('content'):
                full_filter_text += chunk.get('content')
            if chunk.get('reasoning_content'):
                full_thinking_text += chunk.get('reasoning_content')

        permission_sql_msg.append(AIMessage(full_filter_text))

        self.current_logs[OperationEnum.GENERATE_SQL_WITH_PERMISSIONS] = end_log(session=session,
                                                                                 log=self.current_logs[
                                                                                     OperationEnum.GENERATE_SQL_WITH_PERMISSIONS],
                                                                                 full_message=[
                                                                                     {'type': msg.type,
                                                                                      'sqlbot_system': getattr(msg,
                                                                                                               'sqlbot_system',
                                                                                                               False) is True,
                                                                                      'content': msg.content}
                                                                                     for msg in permission_sql_msg],
                                                                                 reasoning_content=full_thinking_text,
                                                                                 token_usage=token_usage)

        SQLBotLogUtil.info(full_filter_text)
        return full_filter_text

    def generate_filter(self, _session: Session, sql: str, tables: List):
        filters = get_row_permission_filters(session=_session, current_user=self.current_user, ds=self.ds,
                                             tables=tables)
        if not filters:
            return None
        return self.build_table_filter(session=_session, sql=sql, filters=filters)

    def generate_assistant_filter(self, _session: Session, sql, tables: List):
        ds: AssistantOutDsSchema = self.ds
        filters = []
        for table in ds.tables:
            if table.name in tables and table.rule:
                filters.append({"table": table.name, "filter": table.rule})
        if not filters:
            return None
        return self.build_table_filter(session=_session, sql=sql, filters=filters)

    def generate_chart(self, _session: Session, chart_type: Optional[str] = '', schema: Optional[str] = ''):
        # append current question
        self.chart_message.append(HumanMessage(self.chat_question.chart_user_question(chart_type, schema)))

        self.current_logs[OperationEnum.GENERATE_CHART] = start_log(session=_session,
                                                                    ai_modal_id=self.chat_question.ai_modal_id,
                                                                    ai_modal_name=self.chat_question.ai_modal_name,
                                                                    operate=OperationEnum.GENERATE_CHART,
                                                                    record_id=self.record.id,
                                                                    full_message=[
                                                                        {'type': msg.type,
                                                                         'sqlbot_system': getattr(msg, 'sqlbot_system',
                                                                                                  False) is True,
                                                                         'content': msg.content} for
                                                                        msg
                                                                        in self.chart_message])
        full_thinking_text = ''
        full_chart_text = ''
        token_usage = {}
        res = process_stream(self.llm.stream(self.chart_message), token_usage)
        for chunk in res:
            if chunk.get('content'):
                full_chart_text += chunk.get('content')
            if chunk.get('reasoning_content'):
                full_thinking_text += chunk.get('reasoning_content')
            yield chunk

        self.chart_message.append(AIMessage(full_chart_text))

        self.record = save_chart_answer(session=_session, record_id=self.record.id,
                                        answer=orjson.dumps({'content': full_chart_text}).decode())
        self.current_logs[OperationEnum.GENERATE_CHART] = end_log(session=_session,
                                                                  log=self.current_logs[OperationEnum.GENERATE_CHART],
                                                                  full_message=[
                                                                      {'type': msg.type,
                                                                       'sqlbot_system': getattr(msg, 'sqlbot_system',
                                                                                                False) is True,
                                                                       'content': msg.content}
                                                                      for msg in self.chart_message],
                                                                  reasoning_content=full_thinking_text,
                                                                  token_usage=token_usage)

    def check_sql(self, session: Session, res: str, operate: OperationEnum) -> tuple[str, Optional[list]]:
        # Lọc theo khóa 'success' thay vì lấy JSON đầu tiên: nếu model kèm văn xuôi (hoặc reasoning
        # tràn vào content khi tắt thinking), mẩu JSON đầu thường là bản nháp trong lúc suy luận.
        json_str = extract_json_object(res, required_keys=('success',))

        log = self.current_logs[operate]

        if json_str is None:
            trigger_log_error(session, log)
            # Content rỗng là ca riêng: model kẹt trong vòng suy luận và không xuất ra gì cả. Báo
            # tách bạch, nếu không thông báo sẽ cụt ngủn ("...json object:\n") che mất nguyên nhân.
            if not res or res.strip() == '':
                raise SingleMessageError(orjson.dumps(
                    {'message': 'LLM returned empty content while generating SQL',
                     'traceback': 'LLM returned empty content while generating SQL. '
                                  'The model likely exhausted its budget on reasoning tokens '
                                  'without emitting an answer.'}).decode())
            raise SingleMessageError(orjson.dumps({'message': 'SQL answer is not a valid json object',
                                                   'traceback': "SQL answer is not a valid json object:\n" + res}).decode())
        sql: str
        data: dict
        try:
            data = orjson.loads(json_str)

            if data['success']:
                sql = data['sql']
            else:
                message = data['message']
                raise SingleMessageError(message)
        except SingleMessageError as e:
            trigger_log_error(session, log)
            raise e
        except Exception:
            trigger_log_error(session, log)
            raise SingleMessageError(orjson.dumps({'message': 'Cannot parse sql from answer',
                                                   'traceback': "Cannot parse sql from answer:\n" + res}).decode())

        if sql.strip() == '':
            trigger_log_error(session, log)
            raise SingleMessageError("SQL query is empty")
        return sql, data.get('tables')

    @staticmethod
    def get_chart_type_from_sql_answer(res: str) -> Optional[str]:
        json_str = extract_json_object(res, required_keys=('success',))
        if json_str is None:
            return None

        chart_type: Optional[str]
        data: dict
        try:
            data = orjson.loads(json_str)

            if data['success']:
                chart_type = data['chart-type']
            else:
                return None
        except Exception:
            return None

        return chart_type

    @staticmethod
    def get_brief_from_sql_answer(res: str) -> Optional[str]:
        json_str = extract_json_object(res, required_keys=('success',))
        if json_str is None:
            return None

        brief: Optional[str]
        data: dict
        try:
            data = orjson.loads(json_str)

            if data['success']:
                brief = data['brief']
            else:
                return None
        except Exception:
            return None

        return brief

    def check_save_sql(self, session: Session, res: str, operate: OperationEnum) -> str:
        sql, *_ = self.check_sql(session=session, res=res, operate=operate)
        save_sql(session=session, sql=sql, record_id=self.record.id)

        self.chat_question.sql = sql

        return sql

    def check_save_chart(self, session: Session, res: str) -> Dict[str, Any]:

        # Cấu hình biểu đồ luôn có khóa 'type' — dùng nó để bỏ qua JSON nháp trong phần suy luận.
        json_str = extract_json_object(res, required_keys=('type',))
        if json_str is None:
            raise SingleMessageError(orjson.dumps({'message': 'Cannot parse chart config from answer',
                                                   'traceback': "Cannot parse chart config from answer:\n" + res}).decode())
        data: dict

        chart: Dict[str, Any] = {}
        message = ''
        error = False

        try:
            data = orjson.loads(json_str)
            if data['type'] and data['type'] != 'error':
                # todo type check
                chart = data
                if chart.get('columns'):
                    for v in chart.get('columns'):
                        v['value'] = v.get('value').lower()
                if chart.get('axis'):
                    if chart.get('axis').get('x'):
                        chart.get('axis').get('x')['value'] = chart.get('axis').get('x').get('value').lower()
                    y_axis = chart.get('axis').get('y')
                    if y_axis:
                        if isinstance(y_axis, list):
                            # 数组格式: y: [{name, value}, ...]
                            for item in y_axis:
                                if item.get('value'):
                                    item['value'] = item['value'].lower()
                        elif isinstance(y_axis, dict) and y_axis.get('value'):
                            # 旧格式: y: {name, value}
                            y_axis['value'] = y_axis['value'].lower()
                    if chart.get('axis').get('series'):
                        chart.get('axis').get('series')['value'] = chart.get('axis').get('series').get('value').lower()
                if chart.get('axis') and chart['axis'].get('multi-quota'):
                    multi_quota = chart['axis']['multi-quota']
                    if multi_quota.get('value'):
                        if isinstance(multi_quota['value'], list):
                            # 将数组中的每个值转换为小写
                            multi_quota['value'] = [v.lower() if v else v for v in multi_quota['value']]
                        elif isinstance(multi_quota['value'], str):
                            # 如果是字符串，也转换为小写
                            multi_quota['value'] = multi_quota['value'].lower()
            elif data['type'] == 'error':
                message = data['reason']
                error = True
            else:
                raise Exception('Chart is empty')
        except Exception:
            error = True
            message = orjson.dumps({'message': 'Cannot parse chart config from answer',
                                    'traceback': "Cannot parse chart config from answer:\n" + res}).decode()

        if error:
            raise SingleMessageError(message)

        save_chart(session=session, chart=orjson.dumps(chart).decode(), record_id=self.record.id)

        return chart

    def check_save_predict_data(self, session: Session, res: str) -> bool:

        json_str = extract_nested_json(res)

        if not json_str:
            json_str = ''

        save_predict_data(session=session, record_id=self.record.id, data=json_str)

        if json_str == '':
            return False

        return True

    def save_error(self, session: Session, message: str):
        return save_error_message(session=session, record_id=self.record.id, message=message)

    def save_sql_data(self, session: Session, data_obj: Dict[str, Any]):
        try:
            data_result = data_obj.get('data')
            limit = 1000
            if data_result:
                data_result = prepare_for_orjson(data_result)
                if data_result and len(data_result) > limit and self.enable_sql_row_limit:
                    data_obj['data'] = data_result[:limit]
                    data_obj['limit'] = limit
                else:
                    data_obj['data'] = data_result
                data_obj['datasource'] = self.ds.id
            return save_sql_exec_data(session=session, record_id=self.record.id,
                                      data=orjson.dumps(data_obj).decode())
        except Exception as e:
            raise e

    @staticmethod
    def build_sql_data_payload(data_obj: Dict[str, Any]) -> Dict[str, Any]:
        """Gọt khối kết quả SQL thành phần chở được trên event ``sql-data``.

        Phải gọi SAU ``save_sql_data``: hàm đó sửa ``data_obj`` tại chỗ — cắt bớt dòng vượt trần
        1000 và gắn khóa ``limit``. Gọi trước thì client nhận nhiều dòng hơn bản lưu trong
        database, hai nguồn cùng một lượt hỏi lại nói khác nhau.

        Bỏ hai khóa có trong bản lưu: ``sql`` (bản mã base64 của chính câu SQL đã phát ở event
        ``sql``) và ``datasource`` (đã có event riêng). Chở lại chỉ tốn băng thông, mà đây là
        event nặng nhất của cả lượt hỏi.

        ``limit`` chỉ xuất hiện khi kết quả bị cắt, nên client thấy khóa này là biết dữ liệu
        chưa đủ — vắng mặt nghĩa là trọn vẹn.
        """
        payload: Dict[str, Any] = {
            'fields': data_obj.get('fields') or [],
            'fields_info': data_obj.get('fields_info') or [],
            'data': data_obj.get('data') or [],
        }
        if data_obj.get('limit'):
            payload['limit'] = data_obj.get('limit')
        return payload

    def finish(self, session: Session):
        return finish_record(session=session, record_id=self.record.id)

    def execute_sql(self, sql: str):
        """Execute SQL query

        Args:
            ds: Data source instance
            sql: SQL query statement

        Returns:
            Query results
        """
        SQLBotLogUtil.info(f"Executing SQL on ds_id {self.ds.id}: {sql}")
        try:
            return exec_sql(ds=self.ds, sql=sql, origin_column=False)
        except Exception as e:
            if isinstance(e, ParseSQLResultError):
                raise e
            else:
                err = traceback.format_exc(limit=1, chain=True)
                raise SQLBotDBError(err)

    def pop_chunk(self):
        try:
            chunk = self.chunk_list.pop(0)
            return chunk
        except IndexError as e:
            return None

    def await_result(self):
        while self.is_running():
            while True:
                chunk = self.pop_chunk()
                if chunk is not None:
                    yield chunk
                else:
                    break
        while True:
            chunk = self.pop_chunk()
            if chunk is None:
                break
            yield chunk

    def run_task_async(self, in_chat: bool = True, stream: bool = True,
                       finish_step: ChatFinishStep = ChatFinishStep.GENERATE_ANSWER, return_img: bool = True):
        if in_chat:
            stream = True
        self.future = executor.submit(self.run_task_cache, in_chat, stream, finish_step, return_img)

    def run_task_cache(self, in_chat: bool = True, stream: bool = True,
                       finish_step: ChatFinishStep = ChatFinishStep.GENERATE_ANSWER, return_img: bool = True):
        for chunk in self.run_task(in_chat, stream, finish_step, return_img):
            self.chunk_list.append(chunk)

    def _sql_phase(
        self,
        _session: Session,
        json_result: Dict[str, Any],
        in_chat: bool,
        stream: bool,
        finish_step: ChatFinishStep,
    ) -> Generator[Any, None, "SqlPhaseResult"]:
        """Chạy trọn pha SQL: sinh, kiểm quyền, thực thi rồi lưu dữ liệu.

        Bao gồm cả vòng thử lại và nhánh hạ cấp khi hết lượt thử.

        Bóc khỏi ``run_task`` để pha này gọi được từ nơi khác, cụ thể là từ
        một tool handler khi pipeline chuyển sang mô hình agent — lúc đó
        ``run_task`` không còn là người gọi duy nhất. Hành vi giữ nguyên tuyệt
        đối so với bản nằm inline.

        Generator này có HAI kênh ra, đừng nhầm:

        - kênh ``yield`` chở event SSE (hoặc markdown ở nhánh MCP) đi thẳng
            lên client, người gọi chỉ cần ``yield from``;
        - kênh ``return`` chở kết quả pha về cho người gọi, qua
            ``SqlPhaseResult``.

        Bẫy quan trọng: ``return`` ở đây KHÔNG còn kết thúc cả request như khi
        khối này còn nằm trong ``run_task``. Khi ``finish_step`` dừng ở
        ``GENERATE_SQL``, pha tự phát nốt ``finish`` rồi trả về
        ``stopped=True``; người gọi bắt buộc phải kiểm cờ đó và ``return``
        theo. Bỏ sót thì request chảy tiếp sang pha chart sau khi đã phát
        ``finish`` — mà client dừng đọc ngay tại ``finish``, nên triệu chứng
        là biểu đồ mất im lặng chứ không phải lỗi ồn ào.

        ``json_result`` được truyền vào và sửa TẠI CHỖ (khóa ``sql``, ``data``,
        ``title``): nó là kênh phụ cho nhánh không-stream. Giữ nguyên cách cũ
        để bước bóc này không kéo theo bất kỳ thay đổi hành vi nào khác.

        Ngoại lệ vẫn ném ra ngoài như cũ — ``yield from`` cho nó đi xuyên lên
        khối ``except`` tổng của ``run_task``.
        """
        # Pha SQL (sinh → kiểm tra → chạy) hỏng vì nhiều lý do phần lớn là ngẫu nhiên: LLM trả
        # JSON sai định dạng, từ chối vì hiểu nhầm câu hỏi, sinh ra SQL không parse được bảng
        # nào, hoặc SQL chạy lỗi trên DB. Thử lại tại chỗ với thông báo lỗi đưa ngược vào hội
        # thoại cứu được đa số ca mà không cần người dùng hỏi lại.
        #
        # Hết lượt thử vẫn hỏng thì KHÔNG ném lỗi ra ngoài nữa: chuyển sang nhánh answer
        # fallback ở khối except. Câu hỏi làm gãy pha SQL thường là câu tham chiếu ngược
        # ("trong đó bảng nào tên dài nhất") — dữ liệu để trả lời đã có từ lượt trước, chỉ là
        # không diễn đạt được thành SQL mới, nên trả về event `error` là phí một câu trả lời
        # hoàn toàn khả thi.
        max_sql_retry = max(0, settings.LLM_SQL_MAX_RETRY)
        sql_attempt = 0
        retry_reason: Optional[str] = None
        degraded_reason: Optional[str] = None
        result: Optional[Dict[str, Any]] = None
        tables = None
        chart_type = None
        _data = None

        while True:
            try:
                # generate sql
                sql_res = self.generate_sql(_session, retry_reason=retry_reason)
                full_sql_text = ""
                for chunk in sql_res:
                    full_sql_text += chunk.get("content")
                    if in_chat:
                        yield "data:" + orjson.dumps(
                            {
                                "content": chunk.get("content"),
                                "reasoning_content": chunk.get("reasoning_content"),
                                "type": "sql-result",
                            }
                        ).decode() + "\n\n"
                if in_chat:
                    yield "data:" + orjson.dumps(
                        {"type": "info", "msg": "sql generated"}
                    ).decode() + "\n\n"
                # filter sql
                SQLBotLogUtil.info(full_sql_text)

                chart_type = self.get_chart_type_from_sql_answer(full_sql_text)

                # return title
                # Chỉ đặt tiêu đề ở lần thử đầu: lần retry đặt lại chỉ sinh thêm một event
                # `brief` thứ hai cho cùng một hội thoại, client không có cách nào hiểu đúng.
                if self.change_title and sql_attempt == 0:
                    llm_brief = self.get_brief_from_sql_answer(full_sql_text)
                    llm_brief_generated = bool(llm_brief)
                    if llm_brief_generated or (
                        self.chat_question.question
                        and self.chat_question.question.strip() != ""
                    ):
                        save_brief = (
                            llm_brief
                            if (llm_brief and llm_brief != "")
                            else trim_chat_brief(self.chat_question.question)
                        )
                        brief = rename_chat(
                            session=_session,
                            rename_object=RenameChat(
                                id=self.get_record().chat_id,
                                brief=save_brief,
                                brief_generate=llm_brief_generated,
                            ),
                        )
                        if in_chat:
                            yield "data:" + orjson.dumps(
                                {"type": "brief", "brief": brief}
                            ).decode() + "\n\n"
                        if not stream:
                            json_result["title"] = brief

                use_dynamic_ds: bool = (
                    self.current_assistant
                    and self.current_assistant.type in dynamic_ds_types
                )
                is_page_embedded: bool = (
                    self.current_assistant and self.current_assistant.type == 4
                )
                dynamic_sql_result = None
                sqlbot_temp_sql_text = None
                assistant_dynamic_sql = None
                # row permission

                sql_operate = OperationEnum.GENERATE_SQL
                sql, tables = self.check_sql(
                    session=_session, res=full_sql_text, operate=sql_operate
                )

                # 表名安全检查：用 sqlglot 解析真实 SQL，不信任 AI 返回的 tables
                try:
                    actual_tables = extract_tables_from_sql(sql, ds_type=self.ds.type)
                except Exception as parse_error:
                    raise SingleMessageError(
                        f"SQL parsing failed: {parse_error}. "
                        f"This may indicate an unsupported SQL syntax or a security issue."
                    )
                # Parse được nhưng không tham chiếu bảng nào thì cho chạy: loại SQL này (đếm
                # trên hằng số, subquery toàn literal…) không đọc được dữ liệu nào nên không có
                # gì để rò rỉ. Đây là cách duy nhất trả lời được các câu hỏi về chính schema,
                # vd "datasource có bao nhiêu bảng".
                allowed_tables = set(self.table_name_list)
                unauthorized_tables = actual_tables - allowed_tables
                if unauthorized_tables:
                    raise SingleMessageError(
                        f"SQL contains unauthorized tables: {', '.join(unauthorized_tables)}. "
                        f"Allowed tables: {', '.join(allowed_tables)}"
                    )

                if (
                    (not self.current_assistant or is_page_embedded)
                    and is_normal_user(self.current_user)
                ) or use_dynamic_ds:
                    sql_result = None

                    if use_dynamic_ds:
                        dynamic_sql_result = self.generate_assistant_dynamic_sql(
                            _session, sql, tables
                        )
                        sqlbot_temp_sql_text = (
                            dynamic_sql_result.get("sqlbot_temp_sql_text")
                            if dynamic_sql_result
                            else None
                        )
                    else:
                        sql_result = self.generate_filter(
                            _session, sql, tables
                        )  # maybe no sql and tables

                    if sql_result:
                        SQLBotLogUtil.info(sql_result)
                        sql_operate = OperationEnum.GENERATE_SQL_WITH_PERMISSIONS
                        sql = self.check_save_sql(
                            session=_session, res=sql_result, operate=sql_operate
                        )
                    elif dynamic_sql_result and sqlbot_temp_sql_text:
                        sql_operate = OperationEnum.GENERATE_DYNAMIC_SQL
                        assistant_dynamic_sql = self.check_save_sql(
                            session=_session, res=sqlbot_temp_sql_text, operate=sql_operate
                        )
                    else:
                        sql = self.check_save_sql(
                            session=_session, res=full_sql_text, operate=sql_operate
                        )
                else:
                    sql = self.check_save_sql(
                        session=_session, res=full_sql_text, operate=sql_operate
                    )

                SQLBotLogUtil.info("sql: " + sql)

                if not stream:
                    json_result["sql"] = sql

                format_sql = sqlparse.format(sql, reindent=True)
                if in_chat:
                    yield "data:" + orjson.dumps(
                        {"content": format_sql, "type": "sql"}
                    ).decode() + "\n\n"
                else:
                    if stream:
                        yield f"```sql\n{format_sql}\n```\n\n"

                # execute sql
                real_execute_sql = sql
                if sqlbot_temp_sql_text and assistant_dynamic_sql:
                    dynamic_sql_result.pop("sqlbot_temp_sql_text")
                    for origin_table, subsql in dynamic_sql_result.items():
                        assistant_dynamic_sql = assistant_dynamic_sql.replace(
                            f"{dynamic_subsql_prefix}{origin_table}", subsql
                        )
                    real_execute_sql = assistant_dynamic_sql

                if finish_step.value <= ChatFinishStep.GENERATE_SQL.value:
                    if in_chat:
                        yield "data:" + orjson.dumps({"type": "finish"}).decode() + "\n\n"
                    if not stream:
                        yield json_result
                    # Pha đã tự phát nốt event cuối. `return` ở đây chỉ
                    # thoát generator này, nên phải báo cờ để người gọi
                    # dừng theo.
                    return SqlPhaseResult(
                        tables=tables, chart_type=chart_type, stopped=True
                    )

                # Cưỡng chế scope của quyền SW: bọc mọi tham chiếu tới bảng có scope thành
                # derived table `(scope) AS <bảng>` — tất định bằng sqlglot, không qua LLM.
                # Chỉ đổi bản THỰC THI (real_execute_sql, cùng khuôn với nhánh assistant động);
                # bản `sql` đã phát cho client giữ nguyên để không rò điều kiện lọc quyền.
                # Rewrite hỏng thì từ chối thực thi — tuyệt đối không fallback về SQL chưa bọc.
                if (
                    self.sw_permission
                    and self.sw_permission.restricted
                    and self.sw_permission.scope_queries
                ):
                    try:
                        real_execute_sql = apply_scope_to_sql(
                            real_execute_sql, self.sw_permission.scope_queries, self.ds.type
                        )
                    except Exception as rewrite_error:
                        raise SingleMessageError(
                            f"Không áp được giới hạn phạm vi dữ liệu lên SQL, "
                            f"từ chối thực thi: {rewrite_error}"
                        )

                self.current_logs[OperationEnum.EXECUTE_SQL] = start_log(
                    session=_session,
                    operate=OperationEnum.EXECUTE_SQL,
                    record_id=self.record.id,
                    local_operation=True,
                )
                result = self.execute_sql(sql=real_execute_sql)
                self.current_logs[OperationEnum.EXECUTE_SQL] = end_log(
                    session=_session,
                    log=self.current_logs[OperationEnum.EXECUTE_SQL],
                    full_message={
                        "sql": real_execute_sql,
                        "count": len(result.get("data")),
                    },
                )

                _data = DataFormat.convert_large_numbers_in_object_array(result.get("data"))
                _data = DataFormat.normalize_qualified_sql_column_keys_in_object_array(
                    _data
                )
                result["data"] = _data

                self.save_sql_data(session=_session, data_obj=result)
                if in_chat:
                    # Chở luôn kết quả SQL trong `data` thay vì bắt client gọi thêm
                    # `GET /chat/record/{id}/data`. Giữ nguyên `content` là chuỗi
                    # 'execute-success' như hợp đồng cũ: client cũ chỉ đọc `type` nên không
                    # vỡ, client mới thấy có `data` thì dùng thẳng.
                    #
                    # Phát ở đây là chỗ sớm nhất có thể, và cũng là lúc rẻ nhất: pha sinh
                    # câu trả lời chưa khởi động nên đường truyền đang rỗng, khối dữ liệu
                    # đi qua mà không chen ngang token nào.
                    yield "data:" + orjson.dumps(
                        {
                            "content": "execute-success",
                            "type": "sql-data",
                            "data": self.build_sql_data_payload(result),
                        }
                    ).decode() + "\n\n"
                if not stream:
                    json_result["data"] = get_chat_chart_data(_session, self.record.id)
            except (
                SingleMessageError,
                SQLBotDBError,
                ParseSQLResultError,
                SQLBotDBConnectionError,
            ) as e:
                reason = _humanize_error(str(e))
                # Mất kết nối DB thì sinh lại SQL không cứu được gì — bỏ qua thẳng sang hạ cấp.
                can_retry = not isinstance(e, SQLBotDBConnectionError)
                if can_retry and sql_attempt < max_sql_retry:
                    sql_attempt += 1
                    retry_reason = reason
                    # Cố ý không phát event SSE nào cho lần sinh lại: hợp đồng stream giữ nguyên
                    # như trước, client không phải biết tới khái niệm retry. Dấu vết nằm ở log.
                    SQLBotLogUtil.warning(
                        f"SQL phase failed (attempt {sql_attempt}/{max_sql_retry}), retrying: {reason}"
                    )
                    continue
                # Hết lượt thử. Chỉ hạ cấp khi client thực sự chờ pha answer: nhánh MCP và các
                # finish_step dừng sớm vẫn cần nhận lỗi thật để giữ nguyên hợp đồng cũ.
                if not (
                    in_chat
                    and settings.LLM_ANSWER_ON_FAILURE
                    and finish_step.value >= ChatFinishStep.GENERATE_ANSWER.value
                ):
                    raise
                degraded_reason = str(e)
                # Cố ý KHÔNG gọi save_error: giao diện web hiện khối lỗi đỏ với mọi record có
                # chat_record.error khác rỗng, mà lượt này sắp có câu trả lời tử tế. Lý do hỏng
                # vẫn nằm đủ trong log ở dòng dưới để truy vết.
                SQLBotLogUtil.warning(
                    f"SQL phase failed after {sql_attempt} retry, "
                    f"falling back to answer-only: {reason}"
                )
                break
            break

        return SqlPhaseResult(
            result=result,
            tables=tables,
            chart_type=chart_type,
            data=_data,
            degraded_reason=degraded_reason,
        )


    def run_task(self, in_chat: bool = True, stream: bool = True,
                 finish_step: ChatFinishStep = ChatFinishStep.GENERATE_ANSWER, return_img: bool = True):
        json_result: Dict[str, Any] = {'success': True}
        _session = None
        try:
            _session = session_maker()
            if self.ds:
                oid = self.ds.oid if isinstance(self.ds, CoreDatasource) else 1
                ds_id = self.ds.id if isinstance(self.ds, CoreDatasource) else None

                self.filter_terminology_template(_session, oid, ds_id)

                self.filter_training_template(_session, oid, ds_id)

                self.filter_custom_prompts(_session, CustomPromptTypeEnum.GENERATE_SQL, oid, ds_id)

                self.init_messages(_session)

            # return id
            if in_chat:
                yield 'data:' + orjson.dumps({'type': 'id', 'id': self.get_record().id}).decode() + '\n\n'
                if self.get_record().regenerate_record_id:
                    yield 'data:' + orjson.dumps({'type': 'regenerate_record_id',
                                                  'regenerate_record_id': self.get_record().regenerate_record_id}).decode() + '\n\n'
                yield 'data:' + orjson.dumps(
                    {'type': 'question', 'question': self.get_record().question}).decode() + '\n\n'
            else:
                if stream:
                    yield '> ' + self.trans('i18n_chat.record_id_in_mcp') + str(self.get_record().id) + '\n'
                    yield '> ' + self.get_record().question + '\n\n'
            if not stream:
                json_result['record_id'] = self.get_record().id

                # select datasource if datasource is none
            if not self.ds:
                ds_res = self.select_datasource(_session)

                for chunk in ds_res:
                    SQLBotLogUtil.info(chunk)
                    if in_chat:
                        yield 'data:' + orjson.dumps(
                            {'content': chunk.get('content'), 'reasoning_content': chunk.get('reasoning_content'),
                             'type': 'datasource-result'}).decode() + '\n\n'
                if in_chat:
                    yield 'data:' + orjson.dumps({'id': self.ds.id, 'datasource_name': self.ds.name,
                                                  'engine_type': self.ds.type_name or self.ds.type,
                                                  'type': 'datasource'}).decode() + '\n\n'

            else:
                self.validate_history_ds(_session)

            # check connection
            connected = check_connection(ds=self.ds, trans=None)
            if not connected:
                raise SQLBotDBConnectionError('Connect DB failed')

            # Cổng định tuyến: lượt này có cần chạy pha SQL không. Đặt SAU check_connection vì
            # hai lẽ — mất kết nối phải là lỗi thật chứ không được hoá thành một câu trả lời chữ
            # tử tế, và tới đây `table_name_list` mới chắc chắn đã có (init_messages chạy ở nhánh
            # có sẵn ds, hoặc ở cuối select_datasource).
            #
            # Chỉ nhánh in_chat được rẽ. Toàn bộ pha answer nằm trong khối `if in_chat` bên dưới,
            # nên rẽ một lượt MCP sang answer là trả về kết quả RỖNG mà không kèm lỗi nào — hỏng
            # im lặng, đúng loại khó lần nhất. Chốt `finish_step` là dây an toàn thứ hai: người
            # gọi đã nói rõ họ chỉ cần SQL hoặc dữ liệu thì không có gì để định tuyến.
            route = fail_open('disabled')
            if in_chat and finish_step.value > ChatFinishStep.QUERY_DATA.value:
                route = decide_route(self, _session)
                if route.forced != 'disabled':
                    _route_msg = f'route {route.action}/{route.category}'
                    if route.forced:
                        _route_msg += f' (forced: {route.forced})'
                    yield 'data:' + orjson.dumps({'type': 'info', 'msg': _route_msg}).decode() + '\n\n'

            fallback_kind = 'failed'
            if route.skip_sql:
                # Bỏ hẳn pha SQL. Đi lại đúng nhánh hạ cấp sẵn có (không có dữ liệu thì trả lời
                # bằng lịch sử + danh sách bảng) thay vì dựng nhánh thứ hai song song: hai nhánh
                # cùng làm một việc sẽ trôi khỏi nhau. Khác biệt gói gọn trong `fallback_kind` —
                # lượt này KHÔNG có sự cố nào để kể, mà prompt hạ cấp cũ thì khẳng định là có.
                fallback_kind = 'no_query'
                result = None
                tables = None
                chart_type = None
                _data = None
                degraded_reason = route.reason or 'Câu hỏi này không cần truy vấn dữ liệu mới.'
            else:
                # Pha SQL bóc riêng: `yield from` chở event SSE lên thẳng
                # client, còn giá trị trả về mang kết quả pha sang các pha sau.
                # Cờ `stopped` phải kiểm ngay — xem docstring của `_sql_phase`
                # để biết vì sao bỏ qua nó là một lỗi im lặng.
                sql_phase = yield from self._sql_phase(
                    _session, json_result, in_chat, stream, finish_step
                )
                if sql_phase.stopped:
                    return
                result = sql_phase.result
                tables = sql_phase.tables
                chart_type = sql_phase.chart_type
                _data = sql_phase.data
                degraded_reason = sql_phase.degraded_reason

            if finish_step.value <= ChatFinishStep.QUERY_DATA.value:
                if stream:
                    if in_chat:
                        yield 'data:' + orjson.dumps({'type': 'finish'}).decode() + '\n\n'
                    else:
                        _column_list = []
                        for field in result.get('fields'):
                            _column_list.append(AxisObj(name=field, value=field))

                        md_data, _fields_list = DataFormat.convert_object_array_for_pandas(_column_list,
                                                                                           result.get('data'))

                        # data, _fields_list, col_formats = self.format_pd_data(_column_list, result.get('data'))

                        if not _data or not _fields_list:
                            yield 'The SQL execution result is empty.\n\n'
                        else:
                            df = pd.DataFrame(_data, columns=_fields_list)
                            df_safe = DataFormat.safe_convert_to_string(df)
                            markdown_table = df_safe.to_markdown(index=False)
                            yield markdown_table + '\n\n'
                else:
                    yield json_result
                return

            # Sinh câu trả lời chữ SONG SONG với pha sinh chart: answer chỉ cần fields/data (đã có
            # ngay sau khi chạy SQL) nên không phải chờ chart, và chart cũng không phải chờ answer.
            # Chỉ answer phát event token; chart về trọn gói một event ở cuối pha, nên trên dây chỉ
            # thấy dòng `answer-result` bị chen ngang đúng hai event mốc của chart.
            # Hiện chỉ bật cho nhánh in_chat (SSE của UI); nhánh MCP (markdown / JSON tổng hợp) chưa
            # dùng tới nên chưa nối vào, tránh đổi hành vi của client MCP đang chạy.
            answer_queue: Optional[queue.Queue] = None
            answer_state: Dict[str, Any] = {}
            recommend_queue: Optional[queue.Queue] = None
            recommend_state: Dict[str, Any] = {}
            if in_chat:
                if degraded_reason:
                    # Lượt này không có dữ liệu mới: trả lời dựa trên lịch sử hội thoại.
                    # Không phát event riêng cho nhánh này: với client, lượt hạ cấp chỉ khác lượt
                    # bình thường ở chỗ thiếu `sql` / `sql-data`, còn lại vẫn answer-result → answer
                    # → finish như cũ.
                    answer_sys_prompt, answer_user_prompt = self.build_answer_fallback_prompts(_session,
                                                                                               degraded_reason,
                                                                                               fallback_kind)
                else:
                    answer_sys_prompt, answer_user_prompt = self.build_answer_prompts(_session,
                                                                                      result.get('fields'),
                                                                                      result.get('data'))
                answer_queue = queue.Queue()
                executor.submit(self.run_answer_worker, answer_sys_prompt, answer_user_prompt, answer_queue)

                # Pha gợi ý câu hỏi tiếp theo: thread thứ ba, song song với answer và chart. Khởi
                # động ở ĐÂY chứ không sớm hơn là có chủ ý — mọi lỗi cứng của pha SQL (mất kết nối,
                # domainCode ngoài quyền, phễu bảng rỗng) đều thoát trước điểm này, nên lượt hỏng
                # hẳn không tốn thêm một lời gọi LLM nào. Lượt hạ cấp thì ngược lại, VẪN đi qua đây:
                # người dùng vừa hỏi hụt là lúc gợi ý có ích nhất.
                if settings.CHAT_INLINE_RECOMMEND_ENABLED and self.chat_question.db_schema:
                    recommend_queue = queue.Queue()
                    executor.submit(self.run_recommend_worker, recommend_queue)

            # Nhánh hạ cấp không có SQL lẫn dữ liệu nên mọi pha sau answer (biểu đồ, phân tích) đều
            # vô nghĩa — dừng ngay tại đây bất kể finish_step yêu cầu đi xa tới đâu.
            if degraded_reason or finish_step.value <= ChatFinishStep.GENERATE_ANSWER.value:
                yield from self.drain_answer_queue(answer_queue, answer_state, block=True)
                yield from self.drain_recommend_queue(recommend_queue, recommend_state, block=True)
                if in_chat:
                    yield 'data:' + orjson.dumps({'type': 'finish'}).decode() + '\n\n'
                if not stream:
                    yield json_result
                return

            # generate chart
            #
            # Cấu hình biểu đồ KHÔNG phát ra client theo từng token như `answer`. Lý do: answer là
            # văn xuôi nên hiện dần vẫn có nghĩa, còn cái này là MỘT khối JSON — mảnh dở dang không
            # parse được, client chẳng làm được gì với nó cho tới lúc nhận đủ. Vẫn lặp qua từng
            # chunk vì vòng lặp này là chỗ vét queue answer xen giữa, giữ cho câu trả lời chảy đều
            # trong lúc chờ pha biểu đồ thay vì dồn cục ở cuối.
            chart: Optional[Dict[str, Any]] = None
            chart_failed = False
            try:
                used_tables_schema, used_tables = self.out_ds_instance.get_db_schema(
                    self.ds.id, self.chat_question.question, embedding=False,
                    table_list=tables) if self.out_ds_instance else get_table_schema(
                    session=_session,
                    current_user=self.current_user,
                    ds=self.ds,
                    question=self.chat_question.question,
                    embedding=False, table_list=tables,
                    column_filter=self.sw_permission.allowed_columns
                    if self.sw_permission and self.sw_permission.restricted else None)
                SQLBotLogUtil.info('used_tables_schema: \n' + used_tables_schema)
                chart_res = self.generate_chart(_session, chart_type, used_tables_schema)
                full_chart_text = ''
                for chunk in chart_res:
                    full_chart_text += chunk.get('content')
                    if in_chat:
                        # Vét không chặn: answer chảy ra cùng nhịp với chart.
                        yield from self.drain_answer_queue(answer_queue, answer_state, block=False)

                # filter chart
                SQLBotLogUtil.info(full_chart_text)
                chart = self.check_save_chart(session=_session, res=full_chart_text)
                SQLBotLogUtil.info(chart)
            except Exception as e:
                # Pha biểu đồ hỏng KHÔNG được giết cả lượt hỏi. SQL đã chạy xong, câu trả lời chữ —
                # thứ client thực sự tiêu thụ — có thể đã sinh xong và đang nằm chờ trong queue; ném
                # tiếp ra ngoài ở đây là mất cả `answer` lẫn `finish` chỉ vì một pha phụ. Xử lý y hệt
                # pha answer: báo bằng event `info` rồi đi tiếp, và KHÔNG gọi save_error vì web UI
                # hiện khối lỗi đỏ với mọi record có `chat_record.error` khác rỗng.
                #
                # Nhánh ngoài app (MCP) vẫn ném như cũ: ở đó biểu đồ là sản phẩm chính chứ không
                # phải pha phụ, và hợp đồng cũ của nó không có khái niệm "biểu đồ hỏng nhưng vẫn ok".
                if not in_chat:
                    raise
                chart_failed = True
                traceback.print_exc()
                SQLBotLogUtil.warning(f'Generate chart failed, keeping the answer: {e}')

            if not stream:
                json_result['chart'] = chart

            if in_chat:
                if chart_failed:
                    yield 'data:' + orjson.dumps({'type': 'info', 'msg': 'chart failed'}).decode() + '\n\n'
                else:
                    yield 'data:' + orjson.dumps({'type': 'info', 'msg': 'chart generated'}).decode() + '\n\n'
                    yield 'data:' + orjson.dumps(
                        {'content': orjson.dumps(chart).decode(), 'type': 'chart'}).decode() + '\n\n'
            else:
                if stream:
                    md_data, _fields_list = DataFormat.convert_data_fields_for_pandas(chart, result.get('fields'),
                                                                                      result.get('data'))
                    # data, _fields_list, col_formats = self.format_pd_data(_column_list, result.get('data'))

                    if not md_data or not _fields_list:
                        yield 'The SQL execution result is empty.\n\n'
                    else:
                        df = pd.DataFrame(md_data, columns=_fields_list)
                        df_safe = DataFormat.safe_convert_to_string(df)
                        markdown_table = df_safe.to_markdown(index=False)
                        yield markdown_table + '\n\n'

            if in_chat:
                # Chart xong trước answer là chuyện bình thường (chart thường nhanh hơn), nên chặn
                # chờ nốt ở đây. 'finish' phải là event cuối cùng — client dừng đọc ngay khi thấy nó.
                yield from self.drain_answer_queue(answer_queue, answer_state, block=True)
                yield from self.drain_recommend_queue(recommend_queue, recommend_state, block=True)
                yield 'data:' + orjson.dumps({'type': 'finish'}).decode() + '\n\n'
            else:
                # generate picture
                try:
                    if chart.get('type') != 'table' and return_img:
                        # yield '### generated chart picture\n\n'
                        self.current_logs[OperationEnum.GENERATE_PICTURE] = start_log(session=_session,
                                                                                      operate=OperationEnum.GENERATE_PICTURE,
                                                                                      record_id=self.record.id,
                                                                                      local_operation=True)
                        image_url, error = request_picture(self.record.chat_id, self.record.id, chart,
                                                           format_json_data(result))
                        SQLBotLogUtil.info(image_url)
                        if stream:
                            yield f'![{chart.get("type")}]({image_url})'
                        else:
                            json_result['image_url'] = image_url
                        if error is not None:
                            raise error

                        self.current_logs[OperationEnum.GENERATE_PICTURE] = end_log(session=_session,
                                                                                    log=self.current_logs[
                                                                                        OperationEnum.GENERATE_PICTURE],
                                                                                    full_message=image_url)
                except Exception as e:
                    if stream:
                        if chart.get('type') != 'table':
                            yield 'generate or fetch chart picture error.\n\n'
                        raise e

            if not stream:
                yield json_result

        except Exception as e:
            traceback.print_exc()
            error_msg: str
            if isinstance(e, SingleMessageError):
                error_msg = str(e)
            elif isinstance(e, SQLBotDBConnectionError):
                error_msg = orjson.dumps(
                    {'message': str(e), 'type': 'db-connection-err'}).decode()
            elif isinstance(e, SQLBotDBError):
                error_msg = orjson.dumps(
                    {'message': 'Execute SQL Failed', 'traceback': str(e), 'type': 'exec-sql-err'}).decode()
            else:
                error_msg = orjson.dumps({'message': str(e), 'traceback': traceback.format_exc(limit=1)}).decode()
            if _session:
                self.save_error(session=_session, message=error_msg)
            if in_chat:
                yield 'data:' + orjson.dumps({'content': error_msg, 'type': 'error'}).decode() + '\n\n'
            else:
                if stream:
                    yield f'&#x274c; **ERROR:**\n'
                    yield f'> {error_msg}\n'
                else:
                    json_result['success'] = False
                    json_result['message'] = error_msg
                    yield json_result
        finally:
            self.finish(_session)
            session_maker.remove()

    def run_recommend_questions_task_async(self):
        self.future = executor.submit(self.run_recommend_questions_task_cache)

    def run_recommend_questions_task_cache(self):
        for chunk in self.run_recommend_questions_task():
            self.chunk_list.append(chunk)

    def run_recommend_questions_task(self):
        try:
            _session = session_maker()
            res = self.generate_recommend_questions_task(_session)

            for chunk in res:
                if chunk.get('recommended_question'):
                    yield 'data:' + orjson.dumps(
                        {'content': chunk.get('recommended_question'),
                         'type': 'recommended_question'}).decode() + '\n\n'
                else:
                    yield 'data:' + orjson.dumps(
                        {'content': chunk.get('content'), 'reasoning_content': chunk.get('reasoning_content'),
                         'type': 'recommended_question_result'}).decode() + '\n\n'
        except Exception:
            traceback.print_exc()
        finally:
            session_maker.remove()

    def run_analysis_or_predict_task_async(self, session: Session, action_type: str, base_record: ChatRecord,
                                           in_chat: bool = True, stream: bool = True):
        self.set_record(save_analysis_predict_record(session, base_record, action_type))
        self.future = executor.submit(self.run_analysis_or_predict_task_cache, action_type, in_chat, stream)

    def run_analysis_or_predict_task_cache(self, action_type: str, in_chat: bool = True, stream: bool = True):
        for chunk in self.run_analysis_or_predict_task(action_type, in_chat, stream):
            self.chunk_list.append(chunk)

    def run_analysis_or_predict_task(self, action_type: str, in_chat: bool = True, stream: bool = True):
        json_result: Dict[str, Any] = {'success': True}
        _session = None
        try:
            _session = session_maker()
            if in_chat:
                yield 'data:' + orjson.dumps({'type': 'id', 'id': self.get_record().id}).decode() + '\n\n'
            else:
                if stream:
                    yield '> ' + self.trans('i18n_chat.record_id_in_mcp') + str(self.get_record().id) + '\n'
                    yield '> ' + self.get_record().question + '\n\n'
            if not stream:
                json_result['record_id'] = self.get_record().id

            if action_type == 'analysis':
                # generate analysis
                analysis_res = self.generate_analysis(_session)
                full_text = ''
                for chunk in analysis_res:
                    full_text += chunk.get('content')
                    if in_chat:
                        yield 'data:' + orjson.dumps(
                            {'content': chunk.get('content'), 'reasoning_content': chunk.get('reasoning_content'),
                             'type': 'analysis-result'}).decode() + '\n\n'
                    else:
                        if stream:
                            yield chunk.get('content')
                if in_chat:
                    yield 'data:' + orjson.dumps({'type': 'info', 'msg': 'analysis generated'}).decode() + '\n\n'
                    yield 'data:' + orjson.dumps({'type': 'analysis_finish'}).decode() + '\n\n'
                else:
                    if stream:
                        yield '\n\n'
                if not stream:
                    json_result['content'] = full_text

            elif action_type == 'predict':
                # generate predict
                analysis_res = self.generate_predict(_session)
                full_text = ''
                for chunk in analysis_res:
                    full_text += chunk.get('content')
                    if in_chat:
                        yield 'data:' + orjson.dumps(
                            {'content': chunk.get('content'), 'reasoning_content': chunk.get('reasoning_content'),
                             'type': 'predict-result'}).decode() + '\n\n'
                if in_chat:
                    yield 'data:' + orjson.dumps({'type': 'info', 'msg': 'predict generated'}).decode() + '\n\n'

                has_data = self.check_save_predict_data(session=_session, res=full_text)
                if has_data:
                    if in_chat:
                        yield 'data:' + orjson.dumps({'type': 'predict-success'}).decode() + '\n\n'
                    else:
                        chart = get_chat_chart_config(_session, self.record.id)
                        origin_data = get_chat_chart_data(_session, self.record.id)
                        predict_data = get_chat_predict_data(_session, self.record.id)

                        if stream:
                            md_data, _fields_list = DataFormat.convert_data_fields_for_pandas(chart,
                                                                                              origin_data.get('fields'),
                                                                                              predict_data)
                            if not md_data or not _fields_list:
                                yield 'Predict data result is empty.\n\n'
                            else:
                                df = pd.DataFrame(md_data, columns=_fields_list)
                                df_safe = DataFormat.safe_convert_to_string(df)
                                markdown_table = df_safe.to_markdown(index=False)
                                yield markdown_table + '\n\n'

                        else:
                            json_result['origin_data'] = origin_data
                            json_result['predict_data'] = predict_data

                        # generate picture
                        try:
                            if chart.get('type') != 'table':
                                # yield '### generated chart picture\n\n'

                                _data = get_chat_chart_data(_session, self.record.id)
                                _data['data'] = _data.get('data') + predict_data

                                image_url, error = request_picture(self.record.chat_id, self.record.id, chart,
                                                                   format_json_data(_data))
                                SQLBotLogUtil.info(image_url)
                                if stream:
                                    yield f'![{chart.get("type")}]({image_url})'
                                else:
                                    json_result['image_url'] = image_url
                                if error is not None:
                                    raise error
                        except Exception as e:
                            if stream:
                                if chart.get('type') != 'table':
                                    yield 'generate or fetch chart picture error.\n\n'
                                raise e
                else:
                    if in_chat:
                        yield 'data:' + orjson.dumps({'type': 'predict-failed'}).decode() + '\n\n'
                    else:
                        if stream:
                            yield full_text + '\n\n'
                    if not stream:
                        json_result['success'] = False
                        json_result['message'] = full_text
                if in_chat:
                    yield 'data:' + orjson.dumps({'type': 'predict_finish'}).decode() + '\n\n'

            self.finish(_session)

            if not stream:
                yield json_result
        except Exception as e:
            traceback.print_exc()
            error_msg: str
            if isinstance(e, SingleMessageError):
                error_msg = str(e)
            else:
                error_msg = orjson.dumps({'message': str(e), 'traceback': traceback.format_exc(limit=1)}).decode()
            if _session:
                self.save_error(session=_session, message=error_msg)
            if in_chat:
                yield 'data:' + orjson.dumps({'content': error_msg, 'type': 'error'}).decode() + '\n\n'
            else:
                if stream:
                    yield f'&#x274c; **ERROR:**\n'
                    yield f'> {error_msg}\n'
                else:
                    json_result['success'] = False
                    json_result['message'] = error_msg
                    yield json_result
        finally:
            # end
            session_maker.remove()

    def validate_history_ds(self, session: Session):
        _ds = self.ds
        if not self.current_assistant or self.current_assistant.type == 4:
            try:
                current_ds = session.get(CoreDatasource, _ds.id)
                if not current_ds:
                    raise SingleMessageError('chat.ds_is_invalid')
            except Exception as e:
                raise SingleMessageError("chat.ds_is_invalid")
        else:
            try:
                _ds_list: list[dict] = get_assistant_ds(session=session, llm_service=self)
                match_ds = any(item.get("id") == _ds.id for item in _ds_list)
                if not match_ds:
                    type = self.current_assistant.type
                    msg = f"[please check ds list and public ds list]" if type == 0 else f"[please check ds api]"
                    raise SingleMessageError(msg)
            except Exception as e:
                raise SingleMessageError(f"ds is invalid [{str(e)}]")


def execute_sql_with_db(db: SQLDatabase, sql: str) -> str:
    """Execute SQL query using SQLDatabase

    Args:
        db: SQLDatabase instance
        sql: SQL query statement

    Returns:
        str: Query results formatted as string
    """
    try:
        # Execute query
        result = db.run(sql)

        if not result:
            return "Query executed successfully but returned no results."

        # Format results
        return str(result)

    except Exception as e:
        error_msg = f"SQL execution failed: {str(e)}"
        SQLBotLogUtil.exception(error_msg)
        raise RuntimeError(error_msg)


def request_picture(chat_id: int, record_id: int, chart: dict, data: dict):
    file_name = f'c_{chat_id}_r_{record_id}'

    columns = chart.get('columns') if chart.get('columns') else []
    x = None
    y = None
    series = None
    multi_quota_fields = []
    multi_quota_name = None

    if chart.get('axis'):
        axis_data = chart.get('axis')
        x = axis_data.get('x')
        y = axis_data.get('y')
        series = axis_data.get('series')
        # 获取multi-quota字段列表
        if axis_data.get('multi-quota') and 'value' in axis_data.get('multi-quota'):
            multi_quota_fields = axis_data.get('multi-quota').get('value', [])
            multi_quota_name = axis_data.get('multi-quota').get('name')

    axis = []
    for v in columns:
        axis.append({'name': v.get('name'), 'value': v.get('value')})
    if x:
        axis.append({'name': x.get('name'), 'value': x.get('value'), 'type': 'x'})
    if y:
        y_list = y if isinstance(y, list) else [y]

        for y_item in y_list:
            if isinstance(y_item, dict) and 'value' in y_item:
                y_obj = {
                    'name': y_item.get('name'),
                    'value': y_item.get('value'),
                    'type': 'y'
                }
                # 如果是multi-quota字段，添加标志
                if y_item.get('value') in multi_quota_fields:
                    y_obj['multi-quota'] = True
                axis.append(y_obj)
    if series:
        axis.append({'name': series.get('name'), 'value': series.get('value'), 'type': 'series'})
    if multi_quota_name:
        axis.append({'name': multi_quota_name, 'value': multi_quota_name, 'type': 'other-info'})

    request_obj = {
        "path": os.path.join(settings.MCP_IMAGE_PATH, file_name),
        "type": chart.get('type'),
        "data": orjson.dumps(data.get('data') if data.get('data') else []).decode(),
        "axis": orjson.dumps(axis).decode(),
    }

    _error = None
    try:
        requests.post(url=settings.MCP_IMAGE_HOST, json=request_obj, timeout=settings.SERVER_IMAGE_TIMEOUT)
    except Exception as e:
        _error = e

    request_path = urllib.parse.urljoin(settings.SERVER_IMAGE_HOST, f"{file_name}.png")

    return request_path, _error


def get_token_usage(chunk: BaseMessageChunk, token_usage: dict = None):
    try:
        if chunk.usage_metadata:
            if token_usage is None:
                token_usage = {}
            token_usage['input_tokens'] = chunk.usage_metadata.get('input_tokens')
            token_usage['output_tokens'] = chunk.usage_metadata.get('output_tokens')
            token_usage['total_tokens'] = chunk.usage_metadata.get('total_tokens')
    except Exception:
        pass


def process_stream(res: Iterator[BaseMessageChunk],
                   token_usage: Dict[str, Any] = None,
                   enable_tag_parsing: bool = settings.PARSE_REASONING_BLOCK_ENABLED,
                   start_tag: str = settings.DEFAULT_REASONING_CONTENT_START,
                   end_tag: str = settings.DEFAULT_REASONING_CONTENT_END
                   ):
    """Chuẩn hoá stream chunk của LLM thành cặp ``{content, reasoning_content}`` cho tầng trên.

    Phần suy luận của model về theo HAI đường hoàn toàn khác nhau, hàm này gộp chúng về một hợp đồng:

    - **Trường riêng của provider** (``additional_kwargs['reasoning_content']``): đường sạch, chỉ
      việc chuyển tiếp. Hệ thống đang tắt đường này bằng ``LLM_DISABLE_THINKING``, nhưng model nào
      không tôn trọng cờ đó thì vẫn rơi vào đây.
    - **Thẻ ``<think>...</think>`` nằm lẫn trong ``content``**: prompt sinh SQL chủ động yêu cầu
      model suy luận trong khối này (templates/template.yaml), nên đây mới là đường đang chạy thật.
      Cần tự bóc thẻ ra khỏi ``content``, kể cả khi thẻ bị cắt đôi giữa hai chunk — đó là việc của
      ``pending_start_tag`` và ``pending_end_tag``: mẩu đuôi khớp tiền tố của thẻ được giữ lại,
      chờ chunk sau ghép vào rồi mới xét. Thiếu bộ đệm cho thẻ ĐÓNG thì một stream cắt kiểu
      ``</th`` + ``ink>`` sẽ không bao giờ khớp thẻ, nuốt trọn câu trả lời vào phần suy luận và
      để ``content`` rỗng.

    Đã thấy trường riêng có nội dung thì bỏ hẳn việc so khớp thẻ: model đã tách sẵn hai luồng, mà
    ``<think>`` xuất hiện trong câu trả lời chính thức lúc đó là dữ liệu chứ không phải thẻ.

    Bất biến quan trọng: mỗi mẩu suy luận chỉ được phát ra ĐÚNG MỘT LẦN. ``current_thinking`` là
    biến tích luỹ nội bộ, không bao giờ được đem phát — phát nó ở thẻ đóng chính là lỗi khiến toàn
    bộ khối ``<think>`` đi qua dây hai lần (một lần theo token, một lần nguyên khối), client cộng
    dồn ``reasoning_content`` sẽ thấy nội dung nhân đôi.
    """
    if token_usage is None:
        token_usage = {}
    in_thinking_block = False  # 标记是否在思考过程块中
    current_thinking = ''  # 当前收集的思考过程内容
    pending_start_tag = ''  # 用于缓存可能被截断的开始标签部分
    pending_end_tag = ''  # mẩu đuôi có thể là phần đầu của thẻ đóng bị cắt, giữ chờ chunk sau

    full_reasoning_log = ''  # 累积完整的思考内容，用于最终日志输出
    full_content_log = ''  # 累积完整的输出内容，用于最终日志输出

    for chunk in res:
        reasoning_content_chunk = ''
        content = chunk.content
        output_content = ''  # 实际要输出的内容

        # 检查additional_kwargs中的reasoning_content
        if 'reasoning_content' in chunk.additional_kwargs:
            reasoning_content = chunk.additional_kwargs.get('reasoning_content', '')
            if reasoning_content is None:
                reasoning_content = ''

            # 累积additional_kwargs中的思考内容到current_thinking
            current_thinking += reasoning_content
            reasoning_content_chunk = reasoning_content

        # 只有当current_thinking不是空字符串时才跳过标签解析
        if not in_thinking_block and current_thinking.strip() != '':
            output_content = content  # 正常输出content
            full_content_log += output_content
            full_reasoning_log += reasoning_content_chunk
            yield {
                'content': output_content,
                'reasoning_content': reasoning_content_chunk
            }
            get_token_usage(chunk, token_usage)
            continue  # 跳过后续的标签解析逻辑

        # 如果没有有效的思考内容，并且启用了标签解析，才执行标签解析逻辑
        # 如果有缓存的开始标签部分，先拼接当前内容
        if pending_start_tag:
            content = pending_start_tag + content
            pending_start_tag = ''

        # Chunk trước có giữ lại một mẩu nghi là đầu thẻ đóng thì ghép vào rồi mới xét tiếp.
        if pending_end_tag:
            content = pending_end_tag + content
            pending_end_tag = ''

        # 检查是否开始思考过程块（处理可能被截断的开始标签）
        if enable_tag_parsing and not in_thinking_block and start_tag:
            if start_tag in content:
                start_idx = content.index(start_tag)
                # 只有当开始标签前面没有其他文本时才认为是真正的思考块开始
                if start_idx == 0 or content[:start_idx].strip() == '':
                    # 完整标签存在且前面没有其他文本
                    output_content += content[:start_idx]  # 输出开始标签之前的内容
                    content = content[start_idx + len(start_tag):]  # 移除开始标签
                    in_thinking_block = True
                else:
                    # 开始标签前面有其他文本，不认为是思考块开始
                    output_content += content
                    content = ''
            else:
                # 检查是否可能有部分开始标签
                for i in range(1, len(start_tag)):
                    if content.endswith(start_tag[:i]):
                        # 只有当当前内容全是空白时才缓存部分标签
                        if content[:-i].strip() == '':
                            pending_start_tag = start_tag[:i]
                            content = content[:-i]  # 移除可能的部分标签
                            output_content += content
                            content = ''
                        break

        # 处理思考块内容
        if enable_tag_parsing and in_thinking_block and end_tag:
            if end_tag in content:
                # Gặp thẻ đóng: chỉ phát PHẦN CÒN LẠI của khối suy luận nằm trước thẻ. Những token
                # phía trước đã được phát từng mẩu ở nhánh `else` bên dưới rồi.
                end_idx = content.index(end_tag)
                reasoning_content_chunk += content[:end_idx]
                content = content[end_idx + len(end_tag):]  # bỏ thẻ đóng, giữ phần sau nó
                # Dọn biến tích luỹ để điều kiện đi tắt ở đầu vòng lặp (dành cho đường
                # `additional_kwargs`) không hiểu nhầm là model đang có suy luận.
                current_thinking = ''
                in_thinking_block = False
                output_content += content  # phần sau thẻ đóng là câu trả lời chính thức
            else:
                # Chưa tới thẻ đóng. Nhưng thẻ có thể bị tokenizer cắt đôi, nên nếu đuôi chunk
                # khớp một tiền tố của thẻ đóng thì giữ mẩu đó lại thay vì phát ra: xét tiền tố
                # dài trước để không cắt hụt. Phát nhầm mẩu này ra thì thẻ đóng vĩnh viễn không
                # khớp được, cả câu trả lời chui vào phần suy luận.
                for i in range(len(end_tag) - 1, 0, -1):
                    if content.endswith(end_tag[:i]):
                        pending_end_tag = end_tag[:i]
                        content = content[:-i]
                        break
                # Phần chắc chắn không thuộc thẻ thì phát ngay như một mẩu suy luận.
                current_thinking += content
                reasoning_content_chunk += content
                content = ''

        else:
            # 不在思考块中或标签解析未启用，正常输出
            output_content += content

        full_content_log += output_content
        full_reasoning_log += reasoning_content_chunk
        yield {
            'content': output_content,
            'reasoning_content': reasoning_content_chunk
        }
        get_token_usage(chunk, token_usage)

    # Stream đứt giữa lúc đang giữ một mẩu nghi là đầu thẻ: hoá ra nó không phải thẻ, phải trả lại
    # đúng chỗ nó thuộc về chứ không được nuốt mất.
    if pending_end_tag:
        full_reasoning_log += pending_end_tag
        yield {'content': '', 'reasoning_content': pending_end_tag}
    if pending_start_tag:
        full_content_log += pending_start_tag
        yield {'content': pending_start_tag, 'reasoning_content': ''}

    # 流式结束后，统一输出两条日志：思考内容与正式内容
    SQLBotLogUtil.info(f"reasoning_content: {full_reasoning_log}")
    SQLBotLogUtil.info(f"content: {full_content_log}")


def get_lang_name(lang: str):
    if not lang:
        return 'Tiếng Trung giản thể'
    normalized = lang.lower()
    if normalized.startswith('vi'):
        return 'Tiếng Việt'
    if normalized.startswith('zh-tw'):
        return 'Tiếng Trung phồn thể'
    if normalized.startswith('en'):
        return 'Tiếng Anh'
    if normalized.startswith('ko'):
        return 'Tiếng Hàn'
    return 'Tiếng Trung giản thể'


def trim_history_by_size(messages: List[dict], max_chars: int) -> List[dict]:
    """Cắt bớt lịch sử từ CŨ tới MỚI cho tới khi tổng số ký tự nằm dưới `max_chars`.

    Đếm theo lượt (`rounds`) là không đủ để chặn vỡ context: một lượt hỏi có thể to bất thường (LLM
    lặp phần suy luận, hoặc câu hỏi kèm dữ liệu dài), mà m-schema trong system prompt đã ăn sẵn
    khoảng 24K ký tự. Đây là lớp chặn cuối, chạy sau `get_last_conversation_rounds`.

    Bỏ từ đầu danh sách vì message mới luôn quan trọng hơn message cũ. Luôn giữ lại ít nhất phần tử
    cuối: mất nốt nó thì lượt hiện tại chẳng còn ngữ cảnh nào, mà một message đơn lẻ vượt trần đã
    được `_cap_history_answer` chặn từ lúc ghi vào lịch sử.
    """
    if not messages or max_chars <= 0:
        return messages
    total = sum(len(m.get('content') or '') for m in messages)
    start = 0
    while total > max_chars and start < len(messages) - 1:
        total -= len(messages[start].get('content') or '')
        start += 1
    if start > 0:
        SQLBotLogUtil.warning(
            f'History trimmed: dropped {start} oldest message(s), {total} chars left (cap {max_chars})')
    return messages[start:]


def get_last_conversation_rounds(messages, rounds=settings.GENERATE_SQL_QUERY_HISTORY_ROUND_COUNT):
    """获取最后N轮对话，处理不完整对话的情况"""
    if not messages or rounds <= 0:
        return []

    # 找到所有用户消息的位置
    human_indices = []
    for index, msg in enumerate(messages):
        if msg.get('type') == 'human':
            human_indices.append(index)

    # 如果没有用户消息，返回空
    if not human_indices:
        return []

    # 计算从哪个索引开始
    if len(human_indices) <= rounds:
        # 如果用户消息数少于等于需要的轮数，从第一个用户消息开始
        start_index = human_indices[0]
    else:
        # 否则，从倒数第N个用户消息开始
        start_index = human_indices[-rounds]

    return messages[start_index:]
