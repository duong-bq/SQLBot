"""CRUD cho `chat_attachment` — bản text trích từ file docx đính kèm lượt hỏi.

Tách file riêng khỏi `curd/chat.py` vì file đó đã rất dày; ở đây chỉ có hai thao tác điểm:
ghi cả lô dòng lúc lượt hỏi đính file, và đọc theo lô record_id lúc dựng khối lịch sử pha answer.
"""

from datetime import datetime
from typing import Iterable

from sqlalchemy import select
from sqlmodel import Session

from apps.chat.models.chat_model import ChatAttachment


def save_chat_attachments(session: Session, chat_id: int, record_id: int,
                          attachments: Iterable) -> list[ChatAttachment]:
    """Ghi bản trích của TẤT CẢ tài liệu đính kèm một lượt, commit MỘT lần cho cả lô.

    Nhận bất cứ object nào có ba thuộc tính `filename`/`content`/`truncated` — trong thực tế là
    `AttachmentContent` vừa trích trong request.

    Một transaction cho cả lô chứ không mỗi file một transaction: hoặc lượt hỏi có đủ bộ tài liệu
    của nó, hoặc không có gì. Nửa vời thì pha answer đọc lịch sử ra một bộ thiếu file mà không có
    gì báo — LLM trả lời trên tài liệu khuyết và không ai biết. Ghi một lượt cũng giữ được thứ tự
    client gửi trong `id` tăng dần, thứ mà `get_attachments_by_record_ids` dựa vào để đọc lại.

    Commit tại chỗ chứ không gộp vào transaction của pipeline: pha answer của CHÍNH lượt này đọc
    lịch sử bằng session khác (`build_answer_prompts` mở session riêng trong luồng chính), và các
    vòng sau cũng vậy — các dòng này phải nhìn thấy được ngay khi pipeline bắt đầu chạy.
    """
    now = datetime.now()
    rows = [ChatAttachment(chat_id=chat_id, record_id=record_id, filename=a.filename,
                           content=a.content, truncated=a.truncated, create_time=now)
            for a in attachments]
    if not rows:
        return []
    session.add_all(rows)
    session.commit()
    for row in rows:
        session.refresh(row)
    return rows


def get_attachments_by_record_ids(session: Session,
                                  record_ids: Iterable[int]) -> dict[int, list[ChatAttachment]]:
    """Đọc tài liệu đính kèm của một lô lượt hỏi, trả map ``record_id -> danh sách ChatAttachment``.

    Một lượt có nhiều file thì trả về ĐỦ, không gộp và không bỏ bớt: cắt bớt ở đây là cắt trong im
    lặng, còn việc ép ngân sách token là chuyện của tầng render (`render_attachment_blocks`) — nơi
    mỗi lần cắt đều kèm lời báo cho LLM.

    Sắp theo `id` tăng dần, tức đúng thứ tự client gửi lúc đính file (cả lô ghi trong một
    transaction nên `id` liên tiếp).

    Đọc cả lô bằng một câu `IN` thay vì hỏi từng record: hàm này chạy trong lúc dựng prompt pha
    answer, mỗi lượt hỏi gọi một lần cho cả cửa sổ lịch sử.
    """
    ids = [i for i in record_ids if i]
    if not ids:
        return {}
    stmt = select(ChatAttachment).where(ChatAttachment.record_id.in_(ids)).order_by(
        ChatAttachment.id)
    result: dict[int, list[ChatAttachment]] = {}
    for row in session.execute(stmt).scalars():
        result.setdefault(row.record_id, []).append(row)
    return result
