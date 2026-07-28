# Đặc tả API tích hợp SQLBot Chat

Tài liệu dành cho hệ thống UI bên thứ ba cần nhúng chức năng hỏi đáp dữ liệu của SQLBot.

Ví dụ trong tài liệu dùng cơ sở dữ liệu tỉnh Lào Cai, nghiệp vụ Thu/Chi ngân sách.

## Mục lục

**Đọc trước khi lập trình**

| | |
|---|---|
| [§0. SQLBot là gì](#0-sqlbot-là-gì) | Sản phẩm làm gì |
| [§1. Khái niệm nền tảng](#1-khái-niệm-nền-tảng) | Datasource, chat, record |
| [§2. Luồng tích hợp](#2-luồng-tích-hợp) | 5 bước gọi API để tích hợp |
| [§3. Quy ước chung](#3-quy-ước-chung) | Xác thực, định dạng response, bảng mã lỗi |

**Đặc tả từng API**

| | |
|---|---|
| [§4. `POST /login/access-token`](#4-post-loginaccess-token--đăng-nhập) | Đăng nhập lấy JWT |
| [§5. `GET /datasource/list`](#5-get-datasourcelist--danh-sách-nguồn-dữ-liệu) | Liệt kê nguồn dữ liệu |
| [§6. `POST /chat/start`](#6-post-chatstart--tạo-hội-thoại) | Tạo hội thoại |
| [§7. `POST /chat/question`](#7-post-chatquestion--hỏi-sse) | **Hỏi (SSE)** — mục dài nhất |
| [§8. `GET /chat/record/{id}/data`](#8-get-chatrecordrecord_iddata--lấy-số-liệu) | **Lấy số liệu** — bắt buộc |

**Vận dụng**

| | |
|---|---|
| [§9. Ghép thành biểu đồ](#9-ghép-thành-biểu-đồ) | Ráp cấu hình biểu đồ với số liệu |
| [§10. Giới hạn hệ thống](#10-giới-hạn-hệ-thống--cần-biết-trước-khi-thiết-kế-ui) | Đọc **trước khi** thiết kế UI |
| [§11. API tùy chọn](#11-api-tùy-chọn) | Lịch sử hội thoại, export Excel… |
| [Checklist tích hợp](#phụ-lục-checklist-tích-hợp) | Kiểm trước khi bàn giao |

---

## 0. SQLBot là gì

SQLBot nhận **câu hỏi bằng tiếng Việt**, sinh SQL, **tự chạy SQL đó** trên database nghiệp vụ, rồi
trả về bốn thứ trong một lượt hỏi: **câu SQL**, **số liệu**, **cấu hình biểu đồ**, và **câu trả lời
bằng lời** (hai thứ cuối sinh song song).

```
"Tổng số thu ngân sách theo từng nhóm nguồn thu"
   → sinh SQL → chạy SQL → [{"Thu cân đối": 7130.61}, ...]
   → song song: chọn cách vẽ biểu đồ  +  viết câu trả lời bằng lời
```

---

## 1. Khái niệm nền tảng

### 1.1. Datasource — nguồn dữ liệu

Một kết nối tới database nghiệp vụ (PostgreSQL, MySQL, Excel/CSV…) do quản trị viên SQLBot khai báo
sẵn. Ví dụ dùng datasource `DataLaoCai` (`id = 5`).

- **Client không tạo datasource** — chỉ liệt kê ([§5](#5-get-datasourcelist--danh-sách-nguồn-dữ-liệu)) và chọn.
- **Mỗi hội thoại gắn cứng đúng một datasource** lúc tạo, không đổi được sau đó.

### 1.2. Chat (hội thoại) — `chat_id`

Một phiên hỏi đáp. Giữ datasource đã chọn và lịch sử câu hỏi để hiểu ngữ cảnh multi-turn (hỏi "còn
năm ngoái thì sao?" sau một câu hỏi khác vẫn hiểu được). Giữ nguyên `chat_id` khi hỏi cùng mạch; tạo
mới khi đổi chủ đề hoặc đổi datasource.

### 1.3. Record (lượt hỏi) — `record_id`

Một câu hỏi và toàn bộ kết quả của nó: SQL đã sinh, **số liệu thực thi**, cấu hình biểu đồ, câu trả
lời bằng lời. Một `chat_id` chứa nhiều `record_id`. `record_id` cấp ngay từ event đầu của stream, là
chìa khóa lấy số liệu ở [§8](#8-get-chatrecordrecord_iddata--lấy-số-liệu).

### 1.4. Quan hệ giữa ba khái niệm

```
Datasource "DataLaoCai" (id=5) ──┐
                                  │  chọn 1 datasource lúc tạo hội thoại
Chat (id=231) ────────────────────┘
   │
   ├── Record (id=260)  "Tổng số thu ngân sách theo từng nhóm nguồn thu"
   │      ├── câu SQL đã sinh
   │      ├── số liệu  ← lấy qua GET /chat/record/260/data
   │      ├── cấu hình biểu đồ
   │      └── câu trả lời bằng lời
   │
   └── Record (id=264)  "còn năm ngoái thì sao?"
```

---

## 2. Luồng tích hợp

```
1. POST /login/access-token          → đăng nhập để lấy JWT
2. GET  /datasource/list             → lấy danh sách các datasource
3. POST /chat/start                  → chat_id          (bắt đầu một cuộc hội thoại mới)
   ┌─────────────────────────────────────────────────── lặp cho mỗi câu hỏi
4. │ POST /chat/question             → dữ liệu stream theo định dạng SSE
5. │ GET  /chat/record/{id}/data     → số liệu           ← lấy kết quả thực thi câu lệnh SQL
   └───────────────────────────────────────────────────
```

**Quan trọng:** SSE ở bước 4 **không chứa số liệu**, chỉ chứa SQL, cấu hình biểu đồ và câu trả lời
bằng lời. Phải gọi thêm bước 5 mới vẽ được biểu đồ hoặc hiện bảng.

---

## 3. Quy ước chung

### 3.1. Base URL

```
https://<host>/api/v1
```
Môi trường test: http://192.168.9.89/api/v1

### 3.2. Xác thực

Mọi API (trừ `/login/*`) yêu cầu header:

```
X-SQLBOT-TOKEN: Bearer <jwt>
```

- Tên header là **`X-SQLBOT-TOKEN`**, không phải `Authorization`.
- Giá trị bắt buộc có tiền tố **`Bearer `** (B hoa, một dấu cách).
- Không cần truyền `user_id` — server tự lấy danh tính từ JWT.

### 3.3. Định dạng response — **bất đối xứng**

Thành công có vỏ bọc, lỗi thì không:

```jsonc
// HTTP 200
{"code": 0, "data": { ... }, "msg": null}

// HTTP 400 / 401 / 500  → chuỗi JSON trần, KHÔNG có vỏ
"Incorrect account or password!"
```

Luôn kiểm tra `status_code` trước khi bóc `.data`. Ngoại lệ: lỗi validate 422 trả `{"detail": [...]}`.

### 3.4. Bảng mã lỗi

| HTTP | Body | Nguyên nhân |
|---|---|---|
| 400 | `"Incorrect account or password!"` | Sai tài khoản/mật khẩu |
| 401 | `"Authentication invalid【Miss Token[X-SQLBOT-TOKEN]!】"` | Không gửi header |
| 401 | `"Authentication invalid【Token schema error!】"` | Thiếu tiền tố `Bearer ` |
| 401 | `"Authentication invalid【Invalid header padding】"` | JWT sai định dạng |
| 401 | `"Authentication invalid【Signature has expired】"` | JWT hết hạn → đăng nhập lại |
| 422 | `{"detail":[...]}` | Sai kiểu dữ liệu / thiếu field |
| 500 | `"Access denied or resource not found!"` | `chat_id` không tồn tại **hoặc** không thuộc user hiện tại |
| 500 | `"Datasource with id X does not belong to current workspace"` | `datasource_id` không lấy từ `/datasource/list` của chính tài khoản đang đăng nhập |

> Truy cập trái phép trả về **500**, không phải 403 — hành vi hiện tại của hệ thống.

---

## 4. `POST /login/access-token` — Đăng nhập

### ⚠ Body là `form-urlencoded`, **không phải JSON**

Gửi JSON sẽ nhận HTTP 422 với thông báo `Field required`.

**Request**

```http
POST /api/v1/login/access-token
Content-Type: application/x-www-form-urlencoded

username=admin&password=SQLBot%40123456
```

```bash
curl -X POST https://<host>/api/v1/login/access-token \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=admin" \
  --data-urlencode "password=SQLBot@123456"
```

**Response 200**

```json
{
  "code": 0,
  "data": {
    "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
    "token_type": "bearer",
    "platform_info": null
  },
  "msg": null
}
```

Token là JWT (HS256). Client không cần đọc nội dung bên trong — chỉ lưu nguyên văn và gắn vào
header ở mọi request sau đó.

**Hạn token: 8 ngày, không có endpoint refresh.** Client phải bắt lỗi 401 và tự đăng nhập lại.

**Response lỗi**

```
HTTP 400  "Incorrect account or password!"
```

Cùng một thông báo cho cả sai mật khẩu lẫn tài khoản không tồn tại (cố ý, tránh dò tài khoản).

---

## 5. `GET /datasource/list` — Danh sách nguồn dữ liệu

Chỉ trả về datasource mà user hiện tại có quyền.

**Request**

```http
GET /api/v1/datasource/list
X-SQLBOT-TOKEN: Bearer <jwt>
```

**Response 200** (rút gọn field không cần cho UI)

```json
{
  "code": 0,
  "data": [
    {"id": 3, "name": "CustomerOrder", "type": "excel", "type_name": "Excel/CSV", "description": ""},
    {"id": 5, "name": "DataLaoCai",    "type": "pg",    "type_name": "PostgreSQL",
     "description": "Dữ liệu thu ngân sách và chi ngân sách của tỉnh Lào Cai"},
    {"id": 4, "name": "NexusRAG",      "type": "pg",    "type_name": "PostgreSQL",
     "description": "Cơ sở dữ liệu văn bản luật"}
  ],
  "msg": null
}
```

Lấy `id` để dùng ở bước tiếp theo. **Không hardcode** — id khác nhau giữa các môi trường.

---

## 6. `POST /chat/start` — Tạo hội thoại

Phải gọi trước khi hỏi. Datasource gắn cứng vào hội thoại tại đây, các câu hỏi sau không khai lại.

**Request**

```http
POST /api/v1/chat/start
X-SQLBOT-TOKEN: Bearer <jwt>
Content-Type: application/json

{"datasource": 5, "origin": 0}
```

| Field | Kiểu | Bắt buộc | Ghi chú |
|---|---|---|---|
| `datasource` | int | ✔ | Lấy từ `/datasource/list` |
| `origin` | int | | luôn có giá trị = `0` |

**Response 200**

```json
{
  "code": 0,
  "data": {
    "id": 231,
    "create_time": "2026-07-28T06:31:40.348476",
    "create_by": 1,
    "brief": "2026-07-28 06:31:40",
    "chat_type": "chat",
    "datasource": 5,
    "engine_type": "PostgreSQL",
    "ds_type": "pg",
    "datasource_name": "DataLaoCai",
    "datasource_exists": true,
    "recommended_question": null,
    "recommended_generate": false,
    "records": [ ... ]
  },
  "msg": null
}
```

Lấy **`data.id`** làm `chat_id`. `brief` ban đầu là timestamp; sau câu hỏi đầu tiên LLM tự đặt tên
và đẩy về qua SSE event `brief` (§7).

**Lỗi:** nếu user không có quyền trên datasource, request bị chặn ngay tại đây, không phải đến lúc
hỏi mới báo.

---

## 7. `POST /chat/question` — Hỏi (SSE)

Endpoint cốt lõi.

**Request**

```http
POST /api/v1/chat/question
X-SQLBOT-TOKEN: Bearer <jwt>
Content-Type: application/json
Accept: text/event-stream

{"chat_id": 232, "question": "Tổng số thu ngân sách theo từng nhóm nguồn thu"}
```

| Field | Kiểu | Bắt buộc |
|---|---|---|
| `chat_id` | int | ✔ |
| `question` | string | ✔ |

**Response**: `HTTP 200`, `Content-Type: text/event-stream; charset=utf-8`

### 7.1. Định dạng dây

Mỗi event chiếm **đúng 2 dòng**:

```
data:{"type":"id","id":260}      ← dòng 1: tiền tố "data:" + JSON
                                 ← dòng 2: dòng trống (dấu ngắt event)
```

Khác SSE thông thường ở hai điểm: **không có `event:`** (chỉ có `type` trong JSON để switch), và
**không có `[DONE]`** — kết thúc duy nhất là `{"type":"finish"}` (hoặc `type: "error"`).

### 7.2. Hai nhóm event

| | Event **token** | Event **mốc** |
|---|---|---|
| `type` | `sql-result`, `chart-result`, `answer-result`, `datasource-result` | tất cả còn lại |
| Cấu trúc | `{content, reasoning_content, type}` | mỗi type một schema riêng |
| Số lượng | hàng nghìn | đúng 1 |
| Cách xử lý | **cộng dồn** để hiện chữ chạy | **thay thế** state |

Event token là LLM stream từng chữ — UI có thể bỏ qua hoàn toàn nếu không cần hiệu ứng gõ chữ, vì
**kết quả chính thức luôn có lại nguyên vẹn ở event mốc**.

### 7.3. Danh mục event

| `type` | Schema | Ý nghĩa |
|---|---|---|
| `id` | `{type, id}` | **Record ID** — lưu lại ngay, cần cho §8 |
| `question` | `{type, question}` | Câu hỏi đã chuẩn hóa |
| `datasource-result` | `{type, content, reasoning_content}` | LLM đang chọn datasource *(chỉ khi hội thoại chưa gắn sẵn ds)* |
| `datasource` | `{type, id, datasource_name, engine_type}` | Datasource đã chọn *(cùng điều kiện trên)* |
| `sql-result` | `{type, content, reasoning_content}` | Đang sinh SQL, từng token |
| `info` | `{type, msg: "sql generated"}` | Kết thúc pha sinh SQL |
| `brief` | `{type, brief}` | Tiêu đề hội thoại LLM tự đặt. *Chỉ có ở câu hỏi đầu tiên* |
| `sql` | `{type, content}` | **SQL chính thức**, đã qua kiểm duyệt và format |
| `sql-data` | `{type, content: "execute-success"}` | **Tín hiệu: SQL chạy xong.** Không kèm số liệu — xem §8 |
| `chart-result` | `{type, content, reasoning_content}` | Đang sinh cấu hình biểu đồ, từng token |
| `answer-result` | `{type, content, reasoning_content}` | Đang sinh câu trả lời chữ — **xen kẽ với `chart-result`**, xem [§7.5](#75-answer-và-chart-chạy-song-song) |
| `info` | `{type, msg: "answer generated"}` | Kết thúc pha answer |
| `info` | `{type, msg: "answer failed"}` | Pha answer lỗi — sẽ **không có** event `answer`, stream vẫn chạy tiếp |
| `answer` | `{type, content}` | **Câu trả lời bằng lời** — text thuần, dùng thẳng |
| `info` | `{type, msg: "chart generated"}` | Kết thúc pha sinh biểu đồ |
| `chart` | `{type, content}` | **Cấu hình biểu đồ** — `content` là chuỗi chứa JSON, parse 2 lớp |
| `finish` | `{type}` | Kết thúc |
| `error` | `{type, content}` | Lỗi pipeline — xem [§7.7](#77--lỗi-giữa-stream-vẫn-là-http-200) |
| `regenerate_record_id` | `{type, regenerate_record_id}` | Chỉ xuất hiện khi dùng `/regenerate` |

### 7.4. Trình tự thực tế

Capture từ record `270`, câu hỏi *"Top 3 nhóm nguồn thu có số thu lớn nhất"*:

```
   event            token đầu → token cuối    số lượng
─────────────────────────────────────────────────────────────────────────
   id                  0.50s                       1
   question            0.50s                       1
   sql-result          2.00s →  21.83s          2345
   info               21.83s                       1   {"msg":"sql generated"}
   brief              21.83s                       1   {"brief":"Top 3 nhóm nguồn thu"}
   sql                21.83s                       1
   sql-data           21.83s                       1
┌─ chart-result       22.34s →  52.77s          3524     hai luồng token
└─ answer-result      22.34s →  42.31s          2247     ĐAN XEN nhau
   info               42.31s                       1   {"msg":"answer generated"}
   answer             42.31s                       1
   info               52.90s                       1   {"msg":"chart generated"}
   chart              52.90s                       1
   finish             52.90s                       1
```

**Không dựa vào thứ tự cố định** — luôn switch trên `type`. Thay đổi khi:

- Hội thoại chưa gắn sẵn datasource → thêm `datasource-result` + `datasource` sau `question`
- Không phải câu hỏi đầu tiên → không có `brief`
- Pha answer lỗi → không có `answer`, chỉ có `info: answer failed`
- `answer` có thể đến trước hoặc sau `chart` — xem [§7.5](#75-answer-và-chart-chạy-song-song)
- Pipeline lỗi → `error` thay thế toàn bộ phần còn lại

Hai mốc đáng khai thác: gọi §8 ngay tại `sql-data` (giây 21.8) để hiện bảng sớm hơn `finish`
(giây 52.9) tới **31 giây**; và hiện `answer` ngay khi nhận (giây 42.3, sớm hơn `chart` 10.6 giây)
thay vì gom lại chờ `finish`.

### 7.5. `answer` và `chart` chạy song song

Ngay khi SQL chạy xong (`sql-data`), server gọi **đồng thời** hai LLM: một sinh biểu đồ, một viết
câu trả lời. Cả hai đổ chung một stream HTTP, mỗi event luôn ghi trọn vẹn — event không bao giờ bị
chèn dở dang vào nhau, chỉ cần tách theo dòng trống rồi parse như bình thường.

Đảm bảo về thứ tự:

| | |
|---|---|
| ✔ | `chart-result` giữ đúng thứ tự với nhau; `answer-result` cũng vậy |
| ✔ | `answer` luôn đến sau toàn bộ `answer-result`; `chart` luôn đến sau toàn bộ `chart-result` |
| ✔ | `finish` luôn là event cuối cùng |
| ✘ | **`answer` đến trước hay sau `chart` — KHÔNG xác định**, tùy bên nào LLM xong trước |

Xử lý bằng hai buffer độc lập, không viết state machine theo "pha":

```js
const state = { sql: '', chart: '', answer: '', recordId: null };

for (const evt of sse) {
  switch (evt.type) {
    case 'id':            state.recordId = evt.id; break;
    case 'sql-result':    state.sql    += evt.content ?? ''; break;
    case 'chart-result':  state.chart  += evt.content ?? ''; break;   // xen kẽ với answer-result,
    case 'answer-result': state.answer += evt.content ?? ''; break;   // cộng dồn riêng là đủ
    case 'sql-data':      fetchData(state.recordId); break;             // số liệu KHÔNG ở trong stream
    case 'answer':        renderAnswer(evt.content); break;             // text thuần, dùng thẳng
    case 'chart':         renderChart(JSON.parse(evt.content)); break;  // JSON lồng trong chuỗi
    case 'finish':        return;
  }
}
```

Ba điều cần nhớ: **`answer-result` chỉ để hiệu ứng gõ chữ** — nếu không cần thì đọc thẳng event
`answer` (nội dung đầy đủ, đã strip). **`info` không phải mốc phân pha** — phân biệt bằng `msg`.
**`answer` có thể vắng mặt hoàn toàn** khi pha answer lỗi (`info: answer failed`, không có
`error`) — SQL và biểu đồ vẫn dùng được, nên **UI bắt buộc render được kết quả thiếu `answer`**.

### 7.6. Nội dung event `sql`, `chart` và `answer`

**`sql`** — chuỗi SQL đã format, hiện thẳng lên UI được (đã qua kiểm duyệt, không phải ghép từ
`sql-result`):

```json
{"type": "sql", "content": "SELECT \"t1\".\"NhomNguonThu_valuedanhmuc\" AS \"nhom_nguon_thu\",\n       SUM(\"t1\".\"SoThuNganSach\") AS \"tong_so_thu\"\nFROM ..."}
```

**`chart`** — `content` là chuỗi chứa JSON, parse hai lớp:

```python
ev    = json.loads(line[5:])       # lớp 1
chart = json.loads(ev["content"])  # lớp 2
```

```json
{
  "type": "column",
  "title": "Tổng số thu ngân sách theo nhóm nguồn thu",
  "axis": {
    "x": {"name": "Nhóm nguồn thu", "value": "nhom_nguon_thu"},
    "y": [{"name": "Tổng số thu",   "value": "tong_so_thu"}]
  }
}
```

| Field | Ý nghĩa |
|---|---|
| `type` | `table` \| `column` (cột dọc) \| `bar` (cột ngang) \| `line` \| `pie` |
| `title` | Tiêu đề biểu đồ |
| `axis.x.value` | **Tên cột** dùng làm trục X |
| `axis.y[].value` | **Tên cột** dùng làm trục Y (có thể nhiều) |
| `axis.series.value` | *(tùy chọn)* Tên cột dùng để phân nhóm/tô màu |
| `*.name` | Nhãn hiển thị |

`axis.*.value` là **tên cột**, không phải số liệu — cấu hình này không chứa con số nào, số liệu lấy
ở §8.

**`answer`** — ngược với `chart`: text thuần, parse một lớp là xong:

```json
{"type": "answer", "content": "Top 3 nhóm nguồn thu có số thu lớn nhất lần lượt là Thu cân đối với 7130.61879, Thu tiền sử dụng đất với 2385.8 và Thu từ hoạt động xuất nhập khẩu với 1569.7. Nhóm Thu cân đối đạt mức thu cao nhất, vượt trội rõ rệt so với hai nhóm còn lại."}
```

| | |
|---|---|
| Định dạng | Văn xuôi thuần, không Markdown — render bằng text thường là đủ |
| Độ dài | 3–5 câu, 300–600 ký tự |
| Ngôn ngữ | Theo ngôn ngữ câu hỏi |
| Khoảng trắng | Đã `strip()` sẵn |
| Số liệu | Trích từ dữ liệu thật, giữ nguyên độ chính xác gốc (`7130.61879`, không làm tròn, không định dạng theo locale) |

### 7.7. ⚠ Lỗi giữa stream vẫn là HTTP 200

Nếu pipeline hỏng (LLM lỗi, SQL sai, DB mất kết nối), server gửi `{"type": "error", "content": "..."}`
rồi đóng stream bình thường — status code vẫn **200**. Client bắt buộc có nhánh xử lý
`type == "error"`; chỉ kiểm tra HTTP status là không đủ.

### 7.8. Có tắt được pha sinh biểu đồ không?

**Hiện tại: không.** `POST /chat/question` luôn chạy hết mọi pha, request không có tham số điều
khiển điểm dừng.

---

## 8. `GET /chat/record/{record_id}/data` — Lấy số liệu

**Bắt buộc.** Số liệu không đi qua SSE — ghi vào DB, lấy qua endpoint riêng. `record_id` là giá trị
từ event `id`.

**Request**

```http
GET /api/v1/chat/record/260/data
X-SQLBOT-TOKEN: Bearer <jwt>
```

**Response 200**

```json
{
  "code": 0,
  "data": {
    "fields": ["nhom_nguon_thu", "tong_so_thu"],
    "fields_info": [
      {"name": "nhom_nguon_thu", "is_numeric": false},
      {"name": "tong_so_thu",    "is_numeric": true}
    ],
    "data": [
      {"nhom_nguon_thu": "Thu cân đối",                    "tong_so_thu": 7130.61879},
      {"nhom_nguon_thu": "Thu tiền sử dụng đất",           "tong_so_thu": 2385.8},
      {"nhom_nguon_thu": "Thu từ hoạt động xuất nhập khẩu","tong_so_thu": 1569.7},
      {"nhom_nguon_thu": "Thu kinh phí đóng góp...",       "tong_so_thu": 583.7},
      {"nhom_nguon_thu": "Thu xổ số kiến thiết",           "tong_so_thu": "46.69999999999999"}
    ]
  },
  "msg": null
}
```

| Field | Ý nghĩa |
|---|---|
| `fields` | Danh sách tên cột, theo đúng thứ tự |
| `fields_info[].is_numeric` | Cột là số hay text — dùng để căn lề bảng, chọn kiểu trục |
| `data` | Mảng object, mỗi phần tử là một dòng |

**Bẫy kiểu dữ liệu:** số có quá 15 chữ số có nghĩa bị chuyển thành **chuỗi** để tránh mất chính xác
qua JSON (dòng cuối ví dụ trên: `"46.69999999999999"` là string, `7130.61879` là number). Client
phải `parseFloat()` trước khi vẽ, không giả định kiểu.

**Giới hạn:** tối đa 1000 dòng. Nếu SQL trả nhiều hơn, kết quả bị cắt và có thêm field `limit`.

### 8.1. Biến thể: `/data_live`

```
GET /api/v1/chat/record/{record_id}/data_live
```

Cùng format nhưng **chạy lại SQL** lấy số liệu mới nhất, thay vì đọc bản đã lưu. Dùng cho nút "Làm
mới" — chậm hơn và tạo tải lên DB nguồn, đừng gọi tự động theo chu kỳ.

---

## 9. Ghép thành biểu đồ

Hai mảnh đến từ hai đường khác nhau và bù nhau:

```
Event "chart" (SSE):   {"type":"column",
                        "axis":{"x":{"value":"nhom_nguon_thu"},
                                "y":[{"value":"tong_so_thu"}]}}
                          ↓ "vẽ cột dọc, trục X lấy cột nhom_nguon_thu, trục Y lấy tong_so_thu"

GET .../data:          [{"nhom_nguon_thu":"Thu cân đối","tong_so_thu":7130.61879}, ...]
                          ↓ số liệu đổ vào

                       → render
```

Thiếu `/data` thì chỉ có khung rỗng. Nếu `chart.type == "table"`, bỏ qua phần vẽ, hiện bảng trực
tiếp từ `fields` + `data`.

**`answer` không phải ghép gì cả** — đoạn văn hoàn chỉnh, hiển thị được ngay cả khi `/data` chưa về
hoặc biểu đồ hỏng. Thứ tự hợp lý cho UI dạng chatbot: `answer` trước, rồi biểu đồ, rồi bảng số liệu
làm phần chứng minh.

---

## 10. Giới hạn hệ thống — cần biết trước khi thiết kế UI

### 10.1. Không có cơ chế hủy

Không có endpoint dừng một câu hỏi đang chạy. Client đóng connection thì server **vẫn chạy hết
pipeline và vẫn tiêu thụ token LLM**. Nút "Dừng" trên UI chỉ ngừng hiển thị phía client.

### 10.2. Không có refresh token

JWT sống 8 ngày, hết hạn phải đăng nhập lại. Client cần lưu credential hoặc bắt 401 để re-login.

### 10.3. Timeout phía client

Đặt **read timeout** (giữa hai chunk), không đặt total timeout. Khuyến nghị 60–90 giây — pha sinh
biểu đồ đôi khi đứng lâu bất thường, total timeout sẽ cắt nhầm câu hỏi nặng nhưng vẫn đang chạy tốt.

### 10.4. Không chạy song song nhiều câu hỏi trên cùng `chat_id`

Ngữ cảnh multi-turn đọc từ lịch sử record. Hai câu hỏi chồng nhau trên cùng `chat_id` cho kết quả
khó đoán — xếp hàng tuần tự, hoặc dùng `chat_id` khác nhau.

> Khác với song song ở [§7.5](#75-answer-và-chart-chạy-song-song): đó là song song **trong một
> lượt hỏi** do server tự làm; ở đây là **nhiều lượt hỏi** do client gửi chồng nhau — không được.

---

## Phụ lục: checklist tích hợp

- [ ] Login gửi `form-urlencoded`, không phải JSON
- [ ] Header `X-SQLBOT-TOKEN`, có tiền tố `Bearer `
- [ ] Kiểm tra `status_code` trước khi bóc `.data` (lỗi không có vỏ bọc)
- [ ] Lưu `record_id` từ event `id` ngay đầu stream
- [ ] Switch trên `type`, không dựa vào thứ tự event
- [ ] Có nhánh xử lý `type == "error"` (HTTP vẫn 200)
- [ ] Gọi `/chat/record/{id}/data` — số liệu không có trong SSE
- [ ] Nếu **không** cần biểu đồ: đã thống nhất phương án ở [§7.8](#78-có-tắt-được-pha-sinh-biểu-đồ-không) (đừng chỉ lặng lẽ bỏ qua event)
- [ ] `answer-result` và `chart-result` cộng dồn vào **hai buffer khác nhau** (chúng đến xen kẽ)
- [ ] **Không** giả định `answer` đến trước `chart` (hoặc ngược lại)
- [ ] UI render được kết quả **thiếu `answer`** (khi gặp `info: answer failed`)
- [ ] Parse 2 lớp JSON cho event `chart`, **1 lớp** cho event `answer`
- [ ] Có đường lùi về dạng bảng khi cấu hình biểu đồ không đọc được
- [ ] `parseFloat()` giá trị số (có thể là string)
- [ ] Read timeout 60–90s, không dùng total timeout
- [ ] Xử lý 401 → đăng nhập lại (không có refresh token)
