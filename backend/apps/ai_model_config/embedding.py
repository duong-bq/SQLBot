"""Model embedding theo datasource: embed lúc lưu bảng/datasource và xếp hạng lúc hỏi.

Có dòng embedding của datasource thì dùng model đó; không có thì dùng model chung
(``EmbeddingModelCache``), y như SQLBot gốc. Chạy được trong thread nền, cache có khoá thread.
"""

import json
import threading
from collections import OrderedDict
from typing import Optional

from langchain_core.embeddings import Embeddings
from openai import BadRequestError
from sqlmodel import Session, select

from apps.ai_model.embedding import EmbeddingModelCache
from apps.ai_model_config.crud import record_dim
from apps.ai_model_config.crypto import cache_key, decrypt_row_key
from apps.ai_model_config.errors import sanitize_secret
from apps.ai_model_config.models import MODEL_EMBEDDING, SCOPE_DATASOURCE, AiModelConfig
from apps.datasource.embedding.utils import cosine_similarity
from common.core.db import engine
from common.utils.utils import SQLBotLogUtil

_EMBEDDER_CACHE_MAX = 256

_lock = threading.Lock()
_embedders: "OrderedDict[tuple, Embeddings]" = OrderedDict()


def _embedding_rows(ds_ids: list[int]) -> dict[int, AiModelConfig]:
    """Dòng embedding của các datasource, đọc bằng session riêng (gọi được từ thread nền).

    Đối tượng trả về đã tách khỏi session nhưng vẫn giữ giá trị các cột đã nạp.
    """
    ids = [ds_id for ds_id in ds_ids if ds_id]
    if not ids:
        return {}
    with Session(engine) as session:
        rows = session.exec(
            select(AiModelConfig).where(
                AiModelConfig.scope == SCOPE_DATASOURCE,
                AiModelConfig.scope_id.in_(ids),
                AiModelConfig.model_type == MODEL_EMBEDDING,
            )
        ).all()
        return {row.scope_id: row for row in rows}


def _embedder_for_row(row: AiModelConfig) -> Embeddings:
    """Client embedding kiểu OpenAI cho một dòng cấu hình, cache LRU có trần."""
    key = cache_key(row)
    with _lock:
        model = _embedders.get(key)
        if model is not None:
            _embedders.move_to_end(key)
            return model
    from langchain_openai import OpenAIEmbeddings

    model = OpenAIEmbeddings(
        model=row.model,
        openai_api_base=row.base_url,
        openai_api_key=decrypt_row_key(row),
        check_embedding_ctx_length=False,
    )
    with _lock:
        _embedders[key] = model
        while len(_embedders) > _EMBEDDER_CACHE_MAX:
            _embedders.popitem(last=False)
    return model


def get_embedding_for_ds(ds_id: Optional[int]) -> Embeddings:
    """Model embedding của datasource; không có dòng cấu hình thì dùng model chung."""
    row = _embedding_rows([ds_id]).get(ds_id) if ds_id else None
    if row is None:
        return EmbeddingModelCache.get_model()
    return _embedder_for_row(row)


class DsEmbedder:
    """Embed văn bản theo model của từng datasource, dùng cho một lượt job embedding nền.

    Một job có thể trộn bảng của nhiều datasource (job lấp embedding lúc khởi động), nên lỗi được
    cô lập theo model: khoá của một datasource hỏng thì bỏ qua các datasource cùng model đó, các
    datasource khác vẫn chạy.
    """

    def __init__(self):
        """Khởi tạo bộ nhớ tạm cho một lượt job."""
        self._rows: dict[int, Optional[AiModelConfig]] = {}
        self._broken: set = set()

    def _row(self, ds_id: int) -> Optional[AiModelConfig]:
        """Dòng embedding của datasource, tra một lần mỗi lượt job."""
        if ds_id not in self._rows:
            self._rows[ds_id] = _embedding_rows([ds_id]).get(ds_id)
        return self._rows[ds_id]

    def embed(self, ds_id: int, text: str, fallback_text: Optional[str] = None) -> Optional[str]:
        """Trả vector dạng chuỗi JSON để ghi thẳng vào cột ``embedding``, hoặc None nếu bỏ qua.

        ``fallback_text`` là bản rút gọn dùng khi văn bản vượt ngữ cảnh của model (lỗi 400). Lỗi
        400 không đánh dấu model hỏng vì nó thuộc về văn bản, không thuộc về model. Lần embed
        thành công đầu tiên ghi số chiều vào dòng cấu hình; về sau lệch số chiều (model phía sau
        LiteLLM đã đổi) thì không ghi, để vector cũ và mới không trộn lẫn trong cùng datasource.
        """
        row = self._row(ds_id)
        memo = row.id if row is not None else "global"
        if memo in self._broken:
            return None
        try:
            model = _embedder_for_row(row) if row is not None else EmbeddingModelCache.get_model()
            try:
                vector = model.embed_query(text)
            except BadRequestError:
                if fallback_text is None:
                    raise
                SQLBotLogUtil.info(
                    f'datasource {ds_id} embedding text exceeds context length, '
                    f'retry with table-name-only summary'
                )
                vector = model.embed_query(fallback_text)
        except BadRequestError as exc:
            SQLBotLogUtil.error(f'embedding of datasource {ds_id} rejected: {sanitize_secret(str(exc))}')
            return None
        except Exception as exc:
            self._broken.add(memo)
            SQLBotLogUtil.error(f'embedding of datasource {ds_id} failed: {sanitize_secret(str(exc))}')
            return None
        if row is not None:
            if row.dim is None:
                with Session(engine) as session:
                    record_dim(session, row.id, len(vector))
                    session.commit()
                row.dim = len(vector)
            elif row.dim != len(vector):
                SQLBotLogUtil.error(
                    f'embedding of datasource {ds_id} has dim {len(vector)}, expected {row.dim}; skipped'
                )
                return None
        return json.dumps(vector)


def rank_ds_by_embedding(items: list[dict], question: str) -> list[dict]:
    """Xếp hạng datasource theo độ gần với câu hỏi khi các datasource có thể dùng model khác nhau.

    Điểm cosine giữa hai model embedding khác nhau không so được với nhau, nên: nhóm datasource
    theo model (``base_url`` + ``model``), xếp hạng trong từng nhóm bằng vector câu hỏi của chính
    model đó, rồi xen kẽ theo thứ hạng. Chỉ một nhóm thì kết quả y như cách xếp hạng gốc.

    ``items`` theo hình dạng của ``get_ds_embedding``: mỗi phần tử có ``id``, ``embedding`` (chuỗi
    JSON hoặc rỗng) và ``cosine_similarity``. Lỗi gọi model hay lệch số chiều được ném ra để bên gọi
    xử lý như trước.
    """
    rows = _embedding_rows([item.get('id') for item in items])
    groups: dict = {}
    for item in items:
        row = rows.get(item.get('id'))
        key = (row.base_url, row.model) if row is not None else None
        groups.setdefault(key, (row, []))[1].append(item)

    ranked = []
    for row, members in groups.values():
        model = _embedder_for_row(row) if row is not None else EmbeddingModelCache.get_model()
        q_embedding = model.embed_query(question)
        for item in members:
            if item.get('embedding'):
                item['cosine_similarity'] = cosine_similarity(q_embedding, json.loads(item['embedding']))
        members.sort(key=lambda x: x['cosine_similarity'], reverse=True)
        ranked.append(members)

    if len(ranked) == 1:
        return ranked[0]
    out = []
    for rank in range(max(len(members) for members in ranked)):
        layer = [members[rank] for members in ranked if rank < len(members)]
        layer.sort(key=lambda x: x['cosine_similarity'], reverse=True)
        out.extend(layer)
    return out
