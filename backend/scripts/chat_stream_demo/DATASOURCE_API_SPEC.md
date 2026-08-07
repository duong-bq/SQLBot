# Đặc tả API nguồn dữ liệu SQLBot

Dành cho hệ thống bên thứ ba cần tạo, nạp tri thức và quản lý nguồn dữ liệu (datasource) qua API.

Phạm vi: 21 endpoint làm việc với database quan hệ.

---

## 1. Quy ước chung

**Base URL:** `https://<host>/api/v1`

**Xác thực:** header `X-SQLBOT-TOKEN: Bearer <jwt>`, lấy bằng `POST /login/access-token`
(form-urlencoded). Toàn bộ endpoint dưới đây yêu cầu tài khoản có quyền `ws_admin`.

**Response thành công:**

```json
{"code": 0, "data": <kết quả>, "msg": null}
```

**Response lỗi:** không có vỏ bọc.

| HTTP | Body | Ý nghĩa |
|---|---|---|
| 401 | `"Authentication invalid【...】"` | Token thiếu, sai định dạng, hoặc hết hạn |
| 422 | `"<thông điệp>"` hoặc `{"detail":[...]}` | Sai tham số |
| 500 | `"<thông điệp>"` | Mọi lỗi còn lại |

⚠ Không có 403 và 404. Thiếu quyền hoặc id không tồn tại đều trả **500** với cùng chuỗi
`"Access denied or resource not found!"`. Thông điệp lỗi thay đổi theo `Accept-Language` — đừng
string-match để điều khiển logic.

⚠ 422 có hai dạng body: chuỗi JSON trần (lỗi do SQLBot ném) hoặc object `{"detail":[...]}` (lỗi do
FastAPI bắt). Client phải xử lý được cả hai.

---

## 2. Bản đồ endpoint

**Tạo nguồn** ([§3](#3-tạo-nguồn-dữ-liệu)) — chưa có `ds_id`, gửi cấu hình kết nối ở mỗi bước:

| | Endpoint |
|---|---|
| 3.1 | Cấu hình kết nối (`type` và `configuration`) |
| 3.2 | `POST /datasource/check` |
| 3.3 | `POST /datasource/getSchemaByConf` |
| 3.4 | `POST /datasource/getTablesByConf` |
| 3.5 | `POST /datasource/add` |

**Đọc thông tin nguồn đã tạo** ([§4](#4-đọc-thông-tin-nguồn-đã-tạo)):

| | Endpoint |
|---|---|
| 4.1 | `GET /datasource/list` |
| 4.2 | `GET /datasource/get/{ds_id}` |
| 4.3 | `GET /datasource/check/{ds_id}` |
| 4.4 | `GET /datasource/tableList/{ds_id}` |
| 4.5 | `GET /datasource/fieldList/{table_id}` |
| 4.6 | `GET /datasource/previewData/{ds_id}` |

**Nạp chú thích** ([§5](#5-nạp-chú-thích)) — quyết định chất lượng SQL sinh ra:

| | Endpoint |
|---|---|
| 5.1 | `POST /datasource/editLocalComment` |
| 5.2 | `POST /datasource/editTable` |
| 5.3 | `POST /datasource/editField` |

**Quan hệ giữa bảng** ([§6](#6-quan-hệ-giữa-bảng)):

| | Endpoint |
|---|---|
| 6.1 | `GET /table_relation/get/{ds_id}` |
| 6.2 | `POST /table_relation/save/{ds_id}` |

**Thay đổi nguồn đã tạo** ([§7](#7-thay-đổi-nguồn-đã-tạo)):

| | Endpoint |
|---|---|
| 7.1 | `GET /datasource/getTables/{ds_id}` |
| 7.2 | `GET /datasource/getFields/{ds_id}/{table_name}` |
| 7.3 | `POST /datasource/chooseTables/{ds_id}` |
| 7.4 | `POST /datasource/syncFields/{table_id}` |
| 7.5 | `POST /datasource/update` |
| 7.6 | `POST /datasource/delete/{ds_id}/{name}` |

Thứ tự tích hợp tối thiểu để chatbot trả lời được: **3.2 → 3.3 → 3.4 → 3.5 → 6.2**.

---

## 3. Tạo nguồn dữ liệu

Ba bước đầu (3.2–3.4) không ghi gì vào database, gọi lại bao nhiêu lần cũng được. Server không lưu
state giữa các bước — client gửi lại cấu hình kết nối ở mỗi bước.

### 3.1. Cấu hình kết nối

Thông tin kết nối nằm ở **hai trường ngang hàng nhau** trong body: `type` chọn loại DB,
`configuration` là object chứa tham số kết nối. Cả bốn endpoint của §3 và endpoint 7.5 đều dùng
chung cấu trúc này.

```json
{
  "type": "pg",
  "configuration": {"host": "10.0.0.15", "port": 5432, "username": "readonly_user",
                    "password": "...", "database": "kho_du_lieu", "dbSchema": "public"}
}
```

**Giá trị `type`:**

| `type` | Tên | Có bước chọn schema |
|---|---|---|
| `pg` | PostgreSQL | ✔ |
| `mysql` | MySQL | ✘ |
| `sqlServer` | Microsoft SQL Server | ✔ |
| `oracle` | Oracle | ✔ |
| `dm` | DM | ✔ |
| `kingbase` | Kingbase | ✔ |
| `redshift` | AWS Redshift | ✔ |
| `ck` | ClickHouse | ✘ |
| `doris` | Apache Doris | ✘ |
| `starrocks` | StarRocks | ✘ |
| `hive` | Apache Hive | ✘ |
| `es` | Elasticsearch | ✘ |

Cột cuối quyết định hai chuyện: có gọi 3.3 hay không, và `configuration` dùng `dbSchema` hay
`database` làm namespace chứa bảng.

Sai `type` → 500 `"Invalid db type: <giá trị>"`.

**Các field của `configuration`:**

| Field | Kiểu | Áp dụng | Mặc định |
|---|---|---|---|
| `host` | string | mọi loại | |
| `port` | int | mọi loại | |
| `username` | string | mọi loại | |
| `password` | string | mọi loại | |
| `database` | string | mọi loại | Với MySQL/Doris/StarRocks đây là namespace chứa bảng |
| `dbSchema` | string | pg, sqlServer, oracle, dm, redshift, kingbase | Lấy từ 3.3 |
| `extraJdbc` | string | mọi loại | `""` |
| `timeout` | int | mọi loại | `30` (giây) |
| `ssl` | bool | mọi loại | `false` |
| `poolSize` | int | mọi loại | `5` |
| `lowVersion` | bool | mysql | `false` |
| `mode` | string | oracle | `service_name` hoặc `sid` |
| `driver` | string | oracle, sqlServer | |

Sai tên field hoặc sai kiểu → 422, body là chuỗi:

```
"Unknown configuration field(s): hostname. Allowed: database, dbSchema, driver, ..."
"Invalid configuration: 1 validation error for DatasourceConf\nport\n  Input should be a valid integer..."
```

**Lưu ý:**

- ⚠ `configuration` trong **response** là chuỗi ở dạng lưu trữ nội bộ, không phải object đã gửi.
  Không parse nó.
- ⚠ Chuỗi đó chứa mật khẩu DB và có mặt trong response của 4.1 và 4.2. Chỉ gọi qua HTTPS, không ghi
  log body ở gateway/proxy.

### 3.2. `POST /datasource/check`

Thử kết nối tới DB nguồn theo cấu hình gửi lên.

**Request**

| Field | Kiểu | Bắt buộc |
|---|---|---|
| `type` | string | ✔ |
| `configuration` | object | ✔ |

```json
{"type": "pg", "configuration": {"host": "10.0.0.15", "port": 5432, "username": "readonly_user",
                                 "password": "...", "database": "kho_du_lieu"}}
```

**Response**

```json
{"code": 0, "data": true, "msg": null}
```

**Lưu ý:**

- ⚠ Không bao giờ trả `data: false`. Kết nối hỏng → **HTTP 500** kèm thông điệp của driver. Client
  phải bắt 500, đừng chờ `data === false`.
- Bước này là bắt buộc: 3.5 không tự kiểm tra kết nối.

### 3.3. `POST /datasource/getSchemaByConf`

Liệt kê schema của DB nguồn để người dùng chọn. Bỏ qua với MySQL, Doris, StarRocks, ClickHouse, Hive.

**Request** — giống 3.2.

**Response** — mảng tên schema.

```json
{"code": 0, "data": ["pg_toast", "pg_catalog", "public", "information_schema", "nds", "staging",
                     "pg_temp_185", "pg_toast_temp_185", "..."], "msg": null}
```

**Lưu ý:**
- Giá trị người dùng chọn đưa vào `configuration.dbSchema` cho hai bước sau.

### 3.4. `POST /datasource/getTablesByConf`

Liệt kê bảng trong schema đã chọn.

**Request** — giống 3.2, `configuration` phải có `dbSchema` (hoặc `database` với nhóm MySQL).

**Response**

```json
{"code": 0, "data": [{"tableName": "nhan_vien", "tableComment": "Danh sách nhân viên"},
                     {"tableName": "phong_ban", "tableComment": ""}], "msg": null}
```

**Lưu ý:**

- ⚠ Thiếu `dbSchema` → trả mảng rỗng, không phải "tất cả các bảng".
- Không phân trang, không lọc theo tên; tìm kiếm phải làm ở client.
- Danh sách gồm cả view và materialized view. `tableComment` thường rỗng.

### 3.5. `POST /datasource/add`

Tạo nguồn dữ liệu và đồng bộ metadata các bảng đã chọn.

**Request**

| Field | Kiểu | Bắt buộc | Ghi chú |
|---|---|---|---|
| `name` | string | ✔ | ≤128 ký tự, duy nhất trong workspace |
| `type` | string | ✔ | |
| `configuration` | object | ✔ | |
| `tables` | array | ✔ | Bảng người dùng chọn ở 3.4 |
| `tables[].table_name` | string | ✔ | |
| `tables[].table_comment` | string | | |
| `description` | string | | ≤512 ký tự |

```json
{
  "name": "Kho dữ liệu nhân sự",
  "description": "Dữ liệu nhân sự khối văn phòng",
  "type": "pg",
  "configuration": {"host": "10.0.0.15", "port": 5432, "username": "readonly_user",
                    "password": "...", "database": "kho_du_lieu", "dbSchema": "nhan_su"},
  "tables": [
    {"table_name": "nhan_vien", "table_comment": "Danh sách nhân viên"},
    {"table_name": "phong_ban", "table_comment": ""}
  ]
}
```

**Response** — bản ghi vừa tạo.

```json
{
  "code": 0,
  "data": {
    "id": 7,
    "name": "Kho dữ liệu nhân sự",
    "description": "Dữ liệu nhân sự khối văn phòng",
    "type": "pg",
    "type_name": "PostgreSQL",
    "status": "Success",
    "num": "",
    "oid": 1,
    "configuration": "<dạng lưu trữ nội bộ>",
    "create_time": "2026-08-07T09:12:44",
    "create_by": 1
  },
  "msg": null
}
```

`id` là `ds_id` dùng cho mọi endpoint sau, và cho `POST /chat/question` khi hỏi trên nguồn này.

**Lưu ý:**

- ⚠ `tables[]` dùng **snake_case** (`table_name`), trong khi 3.4 trả về **camelCase** (`tableName`).
  Phải đổi tên trường.
- ⚠ Endpoint không kiểm tra kết nối; `status` luôn là `"Success"` kể cả khi cấu hình sai.
- ⚠ Hỏng ở khâu đồng bộ metadata → nguồn dữ liệu **vẫn được tạo** và không tự dọn. Client nên gọi
  7.6 để dọn. Response lỗi không kèm `id`, nên đặt `name` có hậu tố duy nhất để tra lại qua 4.1.
- ⚠ `num` trong response luôn rỗng. Cần giá trị thật thì đọc lại bằng 4.2.
- `tables: []` được chấp nhận nhưng tạo ra nguồn không dùng được.
- Thời gian chạy tỷ lệ với số bảng và số cột — đặt timeout rộng.

**Lỗi riêng:** 500 `"Name already exists"` khi trùng tên trong workspace.

---

## 4. Đọc thông tin nguồn đã tạo

### 4.1. `GET /datasource/list`

Danh sách nguồn dữ liệu trong workspace của tài khoản hiện tại, sắp theo `name`.

**Request** — không có tham số.

**Response**

```json
{"code": 0, "data": [
  {"id": 1, "name": "Kho dữ liệu nhân sự", "description": "...", "type": "pg",
   "type_name": "PostgreSQL", "status": "Success", "num": "2/442", "oid": 1,
   "create_time": "2026-07-28T17:10:26.290067", "create_by": 1,
   "configuration": "<dạng lưu trữ nội bộ>", "table_relation": [...], "embedding": "[...]"}
], "msg": null}
```

| Field | Ý nghĩa |
|---|---|
| `id` | `ds_id` |
| `type_name` | Tên hiển thị của loại DB, server tự điền |
| `status` | **Luôn** `"Success"`, không phản ánh kết nối thật — muốn biết thì gọi 4.3 |
| `num` | `"<số bảng đã đồng bộ>/<số bảng trong schema>"`, tính lại ở 3.5 và 7.3 |
| `table_relation` | Đồ thị quan hệ, xem [§6](#6-quan-hệ-giữa-bảng) |

**Lưu ý:**

- ⚠ Response rất nặng: `embedding` là vector ~23 KB **cho mỗi nguồn**, cộng toàn bộ `table_relation`.
- Chỉ trả về nguồn thuộc workspace hiện tại của token.

### 4.2. `GET /datasource/get/{ds_id}`

Chi tiết một nguồn.

**Request** — không có body.

**Response** — một object, cấu trúc giống một phần tử của 4.1.

**Lưu ý:** dùng endpoint này để đọc `num` thật sau khi gọi 3.5.

### 4.3. `GET /datasource/check/{ds_id}`

Thử kết nối tới một nguồn **đã lưu**, dùng cấu hình đang lưu trong SQLBot.

**Request** — không có body.

**Response**

```json
{"code": 0, "data": true, "msg": null}
```

**Lưu ý:**

- ⚠ Không bao giờ trả `false`. Kết nối hỏng → 500, giống 3.2.
- Đây là cách duy nhất biết một nguồn còn kết nối được hay không (`status` không dùng được).

### 4.4. `GET /datasource/tableList/{ds_id}`

Danh sách bảng **đã đồng bộ vào Backend AI** (không truy vấn DB nguồn), sắp theo `table_name`.

**Request** — không có body.

**Response**

```json
{"code": 0, "data": [
  {"id": 6, "ds_id": 1, "table_name": "NQ01_row_values", "table_comment": "",
   "custom_comment": "Danh mục các Nghị quyết...", "embedding": "[...]"}
], "msg": null}
```

| Field | Ý nghĩa |
|---|---|
| `id` | `table_id` — tham số cho 4.5, 4.6, §5, 7.4 và là `cell` trong đồ thị quan hệ (§6) |
| `table_comment` | Comment đọc từ DB nguồn, chỉ đọc |
| `custom_comment` | Chú thích do bạn ghi qua §5 — **đây mới là thứ gửi cho LLM** |

**Lưu ý:**

- ⚠ Phân biệt với 7.1: endpoint này trả về bảng **đã chọn** kèm `table_id`; 7.1 trả về **mọi** bảng
  đang có trong DB nguồn nhưng không có id.
- Bỏ qua `embedding` (~23 KB mỗi bảng).

### 4.5. `GET /datasource/fieldList/{table_id}`

Danh sách cột đã đồng bộ của một bảng, sắp theo thứ tự cột trong DB nguồn.

⚠ Tham số đường dẫn là `table_id` (lấy từ 4.4), **không phải** `ds_id`.

**Request** — không có body.

| Query param | Kiểu | Bắt buộc | Ghi chú |
|---|---|---|---|
| `fieldName` | string | | Lọc theo tên cột, khớp kiểu chứa (`LIKE %x%`), không phân biệt hoa thường. Bỏ trống thì trả toàn bộ cột |

```
GET /datasource/fieldList/6
GET /datasource/fieldList/6?fieldName=ngay
```

**Response**

```json
{"code": 0, "data": [
  {"id": 112, "ds_id": 1, "table_id": 6, "field_name": "id", "field_type": "bigint",
   "field_comment": null, "custom_comment": "Khóa định danh dòng dữ liệu", "field_index": 0}
], "msg": null}
```

| Field | Ý nghĩa |
|---|---|
| `id` | `field_id` — là `port` trong đồ thị quan hệ (§6) |
| `field_comment` | Comment đọc từ DB nguồn, chỉ đọc |
| `custom_comment` | Chú thích do bạn ghi qua §5 — **đây mới là thứ gửi cho LLM** |

### 4.6. `GET /datasource/previewData/{ds_id}`

Xem trước dữ liệu thật của một bảng.

**Request**

```json
GET /datasource/previewData/1?table_id=6
```

| Query param | Kiểu | Bắt buộc | Ghi chú |
|---|---|---|---|
| `table_id` | int | ✔ | `table_id` từ 4.4 |

**Response**

```json
{"code": 0, "data": {
  "fields": ["id", "uuid", "so_ky_hieu"],
  "data": [{"id": 1191, "uuid": "f2d1044b-...", "so_ky_hieu": "01/2026/NQ-HĐND"}],
  "sql": "U0VMRUNUICJpZCIsICJ1dWlkIiwg..."
}, "msg": null}
```

**Lưu ý:**

- ⚠ `sql` được **mã hóa base64**, không phải câu SQL trần. Decode nếu cần hiển thị.
- Thiếu `table_id` → 422. `table_id` không tồn tại, hoặc bảng không còn cột nào → **HTTP 200** với
  `{"fields":[],"data":[],"sql":""}`, không phải lỗi.
- Luôn `LIMIT 100`, không phân trang, không đổi được. Không có tham số lọc.

---

## 5. Nạp chú thích

Mỗi bảng và cột có hai trường chú thích:

- `table_comment` / `field_comment` — comment đọc từ DDL của DB nguồn, Backend AI làm mới mỗi lần đồng bộ, chỉ đọc.
- `custom_comment` — chú thích do bạn ghi.

Chỉ `custom_comment` được đưa vào ngữ cảnh gửi cho LLM. Comment gốc trong DB nguồn **không bao giờ**
tới tay LLM; nó chỉ được copy sang `custom_comment` một lần lúc bảng/cột được đồng bộ lần đầu, và
từ đó về sau đồng bộ lại không ghi đè lên nội dung bạn đã sửa.

Với DB không khai comment và tên bảng/cột mã hóa, đây là thứ duy nhất cho LLM biết dữ liệu là gì.
Bỏ trống bước này thì chất lượng sinh SQL giảm rõ rệt.

### 5.1. `POST /datasource/editLocalComment`

Ghi chú thích cho một bảng và các cột của nó trong **một** request. Đây là endpoint nên dùng khi tạo mới datasource.

**Request**

```json
{
  "table": {"id": 6, "custom_comment": "Danh mục các Nghị quyết. Mỗi dòng là một nghị quyết."},
  "fields": [
    {"id": 112, "custom_comment": "Khóa định danh dòng dữ liệu"},
    {"id": 130, "custom_comment": "Số ký hiệu nghị quyết"}
  ]
}
```

| Field | Kiểu | Bắt buộc | Ghi chú |
|---|---|---|---|
| `table.id` | int | ✔ | `table_id` từ 4.4 |
| `table.custom_comment` | string | ✔ | |
| `fields[].id` | int | ✔ | `field_id` từ 4.5 |
| `fields[].custom_comment` | string | ✔ | |

**Response**

```json
{"code": 0, "data": null, "msg": null}
```

**Lưu ý:**

- `fields` có thể chỉ chứa một phần các cột; cột không nhắc tới giữ nguyên.
- Mọi trường khác trong body bị bỏ qua im lặng.
- Sau khi ghi, embedding của bảng và của nguồn được tính lại ở luồng nền.

### 5.2. `POST /datasource/editTable`

Ghi chú thích cho một bảng.

**Request**

```json
{"id": 6, "custom_comment": "Danh mục các Nghị quyết. Mỗi dòng là một nghị quyết."}
```

**Response** — `data: null`.

### 5.3. `POST /datasource/editField`

Ghi chú thích cho một cột.

**Request**

```json
{"id": 112, "custom_comment": "Khóa định danh dòng dữ liệu"}
```

**Response** — `data: null`.

**Lưu ý chung cho 5.2 và 5.3:**

- ⚠ Thiếu `custom_comment` trong body → server ghi `null`, **xóa mất** chú thích đang có. 
- ⚠ `id` không tồn tại → 500 `"'NoneType' object has no attribute 'checked'"`.

---

## 6. Quan hệ giữa bảng

Khai báo các cặp cột dùng để JOIN giữa các bảng của một nguồn dữ liệu, để câu hỏi trải trên nhiều
bảng được trả lời đúng. Cấu hình lưu theo từng nguồn, đọc bằng 6.1 và ghi bằng 6.2.

Cấu hình là **một mảng phần tử gọi là "cell"**, mỗi cell có trường `shape` cho biết nó là loại nào:

| `shape` | Cell này là gì | Bên tích hợp |
|---|---|---|
| `"edge"` | Một quan hệ: cột A của bảng X nối với cột B của bảng Y | **Cần khai** |
| `"er-rect"` | Một hình chữ nhật trên sơ đồ, đại diện một bảng | Chỉ phục vụ sơ đồ trên giao diện SQLBot, xem [6.3](#63-cell-er-rect-chỉ-phục-vụ-sơ-đồ-trên-giao-diện) |

Nói ngắn gọn: quan hệ nằm ở các cell `edge`. Các cell `er-rect` chỉ để vẽ sơ đồ, không mang thông tin
quan hệ nào.

### 6.1. `GET /table_relation/get/{ds_id}`

Đọc cấu hình quan hệ hiện tại của nguồn `ds_id`.

**Request** — không có body, không có query param.

**Response** — `data` là mảng cell. Trả `[]` khi nguồn chưa có cấu hình quan hệ; `ds_id` không tồn
tại cũng trả `[]`, hai trường hợp này không phân biệt được qua response.

```json
{"code": 0, "msg": null, "data": [
  {
    "id": "3f7c1e02-9a45-4f13-8c77-1b0e5d2a6f80",
    "shape": "edge",
    "source": {"cell": 6, "port": "112"},
    "target": {"cell": 8, "port": "245"},
    "attrs": {"line": {"stroke": "#DEE0E3"}},
    "zIndex": 5
  },
  {
    "id": 6,
    "shape": "er-rect",
    "position": {"x": 0, "y": 0},
    "size": {"width": 180, "height": 51},
    "zIndex": 1,
    "visible": true,
    "attrs": {
      "text": {"text": "nhan_vien"},
      "label": {"text": "nhan_vien", "textAnchor": "left", "refX": 34, "refY": 28,
                "textWrap": {"width": 120, "height": 24, "ellipsis": true}}
    },
    "ports": {"items": [
      {"id": 112, "group": "list",
       "attrs": {"portNameLabel": {"text": "phong_ban_id"}, "portTypeLabel": {"text": "integer"}}}
    ]}
  }
]}
```

Thứ tự các cell trong mảng không có ý nghĩa; `edge` và `er-rect` nằm xen kẽ nhau.

**Các trường của cell `edge`:**

| Trường | Kiểu | Ý nghĩa |
|---|---|---|
| `shape` | string | Luôn `"edge"` — dấu hiệu nhận biết đây là một quan hệ |
| `source.cell` | integer | `table_id` của bảng ở đầu thứ nhất. Đối chiếu được với `id` trả về từ 4.4 |
| `source.port` | integer hoặc string | `field_id` của cột ở đầu thứ nhất. Đối chiếu được với `id` trả về từ 4.5. Kiểu tùy cell được tạo ra lúc nào, luôn ép về số trước khi so khớp |
| `target.cell` | integer | `table_id` của bảng ở đầu thứ hai |
| `target.port` | integer hoặc string | `field_id` của cột ở đầu thứ hai, xem ghi chú kiểu ở `source.port` |
| `id` | string | Định danh của cell. Không mang ý nghĩa nghiệp vụ, chỉ để phân biệt các cell với nhau |
| `attrs.line.stroke` | string | Màu đường vẽ trên sơ đồ. **Web backend không cần quan tâm** |
| `zIndex` | integer | Thứ tự chồng lớp khi vẽ. **Web backend không cần quan tâm** |

Một cell `edge` nghĩa là: `source.cell`.`source.port` = `target.cell`.`target.port`. Với ví dụ trên,
cột `field_id=112` của bảng `table_id=6` nối với cột `field_id=245` của bảng `table_id=8`. Quan hệ
không có chiều — đổi chỗ `source` và `target` cho kết quả như nhau.

**Các trường của cell `er-rect`** — toàn bộ khối này chỉ phục vụ việc vẽ sơ đồ trên giao diện SQLBot.
Nếu bên tích hợp chỉ đọc để biết nguồn đang khai những quan hệ nào thì **bỏ qua hết các cell
`er-rect`**, không có thông tin nào ở đây mà cell `edge` chưa có.

| Trường | Kiểu | Ý nghĩa |
|---|---|---|
| `id` | integer | `table_id` của bảng mà hình chữ nhật này đại diện |
| `shape` | string | Luôn `"er-rect"` |
| `ports.items[].id` | integer | `field_id` của một cột, là điểm mà đường quan hệ cắm vào |
| `ports.items[].group` | string | Luôn `"list"`. **Không cần quan tâm** |
| `ports.items[].attrs.portNameLabel.text` | string | Tên cột hiển thị trên sơ đồ. **Không cần quan tâm** |
| `ports.items[].attrs.portTypeLabel.text` | string | Kiểu cột hiển thị trên sơ đồ. **Không cần quan tâm** |
| `position` | object `{x, y}` | Tọa độ hình chữ nhật trên sơ đồ. **Không cần quan tâm** |
| `size` | object `{width, height}` | Kích thước hình chữ nhật. **Không cần quan tâm** |
| `zIndex` | integer | Thứ tự chồng lớp. **Không cần quan tâm** |
| `visible` | boolean | Hiện/ẩn trên sơ đồ. **Không cần quan tâm** |
| `attrs.text.text`, `attrs.label.*` | object | Nhãn tên bảng và cách canh chữ. **Không cần quan tâm** |

**Lưu ý:** vì 6.2 ghi đè toàn bộ, mọi thay đổi gia tăng đều phải theo trình tự `get` → sửa → `save`.

### 6.2. `POST /table_relation/save/{ds_id}`

Ghi đè toàn bộ cấu hình quan hệ của nguồn `ds_id`.

**Request** — body là **mảng JSON trần**, không bọc trong object. Mỗi phần tử là một cell. Gửi `[]`
để xóa sạch cấu hình quan hệ.

**Chỉ cần gửi các cell `edge`. Không cần gửi lại thông tin bảng** — không cần cell `er-rect`, không
cần tên bảng, không cần tên cột. Mỗi quan hệ chỉ cần 4 id lấy từ 4.4 và 4.5. Payload đủ dùng cho một
quan hệ ngắn đúng như sau:

```json
[
  {
    "id": "rel-6-112-8-245",
    "shape": "edge",
    "source": {"cell": 6, "port": 112},
    "target": {"cell": 8, "port": 245}
  }
]
```

| Trường | Bắt buộc | Kiểu | Ý nghĩa |
|---|---|---|---|
| `shape` | ✔ | string | Phải đúng `"edge"`. Sai hoặc thiếu thì quan hệ không có hiệu lực |
| `source.cell` | ✔ | **integer** | `table_id` của bảng ở đầu thứ nhất, lấy từ 4.4 |
| `source.port` | ✔ | integer hoặc string | `field_id` của cột ở đầu thứ nhất, lấy từ 4.5 |
| `target.cell` | ✔ | **integer** | `table_id` của bảng ở đầu thứ hai |
| `target.port` | ✔ | integer hoặc string | `field_id` của cột ở đầu thứ hai |
| `id` | — | string | Định danh cell, xem quy tắc đặt bên dưới. Không gửi cũng được |

Các trường trình bày (`attrs`, `zIndex`) không cần gửi.

**Cách đặt `id` cho cell `edge`**

Server không đọc `id`, không kiểm tra, không tự sinh hộ: mảng được lưu nguyên trạng và 6.1 trả về
đúng những gì đã gửi. Đã đo: cạnh không có `id` vẫn lưu 200 và đọc lại vẫn không có `id`; hai cạnh
trùng `id` cũng lưu 200.

Nhưng màn hình sơ đồ trên giao diện SQLBot dùng **chung một không gian định danh cho cả cell `edge`
lẫn cell `er-rect`**, mà `id` của cell `er-rect` chính là `table_id` (5, 6, 8…). Vì vậy:

- ⚠ Đừng đặt `id` cạnh là số nguyên nhỏ hoặc trùng một `table_id` nào đó.
- ⚠ Đừng để hai cạnh trùng `id` nhau.

Khuyến nghị dùng UUID, hoặc một chuỗi sinh theo công thức từ chính 4 id của quan hệ (ví dụ
`rel-6-112-8-245`) nếu muốn dựng lại mảng nhiều lần mà `id` vẫn ổn định.

⚠ **`cell` phải là số nguyên, không được là chuỗi.** Đã đo trên hệ thống thật:

| `source.cell` / `target.cell` | Kết quả |
|---|---|
| Cả hai đều là số | Quan hệ có hiệu lực ✔ |
| Cả hai đều là chuỗi | **Quan hệ mất hiệu lực hoàn toàn** — API vẫn trả 200, không có lỗi, không có log |
| Một số một chuỗi | Quan hệ có hiệu lực một phần, kết quả không lường trước được |

`port` thì số hay chuỗi đều chạy đúng (đã đo) — vẫn nên gửi số cho nhất quán.

**Response**

```json
{"code": 0, "data": true, "msg": null}
```

**Lỗi 422** — chỉ hai trường hợp về hình dạng body, kiểm tra dừng ở mức "mảng của các object":

| Body gửi lên | `detail[].msg` |
|---|---|
| Không phải mảng, ví dụ `{"cells": []}` | `Input should be a valid list` |
| Mảng nhưng phần tử không phải object, ví dụ `[1,2,3]` | `Input should be a valid dictionary` |

**Lưu ý:**

- ⚠ Server **không kiểm tra** nội dung: `table_id`/`field_id` không tồn tại, hoặc cột thuộc bảng khác
  với bảng đã khai, vẫn lưu thành công. Sai sót chỉ lộ ra khi người dùng đặt câu hỏi và nhận SQL sai.
  Dựng mảng cell từ chính id vừa đọc về ở 4.4 + 4.5.
- ⚠ `table_id` và `field_id` đổi sau mỗi lần bảng bị xóa rồi thêm lại qua 7.3. Đừng cache id; đọc lại
  4.4 + 4.5 trước mỗi lần ghi quan hệ.
- ⚠ Ghi đè toàn bộ, không có thao tác thêm/xóa từng quan hệ. Hai người sửa cùng lúc thì người sau ghi
  đè người trước.
- ⚠ Ghi đè cả các cell `er-rect` đang có. Nếu bên tích hợp gửi mảng chỉ gồm `edge` thì sơ đồ trên
  giao diện SQLBot sẽ trống — xem 6.3 nếu điều đó là vấn đề.
- Chỉ khai những quan hệ thật sự dùng để JOIN. Mỗi quan hệ khai thừa đều làm giảm chất lượng câu trả
  lời, vì bảng ở đầu kia bị kéo theo vào ngữ cảnh xử lý câu hỏi.
- Khi tạo nguồn ở 3.5 và khi đổi danh sách bảng ở 7.3, nếu cấu hình quan hệ đang rỗng thì server tự
  sinh sẵn quan hệ từ khóa ngoại khai báo trong DB nguồn. Cấu hình đã có sẵn thì không bị đụng tới.
  Vì vậy mảng chỉ có `er-rect` mà không có `edge` nào nghĩa là **nguồn này không khai quan hệ nào**,
  chứ không phải "chưa từng cấu hình".

### 6.3. Cell `er-rect` — chỉ phục vụ sơ đồ trên giao diện

**Bỏ qua toàn bộ mục này nếu bên tích hợp không cần màn hình sơ đồ quan hệ của SQLBot hiển thị đúng.**
Quan hệ vẫn có hiệu lực đầy đủ khi mảng chỉ gồm cell `edge`.

Nếu có cần, cách an toàn nhất là **không tự dựng cell `er-rect`**: gọi 6.1, giữ nguyên toàn bộ cell
`er-rect` đọc về, chỉ thay các cell `edge`, rồi gửi cả mảng lên 6.2.

Chỉ khi cấu hình đang rỗng mà vẫn muốn có sơ đồ thì mới cần tự tạo, theo mẫu tối thiểu sau:

```json
{
  "id": 6,
  "shape": "er-rect",
  "position": {"x": 0, "y": 0},
  "size": {"width": 180, "height": 51},
  "attrs": {"label": {"text": "nhan_vien"}},
  "ports": {"items": [
    {"id": 112, "group": "list",
     "attrs": {"portNameLabel": {"text": "phong_ban_id"}, "portTypeLabel": {"text": "integer"}}}
  ]}
}
```

- `id` là `table_id`, `ports.items[].id` là `field_id` — phải trùng với id mà cell `edge` tham chiếu,
  nếu không đường quan hệ sẽ không cắm vào đâu cả.
- ⚠ `position` bắt buộc phải có. Cell `er-rect` thiếu trường này làm màn hình sơ đồ lỗi hiển thị.
- Mỗi bảng đặt cách nhau khoảng 320px theo trục x và 400px theo trục y là đủ để không chồng lên nhau;
  người dùng có thể tự kéo lại vị trí trên giao diện.

---

## 7. Thay đổi nguồn đã tạo

7.3–7.6 là các thao tác ghi. Hai endpoint đầu chỉ đọc, nhưng đọc từ **DB nguồn** chứ không phải từ
metadata SQLBot đang lưu (đó là việc của §4) — chúng đóng vai trò chuẩn bị cho 7.3 và 7.4, giống
quan hệ giữa 3.3/3.4 và 3.5.

### 7.1. `GET /datasource/getTables/{ds_id}`

Liệt kê **mọi** bảng đang có trong DB nguồn, đọc trực tiếp từ đó. Dùng khi muốn đổi tập bảng đồng bộ
mà không phải giữ lại mật khẩu DB để gọi 3.4.

**Request** — không có body.

**Response** — giống 3.4.

```json
{"code": 0, "data": [{"tableName": "nhan_vien", "tableComment": ""}], "msg": null}
```

**Lưu ý:**

- ⚠ Không phân trang. Đo thật trên một DB nghiệp vụ: 537 bảng trong một response.
- Không có `table_id` — muốn biết bảng nào đã đồng bộ thì đối chiếu với 4.4 theo tên.

### 7.2. `GET /datasource/getFields/{ds_id}/{table_name}`

Liệt kê cột của một bảng, đọc trực tiếp từ DB nguồn.

**Request** — không có body. `table_name` phải URL-encode nếu có ký tự đặc biệt.

**Response**

```json
{"code": 0, "data": [{"fieldName": "id", "fieldType": "bigint", "fieldComment": null}], "msg": null}
```

**Lưu ý:** so sánh kết quả này với 4.5 để phát hiện DDL đã đổi từ lần đồng bộ cuối. (Đo thật trên
một nguồn đang chạy: DB nguồn có 26 cột trong khi SQLBot đang lưu 20 — chênh lệch xử lý bằng 7.4.)

### 7.3. `POST /datasource/chooseTables/{ds_id}`

Đổi tập bảng được đồng bộ của một nguồn đã có.

**Request** — mảng JSON trần:

```json
[
  {"table_name": "nhan_vien", "table_comment": "Danh sách nhân viên"},
  {"table_name": "phong_ban", "table_comment": ""}
]
```

**Response** — `data: null`. Muốn kiểm tra kết quả thì gọi 4.4.

**Lưu ý:**

- ⚠ **Thay thế toàn bộ**, không phải thêm. Bảng không có trong mảng sẽ bị **xóa** cùng metadata cột
  của nó. Thêm một bảng vẫn phải gửi lại cả những bảng đang có. Mảng rỗng xóa sạch.
- ⚠ Bảng bị xóa rồi thêm lại sẽ mang `table_id` mới và **mất toàn bộ chú thích** của bảng lẫn các
  cột. Gọi lại với cùng danh sách thì an toàn — chú thích được giữ.
- ⚠ Quan hệ giữa các bảng **không** được cập nhật theo, phải khai lại bằng 6.2.
- ⚠ Không có transaction bao trùm: hỏng giữa chừng để lại trạng thái dở dang. Xử lý bằng cách đọc
  4.4 rồi gọi lại — thao tác này lặp lại được.
- Khác 3.5: endpoint này **có** kiểm tra kết nối, hỏng thì 500 và không thay đổi gì.
- Thời gian chạy tỷ lệ với số bảng và số cột — đặt timeout rộng, đừng gọi trong request có người
  dùng đang chờ.

### 7.4. `POST /datasource/syncFields/{table_id}`

Đọc lại danh sách cột của **một** bảng từ DB nguồn: thêm cột mới, xóa cột đã biến mất, cập nhật kiểu
dữ liệu. Chú thích của các cột còn lại được giữ nguyên.

⚠ Tham số đường dẫn là `table_id` (lấy từ 4.4), **không phải** `ds_id`.

**Request** — không có body.

**Response** — `data: null`.

**Lưu ý:**

- ⚠ Bảng không còn tồn tại trong DB nguồn → 500 `"Table not exist"`. Trường hợp đó dùng 7.3.
- Chỉ tác động tới cột. Muốn thêm/bớt **bảng** thì dùng 7.3.

### 7.5. `POST /datasource/update`

Sửa tên, mô tả hoặc cấu hình kết nối của một nguồn đã có.

**Request** — cập nhật từng phần, chỉ những field gửi lên mới bị ghi. `configuration` theo đúng cấu
trúc ở [3.1](#31-cấu-hình-kết-nối).

| Field | Kiểu | Bắt buộc |
|---|---|---|
| `id` | int | ✔ |
| `name` | string | |
| `description` | string | |
| `configuration` | object | |

```json
{"id": 1, "name": "Kho dữ liệu nhân sự", "configuration": {"host": "10.0.0.20", "port": 5432,
 "username": "readonly_user", "password": "...", "database": "kho_du_lieu", "dbSchema": "nhan_su"}}
```

**Response** — chỉ gồm các field đã gửi, cộng `status`. Không phải bản ghi đầy đủ.

```json
{"code": 0, "data": {"id": 1, "name": "Kho dữ liệu nhân sự", "status": "Success"}, "msg": null}
```

**Lưu ý:**

- ⚠ Không kiểm tra kết nối và không đồng bộ lại bảng. Đổi `host`/`database` sang một DB khác thì
  metadata bảng cũ vẫn giữ nguyên — phải gọi 3.2 trước và 7.3 sau.
- ⚠ Gửi `configuration` là thay **toàn bộ** object, không merge từng field.
- ⚠ Đừng gửi `type`: server ghi đè loại DB nhưng không tính lại `type_name`, để lại bản ghi không
  nhất quán. Đổi loại DB thì xóa nguồn rồi tạo lại.
- Trùng tên trong workspace → 500 `"Name already exists"`.

### 7.6. `POST /datasource/delete/{ds_id}/{name}`

Xóa một nguồn dữ liệu cùng toàn bộ metadata bảng, cột và chú thích.

**Request** — không có body. `name` chỉ dùng để ghi log, nhưng **bắt buộc** phải có trong đường dẫn;
URL-encode nếu có dấu hoặc khoảng trắng.

**Response**

```json
{"code": 0, "data": {"message": "Datasource with ID 7 deleted successfully."}, "msg": null}
```

**Lưu ý:**

- ⚠ Không hoàn tác được và không hỏi lại. Chú thích đã nạp ở §5 mất theo.
- Lịch sử hội thoại đã hỏi trên nguồn này không bị xóa nhưng sẽ không hỏi tiếp được.

---

