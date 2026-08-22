from datetime import datetime
from enum import Enum
from typing import List, Optional, Any, Union

from fastapi import Body
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage
from pydantic import BaseModel
from sqlalchemy import Column, Integer, String, Text, BigInteger, DateTime, Identity, Boolean
from sqlalchemy import Enum as SQLAlchemyEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import SQLModel, Field

from apps.chat.utils.attachment import AttachmentContent
from apps.db.constant import DB
from apps.template.filter.generator import get_permissions_template
from apps.template.generate_analysis.generator import get_analysis_template
from apps.template.generate_answer.generator import get_answer_template
from apps.template.generate_chart.generator import get_chart_template
from apps.template.generate_dynamic.generator import get_dynamic_template
from apps.template.generate_guess_question.generator import get_guess_question_template
from apps.template.generate_predict.generator import get_predict_template
from apps.template.generate_sql.generator import get_sql_template, get_sql_example_template
from apps.template.select_datasource.generator import get_datasource_template


def enum_values(enum_class: type[Enum]) -> list:
    """Get values for enum."""
    return [status.value for status in enum_class]


class TypeEnum(Enum):
    CHAT = "0"


#     TODO other usage

class OperationEnum(Enum):
    GENERATE_SQL = '0'
    GENERATE_CHART = '1'
    ANALYSIS = '2'
    PREDICT_DATA = '3'
    GENERATE_RECOMMENDED_QUESTIONS = '4'
    GENERATE_SQL_WITH_PERMISSIONS = '5'
    CHOOSE_DATASOURCE = '6'
    GENERATE_DYNAMIC_SQL = '7'
    CHOOSE_TABLE = '8'
    FILTER_TERMS = '9'
    FILTER_SQL_EXAMPLE = '10'
    FILTER_CUSTOM_PROMPT = '11'
    EXECUTE_SQL = '12'
    GENERATE_PICTURE = '13'
    ANSWER = '14'


class ChatFinishStep(Enum):
    """Điểm dừng của pipeline. Giá trị số CHÍNH LÀ thứ tự pha, các chốt trong run_task so sánh
    `finish_step.value <= X` nên chèn pha mới bắt buộc phải đánh số lại cho đúng thứ tự chạy.

    GENERATE_ANSWER nằm giữa QUERY_DATA và GENERATE_CHART vì answer chạy ngay khi có dữ liệu
    (để câu trả lời chữ về sớm), không phải chờ sinh xong biểu đồ.

    Giá trị chỉ tồn tại trong runtime — không lưu DB, không nhận từ request — nên đánh số lại
    an toàn, miễn là sửa hết các chỗ đang truyền tường minh (hiện chỉ có apps/mcp/mcp.py).
    """
    GENERATE_SQL = 1
    QUERY_DATA = 2
    GENERATE_ANSWER = 3
    GENERATE_CHART = 4


class QuickCommand(Enum):
    REGENERATE = '/regenerate'
    ANALYSIS = '/analysis'
    PREDICT_DATA = '/predict'


#     TODO choose table / check connection / generate description

class ChatLog(SQLModel, table=True):
    __tablename__ = "chat_log"
    id: Optional[int] = Field(sa_column=Column(BigInteger, Identity(always=True), primary_key=True))
    type: TypeEnum = Field(
        sa_column=Column(SQLAlchemyEnum(TypeEnum, native_enum=False, values_callable=enum_values, length=3)))
    operate: OperationEnum = Field(
        sa_column=Column(SQLAlchemyEnum(OperationEnum, native_enum=False, values_callable=enum_values, length=3)))
    pid: Optional[int] = Field(sa_column=Column(BigInteger, nullable=True))
    ai_modal_id: Optional[int] = Field(sa_column=Column(BigInteger))
    base_modal: Optional[str] = Field(max_length=255)
    messages: Optional[list[dict]] = Field(sa_column=Column(JSONB))
    reasoning_content: Optional[str | None] = Field(sa_column=Column(Text, nullable=True))
    start_time: datetime = Field(sa_column=Column(DateTime(timezone=False), nullable=True))
    finish_time: datetime = Field(sa_column=Column(DateTime(timezone=False), nullable=True))
    token_usage: Optional[dict | None | int] = Field(sa_column=Column(JSONB))
    local_operation: bool = Field(default=False)
    error: bool = Field(default=False)


class Chat(SQLModel, table=True):
    __tablename__ = "chat"
    id: Optional[int] = Field(sa_column=Column(BigInteger, Identity(always=True), primary_key=True))
    # Khóa đối chiếu do hệ thống tích hợp bên ngoài tự sinh (UUID). Tách khỏi `id` vì `id` khai
    # Identity(always=True) — không thể để client tự đặt mà không phá sequence của giao diện web.
    # UNIQUE để hai lần hỏi cùng UUID chắc chắn rơi vào đúng một hội thoại; NULL với hội thoại tạo
    # từ UI web (Postgres cho phép nhiều NULL trong ràng buộc UNIQUE).
    external_id: Optional[str] = Field(default=None, max_length=64, nullable=True,
                                       unique=True, index=True)
    oid: Optional[int] = Field(sa_column=Column(BigInteger, nullable=True, default=1))
    create_time: datetime = Field(sa_column=Column(DateTime(timezone=False), nullable=True))
    create_by: int = Field(sa_column=Column(BigInteger, nullable=True))
    brief: str = Field(max_length=64, nullable=True)
    chat_type: str = Field(max_length=20, default="chat")  # chat, datasource
    datasource: int = Field(sa_column=Column(BigInteger, nullable=True))
    engine_type: str = Field(max_length=64)
    origin: Optional[int] = Field(
        sa_column=Column(Integer, nullable=False, default=0))  # 0: default, 1: mcp, 2: assistant
    brief_generate: bool = Field(default=False)
    recommended_question_answer: str = Field(sa_column=Column(Text, nullable=True))
    recommended_question: str = Field(sa_column=Column(Text, nullable=True))
    recommended_generate: bool = Field(default=False)


class ChatRecord(SQLModel, table=True):
    __tablename__ = "chat_record"
    id: Optional[int] = Field(sa_column=Column(BigInteger, Identity(always=True), primary_key=True))
    chat_id: int = Field(sa_column=Column(BigInteger, nullable=False))
    ai_modal_id: Optional[int] = Field(sa_column=Column(BigInteger))
    first_chat: bool = Field(sa_column=Column(Boolean, nullable=True, default=False))
    create_time: datetime = Field(sa_column=Column(DateTime(timezone=False), nullable=True))
    finish_time: datetime = Field(sa_column=Column(DateTime(timezone=False), nullable=True))
    create_by: int = Field(sa_column=Column(BigInteger, nullable=True))
    datasource: int = Field(sa_column=Column(BigInteger, nullable=True))
    engine_type: str = Field(max_length=64, nullable=True)
    question: str = Field(sa_column=Column(Text, nullable=True))
    sql_answer: str = Field(sa_column=Column(Text, nullable=True))
    sql: str = Field(sa_column=Column(Text, nullable=True))
    sql_exec_result: str = Field(sa_column=Column(Text, nullable=True))
    data: str = Field(sa_column=Column(Text, nullable=True))
    chart_answer: str = Field(sa_column=Column(Text, nullable=True))
    chart: str = Field(sa_column=Column(Text, nullable=True))
    answer: str = Field(sa_column=Column(Text, nullable=True))
    analysis: str = Field(sa_column=Column(Text, nullable=True))
    predict: str = Field(sa_column=Column(Text, nullable=True))
    predict_data: str = Field(sa_column=Column(Text, nullable=True))
    recommended_question_answer: str = Field(sa_column=Column(Text, nullable=True))
    recommended_question: str = Field(sa_column=Column(Text, nullable=True))
    datasource_select_answer: str = Field(sa_column=Column(Text, nullable=True))
    finish: bool = Field(sa_column=Column(Boolean, nullable=True, default=False))
    error: str = Field(sa_column=Column(Text, nullable=True))
    analysis_record_id: int = Field(sa_column=Column(BigInteger, nullable=True))
    predict_record_id: int = Field(sa_column=Column(BigInteger, nullable=True))
    regenerate_record_id: int = Field(sa_column=Column(BigInteger, nullable=True))
    # Mã lĩnh vực (domainCode) mà lượt hỏi này bị giới hạn theo — NULL nghĩa là không giới hạn
    # lĩnh vực. Phải lưu theo từng record để /regenerate tái lập đúng ràng buộc của lượt gốc.
    domain_code: str = Field(sa_column=Column(String(100), nullable=True))


class ChatAttachment(SQLModel, table=True):
    """Text đã trích từ các file .docx đính kèm một lượt hỏi (`fileUrls` của `POST /chat/question`).

    Lưu bản trích chứ không lưu URL: presigned URL hết hạn sau ít phút nên bản text này là bản gốc
    duy nhất cho các lượt hỏi sau. Tách khỏi `chat_record.question` để cột đó giữ sạch câu người
    dùng gõ (UI, /regenerate, sinh brief đều tiêu thụ nó).

    Tài liệu là MESSAGE, không phải hạ tầng: nó vào prompt theo cơ chế lịch sử hội thoại, chịu cắt
    trần ký tự và trôi theo cửa sổ. Bảng này chỉ giữ bản gốc cho các chính sách render tự quyết.
    """
    __tablename__ = "chat_attachment"
    id: Optional[int] = Field(sa_column=Column(BigInteger, Identity(always=True), primary_key=True))
    chat_id: int = Field(sa_column=Column(BigInteger, nullable=False, index=True))
    # Lượt hỏi ĐÍNH file. Không unique: một lượt đính được nhiều file, mỗi file một dòng. Thứ tự
    # client gửi được suy ra từ `id` tăng dần — cả lô ghi trong một transaction nên không có chỗ
    # cho dòng chen ngang.
    record_id: int = Field(sa_column=Column(BigInteger, nullable=False, index=True))
    filename: Optional[str] = Field(sa_column=Column(Text, nullable=True))
    content: str = Field(sa_column=Column(Text, nullable=False))
    # Bản trích đã bị cắt ngay từ lúc đọc file (trần ký tự chống zip bomb). Phải lưu để mọi lần
    # render sau còn nhắc được LLM là tài liệu không đầy đủ.
    truncated: bool = Field(sa_column=Column(Boolean, nullable=False, default=False))
    create_time: datetime = Field(sa_column=Column(DateTime(timezone=False), nullable=True))


class ChatRecordResult(BaseModel):
    id: Optional[int] = None
    chat_id: Optional[int] = None
    ai_modal_id: Optional[int] = None
    first_chat: bool = False
    create_time: Optional[datetime] = None
    finish_time: Optional[datetime] = None
    question: Optional[str] = None
    sql_answer: Optional[str] = None
    sql: Optional[str] = None
    datasource: Optional[int] = None
    data: Optional[str] = None
    chart_answer: Optional[str] = None
    chart: Optional[str] = None
    answer: Optional[str] = None
    analysis: Optional[str] = None
    predict: Optional[str] = None
    predict_data: Optional[str] = None
    recommended_question: Optional[str] = None
    datasource_select_answer: Optional[str] = None
    finish: Optional[bool] = None
    error: Optional[str] = None
    analysis_record_id: Optional[int] = None
    predict_record_id: Optional[int] = None
    regenerate_record_id: Optional[int] = None
    sql_reasoning_content: Optional[str] = None
    chart_reasoning_content: Optional[str] = None
    analysis_reasoning_content: Optional[str] = None
    predict_reasoning_content: Optional[str] = None
    duration: Optional[float] = None  # 耗时字段（单位：秒）
    total_tokens: Optional[int] = None  # token总消耗


class CreateChat(BaseModel):
    id: int = None
    question: str = None
    datasource: int = None
    origin: Optional[int] = 0  # 0是页面上，mcp是1，小助手是2


class RenameChat(BaseModel):
    id: int = None
    brief: str = ''
    brief_generate: bool = True

class SimpleChat(BaseModel):
    id: int = None
    brief: str = ''

class ChatItem(BaseModel):
    id: Optional[int] = None
    oid: Optional[int] = None
    create_time: Optional[datetime] = None
    create_by: Optional[int] = None
    brief: Optional[str] = None
    chat_type: Optional[str] = "chat"
    datasource: Optional[int] = None
    engine_type: Optional[str] = None
    origin: Optional[int] = 0
    brief_generate: Optional[bool] = False
    recommended_question_answer: Optional[str] = None
    recommended_question: Optional[str] = None
    recommended_generate: Optional[bool] = False
    latest_record_time: Optional[datetime] = None

class ChatInfo(BaseModel):
    id: Optional[int] = None
    create_time: datetime = None
    create_by: int = None
    brief: str = ''
    chat_type: str = "chat"
    datasource: Optional[int] = None
    engine_type: str = ''
    ds_type: str = ''
    datasource_name: str = ''
    datasource_exists: bool = True
    recommended_question: Optional[str] = None
    recommended_generate: Optional[bool] = False
    latest_record_time: Optional[datetime] = None
    records: List[ChatRecord | dict] = []


class ChatLogHistoryItem(BaseModel):
    start_time: Optional[datetime] = None
    finish_time: Optional[datetime] = None
    duration: Optional[float] = None  # 耗时字段（单位：秒）
    total_tokens: Optional[int] = None  # token总消耗
    operate: Optional[str] = None
    local_operation: Optional[bool] = False
    message: Optional[str | dict | list] = None
    error: Optional[bool] = False


class ChatLogHistory(BaseModel):
    start_time: Optional[datetime] = None
    finish_time: Optional[datetime] = None
    duration: Optional[float] = None  # 耗时字段（单位：秒）
    total_tokens: Optional[int] = None  # token总消耗
    steps: List[ChatLogHistoryItem | dict] = []


class AiModelQuestion(BaseModel):
    question: str = None
    ai_modal_id: int = None
    ai_modal_name: str = None  # Specific model name
    engine: str = ""
    db_schema: str = ""
    sql: str = ""
    rule: str = ""
    fields: str = ""
    data: str = ""
    # Mô tả phạm vi của `data` cho pha answer: tổng số dòng thật và việc `data` có bị cắt hay không.
    # Bắt buộc phải có vì `data` chỉ là 100 dòng đầu; thiếu nó thì LLM tự đếm dòng và báo sai tổng.
    data_scope: str = ""
    # Lịch sử hỏi-đáp cho nhánh answer BÌNH THƯỜNG. Tách khỏi `fallback_history` dù nội dung sinh ra
    # từ cùng một hàm: hai nhánh dùng hai template với bộ rule khác nhau, dùng chung một field thì
    # sửa rule bên này sẽ vỡ bên kia mà không có gì báo.
    answer_history: str = ""
    lang: str = "简体中文"
    filter: str = []
    sub_query: Optional[list[dict]] = None
    terminologies: str = ""
    data_training: str = ""
    custom_prompt: str = ""
    error_msg: str = ""
    regenerate_record_id: Optional[int] = None
    sample_data: str = ""
    sqlbot_name: str = "SQLBot"
    # Khối <attached-document> ĐÃ RENDER của tài liệu docx đính kèm lượt này; rỗng khi không đính.
    # Chỉ được ghép vào câu hỏi ở tầng render prompt (question_for_prompt) — cột question trong DB
    # và mọi consumer khác của `question` (UI, /regenerate, brief, RAG) luôn thấy câu hỏi sạch.
    attached_doc: str = ""
    # Dùng cho nhánh answer fallback (pha SQL hỏng hẳn): lý do hỏng và tóm tắt các lượt hỏi-đáp cũ.
    fallback_reason: str = ""
    fallback_history: str = ""
    # Danh sách bảng được phép truy vấn. Có mặt để trả lời được câu hỏi về chính datasource ("có bao
    # nhiêu bảng") — loại câu hỏi mà pha SQL gần như luôn hỏng vì không bảng nghiệp vụ nào chứa
    # thông tin đó, còn schema thì hệ thống nắm sẵn.
    fallback_schema: str = ""
    # Chọn biến thể chữ trong `answer.fallback_variants`: "failed" khi pha SQL hỏng thật,
    # "no_query" khi hệ thống chủ động bỏ qua truy vấn. Sai giá trị này thì câu trả lời sẽ báo
    # sự cố cho một lượt hoàn toàn bình thường, nên mặc định giữ ở nhánh cũ.
    fallback_kind: str = "failed"

    def question_for_prompt(self) -> str:
        """Câu hỏi dùng để RENDER PROMPT: có tài liệu đính kèm thì tài liệu đứng trước câu hỏi.

        Tách thành method để ba chỗ render câu hỏi hiện hành (pha SQL, pha answer, nhánh fallback)
        cùng một hành vi. Tài liệu đứng TRƯỚC vì câu hỏi thường tham chiếu vào nó ("theo tài liệu
        trên..."); LLM đọc theo thứ tự thì phải gặp tài liệu trước.

        Khối ghép này đi vào chat_log như phần nội dung message bình thường (không mang cờ
        sqlbot_system), nên các lượt sau pha SQL tự thấy lại tài liệu qua cơ chế replay lịch sử —
        và cũng tự chịu cắt trần, trôi cửa sổ như mọi message. Đó là hành vi cố ý.
        """
        if self.attached_doc:
            return f'{self.attached_doc}\n{self.question}'
        return self.question

    def sql_sys_question(self, db_type: Union[str, DB], enable_query_limit: bool = True):
        templates: dict[str, str] = {}
        _sql_template = get_sql_example_template(db_type)
        _base_template = get_sql_template()
        _process_check = _sql_template.get('process_check') if _sql_template.get('process_check') else _base_template[
            'process_check']
        _query_limit = _base_template['query_limit'] if enable_query_limit else _base_template['no_query_limit']
        _other_rule = _sql_template['other_rule'].format(multi_table_condition=_base_template['multi_table_condition'])
        _base_sql_rules = _sql_template['quot_rule'] + _query_limit + _sql_template['limit_rule'] + _other_rule
        _sql_examples = _sql_template['basic_example']
        _example_engine = _sql_template['example_engine']
        _example_answer_1 = _sql_template['example_answer_1_with_limit'] if enable_query_limit else _sql_template[
            'example_answer_1']
        _example_answer_2 = _sql_template['example_answer_2_with_limit'] if enable_query_limit else _sql_template[
            'example_answer_2']
        _example_answer_3 = _sql_template['example_answer_3_with_limit'] if enable_query_limit else _sql_template[
            'example_answer_3']

        templates['system'] = _base_template['system'].format(lang=self.lang, process_check=_process_check,
                                                              sqlbot_name=self.sqlbot_name)
        templates['rules'] = _base_template['generate_rules'].format(lang=self.lang,
                                                                     sqlbot_name=self.sqlbot_name,
                                                                     base_sql_rules=_base_sql_rules,
                                                                     basic_sql_examples=_sql_examples,
                                                                     example_engine=_example_engine,
                                                                     example_answer_1=_example_answer_1,
                                                                     example_answer_2=_example_answer_2,
                                                                     example_answer_3=_example_answer_3)
        templates['schema'] = _base_template['generate_basic_info'].format(engine=self.engine, schema=self.db_schema,
                                                                           sample_data=self.sample_data)

        if self.terminologies:
            templates['terminologies'] = _base_template['generate_terminologies_info'].format(
                terminologies=self.terminologies)

        if self.data_training:
            templates['data_training'] = _base_template['generate_data_training_info'].format(
                data_training=self.data_training)

        if self.custom_prompt:
            templates['custom_prompt'] = _base_template['generate_custom_prompt_info'].format(
                custom_prompt=self.custom_prompt)

        return templates

    def sql_user_question(self, current_time: str, change_title: bool):
        """Dựng user prompt của pha sinh SQL. Câu hỏi lấy qua ``question_for_prompt`` để lượt có
        tài liệu đính kèm mang theo khối <attached-document> ngay trong <user-question>."""
        _question = self.question_for_prompt()
        if self.regenerate_record_id:
            _question = get_sql_template()['regenerate_hint'] + _question
        return get_sql_template()['user'].format(lang=self.lang, engine=self.engine, schema=self.db_schema,
                                                 question=_question,
                                                 rule=self.rule, current_time=current_time, error_msg=self.error_msg,
                                                 change_title=change_title)

    def sql_retry_question(self, reason: str):
        """Dựng user prompt cho lần sinh SQL lại trong cùng một lượt hỏi.

        Cố ý KHÔNG lặp lại schema/rule: message này được append vào cuối `sql_message` đang có, nên
        toàn bộ ngữ cảnh (system prompt, m-schema, câu hỏi gốc, câu trả lời hỏng của LLM) vẫn nằm
        ngay phía trên. Nhắc lại chỉ tốn token và làm loãng phần quan trọng nhất là <failure>.
        """
        return get_sql_template()['retry_hint'].format(reason=reason)

    def chart_sys_question(self):
        templates: dict[str, str] = {
            'system': get_chart_template()['system'].format(lang=self.lang, sqlbot_name=self.sqlbot_name),
            'rules': get_chart_template()['generate_rules'].format(lang=self.lang)
        }
        return templates

    def chart_user_question(self, chart_type: Optional[str] = '', schema: Optional[str] = ''):
        return get_chart_template()['user'].format(lang=self.lang, sql=self.sql, question=self.question, rule=self.rule,
                                                   chart_type=chart_type, schema=schema)

    def analysis_sys_question(self):
        return get_analysis_template()['system'].format(lang=self.lang, terminologies=self.terminologies,
                                                        custom_prompt=self.custom_prompt, sqlbot_name=self.sqlbot_name)

    def analysis_user_question(self):
        return get_analysis_template()['user'].format(fields=self.fields, data=self.data)

    def answer_sys_question(self):
        """Dựng system prompt cho pha sinh câu trả lời chữ cuối cùng.

        Dùng chung `terminologies`/`custom_prompt` với analysis để câu trả lời hiểu đúng thuật ngữ
        nghiệp vụ đã khai báo trong workspace.
        """
        return get_answer_template()['system'].format(lang=self.lang, terminologies=self.terminologies,
                                                      custom_prompt=self.custom_prompt,
                                                      sqlbot_name=self.sqlbot_name)

    def answer_user_question(self):
        """Dựng user prompt cho pha answer: lịch sử + câu hỏi gốc + SQL đã chạy + fields + data + data_scope.

        Khác analysis ở chỗ có `question` và `sql`: thiếu câu hỏi thì LLM chỉ mô tả chung chung
        bảng số chứ không trả lời đúng thứ được hỏi.

        `data_scope` đặt SAU `<data>` trong template, cố ý: nó là lời cảnh báo về khối vừa đọc, và
        đứng cuối prompt thì có trọng số cao nhất khi LLM quyết định lấy con số tổng từ đâu. Vì vậy
        `answer_history` phải đứng ĐẦU prompt chứ không phải cuối: lịch sử mang theo số liệu cũ, đặt
        nó sau `<data-scope>` là cướp mất đúng cái chốt chống bịa số đó.

        `answer_history` đã bao gồm sẵn cặp thẻ `<history>` (hoặc rỗng hẳn) — xem build_answer_prompts.
        """
        return get_answer_template()['user'].format(history=self.answer_history,
                                                    question=self.question_for_prompt(),
                                                    sql=self.sql,
                                                    fields=self.fields, data=self.data,
                                                    data_scope=self.data_scope)

    def answer_fallback_sys_question(self):
        """System prompt cho pha answer khi lượt này KHÔNG có dữ liệu truy vấn mới.

        Tách hẳn khỏi `answer_sys_question` thay vì nhét thêm rule vào đó: prompt chính bắt buộc
        "chỉ dùng số trong <data>", mà nhánh này không có <data> nào — dùng chung sẽ đẩy LLM vào
        thế phải trả lời "không tìm thấy dữ liệu" kể cả khi lịch sử hội thoại thừa sức trả lời.

        `fallback_kind` chọn hai mảnh chữ khác nhau giữa các trường hợp không-có-dữ-liệu (xem
        `answer.fallback_variants`). Giá trị lạ thì rơi về "failed": thà thừa một lời xin lỗi còn
        hơn ném KeyError giữa lúc đang dựng prompt cho chính nhánh cứu hộ.
        """
        template = get_answer_template()
        variants = template['fallback_variants']
        variant = variants.get(self.fallback_kind) or variants['failed']
        return template['fallback_system'].format(lang=self.lang,
                                                  terminologies=self.terminologies,
                                                  custom_prompt=self.custom_prompt,
                                                  sqlbot_name=self.sqlbot_name,
                                                  situation=variant['situation'],
                                                  insufficient_rule=variant['insufficient_rule'])

    def answer_fallback_user_question(self):
        """User prompt nhánh fallback: câu hỏi + lý do không có dữ liệu + bảng + các lượt cũ."""
        return get_answer_template()['fallback_user'].format(question=self.question_for_prompt(),
                                                             reason=self.fallback_reason,
                                                             schema=self.fallback_schema,
                                                             history=self.fallback_history)

    def predict_sys_question(self):
        return get_predict_template()['system'].format(lang=self.lang, custom_prompt=self.custom_prompt,
                                                       sqlbot_name=self.sqlbot_name)

    def predict_user_question(self):
        return get_predict_template()['user'].format(fields=self.fields, data=self.data)

    def datasource_sys_question(self):
        return get_datasource_template()['system'].format(lang=self.lang, sqlbot_name=self.sqlbot_name)

    def datasource_user_question(self, datasource_list: str = "[]"):
        return get_datasource_template()['user'].format(lang=self.lang, question=self.question, data=datasource_list)

    def guess_sys_question(self, articles_number: int = 4):
        return get_guess_question_template()['system'].format(lang=self.lang, articles_number=articles_number,
                                                              sqlbot_name=self.sqlbot_name)

    def guess_user_question(self, old_questions: str = "[]"):
        return get_guess_question_template()['user'].format(question=self.question, schema=self.db_schema,
                                                            old_questions=old_questions)

    def filter_sys_question(self):
        return get_permissions_template()['system'].format(lang=self.lang, engine=self.engine,
                                                           sqlbot_name=self.sqlbot_name)

    def filter_user_question(self):
        return get_permissions_template()['user'].format(sql=self.sql, filter=self.filter)

    def dynamic_sys_question(self):
        return get_dynamic_template()['system'].format(lang=self.lang, engine=self.engine, sqlbot_name=self.sqlbot_name)

    def dynamic_user_question(self):
        return get_dynamic_template()['user'].format(sql=self.sql, sub_query=self.sub_query)


class ChatQuestion(AiModelQuestion):
    chat_id: int
    datasource_id: Optional[int] = None
    # Bản GỐC của các tài liệu docx đính kèm lượt này (đã trích thành markdown, chưa cắt theo ngân
    # sách prompt), theo đúng thứ tự client gửi — dùng để ghi bảng chat_attachment sau khi record
    # của lượt được tạo. Bản đưa vào prompt là `attached_doc` (kế thừa từ AiModelQuestion), đã
    # render và đã chia ngân sách chung của cả lượt.
    attachments: list[AttachmentContent] = []
    # Mã lĩnh vực của lượt hỏi (từ `domainCode` của request, hoặc kế thừa từ record gốc khi
    # /regenerate). Được lưu vào chat_record.domain_code và đưa vào chuỗi sàng quyền SW.
    domain_code: Optional[str] = None


class ChatMcp(ChatQuestion):
    token: str


class McpDs(BaseModel):
    token: str = Body(description='用户token')
    oid: Optional[str] = Body(description='组织ID，如果不传则为最后一次登录SQLBot时所使用的组织ID', default=None)


class ChatToken(BaseModel):
    username: str = Body(description='用户名')
    password: str = Body(description='密码')


class ChatStart(BaseModel):
    username: str = Body(description='用户名', default=None)
    password: str = Body(description='密码', default=None)
    token: str = Body(description='token', default=None)
    oid: Optional[str] = Body(
        description='组织ID，仅当数据源ID为空时有效，如果不传则为最后一次登录SQLBot时所使用的组织ID', default=None)


class ChatQuestionBase(BaseModel):
    question: str = Body(description='用户提问')
    # Hai kiểu cùng tồn tại: UI web gửi id nội bộ (int), hệ thống tích hợp bên ngoài gửi UUID (str).
    # Cả hai được quy về id nội bộ ở `resolve_chat_id` trước khi vào pipeline.
    chat_id: Union[int, str] = Body(description='会话ID：内部数字ID，或外部系统自生成的UUID')
    # Hệ thống tích hợp bên ngoài tự sinh chat_id và không gọi /chat/start, nên phải kèm datasource
    # để server tự tạo hội thoại ở lần hỏi đầu. Hội thoại đã tồn tại thì trường này bị bỏ qua —
    # datasource gắn cứng lúc tạo, đổi giữa chừng sẽ làm hỏng ngữ cảnh multi-turn.
    datasource: Optional[int] = Body(description='数据源ID，仅当 chat_id 尚不存在、需要自动创建会话时必填',
                                     default=None)
    # Danh sách presigned URL trỏ tới các file .docx đính kèm lượt hỏi này. Nội dung từng file
    # được trích text và trở thành một phần ngữ cảnh của câu hỏi (cả pha sinh SQL lẫn pha answer),
    # xếp theo đúng thứ tự trong mảng này. Host phải nằm trong FILE_DOWNLOAD_ALLOWED_HOSTS; URL
    # chỉ cần sống qua một lần tải ngay trong request. Tối đa CHAT_DOC_MAX_FILES phần tử; URL
    # trùng nhau chỉ được tải và đưa vào prompt một lần.
    fileUrls: Optional[list[str]] = Body(
        description='Danh sách presigned URL của các file .docx đính kèm câu hỏi',
        default=None)
    # Mã lĩnh vực (linhVucMa phía SW) giới hạn phạm vi câu hỏi của lượt này. Chỉ có tác dụng với
    # user được SW cấp quyền (có dòng trong ai_user_permissions): bảng ngoài lĩnh vực sẽ vô hình
    # với cả LLM lẫn tầng thực thi. Ngữ nghĩa ba trạng thái: KHÔNG gửi trường này = không lọc lĩnh
    # vực, hỏi trên toàn bộ bảng được cấp; gửi null/rỗng = HTTP 400 (chắc chắn là bug client);
    # gửi mã không có trong quyền của user = event SSE error kèm danh sách lĩnh vực hợp lệ.
    # User ngoài hệ SW thì trường này bị bỏ qua.
    domainCode: Optional[str] = Body(description='Mã lĩnh vực giới hạn phạm vi câu hỏi (chỉ áp dụng '
                                                 'với user được cấp quyền qua AI Sync Hook); muốn '
                                                 'hỏi trên toàn bộ lĩnh vực thì bỏ hẳn trường này, '
                                                 'gửi null/rỗng sẽ bị trả 400',
                                     default=None)


class McpQuestion(ChatQuestionBase):
    # Giữ nguyên hợp đồng cũ của MCP: client MCP luôn lấy chat_id từ /mcp/access_token nên chỉ có
    # id nội bộ, không cần (và không nên) nới lỏng sang UUID.
    chat_id: int = Body(description='会话ID')
    token: str = Body(description='token')
    stream: Optional[bool] = Body(description='是否流式输出，默认为true开启, 关闭false则返回JSON对象', default=True)
    lang: Optional[str] = Body(description='语言：zh-CN|zh-TW|en|ko-KR', default='zh-CN')
    datasource_id: Optional[int | str] = Body(description='数据源ID，仅当当前对话没有确定数据源时有效', default=None)
    return_img: Optional[bool] = Body(description='是否返回图表，默认为true开启, 关闭false则仅返回数据', default=True)


class AxisObj(BaseModel):
    name: str = ''
    value: str = ''
    type: str | None = None


class ExcelData(BaseModel):
    axis: list[AxisObj] = []
    data: list[dict] = []
    name: str = 'Excel'


class McpAssistant(BaseModel):
    question: str = Body(description='用户提问')
    url: str = Body(description='第三方数据接口')
    authorization: str = Body(description='第三方接口凭证')
    stream: Optional[bool] = Body(description='是否流式输出，默认为true开启, 关闭false则返回JSON对象', default=True)


class SystemPromptMessage(SystemMessage):
    sqlbot_system: bool = True

    def __init__(
            self, content: Union[str, list[Union[str, dict]]], **kwargs: Any
    ) -> None:
        super().__init__(content=content, **kwargs)


class HumanPromptMessage(HumanMessage):
    sqlbot_system: bool = True

    def __init__(
            self, content: Union[str, list[Union[str, dict]]], **kwargs: Any
    ) -> None:
        super().__init__(content=content, **kwargs)


class AIPromptMessage(AIMessage):
    sqlbot_system: bool = True

    def __init__(
            self, content: Union[str, list[Union[str, dict]]], **kwargs: Any
    ) -> None:
        super().__init__(content=content, **kwargs)
