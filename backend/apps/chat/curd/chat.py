import datetime
from decimal import Decimal
from typing import List, Optional, Union, Dict, Any

import orjson
import sqlparse
from sqlalchemy import and_, select, update
from sqlalchemy import desc, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

from apps.chat.curd.attachment import get_attachments_by_record_ids
from apps.chat.models.chat_model import Chat, ChatRecord, CreateChat, ChatInfo, RenameChat, ChatQuestion, ChatLog, \
    TypeEnum, OperationEnum, ChatRecordResult, ChatLogHistory, ChatLogHistoryItem, ChatItem
from apps.chat.utils.attachment import render_attachment_blocks
from common.core.config import settings
from apps.datasource.crud.datasource import get_ds, is_ds_usable
from apps.datasource.crud.recommended_problem import get_datasource_recommended_chart
from apps.datasource.models.datasource import CoreDatasource
from apps.db.constant import DB
from apps.db.db import exec_sql
from apps.system.crud.assistant import AssistantOutDs, AssistantOutDsFactory
from apps.system.schemas.system_schema import AssistantOutDsSchema
from common.core.deps import CurrentAssistant, SessionDep, CurrentUser, Trans
from common.utils.data_format import DataFormat
from common.utils.utils import extract_nested_json, SQLBotLogUtil


def get_chat_record_by_id(session: SessionDep, record_id: int):
    record: ChatRecord | None = None

    stmt = select(ChatRecord.id, ChatRecord.question, ChatRecord.chat_id, ChatRecord.datasource, ChatRecord.engine_type,
                  ChatRecord.ai_modal_id, ChatRecord.create_by).where(
        and_(ChatRecord.id == record_id))
    result = session.execute(stmt)
    for r in result:
        record = ChatRecord(id=r.id, question=r.question, chat_id=r.chat_id, datasource=r.datasource,
                            engine_type=r.engine_type, ai_modal_id=r.ai_modal_id, create_by=r.create_by)
    return record


def get_chat(session: SessionDep, chat_id: int) -> Chat:
    statement = select(Chat).where(Chat.id == chat_id)
    chat = session.exec(statement).scalars().first()
    return chat


def list_chats(session: SessionDep, current_user: CurrentUser) -> List[ChatItem]:
    oid = current_user.oid if current_user.oid is not None else 1
    # 子查询：获取每个chat对应的最新chat_record的create_time
    latest_record_subq = (
        session.query(
            ChatRecord.chat_id,
            func.max(ChatRecord.create_time).label('latest_record_time')
        )
        .group_by(ChatRecord.chat_id)
        .subquery()
    )
    # 按最新chat_record的create_time排序，如果没有chat_record则使用chat自身的create_time
    sort_time = func.coalesce(latest_record_subq.c.latest_record_time, Chat.create_time)
    results = (
        session.query(Chat, sort_time.label('latest_record_time'))
        .outerjoin(latest_record_subq, Chat.id == latest_record_subq.c.chat_id)
        .filter(and_(Chat.create_by == current_user.id, Chat.oid == oid))
        .order_by(sort_time.desc())
        .all()
    )
    chart_list = []
    for chat, latest_record_time in results:
        item = ChatItem(**chat.model_dump())
        item.latest_record_time = latest_record_time
        chart_list.append(item)
    return chart_list


def list_recent_questions(session: SessionDep, current_user: CurrentUser, datasource_id: int) -> List[str]:
    chat_records = (
        session.query(
            ChatRecord.question
        )
        .join(Chat, ChatRecord.chat_id == Chat.id)  # 关联Chat表
        .filter(
            Chat.datasource == datasource_id,  # 使用Chat表的datasource字段
            ChatRecord.question.isnot(None),
            ChatRecord.create_by == current_user.id
        )
        .group_by(ChatRecord.question)
        .order_by(desc(func.max(ChatRecord.create_time)))
        .limit(10)
        .all()
    )
    return [record[0] for record in chat_records] if chat_records else []


CHAT_BRIEF_MAX_CHARS = 64


def trim_chat_brief(text: Optional[str]) -> str:
    """Cắt tiêu đề hội thoại cho vừa cột ``chat.brief``, ưu tiên không đứt giữa một từ.

    Trần lấy đúng bằng ``max_length`` của cột (64). Trước đây mọi chỗ đặt tiêu đề đều cắt cứng ở 20
    ký tự dù cột chứa được 64, nên tiêu đề do LLM đặt thường xuyên mất chữ cuối — "Số lượng đại biểu
    họ X" về tới client thành "Số lượng đại biểu họ".

    Chỉ cắt khi thật sự vượt trần. Khi phải cắt thì lùi về khoảng trắng gần cuối nhất; khoảng trắng
    đó nằm quá sớm (chưa tới nửa trần) thì cắt cứng, vì lùi thêm chỉ làm mất nhiều chữ hơn là được.
    """
    brief = (text or '').strip()
    if len(brief) <= CHAT_BRIEF_MAX_CHARS:
        return brief
    cut = brief[:CHAT_BRIEF_MAX_CHARS]
    last_space = cut.rfind(' ')
    if last_space >= CHAT_BRIEF_MAX_CHARS // 2:
        cut = cut[:last_space]
    return cut.rstrip()


def rename_chat_with_user(session: SessionDep, current_user: CurrentUser, rename_object: RenameChat) -> str:
    chat = session.get(Chat, rename_object.id)
    if not chat:
        raise Exception(f"Chat with id {rename_object.id} not found")
    if chat.create_by != current_user.id:
        raise Exception(f"Chat with id {rename_object.id} not Owned by the current user")
    chat.brief = trim_chat_brief(rename_object.brief)
    chat.brief_generate = rename_object.brief_generate
    session.add(chat)
    session.flush()
    session.refresh(chat)

    brief = chat.brief
    session.commit()
    return brief


def rename_chat(session: SessionDep, rename_object: RenameChat) -> str:
    chat = session.get(Chat, rename_object.id)
    if not chat:
        raise Exception(f"Chat with id {rename_object.id} not found")

    chat.brief = trim_chat_brief(rename_object.brief)
    chat.brief_generate = rename_object.brief_generate
    session.add(chat)
    session.flush()
    session.refresh(chat)

    brief = chat.brief
    session.commit()
    return brief


def delete_chat(session, chart_id) -> str:
    chat = session.query(Chat).filter(Chat.id == chart_id).first()
    if not chat:
        return f'Chat with id {chart_id} has been deleted'

    session.delete(chat)
    session.commit()

    return f'Chat with id {chart_id} has been deleted'


def delete_chat_with_user(session, current_user: CurrentUser, chart_id) -> str:
    chat = session.query(Chat).filter(Chat.id == chart_id).first()
    if not chat:
        return f'Chat with id {chart_id} has been deleted'
    if chat.create_by != current_user.id:
        raise Exception(f"Chat with id {chart_id} not Owned by the current user")
    session.delete(chat)
    session.commit()

    return f'Chat with id {chart_id} has been deleted'


def get_chart_config(session: SessionDep, chart_record_id: int):
    stmt = select(ChatRecord.chart).where(and_(ChatRecord.id == chart_record_id))
    res = session.execute(stmt)
    for row in res:
        try:
            return orjson.loads(row.chart)
        except Exception:
            pass
    return {}


def _format_column(column: dict) -> str:
    """格式化单个column字段"""
    value = column.get('value', '')
    name = column.get('name', '')
    if value != name and name:
        return f"{value}({name})"
    return value


def format_chart_fields(chart_info: dict) -> list:
    fields = []

    # 处理 columns
    for column in chart_info.get('columns') or []:
        fields.append(_format_column(column))

    # 处理 axis
    if axis := chart_info.get('axis'):
        # 处理 x 轴
        if x_axis := axis.get('x'):
            fields.append(_format_column(x_axis))

        # 处理 y 轴
        if y_axis := axis.get('y'):
            if isinstance(y_axis, list):
                for column in y_axis:
                    fields.append(_format_column(column))
            else:
                fields.append(_format_column(y_axis))

        # 处理 series
        if series := axis.get('series'):
            fields.append(_format_column(series))

    return [field for field in fields if field]  # 过滤空字符串


def get_last_execute_sql_error(session: SessionDep, chart_id: int):
    stmt = select(ChatRecord.error).where(and_(ChatRecord.chat_id == chart_id)).order_by(
        ChatRecord.create_time.desc()).limit(1)
    res = session.execute(stmt).scalar()
    if res:
        try:
            obj = orjson.loads(res)
            if obj.get('type') and obj.get('type') == 'exec-sql-err':
                return obj.get('traceback')
        except Exception:
            pass

    return None


def get_recent_qa_history(session: SessionDep, chat_id: int, exclude_record_id: Optional[int] = None,
                          rounds: int = 3, max_rows: int = 20) -> str:
    """Tóm tắt các lượt hỏi-đáp gần nhất của một hội thoại thành chuỗi cho prompt fallback.

    Đọc thẳng từ chat_record thay vì dùng lại `sql_message` của pha sinh SQL: message ở đó là prompt
    đã render đầy đủ (kèm cả m-schema hàng chục KB) nên nhét vào prompt answer là phí token, mà phần
    cần lại là dữ liệu đã truy vấn được — thứ `sql_message` không hề có.

    Bỏ qua lượt hiện tại (`exclude_record_id`) và các lượt không có dữ liệu lẫn câu trả lời: chúng
    chỉ làm loãng ngữ cảnh. Trả về chuỗi rỗng khi không có gì đáng đưa vào, để caller tự quyết định.

    Lượt nào có tài liệu docx đính kèm (bảng chat_attachment) thì các khối <attached-document>
    được chèn lại vào round của lượt đó. CHAT_DOC_HISTORY_MAX_CHARS là trần cho CẢ LƯỢT, các file
    của lượt chia nhau — nếu không thì một lượt đính năm file sẽ tự nhân năm phần lịch sử của nó
    lên và đẩy các lượt khác ra khỏi context. Đây là điểm móc chính sách duy nhất cho tài liệu ở
    pha answer các lượt sau — các chính sách tương lai (tóm tắt, bỏ qua theo độ liên quan,
    graph-memory) sửa đúng chỗ này. Tài liệu là message: lượt đính file trôi khỏi cửa sổ `rounds`
    thì tài liệu biến mất theo, cố ý không có ngoại lệ.
    """
    stmt = select(ChatRecord.id, ChatRecord.question, ChatRecord.sql, ChatRecord.data,
                  ChatRecord.answer).where(and_(ChatRecord.chat_id == chat_id))
    if exclude_record_id:
        stmt = stmt.where(ChatRecord.id != exclude_record_id)
    stmt = stmt.order_by(ChatRecord.create_time.desc()).limit(rounds)

    rows = session.execute(stmt).all()
    attachments = get_attachments_by_record_ids(session, [row.id for row in rows])

    blocks: List[str] = []
    # Đảo ngược để lượt cũ nhất nằm trên: LLM đọc theo thứ tự thời gian mới hiểu được các câu hỏi
    # tham chiếu ngược kiểu "trong đó cái nào lớn nhất".
    for row in reversed(rows):
        _question, _sql, _data, _answer = row.question, row.sql, row.data, row.answer
        fields_text = ''
        rows_text = ''
        if _data:
            try:
                data_obj = orjson.loads(_data)
                fields_text = orjson.dumps(data_obj.get('fields') or []).decode()
                _rows = data_obj.get('data') or []
                rows_text = orjson.dumps(_rows[:max_rows]).decode()
                if len(_rows) > max_rows:
                    rows_text += f'\n(còn {len(_rows) - max_rows} dòng nữa không hiển thị)'
            except Exception:
                # Dữ liệu cũ hỏng định dạng thì bỏ qua phần data, các phần khác vẫn dùng được.
                fields_text = ''
                rows_text = ''
        if not rows_text and not _answer:
            continue
        parts = [f'  <user-question>\n{_question or ""}\n  </user-question>']
        _attachments = attachments.get(row.id)
        if _attachments:
            parts.append(render_attachment_blocks(
                _attachments, total_max_chars=settings.CHAT_DOC_HISTORY_MAX_CHARS, indent='  '))
        if _sql:
            parts.append(f'  <executed-sql>\n{_sql}\n  </executed-sql>')
        if fields_text:
            parts.append(f'  <fields>\n{fields_text}\n  </fields>')
        if rows_text:
            parts.append(f'  <data>\n{rows_text}\n  </data>')
        if _answer:
            parts.append(f'  <bot-answer>\n{_answer}\n  </bot-answer>')
        blocks.append('<round>\n' + '\n'.join(parts) + '\n</round>')

    return '\n'.join(blocks)


def format_json_data(origin_data: dict):
    result = {'fields': origin_data.get('fields') if origin_data.get('fields') else [],
              'fields_info': origin_data.get('fields_info') if origin_data.get('fields_info') else None}
    _list = origin_data.get('data') if origin_data.get('data') else []
    data = format_json_list_data(_list)
    result['data'] = data

    return result


def format_json_list_data(origin_data: list[dict]):
    data = []
    for _data in origin_data if origin_data else []:
        _row = {}
        for key, value in _data.items():
            if value is not None:
                # 检查是否为数字且需要特殊处理
                if isinstance(value, (int, float)):
                    # 整数且超过15位 → 转字符串并标记为文本列
                    if isinstance(value, int) and len(str(abs(value))) > 15:
                        value = str(value)
                    # 小数且超过15位有效数字 → 转字符串并标记为文本列
                    elif isinstance(value, float):
                        decimal_str = str(Decimal(str(value))).rstrip('0').rstrip('.')
                        if len(decimal_str) > 15:
                            value = str(value)
            _row[key] = value
        data.append(DataFormat.normalize_qualified_sql_column_keys(_row))

    return data


def get_chat_chart_config(session: SessionDep, chat_record_id: int):
    stmt = select(ChatRecord.chart).where(and_(ChatRecord.id == chat_record_id))
    res = session.execute(stmt)
    for row in res:
        try:
            return orjson.loads(row.chart)
        except Exception:
            pass
    return {}


def get_chart_data_with_user(session: SessionDep, current_user: CurrentUser, chat_record_id: int):
    stmt = select(ChatRecord.data).where(and_(ChatRecord.id == chat_record_id, ChatRecord.create_by == current_user.id))
    res = session.execute(stmt)
    for row in res:
        try:
            return orjson.loads(row.data)
        except Exception:
            pass
    return {}


def get_chart_data_with_user_live(session: SessionDep, current_user: CurrentUser, chat_record_id: int):
    stmt = select(ChatRecord.datasource, ChatRecord.sql).where(
        and_(ChatRecord.id == chat_record_id, ChatRecord.create_by == current_user.id))
    row = session.execute(stmt).first()
    return get_chart_data_ds(session, row.datasource, row.sql)


def get_chart_data_ds(session: SessionDep, ds_id, sql):
    json_result: Dict[str, Any] = {'status': 'success', 'data': [], 'message': ''}
    try:
        datasource = get_ds(session, ds_id)
        if datasource is None:
            json_result['status'] = 'failed'
            json_result['message'] = 'Datasource not found'
            return json_result
        else:
            result = exec_sql(ds=datasource, sql=sql, origin_column=False)
            _data = DataFormat.convert_large_numbers_in_object_array(result.get('data'))
            _data = DataFormat.normalize_qualified_sql_column_keys_in_object_array(_data)
            json_result['data'] = _data
            return json_result
    except Exception as e:
        SQLBotLogUtil.error(f"Function failed: {e}")
        json_result['status'] = 'failed'
        json_result['message'] = f"{e}"
        pass
    return json_result


def get_chat_chart_data(session: SessionDep, chat_record_id: int):
    stmt = select(ChatRecord.data).where(and_(ChatRecord.id == chat_record_id))
    res = session.execute(stmt)
    for row in res:
        try:
            return orjson.loads(row.data)
        except Exception:
            pass
    return {}


def get_chat_predict_data_with_user(session: SessionDep, current_user: CurrentUser, chat_record_id: int):
    stmt = select(ChatRecord.predict_data).where(
        and_(ChatRecord.id == chat_record_id, ChatRecord.create_by == current_user.id))
    res = session.execute(stmt)
    for row in res:
        try:
            return orjson.loads(row.predict_data)
        except Exception:
            pass
    return {}


def get_chat_predict_data(session: SessionDep, chat_record_id: int):
    stmt = select(ChatRecord.predict_data).where(and_(ChatRecord.id == chat_record_id))
    res = session.execute(stmt)
    for row in res:
        try:
            return orjson.loads(row.predict_data)
        except Exception:
            pass
    return {}


def get_chat_with_records_with_data(session: SessionDep, chart_id: int, current_user: CurrentUser,
                                    current_assistant: CurrentAssistant) -> ChatInfo:
    return get_chat_with_records(session, chart_id, current_user, current_assistant, True)


dynamic_ds_types = [1, 3]


def get_chat_with_records(session: SessionDep, chart_id: int, current_user: CurrentUser,
                          current_assistant: CurrentAssistant, with_data: bool = False,
                          trans: Trans = None) -> ChatInfo:
    chat = session.get(Chat, chart_id)
    if not chat:
        raise Exception(f"Chat with id {chart_id} not found")
    if chat.create_by != current_user.id:
        raise Exception(f"Chat with id {chart_id} not Owned by the current user")
    chat_info = ChatInfo(**chat.model_dump())

    if current_assistant and current_assistant.type in dynamic_ds_types:
        out_ds_instance = AssistantOutDsFactory.get_instance(current_assistant)
        ds = out_ds_instance.get_ds(chat.datasource, trans)
    else:
        ds = session.get(CoreDatasource, chat.datasource) if chat.datasource else None

    if not ds:
        chat_info.datasource_exists = False
        chat_info.datasource_name = 'Datasource not exist'
    else:
        chat_info.datasource_exists = True
        chat_info.datasource_name = ds.name
        chat_info.ds_type = ds.type

    sql_alias_log = aliased(ChatLog)
    chart_alias_log = aliased(ChatLog)
    analysis_alias_log = aliased(ChatLog)
    predict_alias_log = aliased(ChatLog)

    def latest_log_id(operate: OperationEnum):
        """Id của bản ghi ChatLog mới nhất ứng với một thao tác của bản ghi hỏi đáp đang xét.

        Cần thiết vì một record có thể có NHIỀU log cùng operate: pha sinh SQL được thử lại khi lần
        đầu hỏng, mỗi lần gọi LLM ghi một log riêng (giữ nguyên để không mất token usage). Nếu join
        thẳng theo pid + operate, mỗi log thừa nhân đôi luôn cả dòng ChatRecord và giao diện hiện
        một lượt hỏi thành hai.

        Lấy log mới nhất chứ không phải log đầu tiên: `sql_answer` lưu trên record cũng là của lần
        thử cuối, hai thứ phải khớp nhau.
        """
        inner = aliased(ChatLog)
        return (select(func.max(inner.id))
                .where(and_(inner.pid == ChatRecord.id, inner.type == TypeEnum.CHAT, inner.operate == operate))
                .correlate(ChatRecord).scalar_subquery())

    stmt = (select(ChatRecord.id, ChatRecord.chat_id, ChatRecord.create_time, ChatRecord.finish_time,
                   ChatRecord.question, ChatRecord.sql_answer, ChatRecord.sql, ChatRecord.datasource,
                   ChatRecord.chart_answer, ChatRecord.chart, ChatRecord.answer, ChatRecord.analysis,
                   ChatRecord.predict,
                   ChatRecord.datasource_select_answer, ChatRecord.analysis_record_id, ChatRecord.predict_record_id,
                   ChatRecord.regenerate_record_id,
                   ChatRecord.recommended_question, ChatRecord.first_chat,
                   ChatRecord.finish, ChatRecord.error,
                   sql_alias_log.reasoning_content.label('sql_reasoning_content'),
                   chart_alias_log.reasoning_content.label('chart_reasoning_content'),
                   analysis_alias_log.reasoning_content.label('analysis_reasoning_content'),
                   predict_alias_log.reasoning_content.label('predict_reasoning_content')
                   )
    # Chỉ rõ vế trái của join: các subquery `latest_log_id` cũng tham chiếu ChatRecord nên
    # SQLAlchemy không tự suy được FROM nào là gốc.
    .select_from(ChatRecord)
    .outerjoin(sql_alias_log, and_(sql_alias_log.pid == ChatRecord.id,
                                   sql_alias_log.type == TypeEnum.CHAT,
                                   sql_alias_log.operate == OperationEnum.GENERATE_SQL,
                                   sql_alias_log.id == latest_log_id(OperationEnum.GENERATE_SQL)))
    .outerjoin(chart_alias_log, and_(chart_alias_log.pid == ChatRecord.id,
                                     chart_alias_log.type == TypeEnum.CHAT,
                                     chart_alias_log.operate == OperationEnum.GENERATE_CHART,
                                     chart_alias_log.id == latest_log_id(OperationEnum.GENERATE_CHART)))
    .outerjoin(analysis_alias_log, and_(analysis_alias_log.pid == ChatRecord.id,
                                        analysis_alias_log.type == TypeEnum.CHAT,
                                        analysis_alias_log.operate == OperationEnum.ANALYSIS,
                                        analysis_alias_log.id == latest_log_id(OperationEnum.ANALYSIS)))
    .outerjoin(predict_alias_log, and_(predict_alias_log.pid == ChatRecord.id,
                                       predict_alias_log.type == TypeEnum.CHAT,
                                       predict_alias_log.operate == OperationEnum.PREDICT_DATA,
                                       predict_alias_log.id == latest_log_id(OperationEnum.PREDICT_DATA)))
    .where(and_(ChatRecord.create_by == current_user.id, ChatRecord.chat_id == chart_id)).order_by(
        ChatRecord.create_time))
    if with_data:
        stmt = select(ChatRecord.id, ChatRecord.chat_id, ChatRecord.create_time, ChatRecord.finish_time,
                      ChatRecord.question, ChatRecord.sql_answer, ChatRecord.sql, ChatRecord.datasource,
                      ChatRecord.chart_answer, ChatRecord.chart, ChatRecord.answer, ChatRecord.analysis,
                      ChatRecord.predict,
                      ChatRecord.datasource_select_answer, ChatRecord.analysis_record_id, ChatRecord.predict_record_id,
                      ChatRecord.regenerate_record_id,
                      ChatRecord.recommended_question, ChatRecord.first_chat,
                      ChatRecord.finish, ChatRecord.error, ChatRecord.data, ChatRecord.predict_data).where(
            and_(ChatRecord.create_by == current_user.id, ChatRecord.chat_id == chart_id)).order_by(
            ChatRecord.create_time)

    result = session.execute(stmt).all()
    record_list: list[ChatRecordResult] = []

    # 批量获取所有ChatRecord的token消耗
    record_ids = [row.id for row in result]
    token_usage_map = {}

    if record_ids:
        # 查询所有相关ChatLog的token_usage
        log_stmt = select(ChatLog.pid, ChatLog.token_usage).where(
            and_(
                ChatLog.pid.in_(record_ids),
                ChatLog.local_operation == False,
                ChatLog.operate != OperationEnum.GENERATE_RECOMMENDED_QUESTIONS,
                ChatLog.token_usage.is_not(None)  # 排除token_usage为空的记录
            )
        )
        log_results = session.execute(log_stmt).all()

        # 按pid分组计算total_tokens总和
        for pid, token_usage in log_results:
            if pid and token_usage is not None:
                tokens_to_add = 0

                if isinstance(token_usage, dict):
                    # 处理字典类型: {"input_tokens": 961, "total_tokens": 1006, "output_tokens": 45}
                    if token_usage:  # 非空字典
                        if "total_tokens" in token_usage:
                            token_value = token_usage["total_tokens"]
                            if isinstance(token_value, (int, float)):
                                tokens_to_add = int(token_value)
                elif isinstance(token_usage, (int, float)):
                    tokens_to_add = int(token_usage)
                if tokens_to_add > 0:
                    if pid not in token_usage_map:
                        token_usage_map[pid] = 0
                    token_usage_map[pid] += tokens_to_add

    for row in result:
        # 计算耗时
        duration = None
        if row.create_time and row.finish_time:
            try:
                time_diff = row.finish_time - row.create_time
                duration = time_diff.total_seconds()  # 转换为秒
            except Exception:
                duration = None

        # 获取token总消耗
        total_tokens = token_usage_map.get(row.id, 0)

        if not with_data:
            record_list.append(
                ChatRecordResult(id=row.id, chat_id=row.chat_id, create_time=row.create_time,
                                 finish_time=row.finish_time,
                                 duration=duration,
                                 total_tokens=total_tokens,
                                 question=row.question, sql_answer=row.sql_answer, sql=row.sql,
                                 datasource=row.datasource,
                                 chart_answer=row.chart_answer, chart=row.chart, answer=row.answer,
                                 analysis=row.analysis, predict=row.predict,
                                 datasource_select_answer=row.datasource_select_answer,
                                 analysis_record_id=row.analysis_record_id, predict_record_id=row.predict_record_id,
                                 regenerate_record_id=row.regenerate_record_id,
                                 recommended_question=row.recommended_question, first_chat=row.first_chat,
                                 finish=row.finish, error=row.error,
                                 sql_reasoning_content=row.sql_reasoning_content,
                                 chart_reasoning_content=row.chart_reasoning_content,
                                 analysis_reasoning_content=row.analysis_reasoning_content,
                                 predict_reasoning_content=row.predict_reasoning_content,
                                 ))
        else:
            record_list.append(
                ChatRecordResult(id=row.id, chat_id=row.chat_id, create_time=row.create_time,
                                 finish_time=row.finish_time,
                                 duration=duration,
                                 total_tokens=total_tokens,
                                 question=row.question, sql_answer=row.sql_answer, sql=row.sql,
                                 datasource=row.datasource,
                                 chart_answer=row.chart_answer, chart=row.chart, answer=row.answer,
                                 analysis=row.analysis, predict=row.predict,
                                 datasource_select_answer=row.datasource_select_answer,
                                 analysis_record_id=row.analysis_record_id, predict_record_id=row.predict_record_id,
                                 regenerate_record_id=row.regenerate_record_id,
                                 recommended_question=row.recommended_question, first_chat=row.first_chat,
                                 finish=row.finish, error=row.error, data=row.data, predict_data=row.predict_data))

    result = list(map(format_record, record_list))

    for row in result:
        try:
            data_value = row.get('data')
            if data_value is not None:
                row['data'] = format_json_data(data_value)
        except Exception:
            pass

    chat_info.records = result

    # 从已查出的records中取最新的create_time
    record_times = [r.get('create_time') for r in result if r.get('create_time') is not None]
    if record_times:
        chat_info.latest_record_time = max(record_times)

    return chat_info


def format_record(record: ChatRecordResult):
    _dict = record.model_dump()

    if record.sql_answer and record.sql_answer.strip() != '' and record.sql_answer.strip()[0] == '{' and \
            record.sql_answer.strip()[-1] == '}':
        _obj = orjson.loads(record.sql_answer)
        # Rơi về `content` khi không có `reasoning_content`: thinking đã tắt trên toàn hệ thống nên
        # khóa đó luôn rỗng, mà cột sql_answer thực chất chỉ lưu {"content": ...}. Không fallback thì
        # khối "suy luận" trên UI trống trơn, và với lượt hỏi bị hạ cấp thì đây là chỗ DUY NHẤT còn
        # nhìn thấy được câu SQL model đã sinh.
        _dict['sql_answer'] = _obj.get('reasoning_content') or _obj.get('content')
    if record.sql_reasoning_content and record.sql_reasoning_content.strip() != '':
        _dict['sql_answer'] = record.sql_reasoning_content
    if record.chart_answer and record.chart_answer.strip() != '' and record.chart_answer.strip()[0] == '{' and \
            record.chart_answer.strip()[-1] == '}':
        _obj = orjson.loads(record.chart_answer)
        _dict['chart_answer'] = _obj.get('reasoning_content')
    if record.chart_reasoning_content and record.chart_reasoning_content.strip() != '':
        _dict['chart_answer'] = record.chart_reasoning_content
    # Bóc vỏ {"content": ...} để client nhận thẳng chuỗi văn bản, giống cách xử lý analysis bên dưới:
    # event SSE 'answer' lúc streaming đã trả text thuần, nếu đọc lại lịch sử lại ra JSON bọc thì
    # client phải viết hai nhánh parse cho cùng một dữ liệu.
    if record.answer and record.answer.strip() != '' and record.answer.strip()[0] == '{' and \
            record.answer.strip()[-1] == '}':
        _obj = orjson.loads(record.answer)
        _dict['answer'] = _obj.get('content')
    if record.analysis and record.analysis.strip() != '' and record.analysis.strip()[0] == '{' and \
            record.analysis.strip()[-1] == '}':
        _obj = orjson.loads(record.analysis)
        _dict['analysis_thinking'] = _obj.get('reasoning_content')
        _dict['analysis'] = _obj.get('content')
    if record.analysis_reasoning_content and record.analysis_reasoning_content.strip() != '':
        _dict['analysis_thinking'] = record.analysis_reasoning_content
    if record.predict and record.predict.strip() != '' and record.predict.strip()[0] == '{' and record.predict.strip()[
        -1] == '}':
        _obj = orjson.loads(record.predict)
        _dict['predict'] = _obj.get('reasoning_content')
        _dict['predict_content'] = _obj.get('content')
    if record.predict_reasoning_content and record.predict_reasoning_content.strip() != '':
        _dict['predict'] = record.predict_reasoning_content
    if record.data and record.data.strip() != '':
        try:
            _obj = orjson.loads(record.data)
            _dict['data'] = _obj
        except Exception:
            pass
    if record.predict_data and record.predict_data.strip() != '':
        try:
            _obj = orjson.loads(record.predict_data)
            _dict['predict_data'] = _obj
        except Exception:
            pass
    if record.sql and record.sql.strip() != '':
        try:
            _dict['sql'] = sqlparse.format(record.sql, reindent=True)
        except Exception:
            pass

    # 格式化duration字段，保留2位小数
    if 'duration' in _dict and _dict['duration'] is not None:
        try:
            # 可以格式化为更易读的形式
            _dict['duration'] = round(_dict['duration'], 2)  # 保留2位小数
        except Exception:
            pass

    # 格式化total_tokens字段
    if 'total_tokens' in _dict and _dict['total_tokens'] is not None:
        try:
            # 确保是整数类型
            _dict['total_tokens'] = int(_dict['total_tokens']) if _dict['total_tokens'] else 0
        except Exception:
            _dict['total_tokens'] = 0

    # 去除返回前端多余的字段
    _dict.pop('sql_reasoning_content', None)
    _dict.pop('chart_reasoning_content', None)
    _dict.pop('analysis_reasoning_content', None)
    _dict.pop('predict_reasoning_content', None)

    return _dict


def get_chat_log_history(session: SessionDep, chat_record_id: int, current_user: CurrentUser,
                         without_steps: bool = False) -> ChatLogHistory:
    """
    获取ChatRecord的详细历史记录

    Args:
        session: 数据库会话
        chat_record_id: ChatRecord的ID
        current_user: 当前用户
        without_steps

    Returns:
        ChatLogHistory: 包含历史步骤和时间信息的对象
    """
    # 1. 首先验证ChatRecord存在且属于当前用户
    chat_record = session.get(ChatRecord, chat_record_id)
    if not chat_record:
        raise Exception(f"ChatRecord with id {chat_record_id} not found")

    if chat_record.create_by != current_user.id:
        raise Exception(f"ChatRecord with id {chat_record_id} not owned by the current user")

    # 2. 查询与该ChatRecord相关的所有ChatLog记录
    chat_logs = session.query(ChatLog).filter(
        ChatLog.pid == chat_record_id,
        ChatLog.operate != OperationEnum.GENERATE_RECOMMENDED_QUESTIONS
    ).order_by(ChatLog.start_time).all()

    # 3. 计算总的时间和token信息
    total_tokens = 0
    steps = []

    for log in chat_logs:
        # 计算单条记录的token消耗
        log_tokens = 0
        if log.token_usage is not None:
            if isinstance(log.token_usage, dict):
                if log.token_usage and "total_tokens" in log.token_usage:
                    token_value = log.token_usage["total_tokens"]
                    if isinstance(token_value, (int, float)):
                        log_tokens = int(token_value)
            elif isinstance(log.token_usage, (int, float)):
                log_tokens = log.token_usage

        # 累加到总token消耗
        total_tokens += log_tokens

        if not without_steps:
            # 计算单条记录的耗时
            duration = None
            if log.start_time and log.finish_time:
                try:
                    time_diff = log.finish_time - log.start_time
                    duration = round(time_diff.total_seconds(), 2)
                except Exception:
                    duration = None

            # 获取操作类型的枚举名称
            operate_name = None
            message = None
            if log.operate:
                # 如果是OperationEnum枚举实例
                if isinstance(log.operate, OperationEnum):
                    operate_name = log.operate.name
                # 如果是字符串，尝试从枚举值获取名称
                elif isinstance(log.operate, str):
                    try:
                        # 通过枚举值找到对应的枚举实例
                        for enum_item in OperationEnum:
                            if enum_item.value == log.operate:
                                operate_name = enum_item.name
                                break
                    except Exception:
                        operate_name = log.operate
                else:
                    operate_name = str(log.operate)

                if log.messages is not None:
                    message = log.messages
                    if not log.operate == OperationEnum.CHOOSE_TABLE:
                        try:
                            message = orjson.loads(log.messages)
                        except Exception:
                            pass

            # 创建ChatLogHistoryItem
            history_item = ChatLogHistoryItem(
                start_time=log.start_time,
                finish_time=log.finish_time,
                duration=duration,
                total_tokens=log_tokens,
                operate=operate_name,
                local_operation=log.local_operation,
                error=log.error,
                message=message,
            )

            steps.append(history_item)

    # 4. 计算总耗时（使用ChatRecord的时间）
    total_duration = None
    if chat_record.create_time and chat_record.finish_time:
        try:
            time_diff = chat_record.finish_time - chat_record.create_time
            total_duration = round(time_diff.total_seconds(), 2)
        except Exception:
            total_duration = None

    # 5. 创建并返回ChatLogHistory对象
    chat_log_history = ChatLogHistory(
        start_time=chat_record.create_time,  # 使用ChatRecord的create_time
        finish_time=chat_record.finish_time,  # 使用ChatRecord的finish_time
        duration=total_duration,
        total_tokens=total_tokens,
        steps=steps
    )

    return chat_log_history


def get_chat_brief_generate(session: SessionDep, chat_id: int):
    chat = get_chat(session=session, chat_id=chat_id)
    if chat is not None and chat.brief_generate is not None:
        return chat.brief_generate
    else:
        return False


def list_generate_sql_logs(session: SessionDep, chart_id: int) -> List[ChatLog]:
    stmt = select(ChatLog).where(
        and_(ChatLog.pid.in_(select(ChatRecord.id).where(and_(ChatRecord.chat_id == chart_id))),
             ChatLog.type == TypeEnum.CHAT, ChatLog.operate == OperationEnum.GENERATE_SQL)).order_by(
        ChatLog.start_time)
    result = session.execute(stmt).all()
    _list = []
    for row in result:
        for r in row:
            _list.append(ChatLog(**r.model_dump()))
    return _list


def list_generate_chart_logs(session: SessionDep, chart_id: int) -> List[ChatLog]:
    stmt = select(ChatLog).where(
        and_(ChatLog.pid.in_(select(ChatRecord.id).where(and_(ChatRecord.chat_id == chart_id))),
             ChatLog.type == TypeEnum.CHAT, ChatLog.operate == OperationEnum.GENERATE_CHART)).order_by(
        ChatLog.start_time)
    result = session.execute(stmt).all()
    _list = []
    for row in result:
        for r in row:
            _list.append(ChatLog(**r.model_dump()))
    return _list


def create_chat(session: SessionDep, current_user: CurrentUser, create_chat_obj: CreateChat,
                require_datasource: bool = True, current_assistant: CurrentAssistant = None) -> ChatInfo:
    if not create_chat_obj.datasource and require_datasource:
        raise Exception("Datasource cannot be None")

    if not create_chat_obj.question or create_chat_obj.question.strip() == '':
        create_chat_obj.question = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    chat = Chat(create_time=datetime.datetime.now(),
                create_by=current_user.id,
                oid=current_user.oid if current_user.oid is not None else 1,
                brief=trim_chat_brief(create_chat_obj.question),
                origin=create_chat_obj.origin if create_chat_obj.origin is not None else 0)
    ds: CoreDatasource | AssistantOutDsSchema | None = None
    if create_chat_obj.datasource:
        chat.datasource = create_chat_obj.datasource
        if current_assistant and current_assistant.type == 1:
            out_ds_instance: AssistantOutDs = AssistantOutDsFactory.get_instance(current_assistant)
            ds = out_ds_instance.get_ds(chat.datasource)
            ds.type_name = DB.get_db(ds.type)
        else:
            ds = session.get(CoreDatasource, create_chat_obj.datasource)
            # Kiểm tồn tại phải đứng TRƯỚC: bản cũ đọc ds.oid rồi mới hỏi `if not ds`, nên nguồn dữ
            # liệu không tồn tại văng AttributeError và không bao giờ chạm tới câu báo lỗi tử tế.
            if not ds:
                raise Exception(f"Datasource with id {create_chat_obj.datasource} not found")
            if ds.oid != current_user.oid:
                raise Exception(f"Datasource with id {create_chat_obj.datasource} does not belong to current workspace")
            # Nguồn dữ liệu đang nạp có 0 dòng trong core_table, nên M-Schema dựng ra rỗng và LLM sẽ
            # sinh SQL dựa trên hư không. Chặn ngay từ lúc gắn nguồn vào phiên chat.
            if not is_ds_usable(ds):
                raise Exception(
                    f"Datasource with id {create_chat_obj.datasource} is not ready (status={ds.status})"
                )

        chat.engine_type = ds.type_name
    else:
        chat.engine_type = ''

    chat_info = ChatInfo(**chat.model_dump())

    session.add(chat)
    session.flush()
    session.refresh(chat)
    chat_info.id = chat.id
    session.commit()

    if ds:
        chat_info.datasource_exists = True
        chat_info.datasource_name = ds.name
        chat_info.ds_type = ds.type

    if require_datasource and ds:
        # generate first empty record
        record = ChatRecord()
        record.chat_id = chat.id
        record.datasource = ds.id
        record.engine_type = ds.type_name
        record.first_chat = True
        record.finish = True
        record.create_time = datetime.datetime.now()
        record.create_by = current_user.id
        if isinstance(ds, CoreDatasource) and ds.recommended_config == 2:
            questions = get_datasource_recommended_chart(session, ds.id)
            record.recommended_question = orjson.dumps(questions).decode()
            record.recommended_question_answer = orjson.dumps({
                "content": questions
            }).decode()

        _record = ChatRecord(**record.model_dump())

        session.add(record)
        session.flush()
        session.refresh(record)
        _record.id = record.id
        session.commit()

        chat_info.records.append(_record)

    return chat_info


def resolve_chat_id(session: SessionDep, current_user: CurrentUser, chat_id: Union[int, str],
                    datasource_id: Optional[int] = None) -> int:
    """Quy ``chat_id`` client gửi lên về id nội bộ (int) của bảng chat, tạo hội thoại nếu chưa có.

    Hai nguồn gọi có hai kiểu chat_id khác nhau:

    - UI web / MCP gửi **id nội bộ** (int) của hội thoại đã tạo sẵn qua /chat/start → trả về nguyên
      giá trị đó, không tạo mới.
    - Hệ thống tích hợp bên ngoài gửi **UUID** tự sinh và không gọi /chat/start → tra theo cột
      ``chat.external_id``, chưa có thì tạo hội thoại mới (id nội bộ do Identity cấp như bình thường).

    Vì sao không cho đối tác đặt thẳng ``chat.id``: cột đó khai ``Identity(always=True)``, ghi tay
    vào sẽ phải dùng OVERRIDING SYSTEM VALUE rồi setval lại sequence, và chỉ cần quên một nhịp là
    UI web sinh trùng khóa chính. Tách hẳn sang ``external_id`` bỏ được toàn bộ rủi ro đó.

    Tra ``external_id`` trên phạm vi TOÀN HỆ THỐNG chứ không lọc theo người dùng: nếu lọc, một UUID
    trùng với hội thoại của người khác sẽ lọt xuống nhánh tạo mới rồi chết vì vi phạm UNIQUE. Trả
    về id thật để decorator ``require_permissions(type='chat')`` từ chối đúng bản chất — không có
    quyền, chứ không phải lỗi hệ thống.
    """
    # Chuỗi toàn chữ số vẫn hiểu là id nội bộ: client JS hay gửi số dưới dạng string, còn UUID thì
    # không bao giờ toàn chữ số nên không sợ nhập nhằng.
    if isinstance(chat_id, int) or (isinstance(chat_id, str) and chat_id.strip().isdigit()):
        return int(chat_id)

    external_id = chat_id.strip() if isinstance(chat_id, str) else ''
    if not external_id or len(external_id) > 64:
        raise Exception("chat_id must be a non-empty string of at most 64 characters")

    chat: Chat | None = session.query(Chat).filter(Chat.external_id == external_id).first()
    if chat:
        return chat.id

    if not datasource_id:
        raise Exception(
            f"Chat with id {external_id} does not exist. Provide 'datasource' in the request body "
            f"so it can be created automatically."
        )

    ds = session.get(CoreDatasource, datasource_id)
    if not ds:
        raise Exception(f"Datasource with id {datasource_id} not found")
    if ds.oid != current_user.oid:
        raise Exception(f"Datasource with id {datasource_id} does not belong to current workspace")
    if not is_ds_usable(ds):
        raise Exception(f"Datasource with id {datasource_id} is not ready (status={ds.status})")

    now = datetime.datetime.now()
    chat = Chat(
        external_id=external_id,
        oid=current_user.oid if current_user.oid is not None else 1,
        create_time=now,
        create_by=current_user.id,
        brief=now.strftime("%Y-%m-%d %H:%M:%S"),
        chat_type='chat',
        datasource=datasource_id,
        engine_type=ds.type_name,
        origin=0,
        brief_generate=False,
    )
    session.add(chat)
    try:
        session.commit()
    except IntegrityError:
        # Hai câu hỏi đầu tiên cùng một UUID chạy song song: bản thua cuộc rollback rồi dùng lại
        # hội thoại bản thắng vừa tạo, thay vì ném lỗi ra cho đối tác.
        session.rollback()
        chat = session.query(Chat).filter(Chat.external_id == external_id).first()
        if not chat:
            raise
        return chat.id
    session.refresh(chat)

    return chat.id


def save_question(session: SessionDep, current_user: CurrentUser, question: ChatQuestion) -> ChatRecord:
    if not question.chat_id:
        raise Exception("ChatId cannot be None")
    if not question.question or question.question.strip() == '':
        raise Exception("Question cannot be Empty")

    # chat = session.query(Chat).filter(Chat.id == question.chat_id).first()
    chat: Chat = session.get(Chat, question.chat_id)
    if not chat:
        raise Exception(f"Chat with id {question.chat_id} not found")

    record = ChatRecord()
    record.question = question.question
    record.chat_id = chat.id
    record.create_time = datetime.datetime.now()
    record.create_by = current_user.id
    record.datasource = chat.datasource
    record.engine_type = chat.engine_type
    record.ai_modal_id = question.ai_modal_id
    record.regenerate_record_id = question.regenerate_record_id
    record.domain_code = question.domain_code

    result = ChatRecord(**record.model_dump())

    session.add(record)
    session.flush()
    session.refresh(record)
    result.id = record.id
    session.commit()

    return result


def save_analysis_predict_record(session: SessionDep, base_record: ChatRecord, action_type: str) -> ChatRecord:
    record = ChatRecord()
    record.question = base_record.question
    record.chat_id = base_record.chat_id
    record.datasource = base_record.datasource
    record.engine_type = base_record.engine_type
    record.ai_modal_id = base_record.ai_modal_id
    record.create_time = datetime.datetime.now()
    record.create_by = base_record.create_by
    record.chart = base_record.chart
    record.data = base_record.data

    if action_type == 'analysis':
        record.analysis_record_id = base_record.id
    elif action_type == 'predict':
        record.predict_record_id = base_record.id

    result = ChatRecord(**record.model_dump())

    session.add(record)
    session.flush()
    session.refresh(record)
    result.id = record.id
    session.commit()

    return result


def start_log(session: SessionDep, ai_modal_id: int = None, ai_modal_name: str = None, operate: OperationEnum = None,
              record_id: int = None, full_message: Union[list[dict], dict] = None,
              local_operation: bool = False) -> ChatLog:
    log = ChatLog(type=TypeEnum.CHAT, operate=operate, pid=record_id, ai_modal_id=ai_modal_id, base_modal=ai_modal_name,
                  messages=full_message, start_time=datetime.datetime.now(), local_operation=local_operation)

    result = ChatLog(**log.model_dump())

    session.add(log)
    session.flush()
    session.refresh(log)
    result.id = log.id
    session.commit()

    return result


def end_log(session: SessionDep, log: ChatLog, full_message: Union[list[dict], dict, str],
            reasoning_content: str = None,
            token_usage=None) -> ChatLog:
    if token_usage is None:
        token_usage = {}
    log.messages = full_message
    log.token_usage = token_usage
    log.finish_time = datetime.datetime.now()
    log.reasoning_content = reasoning_content if reasoning_content and len(reasoning_content.strip()) > 0 else None

    stmt = update(ChatLog).where(and_(ChatLog.id == log.id)).values(
        messages=log.messages,
        token_usage=log.token_usage,
        finish_time=log.finish_time,
        reasoning_content=log.reasoning_content
    )
    session.execute(stmt)
    session.commit()

    return log


def trigger_log_error(session: SessionDep, log: ChatLog) -> ChatLog:
    log.error = True
    stmt = update(ChatLog).where(and_(ChatLog.id == log.id)).values(
        error=True
    )
    session.execute(stmt)
    session.commit()

    return log


def save_sql_answer(session: SessionDep, record_id: int, answer: str) -> ChatRecord:
    if not record_id:
        raise Exception("Record id cannot be None")

    stmt = update(ChatRecord).where(and_(ChatRecord.id == record_id)).values(
        sql_answer=answer,
    )

    session.execute(stmt)

    session.commit()

    record = get_chat_record_by_id(session, record_id)

    return record


def save_analysis_answer(session: SessionDep, record_id: int, answer: str = '') -> ChatRecord:
    if not record_id:
        raise Exception("Record id cannot be None")

    stmt = update(ChatRecord).where(and_(ChatRecord.id == record_id)).values(
        analysis=answer,
    )

    session.execute(stmt)

    session.commit()

    record = get_chat_record_by_id(session, record_id)

    return record


def save_answer(session: SessionDep, record_id: int, answer: str = '') -> None:
    """Lưu câu trả lời chữ cuối cùng vào cột chat_record.answer.

    Cố ý KHÔNG select lại record để trả về (khác save_analysis_answer): hàm này chạy trong thread
    riêng song song với pha sinh chart, mà thread chart cũng đang gán lại self.record — trả record
    về đây chỉ tạo cơ hội cho hai luồng ghi đè state của nhau. UPDATE chỉ chạm đúng một cột nên
    hai luồng ghi đồng thời không tranh chấp ở phía DB.
    """
    if not record_id:
        raise Exception("Record id cannot be None")

    stmt = update(ChatRecord).where(and_(ChatRecord.id == record_id)).values(
        answer=answer,
    )

    session.execute(stmt)

    session.commit()


def save_predict_answer(session: SessionDep, record_id: int, answer: str) -> ChatRecord:
    if not record_id:
        raise Exception("Record id cannot be None")

    stmt = update(ChatRecord).where(and_(ChatRecord.id == record_id)).values(
        predict=answer,
    )

    session.execute(stmt)

    session.commit()

    record = get_chat_record_by_id(session, record_id)

    return record


def save_select_datasource_answer(session: SessionDep, record_id: int, answer: str,
                                  datasource: int = None, engine_type: str = None) -> ChatRecord:
    if not record_id:
        raise Exception("Record id cannot be None")
    record = get_chat_record_by_id(session, record_id)

    record.datasource_select_answer = answer

    if datasource:
        record.datasource = datasource
        record.engine_type = engine_type

    result = ChatRecord(**record.model_dump())

    if datasource:
        stmt = update(ChatRecord).where(and_(ChatRecord.id == record.id)).values(
            datasource_select_answer=record.datasource_select_answer,
            datasource=record.datasource,
            engine_type=record.engine_type,
        )
    else:
        stmt = update(ChatRecord).where(and_(ChatRecord.id == record.id)).values(
            datasource_select_answer=record.datasource_select_answer,
        )

    session.execute(stmt)

    session.commit()

    return result


def save_recommend_question_answer(session: SessionDep, record_id: int,
                                   answer: dict = None, articles_number: Optional[int] = 4) -> ChatRecord:
    if not record_id:
        raise Exception("Record id cannot be None")

    recommended_question_answer = orjson.dumps(answer).decode()

    json_str = '[]'
    if answer and answer.get('content') and answer.get('content') != '':
        try:
            json_str = extract_nested_json(answer.get('content'))

            if not json_str:
                json_str = '[]'
        except Exception as e:
            pass
    recommended_question = json_str

    stmt = update(ChatRecord).where(and_(ChatRecord.id == record_id)).values(
        recommended_question_answer=recommended_question_answer,
        recommended_question=recommended_question,
    )

    session.execute(stmt)
    session.commit()

    record = get_chat_record_by_id(session, record_id)
    record.recommended_question_answer = recommended_question_answer
    record.recommended_question = recommended_question
    if articles_number > 4:
        stmt_chat = update(Chat).where(and_(Chat.id == record.chat_id)).values(
            recommended_question_answer=recommended_question_answer,
            recommended_question=recommended_question,
            recommended_generate=True
        )
        session.execute(stmt_chat)
        session.commit()

    return record


def save_sql(session: SessionDep, record_id: int, sql: str) -> ChatRecord:
    if not record_id:
        raise Exception("Record id cannot be None")

    record = get_chat_record_by_id(session, record_id)

    record.sql = sql

    result = ChatRecord(**record.model_dump())

    stmt = update(ChatRecord).where(and_(ChatRecord.id == record.id)).values(
        sql=record.sql
    )

    session.execute(stmt)

    session.commit()

    return result


def save_chart_answer(session: SessionDep, record_id: int, answer: str) -> ChatRecord:
    if not record_id:
        raise Exception("Record id cannot be None")

    stmt = update(ChatRecord).where(and_(ChatRecord.id == record_id)).values(
        chart_answer=answer,
    )

    session.execute(stmt)

    session.commit()

    record = get_chat_record_by_id(session, record_id)

    return record


def save_chart(session: SessionDep, record_id: int, chart: str) -> ChatRecord:
    if not record_id:
        raise Exception("Record id cannot be None")
    record = get_chat_record_by_id(session, record_id)

    record.chart = chart

    result = ChatRecord(**record.model_dump())

    stmt = update(ChatRecord).where(and_(ChatRecord.id == record.id)).values(
        chart=record.chart
    )

    session.execute(stmt)

    session.commit()

    return result


def save_predict_data(session: SessionDep, record_id: int, data: str = '') -> ChatRecord:
    if not record_id:
        raise Exception("Record id cannot be None")
    record = get_chat_record_by_id(session, record_id)

    record.predict_data = data

    result = ChatRecord(**record.model_dump())

    stmt = update(ChatRecord).where(and_(ChatRecord.id == record.id)).values(
        predict_data=record.predict_data
    )

    session.execute(stmt)

    session.commit()

    return result


def save_error_message(session: SessionDep, record_id: int, message: str) -> ChatRecord:
    if not record_id:
        raise Exception("Record id cannot be None")
    record = get_chat_record_by_id(session, record_id)

    record.error = message
    record.finish = True
    record.finish_time = datetime.datetime.now()

    result = ChatRecord(**record.model_dump())

    stmt = update(ChatRecord).where(and_(ChatRecord.id == record.id)).values(
        error=record.error,
        finish=record.finish,
        finish_time=record.finish_time
    )

    session.execute(stmt)

    session.commit()

    # log error finish
    stmt = update(ChatLog).where(and_(ChatLog.pid == record.id, ChatLog.finish_time.is_(None))).values(
        finish_time=record.finish_time,
        error=True
    )
    session.execute(stmt)
    session.commit()

    return result


def save_sql_exec_data(session: SessionDep, record_id: int, data: str) -> ChatRecord:
    if not record_id:
        raise Exception("Record id cannot be None")
    record = get_chat_record_by_id(session, record_id)

    record.data = data

    result = ChatRecord(**record.model_dump())

    stmt = update(ChatRecord).where(and_(ChatRecord.id == record.id)).values(
        data=record.data,
    )

    session.execute(stmt)

    session.commit()

    return result


def finish_record(session: SessionDep, record_id: int) -> ChatRecord:
    if not record_id:
        raise Exception("Record id cannot be None")
    record = get_chat_record_by_id(session, record_id)

    record.finish = True
    record.finish_time = datetime.datetime.now()

    result = ChatRecord(**record.model_dump())

    stmt = update(ChatRecord).where(and_(ChatRecord.id == record.id)).values(
        finish=record.finish,
        finish_time=record.finish_time
    )

    session.execute(stmt)

    session.commit()

    return result


def get_user_old_questions(session: SessionDep, datasource: int, create_by: int,
                           domain_code: Optional[str] = None, limit: int = 20) -> List[str]:
    """Câu hỏi cũ CỦA CHÍNH user, dùng làm gợi ý ngữ cảnh cho pha sinh câu hỏi tiếp theo.

    Tồn tại song song với `get_old_questions` chứ không thay thế: hàm kia lọc mỗi theo datasource
    nên trả về câu hỏi của MỌI user dùng chung nguồn dữ liệu — chấp nhận được với endpoint rời
    (đang giữ nguyên hợp đồng cũ), nhưng không chấp nhận được ở đây vì chuỗi câu hỏi thô của người
    khác lọt thẳng vào prompt rồi hiện lên màn hình người đang hỏi.

    `domain_code` khác None thì siết thêm về đúng lĩnh vực của lượt hiện tại, để gợi ý không kéo
    người dùng sang lĩnh vực họ đang không làm việc. Để None thì lấy câu hỏi cũ ở mọi lĩnh vực —
    an toàn vì mọi record lấy ra đều do chính user này tạo, tức đã nằm trong quyền của họ tại thời
    điểm hỏi.

    Chỉ lấy lượt thành công (`error IS NULL`): câu hỏi làm gãy pipeline lần trước mà đem gợi ý lại
    thì khả năng cao gãy tiếp.
    """
    records: List[str] = []
    if not datasource or not create_by:
        return records
    conditions = [ChatRecord.datasource == datasource,
                  ChatRecord.create_by == create_by,
                  ChatRecord.question.isnot(None),
                  ChatRecord.error.is_(None)]
    if domain_code:
        conditions.append(ChatRecord.domain_code == domain_code)
    stmt = select(ChatRecord.question).where(and_(*conditions)).order_by(
        ChatRecord.create_time.desc()).limit(limit)
    for r in session.execute(stmt):
        records.append(r.question)
    return records


def save_record_recommend_questions(session: SessionDep, record_id: int, answer: dict) -> str:
    """Ghi gợi ý câu hỏi vào ĐÚNG một record, không đụng tới bản ghi hội thoại.

    Khác `save_recommend_question_answer` ở chỗ bỏ hẳn nhánh `articles_number > 4` — ngưỡng đó
    thực chất mã hoá "panel nào của UI đang gọi" chứ không phải ý định của người dùng, và tác dụng
    phụ của nó (bật cờ `recommended_generate` cấp hội thoại) sẽ khoá cache gợi ý của cả hội thoại
    theo kết quả của một lượt hỏi lẻ.

    LLM trả JSON hỏng thì lưu mảng rỗng và ghi warning chứ không ném: gợi ý là pha phụ, giết lượt
    hỏi vì nó là mất cả câu trả lời đã sinh xong.

    Trả về chuỗi JSON đã lưu để luồng gọi phát thẳng ra SSE, khỏi đọc lại record.
    """
    if not record_id:
        raise Exception("Record id cannot be None")

    recommended_question = '[]'
    content = (answer or {}).get('content')
    if content:
        try:
            recommended_question = extract_nested_json(content) or '[]'
        except Exception as e:
            SQLBotLogUtil.warning(f'Parse inline recommended questions failed, saving empty list: {e}')

    stmt = update(ChatRecord).where(and_(ChatRecord.id == record_id)).values(
        recommended_question_answer=orjson.dumps(answer).decode(),
        recommended_question=recommended_question,
    )
    session.execute(stmt)
    session.commit()
    return recommended_question


def get_old_questions(session: SessionDep, datasource: int):
    records = []
    if not datasource:
        return records
    stmt = select(ChatRecord.question).where(
        and_(ChatRecord.datasource == datasource, ChatRecord.question.isnot(None),
             ChatRecord.error.is_(None))).order_by(
        ChatRecord.create_time.desc()).limit(20)
    result = session.execute(stmt)
    for r in result:
        records.append(r.question)
    return records
