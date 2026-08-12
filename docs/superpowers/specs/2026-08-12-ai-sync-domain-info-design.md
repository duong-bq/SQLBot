# AI Sync Hook — lưu thông tin lĩnh vực (domain) của bảng

> Bổ sung cho domain `apps/hooks/` (xem [2026-08-10-ai-sync-hook-design.md](2026-08-10-ai-sync-hook-design.md),
> [2026-08-11-ai-sync-hook-batch-users-design.md](2026-08-11-ai-sync-hook-batch-users-design.md),
> [2026-08-12-ai-permission-query-api-design.md](2026-08-12-ai-permission-query-api-design.md)).
> Chỉ thêm cột lưu metadata lĩnh vực vào `ai_user_permissions` và phơi ra qua API đọc quyền sẵn có —
> không đổi ngữ nghĩa FULL SNAPSHOT, không đụng route `/hooks/ai-sync` ngoài việc parse thêm field,
> không tích hợp vào pipeline Text2SQL (M-Schema/WHERE) — vẫn ngoài phạm vi như toàn bộ domain này.

## 1. Bối cảnh

SW đã thêm 4 trường lĩnh vực vào `tableInfo` của bản tin `AUTHORIZATION_SYNC`:

```json
"tableInfo": {
  "databaseTableName": "kdl_nhan_khau_row_values",
  "tableDisplayName": "Dữ liệu Nhân khẩu",
  "tableDescription": "Bảng lưu trữ thông tin cư trú của công dân",
  "linhVucMa": "LV_DAN_CU",
  "linhVucUuid": "lv-uuid-5678",
  "linhVucName": "Lĩnh vực Dân cư",
  "linhVucDescription": "Lĩnh vực quản lý các thông tin liên quan đến dân cư",
  "fields": [...]
}
```

Hook hiện tại (`TableInfo` trong `ai_sync_schema.py`) chưa khai các trường này nên bị `extra="ignore"`
âm thầm bỏ qua khi parse — cần thêm để không mất dữ liệu.

## 2. Quyết định đã chốt

| Vấn đề | Quyết định | Lý do |
|---|---|---|
| Cấu trúc lưu | Thêm 4 cột nullable trực tiếp vào `ai_user_permissions`, không tách bảng lĩnh vực riêng | Domain info chỉ là metadata đọc kèm khi truy vấn quyền user, không cần chuẩn hoá/JOIN; nhất quán với cách `table_display_name`/`table_description` đang lưu |
| Thuật ngữ tiếng Anh | `domain` (không dùng `linh_vuc`, không dùng `sector`/`business_domain`) | Ngắn gọn, đúng quy ước code tiếng Anh của repo; `field` đã dùng cho cột dữ liệu trong `tableInfo.fields` nên tránh đụng bằng cách không gọi là `category`/`field_group` |
| Tên cột | `domain_code`, `domain_uuid`, `domain_name`, `domain_description` | Ánh xạ 1-1 với `linhVucMa`/`linhVucUuid`/`linhVucName`/`linhVucDescription` |
| Bắt buộc hay optional | Cả 4 cột optional (nullable), không validate liên trường | Nhất quán với `table_display_name`/`table_description`; SW cũ chưa gửi domain vẫn hoạt động bình thường — không breaking change |
| Index | Thêm index thường (không unique) trên `domain_code` | Chi phí thấp, dọn đường cho nhu cầu lọc "tất cả bảng thuộc lĩnh vực X" sau này mà không cần migration riêng khi nhu cầu đó phát sinh |
| Phạm vi API đọc | Phơi 4 field qua `GET /hooks/ai-sync/permissions/{user_id}` (đã có sẵn từ spec trước), không thêm endpoint mới | Domain info đi kèm `tableInfo` như các field khác, dùng lại đúng route/response đã có |

## 3. Thay đổi dữ liệu

### 3.1. Model — `apps/hooks/models/ai_sync_model.py`

Thêm vào `AiUserPermission`, đặt ngay sau `table_description`:

```python
domain_code: str | None = Field(default=None, sa_column=Column(String(100), nullable=True))
domain_uuid: str | None = Field(default=None, sa_column=Column(String(100), nullable=True))
domain_name: str | None = Field(default=None, sa_column=Column(String(255), nullable=True))
domain_description: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
```

Thêm vào `__table_args__`: `Index("idx_ai_user_permissions_domain_code", "domain_code")`.

### 3.2. Migration — `backend/alembic/versions/075_ai_user_permissions_domain_info.py`

`ALTER TABLE ai_user_permissions ADD COLUMN domain_code/domain_uuid/domain_name/domain_description`
(nullable, không default) + `CREATE INDEX idx_ai_user_permissions_domain_code`. Không backfill — dữ
liệu cũ giữ `NULL`. `down_revision` trỏ về `074_ai_sync_hook`.

## 4. Thay đổi parse input

### 4.1. Schema — `apps/hooks/schemas/ai_sync_schema.py`

Thêm 4 field optional vào `TableInfo`, cùng cấp `table_display_name`/`table_description`:

```python
domain_code: str | None = Field(default=None, alias="linhVucMa")
domain_uuid: str | None = Field(default=None, alias="linhVucUuid")
domain_name: str | None = Field(default=None, alias="linhVucName")
domain_description: str | None = Field(default=None, alias="linhVucDescription")
```

### 4.2. CRUD ghi — `apps/hooks/crud/ai_user_permission.py`

`replace_user_permissions`: thêm 4 khoá vào dict `values` (nằm trong khối đã có, tự động được
`on_conflict_do_update` cập nhật khi domain đổi ở lần sync sau):

```python
"domain_code": form.table_info.domain_code,
"domain_uuid": form.table_info.domain_uuid,
"domain_name": form.table_info.domain_name,
"domain_description": form.table_info.domain_description,
```

## 5. Thay đổi API đọc

### 5.1. Schema output — `apps/hooks/schemas/permission_query_schema.py`

Thêm 4 field cùng tên alias JSON như input vào `PermissionTableInfoOut`:

```python
linhVucMa: str | None = None
linhVucUuid: str | None = None
linhVucName: str | None = None
linhVucDescription: str | None = None
```

`to_permission_detail` map thêm từ `row.domain_code`/`row.domain_uuid`/`row.domain_name`/
`row.domain_description`. Giữ đúng convention hiện có của file: field output đặt tên giống hệt
input SW gửi để dễ đối chiếu "SW gửi gì — SQLBot lưu được gì".

## 6. Kiểm thử

- `tests/test_ai_sync_schema.py`: `TableInfo` parse đúng 4 field domain từ alias `linhVucMa`/...;
  parse OK khi thiếu cả 4 (giữ tương thích ngược với SW cũ).
- `tests/test_ai_sync_models.py`: cột mới tồn tại đúng kiểu/nullable trên `AiUserPermission`.
- `tests/test_ai_sync_crud.py`: `replace_user_permissions` ghi đúng 4 cột domain; upsert lần 2 với
  domain khác thì cập nhật đúng (không giữ giá trị cũ).
- `tests/test_ai_permission_query_schema.py`: `to_permission_detail` map đúng 4 field domain vào
  `PermissionTableInfoOut`, kể cả khi domain toàn `None`.
- `tests/test_ai_permission_query_api.py`: response `GET /hooks/ai-sync/permissions/{user_id}` có
  đủ 4 field domain trong `tableInfo`.

## 7. Tài liệu cần rà lại sau khi implement

Theo bảng ánh xạ trong `CLAUDE.md` — **đề xuất, chưa tự sửa**:

- `backend/scripts/ai_sync_hook/AI_SYNC_HOOK_API_SPEC.md` §4: thêm 4 field domain vào bảng mô tả
  payload `tableInfo`, kèm ví dụ JSON.
- `backend/scripts/ai_sync_hook/AI_PERMISSION_QUERY_API_SPEC.md`: thêm 4 field domain vào ví dụ
  response và bảng mô tả `tableInfo` output.
