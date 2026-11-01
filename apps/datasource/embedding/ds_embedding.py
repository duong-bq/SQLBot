# Author: Junjun
# Date: 2025/9/18
import json
import time
import traceback
from typing import Optional

from apps.ai_model.embedding import EmbeddingModelCache
from apps.ai_model_config.embedding import rank_ds_by_embedding
from apps.datasource.embedding.utils import cosine_similarity
from apps.datasource.models.datasource import CoreDatasource
from apps.system.crud.assistant import AssistantOutDs
from common.core.config import settings
from common.core.deps import CurrentAssistant
from common.core.deps import SessionDep
from common.utils.utils import SQLBotLogUtil


def get_ds_embedding(session: SessionDep, _ds_list, out_ds: AssistantOutDs,
                     question: str,
                     current_assistant: Optional[CurrentAssistant] = None):
    """Chọn các datasource gần câu hỏi nhất theo embedding, tối đa ``DS_EMBEDDING_COUNT``.

    Datasource nội bộ có thể dùng model embedding khác nhau nên xếp hạng theo nhóm model
    (``rank_ds_by_embedding``). Lỗi thì trả danh sách chưa lọc, như hành vi gốc.
    """
    _list = []
    if current_assistant and current_assistant.type == 1:
        if out_ds.ds_list:
            for _ds in out_ds.ds_list:
                ds = out_ds.get_ds(_ds.id)
                table_schema, tables = out_ds.get_db_schema(_ds.id, question, embedding=False)
                ds_info = f"{ds.name}, {ds.description}\n"
                ds_schema = ds_info + table_schema
                _list.append({"id": ds.id, "ds_schema": ds_schema, "cosine_similarity": 0.0, "ds": ds})

        if _list:
            try:
                text = [s.get('ds_schema') for s in _list]

                model = EmbeddingModelCache.get_model()
                results = model.embed_documents(text)

                q_embedding = model.embed_query(question)
                for index in range(len(results)):
                    item = results[index]
                    _list[index]['cosine_similarity'] = cosine_similarity(q_embedding, item)

                _list.sort(key=lambda x: x['cosine_similarity'], reverse=True)
                # print(len(_list))
                _list = _list[:settings.DS_EMBEDDING_COUNT]
                SQLBotLogUtil.info(
                    f'Ranked {len(_list)} datasources by embedding similarity, best {[ele.get("id") for ele in _list[:5]]}',
                    extra={'datasources': len(_list), 'best_ds_ids': [ele.get('id') for ele in _list[:5]]},
                )
                return [{"id": obj.get('ds').id, "name": obj.get('ds').name, "description": obj.get('ds').description}
                        for obj in _list]
            except Exception:
                SQLBotLogUtil.exception("Unexpected error in get ds embedding", extra={"function": "get_ds_embedding"})
    else:
        for _ds in _ds_list:
            if _ds.get('id'):
                ds = session.get(CoreDatasource, _ds.get('id'))
                # table_schema = get_table_schema(session, current_user, ds, question, embedding=False)
                # ds_info = f"{ds.name}, {ds.description}\n"
                # ds_schema = ds_info + table_schema
                _list.append({"id": ds.id, "cosine_similarity": 0.0, "ds": ds, "embedding": ds.embedding})

        if _list:
            try:
                # text = [s.get('ds_schema') for s in _list]

                start_time = time.time()
                # Datasource có thể dùng model embedding khác nhau: xếp hạng theo nhóm model.
                _list = rank_ds_by_embedding(_list, question)
                # print(len(_list))
                end_time = time.time()
                SQLBotLogUtil.info(
                    f'Ranked the datasources by embedding in {end_time - start_time:.2f} seconds',
                    extra={'duration_s': round(end_time - start_time, 2)},
                )
                _list = _list[:settings.DS_EMBEDDING_COUNT]
                SQLBotLogUtil.info(
                    f'Kept {len(_list)} datasources after ranking, best {[ele.get("id") for ele in _list[:5]]}',
                    extra={'datasources': len(_list), 'best_ds_ids': [ele.get('id') for ele in _list[:5]]},
                )
                return [{"id": obj.get('ds').id, "name": obj.get('ds').name, "description": obj.get('ds').description}
                        for obj in _list]
            except Exception:
                SQLBotLogUtil.exception("Unexpected error in get ds embedding", extra={"function": "get_ds_embedding"})
    return _list
