"""Cấu hình model theo workspace và datasource do AI Gateway đẩy sang.

Gateway cấp cho mỗi workspace một bộ model (llm, embedding, rerank) qua LiteLLM. Module này lưu bộ
đó, chép vào datasource lúc tạo, và giải ra model cụ thể lúc chạy. Không có dòng cấu hình nào thì
SQLBot chạy y như bản gốc, dùng model chung trong ``ai_model`` và ``EMBEDDING_API_*``.
"""
