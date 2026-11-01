# Author: Junjun
# Date: 2025/9/23
import json
import time
import traceback

from apps.ai_model.embedding import EmbeddingModelCache
from apps.ai_model_config.embedding import get_embedding_for_ds
from apps.datasource.embedding.utils import cosine_similarity
from common.core.config import settings
from common.utils.utils import SQLBotLogUtil


def get_table_embedding(tables: list[dict], question: str):
    _list = []
    for table in tables:
        _list.append({"id": table.get('id'), "schema_table": table.get('schema_table'), "cosine_similarity": 0.0})

    if _list:
        try:
            text = [s.get('schema_table') for s in _list]

            model = EmbeddingModelCache.get_model()
            start_time = time.time()
            results = model.embed_documents(text)
            end_time = time.time()
            SQLBotLogUtil.info(
                f'Embedded {len(text)} table schemas in {end_time - start_time:.2f} seconds',
                extra={'tables': len(text), 'duration_s': round(end_time - start_time, 2)},
            )

            q_embedding = model.embed_query(question)
            for index in range(len(results)):
                item = results[index]
                _list[index]['cosine_similarity'] = cosine_similarity(q_embedding, item)

            _list.sort(key=lambda x: x['cosine_similarity'], reverse=True)
            _list = _list[:settings.TABLE_EMBEDDING_COUNT]
            # print(len(_list))
            SQLBotLogUtil.info(
                f'Ranked the tables by embedding similarity, kept {len(_list)}',
                extra={'tables': len(_list), 'best_table_ids': [ele.get('id') for ele in _list[:5]]},
            )
            return _list
        except Exception:
            SQLBotLogUtil.exception("Unexpected error in get table embedding", extra={"function": "get_table_embedding"})
    return _list


def calc_table_embedding(tables: list[dict], question: str, ds_id: int = None):
    """Xếp hạng bảng theo độ gần với câu hỏi, dùng vector đã lưu của từng bảng.

    ``ds_id`` để embed câu hỏi bằng đúng model đã embed các bảng của datasource đó. Lỗi (kể cả lệch
    số chiều) thì trả nguyên danh sách bảng, không lọc — như hành vi gốc.
    """
    _list = []
    for table in tables:
        _list.append(
            {"id": table.get('id'), "schema_table": table.get('schema_table'), "embedding": table.get('embedding'),
             "cosine_similarity": 0.0, "table_name": table.get('table_name')})

    if _list:
        try:
            # text = [s.get('schema_table') for s in _list]
            #
            model = get_embedding_for_ds(ds_id)
            start_time = time.time()
            # results = model.embed_documents(text)
            # end_time = time.time()
            # SQLBotLogUtil.info(str(end_time - start_time))
            results = [item.get('embedding') for item in _list]

            q_embedding = model.embed_query(question)
            for index in range(len(results)):
                item = results[index]
                if item:
                    _list[index]['cosine_similarity'] = cosine_similarity(q_embedding, json.loads(item))

            _list.sort(key=lambda x: x['cosine_similarity'], reverse=True)
            _list = _list[:settings.TABLE_EMBEDDING_COUNT]
            # print(len(_list))
            end_time = time.time()
            SQLBotLogUtil.info(
                f'Ranked the tables by stored embedding in {end_time - start_time:.2f} seconds',
                extra={'duration_s': round(end_time - start_time, 2)},
            )
            SQLBotLogUtil.info(
                f'Kept {len(_list)} tables after ranking, best {[ele.get("table_name") for ele in _list[:5]]}',
                extra={'tables': len(_list), 'best_table_names': [ele.get('table_name') for ele in _list[:5]]},
            )
            return _list
        except Exception:
            SQLBotLogUtil.exception("Unexpected error in calc table embedding", extra={"function": "calc_table_embedding"})
    return _list
