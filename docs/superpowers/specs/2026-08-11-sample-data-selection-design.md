# Sample data cho pipeline chọn table schema — Thiết kế

> Phạm vi: sửa `get_table_sample_data`/`get_tables_sample_data`/`get_table_schema` trong
> `backend/apps/datasource/crud/datasource.py` và điểm gọi `choose_table_schema()` trong
> `backend/apps/chat/task/llm.py`. **Không** đụng tới việc nối `ai_user_permissions` (AI Sync Hook)
> vào pipeline — đó là gap riêng, đã biết (`docs/AI_ONBOARDING.md:40-42`), làm ở spec khác.

## 1. Vấn đề hiện tại

Đọc code thật tại `backend/apps/datasource/crud/datasource.py` (nhánh trước khi sửa):

1. **`fields[:10]`** (dòng 577, trong `get_table_sample_data`) cắt cứng 10 field đầu theo thứ tự
   `obj.fields` gốc (không sort theo `field_index`, không liên quan gì đến câu hỏi) → field liên
   quan nằm sau cột 10 bị loại hoàn toàn khỏi sample.
2. **Không có field-level semantic matching.** `CoreTable` có cột `embedding` dùng cho
   `calc_table_embedding()` (`table_embedding.py:43`) để chọn bảng, nhưng `CoreField` không có cột
   tương đương — chưa có hạ tầng nào so khớp `question` với `field_name`/`custom_comment`/
   `field_comment`.
3. **Chưa chuẩn hóa tiếng Việt/tên field.** Không có helper bỏ dấu/chuẩn hóa `_`/hoa-thường ở đâu
   trong repo.
4. **Row permission bị bỏ sót trong sample query.** `get_table_sample_data` build SQL trực tiếp
   (`SELECT ... LIMIT 3`, dòng 583-593) và gọi `exec_sql` (dòng 596) — không nối `WHERE` filter.
   Trong khi đó luồng sinh SQL thật gọi `get_row_permission_filters()`
   (`permission.py:15`, dùng trong `generate_filter()` tại `llm.py:1239`) để áp filter theo
   `DsPermission`/`DsRules`. Column permission thì **đã** đúng — `get_tables_sample_data` dùng lại
   `get_table_obj_by_ds()` (dòng 623), hàm này đã lọc field theo `get_column_permission_fields()`
   (`permission.py:58`).
5. **Giới hạn 10 field / 3 row hard-code**, không cấu hình được.
6. **Logic gộp 4 việc trong 1 hàm 53 dòng** (chọn field, build SQL theo dialect, execute, serialize
   JSON) → không tách được để unit test riêng phần chọn field.
7. **`choose_table_schema()` không truyền `question` xuống sample data.** (`llm.py:531-549`) —
   `self.chat_question.question` có sẵn trong scope (dùng ngay phía trên để gọi `get_table_schema`)
   nhưng không truyền vào `get_tables_sample_data`.
8. **`except Exception: pass`** (dòng 615-616) nuốt lỗi hoàn toàn, không log gì — kể cả khi sample
   query lỗi vì cột không tồn tại, mất quyền DB, timeout.
9. **Không có test nào** cho toàn bộ chuỗi hàm này.
10. **LLM sinh sai kiểu dữ liệu** (vd so `text = boolean`) vì schema chỉ hiển thị
    `(field_name:field_type)` (DB physical type, `datasource.py:672-674` trong `get_table_schema`),
    tách biệt hoàn toàn khỏi sample data — không có gì giúp LLM biết field type `text` thực chứa
    literal `"Có"`/`"Không"`.

## 2. Kiến trúc

Tách logic chọn field/normalize/permission thành các hàm thuần trong file mới
`backend/apps/datasource/crud/sample_data.py`:

```
sample_data.py
├── normalize_vi_text(text: str) -> str
├── select_sample_fields(fields: list[CoreField], question: str, max_fields: int) -> list[CoreField]
├── build_row_permission_where(session, current_user, ds, table) -> str | None
├── get_table_sample_data(ds, table_name, fields, question) -> str      # điều phối
└── get_tables_sample_data(session, current_user, ds, table_list, question) -> str
```

`datasource.py` giữ nguyên `get_table_obj_by_ds` (column permission) và import các hàm trên từ
`sample_data.py`.

**Ràng buộc quan trọng phát hiện khi rà lại:** `get_table_schema()` hiện có **3 điểm gọi** trong
`llm.py` (dòng 537, 846, 1840), cả 3 đều unpack `tuple[str, list]` — 2 điểm sau
(`generate_recommend_questions_task`, build schema cho chart) gọi với `embedding=False` và **không**
dùng sample data, giữ nguyên hành vi cũ trước giờ. Đổi signature `get_table_schema()` sang trả thêm
`sample_data` sẽ phá 2 điểm gọi đó, và nếu áp dụng enrichment đồng loạt sẽ khiến sample query chạy
thừa (tốn thời gian, tốn quyền truy vấn) cho các luồng chưa từng cần nó. Vì vậy: **không đổi
signature `get_table_schema()`**. Thay vào đó tách phần chọn bảng ra thành hàm dùng chung, còn phần
build field_list (kèm enrichment) tách riêng — xem mục 4.

## 3. Chọn field liên quan (`select_sample_fields`)

Thay thế `fields[:10]`:

1. Normalize `question` và (`field_name`, `custom_comment`, `field_comment`) của từng field bằng
   `normalize_vi_text()`.
2. Field được đánh dấu **khớp** nếu chuỗi field đã normalize chứa (hoặc bị chứa trong) một token
   của `question` đã normalize.
3. Nếu số field khớp ≤ `SAMPLE_DATA_MAX_FIELDS`: lấy hết field khớp (giữ thứ tự `field_index`), rồi
   điền thêm field không khớp theo `field_index` tăng dần cho đủ `SAMPLE_DATA_MAX_FIELDS`.
4. Nếu số field khớp > `SAMPLE_DATA_MAX_FIELDS`: cắt trong tập field khớp theo `field_index` tăng
   dần (không ranking theo điểm khớp — giữ deterministic, đơn giản).
5. Nếu không field nào khớp: fallback nguyên trạng hành vi cũ — `SAMPLE_DATA_MAX_FIELDS` field đầu
   theo `field_index`.

### `normalize_vi_text`

`unicodedata.normalize('NFD', text)` → loại combining marks (`̀-ͯ`) → `lower()` → thay
`_`/`-` bằng khoảng trắng → `strip()`.

## 4. Gắn sample values vào schema (fix vấn đề #10)

Tách phần **chọn bảng** ra khỏi phần **build chuỗi field_list** thành 2 hàm riêng trong
`datasource.py`, cả hai đều tái sử dụng được:

- **`select_relevant_tables(session, current_user, ds, question, embedding=True, table_list=None) -> list[TableObj]`**
  — chính là logic chọn bảng hiện có trong `get_table_schema()` (table embedding qua
  `calc_table_embedding`, bổ sung bảng theo `table_relation`), tách ra nguyên vẹn, không đổi hành
  vi.
- **`build_schema_string(table_objs, sample_data: dict | None = None) -> tuple[str, list]`** — build
  `field_list` string cho từng bảng. Nếu `sample_data` có giá trị và field đó xuất hiện trong đó:
  lấy tối đa 2-3 giá trị distinct từ sample rows, format
  `(dai_bieu_chuyen_trach:text, vd: 'Có'/'Không')`. Nếu `sample_data=None` hoặc field không có
  trong đó: giữ format cũ `(field_name:field_type, comment)` — **y hệt hành vi hiện tại**.

`get_table_schema()` giữ nguyên signature/hành vi/return type (`tuple[str, list]`) — trở thành hàm
mỏng gọi `select_relevant_tables()` rồi `build_schema_string(table_objs, sample_data=None)`. **3
điểm gọi hiện có trong `llm.py` (dòng 537, 846, 1840) không cần sửa gì.**

`choose_table_schema()` (`llm.py:531`) — điểm gọi **duy nhất** cần enrichment — được viết lại để tự
điều phối 3 bước thay vì gọi `get_table_schema()`:

```python
table_objs = select_relevant_tables(session=_session, current_user=self.current_user, ds=self.ds,
                                     question=self.chat_question.question)
self.chat_question.sample_data = get_tables_sample_data(
    session=_session, current_user=self.current_user, ds=self.ds,
    table_list=[t.table.table_name for t in table_objs],
    question=self.chat_question.question)
self.chat_question.db_schema, tables = build_schema_string(table_objs, sample_data=self.chat_question.sample_data)
```

Sample query chỉ chạy đúng 1 lần cho luồng sinh SQL chính, không ảnh hưởng 2 luồng còn lại
(recommend-questions, chart schema) — giữ đúng hiệu năng hiện tại của các luồng đó.

## 5. Row permission trong sample query

`get_table_sample_data` gọi `build_row_permission_where()` — hàm này tái sử dụng
`get_row_permission_filters()` (`permission.py:15`, đã dùng trong `generate_filter()` ở luồng sinh
SQL thật) để build đúng `WHERE` clause theo `DsPermission`/`DsRules`, nối vào SQL sample trước
`LIMIT`/`TOP`/`ROWNUM`. Column permission giữ nguyên cơ chế cũ qua `get_table_obj_by_ds()` (không
đổi, đã đúng).

**Ngoài phạm vi:** `ai_user_permissions` (permission do AI Sync Hook đồng bộ từ SW) chưa được nối
vào bất kỳ pipeline nào — kể cả luồng sinh SQL thật (`docs/AI_ONBOARDING.md:40-42` xác nhận). Việc
nối nó vào (row permission thật cho cả sinh SQL lẫn sample data) là một spec riêng, lớn hơn, cần
thiết kế cách map `database_table_name` → `CoreTable` và cách dùng `postgres_query`/
`clickhouse_query` đã lưu nhưng chưa parse.

## 6. Config mới

`backend/common/core/config.py` — theo pattern có sẵn (`TABLE_EMBEDDING_COUNT`):

```python
SAMPLE_DATA_MAX_FIELDS: int = 10
SAMPLE_DATA_MAX_ROWS: int = 3
```

## 7. Error handling

`get_table_sample_data` bắt lỗi cụ thể quanh `exec_sql` + serialize, log qua
`SQLBotLogUtil.warning(f"Sample data fetch failed for table {table_name}: {e}")` — không log
data/rows thực tế — rồi trả `""` cho bảng đó. Bảng khác và toàn bộ request không bị ảnh hưởng
(khác với hành vi cũ: `except Exception: pass`, im lặng tuyệt đối).

## 8. Testing

Unit test thuần cho `sample_data.py` (mock `list[CoreField]`, không cần DB thật):

- Field liên quan nằm sau field thứ 10 (theo `field_index` gốc) vẫn được `select_sample_fields`
  chọn khi khớp keyword với question.
- Không field nào khớp keyword → fallback đúng `SAMPLE_DATA_MAX_FIELDS` field đầu theo
  `field_index`.
- `normalize_vi_text` chuẩn hóa đúng: dấu tiếng Việt, `_`/`-`, hoa/thường.
- Giới hạn đúng `SAMPLE_DATA_MAX_FIELDS`/`SAMPLE_DATA_MAX_ROWS` (kể cả case > max field khớp).
- `get_table_sample_data` khi `exec_sql` raise exception → trả `""`, không raise ra ngoài, có gọi
  `SQLBotLogUtil.warning` với table name (assert qua mock/spy), không có data thực tế trong log.
- Field text chứa `"Có"`/`"Không"` không bị hệ thống tự suy diễn thành boolean — `field_type` gốc
  giữ nguyên trong output, chỉ thêm sample values bên cạnh.

`build_row_permission_where`: unit test với `DsPermission`/`DsRules` mock/fixture đơn giản (không
cần DB thật), xác nhận SQL WHERE build đúng theo permission giả lập.

Column permission: không cần test mới — cơ chế cũ (`get_table_obj_by_ds`) không đổi.

## 9. Tài liệu cần cập nhật sau khi code xong (đề xuất, chờ user quyết)

Theo bảng quy ước trong CLAUDE.md:

| Đổi gì | Tài liệu |
|---|---|
| Field/sample trong schema prompt, `choose_table_schema()` | `docs/TEXT2SQL_PIPELINE.md` |
| `SAMPLE_DATA_MAX_FIELDS`/`SAMPLE_DATA_MAX_ROWS` trong `config.py` | `docs/OPERATIONS.md` |
| Bẫy "im lặng nuốt lỗi" đã sửa (không còn `except: pass`) — có thể vẫn đáng ghi lại pattern cũ từng tồn tại | mục "Bẫy đã biết" `docs/OPERATIONS.md` |
