# Đặc tả API AI Sync Hook

Dành cho hệ thống SW đồng bộ quyền người dùng và cấu trúc nguồn dữ liệu sang SQLBot.

Phạm vi bản này: **1 endpoint**, **2 loại bản tin đã triển khai** — `AUTHORIZATION_SYNC` và
`DATASOURCE_SYNC`.

## Mục lục

| Mục | Nội dung |
|---|---|
| [1](#1-xác-thực-và-headers) | Xác thực và headers — gồm cả quy tắc dùng idempotency key |
| [2](#2-envelope-request) | Envelope request — vỏ chung của mọi bản tin |
| [3](#3-khung-response) | Khung response — từng trường, và quy tắc tương thích tiến |
| [5](#5-bản-tin-authorization_sync) | Bản tin `AUTHORIZATION_SYNC` — đồng bộ quyền người dùng |
| [6](#6-bản-tin-datasource_sync) | Bản tin `DATASOURCE_SYNC` — đồng bộ cấu trúc nguồn dữ liệu |
| [7](#7-vòng-đời-kết-nối) | Vòng đời kết nối — thời gian xử lý, timeout, xử lý sau khi đứt |
| [8](#8-khi-có-sự-cố) | Khi có sự cố — bảng mã lỗi và response mẫu |
| [9](#9-phụ-lục) | Phụ lục — thay đổi phá vỡ tương thích, lý do thiết kế |

---

## 1. Xác thực và headers

**Base URL:** `https://<host>/api/v1`

**Endpoint:** `POST /hooks/ai-sync`

### 1.1. Lấy access token

Hook dùng chung cơ chế JWT của SQLBot, **không có static token riêng**.

1. Gọi `POST /login/access-token` với body `application/x-www-form-urlencoded`, hai field
   `username` và `password`.
2. Gửi lại `access_token` nhận được qua header `X-SQLBOT-TOKEN: Bearer <access_token>` ở mọi
   request tới `/hooks/ai-sync`.

```bash
TOKEN=$(curl -s -X POST "https://<host>/api/v1/login/access-token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=<tai-khoan>&password=<mat-khau>" | jq -r .data.access_token)
```

Tài khoản đăng nhập **phải có quyền `ws_admin`** (hoặc là admin toàn cục) — thiếu quyền thì mọi
bản tin đều bị từ chối, xem [§8.1](#81-bảng-mã-lỗi). Token hết hạn theo cấu hình của SQLBot (mặc
định 8 ngày) và **không có endpoint refresh**: hết hạn thì đăng nhập lại.

### 1.2. Bảng headers

| Header | Bắt buộc | Ý nghĩa |
|---|---|---|
| `Content-Type: application/json` | Có | |
| `X-SQLBOT-TOKEN: Bearer <access_token>` | Có | Xác thực, lấy ở [§1.1](#11-lấy-access-token) |
| `X-Idempotency-Key: <UUID>` | Có | Chống xử lý trùng — [§1.3](#13-idempotency-key) |
| `X-Request-ID: <UUID>` | Không | Chỉ để trace, được vọng lại nguyên văn trong response |

⚠ **Hai header UUID không được dài quá 100 ký tự.** Vượt ngưỡng thì request hỏng ở tầng dưới và
trả về HTTP 500 **không theo khuôn response của hook** — không có `errorCode` để đọc. Dùng UUID
chuẩn (36 ký tự) là an toàn.

### 1.3. Idempotency key

Mỗi bản tin phải mang một `X-Idempotency-Key` **mới và duy nhất** (khuyến nghị UUID v4). SQLBot ghi
nhớ mọi key đã tiếp nhận; gửi lại đúng key đó thì bản tin **không được xử lý lại**, và response là
HTTP 200 với `status: "DUPLICATE"`:

```json
{
  "requestId": "9a1c7f4e-3d2b-4a10-8c55-2b7e6f0a1d34",
  "idempotencyKey": "550e8400-e29b-41d4-a716-446655440000",
  "actionType": 1,
  "status": "DUPLICATE",
  "logId": "b3f1c2a0-8e77-4c31-9a2d-77c0f5e41b90",
  "results": [],
  "applied": {"upserted": 0, "deleted": 0},
  "errorCode": null,
  "errorMessage": null
}
```

⚠ **Nhánh `DUPLICATE` không dựng lại được chi tiết của lần đầu.** `results` luôn rỗng và `applied`
luôn bằng 0 ở đây — `applied.upserted: 0` **không** có nghĩa là lần đầu chưa ghi được gì. Căn cứ
duy nhất để biết lần đầu ra sao là `errorCode`: `null` nghĩa là lần đầu thành công, khác `null`
nghĩa là lần đầu thất bại (kèm `errorMessage` và `logId` của chính lần đó).

⚠ **Một key chỉ dùng cho đúng một nội dung bản tin.** Mọi response có `logId` — kể cả 400, 422 và
500 — đều đã tiêu key đó. Nên kịch bản "gửi payload sai, nhận 400, sửa payload, gửi lại **cùng
key**" khiến bản tin đã sửa **không bao giờ được xử lý**: SW chỉ nhận lại đúng lỗi cũ dưới dạng
`DUPLICATE`.

**Quy tắc rút gọn:** gửi lại cùng key chỉ dành cho ca **không nhận được response** (timeout, đứt
mạng, 502/504 của proxy). Đã nhận về một body có `logId` thì lần gửi sau phải dùng key mới.

Key **chưa bị tiêu** khi response có `logId: null` — dùng lại được. Hiện có hai nhánh như vậy:
`MALFORMED_JSON` và `MISSING_IDEMPOTENCY_KEY`.

> Cứ nhìn `logId` chứ đừng nhớ theo mã lỗi: `logId` là id của dòng nhật ký bản tin, `null` nghĩa là
> chưa có dòng nào được ghi, nên key vẫn còn nguyên. Quy tắc này đúng cả khi danh sách mã lỗi dài
> thêm về sau.

Hai request **song song** mang cùng một key thì đúng một trong hai được xử lý, request còn lại nhận
`DUPLICATE`. Không có ca nào cả hai cùng được áp dụng.

---

## 2. Envelope request

```json
{
  "actionType": 1,
  "payload": {"...": "..."}
}
```

| Trường | Kiểu | Bắt buộc | Ghi chú |
|---|---|---|---|
| `actionType` | integer | Có | Loại bản tin — [§2.1](#21-bảng-actiontype) |
| `payload` | object | Có | Nội dung theo `actionType` — [§5.1](#51-payload-authorization_sync), [§6.1](#61-payload-datasource_sync) |

Vỏ envelope giống nhau cho mọi loại bản tin. Trường lạ gửi kèm đều bị bỏ qua âm thầm, không lỗi.

⚠ **Nội dung phải nằm dưới khoá `payload`.** Khoá `data` của hợp đồng gốc không còn được nhận: gửi
`data` bị hiểu là **thiếu `payload`** và nhận 400 `INVALID_ENVELOPE` — xem
[§9.1](#91-thay-đổi-phá-vỡ-tương-thích).

### 2.1. Bảng actionType

| `actionType` | Tên | Ý nghĩa | Trạng thái |
|---|---|---|---|
| 1 | `AUTHORIZATION_SYNC` | Đồng bộ quyền người dùng | **Đã triển khai** — [§5](#5-bản-tin-authorization_sync) |
| 2 | `USER_SYNC` | Đồng bộ thông tin người dùng | Chưa triển khai |
| 3 | `ORGANIZATION_SYNC` | Đồng bộ tổ chức | Chưa triển khai |
| 4 | `DATASOURCE_SYNC` | Đồng bộ cấu trúc nguồn dữ liệu | **Đã triển khai** — [§6](#6-bản-tin-datasource_sync) |
| 5 | `KNOWLEDGE_SYNC` | Đồng bộ knowledge | Chưa triển khai |
| 6 | `DOCUMENT_SYNC` | Đồng bộ document | Chưa triển khai |

`actionType` phải là **integer**, không phải chuỗi — gửi `"1"` nhận 422 `UNKNOWN_ACTION_TYPE`. Gửi
2, 3, 5, 6 nhận 422 `UNSUPPORTED_ACTION_TYPE`, không bị bỏ qua âm thầm.

Một `actionType` đã phát hành thì không đổi ý nghĩa.

---

## 3. Khung response

Response của hook là **JSON thô**, không bọc trong envelope `{code, data, msg}` mà các API SQLBot
khác dùng. Hình dạng giống nhau ở mọi nhánh — thành công, thất bại, hay trùng key.

HTTP status phản ánh đúng kết quả: 2xx thành công, 4xx lỗi phía SW, 5xx lỗi phía SQLBot.

### 3.1. Các trường của response

| Trường | Kiểu | Ý nghĩa |
|---|---|---|
| `requestId` | string hoặc null | Vọng lại `X-Request-ID`; `null` nếu SW không gửi header đó |
| `idempotencyKey` | string hoặc null | Vọng lại `X-Idempotency-Key`; `null` ở nhánh thiếu hẳn header |
| `actionType` | integer | Loại bản tin — xem cảnh báo dưới bảng |
| `status` | string | `SUCCESS`, `FAILED` hoặc `DUPLICATE` |
| `logId` | string (UUID) hoặc null | Id dòng nhật ký, dùng để tra cứu về sau. `null` nghĩa là bản tin chưa được ghi nhận — [§1.3](#13-idempotency-key) |
| `results` | array | Kết quả từng phần tử; cấu trúc theo loại bản tin — [§5.3](#53-kết-quả-từng-người-dùng), [§6.3](#63-kết-quả-từng-nguồn-dữ-liệu). Rỗng ở mọi nhánh lỗi và ở nhánh `DUPLICATE` |
| `applied.upserted` | integer | Tổng cộng dồn của `results[]`, để SW khỏi phải tự cộng |
| `applied.deleted` | integer | Tổng cộng dồn của `results[]` |
| `errorCode` | string hoặc null | Mã lỗi — [§8.1](#81-bảng-mã-lỗi) |
| `errorMessage` | string hoặc null | Mô tả lỗi cho người đọc, **không có cấu trúc cố định** — hiển thị hoặc ghi log, đừng parse để lấy dữ liệu |

⚠ **HTTP 200 chưa chắc là mọi việc đều xong.** Với `DATASOURCE_SYNC`, từng nguồn hỏng riêng lẻ vẫn
cho HTTP 200 và `status: "SUCCESS"` ở cấp request. Luôn duyệt `results[]` —
[§6.3](#63-kết-quả-từng-nguồn-dữ-liệu).

⚠ **`actionType` trong response có thể mang giá trị `0`**, ở đúng hai nhánh lỗi sớm nhất: thiếu
`X-Idempotency-Key`, và body không phải JSON object. Hai ca đó bị chặn trước khi SQLBot đọc được
tới `actionType` nên không có giá trị thật để vọng lại. Bộ parse nào ép `actionType` về đúng dải
1–6 sẽ vỡ đúng lúc cần đọc thông báo lỗi nhất.

Ngoài hai nhánh đó, `actionType` là giá trị SW đã gửi: vọng lại nguyên văn khi nó là integer — kể
cả integer ngoài dải 1–6, gửi `99` thì nhận lại `99` — và bằng `0` khi nó sai kiểu, ví dụ gửi chuỗi
`"1"`. Riêng nhánh `DUPLICATE` vọng lại `actionType` của **bản tin đầu tiên** đã dùng key đó, không
phải của request hiện tại.

---

## 5. Bản tin phân quyền

`actionType = 1`. Đồng bộ **quyền khai thác dữ liệu** của người dùng: mỗi người được kê danh sách
bảng họ được phép truy vấn, kèm phạm vi cột và dòng trên từng bảng. Một bản tin mang được cả một lô
nhiều người dùng.

### 5.1. Payload

```json
{
  "users": [
    {
      "userId": "usr-12345-67890",
      "fullName": "Nguyễn Văn A",
      "isAdmin": false,
      "formQueries": [
        {
          "formUuid": "form-abcd-1234",
          "tableInfo": {
            "databaseTableName": "kdl_nhan_khau_row_values",
            "tableDisplayName": "Dữ liệu Nhân khẩu",
            "linhVucMa": "LV_DAN_CU",
            "linhVucUuid": "lv-uuid-5678",
            "linhVucName": "Lĩnh vực Dân cư",
            "linhVucDescription": "Lĩnh vực quản lý các thông tin liên quan đến dân cư",
            "queries": [
              {
                "datasourceId": "7",
                "datasourceType": "postgresql",
                "query": "SELECT ho_ten, nam_sinh, province_id FROM kdl_nhan_khau_row_values WHERE province_id = '01'"
              }
            ]
          }
        },
        {
          "formUuid": "form-efgh-5678",
          "tableInfo": {
            "databaseTableName": "kdl_ho_ngheo_row_values",
            "tableDisplayName": "Dữ liệu Hộ nghèo",
            "linhVucMa": "LV_DAN_CU",
            "queries": [
              {"datasourceId": "7", "datasourceType": "postgresql", "query": ""}
            ]
          }
        }
      ]
    }
  ]
}
```

| Trường | Kiểu | Bắt buộc | Ghi chú |
|---|---|---|---|
| `users` | array | Có | **Không được rỗng** — mã lỗi `EMPTY_USER_LIST` |
| `users[].userId` | string | Có | Không được rỗng, không được lặp giữa các phần tử trong cùng `users` |
| `users[].fullName` | string | Không | |
| `users[].isAdmin` | boolean | Có | |
| `users[].formQueries` | array | Có | **Có thể rỗng** — [§5.2](#52-quy-tắc-thay-thế-toàn-bộ) |
| `formQueries[].formUuid` | string | Không | Chỉ để tham khảo, **không** dùng để định danh — xem cảnh báo dưới bảng |
| `formQueries[].tableInfo` | object | Có | |
| `tableInfo.databaseTableName` | string | Có | Tên bảng vật lý. **Không được lặp trong `formQueries` của cùng một người dùng** — mã lỗi `DUPLICATE_DATABASE_TABLE_NAME` |
| `tableInfo.tableDisplayName` | string | Không | Tên hiển thị của bảng |
| `tableInfo.linhVucMa` | string | Không | Mã lĩnh vực nghiệp vụ của bảng |
| `tableInfo.linhVucUuid` | string | Không | |
| `tableInfo.linhVucName` | string | Không | |
| `tableInfo.linhVucDescription` | string | Không | |
| `tableInfo.queries` | array | Có | **Không được rỗng**: một bảng không biết lấy dữ liệu từ nguồn nào thì vô nghĩa |
| `queries[].datasourceId` | string | Có | Id nguồn dữ liệu **phía SQLBot** (số, viết dưới dạng chuỗi). Không được lặp trong cùng một `queries` |
| `queries[].datasourceType` | string | Có | Chuỗi tự do (`postgresql`, `clickhouse`…), SQLBot không kiểm theo danh sách |
| `queries[].query` | string | Có | Câu truy vấn giới hạn phạm vi dữ liệu trên đúng nguồn đó — xem hai đoạn dưới |

**Nội dung `query` quyết định hai thứ:** danh sách cột trong mệnh đề `SELECT` chính là các cột
người dùng được phép thấy (`SELECT *` nghĩa là mọi cột), và mệnh đề `WHERE` là giới hạn dòng.

⚠ **`query` rỗng khác hẳn với việc không kê nguồn đó.** Chuỗi rỗng (`""`) là cách khai "được cấp
quyền trên bảng này, mọi cột, không giới hạn dòng" — như bảng `kdl_ho_ngheo_row_values` trong ví dụ
trên. Còn nếu `queries[]` **không có phần tử nào** trỏ tới một nguồn thì trên nguồn đó người dùng
**không được cấp** bảng này. Nhầm hai ca này là cấp dư quyền hoặc cắt nhầm quyền.

⚠ **`formUuid` không phải khoá định danh.** Khoá thật để SQLBot nhận ra "đây là quyền nào" là cặp
`(userId, databaseTableName)`. Gửi lại đúng `databaseTableName` đó ở lần sau — dù kèm `formUuid`
khác, thiếu hẳn `formUuid`, hay chưa bao giờ gửi `formUuid` — đều **ghi đè** đúng dòng quyền cũ,
không tạo dòng mới. Ngược lại, hai `formQuery` trỏ **cùng một bảng** trong cùng một bản tin là lỗi,
kể cả khi `formUuid` của chúng khác nhau.

### 5.2. Quy tắc thay thế toàn bộ

**`formQueries` là toàn bộ danh sách quyền hiện hành của người dùng, không phải phần vừa thay
đổi.** Mỗi lần gọi hook, SW phải kê đủ **mọi** bảng người đó được phép truy cập tại thời điểm gửi;
SQLBot thay danh sách cũ bằng danh sách mới, nguyên khối.

| Bảng đó trong `formQueries` | Người dùng đang có quyền | Kết quả |
|---|---|---|
| Có | Chưa | Cấp mới |
| Có | Rồi | Ghi đè bằng nội dung mới |
| **Không có** | **Rồi** | **Thu hồi** |

Ba điểm đi kèm:

- Khoá so khớp giữa các lần đồng bộ là `databaseTableName`, không phải `formUuid`.
- Phạm vi thay thế tính theo **từng người một**: `formQueries` của người này không đụng tới quyền
  của người khác trong cùng lô.
- `formQueries: []` thu hồi toàn bộ quyền của người đó.

⚠ **`formQueries: []` không phải là cách chặn một người dùng.** Nó xoá hết dòng quyền, mà người
không còn dòng quyền nào thì luồng hỏi đáp **không áp giới hạn nào** lên họ — đây là trạng thái
dành cho tài khoản quản trị nội bộ của SQLBot, vốn không đi qua cơ chế này. Muốn một người không
truy vấn được nữa thì **khoá tài khoản** của họ qua API quản trị người dùng
(`PATCH /user/by-account/status` với `status = 0`), đừng thu hồi quyền bằng danh sách rỗng.

### 5.3. Kết quả từng người dùng

Mỗi phần tử của `results[]`:

| Trường | Ý nghĩa |
|---|---|
| `userId` | Vọng lại đúng chuỗi SW đã gửi |
| `status` | Hiện luôn là `SUCCESS` — xem cảnh báo dưới bảng |
| `applied.upserted` | Số dòng quyền đã ghi cho người đó (cấp mới + ghi đè) |
| `applied.deleted` | Số dòng quyền đã thu hồi của người đó |

⚠ **Không có ca "một người trong lô hỏng riêng".** Lỗi cấu trúc ở bất kỳ người nào cũng giết cả bản
tin: HTTP 400, `results` rỗng, **không ai trong lô được áp dụng**. Nên với bản tin này, `results[]`
chỉ dùng để đọc số liệu chứ không dùng để dò lỗi — khác hẳn `DATASOURCE_SYNC`
([§6.3](#63-kết-quả-từng-nguồn-dữ-liệu)). Dù vậy vẫn nên kiểm `status` theo
[§3.2](#32-quy-tắc-tương-thích-tiến) thay vì tin là nó bất biến.

### 5.4. Response mẫu AUTHORIZATION_SYNC

Bối cảnh: trước lần gọi này, `usr-12345-67890` đang có quyền trên **3** bảng. Bản tin ở
[§5.1](#51-payload-authorization_sync) chỉ kê **2** bảng, nên bảng thứ ba bị thu hồi.

```json
{
  "requestId": "9a1c7f4e-3d2b-4a10-8c55-2b7e6f0a1d34",
  "idempotencyKey": "550e8400-e29b-41d4-a716-446655440000",
  "actionType": 1,
  "status": "SUCCESS",
  "logId": "b3f1c2a0-8e77-4c31-9a2d-77c0f5e41b90",
  "results": [
    {"userId": "usr-12345-67890", "status": "SUCCESS", "applied": {"upserted": 2, "deleted": 1}}
  ],
  "applied": {"upserted": 2, "deleted": 1},
  "errorCode": null,
  "errorMessage": null
}
```

---

## 6. Bản tin DATASOURCE_SYNC

`actionType = 4`. Báo cho SQLBot rằng cấu trúc của một số nguồn dữ liệu vừa đổi và cần đọc lại. Một
bản tin mang được cả một lô nhiều nguồn.

### 6.1. Payload DATASOURCE_SYNC

```json
{
  "datasourceIds": ["7", "12"]
}
```

| Trường | Kiểu | Bắt buộc | Ghi chú |
|---|---|---|---|
| `datasourceIds` | array of string | Có | Id nguồn dữ liệu **phía SQLBot** (số, viết dưới dạng chuỗi) — cùng họ với `queries[].datasourceId` ở [§5.1](#51-payload-authorization_sync). **Không được rỗng**, **không được lặp**, và mọi phần tử phải là chuỗi chứa số |

### 6.2. Quy tắc chép lại toàn bộ cấu trúc nguồn

Bản tin này **không mang dữ liệu mới**. Nó chỉ báo "cấu trúc của các nguồn dưới đây vừa đổi, đọc
lại đi". SQLBot tự kết nối vào DB nguồn, đọc lại danh sách bảng, cột, kiểu dữ liệu, chú thích và
khoá ngoại, rồi cập nhật metadata của mình theo đúng những gì DB nguồn **đang** có.

Gửi bản tin này sau khi SW tạo, xoá hoặc sửa bảng ở DB nghiệp vụ. Không gửi thì SQLBot vẫn dùng
metadata cũ: bảng mới không hỏi được, còn bảng đã xoá vẫn xuất hiện trong ngữ cảnh sinh SQL và làm
câu trả lời sai.

- Trạng thái đích là **toàn bộ bảng đang có ở nguồn**, không phải phần chênh lệch: bảng mới xuất
  hiện được thêm vào và bật sẵn, bảng đã biến mất bị xoá khỏi metadata kèm toàn bộ cột và quan hệ
  liên quan.
- Phạm vi này có thể **rộng hơn** danh sách bảng đang được chọn thủ công trong giao diện SQLBot.
  Nhưng ai được hỏi bảng nào thì **không** đổi theo bản tin này — việc đó do quyền gửi qua
  `AUTHORIZATION_SYNC` quyết định ([§5](#5-bản-tin-authorization_sync)).
- Đổi tên một bảng ở nguồn được hiểu là **xoá bảng cũ và thêm bảng mới**. Mọi mô tả hiển thị và chú
  thích cột SW đã đặt cho bảng cũ **không đi theo** tên mới, nên sau khi đổi tên bảng thì SW phải
  đẩy lại phần mô tả.
- Chỉ metadata được đồng bộ. Dữ liệu trong bảng không bị đọc, không bị chép đi đâu.

⚠ **Gọi lại một bản tin cũ luôn an toàn.** Bản tin không mang trạng thái riêng nên gọi lại chỉ là
đọc lại DB nguồn thêm một lần nữa. Lưu ý đây là nói về **nội dung** bản tin: chống xử lý trùng khi
retry vẫn theo `X-Idempotency-Key` như mọi loại bản tin khác, và gửi lại thì phải dùng key mới
([§1.3](#13-idempotency-key)).

### 6.3. Kết quả từng nguồn dữ liệu

Hai tầng khác nhau, đừng gộp:

1. **Lỗi cấu trúc payload** (`datasourceIds` rỗng, id lặp, id không phải số) cho HTTP 400 và
   **không nguồn nào** được xử lý.
2. Qua được tầng đó thì mỗi nguồn xử lý **độc lập**: HTTP luôn **200** và `status` cấp request luôn
   `SUCCESS`, kể cả khi mọi nguồn trong lô đều hỏng. Kết cục thật của từng nguồn nằm trong
   `results[]`.

Mỗi phần tử của `results[]`:

| Trường | Ý nghĩa |
|---|---|
| `datasourceId` | Vọng lại đúng chuỗi id SW đã gửi |
| `status` | `SUCCESS` hoặc `FAILED` |
| `applied.upserted` | Số bảng của nguồn đó **sau** khi đồng bộ |
| `applied.deleted` | Số bảng bị loại vì không còn ở nguồn |
| `message` | Chỉ có nghĩa khi `FAILED`: lý do nguồn đó không đồng bộ được |

Các lý do một nguồn `FAILED`:

| Tình huống | `message` |
|---|---|
| Id không tồn tại trong SQLBot | `datasource không tồn tại` |
| Nguồn kiểu Excel hoặc CSV, không có DB nguồn ngoài để đọc lại | `nguồn excel không có DB nguồn để đồng bộ lại` |
| Nguồn đang trong quá trình nạp dữ liệu | `nguồn đang nạp dữ liệu (Importing), thử lại sau` |
| Không kết nối được DB nguồn, hoặc đọc catalog lỗi | Thông điệp lỗi gốc từ driver DB |
| DB nguồn trả về **0 bảng** | SQLBot từ chối thay vì xoá sạch metadata — xem [§9.2](#92-vì-sao-hợp-đồng-lại-như-vậy) |

### 6.4. Response mẫu DATASOURCE_SYNC

Bối cảnh: bản tin ở [§6.1](#61-payload-datasource_sync) kê hai nguồn; nguồn `7` đồng bộ được (102
bảng, 1 bảng đã biến mất khỏi nguồn), nguồn `12` không tồn tại trong SQLBot.

```json
{
  "requestId": "1f0b6d92-5a44-4c8e-9f21-0d3e77a5b8c1",
  "idempotencyKey": "9f1d2b3c-7e60-4a95-bb12-4c8ad0e6f733",
  "actionType": 4,
  "status": "SUCCESS",
  "logId": "c72e4b18-0a93-4d6f-8b55-19ac3f0e2d47",
  "results": [
    {"datasourceId": "7", "status": "SUCCESS", "applied": {"upserted": 102, "deleted": 1}, "message": null},
    {"datasourceId": "12", "status": "FAILED", "applied": {"upserted": 0, "deleted": 0}, "message": "datasource không tồn tại"}
  ],
  "applied": {"upserted": 102, "deleted": 1},
  "errorCode": null,
  "errorMessage": null
}
```

---

## 7. Vòng đời kết nối

Hook xử lý **đồng bộ ngay trong request**, không có hàng đợi nền: response chỉ về khi mọi việc đã
làm xong. Không có cơ chế thăm dò tiến độ và không có endpoint hỏi trạng thái một bản tin.

`AUTHORIZATION_SYNC` chỉ ghi vào DB nội bộ của SQLBot nên trả về gần như ngay.

`DATASOURCE_SYNC` phải đọc catalog của từng DB nguồn nên chậm hơn hẳn. Đo thực tế: một nguồn
PostgreSQL khoảng 100 bảng mất **4–8 giây**, nguồn 537 bảng mất **20–25 giây**. Con số chỉ để hình
dung tỷ lệ, không phải cam kết hiệu năng — nó dao động theo độ trễ mạng tới DB nguồn.

Hệ quả cần chuẩn bị:

- Đặt timeout phía SW **rộng rãi** (tối thiểu vài phút) cho `DATASOURCE_SYNC`.
- Kiểm cả timeout của proxy ngược trên đường đi: proxy cắt sớm hơn thì SW nhận 502/504 trong khi
  SQLBot vẫn đang chạy.
- Chia lô nhỏ. Lô càng nhiều nguồn thì request treo càng lâu.

⚠ **Timeout không có nghĩa là bản tin không được áp dụng.** Request bị cắt ở phía SW vẫn chạy tiếp
đến hết ở phía SQLBot. Gửi lại **cùng key** để hỏi lại kết quả sẽ nhận `DUPLICATE`, tức là biết
được lần đầu thành hay bại qua `errorCode`, nhưng **không lấy lại được `results[]`**
([§1.3](#13-idempotency-key)). Nói cách khác, lô nào timeout thì SW mất phần chi tiết của lô đó, và
muốn biết từng phần tử phải nhờ bên vận hành SQLBot tra nhật ký theo chính `X-Idempotency-Key` đã
gửi. Cách phòng duy nhất là chia lô đủ nhỏ để một lần gửi không chạm ngưỡng timeout.

⚠ **Không có cơ chế chống bản tin đến sai thứ tự.** Với một key mới, mỗi bản tin
`AUTHORIZATION_SYNC` hợp lệ đều ghi đè danh sách quyền của lần trước, bất kể nó thực sự phát sinh
trước hay sau về mặt thời gian. SW phải tự đảm bảo thứ tự gửi — gửi tuần tự, đừng bắn song song
nhiều bản tin cho cùng một `userId`.

---

## 8. Khi có sự cố

### 8.1. Bảng mã lỗi

Cột **Phạm vi hỏng** cho biết lỗi đó giết cả bản tin hay chỉ giết một phần tử trong lô.

| HTTP | `status` | `errorCode` | Áp cho | Phạm vi hỏng | Ý nghĩa |
|---|---|---|---|---|---|
| 200 | `SUCCESS` | `null` | cả hai | — | Bản tin đã xử lý xong; vẫn phải duyệt `results[]` |
| 200 | `SUCCESS` | `null` | 4 | riêng nguồn | Một hoặc nhiều nguồn thất bại, chi tiết trong `results[].message` — [§6.3](#63-kết-quả-từng-nguồn-dữ-liệu) |
| 200 | `DUPLICATE` | của lần đầu | cả hai | — | Trùng `X-Idempotency-Key` — [§1.3](#13-idempotency-key) |
| 400 | `FAILED` | `MALFORMED_JSON` | cả hai | cả bản tin | Body không phải JSON hợp lệ, hoặc không phải một object |
| 400 | `FAILED` | `MISSING_IDEMPOTENCY_KEY` | cả hai | cả bản tin | Thiếu header `X-Idempotency-Key` |
| 400 | `FAILED` | `INVALID_ENVELOPE` | cả hai | cả bản tin | Envelope thiếu trường hoặc sai kiểu, gồm cả ca gửi `data` thay cho `payload` |
| 400 | `FAILED` | `INVALID_PAYLOAD` | cả hai | cả bản tin | `payload` sai schema; với `actionType = 4` gồm cả ca `datasourceIds` chứa phần tử không phải chuỗi số |
| 400 | `FAILED` | `EMPTY_USER_LIST` | 1 | cả bản tin | `payload.users` rỗng hoặc thiếu |
| 400 | `FAILED` | `DUPLICATE_USER_ID` | 1 | cả bản tin | `userId` lặp giữa các phần tử của `users` |
| 400 | `FAILED` | `DUPLICATE_DATABASE_TABLE_NAME` | 1 | cả bản tin | `databaseTableName` lặp trong `formQueries` của cùng một người dùng |
| 400 | `FAILED` | `DUPLICATE_DATASOURCE_ID` | cả hai | cả bản tin | `datasourceId` lặp — trong `queries` của cùng một bảng (loại 1), hoặc trong `datasourceIds` (loại 4) |
| 400 | `FAILED` | `EMPTY_DATASOURCE_LIST` | 4 | cả bản tin | `payload.datasourceIds` rỗng hoặc thiếu |
| 422 | `FAILED` | `UNSUPPORTED_ACTION_TYPE` | — | cả bản tin | `actionType` đã đặt tên nhưng chưa triển khai (2, 3, 5, 6) |
| 422 | `FAILED` | `UNKNOWN_ACTION_TYPE` | — | cả bản tin | `actionType` sai kiểu, thiếu, hoặc ngoài dải 1–6 |
| 500 | `FAILED` | `INTERNAL_ERROR` | cả hai | cả bản tin | Lỗi phía SQLBot |
| 401 | *(không có body của hook)* | — | — | cả bản tin | Thiếu, sai, hoặc hết hạn `X-SQLBOT-TOKEN` |
| 500 | *(không có body của hook)* | — | — | cả bản tin | Tài khoản không có quyền `ws_admin`, hoặc header UUID dài quá 100 ký tự |

**Ba dòng cuối nhận ra bằng cách nào:** mọi response do hook dựng đều có khoá `status` và
`errorCode`. Body **không** có hai khoá đó nghĩa là request bị chặn trước khi vào hook, hoặc hỏng ở
tầng dưới nó — đừng cố đọc `errorCode` trong những ca này.

Gặp `errorCode` không có trong bảng thì xử theo [§3.2](#32-quy-tắc-tương-thích-tiến): coi như một
lỗi chung theo HTTP status, đừng ném lỗi.

### 8.2. Response mẫu khi lỗi

Một lần chạy khác: bản tin `AUTHORIZATION_SYNC` đặt nội dung dưới khoá `data` thay vì `payload`.

```json
{
  "requestId": "3c8e51f7-9b02-4d1a-a6f4-8e5c07b2d913",
  "idempotencyKey": "7d21e4a8-1c93-4f57-b0e6-5a92f3c8d104",
  "actionType": 1,
  "status": "FAILED",
  "logId": "e91f0c23-4b7d-4a68-9f30-6d21ba54e7c8",
  "results": [],
  "applied": {"upserted": 0, "deleted": 0},
  "errorCode": "INVALID_ENVELOPE",
  "errorMessage": "1 validation error for SyncEnvelope\npayload\n  Field required ..."
}
```

`errorMessage` là chuỗi tự do sinh từ bộ kiểm schema, hình dạng có thể đổi bất cứ lúc nào — hiển
thị hoặc ghi log thì được, đừng parse. `logId` khác `null` nghĩa là key đã bị tiêu, lần gửi sau
phải dùng key mới ([§1.3](#13-idempotency-key)).
