# Concept & Pipeline Text2SQL của SQLBot

> Tài liệu mô tả chi tiết bài toán Text-to-SQL (ChatBI) mà project này xây dựng: từ cách
> người dùng chuẩn bị metadata (bảng, thuật ngữ, ví dụ SQL), cách hệ thống embedding chúng,
> đến toàn bộ pipeline xử lý một câu hỏi của user — mỗi bước gọi LLM/embedding nào, prompt ra
> sao, output là gì và rẽ nhánh thế nào.
>
> Mục tiêu: đọc xong tài liệu này, một kỹ sư có thể dựng lại ~80% hệ thống, chỉ khác ở nội
> dung prompt cụ thể.

---

## 1. Bài toán & triết lý thiết kế

SQLBot là một hệ **ChatBI**: user gõ câu hỏi bằng ngôn ngữ tự nhiên ("doanh thu theo tháng
năm nay"), hệ thống tự sinh SQL, **thực thi thật** trên database của user, rồi trả về **dữ
liệu + biểu đồ**.

Điểm cốt lõi về mặt thiết kế:

1. **Không phải "chat" tự do.** Mỗi lượt hỏi là một tác vụ: NL → SQL → execute → chart. LLM
   bị ép trả lời **JSON có cấu trúc**, không trả lời văn xuôi. Mọi câu hỏi không sinh được SQL
   (hỏi thời tiết, hỏi metadata, yêu cầu sửa/xóa DB) đều bị từ chối **theo thiết kế** bằng
   `{"success":false,"message":"..."}`.

2. **LLM không bao giờ chạm DB.** LLM chỉ nhận **schema dạng văn bản** (M-Schema) + vài dòng
   sample data trong prompt, rồi sinh SQL. Việc kết nối và thực thi do backend làm.

3. **Không tin SQL của LLM về mặt bảo mật.** Sau khi LLM sinh SQL, backend **tự parse bằng
   sqlglot** để lấy danh sách bảng thật và đối chiếu với whitelist; không tin mảng `tables` mà
   LLM tự khai.

4. **RAG để thu hẹp context.** DB thật có thể có hàng trăm bảng — không thể nhồi hết vào
   prompt. Hệ dùng **embedding + cosine similarity** để chọn ra Top-K bảng / thuật ngữ / ví dụ
   SQL liên quan nhất tới câu hỏi, rồi chỉ đưa những thứ đó vào prompt.

### Stack
- Backend: **FastAPI / Python 3.11**, SQLModel/SQLAlchemy.
- Metadata store + vector store: **PostgreSQL + pgvector** (extension cho cột `embedding`).
- LLM: bất kỳ model OpenAI-compatible nào (qua LangChain `BaseChatModel`), gọi **streaming**.
- Embedding: model embedding riêng (qua `EmbeddingModelCache`).
- Frontend: Vue3 + TS, nhận **SSE** (Server-Sent Events).

### Hai loại model AI — phân biệt rõ
| | **Embedding model** | **Chat LLM** |
|---|---|---|
| Dùng để | Truy hồi (RAG): chọn bảng, thuật ngữ, ví dụ SQL, datasource | Sinh SQL, sinh chart, chọn DS, lọc quyền, dynamic SQL, phân tích, dự đoán |
| Khi nào | Lúc lưu metadata (tạo vector) + lúc user hỏi (so khớp) | Trong pipeline trả lời |
| Output | vector `float[]` | text (JSON) streaming |

---

## 2. Mô hình dữ liệu metadata (user chuẩn bị gì)

Trước khi hỏi, user (hoặc admin workspace) cấu hình 4 nhóm metadata. Tất cả đều gắn `oid`
(organization/workspace id) để cô lập theo workspace.

### 2.1. Datasource (`core_datasource`)
Kết nối tới DB thật của user (MySQL, PostgreSQL, SQL Server, Oracle, ClickHouse, Doris, ES,
Excel→PG, …). Mỗi datasource có: `name`, `description`, `type` (engine), thông tin kết nối, và
một cột `embedding` (vector của `name + description`, để chọn DS bằng RAG khi user có nhiều DS).

### 2.2. Bảng & trường (`core_table`, `core_field`)
Khi user kết nối DS, hệ thống đồng bộ danh sách bảng/cột. User có thể bổ sung **chú thích**:
- `core_table.custom_comment`: mô tả bảng (vd "Bảng đơn hàng").
- `core_field.custom_comment`: mô tả cột (vd "trạng thái: 1=mới, 2=đã giao").
- `core_table.embedding`: **vector của chuỗi M-Schema của bảng đó** (xem §3.1).
- `core_datasource.table_relation`: quan hệ khóa ngoại (đồ thị node/edge) do user vẽ trên UI.

Chú thích rất quan trọng: nó đi thẳng vào M-Schema và là tín hiệu chính để LLM hiểu nghĩa
nghiệp vụ của cột.

### 2.3. Thuật ngữ (`terminology`)
Từ điển nghiệp vụ, dạng cây cha-con:
- 1 bản ghi **cha** (`pid = NULL`): `word` (từ chính), `description` (giải thích / công thức /
  điều kiện truy vấn).
- N bản ghi **con** (`pid = id_cha`): các từ đồng nghĩa (`word`).
- Phạm vi: `specific_ds` + `datasource_ids[]` (áp dụng cho DS nào), hoặc `advanced_application`
  (gắn với một assistant). `enabled` bật/tắt.
- Cột `embedding`: **vector của từng `word`** (cả cha lẫn con đều được embed riêng).

Ví dụ: cha `word="GDP"`, con `word="Tổng sản phẩm quốc nội"`, `description="giá trị toàn bộ
sản phẩm... trong một năm"`. Khi user hỏi "GDP Việt Nam", LLM hiểu GDP nghĩa là gì và "VN" =
"Việt Nam".

> **Embedding thuật ngữ chỉ embed `word`, KHÔNG embed `description`.** Xác nhận trong
> `save_embeddings()`: `_words_list = [item.word for item in _list]` — chỉ vector hóa từ khóa.
> Lý do: `description` là phần *giải nghĩa* (có thể dài, là công thức/điều kiện), không phải thứ
> để so khớp với câu hỏi; vector hóa nó sẽ làm nhiễu. Cơ chế đúng là: **match bằng `word`**
> (đồng nghĩa cũng là `word` của bản ghi con), rồi **sau khi khớp mới đính kèm `description`**
> vào prompt để LLM đọc nghĩa. Tức embedding lo việc "câu hỏi có nhắc tới thuật ngữ này không",
> còn `description` lo việc "thuật ngữ này nghĩa là gì" — hai vai trò tách bạch.

### 2.4. Ví dụ SQL / Data training (`data_training`)
Cặp **câu hỏi mẫu → SQL/giải thích mẫu** (few-shot). Mỗi bản ghi: `question`, `description`
(SQL mẫu hoặc lời giải), gắn `datasource` hoặc `advanced_application`, `enabled`. Cột
`embedding`: **vector của `question`**.

Dùng để "dạy" LLM cách viết SQL đúng phong cách/nghiệp vụ cho các câu hỏi hay gặp.

> **Custom prompt** (bản xpack/licensed): các đoạn hướng dẫn bổ sung, phân loại theo
> `CustomPromptTypeEnum` (GENERATE_SQL / ANALYSIS / PREDICT_DATA). Cũng được truy hồi và chèn
> vào prompt tương tự.

---

## 3. Embedding metadata hoạt động thế nào

Có **hai kiểu lưu vector** trong project, cần phân biệt:

| Đối tượng | Lưu vector ở | Truy hồi bằng | Hàm so khớp |
|---|---|---|---|
| **Bảng** (`core_table.embedding`) | cột text/JSON | cosine tính **trong Python** | `cosine_similarity()` |
| **Datasource** (`core_datasource.embedding`) | cột text/JSON | cosine **trong Python** | `cosine_similarity()` |
| **Thuật ngữ** (`terminology.embedding`) | cột **pgvector** | toán tử `<=>` **trong SQL** | pgvector |
| **Ví dụ SQL** (`data_training.embedding`) | cột **pgvector** | toán tử `<=>` **trong SQL** | pgvector |

Sự khác biệt: bảng/DS thường ít (vài chục) nên load hết về Python rồi tính cosine; thuật ngữ/ví
dụ có thể nhiều nên đẩy phép tính xuống pgvector với `ORDER BY ... LIMIT`.

### 3.1. Embedding bảng

**Lúc tạo/đồng bộ:** với mỗi bảng, hệ build chuỗi **M-Schema** của bảng (xem §4) rồi gọi
`embedding_model.embed_documents([schema_table])`, lưu vào `core_table.embedding`.

**Lúc user hỏi** — `calc_table_embedding(tables, question)`:
```python
q_embedding = model.embed_query(question)          # vector câu hỏi
for t in tables:
    t.cosine = cosine_similarity(q_embedding, json.loads(t.embedding))
tables.sort(by=cosine, desc=True)
tables = tables[:settings.TABLE_EMBEDDING_COUNT]   # giữ Top-K bảng
```
Tức: so khớp **câu hỏi ↔ M-Schema của từng bảng**, giữ lại Top-K bảng giống nhất. Bật/tắt bằng
`settings.TABLE_EMBEDDING_ENABLED`; nếu tắt → đưa **toàn bộ** bảng vào prompt.

> **Bổ sung quan hệ:** sau khi chọn Top-K bảng bằng embedding, hệ còn duyệt
> `ds.table_relation` (đồ thị FK). Nếu một quan hệ nối tới bảng đã chọn nhưng bảng kia chưa
> được chọn, nó **kéo bảng thiếu vào** (để JOIN không bị hụt bảng), rồi append khối
> `【Foreign keys】 a.x=b.y` vào schema. → đảm bảo JOIN khả thi dù embedding bỏ sót.

### 3.2. Embedding datasource — `get_ds_embedding()`
Chỉ chạy khi user **chưa chọn DS** và workspace có **nhiều DS**. So khớp câu hỏi ↔ vector của
mỗi DS (name+description), giữ Top-K (`DS_EMBEDDING_COUNT`) để đưa cho LLM chọn (xem §6, Pha 2).

### 3.3. Embedding thuật ngữ — `select_terminology_by_word()`
Truy hồi **lai (hybrid)**, gộp 2 nguồn kết quả:
1. **Substring match (SQL ILIKE):** `:question ILIKE '%' || word || '%'` — câu hỏi có chứa
   nguyên văn từ thuật ngữ thì lấy. (Bắt khớp chính xác, không cần embedding.)
2. **Vector match (pgvector):** nếu `EMBEDDING_ENABLED`:
   ```sql
   ( 1 - (embedding <=> :q_embedding) ) AS similarity
   WHERE similarity > EMBEDDING_TERMINOLOGY_SIMILARITY AND oid = :oid AND enabled = true
   ORDER BY similarity DESC LIMIT EMBEDDING_TERMINOLOGY_TOP_COUNT
   ```
   (lọc thêm theo phạm vi: `specific_ds=false` hoặc `datasource_ids @> [ds]`, hoặc theo
   `advanced_application`).

Hai nguồn được khử trùng (theo id cha), rồi với mỗi thuật ngữ khớp, gom **tất cả từ đồng nghĩa
+ description** → render thành XML `<terminologies>` để nhúng prompt.

### 3.4. Embedding ví dụ SQL — `select_training_by_question()`
Tương tự thuật ngữ: hybrid ILIKE (2 chiều) + pgvector trên `data_training.question`, ngưỡng
`EMBEDDING_DATA_TRAINING_SIMILARITY`, lấy `EMBEDDING_DATA_TRAINING_TOP_COUNT`. Mỗi ví dụ khớp →
lấy `question` + `description` để nhúng.

> Lưu ý: thuật ngữ/ví dụ luôn chạy ILIKE; embedding chỉ **bổ sung** thêm các khớp ngữ nghĩa.
> Vì vậy ngay cả khi `EMBEDDING_ENABLED=false`, hệ vẫn truy hồi được bằng substring.

---

## 4. Định dạng M-Schema (cách "kể" cấu trúc bảng cho LLM)

> **M-Schema là tiêu chuẩn gì?** Đây **không** phải tiêu chuẩn thế giới (ISO/W3C). M-Schema là
> định dạng schema bán cấu trúc xuất phát từ dự án **XiYan-SQL của Alibaba**, được cộng đồng
> Text2SQL dùng vì nó nén được nhiều tín hiệu (kiểu cột, chú thích, ví dụ giá trị, khóa ngoại)
> vào ít token. SQLBot **tự cài đặt lại** định dạng này trong `get_table_schema()` — không
> import thư viện chuẩn nào. Bạn hoàn toàn có thể đổi sang định dạng khác (DDL thuần, JSON…),
> miễn LLM hiểu; M-Schema chỉ là một lựa chọn tiết kiệm token và giàu ngữ nghĩa.

Toàn bộ schema được tuần tự hóa thành text theo định dạng **M-Schema** — `get_table_schema()`:

```
【DB_ID】 my_database, Mô tả database
【Schema】
# Table: my_database.orders, Bảng đơn hàng
[
(id:bigint, Primary key, ID),
(user_id:bigint, ID người dùng),
(amount:decimal, Số tiền),
(status:varchar, Trạng thái, examples:['mới','đã giao']),
(created_at:datetime, Thời gian tạo),
]
# Table: my_database.users, Người dùng
[
(id:bigint, Primary key, ID),
(name:varchar, Họ tên),
]
【Foreign keys】
orders.user_id=users.id
```

Quy tắc:
- Với engine không có schema namespace (`mysql, es, sqlite, hive, doris, starrocks`) thì bỏ
  tiền tố `db_name.`.
- Mỗi cột = `(field_name:field_type[, custom_comment])`. `custom_comment` chính là chú thích
  user nhập.
- Khối `【Foreign keys】` sinh từ `ds.table_relation`.

Hàm trả về `(schema_str, table_name_list)`. **`table_name_list` chính là whitelist bảng** dùng
cho security check ở §6 Pha 4.

Ngoài ra `get_tables_sample_data()` lấy **3 dòng mẫu** mỗi bảng, nhúng vào `<sample-data>` để
LLM hiểu phân bố dữ liệu thực.

---

## 5. Cách prompt được lắp ráp (multi-turn priming)

Prompt **không** phải một system message khổng lồ. Hệ mô phỏng một đoạn hội thoại nhiều lượt đã
"mồi" sẵn (priming), để LLM "đồng ý tuân thủ" từng phần trước khi nhận câu hỏi thật.

Template gốc nằm trong `backend/templates/template.yaml` (+ `templates/sql_examples/*.yaml` cho
ví dụ theo từng engine), lắp bằng Python `str.format()`. Trong YAML, các `{}` của JSON literal
được escape thành `{{ }}`; các placeholder thật: `{sqlbot_name} {engine} {schema} {sample_data}
{terminologies} {data_training} {custom_prompt} {question} {current_time} {change_title} {sql}
{filter} {sub_query} {chart_type} {articles_number}` …

`init_messages()` dựng `sql_message` (cho lần gọi sinh SQL) theo thứ tự:

```
[System]  vai trò + nhiệm vụ + mô tả các khối <Info>, quy trình kiểm tra        (sql.system)
[Human]   toàn bộ <Rules> + ví dụ few-shot cơ bản theo engine                    (sql.generate_rules)
[AI]      "Tôi đã nắm rõ tất cả quy tắc... sẽ tuân thủ nghiêm ngặt."             (ack cứng)
[Human]   <Info> chứa <db-engine> + <m-schema> + <sample-data>                   (sql.generate_basic_info)
[AI]      "Tôi đã xác nhận schema... SQL sẽ không vượt phạm vi."                  (ack cứng)
[Human]   (tùy chọn) custom_prompt    → [AI] "đã xác nhận thông tin bổ sung"
[Human]   (tùy chọn) <terminologies>  → [AI] "đã xác nhận thuật ngữ"
[Human]   (tùy chọn) <sql-examples>   → [AI] "đã xác nhận ví dụ SQL"
... (lịch sử N lượt hội thoại trước, lọc theo context_record_count) ...
[Human]   câu hỏi hiện tại + <current-time> + <change-title>                     (sql.user)  ← append ở generate_sql()
```

Các message ack ("Tôi đã nắm rõ...") là **chuỗi cứng tiếng Việt** trong code (`init_messages`),
không phải từ LLM — chúng tạo ngữ cảnh "LLM đã cam kết tuân thủ".

> **Thuật ngữ được đưa cho LLM ở đâu, và giúp gì?** Nó được chèn vào **lượt sinh SQL** (Pha 3),
> dưới dạng một khối `<terminologies>` đặt **trước câu hỏi thật** trong chuỗi priming (dòng
> `if _system_templates.get('terminologies')` ở `init_messages()`), kèm một AI-ack "Tôi đã xác
> nhận thông tin thuật ngữ". Tức là LLM "đọc từ điển nghiệp vụ" *trước khi* đọc câu hỏi. Tác
> dụng cụ thể với việc sinh SQL:
> - **Khử nhập nhằng từ vựng:** "doanh thu" → công thức/cột nào (`SUM(amount)` hay
>   `SUM(price*qty)`), "khách VIP" → điều kiện `tier='vip'`. `description` của thuật ngữ thường
>   chính là **mệnh đề/điều kiện SQL** để LLM dán vào.
> - **Đồng nghĩa & viết tắt:** câu hỏi dùng "VN"/"GDP" nhưng dữ liệu lưu "Việt Nam"/"Tổng sản
>   phẩm quốc nội" → ánh xạ đúng giá trị filter.
> - **Tri thức ngoài schema:** những quy ước nghiệp vụ mà tên cột không nói lên được.
>
> Lưu ý: thuật ngữ **không bắt buộc** — chỉ những thuật ngữ *khớp* câu hỏi (qua ILIKE/embedding
> ở Pha 0) mới được chèn, nên prompt không phình to vô ích.

`chart_message` dựng tương tự nhưng ngắn hơn: `[System chart.system][Human chart.generate_rules]
[AI ack]`, rồi ở Pha 6 append `chart.user`.

Lịch sử hội thoại: `get_last_conversation_rounds()` giữ N lượt gần nhất (cấu hình
`chat.context_record_count`), loại các message đánh dấu `sqlbot_system=true`.

**Về ngôn ngữ:** prompt yêu cầu LLM **tự nhận diện ngôn ngữ trong `<user-question>` và trả lời
bằng đúng ngôn ngữ đó**; reasoning có thể bằng tiếng Anh nhưng đáp án cuối phải cùng ngôn ngữ
câu hỏi. Định danh trong SQL (tên bảng/cột) **giữ nguyên xi theo M-Schema**, không dịch.

---

## 6. Pipeline xử lý một câu hỏi (đầy đủ)

Entry point: `chat.py → question_answer_inner() → stream_sql()` → `LLMService.create()` →
`init_record()` → `run_task_async()`. Toàn bộ chạy trong thread, kết quả đẩy ra UI qua **SSE**
(`process_stream()` tách `content` và `reasoning_content`).

Mức độ hoàn thành điều khiển bởi `ChatFinishStep`:
- `GENERATE_SQL` — chỉ sinh SQL rồi dừng (không chạy).
- `QUERY_DATA` — sinh SQL + thực thi + trả data (không vẽ chart).
- `GENERATE_CHART` — full (mặc định trong chat).

Sau đây là từng pha trong `run_task()`.

### Pha 0 — Chuẩn bị (RAG, không gọi LLM)
Nếu đã biết DS: chạy song song việc truy hồi:
- `filter_terminology_template()` → `chat_question.terminologies` (XML thuật ngữ khớp).
- `filter_training_template()` → `chat_question.data_training` (ví dụ SQL khớp).
- `filter_custom_prompts(GENERATE_SQL)` → `chat_question.custom_prompt`.
- `init_messages()` → trong đó `choose_table_schema()` chọn bảng bằng **table embedding**
  (§3.1) và build M-Schema + sample data, đồng thời set `table_name_list` (whitelist).

→ Lắp xong `sql_message` như §5.

### Pha 1 — (tùy chọn) Chọn Datasource — **LLM call #0**
Chỉ khi `self.ds is None` (user/assistant chưa cố định DS). `select_datasource()`:
- Lấy danh sách DS của workspace (id/name/description).
- Nếu nhiều DS và bật embedding → `get_ds_embedding()` rút Top-K (§3.2).
- **LLM** (prompt `datasource`): nhận mảng JSON DS, trả `{"id": <id>}` hoặc
  `{"fail":"..."}`.
- Nếu chỉ có **1 DS** → bỏ qua LLM, chọn luôn.

Output: cố định `self.ds`, rồi chạy lại Pha 0 (terminology/training/custom + init_messages) cho
đúng DS.

### Pha 2 — Kiểm tra kết nối
`check_connection(ds)`. Hỏng → `SQLBotDBConnectionError`.

### Pha 3 — Sinh SQL — **LLM call #1 (chính)**
`generate_sql()`: append câu hỏi (`sql.user`, kèm `current_time`, `change_title`) vào
`sql_message`, gọi `llm.stream()`. LLM trả **JSON**:
```json
{"success":true,
 "sql":"SELECT ... LIMIT 1000",
 "tables":["orders","users"],
 "chart-type":"line",
 "brief":"Doanh thu theo tháng"}
```
hoặc `{"success":false,"message":"lý do từ chối"}`.

Từ đó trích: `chart_type` (gợi ý loại biểu đồ), `brief` (đặt tiêu đề hội thoại nếu
`change_title`). `check_sql()` parse JSON, nếu `success=false` → ném lỗi (đây là cơ chế **từ
chối theo thiết kế**).

### Pha 4 — Security check (KHÔNG tin LLM)
```python
actual_tables = extract_tables_from_sql(sql, ds_type)   # sqlglot parse THẬT
allowed_tables = set(self.table_name_list)               # whitelist từ M-Schema
unauthorized = actual_tables - allowed_tables
if unauthorized: raise SingleMessageError(...)           # chặn truy cập bảng ngoài phạm vi
```
Không dùng mảng `tables` do LLM khai; parse cây cú pháp để lấy bảng thật. Nếu parse ra rỗng →
cũng chặn (nghi ngờ cú pháp lạ/injection).

> **"Whitelist từ M-Schema" nghĩa là gì?** `self.table_name_list` được set ngay ở Pha 0 chính
> là **danh sách bảng đã được serialize vào M-Schema** đưa cho LLM — tức Top-K bảng do embedding
> chọn **cộng** các bảng bị kéo thêm vì khóa ngoại (§3.1). Nói cách khác: *bảng nào LLM được
> "nhìn thấy" thì mới được phép dùng*. Logic: `actual_tables` (sqlglot parse từ SQL thật) phải
> là **tập con** của whitelist này; nếu SQL đụng tới một bảng **không** nằm trong M-Schema (LLM
> bịa tên bảng, hoặc cố truy cập bảng nhạy cảm ngoài phạm vi) → `unauthorized` khác rỗng → chặn.
> Đây là tầng phòng thủ độc lập với prompt: kể cả LLM bị "jailbreak" sinh SQL đụng bảng lạ,
> backend vẫn chặn vì đối chiếu bằng AST chứ không tin lời LLM.

### Pha 5 — (tùy chọn) Hậu xử lý SQL — **LLM call #2a hoặc #2b**
Rẽ nhánh **loại trừ nhau** theo loại ngữ cảnh (`run_task` ~ dòng 1297–1342):

```python
use_dynamic_ds  = assistant and assistant.type in [1, 3]     # assistant "dynamic datasource"
is_page_embedded = assistant and assistant.type == 4         # widget nhúng trang

if ((no assistant or is_page_embedded) and is_normal_user) or use_dynamic_ds:
    if use_dynamic_ds:  → generate_assistant_dynamic_sql()   # prompt dynamic_sql
    else:               → generate_filter()                  # prompt permissions (row-level)
else:
    dùng thẳng SQL gốc                                       # vd admin, không có rule
```

**(2a) `permissions` — lọc quyền theo hàng (row-level security).**
`generate_filter()` lấy filter từ `get_row_permission_filters(user, ds, tables)`; **nếu không
có rule → trả None, bỏ qua**. Nếu có → **LLM** (prompt `permissions`) nhận `<sql>` +
`<filter-list>` dạng `[{"table","filter"}]`, chèn điều kiện `WHERE` vào SQL, trả
`{"success":true,"sql":"SQL mới"}`. Mục đích: đảm bảo user chỉ thấy dữ liệu được phép.

> **"Lọc quyền theo hàng" (row-level) là gì?** Khác với quyền theo *bảng/cột* (được/không được
> thấy cả bảng), row-level giới hạn **những dòng nào** trong cùng một bảng user được xem. Vd
> nhân viên chi nhánh Hà Nội chỉ thấy `WHERE branch='HN'`. Cấu hình bằng `DsPermission(type=
> 'row')` + `DsRules` (ánh xạ `permission_list`/`user_list` → điều kiện), rồi `transFilterTree`
> dịch cây điều kiện thành mệnh đề `WHERE`. LLM ở đây chỉ làm việc cơ học: **ghép** mệnh đề filter
> vào đúng bảng trong SQL (dùng LLM thay vì nối chuỗi để xử lý đúng alias/subquery/JOIN phức tạp).
>
> **Admin (id=1) bỏ qua mọi filter.** Điều kiện vào nhánh này có `is_normal_user`, mà
> `is_normal_user() = (current_user.id != 1)`. User id=1 (admin gốc) **không** bị áp row-level —
> xem được toàn bộ dữ liệu. Đây là lý do nhánh "dùng thẳng SQL gốc" tồn tại.

**(2b) `dynamic_sql` — thay bảng ảo bằng truy vấn con (federation/virtual table).**
Dùng cho assistant kiểu *dynamic datasource* (type 1/3), nơi mỗi "bảng" thực ra là một
**subquery** (`table.sql`). Cơ chế 2 bước tránh để LLM chạm subquery thật:
```
B1 (LLM, prompt dynamic_sql): SQL gốc + [{"table":"orders","query":"select * from sqlbot_dynamic_temp_table_orders"}]
   → LLM thay tên bảng bằng PLACEHOLDER, giữ nguyên cấu trúc:
     "... FROM (select * from sqlbot_dynamic_temp_table_orders) orders"
B2 (CODE, run_task): thay placeholder bằng subquery THẬT (table.sql):
     "... FROM (SELECT ... FROM real WHERE tenant=42) orders"
```
`dynamic_subsql_prefix = 'select * from sqlbot_dynamic_temp_table_'`.

> Tóm tắt: `permissions` = bảo mật hàng; `dynamic_sql` = dịch bảng-ảo→SQL-thật. Hai nhánh không
> bao giờ cùng chạy trong một lượt.

`real_execute_sql` = SQL sau hậu xử lý (hoặc SQL gốc nếu không qua nhánh nào).

→ Nếu `finish_step == GENERATE_SQL`: dừng tại đây, trả SQL, không chạy.

### Pha 6 — Thực thi SQL (chạy thật)
`execute_sql(real_execute_sql)` → `exec_sql(ds, sql)` kết nối DB thật, lấy `{fields, data}`.
Chuẩn hóa số lớn, cắt còn `limit=1000` dòng nếu bật `enable_sql_row_limit`, lưu
`save_sql_exec_data`. UI nhận `type:'sql-data'`.

→ Nếu `finish_step == QUERY_DATA`: dừng, trả bảng dữ liệu (markdown/JSON), không vẽ chart.

### Pha 7 — Sinh cấu hình biểu đồ — **LLM call #3**
`generate_chart()`: trước hết build lại M-Schema **chỉ của các bảng đã dùng** (`tables`,
`embedding=False`). Append `chart.user` (kèm `<sql>`, `<m-schema>`, `<chart-type>`) vào
`chart_message`, gọi LLM. Output JSON cấu hình chart, ví dụ:
```json
{"type":"line","title":"Doanh thu theo tháng",
 "axis":{"x":{"name":"Tháng","value":"month"},
         "y":[{"name":"Doanh thu","value":"revenue"}]}}
```
Các loại: `table / column / bar / line / pie`; có quy tắc series vs multi-quota (loại trừ
nhau), pie chỉ 1 chỉ số + 1 phân loại, v.v. Nếu không dựng được → `{"type":"error","reason":...}`.

`check_save_chart()` chuẩn hóa (lowercase `value`), lưu. UI nhận `type:'chart'`. Ở chế độ
MCP/non-chat còn gọi `request_picture()` để render ảnh PNG.

#### 7.1. "Build lại M-Schema chỉ của các bảng đã dùng" nghĩa là gì
M-Schema ở Pha 7 **khác** M-Schema ở Pha 0:

| | Pha 0 (sinh SQL) | Pha 7 (sinh chart) |
|---|---|---|
| Tham số | `embedding=True`, không lọc bảng | `embedding=False`, `table_list=tables` |
| Phạm vi | Top-K bảng do **embedding đoán** (rộng, có thể thừa) | **đúng các bảng SQL cuối dùng** |

Ở Pha 7, `get_table_schema(..., embedding=False, table_list=tables)`:
- `table_list=tables`: chỉ serialize các bảng nằm trong danh sách (dòng
  `if table_list is not None and obj.table.table_name not in table_list: continue`). `tables`
  ở đây là mảng bảng LLM khai ở Pha 3.
- `embedding=False`: không chạy `calc_table_embedding` cắt Top-K — đã biết chính xác bảng nào
  dùng rồi, khỏi đoán.

**Vì sao:** Pha 3 nhồi Top-K bảng (vd 15) để LLM đủ lựa chọn sinh SQL, nhưng SQL cuối có thể chỉ
dùng 2 bảng. Đưa cả 15 cho bước chart là thừa và gây nhiễu → thu hẹp còn đúng 2 bảng. Mục đích
chính: cung cấp `custom_comment` của từng cột để LLM đặt **nhãn hiển thị đẹp** (SQL trả cột
`revenue`, M-Schema ghi `(revenue:decimal, Doanh thu)` → `"name":"Doanh thu"` cho trục y). Không
có schema này, nhãn biểu đồ chỉ là tên cột kỹ thuật.

> Ở đây dùng `tables` (mảng LLM tự khai) chứ không phải `actual_tables` (sqlglot) là chấp nhận
> được, vì đây không phải bước bảo mật — sai sót cùng lắm làm nhãn chưa tối ưu, không gây rủi ro.

#### 7.2. Cú pháp biểu đồ: do tác giả tự thiết kế (KHÔNG theo chuẩn nào)
JSON cấu hình chart là một **DSL riêng của SQLBot**, đặc tả **hoàn toàn trong prompt** tại
`template.yaml` khóa `chart:` (`system` + `generate_rules` + `user`). Nó **không** theo chuẩn
như Vega-Lite hay option của ECharts/G2. Luồng:

```
LLM sinh JSON-DSL-riêng → frontend đọc JSON → map sang option AntV G2/S2 → vẽ
```

LLM **không** sinh code G2, chỉ sinh cấu hình trung gian; tách LLM khỏi chi tiết thư viện vẽ
(đổi thư viện chỉ cần sửa lớp map, prompt giữ nguyên). Cú pháp từng loại định nghĩa trong
`chart.generate_rules`, mỗi loại có một **khuôn JSON mẫu** kèm few-shot:
- `table`: `{"type":"table","columns":[{"name","value"},...]}`
- `column/bar/line`: `{"type":...,"axis":{"x":{name,value},"y":[{name,value}],"series":{name,value}}}`
- `pie`: `{"type":"pie","axis":{"y":{...},"series":{...}}}`

Trong đó **`value` = tên cột/bí danh trong SQL** (phải khớp từng ký tự), **`name` = nhãn hiển
thị** bằng ngôn ngữ câu hỏi (suy từ chú thích M-Schema). Các quy tắc ràng buộc (cũng trong
prompt): phân loại trường thành **chiều (x) / chỉ số (y) / phân loại (series)**; `series` và
`multi-quota` **loại trừ nhau** (có cột phân loại → `series` 1 chỉ số; không có nhưng nhiều cột
số → `multi-quota`); `pie` đúng 1 series + 1 y; `bar` = `column` xoay trục.

#### 7.3. Các bước LLM sinh chart
Input (`chart.user`): `<user-question>`, `<sql>`, `<m-schema>` (rút gọn §7.1), `<chart-type>`
(gợi ý từ Pha 3).
1. Phân tích `<sql>` + `<chart-type>` → xác định bảng kết quả có cột nào.
2. Phân loại từng cột: chiều / chỉ số / phân loại (dựa kiểu dữ liệu + ngữ nghĩa).
3. Áp quy tắc quyết định cấu trúc (series? multi-quota? mấy trục y?).
4. Dùng `<m-schema>` đặt `name` hiển thị; lấy `value` = bí danh cột SQL.
5. Xuất JSON-DSL.

#### 7.4. Hậu xử lý & render
- `check_save_chart()`: parse JSON, **lowercase mọi `value`** (khớp tên cột không phân biệt hoa
  thường), kiểm `type`, lưu DB, đẩy `type:'chart'` ra UI.
- **Frontend** đọc DSL, map sang **AntV G2** (biểu đồ) / **AntV S2** (bảng, pivot) để vẽ trong
  trình duyệt. (Lưu ý: **AntV X6** là thư viện khác, dùng cho graph editor vẽ `table_relation`,
  không liên quan render chart.)
- **Chế độ MCP/non-chat:** `request_picture()` gói `{type, data, axis}` gửi sang **image
  microservice** (`MCP_IMAGE_HOST`) render thành **PNG**, trả URL ảnh (khi cần ảnh tĩnh thay vì
  UI tương tác).

**Tóm lại:** cú pháp chart là **DSL tự chế của tác giả**, đặc tả bằng prompt, không theo chuẩn
quốc tế. LLM đóng vai "phân tích cột SQL → ánh xạ trục/chỉ số/phân loại → xuất JSON cấu hình",
việc vẽ thật do AntV G2/S2 (hoặc image service cho PNG) đảm nhận.

### Kết thúc
`finish_record()`. Mọi lỗi giữa chừng → lưu `save_error` + đẩy `type:'error'` ra UI. Mỗi bước
LLM đều ghi `ChatLog` (messages, reasoning, token_usage) để audit/regenerate.

---

## 7. Số lần gọi LLM mỗi câu hỏi

| Tình huống | LLM calls |
|---|---|
| DS đã cố định, không rule, full chart | **2** (SQL + chart) |
| Chưa chọn DS (nhiều DS) | +1 (chọn DS) |
| User thường có row-permission | +1 (permissions) |
| Assistant dynamic datasource | +1 (dynamic_sql) thay cho permissions |
| `finish_step=QUERY_DATA` | **1** (chỉ SQL; có thể +DS/+filter) |
| `/analysis`, `/predict` (lệnh riêng) | 1 mỗi lệnh, pipeline riêng |

Embedding model được gọi thêm vài lần ở Pha 0/1 (chọn bảng/DS/thuật ngữ/ví dụ) — không tính là
"LLM call".

---

## 8. Danh mục prompt (template.yaml) & vai trò

| Key | Pha | Vai trò | Input chính | Output |
|---|---|---|---|---|
| `sql` | 3 | Sinh SQL + chart-type + brief | schema, terminologies, examples, question | JSON `{success,sql,tables,chart-type,brief}` |
| `chart` | 7 | Sinh cấu hình biểu đồ | sql, m-schema, chart-type | JSON chart config |
| `datasource` | 1 | Chọn DS phù hợp | danh sách DS | JSON `{id}` / `{fail}` |
| `permissions` | 5a | Chèn filter quyền hàng | sql, filter-list | JSON `{success,sql}` |
| `dynamic_sql` | 5b | Thay bảng ảo bằng subquery | sql, sub_query | JSON `{success,sql}` |
| `guess` | phụ | Gợi ý câu hỏi tiếp theo | schema, câu hỏi cũ | JSON mảng câu hỏi |
| `analysis` | `/analysis` | Phân tích dữ liệu | fields, data | văn bản |
| `predict` | `/predict` | Dự đoán xu hướng | fields, data | JSON mảng dữ liệu |
| `terminology`/`data_training` | 0 | Khung XML nhúng thuật ngữ/ví dụ | (đã truy hồi) | text |

`sql_examples/*.yaml`: mỗi engine (MySQL, PostgreSQL, …) có bộ ví dụ + cú pháp dialect riêng,
ghép vào `sql` template để LLM sinh đúng phương ngữ SQL.

---

## 9. Checklist để dựng lại hệ thống (~80%)

1. **DB metadata + pgvector:** bảng `core_datasource, core_table, core_field, terminology,
   data_training`, mỗi cái có cột `embedding`. Bật extension `vector` cho terminology/training.
2. **Đồng bộ schema:** đọc bảng/cột từ DS thật → lưu; cho phép user thêm `custom_comment` và vẽ
   `table_relation`.
3. **Embedding job:** khi tạo/sửa metadata → tính & lưu vector (bảng: vector của M-Schema; DS:
   name+desc; thuật ngữ: từng word; ví dụ: question).
4. **M-Schema serializer:** `get_table_schema()` → chuỗi `【DB_ID】/【Schema】/【Foreign keys】`
   + whitelist bảng.
5. **RAG retrieval:** cosine (Python) cho bảng/DS; ILIKE + pgvector `<=>` cho thuật ngữ/ví dụ;
   Top-K + ngưỡng cấu hình được.
6. **Prompt builder multi-turn:** System→rules→(ack)→schema→(ack)→[terms/examples/custom]→
   history→question; ép output JSON; tự nhận diện ngôn ngữ; giữ nguyên định danh SQL.
7. **Orchestrator** `run_task()`: (chọn DS)→check conn→sinh SQL→**security check sqlglot**→
   (permissions | dynamic_sql)→execute→sinh chart; điều khiển bằng `finish_step`; stream SSE.
8. **Security:** parse SQL bằng sqlglot, đối chiếu whitelist; chặn non-SELECT (rule trong
   prompt + có thể chặn thêm ở backend); row-permission qua `permissions`.
9. **Streaming & logging:** tách reasoning/content; lưu ChatLog mỗi bước để regenerate/audit.

Phần còn lại (20%) chủ yếu là **nội dung prompt cụ thể**, danh sách dialect, xpack (custom
prompt/license/assistant), và chi tiết kết nối từng loại DB.
```
