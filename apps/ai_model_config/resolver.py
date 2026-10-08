"""Giải cấu hình LLM cho một lượt hỏi.

Nguyên tắc chung: có dòng cấu hình thì dùng đúng dòng đó, hỏng thì báo lỗi chứ KHÔNG rơi về model
chung (fail-closed). Không có dòng thì chạy y như SQLBot gốc.

Tách khỏi ``embedding.py`` vì module này cần ``model_factory`` (kéo theo sqlbot_xpack), còn phần
embedding được import từ rất sớm trong chuỗi import của upstream.
"""


from sqlmodel import Session

from apps.ai_model.model_factory import LLMConfig, get_default_config
from apps.ai_model_config.crud import get_row
from apps.ai_model_config.crypto import decrypt_row_key
from apps.ai_model_config.models import (
    MODEL_LLM,
    SCOPE_DATASOURCE,
    SCOPE_WORKSPACE,
    AiModelConfig,
)
from apps.chat.models.chat_model import Chat
from apps.datasource.models.datasource import CoreDatasource


def _scoped_llm_row(session: Session, chat_question) -> AiModelConfig | None:
    """Dòng LLM áp cho lượt hỏi: của datasource nếu chat đã có datasource, không thì của workspace.

    Datasource lấy giống ``LLMService.__init__``: datasource đã gắn vào chat thắng
    ``datasource_id`` của câu hỏi. Datasource không có dòng thì trả None (dùng model chung), KHÔNG
    lấy dòng workspace: datasource tạo trước tính năng này phải chạy y như cũ. Chat hay datasource
    không hợp lệ cũng trả None, để ``__init__`` báo lỗi theo cách sẵn có.
    """
    chat = session.get(Chat, chat_question.chat_id)
    if chat is None:
        return None
    ds_id = chat.datasource or chat_question.datasource_id
    if ds_id:
        ds = session.get(CoreDatasource, ds_id)
        if ds is None or ds.oid != chat.oid:
            return None
        return get_row(session, SCOPE_DATASOURCE, ds_id, MODEL_LLM)
    if chat.oid:
        return get_row(session, SCOPE_WORKSPACE, chat.oid, MODEL_LLM)
    return None


async def resolve_llm_config(
    session: Session,
    chat_question,
    specialized_model_id: int | None = None,
    skip_scoped: bool = False,
) -> LLMConfig:
    """Chọn cấu hình LLM cho một lượt hỏi.

    Thứ tự: model riêng của trợ lý nhúng (``specialized_model_id``) → dòng LLM theo
    ``_scoped_llm_row`` → model mặc định của hệ thống. ``skip_scoped`` cho trợ lý dùng datasource
    bên ngoài: datasource của họ không nằm trong ``core_datasource``.

    Không áp ``apply_disable_thinking``: tham số đó dành cho model tự host, gửi kèm tới model
    qua LiteLLM có thể bị từ chối. ``model_id`` để None vì model này không nằm trong ``ai_model``.
    """
    if specialized_model_id or skip_scoped:
        return await get_default_config(specialized_model_id)
    row = _scoped_llm_row(session, chat_question)
    if row is None:
        return await get_default_config(None)
    return LLMConfig(
        model_id=None,
        model_type="openai",
        model_name=row.model,
        api_key=decrypt_row_key(row),
        api_base_url=row.base_url,
    )
