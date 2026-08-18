"""Schema Pydantic của AI Sync Hook và các hàm thuần đi kèm.

SW gửi JSON camelCase, code trong repo dùng snake_case, nên mọi trường đều khai alias. Bẫy đáng
chú ý: alias là `clickHouseQuery` (chữ H hoa) — viết sai alias thì query bị bỏ qua âm thầm.
"""

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SyncEnvelope(BaseModel):
    """Phần vỏ chung của mọi bản tin, không phụ thuộc actionType.

    `action_type` ở đây chỉ khai kiểu int cho đủ hình; việc phân biệt "không phải integer" với
    "integer nhưng chưa hỗ trợ" do `resolve_action_type` làm TRƯỚC khi validate envelope, vì hai ca
    đó trả HTTP status khác nhau (400 vs 422) và cần ghi audit khác nhau.

    `extra="ignore"`: SW thêm trường mới ở vỏ thì hook không vỡ.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    action_type: int = Field(alias="actionType")
    version: str = Field(min_length=1)
    timestamp: datetime
    data: dict[str, Any]


class QueryItem(BaseModel):
    """Một phần tử trong `tableInfo.queries[]` — gắn 1 datasource cụ thể với query giới hạn phạm vi
    dữ liệu trên đúng datasource đó. Thay thế 2 field cố định `postgresQuery`/`clickHouseQuery` cũ
    (chỉ hỗ trợ đúng 2 loại datasource) bằng danh sách tổng quát theo `datasourceId`/`datasourceType`.

    `query` được phép rỗng — SW dùng `""` để biểu diễn "không giới hạn dữ liệu" trên datasource đó,
    khác với thiếu hẳn `queries[]` (vô nghĩa, không biết lấy dữ liệu từ nguồn nào — vẫn bị chặn).
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    datasource_id: str = Field(alias="datasourceId", min_length=1)
    datasource_type: str = Field(alias="datasourceType", min_length=1)
    query: str = ""


class TableInfo(BaseModel):
    """Bảng nghiệp vụ kèm danh sách query theo từng datasource.

    Không còn nhận `fields`/`tableDescription` từ AUTHORIZATION_SYNC — field-level metadata và mô
    tả bảng đã chuyển sang endpoint edit field/table riêng, gửi kèm ở đây (nếu có) bị bỏ qua âm thầm
    nhờ `extra="ignore"`.

    `queries` KHÔNG được rỗng/thiếu: một form không biết lấy dữ liệu từ nguồn nào thì vô nghĩa.

    `domain_*` (alias `linhVucMa`/`linhVucUuid`/`linhVucName`/`linhVucDescription`) là metadata
    lĩnh vực nghiệp vụ của bảng, optional — SW cũ chưa gửi vẫn parse hợp lệ.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    database_table_name: str = Field(alias="databaseTableName", min_length=1)
    table_display_name: str | None = Field(default=None, alias="tableDisplayName")
    domain_code: str | None = Field(default=None, alias="linhVucMa")
    domain_uuid: str | None = Field(default=None, alias="linhVucUuid")
    domain_name: str | None = Field(default=None, alias="linhVucName")
    domain_description: str | None = Field(default=None, alias="linhVucDescription")
    queries: list[QueryItem] = Field(alias="queries", min_length=1)


class FormQuery(BaseModel):
    """Quyền của user trên một form: bảng, field, và danh sách query theo datasource (trong
    `table_info.queries`). Không còn field query riêng ở cấp này — đã dời hẳn vào `TableInfo`.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    form_uuid: str = Field(alias="formUuid", min_length=1)
    table_info: TableInfo = Field(alias="tableInfo")


class AuthorizationSyncData(BaseModel):
    """Payload của `actionType = 1`.

    `formQueries` là FULL SNAPSHOT toàn bộ quyền hiện tại của user, không phải phần thay đổi: mảng
    rỗng nghĩa là thu hồi hết quyền.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    user_id: str = Field(alias="userId", min_length=1)
    full_name: str | None = Field(default=None, alias="fullName")
    is_admin: bool = Field(alias="isAdmin")
    form_queries: list[FormQuery] = Field(alias="formQueries")


class AuthorizationSyncBatch(BaseModel):
    """Payload thật của `actionType = 1`: bọc mảng user trong `data.users`.

    Không ép `min_length=1` ở đây — batch rỗng cần trả mã lỗi riêng `EMPTY_USER_LIST` (kiểm tường
    minh ở handler), không lẫn với `INVALID_PAYLOAD` chung của lỗi schema.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    users: list[AuthorizationSyncData]


class DatasourceSyncData(BaseModel):
    """Payload của `actionType = 4` (DATASOURCE_SYNC).

    Bản tin KHÔNG mang trạng thái mới — chỉ là cú hích bảo SQLBot đọc lại catalog DB nguồn của các
    datasource được nêu tên, nên trường duy nhất là danh sách id. `datasourceIds` dùng đúng họ tên
    và kiểu của `queries[].datasourceId` bên AUTHORIZATION_SYNC (id SQLBot, số dạng chuỗi) — không
    dùng họ `dsId` (int) của hợp đồng datasource API, để trong cùng spec hook một khái niệm chỉ có
    một tên.

    Không ép `min_length=1`: mảng rỗng cần mã lỗi riêng `EMPTY_DATASOURCE_LIST` (kiểm ở handler),
    cùng khuôn `AuthorizationSyncBatch`.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    datasource_ids: list[str] = Field(alias="datasourceIds")


class SyncAppliedCounts(BaseModel):
    """Số dòng quyền đã ghi/xoá của một lần xử lý."""

    upserted: int = 0
    deleted: int = 0


class UserSyncResult(BaseModel):
    """Kết quả xử lý của một user trong batch, nằm trong `results` của `SyncHookResponse`."""

    model_config = ConfigDict(populate_by_name=True)

    userId: str
    status: str
    applied: SyncAppliedCounts = Field(default_factory=SyncAppliedCounts)


class DatasourceSyncResult(BaseModel):
    """Kết quả xử lý của một datasource trong batch DATASOURCE_SYNC.

    `applied.upserted` là số bảng sau đồng bộ, `applied.deleted` là số bảng bị loại — giữ đúng
    khuôn `SyncAppliedCounts` để route cộng dồn chung với `UserSyncResult` không cần phân nhánh.
    `message` chỉ có nghĩa khi FAILED: lý do nguồn đó không đồng bộ được.
    """

    model_config = ConfigDict(populate_by_name=True)

    datasourceId: str
    status: str
    applied: SyncAppliedCounts = Field(default_factory=SyncAppliedCounts)
    message: str | None = None


SyncResultItem = UserSyncResult | DatasourceSyncResult


class SyncHookResponse(BaseModel):
    """Body trả cho SW. Tên trường cố ý camelCase để serialize thẳng, không cần alias."""

    model_config = ConfigDict(populate_by_name=True)

    requestId: str | None = None
    idempotencyKey: str | None = None
    actionType: int
    status: str
    logId: str | None = None
    results: list[SyncResultItem] = Field(default_factory=list)
    applied: SyncAppliedCounts = Field(default_factory=SyncAppliedCounts)
    errorCode: str | None = None
    errorMessage: str | None = None


def to_sync_version(ts: datetime) -> int:
    """Quy đổi timestamp của SW thành epoch millis, dùng làm `sync_version`.

    Datetime naive (SW gửi chuỗi không kèm offset) được coi là UTC — nếu không thì `timestamp()` sẽ
    hiểu theo timezone của máy chạy backend, làm mốc chống-lùi lệch theo nơi deploy.
    """
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return int(ts.timestamp() * 1000)


def normalize_queries(items: list[QueryItem]) -> list[dict[str, Any]]:
    """Chuẩn hoá danh sách query về list dict đúng 3 khoá để ghi vào cột JSONB.

    Giữ khoá **camelCase** (`datasourceId`/`datasourceType`/`query`) — khác `normalize_fields` vốn
    dùng khoá đã sẵn cùng tên cả 2 phía — để output API đọc thẳng cột JSONB ra ngoài không phải remap.
    """
    return [
        {"datasourceId": item.datasource_id, "datasourceType": item.datasource_type, "query": item.query}
        for item in items
    ]


def find_duplicate_form_uuid(form_queries: list[FormQuery]) -> str | None:
    """Trả `formUuid` đầu tiên bị lặp trong payload, None nếu không có.

    Kiểm tường minh thay vì để `UNIQUE(user_id, form_uuid)` xử lý: nếu để DB lo thì hành vi thành
    "bản sau đè bản trước" một cách âm thầm, còn ở đây là lỗi 400 rõ ràng cho SW.
    """
    seen: set[str] = set()
    for form in form_queries:
        if form.form_uuid in seen:
            return form.form_uuid
        seen.add(form.form_uuid)
    return None


def find_duplicate_datasource_id(queries: list[QueryItem]) -> str | None:
    """Trả `datasourceId` đầu tiên bị lặp trong `queries` của MỘT form, None nếu không có.

    Cùng khuôn `find_duplicate_form_uuid` nhưng phạm vi kiểm là 1 form, không phải toàn batch —
    kiểm tường minh để tránh DB âm thầm ghép đè khi lưu vào JSONB (JSONB không có ràng buộc unique
    trên phần tử mảng).
    """
    seen: set[str] = set()
    for item in queries:
        if item.datasource_id in seen:
            return item.datasource_id
        seen.add(item.datasource_id)
    return None


def find_first_duplicate(values: list[str]) -> str | None:
    """Trả phần tử đầu tiên bị lặp trong một danh sách chuỗi trần, None nếu không có.

    Bản tổng quát của các `find_duplicate_*` bên trên, dùng cho payload dạng mảng id không bọc
    object (vd `datasourceIds` của DATASOURCE_SYNC) — kiểm tường minh để trả lỗi 400 rõ ràng thay
    vì để phần tử sau âm thầm lặp việc của phần tử trước trong vòng áp dụng.
    """
    seen: set[str] = set()
    for value in values:
        if value in seen:
            return value
        seen.add(value)
    return None


def find_duplicate_user_id(users: list[AuthorizationSyncData]) -> str | None:
    """Trả `userId` đầu tiên bị lặp trong batch, None nếu không có.

    Giống `find_duplicate_form_uuid` nhưng ở cấp user: kiểm tường minh trước khi áp dụng, để phần
    tử sau không âm thầm đè kết quả của phần tử trước trong vòng lặp áp dụng của handler.
    """
    seen: set[str] = set()
    for user in users:
        if user.user_id in seen:
            return user.user_id
        seen.add(user.user_id)
    return None
