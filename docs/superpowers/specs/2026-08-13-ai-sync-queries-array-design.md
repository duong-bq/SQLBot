# AI Sync Hook — chuyển `postgresQuery`/`clickHouseQuery` sang mảng `queries[]` tổng quát

> Bổ sung cho domain `apps/hooks/` (xem [2026-08-10-ai-sync-hook-design.md](2026-08-10-ai-sync-hook-design.md),
> [2026-08-11-ai-sync-hook-batch-users-design.md](2026-08-11-ai-sync-hook-batch-users-design.md),
> [2026-08-12-ai-permission-query-api-design.md](2026-08-12-ai-permission-query-api-design.md),
> [2026-08-12-ai-sync-domain-info-design.md](2026-08-12-ai-sync-domain-info-design.md)).
> Đây là **breaking change** trên input schema `AUTHORIZATION_SYNC`: xoá hẳn 2 field
> `postgresQuery`/`clickHouseQuery`, không giữ tương thích ngược — SW phải chuyển đổi đồng thời với
> lúc SQLBot triển khai bản này.

## 1. Bối cảnh

SW cập nhật cách bắn query để tổng quát hơn thay vì mặc định cứng 2 loại datasource (PostgreSQL,
ClickHouse). Thay vì `formQueries[].postgresQuery` / `formQueries[].clickHouseQuery` (2 field cố
định, ngang cấp `tableInfo`), SW gửi một mảng `tableInfo.queries[]` — mỗi phần tử gắn với 1
`datasourceId`/`datasourceType` cụ thể:

```json
"tableInfo": {
  "databaseTableName": "kdl_nhan_khau_row_values",
  "fields": [...],
  "queries": [
    {"datasourceId": "ds-001", "datasourceType": "postgresql", "query": "SELECT ..."},
    {"datasourceId": "ds-002", "datasourceType": "clickhouse", "query": "SELECT ..."}
  ]
}
```

Một bảng chỉ có 1 nguồn OLAP thì `queries` có 1 phần tử; bảng có cả OLTP và OLAP thì nhiều phần tử.
Số lượng và loại datasource không còn cố định ở 2 như trước.

## 2. Quyết định đã chốt

| Vấn đề | Quyết định | Lý do |
|---|---|---|
| Tương thích ngược | **Cắt hẳn** sang `queries[]`, xoá `postgresQuery`/`clickHouseQuery` khỏi input schema | Đơn giản nhất về code (không cần xử lý 2 format song song); đổi lại cần SW chuyển đổi đồng thời với lúc SQLBot triển khai bản này |
| Vị trí `queries[]` | Nằm **trong `tableInfo`**, không phải ngang cấp `tableInfo` như 2 field cũ | Đúng theo payload mẫu SW gửi — `queries` mô tả cách truy vấn CHÍNH bảng đó trên từng nguồn, hợp lý hơn khi đặt cùng cấp `databaseTableName`/`fields` |
| Cách lưu trên `ai_user_permissions` | 1 cột JSONB `queries` (mảng), thay thế 2 cột `postgres_query`/`clickhouse_query` | Nhất quán với cách cột `fields` đang lưu (JSONB list of dict), không cần bảng con/JOIN, không cần logic xoá-chèn lại con mỗi lần sync |
| Khoá lưu trong JSONB | Giữ nguyên **camelCase** (`datasourceId`, `datasourceType`, `query`) — không chuyển snake_case | Cho phép output API đọc thẳng `row.queries` rồi trả ra ngoài không qua remap, giống hệt cách cột `fields` đang được `to_permission_detail` dùng lại trực tiếp |
| `queries` bắt buộc/rỗng | **Bắt buộc, tối thiểu 1 phần tử** | Một form không có query nào thì vô nghĩa — không biết lấy dữ liệu từ nguồn nào; rỗng bị từ chối `400 INVALID_PAYLOAD` |
| `datasourceType` | Free string, không validate theo enum | SQLBot chỉ lưu để đọc lại ở phase này, không dùng để route/chạy query; SW có thể thêm datasource mới mà không cần SQLBot deploy lại |
| Trùng `datasourceId` trong cùng 1 form | Kiểm tường minh, trả `400 DUPLICATE_DATASOURCE_ID` | Nhất quán với cách `DUPLICATE_FORM_UUID`/`DUPLICATE_USER_ID` đang được kiểm tường minh thay vì để DB âm thầm ghép đè |
| Cột `postgres_query`/`clickhouse_query` cũ | **Drop hẳn** trong cùng migration thêm cột `queries` | Đã cắt hẳn sang format mới nên không còn ai ghi/đọc 2 cột này; dữ liệu cũ dù sao cũng bị ghi đè ở lần `AUTHORIZATION_SYNC` kế tiếp của từng user (full snapshot) |

## 3. Thay đổi input schema — `apps/hooks/schemas/ai_sync_schema.py`

Thêm `QueryItem`:

```python
class QueryItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    datasource_id: str = Field(alias="datasourceId", min_length=1)
    datasource_type: str = Field(alias="datasourceType", min_length=1)
    query: str = Field(min_length=1)
```

`TableInfo` thêm field `queries: list[QueryItem] = Field(alias="queries", min_length=1)`, đặt sau
`domain_description`, trước `field_list`.

`FormQuery` **xoá** `postgres_query`/`clickhouse_query` (2 field alias `postgresQuery`/
`clickHouseQuery`) — không còn ở cấp này nữa.

Thêm hàm thuần `normalize_queries(items: list[QueryItem]) -> list[dict[str, Any]]`, cùng khuôn
`normalize_fields`: trả `[{"datasourceId": ..., "datasourceType": ..., "query": ...}, ...]` (camelCase,
xem quyết định ở §2).

Thêm hàm thuần `find_duplicate_datasource_id(queries: list[QueryItem]) -> str | None`, cùng khuôn
`find_duplicate_form_uuid`.

## 4. Thay đổi handler — `apps/hooks/handlers/authorization.py`

Trong bước validate cấu trúc CẢ BATCH (trước khi áp dụng bất kỳ ai — giữ nguyên nguyên tắc
all-or-nothing đã có), lặp thêm: với mỗi `form` trong `user_data.form_queries`, gọi
`find_duplicate_datasource_id(form.table_info.queries)`; trùng thì raise
`SyncHookError(400, SyncErrorCode.DUPLICATE_DATASOURCE_ID, f"datasourceId bị lặp trong payload của
user {user_data.user_id}, form {form.form_uuid}: {dup}")`.

## 5. Thay đổi mã lỗi — `apps/hooks/constants.py`

Thêm `DUPLICATE_DATASOURCE_ID = "DUPLICATE_DATASOURCE_ID"` vào `SyncErrorCode`.

## 6. Thay đổi model — `apps/hooks/models/ai_sync_model.py`

Trên `AiUserPermission`: xoá field `postgres_query`/`clickhouse_query`, thêm:

```python
queries: list[dict[str, Any]] = Field(
    default_factory=list,
    sa_column=Column(JSONB, nullable=False, server_default=text("'[]'::jsonb")),
)
```

## 7. Migration — `backend/alembic/versions/076_ai_user_permissions_queries.py`

`down_revision` trỏ về `075a1b2c3d4e` (migration domain info gần nhất).

```python
def upgrade():
    op.add_column('ai_user_permissions', sa.Column(
        'queries', postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'[]'::jsonb"), nullable=False,
    ))
    op.drop_column('ai_user_permissions', 'postgres_query')
    op.drop_column('ai_user_permissions', 'clickhouse_query')

def downgrade():
    op.add_column('ai_user_permissions', sa.Column('clickhouse_query', sa.Text(), nullable=True))
    op.add_column('ai_user_permissions', sa.Column('postgres_query', sa.Text(), nullable=True))
    op.drop_column('ai_user_permissions', 'queries')
```

Không backfill `queries` từ dữ liệu `postgres_query`/`clickhouse_query` cũ — không đủ thông tin
`datasourceId`/`datasourceType` để suy ra từ 2 cột cũ một cách đáng tin cậy.

## 8. Thay đổi CRUD ghi — `apps/hooks/crud/ai_user_permission.py`

`replace_user_permissions`: bỏ `"postgres_query"`/`"clickhouse_query"` khỏi dict `values`, thay bằng
`"queries": normalize_queries(form.table_info.queries)`.

## 9. Thay đổi output API — `apps/hooks/schemas/permission_query_schema.py`

`PermissionFormQueryOut` xoá `postgresQuery`/`clickHouseQuery`. `PermissionTableInfoOut` thêm
`queries: list[dict[str, Any]]`, đặt sau `fields`. `to_permission_detail` map
`queries=row.queries` (đọc thẳng, không remap khoá — theo quyết định ở §2, cùng cách `fields`
đang được truyền thẳng `fields=row.fields`).

## 10. Kiểm thử

- `tests/test_ai_sync_schema.py`: `TableInfo` parse đúng `queries[]` (alias `datasourceId`/
  `datasourceType`); `queries` rỗng hoặc thiếu bị từ chối (`ValidationError`); `normalize_queries`
  trả đúng 3 khoá camelCase; `find_duplicate_datasource_id` phát hiện trùng và trả `None` khi không
  trùng. Xoá/sửa mọi assertion cũ liên quan `postgresQuery`/`clickHouseQuery` ở cấp `FormQuery`.
- `tests/test_ai_sync_models.py`: `AiUserPermission` có cột `queries`, không còn cột
  `postgres_query`/`clickhouse_query`.
- `tests/test_ai_sync_crud.py`: `replace_user_permissions` ghi đúng `queries` (camelCase, nhiều
  phần tử); upsert lần 2 với `queries` khác thì cập nhật đúng.
- `tests/test_ai_sync_handler.py`: batch có 1 form trùng `datasourceId` → raise `SyncHookError`
  với `DUPLICATE_DATASOURCE_ID`, không user nào trong batch được áp dụng (theo mẫu
  `test_trung_form_uuid_trong_1_user_raise_duplicate_form_uuid`).
- `tests/test_ai_sync_api.py`, `tests/test_ai_sync_e2e.py`: cập nhật `_body`/`VALID_BODY` sang
  `queries[]`; thêm ca `DUPLICATE_DATASOURCE_ID` trả `400` ở tầng route.
- `tests/test_ai_permission_query_schema.py`, `tests/test_ai_permission_query_api.py`: cập nhật
  fixture/assertion sang `queries[]` ở `tableInfo`, không còn `postgresQuery`/`clickHouseQuery`.

## 11. Tài liệu cần rà lại sau khi implement

Theo bảng ánh xạ trong `CLAUDE.md` — **đề xuất, chưa tự sửa**:

- `backend/scripts/ai_sync_hook/AI_SYNC_HOOK_API_SPEC.md` §4: thay ví dụ JSON và bảng mô tả field
  — bỏ `formQueries[].postgresQuery`/`formQueries[].clickHouseQuery`, thêm
  `tableInfo.queries[]`/`queries[].datasourceId`/`queries[].datasourceType`/`queries[].query`.
  §7 (bảng lỗi): thêm dòng `DUPLICATE_DATASOURCE_ID`.
- `backend/scripts/ai_sync_hook/AI_PERMISSION_QUERY_API_SPEC.md`: cập nhật ví dụ response — bỏ
  `postgresQuery`/`clickHouseQuery` ở cấp `formQueries[]`, thêm `queries[]` trong `tableInfo`.
