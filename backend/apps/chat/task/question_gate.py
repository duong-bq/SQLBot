"""Cổng định tuyến câu hỏi: quyết định lượt hỏi có cần chạy pha SQL hay không.

Pha SQL là pha đắt nhất của pipeline — một lượt gọi LLM dài, mang theo cả m-schema vài chục KB —
nhưng nó chạy cho MỌI câu hỏi, kể cả câu chào hỏi, câu tham chiếu ngược vào kết quả lượt trước,
hay câu hỏi về chính datasource. Ba cái giá phải trả: tiền và độ trễ, ngữ cảnh bị nhiễu bởi dữ
liệu không ai cần, và những event SSE đã trót phát ra dây mà không prompt nào ở pha sau gỡ lại
được.

Module này đặt một lượt gọi LLM NGẮN trước pha SQL để phân loại câu hỏi. Ba ràng buộc thiết kế:

- **Hỏng-mở.** Mọi đường hỏng — LLM lỗi, JSON không đọc được, phân loại mâu thuẫn — đều rơi về
  ``'sql'``, tức đúng hành vi hiện hành. Cổng không bao giờ ném exception ra ngoài; chọn nhầm
  ``'sql'`` chỉ tốn vài giây, chọn nhầm ``'answer'`` là người dùng mất số liệu họ cần.
- **Không phải generator.** Cổng trả về một giá trị thường chứ không ``yield``, nên chỗ gọi chỉ là
  một phép gán. Đây là lựa chọn có chủ ý: đi qua ``yield from`` là kéo theo cả lớp bẫy hai-kênh mà
  ``_sql_phase`` phải cảnh báo bằng cờ ``stopped``. Cổng không phát event SSE nào của riêng nó.
- **Log riêng, không đụng ``current_logs``.** Cổng tự đóng bản ghi ``chat_log`` của mình trước khi
  trả về, nên không cần gửi gắm cho khối ``except`` của ``run_task``.

Chế độ CHỈ ĐO (``LLM_ROUTE_SHADOW``) là bước bắt buộc trước khi bật nhánh answer: cổng vẫn chạy,
vẫn ghi log đầy đủ, nhưng quyết định bị ép về ``'sql'``. Đó là cách duy nhất lấy được phân bố thật
trên lưu lượng thật — bộ eval offline quá nhỏ để tin vào tỷ lệ nó cho ra.
"""

from dataclasses import dataclass, replace
from typing import Any

import orjson
from sqlmodel import Session

from apps.chat.curd.chat import end_log, get_recent_qa_history, start_log, trigger_log_error
from apps.chat.models.chat_model import (
    AIPromptMessage,
    HumanPromptMessage,
    OperationEnum,
    SystemPromptMessage,
)
from common.core.config import settings
from common.utils.utils import SQLBotLogUtil, extract_json_object

ACTION_SQL = 'sql'
ACTION_ANSWER = 'answer'

# Nhóm dẫn tới nhánh trả lời thẳng, không chạy SQL.
ANSWER_CATEGORIES = frozenset({'backref', 'meta', 'chitchat', 'general'})
# Nhóm duy nhất dẫn tới pha SQL. Tách hằng số riêng vì đây là giá trị mặc định của mọi đường hỏng.
CATEGORY_NEED_DATA = 'need_data'
ALL_CATEGORIES = ANSWER_CATEGORIES | {CATEGORY_NEED_DATA}


@dataclass(frozen=True)
class RouteDecision:
    """Quyết định của cổng, đã chuẩn hoá và đã an toàn để hành động theo.

    ``action`` là thứ duy nhất chi phối luồng chạy. ``category`` là tập ĐÓNG, tồn tại để tổng hợp
    được bằng SQL trên bảng log — ``reason`` là chữ tự do nên không đếm được, còn tỷ lệ từng nhóm
    mới là con số dùng để quyết định có bật cổng hay không.

    ``forced`` ghi lý do ``action`` bị ép khác với thứ LLM chọn: rỗng là không bị ép, còn lại là
    ``'shadow'`` (chế độ chỉ đo), ``'unparsed'``, ``'mismatch'``, ``'error'``, ``'disabled'``.
    Nhờ vậy log phân biệt được "cổng chọn sql" với "cổng bị ép về sql", hai thứ mà gộp lại thì số
    đo mất sạch ý nghĩa.
    """

    action: str
    category: str
    reason: str = ''
    forced: str = ''

    @property
    def skip_sql(self) -> bool:
        """Có bỏ qua pha SQL cho lượt này không. Điểm quyết định duy nhất ở chỗ gọi."""
        return self.action == ACTION_ANSWER


def fail_open(forced: str, reason: str = '') -> RouteDecision:
    """Quyết định mặc định khi cổng không cho ra kết luận dùng được: chạy pha SQL như cũ."""
    return RouteDecision(action=ACTION_SQL, category=CATEGORY_NEED_DATA,
                         reason=reason, forced=forced)


def parse_route_answer(text: str) -> RouteDecision:
    """Đọc chữ LLM trả về thành một ``RouteDecision`` đã chuẩn hoá.

    Hàm thuần, không chạm DB lẫn LLM — đây là chỗ chứa toàn bộ luật hỏng-mở nên cũng là chỗ đáng
    test nhất của module.

    Ba lớp kiểm, lớp nào không qua cũng rơi về ``'sql'``:

    1. Có đọc được một object JSON có khoá ``action`` trong chuỗi không. Lọc theo khoá thay vì
       theo vị trí là bắt buộc: khi model không tắt được thinking, phần suy luận rơi thẳng vào
       content và thường mang sẵn một object JSON nháp — lấy "cái đầu tiên" là bốc nhầm bản nháp.
    2. ``action`` có thuộc hai giá trị hợp lệ không.
    3. ``action`` và ``category`` có nhất quán không. Mâu thuẫn nghĩa là LLM đang lưỡng lự, mà cổng
       lưỡng lự thì phải nghiêng về phía an toàn chứ không phải bốc một trong hai.
    """
    obj = None
    if text and text.strip():
        try:
            raw = extract_json_object(text, required_keys={'action'})
            obj = orjson.loads(raw) if raw else None
        except Exception:
            obj = None
    if not isinstance(obj, dict):
        return fail_open('unparsed')

    action = str(obj.get('action') or '').strip().lower()
    category = str(obj.get('category') or '').strip().lower()
    reason = str(obj.get('reason') or '').strip()

    if action not in (ACTION_SQL, ACTION_ANSWER):
        return fail_open('unparsed', reason)
    if category not in ALL_CATEGORIES:
        return fail_open('mismatch', reason)
    expected = ACTION_ANSWER if category in ANSWER_CATEGORIES else ACTION_SQL
    if action != expected:
        return RouteDecision(action=ACTION_SQL, category=category,
                             reason=reason, forced='mismatch')
    return RouteDecision(action=action, category=category, reason=reason)


def build_route_prompts(llm_service: Any, _session: Session) -> tuple[str, str]:
    """Dựng cặp (system, user) prompt cho cổng.

    Chỉ đưa TÊN bảng chứ không đưa m-schema: cổng mà đọc m-schema thì nó đã tiêu đúng cái ngân
    sách ngữ cảnh mà nó sinh ra để tiết kiệm. Tên bảng đủ để nhận nhóm hỏi-về-datasource, còn
    nhóm cần-số-liệu vốn không phụ thuộc vào việc biết chi tiết cột.

    Lịch sử lấy ít lượt và ít dòng hơn nhánh answer hạ cấp (``LLM_ROUTE_HISTORY_*``): cổng chỉ cần
    biết lượt trước đã lấy về những gì để nhận ra câu tham chiếu ngược, không cần đủ dữ liệu để
    trả lời.
    """
    question = llm_service.chat_question.model_copy()
    question.route_schema = '\n'.join(llm_service.table_name_list or [])
    question.route_history = get_recent_qa_history(
        session=_session,
        chat_id=llm_service.record.chat_id,
        exclude_record_id=llm_service.record.id,
        rounds=settings.LLM_ROUTE_HISTORY_ROUNDS,
        max_rows=settings.LLM_ROUTE_HISTORY_ROWS,
    )
    return question.route_sys_question(), question.route_user_question()


def decide_route(llm_service: Any, _session: Session) -> RouteDecision:
    """Chạy cổng cho lượt hỏi hiện tại và trả về quyết định đã an toàn để hành động theo.

    Nhận nguyên ``llm_service`` thay vì các giá trị rời: cổng cần model, câu hỏi, danh sách bảng
    và ``record`` để ghi log — truyền rời thì chữ ký dài hơn phần thân. Phần thuần đã tách sang
    ``parse_route_answer`` nên vẫn test được mà không phải dựng một service giả.

    Không ném exception ra ngoài trong MỌI trường hợp: cổng là thứ chèn vào giữa một đường đang
    chạy tốt, nên hỏng ở đây phải im lặng thành hành vi cũ chứ không được giết lượt hỏi.
    """
    # Import muộn để tránh vòng import: `llm.py` import module này ở cấp module, còn `process_stream`
    # lại nằm trong chính `llm.py`. Tới lúc hàm này chạy thì `llm.py` đã nạp xong.
    from apps.chat.task.llm import process_stream

    if not settings.LLM_ROUTE_ENABLED:
        return fail_open('disabled')

    log = None
    try:
        sys_prompt, user_prompt = build_route_prompts(llm_service, _session)
        route_msg = [SystemPromptMessage(content=sys_prompt),
                     HumanPromptMessage(content=user_prompt)]
        log = start_log(session=_session,
                        ai_modal_id=llm_service.route_config.model_id,
                        ai_modal_name=llm_service.route_config.model_name,
                        operate=OperationEnum.ROUTE_QUESTION,
                        record_id=llm_service.record.id,
                        full_message=[{'type': msg.type,
                                       'sqlbot_system': getattr(msg, 'sqlbot_system', False) is True,
                                       'content': msg.content} for msg in route_msg])

        full_text = ''
        full_thinking = ''
        token_usage: dict = {}
        for chunk in process_stream(llm_service.route_llm.stream(route_msg), token_usage):
            if chunk.get('content'):
                full_text += chunk.get('content')
            if chunk.get('reasoning_content'):
                full_thinking += chunk.get('reasoning_content')

        decision = parse_route_answer(full_text)
        if settings.LLM_ROUTE_SHADOW and decision.action == ACTION_ANSWER:
            decision = replace(decision, action=ACTION_SQL, forced='shadow')

        route_msg.append(AIPromptMessage(content=full_text))
        end_log(session=_session, log=log,
                full_message=[{'type': msg.type,
                               'sqlbot_system': getattr(msg, 'sqlbot_system', False) is True,
                               'content': msg.content} for msg in route_msg],
                reasoning_content=full_thinking,
                token_usage=token_usage)
        SQLBotLogUtil.info(
            f'[route] action={decision.action} category={decision.category} '
            f'forced={decision.forced or "-"} reason={decision.reason}')
        return decision
    except Exception as e:
        SQLBotLogUtil.error(f'[route] gate failed, fall back to sql: {e}')
        if log is not None:
            try:
                trigger_log_error(session=_session, log=log)
            except Exception:
                # Đường log hỏng thì thôi: mất một dòng thống kê còn hơn giết lượt hỏi vì nó.
                pass
        return fail_open('error')
