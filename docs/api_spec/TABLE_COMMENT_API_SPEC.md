# Đặc tả API cập nhật bảng / cột theo tên

Dành cho hệ thống bên thứ ba cần **ghi chú thích mô tả** cho các bảng và cột của một nguồn dữ liệu
đã tạo trong SQLBot, và **bật/tắt** từng bảng trong phạm vi hỏi đáp.

Phạm vi: **3 endpoint**. Bảng và cột được chỉ định bằng **tên** (đúng tên trong database gốc), không
cần tra id. Thông tin duy nhất cần giữ từ phía SQLBot là `ds_id` — id nguồn dữ liệu mà hệ thống của
bạn đã nhận khi tạo nguồn.

| Endpoint | Ghi gì |
|---|---|
| [`editTableCommentsByName`](#3-post-datasourceedittablecommentsbyname) | Chú thích của nhiều bảng |
| [`editFieldCommentsByName`](#4-post-datasourceeditfieldcommentsbyname) | Chú thích của nhiều cột trong một bảng |
| [`editTableStatusByName`](#5-post-datasourceedittablestatusbyname) | Trạng thái bật/tắt của nhiều bảng |

Mỗi endpoint ghi đúng một thứ và không đụng tới thứ của endpoint kia — đổi chú thích không làm bảng
bật/tắt, và ngược lại.

Mọi ví dụ response trong tài liệu là **capture thật** từ một hệ thống đang chạy.

---

## 1. Vì sao chú thích quan trọng

Chú thích là văn bản tự do được đưa vào ngữ cảnh gửi cho mô hình ngôn ngữ khi sinh SQL. Với database
đặt tên bảng/cột theo mã (`DB01_978A87_DACC_row_values`, `so_dien_thoai_2607290831`), đây là thứ
**duy nhất** cho mô hình biết dữ liệu là gì. Bỏ trống bước này thì chất lượng câu SQL sinh ra giảm
rõ rệt.

Chú thích ghi qua API này là trường `custom_comment` của bảng/cột trong SQLBot — độc lập với comment
gốc trong DDL của database, và **không bị ghi đè** khi SQLBot đồng bộ lại schema.

---

## 2. Quy ước chung

**Base URL:** `https://<host>/api/v1`

**Xác thực:** header `X-SQLBOT-TOKEN: Bearer <jwt>`.

Lấy JWT bằng `POST /login/access-token`, body **form-urlencoded** (không phải JSON):

```
POST /login/access-token
Content-Type: application/x-www-form-urlencoded

username=admin&password=<mật khẩu>
```

```json
{
  "code": 0,
  "data": {
    "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpZCI6MSwiYWNjb3VudCI6ImFkbWluIiwib2lkIjoxLCJleHAiOjE3ODczNjk0OTN9...",
    "token_type": "bearer",
    "platform_info": null
  },
  "msg": null
}
```

Gắn vào mọi lời gọi sau đó:

```
X-SQLBOT-TOKEN: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
```

⚠ Tên header là `X-SQLBOT-TOKEN`, **không phải** `Authorization`. Tiền tố `Bearer ` là bắt buộc,
thiếu nó sẽ nhận lỗi `Token schema error!`.

**Quyền:** cả ba endpoint yêu cầu tài khoản có vai trò `ws_admin`, và nguồn dữ liệu `ds_id` phải
thuộc workspace của tài khoản đó.

**Response thành công:** mọi mã 2xx đều được bọc cùng một vỏ.

```json
{"code": 0, "data": "<kết quả>", "msg": null}
```

**Response lỗi:** không có vỏ bọc. Phân tầng như sau:

| HTTP | Body | Ý nghĩa |
|---|---|---|
| 401 | `"Authentication invalid【...】"` | Token thiếu, sai định dạng, hoặc hết hạn |
| 404 | JSON — xem [§3](#3-post-datasourceedittablecommentsbyname) / [§4](#4-post-datasourceeditfieldcommentsbyname) / [§5](#5-post-datasourceedittablestatusbyname) | Tên bảng/cột không tồn tại trong nguồn dữ liệu. **Không có gì được ghi** |
| 422 | `{"detail":[...]}` (định dạng chuẩn FastAPI) | Body sai cấu trúc: thiếu trường bắt buộc, mảng rỗng, tên lặp |
| 500 | `"<thông điệp>"` | `ds_id` không tồn tại / không thuộc workspace (`"Access denied or resource not found!"`), hoặc lỗi hệ thống |

⚠ Thông điệp lỗi dạng chuỗi đổi theo `Accept-Language` — **đừng so khớp chuỗi** để điều khiển
logic. Điều khiển bằng HTTP status; riêng 404 có body JSON cấu trúc, parse được bằng máy.

---

## 3. `POST /datasource/editTableCommentsByName`

Ghi chú thích cho **nhiều bảng** của một nguồn dữ liệu trong một lời gọi.

**Request**

```json
{
  "ds_id": 1,
  "tables": [
    {
      "table_name": "NQ01_CE5BE0_7E59_row_values",
      "custom_comment": "Danh mục các Nghị quyết. Mỗi dòng là một nghị quyết. Dữ liệu trải dài từ năm 2012 đến 2026."
    },
    {
      "table_name": "DB01_978A87_DACC_row_values",
      "custom_comment": "Danh sách đại biểu HĐND Thành phố Hà Nội. Mỗi dòng là thông tin một đại biểu."
    }
  ]
}
```

| Field | Kiểu | Bắt buộc | Ghi chú |
|---|---|---|---|
| `ds_id` | int | ✔ | Id nguồn dữ liệu trong SQLBot |
| `tables` | array | ✔ | Ít nhất 1 phần tử, tên bảng không được lặp |
| `tables[].table_name` | string | ✔ | Tên bảng thật trong database, khớp **chính xác**, phân biệt hoa thường |
| `tables[].custom_comment` | string | ✔ | Chú thích mới. Chuỗi rỗng `""` = **xóa** chú thích đang có |

**Ngữ nghĩa:**

- Bảng **không được liệt kê** trong `tables` thì **giữ nguyên**, không bị đụng tới. Đây là cách duy
  nhất để "giữ nguyên" — đã liệt kê một bảng thì phải nói rõ ghi gì.
- Endpoint chỉ ghi chú thích. Các thuộc tính khác của bảng không bị thay đổi — muốn bật/tắt bảng
  trong phạm vi hỏi đáp thì dùng [§5](#5-post-datasourceedittablestatusbyname).
- Bảng phải là bảng **đã đồng bộ vào SQLBot**. Tên có trong database gốc nhưng chưa được chọn đồng
  bộ cũng tính là không tìm thấy.

**Response 200**

```json
{"code": 0, "data": {"tables_updated": 2}, "msg": null}
```

`tables_updated` = số bảng đã ghi, dùng để đối soát với số phần tử đã gửi.

**Response 404** — có ít nhất một tên bảng không khớp. Body liệt kê **đủ** các tên hỏng (không dừng
ở tên đầu tiên), và **không bảng nào được ghi**, kể cả những bảng có tên đúng trong cùng request:

```json
{
  "message": "Some tables not found in datasource",
  "tables_not_found": ["bang_khong_ton_tai"]
}
```

**Response 422** — body sai cấu trúc (thiếu `custom_comment`, `tables` rỗng, tên bảng lặp...), định
dạng chuẩn FastAPI. Ví dụ với tên bảng lặp:

```json
{
  "detail": [
    {
      "type": "value_error",
      "loc": ["body", "tables"],
      "msg": "Value error, duplicated table_name: ['DB01_978A87_DACC_row_values']",
      "input": [
        {"table_name": "DB01_978A87_DACC_row_values", "custom_comment": "a"},
        {"table_name": "DB01_978A87_DACC_row_values", "custom_comment": "b"}
      ],
      "ctx": {"error": {}}
    }
  ]
}
```

---

## 4. `POST /datasource/editFieldCommentsByName`

Ghi chú thích cho **nhiều cột** của **một bảng** trong một lời gọi. Cập nhật cột của N bảng thì gọi
N lần, mỗi bảng một lời gọi.

**Request**

```json
{
  "ds_id": 1,
  "table_name": "NQ01_CE5BE0_7E59_row_values",
  "fields": [
    {
      "field_name": "ngay_ban_hanh",
      "custom_comment": "Ngày ban hành nghị quyết"
    },
    {
      "field_name": "trang_thai",
      "custom_comment": "Trạng thái xử lý: 1 = chờ duyệt, 2 = đã duyệt, 3 = từ chối"
    }
  ]
}
```

| Field | Kiểu | Bắt buộc | Ghi chú |
|---|---|---|---|
| `ds_id` | int | ✔ | Id nguồn dữ liệu trong SQLBot |
| `table_name` | string | ✔ | Tên bảng chứa các cột, khớp chính xác, phân biệt hoa thường |
| `fields` | array | ✔ | Ít nhất 1 phần tử, tên cột không được lặp |
| `fields[].field_name` | string | ✔ | Tên cột thật trong database, khớp chính xác, phân biệt hoa thường |
| `fields[].custom_comment` | string | ✔ | Chú thích mới. Chuỗi rỗng `""` = **xóa** chú thích đang có |

**Ngữ nghĩa:** như [§3](#3-post-datasourceedittablecommentsbyname) — cột không liệt kê thì giữ
nguyên, chỉ chú thích bị ghi, cột phải đã đồng bộ vào SQLBot.

**Response 200**

```json
{"code": 0, "data": {"fields_updated": 2}, "msg": null}
```

**Response 404** — body luôn có đủ cả hai danh sách để client parse một khuôn duy nhất.

Tên bảng không khớp:

```json
{
  "message": "Table not found in datasource",
  "tables_not_found": ["bang_khong_ton_tai"],
  "fields_not_found": []
}
```

Tên bảng đúng nhưng có tên cột không khớp (không cột nào được ghi):

```json
{
  "message": "Some fields not found in table",
  "tables_not_found": [],
  "fields_not_found": ["cot_khong_ton_tai"]
}
```

**Response 422** — như [§3](#3-post-datasourceedittablecommentsbyname).

---

## 5. `POST /datasource/editTableStatusByName`

Bật/tắt **nhiều bảng** của một nguồn dữ liệu trong một lời gọi.

Tắt một bảng nghĩa là **loại nó khỏi ngữ cảnh gửi cho mô hình ngôn ngữ**: bảng vẫn còn nguyên trong
SQLBot cùng chú thích và cấu hình quan hệ, chỉ là không được dùng để trả lời câu hỏi nữa. Bật lại là
thao tác đối xứng, không mất gì. Dùng khi một bảng chưa sẵn sàng cho người dùng cuối, dữ liệu đang
sai, hoặc muốn thu hẹp phạm vi để mô hình chọn bảng chính xác hơn.

**Request**

```json
{
  "ds_id": 1,
  "tables": [
    {"table_name": "NQ01_CE5BE0_7E59_row_values", "checked": true},
    {"table_name": "DB01_978A87_DACC_row_values", "checked": false}
  ]
}
```

| Field | Kiểu | Bắt buộc | Ghi chú |
|---|---|---|---|
| `ds_id` | int | ✔ | Id nguồn dữ liệu trong SQLBot |
| `tables` | array | ✔ | Ít nhất 1 phần tử, tên bảng không được lặp |
| `tables[].table_name` | string | ✔ | Tên bảng thật trong database, khớp **chính xác**, phân biệt hoa thường |
| `tables[].checked` | bool | ✔ | `true` = bảng được dùng cho hỏi đáp, `false` = không |

**Ngữ nghĩa:**

- Bảng **không được liệt kê** thì **giữ nguyên** trạng thái. Đã liệt kê thì phải nói rõ `checked`,
  không có giá trị mặc định.
- Endpoint chỉ ghi trạng thái. **Chú thích không bị đụng tới** — không cần gửi lại chú thích đang có.
- Ghi lại đúng trạng thái sẵn có là hợp lệ, không có tác dụng gì (idempotent).
- **Được phép tắt hết mọi bảng.** Khi đó nguồn dữ liệu không còn bảng nào để trả lời câu hỏi; hệ
  thống không chặn, hãy tự kiểm `enabled_count` trong response.

**Response 200**

```json
{"code": 0, "data": {"tables_updated": 2, "enabled_count": 5}, "msg": null}
```

| Field | Ý nghĩa |
|---|---|
| `tables_updated` | Số bảng đã ghi, dùng để đối soát với số phần tử đã gửi |
| `enabled_count` | Tổng số bảng **đang bật** của nguồn sau lời gọi này — kể cả bảng không nằm trong request |

**Response 404** — có ít nhất một tên bảng không khớp. Body liệt kê đủ các tên hỏng, và **không
bảng nào được ghi**, kể cả những bảng có tên đúng trong cùng request:

```json
{
  "message": "Some tables not found in datasource",
  "tables_not_found": ["bang_khong_ton_tai"]
}
```

**Response 422** — như [§3](#3-post-datasourceedittablecommentsbyname).

**Khác biệt so với việc chọn bảng đồng bộ:** endpoint này không thêm hay xóa bảng khỏi SQLBot. Bảng
phải đã được đồng bộ trước; đổi hẳn tập bảng được đồng bộ là chức năng khác, không nằm trong tài
liệu này.

---

## 6. Ví dụ luồng hoàn chỉnh

Nạp chú thích cho một nguồn dữ liệu có 2 bảng — tổng cộng **3 lời gọi**:

**Bước 1** — ghi chú thích cả 2 bảng trong một lời gọi

```
POST /datasource/editTableCommentsByName
body: { "ds_id": 1, "tables": [ {2 phần tử như ví dụ §3} ] }
→ {"code": 0, "data": {"tables_updated": 2}, "msg": null}
```

**Bước 2, 3** — với từng bảng, ghi chú thích các cột của nó

```
POST /datasource/editFieldCommentsByName
body: { "ds_id": 1, "table_name": "NQ01_CE5BE0_7E59_row_values", "fields": [...] }
→ {"code": 0, "data": {"fields_updated": 20}, "msg": null}

POST /datasource/editFieldCommentsByName
body: { "ds_id": 1, "table_name": "DB01_978A87_DACC_row_values", "fields": [...] }
→ {"code": 0, "data": {"fields_updated": 25}, "msg": null}
```

Chú thích có hiệu lực với các câu hỏi phát sinh **sau** lời gọi vài giây (hệ thống cập nhật lại
ngữ cảnh chọn bảng ở luồng nền); không có endpoint báo trạng thái của bước đó.

Khi chỉ cần sửa lẻ một chú thích, vẫn dùng đúng hai endpoint này với mảng 1 phần tử.

Bật/tắt bảng ([§5](#5-post-datasourceedittablestatusbyname)) là luồng độc lập, gọi lúc nào cũng
được và không cần nhắc lại chú thích.

---

## 7. Gợi ý viết chú thích

Chú thích là văn bản tự do gửi thẳng cho mô hình ngôn ngữ, nên chất lượng câu chữ ảnh hưởng trực
tiếp tới chất lượng câu SQL sinh ra.

**Với bảng** — nói rõ *một dòng đại diện cho cái gì*, vì đó là thứ quyết định mô hình chọn `COUNT(*)`
hay `COUNT(DISTINCT ...)`:

> "Danh mục các Nghị quyết. Mỗi dòng là một nghị quyết. Dữ liệu trải dài từ năm 2012 đến 2026."

**Với cột** — nói nghĩa nghiệp vụ, kèm đơn vị và tập giá trị nếu có:

> "Ngày ban hành nghị quyết"
> "Trạng thái xử lý: 1 = chờ duyệt, 2 = đã duyệt, 3 = từ chối"
> "Tổng chi ngân sách, đơn vị: triệu đồng"

Nên tránh:

- Chép lại tên cột (`custom_comment: "ngay_ban_hanh"`) — không thêm thông tin nào.
- Mô tả kiểu dữ liệu (`"kiểu date"`) — mô hình đã biết kiểu của cột.
- Bỏ trống các cột khóa và cột mã. Chính chúng là chỗ mô hình hay đoán sai nhất.
