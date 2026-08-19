# Đặc tả API nguồn dữ liệu SQLBot

Dành cho hệ thống bên thứ ba cần tạo, nạp tri thức và quản lý nguồn dữ liệu (datasource) qua API.

Phạm vi: 26 endpoint. Nguồn dữ liệu là database quan hệ (§3–§7) hoặc file Excel/CSV ([§8](#8-tạo-nguồn-dữ-liệu-từ-file-excel--csv), [§9](#9-tạo-nguồn-excelcsv-bằng-một-lời-gọi)).

---

## 1. Quy ước chung

**Base URL:** `https://<host>/api/v1`

**Xác thực:** header `X-SQLBOT-TOKEN: Bearer <jwt>`, lấy bằng `POST /login/access-token`
(form-urlencoded). Toàn bộ endpoint dưới đây yêu cầu tài khoản có quyền `ws_admin`.

**Response thành công:** mọi mã 2xx đều được bọc cùng một vỏ, kể cả `202` của
[9.2](#92-post-datasourcecreatefromexcelasync).

```json
{"code": 0, "data": <kết quả>, "msg": null}
```

**Response lỗi:** không có vỏ bọc.

| HTTP | Body | Ý nghĩa |
|---|---|---|
| 400 | `"<thông điệp>"` | Đầu vào sai ở mức nhìn ra được ngay — chỉ [§9](#9-tạo-nguồn-excelcsv-bằng-một-lời-gọi) dùng |
| 401 | `"Authentication invalid【...】"` | Token thiếu, sai định dạng, hoặc hết hạn |
| 409 | `{"code": "...", ...}` | Xung đột tên nguồn dữ liệu — chỉ [9.2](#92-post-datasourcecreatefromexcelasync) dùng |
| 422 | `"<thông điệp>"` hoặc `{"detail":[...]}` | Sai tham số |
| 500 | `"<thông điệp>"` | Mọi lỗi còn lại |

⚠ Trừ §9, các endpoint còn lại **không có 403 và 404**. Thiếu quyền hoặc id không tồn tại đều trả
**500** với cùng chuỗi `"Access denied or resource not found!"`. Thông điệp lỗi thay đổi theo
`Accept-Language` — đừng string-match để điều khiển logic.

⚠ 422 có hai dạng body: chuỗi JSON trần (lỗi do SQLBot ném) hoặc object `{"detail":[...]}` (lỗi do
FastAPI bắt). Client phải xử lý được cả hai.

---

## 2. Bản đồ endpoint

**Tạo nguồn từ database** ([§3](#3-tạo-nguồn-dữ-liệu)) — chưa có `ds_id`, gửi cấu hình kết nối ở mỗi bước:

| | Endpoint |
|---|---|
| 3.1 | Cấu hình kết nối (`type` và `configuration`) |
| 3.2 | `POST /datasource/check` |
| 3.3 | `POST /datasource/getSchemaByConf` |
| 3.4 | `POST /datasource/getTablesByConf` |
| 3.5 | `POST /datasource/add` |

**Tạo nguồn từ file Excel/CSV, ba bước** ([§8](#8-tạo-nguồn-dữ-liệu-từ-file-excel--csv)) — luồng riêng, không dùng 3.2–3.4. Chọn đường này khi cần xác nhận hoặc sửa kiểu từng cột trước lúc nạp:

| | Endpoint |
|---|---|
| 8.1 | `POST /datasource/parseExcel` |
| 8.2 | `POST /datasource/importToDb` |
| 8.3 | `POST /datasource/add` với `type: "excel"` |

**Tạo nguồn từ file Excel/CSV, một lời gọi** ([§9](#9-tạo-nguồn-excelcsv-bằng-một-lời-gọi)) — server tự làm cả ba bước trên:

| | Endpoint | |
|---|---|---|
| 9.1 | `POST /datasource/createFromExcel` | đồng bộ, chờ nạp xong mới trả về |
| 9.2 | `POST /datasource/createFromExcelAsync` | nhận URL của file, trả `202` ngay, tải và nạp ở nền, báo kết quả bằng callback |
| 9.4 | `GET /datasource/excelImportStatus/{ds_id}` | tra trạng thái một lần nạp |

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

Thứ tự tích hợp tối thiểu để chatbot trả lời được:

- nguồn là database: **3.2 → 3.3 → 3.4 → 3.5 → 6.2**
- nguồn là file Excel/CSV: **9.2** (một lời gọi), hoặc **8.1 → 8.2 → 8.3** nếu cần can thiệp kiểu cột

---

## 3. Tạo nguồn dữ liệu

Ba bước đầu (3.2–3.4) không ghi gì vào database, gọi lại bao nhiêu lần cũng được. Server không lưu
state giữa các bước — client gửi lại cấu hình kết nối ở mỗi bước.

Cả mục này chỉ áp dụng cho nguồn là **database quan hệ**. Nguồn là file Excel/CSV đi theo luồng riêng
ở [§8](#8-tạo-nguồn-dữ-liệu-từ-file-excel--csv).

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
- ⚠ Quan hệ giữa các bảng **không** được cập nhật theo, phải khai lại bằng 6.2. Nặng hơn "không cập
  nhật": sơ đồ quan hệ vẫn giữ nguyên các bảng/cột vừa bị gỡ, và những mẩu trỏ vào chỗ trống đó đi
  thẳng vào ngữ cảnh sinh SQL. Đổi tập bảng xong thì **luôn** gọi 6.2 để khai lại quan hệ theo tập
  bảng mới — kể cả khi không có quan hệ nào (gửi mảng rỗng).
- Nếu chỉ cần cập nhật cấu trúc cho khớp DB nguồn (chứ không phải tự chọn tập bảng), dùng bản tin
  `actionType 4` của `AI_SYNC_HOOK_API_SPEC.md` §6: nó tự đọc lại catalog nguồn và tự dọn sơ đồ quan
  hệ, không cần gọi 6.2 sau đó.
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
- ⚠ Với nguồn `type: "excel"`, endpoint này **xóa luôn dữ liệu**: mọi bảng liệt kê trong
  `configuration.sheets` bị drop khỏi kho dữ liệu. Nguồn database chỉ mất khai báo, dữ liệu gốc
  không bị đụng tới.
- Lịch sử hội thoại đã hỏi trên nguồn này không bị xóa nhưng sẽ không hỏi tiếp được.
- Đây cũng là cách dọn nguồn dữ liệu ở trạng thái `Failed` do
  [9.2](#92-post-datasourcecreatefromexcelasync) để lại: nó không hỏi đáp được nhưng vẫn chiếm tên,
  không xóa thì tạo lại đúng tên đó sẽ bị `409`.

---

## 8. Tạo nguồn dữ liệu từ file Excel / CSV

Nguồn dữ liệu loại `excel` được tạo qua ba bước: **8.1 → 8.2 → 8.3**.

Cả ba bước này gộp được thành một lời gọi duy nhất — xem
[§9](#9-tạo-nguồn-excelcsv-bằng-một-lời-gọi). Chỉ đi đường ba bước khi cần xem trước cấu trúc file
hoặc sửa kiểu dữ liệu của cột trước lúc nạp; §9 không cho can thiệp vào kiểu cột.

| Bước | Việc xảy ra |
|---|---|
| 8.1 `parseExcel` | Tải file lên, đọc thử cấu trúc. Chưa tạo bảng, chưa tạo nguồn dữ liệu |
| 8.2 `importToDb` | Nạp dữ liệu thành bảng. Đã ghi dữ liệu, nhưng chưa có nguồn dữ liệu nào |
| 8.3 `add` | Tạo nguồn dữ liệu trỏ tới các bảng ở 8.2. Từ đây chatbot mới trả lời được |

**Không dùng 3.2–3.4 cho luồng này.** Với `type: "excel"` các endpoint đó không mang thông tin gì hữu
ích: `POST /datasource/check` luôn trả `true` bất kể file ra sao, `getSchemaByConf` không có schema để
chọn, còn `getTablesByConf` và 7.1 trả về danh sách bảng của kho dữ liệu nội bộ chứ không phải bảng
của nguồn này. Tương tự, 4.3 `GET /datasource/check/{ds_id}` luôn trả `true` với nguồn `excel`.

Sau bước 8.3 thì mọi endpoint của §4, §5, §6 và 7.3–7.6 dùng bình thường như nguồn database, trừ một
khác biệt ở 7.6 (xem lưu ý tại mục đó).

### 8.1. `POST /datasource/parseExcel`

Tải file lên và trả về cấu trúc đọc được: có những sheet nào, mỗi sheet có cột nào, kiểu dữ liệu đoán
được của từng cột, kèm tối đa 10 dòng đầu để đối chiếu.

Bước này không tạo bảng và không tạo nguồn dữ liệu. Mục đích là để phía tích hợp xác nhận hoặc sửa
kiểu từng cột trước khi nạp thật ở 8.2. Gọi lại bao nhiêu lần cũng được — mỗi lần cho một `filePath`
mới, độc lập với các lần trước.

**Request** — `multipart/form-data`, không phải JSON.

| Field | Kiểu | Bắt buộc | Ghi chú |
|---|---|---|---|
| `file` | file | ✔ | Tên part phải đúng là `file` |

| Ràng buộc trên `file` | Giá trị |
|---|---|
| Phần mở rộng | `.xlsx`, `.xls`, `.csv` (không phân biệt hoa thường) |
| Dung lượng | Không giới hạn |

```bash
curl -X POST 'https://<host>/api/v1/datasource/parseExcel' \
  -H 'X-SQLBOT-TOKEN: Bearer <jwt>' \
  -F 'file=@thu_ngan_sach_2024.xlsx'
```

**Response**

```json
{
  "code": 0,
  "msg": null,
  "data": {
    "filePath": "thu_ngan_sach_2024_9f3c1ab27d.xlsx",
    "data": [
      {
        "sheetName": "Quy1",
        "fields": [
          {"fieldName": "ma_don_vi",  "fieldType": "string"},
          {"fieldName": "ten_don_vi", "fieldType": "string"},
          {"fieldName": "ngay_thu",   "fieldType": "datetime"},
          {"fieldName": "so_tien",    "fieldType": "float"}
        ],
        "data": [
          {"ma_don_vi": "001", "ten_don_vi": "Sở Tài chính", "ngay_thu": "2024-01-15T00:00:00",
           "so_tien": 1250000.0},
          {"ma_don_vi": "002", "ten_don_vi": "Sở Y tế", "ngay_thu": "2024-01-18T00:00:00",
           "so_tien": 870000.0}
        ],
        "rows": 1543
      },
      {"sheetName": "Quy2", "fields": [], "data": [], "rows": 1602}
    ]
  }
}
```

| Field | Kiểu | Ý nghĩa |
|---|---|---|
| `filePath` | string | Định danh của file vừa tải lên. Chuỗi đục — không phân tích, không tự sinh, không sửa. Phải gửi lại nguyên văn ở 8.2. Đây là thứ duy nhất nối 8.1 với 8.2 |
| `data` | array | Mỗi phần tử là một sheet. File `.csv` luôn cho đúng một phần tử |
| `data[].sheetName` | string | Tên sheet. Với file `.csv` giá trị luôn là `"Sheet1"`, không liên quan tới tên file |
| `data[].fields` | array | Danh sách cột, theo đúng thứ tự cột trong file |
| `data[].fields[].fieldName` | string hoặc number | Tên cột lấy từ dòng tiêu đề. Nếu ô tiêu đề trong file là số thì trường này là số chứ không phải chuỗi |
| `data[].fields[].fieldType` | string | Kiểu đoán được: `string`, `int`, `float` hoặc `datetime`. Đây là **đề xuất**, được phép sửa ở 8.2 |
| `data[].data` | array | Tối đa 10 dòng đầu, mỗi dòng là object `{tên cột: giá trị}`. Ô rỗng trả `null`. Chỉ để xem đối chiếu |
| `data[].rows` | number | Tổng số dòng thật của sheet, không phải số phần tử trong `data[].data` |

**Lưu ý:**

- ⚠ Cột ngày tháng trong file `.csv` gần như luôn được đoán là `string`. Đọc cảnh báo ở 8.2 trước khi
  định sửa lại thành `datetime`.
- ⚠ Mỗi lần gọi để lại một bản sao file trên server và bản sao đó không tự xóa, kể cả khi không bao
  giờ gọi 8.2.
- ⚠ Nếu SQLBot chạy nhiều instance sau load balancer, `filePath` chỉ dùng được trên đúng instance đã
  nhận file; khi đó 8.2 có thể trả 400 `"File not found"` một cách ngẫu nhiên. Cần xác nhận mô hình
  triển khai trước khi tích hợp.

**Lỗi riêng:**

| HTTP | Body | Khi nào |
|---|---|---|
| 400 | `"Only support .xlsx/.xls/.csv"` | Phần mở rộng không hợp lệ |
| 500 | thông điệp của trình đọc file | File hỏng hoặc không mở được |

### 8.2. `POST /datasource/importToDb`

Nạp dữ liệu từ file đã tải ở 8.1 vào kho dữ liệu của SQLBot, mỗi sheet thành một bảng. Trả về tên
bảng đã tạo — giá trị bắt buộc phải có để sang 8.3.

Đây là bước **có ghi dữ liệu**. Gọi lại lần nữa với cùng `filePath` tạo ra một bộ bảng mới hoàn toàn
(tên khác), không ghi đè bộ cũ; bộ cũ trở thành bảng mồ côi không thuộc nguồn dữ liệu nào. Chỉ gọi
một lần cho mỗi lần import.

**Request**

| Field | Kiểu | Bắt buộc | Ý nghĩa |
|---|---|---|---|
| `filePath` | string | ✔ | Lấy nguyên văn từ `data.filePath` của 8.1 |
| `sheets` | array | ✔ | Sheet muốn nạp. Chỉ sheet có tên ở đây mới được tạo bảng — bỏ bớt phần tử là cách duy nhất để không import một sheet |
| `sheets[].sheetName` | string | ✔ | Phải trùng khít một `data[].sheetName` của 8.1 |
| `sheets[].fields` | array | ✔ | Khai kiểu cho từng cột |
| `sheets[].fields[].fieldName` | string hoặc number | ✔ | Giữ nguyên giá trị và kiểu như 8.1 trả về |
| `sheets[].fields[].fieldType` | string | ✔ | `string`, `int`, `float` hoặc `datetime`. Giá trị khác bị coi là `string` mà không báo lỗi |

```json
{
  "filePath": "thu_ngan_sach_2024_9f3c1ab27d.xlsx",
  "sheets": [
    {
      "sheetName": "Quy1",
      "fields": [
        {"fieldName": "ma_don_vi",  "fieldType": "string"},
        {"fieldName": "ten_don_vi", "fieldType": "string"},
        {"fieldName": "ngay_thu",   "fieldType": "datetime"},
        {"fieldName": "so_tien",    "fieldType": "float"}
      ]
    }
  ]
}
```

**Response**

```json
{
  "code": 0,
  "msg": null,
  "data": {
    "filename": "thu_ngan_sach_2024_9f3c1ab27d.xlsx",
    "sheets": [
      {"sheetName": "Quy1", "tableName": "excel_Quy1_4d8e2f1a03", "tableComment": "", "rows": 1543},
      {"sheetName": "Quy2", "tableName": "excel_Quy2_b71c05e9d2", "tableComment": "", "rows": 1602}
    ]
  }
}
```

| Field | Kiểu | Ý nghĩa |
|---|---|---|
| `filename` | string | Chính là `filePath` đã gửi lên, trả lại để đối chiếu |
| `sheets` | array | Kết quả từng sheet, cùng thứ tự với request |
| `sheets[].sheetName` | string | Tên sheet đã nạp. Với file `.csv` giá trị này luôn bị đặt lại thành `"Sheet1"` dù request gửi tên khác |
| `sheets[].tableName` | string | Tên bảng thật đã tạo. Do server sinh, không đoán trước được, không lặp lại giữa hai lần gọi |
| `sheets[].tableComment` | string | Luôn rỗng. Chú thích cho bảng đặt sau bằng 5.1 |
| `sheets[].rows` | number | Số dòng đã nạp vào bảng |

Giữ lại **toàn bộ object `data`** này: bước 8.3 cần cả `filename` lẫn nguyên mảng `sheets`.

**Lưu ý:**

- ⚠ `fields` không lọc được cột. Cột nào có trong file thì đều được nạp, kể cả khi không khai trong
  `fields`; khai `fields` chỉ để ép kiểu. Muốn chatbot bỏ qua một cột thì tắt cột đó sau bằng 5.3.
- ⚠ `fieldName` không tồn tại trong file bị bỏ qua im lặng, không có lỗi. Gõ sai tên cột thì hiện
  tượng quan sát được là kiểu dữ liệu của cột đó không đổi như mong muốn.
- ⚠ **Với file `.csv`, không được đặt `fieldType: "datetime"` cho bất kỳ cột nào** — request trả 500
  và không bảng nào được tạo. Cột ngày trong CSV phải để `string`. Hạn chế này không áp dụng cho
  `.xlsx` / `.xls`.
- ⚠ Request nhiều sheet mà hỏng ở sheet giữa chừng: các sheet trước đó **đã tạo bảng và vẫn tồn tại**,
  nhưng response lỗi không cho biết chúng tên gì. Gửi từng sheet một nếu cần kiểm soát chặt.
- ⚠ Bảng đã tạo không tự cập nhật khi file gốc thay đổi. Muốn có dữ liệu mới phải làm lại từ 8.1 và
  tạo bộ bảng mới.

**Lỗi riêng:**

| HTTP | Body | Khi nào |
|---|---|---|
| 400 | `"File not found"` | `filePath` sai, hoặc file không còn trên server |
| 500 | `"…: the dtype datetime64[ns] is not supported for parsing…"` | Đặt `datetime` cho cột của file `.csv` |
| 500 | `"…: Worksheet named '<tên>' not found"` | `sheetName` không có trong file |
| 500 | `"Insert data failed for <tên bảng>: …"` | Lỗi khi ghi vào kho dữ liệu |

### 8.3. `POST /datasource/add` với `type: "excel"`

Cùng endpoint với [3.5](#35-post-datasourceadd), nhưng `configuration` và `tables` lấy từ response
của 8.2.

**Request**

| Field | Kiểu | Bắt buộc | Ghi chú |
|---|---|---|---|
| `name` | string | ✔ | ≤128 ký tự, duy nhất trong workspace |
| `type` | string | ✔ | Cố định `"excel"` |
| `configuration` | object | ✔ | Chỉ gồm `filename` và `sheets` |
| `configuration.filename` | string | ✔ | `data.filename` của 8.2 |
| `configuration.sheets` | array | ✔ | Nguyên mảng `data.sheets` của 8.2, không cắt bớt phần tử, không bỏ field |
| `tables` | array | ✔ | Bảng muốn chatbot nhìn thấy |
| `tables[].table_name` | string | ✔ | Lấy từ `sheets[].tableName` của 8.2 |
| `tables[].table_comment` | string | | |
| `description` | string | | ≤512 ký tự |

`configuration` ở đây **không có** `host`, `port`, `username`, `password`, `database`, `dbSchema` —
những field đó chỉ dành cho nguồn database.

```json
{
  "name": "Thu ngân sách 2024",
  "description": "Số liệu thu ngân sách theo quý",
  "type": "excel",
  "configuration": {
    "filename": "thu_ngan_sach_2024_9f3c1ab27d.xlsx",
    "sheets": [
      {"sheetName": "Quy1", "tableName": "excel_Quy1_4d8e2f1a03", "tableComment": "", "rows": 1543},
      {"sheetName": "Quy2", "tableName": "excel_Quy2_b71c05e9d2", "tableComment": "", "rows": 1602}
    ]
  },
  "tables": [
    {"table_name": "excel_Quy1_4d8e2f1a03", "table_comment": "Thu ngân sách quý 1/2024"},
    {"table_name": "excel_Quy2_b71c05e9d2", "table_comment": "Thu ngân sách quý 2/2024"}
  ]
}
```

**Response** — giống 3.5, với `type: "excel"` và `type_name: "Excel/CSV"`.

**Lưu ý:**

- ⚠ `tables[].table_name` phải là **`tableName`**, không phải `sheetName`. Đưa nhầm `sheetName` thì
  API vẫn trả 200 và nguồn dữ liệu vẫn hiện trong danh sách, nhưng nó không có cột nào và chatbot
  không trả lời được câu hỏi nào về nguồn đó.
- ⚠ Không cắt bớt `configuration.sheets`, kể cả sheet không đưa vào `tables`. Mảng này là thứ duy
  nhất nối nguồn dữ liệu với các bảng đã tạo; sheet nào bị gỡ khỏi mảng thì bảng tương ứng sẽ không
  được dọn khi xóa nguồn dữ liệu (7.6) và ở lại vĩnh viễn trong kho.
- ⚠ Tên bảng do server sinh (`excel_<tên sheet đã lọc>_<hash>`) và bỏ hết dấu tiếng Việt cùng ký tự
  đặc biệt, nên nó gần như không mang nghĩa. Chất lượng SQL sinh ra phụ thuộc nặng vào chú thích, vì
  vậy với nguồn Excel/CSV thì **nạp chú thích ở §5 là bắt buộc**, không còn là tùy chọn.
- Các lưu ý còn lại của 3.5 vẫn áp dụng.

### 8.4. Thêm file mới vào nguồn dữ liệu đã có

Không có endpoint riêng cho việc này. Trình tự phải tự ghép:

1. `8.1` rồi `8.2` với file mới → thu được mảng `sheets` mới.
2. `7.5 POST /datasource/update` với `configuration.sheets` = **mảng cũ ∪ mảng mới**. Đọc mảng cũ ở
   đâu: giữ lại từ lần tạo trước, vì `configuration` trong response của 4.1/4.2 ở dạng lưu trữ nội bộ
   và không parse được.
3. `7.3 POST /datasource/chooseTables/{ds_id}` với danh sách **đầy đủ** bảng cũ và mới. Endpoint này
   thay thế toàn bộ tập bảng — bảng cũ không có trong danh sách sẽ bị gỡ khỏi nguồn dữ liệu cùng mọi
   chú thích đã nạp cho nó.

⚠ Bỏ sót bước 2 thì nguồn dữ liệu vẫn hoạt động, nhưng bảng của file mới sẽ không bị dọn khi xóa
nguồn. Bỏ sót bước 3 thì bảng mới đã tồn tại nhưng chatbot không nhìn thấy.

---


## 9. Tạo nguồn Excel/CSV bằng một lời gọi

Ba bước 8.1 → 8.2 → 8.3 gộp lại thành **một** request. Server tự đọc file, tạo bảng, tạo nguồn dữ
liệu và đăng ký các bảng đó.

Đánh đổi so với §8: không xem trước được cấu trúc và **không sửa được kiểu cột** — kiểu do server tự
đoán. Cần can thiệp kiểu thì phải quay lại đường ba bước.

Hai biến thể, khác nhau ở *cách file tới tay server* và ở *khi nào* response trả về:

| | 9.1 `createFromExcel` | 9.2 `createFromExcelAsync` |
|---|---|---|
| File tới bằng | Bytes trong request (`multipart/form-data`) | **URL** trỏ tới file, server tự tải (`application/json`) |
| Trả về khi | Đã nạp xong toàn bộ | Đã nhận việc, **chưa** tải, chưa nạp |
| HTTP | `200` | `202` |
| Thời gian giữ kết nối | Bằng thời gian tải lên **cộng** thời gian nạp — file lớn có thể vài phút | Cỡ vài chục ms |
| Biết kết quả bằng cách | Đọc luôn response | Nhận [callback](#93-callback-báo-kết-quả) hoặc gọi [9.4](#94-get-datasourceexcelimportstatusds_id) |
| Nguồn dữ liệu dùng được ngay sau khi trả về | ✔ | ✘ — đang ở trạng thái `Importing` |

**Nên dùng 9.2.** 9.1 giữ lại cho tích hợp cũ, và nó có hai chỗ hỏng độc lập nhau. Thứ nhất, nó
buộc client chờ suốt quá trình nạp, mà mọi reverse proxy đứng giữa đều có timeout riêng — file đủ
lớn thì client nhận `504` trong khi server vẫn đang nạp và cuối cùng vẫn tạo ra nguồn dữ liệu, tức
là client không còn suy ra được trạng thái thật từ lỗi mình nhận. Thứ hai, bytes của file phải đi
qua đúng cái kết nối HTTP đang chờ đó; đường truyền giữa hai bên chậm hay chập chờn thì cả request
hỏng và phải tải lại từ đầu.

9.2 gỡ cả hai bằng cách **không nhận bytes nữa**: bên tích hợp đẩy file lên object storage của mình
rồi đưa cho SQLBot một URL ký sẵn (presigned URL). Việc tải nằm ở tiến trình nền, chậm bao lâu cũng
không có ai ngồi chờ, và tải hỏng thì SQLBot tự thử lại.

Cả hai endpoint đều cần quyền **`ws_admin`**.

### 9.1. `POST /datasource/createFromExcel`

**Request** — `multipart/form-data`.

| Field | Kiểu | Bắt buộc | Ghi chú |
|---|---|---|---|
| `file` | file | ✔ | `.xlsx`, `.xls`, `.csv` |
| `name` | string | ✔ | Tên nguồn dữ liệu, phải chưa tồn tại trong workspace |
| `sheetNames` | string (lặp lại) | | Chỉ nạp các sheet này. Bỏ trống = nạp tất cả. File CSV bỏ qua trường này |
| `description` | string | | ≤512 ký tự |

`sheetNames` lặp lại tên field cho mỗi giá trị, không phải mảng JSON:

```bash
curl -X POST 'https://<host>/api/v1/datasource/createFromExcel' \
  -H 'X-SQLBOT-TOKEN: Bearer <jwt>' \
  -F 'file=@thu_ngan_sach_2024.xlsx' \
  -F 'name=Thu ngân sách 2024' \
  -F 'sheetNames=Quy1' \
  -F 'sheetNames=Quy2' \
  -F 'description=Số liệu thu ngân sách theo quý'
```

**Response `200`:**

```json
{
  "code": 0,
  "data": {
    "dsId": 42,
    "name": "Thu ngân sách 2024",
    "tables": [
      {"tableId": 118, "tableName": "excel_Quy1_4d8e2f1a03", "sheetName": "Quy1", "rows": 1543},
      {"tableId": 119, "tableName": "excel_Quy2_b71c05e9d2", "sheetName": "Quy2", "rows": 1602}
    ]
  },
  "msg": null
}
```

### 9.2. `POST /datasource/createFromExcelAsync`

**Request** — `application/json`. Không còn `multipart/form-data`, không còn gửi bytes.

| Field | Kiểu | Bắt buộc | Ghi chú |
|---|---|---|---|
| `fileUrl` | string | ✔ | URL ký sẵn trỏ tới file trên object storage. Xem điều kiện bên dưới |
| `name` | string | ✔ | Tên nguồn dữ liệu, phải chưa tồn tại trong workspace |
| `sheetNames` | string[] | | Mảng JSON. Chỉ nạp các sheet này, bỏ trống = nạp tất cả. File CSV bỏ qua trường này |
| `description` | string | | ≤512 ký tự |

```bash
curl -X POST 'https://<host>/api/v1/datasource/createFromExcelAsync' \
  -H 'X-SQLBOT-TOKEN: Bearer <jwt>' \
  -H 'Content-Type: application/json' \
  -d '{
        "fileUrl": "https://minio.example.com/bucket/thu_ngan_sach_2024.xlsx?X-Amz-Algorithm=...&X-Amz-Signature=...",
        "name": "Thu ngân sách 2024",
        "sheetNames": ["Quy1", "Quy2"],
        "description": "Số liệu thu ngân sách theo quý"
      }'
```

**Bốn điều kiện của `fileUrl`.** Ba điều đầu do bên tích hợp bảo đảm; sai điều nào cũng ăn `400`:

1. **Host phải nằm trong danh sách cho phép của SQLBot.** Đây là cấu hình phía SQLBot, không phải
   thứ đặt trong request — báo trước cho bên vận hành SQLBot host object storage sẽ dùng, trước khi
   gọi lần đầu. Host lạ bị từ chối, kể cả khi URL hoàn toàn hợp lệ.
2. **Đường dẫn phải kết thúc bằng `.xlsx`, `.xls` hoặc `.csv`.** Đuôi file đọc từ phần đường dẫn,
   phần chữ ký nằm sau dấu `?` không tính.
3. **Hạn chữ ký còn ít nhất 1 giờ** kể từ lúc gọi. URL ký sẵn hết hạn sau khi SQLBot đã nhận việc
   thì việc đó hỏng hẳn, không cứu được — mà job có thể phải xếp hàng sau các job khác trước khi
   tới lượt tải. Ký ngắn hơn 1 giờ bị từ chối ngay. **Ký 24 giờ là an toàn nhất.**
4. **File không quá 100 MB.** Vượt trần thì bị từ chối, ở response hoặc ở callback tùy lúc nào
   SQLBot biết được dung lượng thật.

SQLBot chỉ đọc file bằng `GET` và **không đi theo redirect** — URL phải trỏ thẳng tới đối tượng.

**Response `202`:**

```json
{
  "code": 0,
  "data": {"dsId": 42, "name": "Thu ngân sách 2024", "status": "Importing"},
  "msg": null
}
```

`dsId` có ngay từ lúc này và không đổi về sau — dùng nó làm khóa đối chiếu với callback.

Response **không** có `tables`: lúc trả về chưa bảng nào tồn tại.

Nguồn dữ liệu đã nằm trong database nhưng ở trạng thái `Importing`, nên nó **không** xuất hiện trong
danh sách nguồn để hỏi đáp và chatbot chưa trả lời được câu nào về nó. Trạng thái chuyển thành
`Success` hoặc `Failed` khi nạp xong.

**Những lỗi trả luôn trong response** (chưa tạo gì, chưa có `dsId`):

| HTTP | `code` | Khi nào |
|---|---|---|
| `400` | `URL_INVALID` | `fileUrl` rỗng, không phải `http(s)`, hoặc không phân tích được |
| `400` | `URL_HOST_NOT_ALLOWED` | Host của `fileUrl` không nằm trong danh sách cho phép |
| `400` | `URL_BAD_EXTENSION` | Đường dẫn không kết thúc bằng `.xlsx`/`.xls`/`.csv` |
| `400` | `URL_EXPIRED` | Chữ ký đã hết hạn, hoặc còn hạn quá ngắn |
| `400` | `DOWNLOAD_FORBIDDEN` | Object storage trả `403` — chữ ký sai hoặc không đủ quyền |
| `400` | `DOWNLOAD_NOT_FOUND` | Object storage trả `404` — sai đường dẫn hoặc file chưa được đẩy lên |
| `400` | `FILE_TOO_LARGE` | Dung lượng khai báo vượt trần 100 MB |
| `400` | — | `name` rỗng: `Datasource name is required` |
| `409` | `DATASOURCE_NAME_EXISTS` | Tên nguồn dữ liệu đã tồn tại — xem dưới |
| `409` | `DATASOURCE_NAME_LOCKED` | Đang có request khác tạo cùng tên đó — xem dưới |

Thân của các lỗi có `code` là một object, không phải chuỗi:

```json
{"code": "URL_EXPIRED", "message": "fileUrl expires in 120s, needs at least 3600s"}
```

**Ranh giới giữa response và callback đã dịch so với bản `multipart` trước đây.** Trước kia SQLBot
cầm sẵn file ngay trong request nên nó đối chiếu được tên sheet và trả `400` luôn. Nay nó chỉ có
URL, nên **`sheetNames` gõ sai và file hỏng đều về bằng callback**, kèm một nguồn dữ liệu `Failed`
phải dọn. Xem điều 3 của [§9.3](#93-callback-báo-kết-quả).

**Đọc kỹ chỗ này nếu bên tích hợp có logic thử lại:** một hỏng hóc của object storage có thể lộ ra ở
response hoặc ở callback, tùy vào việc SQLBot có kịp biết trước khi nhận việc hay không. SQLBot chỉ
từ chối ngay khi object storage **trả lời dứt khoát** (`403`, `404`, dung lượng vượt trần); nếu nó
timeout, đứt kết nối hay trả `5xx` thì SQLBot vẫn nhận việc và trả `202`, rồi mới báo hỏng bằng
callback nếu tải vẫn không được. Nghĩa là **`202` không bảo đảm file tải được**, và cùng một lỗi
`DOWNLOAD_FAILED` có thể tới bằng cả hai đường — nên hai nhánh xử lý phải dùng chung một hàm.

**Hai dạng `409`**, phân biệt bằng trường `code`:

```json
{"code": "DATASOURCE_NAME_EXISTS", "message": "Datasource name already exists: Thu ngân sách 2024",
 "dsId": 42, "status": "Success"}
```

Gửi trùng tên. Response mang theo `dsId` và `status` của nguồn đang chiếm tên, nên client tự phân
biệt được hai tình huống: `status` là `Importing` nghĩa là **chính request trước của mình** đang
chạy (gọi lại do timeout mạng chẳng hạn) — cứ chờ callback, đừng tạo lại; `Success` nghĩa là tên đã
thuộc về một nguồn khác — phải đổi tên.

```json
{"code": "DATASOURCE_NAME_LOCKED", "message": "Another import for name '...' is in progress"}
```

Hai request cùng tên bắn ra gần như cùng lúc và bên kia chiếm chỗ quá lâu; request này bị trả về
thay vì xếp hàng vô hạn. Hiếm gặp — bình thường bên thua chờ vài trăm ms rồi nhận
`DATASOURCE_NAME_EXISTS` kèm `dsId` của bên thắng. **Lỗi tạm thời, gọi lại sau vài giây.**

### 9.3. Callback báo kết quả

Khi nạp xong (thành công hay thất bại), SQLBot gửi một request tới URL do bên tích hợp cung cấp:

```
POST <URL đã cấu hình>
Content-Type: application/json

{
  "actionType": 999,
  "payload": {
    "Id": 42,
    "status": true,
    "message": "Import succeeded"
  }
}
```

| Trường | Nghĩa |
|---|---|
| `actionType` | Mã sự kiện cố định `999`, cấp riêng cho việc báo kết quả nạp excel |
| `payload.Id` | **Chính là `dsId`** đã trả về ở response `202`. Viết hoa chữ `I` |
| `payload.status` | `true` = nguồn dữ liệu đã dùng được; `false` = nạp hỏng |
| `payload.message` | Mô tả bằng tiếng Anh. Khi hỏng đây là lý do (`Sheet not found: Quy3. Available: Quy1, Quy2`, `Cannot download after 3 attempts: ...`, …) |

`payload` **không có** `errorCode`. Cần mã lỗi phân loại được bằng máy thì gọi
[9.4](#94-get-datasourceexcelimportstatusds_id) với `Id` vừa nhận.

Coi là nhận thành công khi trả về HTTP **2xx** bất kỳ. Nội dung body không được đọc.

⚠ Vì thế **không được trả 2xx cho một bản tin bị từ chối**. Báo lỗi nghiệp vụ bằng thân response trong khi mã trạng thái vẫn là `200` sẽ khiến SQLBot ghi nhận đã gửi thành công và không bao giờ thử lại — hai bên sẽ tin vào hai sự thật khác nhau. Không xử lý được thì trả mã 4xx/5xx để cơ chế thử lại làm việc.

**Bốn điều bên nhận bắt buộc phải xử lý:**

1. **Có thể nhận trùng.** Cơ chế gửi bảo đảm *ít nhất một lần*, không phải *đúng một lần*: trả 2xx
   chậm hơn timeout thì lá thư đó vẫn được gửi lại. Xử lý theo `Id` phải **idempotent** —
   lần thứ hai với cùng `Id` không được sinh ra tác dụng phụ mới.
2. **Có thể không bao giờ nhận được.** Sau một số lần thử (giãn cách tăng dần) SQLBot dừng hẳn. Đó
   là lý do phải có [9.4](#94-get-datasourceexcelimportstatusds_id) làm đường đối chiếu — hệ ngoài
   nên tự hỏi lại với những `dsId` chờ quá lâu thay vì chờ vô hạn.
3. **`status: false` để lại một nguồn dữ liệu rác.** Nguồn đó tồn tại với trạng thái `Failed` và
   không vào hỏi đáp, nhưng vẫn hiện trong [`GET /datasource/list`](#41-get-datasourcelist). Nó
   **không chiếm tên**: gọi lại đúng tên đó sau khi sửa nguyên nhân sẽ ra `202` bình thường chứ
   không phải `409`. Nhưng mỗi lần thử lại đẻ thêm một dòng `Failed` nữa, nên bên tích hợp vẫn phải
   gọi [7.6 `POST /datasource/delete/{ds_id}/{name}`](#76-post-datasourcedeleteds_idname) để dọn.

   Từ khi file tới bằng `fileUrl`, **tập lỗi rơi vào nhánh này rộng hơn hẳn trước**: `sheetNames` gõ
   sai, file hỏng, tải không được — những thứ trước đây là `400` và không tạo ra gì. Đường dọn dẹp
   vì thế không còn là nhánh hiếm gặp mà là nhánh chạy thường xuyên; nên gắn nó vào cùng chỗ xử lý
   `status: false` thay vì làm thủ công.
4. **Chú thích vẫn phải nạp riêng.** Nhận `status: true` mới là có dữ liệu; tên bảng do server sinh
   ra gần như vô nghĩa, nên chất lượng trả lời phụ thuộc vào việc nạp chú thích ở [§5](#5-nạp-chú-thích).

### 9.4. `GET /datasource/excelImportStatus/{ds_id}`

Tra trạng thái một lần nạp. Dùng khi callback thất lạc, hoặc để hiển thị tiến trình.

**Response `200`:**

```json
{
  "code": 0,
  "data": {
    "dsId": 42,
    "name": "Thu ngân sách 2024",
    "status": "Success",
    "errorCode": null,
    "errorMessage": null,
    "tableCount": 2
  },
  "msg": null
}
```

| Trường | Nghĩa |
|---|---|
| `status` | `Importing` (đang chạy) · `Success` · `Failed` |
| `errorCode` | Chỉ có khi `Failed` — xem bảng dưới |
| `errorMessage` | Mô tả chi tiết đi kèm `errorCode` |
| `tableCount` | Số bảng đã đăng ký cho nguồn dữ liệu này |

**Các giá trị `errorCode`**, chia theo việc bên tích hợp phải làm gì:

| `errorCode` | Nghĩa | Xử lý |
|---|---|---|
| `URL_EXPIRED` | Chữ ký hết hạn trước khi tới lượt tải | Ký URL mới với hạn dài hơn rồi gọi lại |
| `URL_HOST_NOT_ALLOWED` | Host bị gỡ khỏi danh sách cho phép sau khi đã nhận việc | Liên hệ bên vận hành SQLBot |
| `DOWNLOAD_FORBIDDEN` | Object storage trả `403` | Kiểm lại chữ ký và quyền của object |
| `DOWNLOAD_NOT_FOUND` | Object storage trả `404` | Kiểm object còn tồn tại không (vòng đời tự xóa?) |
| `DOWNLOAD_FAILED` | Tải hỏng sau nhiều lần thử: mạng đứt, `5xx`, hoặc quá chậm | Lỗi tạm thời — gọi lại với URL mới |
| `FILE_TOO_LARGE` | File vượt trần 100 MB | Chia nhỏ file |
| `SHEET_NOT_FOUND` | `sheetNames` có tên không tồn tại. `errorMessage` liệt kê tên có thật | Sửa `sheetNames` rồi gọi lại |
| `CANNOT_READ_WORKBOOK` | Tải về được nhưng không mở được bằng thư viện đọc excel | File hỏng hoặc sai định dạng so với đuôi |
| `NO_SHEET_PARSED` | File mở được nhưng không sheet nào đọc ra dữ liệu | Kiểm lại nội dung file |
| `FILE_NOT_FOUND` | File biến mất khỏi máy chủ giữa chừng | Gọi lại |
| `JOB_ABANDONED` | Tiến trình nạp bị tắt giữa chừng (deploy, hết bộ nhớ) | Lỗi tạm thời — gọi lại |
| `INTERNAL_ERROR` | Lỗi không phân loại được | Báo cho bên vận hành SQLBot kèm `dsId` |

Bốn mã `URL_*` và `DOWNLOAD_*` xuất hiện ở **cả** response `400` của [9.2](#92-post-datasourcecreatefromexcelasync)
lẫn ở đây — cùng một tên cho cùng một chuyện, chỉ khác thời điểm phát hiện.

Mọi trường hợp `Failed` đều để lại một nguồn dữ liệu rác phải dọn bằng
[7.6](#76-post-datasourcedeleteds_idname), kể cả các lỗi chỉ liên quan tới việc tải file.

`404` nếu `ds_id` không tồn tại. Nguồn dữ liệu tạo bằng đường 9.1 hoặc §8 cũng tra được ở đây, khi
đó `errorCode`/`errorMessage` luôn `null`.

Không có giới hạn tần suất, nhưng thời gian xử lý giờ gồm cả việc tải file về từ object storage nên
nó phụ thuộc đường truyền giữa SQLBot và object storage, không chỉ phụ thuộc kích thước file. Hỏi
lại mỗi 2–5 giây là đủ; đừng đặt hạn chờ cứng theo kích thước file.

**Một chi tiết về thời điểm:** `status` chuyển sang `Failed` sớm hơn `errorCode` một nhịp rất ngắn,
nên có khả năng đọc được `Failed` kèm `errorCode: null`. Gặp trường hợp đó thì hỏi lại một lần nữa
thay vì coi là lỗi không rõ nguyên nhân.

---
