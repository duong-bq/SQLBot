"""CRUD cho `chat_attachment` — bản text trích từ file docx đính kèm lượt hỏi.

Tách file riêng khỏi `curd/chat.py` vì file đó đã rất dày; ở đây chỉ có hai thao tác điểm:
ghi một dòng lúc lượt hỏi đính file, và đọc theo lô record_id lúc dựng khối lịch sử pha answer.
"""

from datetime import datetime
from typing import Iterable

from sqlalchemy import select
from sqlmodel import Session

from apps.chat.models.chat_model import ChatAttachment


def save_chat_attachment(session: Session, chat_id: int, record_id: int,
                         filename: str | None, content: str, truncated: bool) -> ChatAttachment:
    """Ghi bản trích của tài liệu đính kèm, commit ngay.

    Commit tại chỗ chứ không gộp vào transaction của pipeline: pha answer của CHÍNH lượt này đọc
    lịch sử bằng session khác (`build_answer_prompts` mở session riêng trong luồng chính), và các
    vòng sau cũng vậy — dòng này phải nhìn thấy được ngay khi pipeline bắt đầu chạy.
    """
    attachment = ChatAttachment(chat_id=chat_id, record_id=record_id, filename=filename,
                                content=content, truncated=truncated,
                                create_time=datetime.now())
    session.add(attachment)
    session.commit()
    session.refresh(attachment)
    return attachment


def get_attachments_by_record_ids(session: Session,
                                  record_ids: Iterable[int]) -> dict[int, ChatAttachment]:
    """Đọc tài liệu đính kèm của một lô lượt hỏi, trả map ``record_id -> ChatAttachment``.

    Một lượt có nhiều dòng (tương lai) thì lấy dòng mới nhất — hành vi hợp lý duy nhất khi khối
    lịch sử chỉ chừa chỗ cho một tài liệu mỗi lượt.
    """
    ids = [i for i in record_ids if i]
    if not ids:
        return {}
    stmt = select(ChatAttachment).where(ChatAttachment.record_id.in_(ids)).order_by(
        ChatAttachment.id)
    result: dict[int, ChatAttachment] = {}
    for row in session.execute(stmt).scalars():
        result[row.record_id] = row
    return result
