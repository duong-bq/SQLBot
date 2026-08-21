# Đặc tả API AI Sync Hook

Dành cho hệ thống SW cần đồng bộ quyền user và cấu trúc nguồn dữ liệu sang SQLBot.

Phạm vi phase hiện tại: **1 endpoint**, **2 actionType** (`AUTHORIZATION_SYNC`, `DATASOURCE_SYNC`).

⚠ **Quyền gửi qua endpoint này đã có hiệu lực thật trong luồng hỏi đáp.** Bảng, cột và phạm vi dòng
khai ở đây quyết định trực tiếp người dùng hỏi được gì trên `POST /chat/question` — sai một trường
là người dùng mất quyền (hoặc dư quyền) ngay ở lần đồng bộ kế tiếp, không có bước duyệt trung gian
nào. Đọc kỹ hai cảnh báo ở §4 và ngữ nghĩa full snapshot ở §5 trước khi gửi bản tin thật.

---

## 1. Quy ước chung

**Base URL:** `https://<host>/api/v1`

**Endpoint:** `POST /hooks/ai-sync`

**Xác thực:** dùng chung cơ chế JWT của SQLBot — **không có static token riêng**.

1. Đăng nhập bằng `POST /login/access-token` (body `application/x-www-form-urlencoded`, field
   `username`/`password`) để lấy `access_token`.
2. Gửi lại token đó qua header `X-SQLBOT-TOKEN: Bearer <access_token>` ở mọi request tới
   `/hooks/ai-sync`.

Tài khoản dùng để đăng nhập **phải có quyền `ws_admin`** (hoặc admin toàn cục) — thiếu quyền sẽ bị
từ chối. Token hết hạn theo `ACCESS_TOKEN_EXPIRE_MINUTES` của SQLBot (mặc định 8 ngày), **không có
refresh endpoint** — hết hạn thì đăng nhập lại.

**Headers bắt buộc:**

| Header | Bắt buộc | Ý nghĩa |
|---|---|---|
| `Content-Type: application/json` | Có | |
| `X-SQLBOT-TOKEN: Bearer <access_token>` | Có | Xác thực, lấy từ `POST /login/access-token` |
| `X-Idempotency-Key: <UUID>` | **Có** | Chống xử lý trùng khi retry — xem §7 |
| `X-Request-ID: <UUID>` | Không | Chỉ để trace log, vọng lại nguyên văn trong response |

⚠ Lỗi xác thực (thiếu/sai/hết hạn token) và lỗi thiếu quyền `ws_admin` xảy ra **trước khi vào logic
của hook**, nên trả theo đúng quy ước lỗi chung của SQLBot (không phải theo format `SyncHookResponse`
ở §8 dưới đây). Thiếu quyền hiện trả HTTP 500 chứ không phải 403 — đây là quy ước chung toàn hệ
thống, không riêng gì hook.

**Response:** JSON thô, **không** bọc theo envelope `{code,data,msg}` mà các API SQLBot khác dùng.
HTTP status phản ánh đúng kết quả (2xx thành công, 4xx lỗi phía SW, 5xx lỗi phía SQLBot).

```json
{
  "requestId": "req-001",
  "idempotencyKey": "550e8400-e29b-41d4-a716-446655440000",
  "actionType": 1,
  "status": "SUCCESS",
  "logId": "b3f1c2a0-...-uuid",
  "results": [
    {"userId": "usr-12345-67890", "status": "SUCCESS", "applied": {"upserted": 2, "deleted": 0}}
  ],
  "applied": {"upserted": 2, "deleted": 0},
  "errorCode": null,
  "errorMessage": null
}
```

Cấu trúc `results[]` thay đổi theo `actionType`: phần tử có khoá `userId` với actionType 1, khoá
`datasourceId` với actionType 4 (§6). Các trường còn lại giữ nguyên hình dạng.

⚠ **`actionType` trong response có thể mang giá trị `0`, và `logId` có thể là `null`** — ở đúng hai
nhánh lỗi sớm nhất: thiếu `X-Idempotency-Key`, và body không phải JSON object. Hai ca đó bị chặn
trước khi hook đọc được tới `actionType` và trước khi dòng audit được ghi, nên không có giá trị
thật để vọng lại. Client parse `actionType` theo enum 1–6 (§2) **phải chấp nhận thêm giá trị `0`**,
nếu không sẽ vỡ đúng lúc cần đọc thông báo lỗi nhất.

Ngoài hai nhánh đó, `actionType` là giá trị SW đã gửi: vọng lại nguyên văn khi nó là integer — kể
cả integer ngoài dải 1–6, gửi `99` thì nhận lại `99` — và bằng `0` khi nó sai kiểu, ví dụ gửi chuỗi
`"1"`. Riêng response `DUPLICATE` (§7) vọng lại `actionType` của **bản tin đầu tiên** đã dùng key
đó, không phải của request hiện tại.

---

## 2. Action Type

`actionType` là **integer**, không phải string.

| actionType | Constant | Ý nghĩa | Trạng thái |
|---|---|---|---|
| 1 | `AUTHORIZATION_SYNC` | Đồng bộ quyền user | **Đã triển khai** — §4 |
| 2 | `USER_SYNC` | Đồng bộ thông tin user | Chưa triển khai |
| 3 | `ORGANIZATION_SYNC` | Đồng bộ tổ chức | Chưa triển khai |
| 4 | `DATASOURCE_SYNC` | Đồng bộ cấu trúc nguồn dữ liệu | **Đã triển khai** — §6 |
| 5 | `KNOWLEDGE_SYNC` | Đồng bộ knowledge | Chưa triển khai |
| 6 | `DOCUMENT_SYNC` | Đồng bộ document | Chưa triển khai |

Gửi actionType 2, 3, 5, 6 nhận **422 `UNSUPPORTED_ACTION_TYPE`** — không bị bỏ qua âm thầm. Gửi
actionType ngoài 1–6 (kể cả 0, string, thiếu trường) nhận **422 `UNKNOWN_ACTION_TYPE`**.

Một actionType đã release thì không được đổi ý nghĩa.

---

## 3. Envelope request

```json
{
  "actionType": 1,
  "payload": { "...": "..." }
}
```

| Trường | Kiểu | Bắt buộc | Ghi chú |
|---|---|---|---|
| `actionType` | integer | Có | Xem §2 |
| `payload` | object | Có | Payload theo `actionType`, xem §4 và §6 |

Vỏ envelope giống nhau cho mọi actionType. SW gửi kèm trường lạ nào (kể cả `version`/`timestamp`
của hợp đồng cũ) đều bị bỏ qua âm thầm, không lỗi.

⚠ **Payload phải nằm dưới khoá `payload`; khoá `data` của hợp đồng gốc KHÔNG còn được nhận**
(breaking change, không tương thích ngược). Trước đây hook nhận cả hai tên để không bên nào phải
sửa client; nay gửi `data` bị hiểu là **thiếu `payload`** và nhận **400 `INVALID_ENVELOPE`**. Khác
với `version`/`timestamp`: hai trường đó chỉ là trường thừa nên bỏ qua được, còn `data` thì không —
bỏ qua nó là bản tin mất hẳn phần nội dung.

---

## 4. Payload AUTHORIZATION_SYNC (`actionType = 1`) — batch nhiều user

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
        }
      ]
    }
  ]
}
```

⚠ `queries[]` nằm **trong `tableInfo`**, không phải ngang cấp `tableInfo` — bản cũ dùng 2 field cố
định `postgresQuery`/`clickHouseQuery` ở cấp `formQueries[i]`, giờ đã bỏ hẳn (breaking change, không
tương thích ngược) để hỗ trợ số lượng/loại datasource tuỳ ý thay vì cố định 2 loại.

⚠ `tableInfo.fields` và `tableInfo.tableDescription` **không còn được nhận** từ bản tin này (breaking
change, không tương thích ngược) — field-level metadata (tên/mô tả cột) và mô tả bảng đã chuyển sang
endpoint edit field/table riêng của SW. Gửi kèm 2 trường này vẫn không báo lỗi (bị bỏ qua âm thầm),
nhưng không còn tác dụng gì.

⚠ **`formUuid` không còn là khoá định danh.** Khoá thật để SQLBot nhận biết "đây là quyền nào" là
`(userId, databaseTableName)`. Gửi lại đúng `databaseTableName` đó ở lần đồng bộ sau — dù kèm
`formUuid` khác, thiếu hẳn `formUuid`, hay không gửi `formUuid` bao giờ — đều **ghi đè** đúng dòng
quyền cũ, không tạo dòng mới. Hai `formQuery` khác nhau trỏ **cùng một bảng** trong CÙNG một request
là lỗi (`DUPLICATE_DATABASE_TABLE_NAME`), kể cả khi `formUuid` của chúng khác nhau.

| Trường | Kiểu | Bắt buộc | Ghi chú |
|---|---|---|---|
| `users` | array | Có | **Không được rỗng** — xem §8 mã lỗi `EMPTY_USER_LIST` |
| `users[].userId` | string | Có | Không được rỗng, không được lặp giữa các phần tử trong cùng `users` |
| `users[].fullName` | string | Không | |
| `users[].isAdmin` | boolean | Có | |
| `users[].formQueries` | array | Có | **Có thể rỗng** — xem §5 |
| `formQueries[].formUuid` | string | Không | Chỉ tham khảo, KHÔNG dùng để định danh dòng quyền — xem cảnh báo dưới |
| `formQueries[].tableInfo` | object | Có | |
| `tableInfo.databaseTableName` | string | Có | **Không được lặp trong `formQueries` của CÙNG một user** — đây mới là khoá định danh, xem §8 mã lỗi `DUPLICATE_DATABASE_TABLE_NAME` |
| `tableInfo.tableDisplayName` | string | Không | |
| `tableInfo.linhVucMa` | string | Không | Mã lĩnh vực nghiệp vụ của bảng |
| `tableInfo.linhVucUuid` | string | Không | |
| `tableInfo.linhVucName` | string | Không | |
| `tableInfo.linhVucDescription` | string | Không | |
| `tableInfo.queries` | array | Có | **Không được rỗng** — một form không biết lấy dữ liệu từ nguồn nào thì vô nghĩa |
| `queries[].datasourceId` | string | Có | **Id nguồn dữ liệu phía SQLBot** (số, dạng chuỗi)|
| `queries[].datasourceType` | string | Có | Free string (`postgresql`, `clickhouse`, ...), SQLBot không validate theo enum |
| `queries[].query` | string | Có | Query giới hạn phạm vi dữ liệu trên đúng datasource đó. **Danh sách cột trong `SELECT` chính là các cột người dùng được phép thấy**. **Được phép rỗng** (`""`) — nghĩa là không giới hạn dữ liệu trên datasource đó |

---

## 5. Ngữ nghĩa FULL SNAPSHOT — đọc kỹ trước khi tích hợp

**`formQueries` là toàn bộ quyền hiện tại của TỪNG user trong `users`, không phải phần thay đổi.**

- Với mỗi user trong `users`, SQLBot **thay thế toàn bộ** danh sách quyền của `userId` đó bằng đúng
  những gì có trong `formQueries` của chính user đó — không liên quan tới `formQueries` của user
  khác trong cùng batch.
- Bảng nào (`databaseTableName`) **không có mặt** trong `formQueries` sẽ **bị xoá quyền** — kể cả
  khi trước đó user đã có quyền trên bảng đó. Đây không phải "giữ nguyên", mà là "thu hồi". Khoá so
  khớp giữa các lần đồng bộ là `databaseTableName`, không phải `formUuid` — xem cảnh báo ở §4.
- `formQueries: []` nghĩa là **thu hồi toàn bộ quyền** của user.

Nói cách khác: mỗi lần gọi hook, SW phải gửi đủ **toàn bộ** danh sách bảng user được phép truy cập
tại thời điểm đó, không chỉ phần vừa thêm/sửa.

⚠ **`formQueries: []` không phải là cách chặn một người dùng.** Nó xoá hết dòng quyền của user, và
user không còn dòng quyền nào thì luồng hỏi đáp **không áp giới hạn nào** lên họ — đây là trạng thái
dành cho các tài khoản quản trị nội bộ của SQLBot, vốn chưa bao giờ đi qua cơ chế này. Muốn một
người dùng không truy vấn được nữa thì **khoá tài khoản** của họ (`USER_ADMIN_API_SPEC.md`), đừng
thu hồi quyền bằng danh sách rỗng.

---

## 6. Payload DATASOURCE_SYNC (`actionType = 4`) — batch nhiều nguồn dữ liệu

Bản tin này **không mang dữ liệu mới**. Nó chỉ báo cho SQLBot: "cấu trúc của các nguồn dưới đây vừa
đổi, đọc lại đi". SQLBot tự kết nối vào DB nguồn, đọc lại danh sách bảng, cột, kiểu dữ liệu, chú
thích và khoá ngoại, rồi cập nhật metadata của mình theo đúng những gì DB nguồn **đang** có.

Gửi bản tin này sau khi SW tạo/xoá/sửa bảng ở DB nghiệp vụ. Không gửi thì SQLBot vẫn dùng metadata
cũ: bảng mới không hỏi được, bảng đã xoá vẫn xuất hiện trong ngữ cảnh sinh SQL và làm câu trả lời
sai.

```json
{
  "datasourceIds": ["7", "12"]
}
```

| Trường | Kiểu | Bắt buộc | Ghi chú |
|---|---|---|---|
| `datasourceIds` | array<string> | Có | **Id nguồn dữ liệu phía SQLBot** (số, dạng chuỗi) — cùng họ với `queries[].datasourceId` ở §4. **Không được rỗng** (§8 `EMPTY_DATASOURCE_LIST`), **không được lặp** (§8 `DUPLICATE_DATASOURCE_ID`) |

### 6.1. Ngữ nghĩa: mirror toàn bộ schema nguồn

- Trạng thái đích là **toàn bộ bảng có trong schema nguồn**, không phải phần chênh lệch: bảng mới
  xuất hiện được thêm vào và bật sẵn, bảng đã biến mất bị xoá khỏi metadata kèm toàn bộ cột và quan
  hệ liên quan tới nó.
- Phạm vi này có thể **rộng hơn** danh sách bảng đang được chọn thủ công trong giao diện SQLBot. Sau
  đồng bộ, mọi bảng của schema đều nằm trong metadata. Việc ai được hỏi bảng nào **không** thay đổi
  theo bản tin này — vẫn do quyền gửi qua `actionType = 1` (§4) quyết định.
- Đổi tên một bảng ở nguồn được hiểu là **xoá bảng cũ + thêm bảng mới**. Mọi mô tả hiển thị và chú
  thích cột SW đã đặt cho bảng cũ **không đi theo** tên mới → sau khi đổi tên bảng, SW phải đẩy lại
  phần mô tả.
- Chỉ metadata (cấu trúc) được đồng bộ. Dữ liệu trong bảng không bị đọc, không bị chép đi đâu.

### 6.2. Kết quả tính theo TỪNG nguồn — khác `actionType = 1`

Hai tầng khác nhau, đừng gộp:

1. **Lỗi cấu trúc payload** (`datasourceIds` rỗng, id lặp, id không phải số) → HTTP 400, **không
   nguồn nào** được xử lý.
2. Qua được tầng đó thì mỗi nguồn xử lý **độc lập**: HTTP luôn **200** và `status` cấp request luôn
   `SUCCESS`, kể cả khi mọi nguồn trong batch đều hỏng. Kết cục thật của từng nguồn nằm ở
   `results[]`. **Phải đọc `results[]`**, không kết luận theo HTTP status.

Với mỗi phần tử của `results[]`:

| Trường | Ý nghĩa |
|---|---|
| `datasourceId` | Vọng lại đúng chuỗi id SW đã gửi |
| `status` | `SUCCESS` hoặc `FAILED` |
| `applied.upserted` | Số bảng của nguồn **sau** khi đồng bộ |
| `applied.deleted` | Số bảng bị loại vì không còn ở nguồn |
| `message` | Chỉ có nghĩa khi `FAILED`: lý do nguồn đó không đồng bộ được |

Các lý do một nguồn `FAILED`:

| Tình huống | `message` |
|---|---|
| Id không tồn tại trong SQLBot | `datasource không tồn tại` |
| Nguồn kiểu Excel/CSV (không có DB nguồn ngoài để đọc lại) | `nguồn excel không có DB nguồn để đồng bộ lại` |
| Nguồn đang trong quá trình nạp dữ liệu | `nguồn đang nạp dữ liệu (Importing), thử lại sau` |
| Không kết nối được DB nguồn, hoặc đọc catalog lỗi | Thông điệp lỗi gốc từ driver DB |
| DB nguồn trả về **0 bảng** | SQLBot **từ chối** thay vì xoá sạch metadata — 0 bảng gần như luôn là cấu hình schema sai hoặc quyền DB bị thu hồi, không phải "schema rỗng thật" |

### 6.3. Hai điểm cần lưu ý khi tích hợp

⚠ **Xử lý đồng bộ ngay trong request.** Không có hàng đợi nền: request chỉ trả về khi đã đọc xong
catalog của tất cả các nguồn trong batch. Đo thực tế: một nguồn PostgreSQL khoảng 100 bảng mất
**4–8 giây**, nguồn 537 bảng mất **20–25 giây**. Con số dao động theo độ trễ mạng tới DB nguồn nên
đừng lấy làm ngưỡng cứng. Batch càng nhiều nguồn thì request treo càng lâu → chia nhỏ batch và đặt
timeout phía SW rộng rãi (tối thiểu vài phút).

⚠ **Gọi lại bản tin cũ luôn an toàn.** Bản tin không mang trạng thái riêng, nên gọi lại một bản tin
cũ chỉ là đọc lại DB nguồn thêm một lần nữa — vô hại. Chống xử lý trùng khi retry vẫn theo
`X-Idempotency-Key` như mọi actionType khác (§7).

### 6.4. Response mẫu

```json
{
  "requestId": "req-042",
  "idempotencyKey": "9f1d2b3c-...-uuid",
  "actionType": 4,
  "status": "SUCCESS",
  "logId": "b3f1c2a0-...-uuid",
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

## 7. Idempotency

**`X-Idempotency-Key` là bắt buộc.** Gửi lại đúng key đã dùng trước đó (ví dụ do timeout mạng và
SW tự động retry) sẽ **không xử lý lại** — trả 200 với `status: "DUPLICATE"`, cho biết lần xử lý
đầu tiên đã kết thúc ra sao (kể cả khi lần đầu đó FAILED).

⚠ **Response `DUPLICATE` không dựng lại được chi tiết của lần đầu.** `results` **luôn** là mảng
rỗng và `applied` **luôn** là `{"upserted": 0, "deleted": 0}` — bảng audit không lưu phần chi tiết
theo từng user/nguồn nên không có gì để trả lại. Đừng đọc hai trường này ở nhánh DUPLICATE:
`applied.upserted = 0` ở đây **không** có nghĩa là chưa ghi được gì. Căn cứ duy nhất để biết lần
đầu ra sao là `errorCode` — `null` nghĩa là lần đầu SUCCESS, khác `null` nghĩa là lần đầu FAILED
(kèm `errorMessage` và `logId` của chính lần đó). Cần chi tiết từng phần tử thì tra theo `logId`.

⚠ **Một key chỉ dùng được cho đúng một nội dung bản tin.** Kiểm trùng chạy **trước** khi validate
`payload`, và mọi response có `logId` (400, 422, 500) đều đã ghi dấu key đó lại. Nên kịch bản "gửi
payload sai → nhận 400 → sửa payload → gửi lại **cùng key**" khiến bản tin đã sửa **không bao giờ
được xử lý**: SW chỉ nhận lại đúng lỗi cũ dưới dạng `DUPLICATE`. Ca 500 `INTERNAL_ERROR` cũng vậy —
retry cùng key sau lỗi 500 chỉ nhận lại chính lỗi đó, không phải một lần thử mới.

Quy tắc rút gọn: **retry cùng key chỉ dành cho ca không nhận được response** (timeout, đứt mạng,
502/504 của proxy). Đã nhận về một response có body — kể cả body lỗi — thì lần gửi sau phải dùng
key mới. Ngoại lệ duy nhất là `MALFORMED_JSON`: nhánh đó bị chặn trước khi ghi audit nên key chưa
bị tiêu, dùng lại được.

⚠ Không có cơ chế chống bản tin đến sai thứ tự. Với `X-Idempotency-Key` **mới**, mỗi bản tin
`AUTHORIZATION_SYNC` (actionType 1) hợp lệ về cấu trúc đều **ghi đè** full snapshot của lần trước
cho đúng user đó, bất kể bản tin đó thực sự phát sinh trước hay sau về mặt thời gian thực. SW phải tự
đảm bảo thứ tự gửi (ví dụ gửi tuần tự, không bắn song song nhiều bản tin cho cùng `userId`) nếu thứ
tự áp dụng có ý nghĩa với nghiệp vụ.

---

## 8. Bảng lỗi

| HTTP | `status` | `errorCode` | Ý nghĩa |
|---|---|---|---|
| 200 | `SUCCESS` | — | Áp dụng thành công |
| 200 | `DUPLICATE` | (của lần đầu) | Trùng `X-Idempotency-Key`. Chỉ `errorCode`/`errorMessage`/`logId` là của lần đầu; `results` rỗng và `applied` bằng 0 — xem §7 |
| 400 | `FAILED` | `MALFORMED_JSON` | Body không phải JSON hợp lệ hoặc không phải object |
| 400 | `FAILED` | `MISSING_IDEMPOTENCY_KEY` | Thiếu header `X-Idempotency-Key` |
| 400 | `FAILED` | `INVALID_ENVELOPE` | Envelope thiếu trường hoặc sai kiểu |
| 400 | `FAILED` | `INVALID_PAYLOAD` | `payload` sai theo schema ở §4 / §6 (gồm cả `datasourceIds` chứa phần tử không phải số dạng chuỗi) |
| 400 | `FAILED` | `DUPLICATE_DATABASE_TABLE_NAME` | `databaseTableName` bị lặp trong `formQueries` của cùng một user |
| 400 | `FAILED` | `DUPLICATE_DATASOURCE_ID` | `datasourceId` bị lặp — trong `queries` của cùng một form (actionType 1), hoặc trong `datasourceIds` (actionType 4) |
| 400 | `FAILED` | `DUPLICATE_USER_ID` | `userId` bị lặp giữa các phần tử trong `users` |
| 400 | `FAILED` | `EMPTY_USER_LIST` | `payload.users` rỗng hoặc thiếu |
| 400 | `FAILED` | `EMPTY_DATASOURCE_LIST` | `payload.datasourceIds` rỗng hoặc thiếu |
| 422 | `FAILED` | `UNSUPPORTED_ACTION_TYPE` | actionType đã đặt tên nhưng chưa triển khai (2, 3, 5, 6) |
| 422 | `FAILED` | `UNKNOWN_ACTION_TYPE` | actionType không hợp lệ hoặc ngoài phạm vi |
| 500 | `FAILED` | `INTERNAL_ERROR` | Lỗi phía SQLBot |

**actionType 1:** lỗi cấu trúc ở BẤT KỲ user nào trong `users` (kể cả `INVALID_PAYLOAD`,
`DUPLICATE_DATABASE_TABLE_NAME`, `DUPLICATE_USER_ID`, `EMPTY_USER_LIST`) làm **cả request FAILED** —
không user nào trong batch được áp dụng.

**actionType 4:** chỉ ba lỗi cấu trúc `EMPTY_DATASOURCE_LIST` / `DUPLICATE_DATASOURCE_ID` /
`INVALID_PAYLOAD` làm cả request FAILED. Mọi lý do khác (nguồn không tồn tại, không kết nối được,
đang bận nạp…) là **FAILED của riêng nguồn đó** và nằm trong `results[]`, request vẫn 200 (§6.2).

Ngoài bảng trên, hai lỗi xảy ra **trước khi vào hook** (không có `errorCode` theo format này — xem
cảnh báo ở §1): **401** khi thiếu/sai/hết hạn `X-SQLBOT-TOKEN`; **500** kèm thông điệp riêng khi tài
khoản không có quyền `ws_admin`.

---

## 9. Ví dụ `curl`

### 9.1. Đồng bộ quyền (`actionType = 1`)

```bash
# Bước 1: đăng nhập lấy access_token (tài khoản phải có quyền ws_admin)
TOKEN=$(curl -s -X POST "https://<host>/api/v1/login/access-token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=<tai-khoan>&password=<mat-khau>" | jq -r .data.access_token)

# Bước 2: gọi hook kèm token vừa lấy
curl -X POST "https://<host>/api/v1/hooks/ai-sync" \
  -H "Content-Type: application/json" \
  -H "X-SQLBOT-TOKEN: Bearer $TOKEN" \
  -H "X-Request-ID: $(uuidgen)" \
  -H "X-Idempotency-Key: $(uuidgen)" \
  -d '{
    "actionType": 1,
    "payload": {
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
                "queries": [{"datasourceId": "7", "datasourceType": "postgresql", "query": "SELECT * FROM kdl_nhan_khau_row_values WHERE province_id = '\''01'\''"}]
              }
            }
          ]
        }
      ]
    }
  }'
```

Response mong đợi:

```json
{
  "requestId": "...",
  "idempotencyKey": "...",
  "actionType": 1,
  "status": "SUCCESS",
  "logId": "...",
  "results": [
    {"userId": "usr-12345-67890", "status": "SUCCESS", "applied": {"upserted": 1, "deleted": 0}}
  ],
  "applied": {"upserted": 1, "deleted": 0},
  "errorCode": null,
  "errorMessage": null
}
```

### 9.2. Đồng bộ cấu trúc nguồn dữ liệu (`actionType = 4`)

```bash
curl -X POST "https://<host>/api/v1/hooks/ai-sync" \
  --max-time 600 \
  -H "Content-Type: application/json" \
  -H "X-SQLBOT-TOKEN: Bearer $TOKEN" \
  -H "X-Request-ID: $(uuidgen)" \
  -H "X-Idempotency-Key: $(uuidgen)" \
  -d '{
    "actionType": 4,
    "payload": {"datasourceIds": ["7"]}
  }'
```

Response mong đợi: xem §6.4.
