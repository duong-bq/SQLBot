# Đặc tả API tích hợp SQLBot Chat

Tài liệu dành cho hệ thống UI bên thứ ba cần nhúng chức năng hỏi đáp dữ liệu của SQLBot.

## Mục lục

**Đọc trước khi lập trình**

| | |
|---|---|
| [§0. SQLBot là gì](#0-sqlbot-là-gì) | Sản phẩm làm gì |
| [§1. Khái niệm nền tảng](#1-khái-niệm-nền-tảng) | Datasource, chat, record |
| [§2. Luồng tích hợp](#2-luồng-tích-hợp) | 3 bước gọi API để tích hợp |
| [§3. Quy ước chung](#3-quy-ước-chung) | Xác thực, định dạng response, bảng mã lỗi |

**Đặc tả từng API**

| | |
|---|---|
| [§4. `POST /login/access-token`](#4-post-loginaccess-token--đăng-nhập) | Đăng nhập lấy JWT |
| [§5. `GET /datasource/list`](#5-get-datasourcelist--danh-sách-nguồn-dữ-liệu) | Liệt kê nguồn dữ liệu |
| [§6. `POST /chat/question`](#6-post-chatquestion--hỏi-sse) | **Hỏi (SSE)** — mục dài nhất |

**Vận dụng**

| | |
|---|---|
| [§7. Giới hạn hệ thống](#7-giới-hạn-hệ-thống--cần-biết-trước-khi-thiết-kế-ui) | Đọc **trước khi** thiết kế UI |
| [Checklist tích hợp](#phụ-lục-checklist-tích-hợp) | Kiểm trước khi bàn giao |

---

## 0. SQLBot là gì

SQLBot nhận **câu hỏi bằng tiếng Việt**, sinh SQL, **tự chạy SQL đó** trên database nghiệp vụ, rồi
trả về **câu trả lời bằng lời** — kèm cả câu SQL đã dùng nếu bạn muốn hiển thị.

```
"Có tổng số bao nhiêu Nghị quyết"
   → sinh SQL → chạy SQL → đọc kết quả
   → "Tổng số Nghị quyết là 976, được tính bằng tổng của 83, 458 và 435 Nghị quyết
      từ các bảng dữ liệu liên quan."
```

Toàn bộ diễn ra trong **một lời gọi API duy nhất**, trả về dạng SSE.

---

## 1. Khái niệm nền tảng

### 1.1. Datasource — nguồn dữ liệu

Một kết nối tới database nghiệp vụ (PostgreSQL, MySQL, Excel/CSV…) do quản trị viên SQLBot khai báo
sẵn.

- **Client không tạo datasource** — chỉ liệt kê ([§5](#5-get-datasourcelist--danh-sách-nguồn-dữ-liệu)) và chọn.
- **Mỗi hội thoại gắn cứng đúng một datasource** lúc tạo, không đổi được sau đó.

### 1.2. Chat (hội thoại) — `chat_id`

Một phiên hỏi đáp. Giữ datasource đã chọn và lịch sử câu hỏi để hiểu ngữ cảnh multi-turn (hỏi "còn
năm ngoái thì sao?" sau một câu hỏi khác vẫn hiểu được).

**`chat_id` do client tự sinh, kiểu UUID.** Không có bước tạo hội thoại riêng:

- Lần hỏi **đầu tiên** của một `chat_id` mới → gửi kèm `datasource`, server tự tạo hội thoại.
- Các lần hỏi **sau** trên cùng `chat_id` → không cần gửi `datasource` nữa (gửi cũng bị bỏ qua).

Giữ nguyên `chat_id` khi hỏi cùng mạch; sinh `chat_id` mới khi đổi chủ đề hoặc đổi datasource.

> `chat_id` là **chuỗi UUID**

### 1.3. Record (lượt hỏi) — `record_id`

Một câu hỏi và toàn bộ kết quả của nó. Một `chat_id` chứa nhiều `record_id`. `record_id` được cấp
ngay từ event đầu của stream — lưu lại để đối soát/tra log khi cần.

### 1.4. Quan hệ giữa ba khái niệm

```
Datasource "Hội đồng nhân dân thành phố Hà Nội" (id=1) ──┐
                                                          │  chọn 1 datasource ở câu hỏi ĐẦU TIÊN
Chat (chat_id="7fa1a92e-…-1210897903e2", client tự sinh) ─┘
   │
   ├── Record (id=33)   "Có tổng số bao nhiêu Nghị quyết"
   │      ├── câu SQL đã sinh
   │      └── câu trả lời bằng lời
   │
   └── Record (id=34)   "Trong đó bảng nào nhiều nhất"
```

---

## 2. Luồng tích hợp

```
1. POST /login/access-token          → đăng nhập để lấy JWT
2. GET  /datasource/list             → lấy danh sách các datasource (làm 1 lần, cache lại)
   ┌─────────────────────────────────────────────────── lặp cho mỗi câu hỏi
3. │ POST /chat/question             → dữ liệu stream theo định dạng SSE
   └───────────────────────────────────────────────────
```

Chỉ ba lời gọi, và không cần gọi API tạo hội thoại. Mọi thứ của một lượt hỏi — câu SQL, số liệu,
câu trả lời bằng lời, cấu hình biểu đồ — đều về trên cùng một stream ở bước 3.

---

## 3. Quy ước chung

### 3.1. Base URL

```
https://<host>/api/v1
```
Môi trường test: http://192.168.9.89:8001/api/v1

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
| 500 | `"Chat with id X does not exist. Provide 'datasource' ..."` | `chat_id` mới nhưng quên gửi `datasource` |
| 500 | `"chat_id must be a non-empty string of at most 64 characters"` | `chat_id` rỗng hoặc quá dài |
| 500 | `"Datasource with id X not found"` | `datasource` không tồn tại |
| 500 | `"Datasource with id X does not belong to current workspace"` | `datasource` không lấy từ `/datasource/list` của chính tài khoản đang đăng nhập |
| 500 | `"Access denied or resource not found!"` | `chat_id` trùng UUID của hội thoại thuộc người dùng khác |

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
curl -X POST http://<host>:8001/api/v1/login/access-token \
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

Chỉ trả về datasource mà user hiện tại có quyền. Gọi một lần lúc khởi động và cache lại.

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
    {
      "id": 1,
      "name": "Hội đồng nhân dân thành phố Hà Nội",
      "type": "pg",
      "type_name": "PostgreSQL",
      "description": "Kho dữ liệu hoạt động khối dân cư của Hội đồng nhân dân thành phố Hà Nội",
      "status": "Success",
      "num": "4/430"
    }
  ],
  "msg": null
}
```

Đây là dữ liệu thật của môi trường test: hiện chỉ có **một** datasource, `id = 1`. Response đầy đủ
còn nhiều field khác (`configuration`, `table_relation`, …) phục vụ giao diện quản trị — client tích
hợp bỏ qua được, chỉ cần `id`, `name`, `description`.

Lấy `id` để dùng ở bước tiếp theo. **Không hardcode** — id khác nhau giữa các môi trường; danh sách
này cũng sẽ dài thêm khi quản trị viên khai báo thêm nguồn dữ liệu.

---

## 6. `POST /chat/question` — Hỏi (SSE)

Endpoint cốt lõi, và là endpoint duy nhất bạn gọi lặp lại.

**Request**

```http
POST /api/v1/chat/question
X-SQLBOT-TOKEN: Bearer <jwt>
Content-Type: application/json
Accept: text/event-stream

{
  "chat_id": "7fa1a92e-a07b-442b-bade-1210897903e2",
  "question": "Có tổng số bao nhiêu Nghị quyết",
  "datasource": 1
}
```

| Field | Kiểu | Bắt buộc | Ghi chú |
|---|---|---|---|
| `chat_id` | string (UUID) | ✔ | Do client tự sinh, xem [§1.2](#12-chat-hội-thoại--chat_id) |
| `question` | string | ✔ | Câu hỏi bằng tiếng Việt |
| `datasource` | int | chỉ lần đầu | Bắt buộc khi `chat_id` chưa tồn tại; các lần sau có thể không cần gửi |

Câu hỏi **tiếp theo** trên cùng hội thoại dùng `chat_id` cũ:

```json
{"chat_id": "7fa1a92e-a07b-442b-bade-1210897903e2", "question": "Trong đó bảng nào nhiều nhất"}
```

**Response**: `HTTP 200`, `Content-Type: text/event-stream; charset=utf-8`

### 6.1. Định dạng dây

Mỗi event chiếm **đúng 2 dòng**:

```
data:{"type":"id","id":33}       ← dòng 1: tiền tố "data:" + JSON
                                 ← dòng 2: dòng trống (dấu ngắt event)
```

Khác SSE thông thường ở hai điểm: **không có `event:`** (chỉ có `type` trong JSON để switch), và
**không có `[DONE]`** — kết thúc duy nhất là `{"type":"finish"}` (hoặc `type: "error"`).

### 6.2. Hai nhóm event

| | Event **token** | Event **mốc** |
|---|---|---|
| `type` | `sql-result`, `answer-result` | tất cả còn lại |
| Cấu trúc | `{content, reasoning_content, type}` | mỗi type một schema riêng |
| Số lượng | hàng trăm | đúng 1 |
| Cách xử lý | **cộng dồn `content`** để hiện chữ chạy | **thay thế** state |

> Ở cả `sql-result` lẫn `answer-result`, chỉ lấy trường **`content`**. Trường `reasoning_content`
> luôn rỗng — hệ thống đã tắt chế độ thinking của LLM — nên bỏ qua nó.

#### `answer-result` là câu trả lời của Bot

Cộng dồn `content` của các event `answer-result` → đó là câu trả lời của bot, hiện dần ra trong lúc
LLM đang viết. **Đây là thứ cần render.**

Event `answer` ở cuối mang **đúng cùng nội dung đó**, gộp sẵn thành một chuỗi hoàn chỉnh. Dùng nếu
bạn muốn set lại UI một lần cho chắc sau khi stream xong — không bắt buộc.

Nhận `info: answer failed` thì pha trả lời đã hỏng: phần chữ đã hiện là dở dang và sẽ **không có**
event `answer`. SQL vẫn dùng được bình thường.

### 6.3. Danh mục event

| `type` | Schema | Ý nghĩa |
|---|---|---|
| `id` | `{type, id}` | **Record ID** — lưu lại để tra log |
| `question` | `{type, question}` | Câu hỏi đã chuẩn hóa |
| `sql-result` | `{type, content, reasoning_content}` | Đang sinh SQL, từng token |
| `info` | `{type, msg: "sql generated"}` | Kết thúc **một lượt** sinh SQL — có thể xuất hiện nhiều lần, xem [§6.8](#68-sql-hỏng-không-phải-lúc-nào-cũng-thành-error) |
| `brief` | `{type, brief}` | Tiêu đề hội thoại LLM tự đặt. *Chỉ có ở câu hỏi đầu tiên* |
| `sql` | `{type, content}` | **SQL chính thức**, đã qua kiểm duyệt và format. *Có thể không có* — xem [§6.8](#68-sql-hỏng-không-phải-lúc-nào-cũng-thành-error) |
| `sql-data` | `{type, content: "execute-success", data}` | ★ **Số liệu** — kết quả chạy SQL, xem [§6.6](#66-số-liệu--event-sql-data). *Có thể không có* |
| `answer-result` | `{type, content, reasoning_content}` | ★ **Câu trả lời**, từng token — cộng dồn `content` |
| `info` | `{type, msg: "chart generated"}` | Pha biểu đồ xong |
| `chart` | `{type, content}` | **Cấu hình biểu đồ**, `content` là **chuỗi JSON** — xem [§6.9](#69-cấu-hình-biểu-đồ) |
| `info` | `{type, msg: "chart failed"}` | Pha biểu đồ lỗi — **không có** event `chart`, phần còn lại của lượt hỏi vẫn bình thường |
| `info` | `{type, msg: "answer generated"}` | Kết thúc pha answer |
| `info` | `{type, msg: "answer failed"}` | Pha answer lỗi — sẽ **không có** event `answer` |
| `answer` | `{type, content}` | Cùng câu trả lời đó, gộp sẵn một chuỗi — tùy chọn |
| `finish` | `{type}` | Kết thúc |
| `error` | `{type, content}` | Lỗi pipeline — xem [§6.7](#67--lỗi-giữa-stream-vẫn-là-http-200) |

Nhóm event biểu đồ (`info: chart generated` + `chart`, hoặc `info: chart failed`) **chỉ có khi hệ
thống bật pha sinh biểu đồ** (mặc định bật). Không dùng biểu đồ thì bỏ qua cả nhóm, phần còn lại của
hợp đồng không đổi.

Cấu hình biểu đồ **không** được stream theo từng token: nó là một khối JSON, mảnh dở dang thì không
parse được nên chẳng dùng vào việc gì. Client nhận trọn gói ở event `chart`.

### 6.4. Trình tự thực tế

Capture thật, câu hỏi *"Có tổng số bao nhiêu Nghị quyết"* trên datasource `id = 1`, tổng thời gian
**4 giây**:

```
   event            số lượng
──────────────────────────────────────────────────────
   id                  1     {"id":33}
   question            1
   sql-result        305     ← quá trình LLM viết suy nghĩ và viết SQL, bỏ qua hoặc UI có thể sử dụng như là phần suy luận của Bot
   info                1     {"msg":"sql generated"}
   brief               1     {"brief":"Tổng số Nghị quyết"}
   sql                 1
   sql-data            1
   answer-result      41     ★ câu trả lời của BOT
   info                1     {"msg":"answer generated"}
   answer              1     kết quả cộng dồn các `answer-result`
   finish              1
```

Câu hỏi thứ hai trên cùng `chat_id` (*"Trong đó bảng nào nhiều nhất"*, **5 giây**) cho đúng bộ event
trên, **trừ `brief`** — tiêu đề chỉ sinh một lần cho cả hội thoại.

#### Khi bật pha sinh biểu đồ

Phần đầu (tới `sql-data`) không đổi. Chỉ thêm hai event mốc chen vào giữa dòng `answer-result`:

```
sql-data            1
answer-result  ×M         câu trả lời chảy về trong lúc biểu đồ đang được sinh
info                1     {"msg":"chart generated"}   ← hoặc {"msg":"chart failed"}
chart               1     ★ cấu hình biểu đồ          ← không có nếu chart failed
answer-result  ×K         phần câu trả lời còn lại
info                1     {"msg":"answer generated"}
answer              1     ★ câu trả lời gộp sẵn
finish              1
```

Biểu đồ và câu trả lời được sinh **song song**, nên `answer-result` xuất hiện ở cả hai phía của cặp
event biểu đồ. Tỷ lệ trước/sau phụ thuộc pha nào xong trước, đừng dựa vào nó.

Ba điều **giữ nguyên** so với khi tắt biểu đồ, dựa vào được:

- `answer` vẫn là event áp chót, ngay trước `finish`.
- `chart` luôn đứng ngay sau `info: chart generated`.
- Không có event token nào mới — chỉ `sql-result` và `answer-result` như cũ.

#### Mốc thời gian thực đo — dữ liệu về **dần theo pha**

| Thời điểm | Event | Client có được gì |
|---:|---|---|
| 0.58s | `id`, `question` | `record_id` — có gần như ngay lập tức |
| 0.58 → 3.12s | `sql-result` ×295 | LLM đang viết SQL (**2.5s — phần lớn thời gian chờ**) |
| 3.12s | `brief`, `sql`, `sql-data` | SQL chính thức, đã chạy xong trên DB |
| 3.12 → 3.39s | `answer-result` ×63 | ★ câu trả lời hiện dần (**0.27s**) |
| 3.39s | `answer` | kết quả cộng dồn các `answer-result` |
| 3.39s | `finish` | đóng stream |


**Không dựa vào thứ tự cố định** — luôn switch trên `type`. Thay đổi khi:

- Không phải câu hỏi đầu tiên → không có `brief`
- Pha answer lỗi → không có `answer`, chỉ có `info: answer failed`
- Pipeline lỗi → `error` thay thế toàn bộ phần còn lại
- SQL sinh hỏng, server sinh lại → **`sql-result` và `info: sql generated` về thành nhiều đợt**
  (xem [§6.8](#68-sql-hỏng-không-phải-lúc-nào-cũng-thành-error))
- SQL hỏng hẳn → không có `sql` / `sql-data`, nhưng **vẫn có `answer-result` + `answer` + `finish`**;
  lượt đó cũng **không có** nhóm event biểu đồ dù hệ thống đang bật biểu đồ — không có dữ liệu thì
  không vẽ được gì
- Pha biểu đồ lỗi → `info: chart failed` thay cho `info: chart generated` + `chart`; **mọi thứ khác
  giữ nguyên**, vẫn có đủ `answer` và `finish`
- Hệ thống tắt pha biểu đồ → không có `info: chart generated` / `chart` / `info: chart failed`

### 6.5. Nội dung câu trả lời và câu SQL

**`sql`** — chuỗi SQL đã format, hiện thẳng lên UI được:

```json
{"type": "sql", "content": "SELECT COUNT(*) AS \"total_count\"\nFROM \"public\".\"NGHI_QUYET_BAO_CAO_D3D7BB_6F6F_row_values\"\nUNION ALL\nSELECT COUNT(*) AS \"total_count\"\nFROM \"public\".\"NGHI_QUYET_HDND_B09846_70FE_row_values\"\nUNION ALL\nSELECT COUNT(*) AS \"total_count\"\nFROM \"public\".\"NGHI_QUYET_THONG_TIN_CHUNG_2A8963_93D9_row_values\"\nUNION ALL\nSELECT COUNT(*) AS \"total_count\"\nFROM \"public\".\"NGHI_QUYET_TTC_F2B2C6_4A4E_row_values\"\nLIMIT 1000"}
```

**Câu trả lời** — text thuần; dưới đây là event `answer`, cộng dồn `answer-result` ra đúng chuỗi này:

```json
{"type": "answer", "content": "Tổng số Nghị quyết là 976, được tính bằng tổng của 83, 458 và 435 Nghị quyết từ các bảng dữ liệu liên quan."}
```

| | |
|---|---|
| Định dạng | Thường là văn xuôi, nhưng **có thể chứa Markdown** (danh sách đánh số, xuống dòng `\n`) khi câu hỏi yêu cầu liệt kê. Render bằng Markdown hoặc giữ nguyên xuống dòng. |
| Độ dài | Thường 1–5 câu |
| Ngôn ngữ | Theo ngôn ngữ câu hỏi |
| Khoảng trắng | Nên `trim()` khi hiển thị |
| Số liệu | Trích từ dữ liệu thật, giữ nguyên độ chính xác gốc (không làm tròn, không định dạng theo locale) |
| Không có dữ liệu | Trả về câu báo rõ, ví dụ `"Không tìm thấy dữ liệu phù hợp với câu hỏi."` |

### 6.6. Số liệu — event `sql-data`

Kết quả chạy SQL về trọn gói trong trường `data` của event `sql-data`. Đây là event nặng nhất của
lượt hỏi, nhưng cũng về sớm — ngay sau khi SQL chạy xong, trước cả token đầu tiên của câu trả lời.

```json
{"type": "sql-data", "content": "execute-success",
 "data": {"fields": ["linh_vuc", "so_luong"],
          "fields_info": [{"name": "linh_vuc", "is_numeric": false},
                          {"name": "so_luong", "is_numeric": true}],
          "data": [{"linh_vuc": "Kinh tế", "so_luong": 83},
                   {"linh_vuc": "Văn hóa", "so_luong": 45}]}}
```

| Trường | Ý nghĩa |
|---|---|
| `content` | Luôn là chuỗi `"execute-success"`. Giữ lại cho tương thích ngược, không mang thông tin gì |
| `data.fields` | Tên các cột, đúng thứ tự cột trong kết quả |
| `data.fields_info` | Mỗi cột một phần tử: `name` và `is_numeric`. Dùng để biết cột nào là số |
| `data.data` | Danh sách **dòng**. Mỗi dòng là một object, khóa là tên cột |
| `data.limit` | **Chỉ xuất hiện khi kết quả bị cắt bớt.** Giá trị là số dòng tối đa được trả (1000). Vắng mặt nghĩa là dữ liệu trọn vẹn |

⚠ **Số quá lớn về dưới dạng chuỗi.** Số nguyên vượt ngưỡng an toàn của JSON, và số thập phân quá
lớn hoặc quá nhỏ, được server chuyển thành chuỗi trước khi gửi — nếu để nguyên dạng số thì nhiều
client sẽ tự làm tròn và mất chữ số cuối mà không báo lỗi. Hệ quả: **cùng một cột có thể lẫn cả số
lẫn chuỗi giữa các dòng**. Hãy ép kiểu trước khi cộng dồn, so sánh hay định dạng, đừng tin vào kiểu
của giá trị ở dòng đầu tiên.

Trần **1000 dòng** áp cho mọi lượt hỏi. Câu hỏi quét cả bảng sẽ bị cắt, và `data.limit` là dấu hiệu
duy nhất để biết điều đó — nên hiện một dòng ghi chú cho người dùng khi thấy nó.

Event này **có thể không xuất hiện**, khi câu hỏi không chạy được SQL nào — xem
[§6.8](#68-sql-hỏng-không-phải-lúc-nào-cũng-thành-error).

### 6.7. ⚠ Lỗi giữa stream vẫn là HTTP 200

Nếu pipeline hỏng (LLM lỗi, SQL sai, DB mất kết nối), server gửi `{"type": "error", "content": "..."}`
rồi đóng stream bình thường — status code vẫn **200**. Client bắt buộc có nhánh xử lý
`type == "error"`; chỉ kiểm tra HTTP status là không đủ.

Trường `content` của event `error` **không có schema cố định**: khi là văn xuôi, khi là một chuỗi
JSON `{"message": ..., "traceback": ...}`. Hãy hiển thị nó như text; nếu muốn bóc `message` thì thử
`JSON.parse` trong `try/catch` rồi fallback về chuỗi gốc.

### 6.8. SQL hỏng không phải lúc nào cũng thành `error`

Khi pha sinh SQL hỏng, server **tự sinh lại một lần** trước khi bỏ cuộc. Việc này **không sinh ra
event mới nào** — client chỉ thấy pha sinh SQL lặp lại:

```
sql-result ×N                        ← lần thử 1 (SQL hỏng, sẽ không thành event `sql`)
info  {"msg":"sql generated"}
sql-result ×M                        ← lần thử 2, nối tiếp ngay sau
info  {"msg":"sql generated"}
```

Hệ quả: **`info: sql generated` có thể đến nhiều lần trong một câu hỏi**, và nội dung `sql-result`
cộng dồn sẽ chứa cả lần hỏng lẫn lần sau. Nếu UI hiển thị `sql-result` như phần "suy luận" của bot
thì cứ nối tiếp bình thường, không cần xử lý gì thêm. Nếu muốn tách sạch từng lần thử, hãy dùng
`info: sql generated` làm mốc cắt.

Nếu lần thử lại vẫn hỏng, thay vì trả `error`, server chuyển sang cho bot **trả lời bằng lời dựa
trên lịch sử hội thoại**:

```
answer-result ×K   → answer → finish   ← KHÔNG có sql, KHÔNG có sql-data, KHÔNG có error
```

Đây là ca thường gặp với câu hỏi tham chiếu ngược (*"trong đó bảng nào tên dài nhất"*): dữ liệu để
trả lời đã có từ lượt trước, chỉ là không diễn đạt được thành SQL mới. Với client, hệ quả là:

- **`sql` và `sql-data` là tùy chọn**, không phải event chắc chắn có. Đừng chờ `sql-data` mới bắt
  đầu render `answer-result`, và đừng coi việc thiếu chúng là lỗi.
- Lý do hỏng **không được gửi qua stream** — nó chỉ nằm trong log server. Người dùng cuối chỉ thấy
  câu trả lời trong `answer-result`.

### 6.9. Cấu hình biểu đồ

Khi hệ thống bật pha sinh biểu đồ (mặc định bật), server đề xuất một cách **trực quan hóa** kết quả
truy vấn. Nó cho bạn *vẽ cái gì, từ cột nào* — **không kèm số liệu**. Số liệu nằm ở event `sql-data`
([§6.6](#66-số-liệu--event-sql-data)); ghép hai thứ lại bằng tên cột thì ra biểu đồ.

#### 6.9.1. Event `chart`

```json
{"type": "chart", "content": "{\"type\":\"pie\",\"title\":\"Số nghị quyết theo lĩnh vực\",\"axis\":{\"y\":{\"name\":\"Số lượng\",\"value\":\"so_luong\"},\"series\":{\"name\":\"Lĩnh vực\",\"value\":\"linh_vuc\"}}}"}
```

⚠ `content` là **chuỗi JSON đã escape**, không phải object lồng. Phải `JSON.parse(event.content)`
thêm một lần nữa mới ra cấu hình. Đây là điểm khác với các event khác, rất dễ vấp.

Event này về **trọn gói một lần**, không stream từng token.

#### 6.9.2. Năm loại biểu đồ

Trường `type` nhận đúng một trong năm giá trị: `table`, `column` (cột dọc), `bar` (cột ngang),
`line` (đường), `pie` (tròn). Mọi cấu hình đều có `title` — một tiêu đề ngắn do bot đặt.

**`table`** — dùng `columns`, không có `axis`:

```json
{"type":"table","title":"Danh sách nghị quyết",
 "columns":[{"name":"Số nghị quyết","value":"so_nghi_quyet"},
            {"name":"Ngày ban hành","value":"ngay_ban_hanh"}]}
```

**`column` / `bar` / `line`** — cùng **một** schema, chỉ khác cách vẽ. Trục `x` là chiều (thời gian
hoặc phân loại), `y` là **mảng** các chỉ số số:

```json
{"type":"line","title":"Xu hướng thu chi",
 "axis":{"x":{"name":"Tháng","value":"thang"},
         "y":[{"name":"Thu","value":"thu"},{"name":"Chi","value":"chi"}]}}
```

> `bar` là `column` xoay ngang về mặt **hiển thị**; ánh xạ dữ liệu giữ nguyên — `x` vẫn là chiều,
> `y` vẫn là số. Đừng hoán đổi hai trục khi render `bar`.

Với ba loại này, `axis.y` **nên** là mảng nhưng thỉnh thoảng về dưới dạng một object đơn
(`{"name":..., "value":...}`) — server chấp nhận cả hai. Hãy chuẩn hóa về mảng ngay khi nhận:
`const y = Array.isArray(axis.y) ? axis.y : [axis.y]`.

**`pie`** — không có trục `x`; `y` là **object** (không phải mảng) chỉ kích thước từng phần, `series`
chỉ tên từng phần:

```json
{"type":"pie","title":"Tỷ trọng theo lĩnh vực",
 "axis":{"y":{"name":"Số lượng","value":"so_luong"},
         "series":{"name":"Lĩnh vực","value":"linh_vuc"}}}
```

#### 6.9.3. Hai trường tùy chọn — `series` và `multi-quota`

Cả hai nằm trong `axis`, và **loại trừ lẫn nhau**: một cấu hình chỉ có tối đa một trong hai.

| Trường | Có khi | Ý nghĩa | Dạng |
|---|---|---|---|
| `series` | kết quả có cột phân loại (rời rạc, không phải số/thời gian) | nhóm dữ liệu thành nhiều chuỗi | `{"name": "...", "value": "ten_cot"}` |
| `multi-quota` | **không** có cột phân loại nhưng có **nhiều** cột số | gộp nhiều chỉ số lên cùng một biểu đồ | `{"name": "...", "value": ["cot_1", "cot_2"]}` |

Không có cột phân loại và chỉ có một cột số thì **cả hai đều vắng mặt** — đây là ca phổ biến nhất.
`pie` luôn có `series` (nó chính là tên từng phần).

Client phải coi `series` và `multi-quota` là **có thể không tồn tại**, đừng đọc thẳng
`axis.series.value`.

#### 6.9.4. Biểu đồ là pha phụ — hỏng thì bỏ qua, không mất lượt hỏi

Nếu bot không dựng được cấu hình biểu đồ hợp lệ (dữ liệu không có cột nào hợp làm phân loại, JSON
trả về sai định dạng…), server phát:

```
info  {"msg":"chart failed"}
```

rồi **đi tiếp bình thường**: vẫn có `answer`, vẫn có `finish`, vẫn HTTP 200 và **không** có event
`error`. Chỉ thiếu đúng event `chart`.

Client chỉ cần: nhận `chart failed` thì ẩn khu vực biểu đồ và hiển thị câu trả lời bằng lời như một
lượt hỏi bình thường. Không cần báo lỗi cho người dùng — với họ, lượt hỏi này đã thành công.

Quy ước này giống hệt `info: answer failed` ở pha answer: **một pha phụ hỏng không được giết cả lượt
hỏi.** Chỉ có `type: "error"` mới nghĩa là cả lượt hỏi thất bại.

---

## 7. Giới hạn hệ thống — cần biết trước khi thiết kế UI

### 7.1. Không có cơ chế hủy

Không có endpoint dừng một câu hỏi đang chạy. Client đóng connection thì server **vẫn chạy hết
pipeline và vẫn tiêu thụ token LLM**. Nút "Dừng" trên UI chỉ ngừng hiển thị phía client.

### 7.2. Không có refresh token

JWT sống 8 ngày, hết hạn phải đăng nhập lại. Client cần lưu credential hoặc bắt 401 để re-login.

### 7.3. Timeout phía client

Đặt **read timeout** (giữa hai chunk), không đặt total timeout. Khuyến nghị 30–60 giây. Câu hỏi
thông thường trả lời trong **2–10 giây**, nhưng câu hỏi nặng trên bảng lớn có thể lâu hơn — total
timeout sẽ cắt nhầm những câu vẫn đang chạy tốt.

### 7.4. Không chạy song song nhiều câu hỏi trên cùng `chat_id`

Ngữ cảnh multi-turn đọc từ lịch sử record. Hai câu hỏi chồng nhau trên cùng `chat_id` cho kết quả
khó đoán — xếp hàng tuần tự, hoặc dùng `chat_id` khác nhau.

### 7.5. Số liệu thô bị cắt ở 1000 dòng

Stream của `POST /chat/question` trả đủ cả bốn thứ trong một lần gọi: **câu trả lời bằng lời**,
**câu SQL**, **số liệu** và **cấu hình biểu đồ**. Nhưng số liệu bị cắt ở **1000 dòng** — câu hỏi
quét cả bảng sẽ không về đủ. Client biết được điều đó qua khóa `data.limit` của event `sql-data`
([§6.6](#66-số-liệu--event-sql-data)), và nên báo cho người dùng thay vì hiển thị như thể trọn vẹn.

Vì vậy đừng thiết kế màn hình theo hướng xuất/duyệt toàn bộ dữ liệu. SQLBot phục vụ hỏi đáp và tổng
hợp, không phải công cụ trích xuất dữ liệu.

---

## Phụ lục: checklist tích hợp

- [ ] Login gửi `form-urlencoded`, không phải JSON
- [ ] Header `X-SQLBOT-TOKEN`, có tiền tố `Bearer `
- [ ] Kiểm tra `status_code` trước khi bóc `.data` (lỗi không có vỏ bọc)
- [ ] `chat_id` do client sinh dạng **UUID** (chuỗi, ≤ 64 ký tự), lưu lại để hỏi tiếp cùng mạch
- [ ] Câu hỏi **đầu tiên** của mỗi `chat_id` có gửi kèm `datasource`
- [ ] Switch trên `type`, không dựa vào thứ tự event
- [ ] Có nhánh xử lý `type == "error"` (HTTP vẫn 200)
- [ ] Câu trả lời render từ **`answer-result` cộng dồn**; gặp `info: answer failed` thì báo lỗi
- [ ] Câu trả lời render được cả trường hợp có Markdown / xuống dòng
- [ ] Đọc số liệu từ `data` của event `sql-data`; ép kiểu trước khi tính vì **số lớn về dạng chuỗi**
- [ ] Thấy `data.limit` thì báo cho người dùng biết kết quả đã bị cắt (§6.6)
- [ ] Dùng biểu đồ: `JSON.parse` **hai lần** để bóc event `chart` (§6.9.1)
- [ ] Dùng biểu đồ: coi `series` / `multi-quota` là có thể vắng mặt, `axis.y` chuẩn hóa về mảng
- [ ] Dùng biểu đồ: `info: chart failed` chỉ ẩn biểu đồ, **không** báo lỗi cả lượt hỏi (§6.9.4)
- [ ] Read timeout 30–60s, không dùng total timeout
- [ ] Không gửi hai câu hỏi song song trên cùng `chat_id`
- [ ] Xử lý 401 → đăng nhập lại (không có refresh token)
