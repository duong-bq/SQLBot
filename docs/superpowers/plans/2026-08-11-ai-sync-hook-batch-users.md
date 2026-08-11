# AI Sync Hook — Batch nhiều user Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Đổi payload `AUTHORIZATION_SYNC` (`actionType = 1`) của `/api/v1/hooks/ai-sync` để nhận một mảng user (`data.users`) trong 1 request thay vì đúng 1 user, để SW gửi được ảnh hưởng của 1 lần đổi quyền role tới cả danh sách user trong 1 lần gọi.

**Architecture:** Route giữ nguyên vai trò giao thức (idempotency, audit, dịch lỗi HTTP). Handler `handle_authorization_sync` nhận batch: validate cấu trúc TOÀN BỘ batch trước (all-or-nothing với lỗi cấu trúc), sau đó lặp áp dụng từng user với `STALE` tính riêng theo từng user, gom kết quả vào `results[]`, commit đúng 1 lần cho cả batch. Response trả `results: list[{userId, status, applied}]` thay vì field đơn `userId`/`applied`.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic v2, SQLModel/SQLAlchemy, Postgres (JSONB, `ON CONFLICT`), pytest.

## Global Constraints

- Đây là breaking change đã được thống nhất: **không** giữ nhánh tương thích payload 1-user (object) cũ.
- Không đổi schema 2 bảng (`ai_sync_hook_logs`, `ai_user_permissions`) — không cần migration Alembic mới.
- Không đổi cơ chế xác thực (JWT `X-SQLBOT-TOKEN` + `ws_admin`), không đổi `X-Idempotency-Key` (vẫn 1 key/request, vẫn 1 dòng audit/request).
- Docstring hàm mới/sửa phải viết bằng tiếng Việt (quy ước CLAUDE.md của repo).
- Test DB (`tests/test_ai_sync_handler.py`, `tests/test_ai_sync_permission_crud.py`, `tests/test_ai_sync_e2e.py`) cần biến môi trường `SQLBOT_TEST_DB_URL`; không có thì tự skip, không fail.
- Tham chiếu thiết kế đầy đủ: [`docs/superpowers/specs/2026-08-11-ai-sync-hook-batch-users-design.md`](../specs/2026-08-11-ai-sync-hook-batch-users-design.md).

---

### Task 1: Schema — `AuthorizationSyncBatch`, `find_duplicate_user_id`, `UserSyncResult`

**Files:**
- Modify: `backend/apps/hooks/schemas/ai_sync_schema.py`
- Test: `tests/test_ai_sync_schema.py`

**Interfaces:**
- Produces: `AuthorizationSyncBatch(BaseModel)` với field `users: list[AuthorizationSyncData]`.
- Produces: `find_duplicate_user_id(users: list[AuthorizationSyncData]) -> str | None`.
- Produces: `UserSyncResult(BaseModel)` với field `userId: str`, `status: str`, `applied: SyncAppliedCounts`.
- Produces: `SyncHookResponse` đổi field — bỏ `userId: str | None`, thêm `results: list[UserSyncResult] = Field(default_factory=list)`.

- [ ] **Step 1: Viết test cho `AuthorizationSyncBatch` và `find_duplicate_user_id` (test FAIL vì chưa có code)**

Thêm vào `tests/test_ai_sync_schema.py`, sau dòng import hiện tại đổi thành:

```python
from apps.hooks.schemas.ai_sync_schema import (
    AuthorizationSyncBatch,
    AuthorizationSyncData,
    SyncEnvelope,
    SyncHookResponse,
    UserSyncResult,
    find_duplicate_form_uuid,
    find_duplicate_user_id,
    normalize_fields,
    to_sync_version,
)
```

Thêm vào cuối file:

```python
BATCH_DATA = {"users": [VALID_DATA, {"userId": "u2", "isAdmin": True, "formQueries": []}]}


def test_batch_hop_le_parse_dung_2_user():
    batch = AuthorizationSyncBatch.model_validate(BATCH_DATA)
    assert len(batch.users) == 2
    assert batch.users[0].user_id == "usr-12345-67890"
    assert batch.users[1].user_id == "u2"
    assert batch.users[1].is_admin is True


def test_batch_users_rong_van_parse_duoc_schema():
    # Schema không tự chặn rỗng — EMPTY_USER_LIST là lỗi tường minh ở handler (Task 4),
    # không lẫn với lỗi schema chung.
    batch = AuthorizationSyncBatch.model_validate({"users": []})
    assert batch.users == []


def test_batch_thieu_truong_users_bi_loai():
    with pytest.raises(ValidationError):
        AuthorizationSyncBatch.model_validate({})


def test_find_duplicate_user_id():
    batch = AuthorizationSyncBatch.model_validate(
        {
            "users": [
                {"userId": "u1", "isAdmin": False, "formQueries": []},
                {"userId": "u1", "isAdmin": False, "formQueries": []},
            ]
        }
    )
    assert find_duplicate_user_id(batch.users) == "u1"


def test_find_duplicate_user_id_tra_none_khi_khong_trung():
    batch = AuthorizationSyncBatch.model_validate(
        {
            "users": [
                {"userId": "u1", "isAdmin": False, "formQueries": []},
                {"userId": "u2", "isAdmin": False, "formQueries": []},
            ]
        }
    )
    assert find_duplicate_user_id(batch.users) is None


def test_user_sync_result_serialize_dung_ten():
    result = UserSyncResult(userId="u1", status="SUCCESS")
    assert result.model_dump() == {
        "userId": "u1", "status": "SUCCESS", "applied": {"upserted": 0, "deleted": 0}
    }
```

Sửa test cũ `test_response_serialize_dung_ten_camel_case` (đang assert `body["applied"]`) thành:

```python
def test_response_serialize_dung_ten_camel_case():
    body = SyncHookResponse(actionType=1, status="SUCCESS").model_dump()
    assert body["actionType"] == 1
    assert body["status"] == "SUCCESS"
    assert body["applied"] == {"upserted": 0, "deleted": 0}
    assert body["results"] == []
    assert body["errorCode"] is None
```

- [ ] **Step 2: Chạy test để xác nhận FAIL**

Run: `pytest tests/test_ai_sync_schema.py -v`
Expected: FAIL — `ImportError: cannot import name 'AuthorizationSyncBatch'` (hoặc tương tự cho `find_duplicate_user_id`, `UserSyncResult`).

- [ ] **Step 3: Thêm `AuthorizationSyncBatch` và `find_duplicate_user_id` vào `ai_sync_schema.py`**

Thêm sau class `AuthorizationSyncData` (sau dòng `form_queries: list[FormQuery] = Field(alias="formQueries")`):

```python
class AuthorizationSyncBatch(BaseModel):
    """Payload thật của `actionType = 1`: bọc mảng user trong `data.users`.

    Không ép `min_length=1` ở đây — batch rỗng cần trả mã lỗi riêng `EMPTY_USER_LIST` (kiểm tường
    minh ở handler), không lẫn với `INVALID_PAYLOAD` chung của lỗi schema.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    users: list[AuthorizationSyncData]
```

Thêm sau hàm `find_duplicate_form_uuid` (cuối file):

```python
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
```

- [ ] **Step 4: Thêm `UserSyncResult` và sửa `SyncHookResponse`**

Thay khối sau (từ `class SyncHookResponse` tới hết class đó):

```python
class SyncHookResponse(BaseModel):
    """Body trả cho SW. Tên trường cố ý camelCase để serialize thẳng, không cần alias."""

    model_config = ConfigDict(populate_by_name=True)

    requestId: str | None = None
    idempotencyKey: str | None = None
    actionType: int
    status: str
    logId: str | None = None
    userId: str | None = None
    applied: SyncAppliedCounts = Field(default_factory=SyncAppliedCounts)
    errorCode: str | None = None
    errorMessage: str | None = None
```

thành:

```python
class UserSyncResult(BaseModel):
    """Kết quả xử lý của một user trong batch, nằm trong `results` của `SyncHookResponse`."""

    model_config = ConfigDict(populate_by_name=True)

    userId: str
    status: str
    applied: SyncAppliedCounts = Field(default_factory=SyncAppliedCounts)


class SyncHookResponse(BaseModel):
    """Body trả cho SW. Tên trường cố ý camelCase để serialize thẳng, không cần alias."""

    model_config = ConfigDict(populate_by_name=True)

    requestId: str | None = None
    idempotencyKey: str | None = None
    actionType: int
    status: str
    logId: str | None = None
    results: list[UserSyncResult] = Field(default_factory=list)
    applied: SyncAppliedCounts = Field(default_factory=SyncAppliedCounts)
    errorCode: str | None = None
    errorMessage: str | None = None
```

- [ ] **Step 5: Chạy test để xác nhận PASS**

Run: `pytest tests/test_ai_sync_schema.py -v`
Expected: PASS toàn bộ.

- [ ] **Step 6: Commit**

```bash
git add backend/apps/hooks/schemas/ai_sync_schema.py tests/test_ai_sync_schema.py
git commit -m "feat: thêm schema batch nhiều user cho AI Sync Hook"
```

---

### Task 2: Bỏ commit nội bộ trong `replace_user_permissions`

**Files:**
- Modify: `backend/apps/hooks/crud/ai_user_permission.py`
- Test: `tests/test_ai_sync_permission_crud.py`

**Interfaces:**
- Consumes: (không đổi) `replace_user_permissions(session, *, user_id, full_name, is_admin, form_queries, sync_version) -> tuple[int, int]`.
- Produces: hàm trên **không còn tự `session.commit()`** — caller (Task 4) chịu trách nhiệm commit.

**Vì sao task này đứng riêng:** đây là thay đổi hành vi transaction nền tảng mà Task 4 (handler) phụ thuộc vào — tách riêng để có 1 bài test chứng minh đúng hành vi commit trước khi handler dùng tới.

- [ ] **Step 1: Viết test chứng minh hàm không tự commit (test FAIL vì code hiện tại VẪN tự commit)**

Thêm vào đầu `tests/test_ai_sync_permission_crud.py`, sau import hiện có, thêm test mới (đặt trước `test_ghi_moi_snapshot`):

```python
def test_replace_khong_tu_commit_caller_phai_tu_commit(db_session, pg_url):
    """`replace_user_permissions` không tự commit — dữ liệu chỉ thấy được từ session khác sau khi
    caller tự gọi `commit()`. Đây là điều kiện để handler gộp nhiều user vào đúng 1 transaction."""
    from sqlalchemy import create_engine, select
    from sqlmodel import Session

    from apps.hooks.models.ai_sync_model import AiUserPermission

    replace_user_permissions(
        db_session, user_id="u-commit-test", full_name=None, is_admin=False,
        form_queries=_forms(("f1", "t1", ["a"])), sync_version=100,
    )

    other_engine = create_engine(pg_url)
    with Session(other_engine) as other_session:
        rows = other_session.execute(
            select(AiUserPermission).where(AiUserPermission.user_id == "u-commit-test")
        ).scalars().all()
        assert rows == []  # chưa commit ở db_session nên session khác chưa thấy

    db_session.commit()

    with Session(other_engine) as other_session:
        rows = other_session.execute(
            select(AiUserPermission).where(AiUserPermission.user_id == "u-commit-test")
        ).scalars().all()
        assert len(rows) == 1
    other_engine.dispose()
```

Sửa các test hiện có trong file để commit tường minh sau mỗi lần gọi `replace_user_permissions` (khớp hợp đồng mới — caller phải tự commit). Cụ thể, sau MỖI lệnh gọi `replace_user_permissions(...)` trong các hàm `test_ghi_moi_snapshot`, `test_snapshot_moi_xoa_form_khong_con_trong_payload`, `test_form_queries_rong_thu_hoi_het_quyen`, `test_upsert_ghi_de_dung_form_cu`, `test_khong_dung_den_quyen_cua_user_khac`, thêm dòng `db_session.commit()` ngay sau lệnh gọi đó. Ví dụ với `test_ghi_moi_snapshot`:

```python
def test_ghi_moi_snapshot(db_session):
    upserted, deleted = replace_user_permissions(
        db_session, user_id="u1", full_name="Nguyễn Văn A", is_admin=False,
        form_queries=_forms(("f1", "t1", ["a", "b"]), ("f2", "t2", ["c"])), sync_version=100,
    )
    db_session.commit()
    assert (upserted, deleted) == (2, 0)
    rows = get_user_permissions(db_session, "u1")
    ...
```

Áp dụng đúng khuôn mẫu này (thêm `db_session.commit()` ngay sau lệnh gọi, trước các `assert`) cho toàn bộ 5 test còn lại liệt kê ở trên — kể cả các lệnh gọi thứ 2/thứ 3 trong cùng 1 test (vd `test_snapshot_moi_xoa_form_khong_con_trong_payload` có 2 lệnh gọi, cả hai đều cần `db_session.commit()` theo sau).

- [ ] **Step 2: Chạy test mới để xác nhận FAIL**

Run: `pytest tests/test_ai_sync_permission_crud.py::test_replace_khong_tu_commit_caller_phai_tu_commit -v`
Expected: FAIL tại `assert rows == []` — vì code hiện tại tự `commit()` bên trong nên session khác đã thấy dữ liệu ngay.

(Bỏ qua bước này nếu môi trường chưa có `SQLBOT_TEST_DB_URL` — test tự skip, không phải fail thật; xác nhận bằng cách đọc output có dòng `SKIPPED`.)

- [ ] **Step 3: Bỏ `session.commit()` khỏi `replace_user_permissions`**

Trong `backend/apps/hooks/crud/ai_user_permission.py`, sửa:

```python
        session.execute(statement)
        upserted += 1

    session.commit()
    return upserted, deleted
```

thành:

```python
        session.execute(statement)
        upserted += 1

    return upserted, deleted
```

Và thêm một câu vào docstring của hàm (sau đoạn nói về `INSERT ... ON CONFLICT`):

```python
    Không tự commit — caller chịu trách nhiệm commit sau khi lặp áp dụng xong cho cả batch, để
    nhiều user trong cùng request nằm trong đúng 1 transaction (xem `handlers/authorization.py`).
```

- [ ] **Step 4: Chạy lại toàn bộ file test để xác nhận PASS**

Run: `pytest tests/test_ai_sync_permission_crud.py -v`
Expected: PASS toàn bộ (hoặc SKIPPED nếu thiếu `SQLBOT_TEST_DB_URL`, không FAIL).

- [ ] **Step 5: Commit**

```bash
git add backend/apps/hooks/crud/ai_user_permission.py tests/test_ai_sync_permission_crud.py
git commit -m "refactor: bỏ commit nội bộ khỏi replace_user_permissions"
```

---

### Task 3: Handler — xử lý batch, all-or-nothing cấu trúc, STALE theo từng user

**Files:**
- Modify: `backend/apps/hooks/constants.py`
- Modify: `backend/apps/hooks/handlers/authorization.py`
- Test: `tests/test_ai_sync_handler.py`

**Interfaces:**
- Consumes: `AuthorizationSyncBatch`, `find_duplicate_user_id`, `UserSyncResult`, `SyncAppliedCounts` (Task 1); `replace_user_permissions` không tự commit (Task 2); `get_last_applied_version(session, user_id) -> int` (đã có).
- Produces: `HandlerResult(status: SyncStatus, results: list[UserSyncResult])` (thay cho `HandlerResult(user_id, status, upserted, deleted)` cũ) — Task 4 (route) tiêu thụ field này.
- Produces: `SyncErrorCode.EMPTY_USER_LIST`, `SyncErrorCode.DUPLICATE_USER_ID` (hằng số mới).

- [ ] **Step 1: Thêm 2 mã lỗi mới vào `constants.py`**

Trong `backend/apps/hooks/constants.py`, sửa:

```python
    INVALID_PAYLOAD = "INVALID_PAYLOAD"
    DUPLICATE_FORM_UUID = "DUPLICATE_FORM_UUID"
```

thành:

```python
    INVALID_PAYLOAD = "INVALID_PAYLOAD"
    DUPLICATE_FORM_UUID = "DUPLICATE_FORM_UUID"
    EMPTY_USER_LIST = "EMPTY_USER_LIST"
    DUPLICATE_USER_ID = "DUPLICATE_USER_ID"
```

- [ ] **Step 2: Viết lại toàn bộ `tests/test_ai_sync_handler.py` (test FAIL vì handler chưa đổi)**

Thay toàn bộ nội dung file bằng:

```python
"""Test handler actionType=1: validate batch, chống bản tin lùi theo từng user, áp snapshot."""

import pytest

from apps.hooks.constants import SyncActionType, SyncErrorCode, SyncStatus
from apps.hooks.crud.ai_sync_log import create_received_log, finish_log
from apps.hooks.crud.ai_user_permission import get_user_permissions
from apps.hooks.errors import SyncHookError
from apps.hooks.handlers import ACTION_HANDLERS
from apps.hooks.handlers.authorization import handle_authorization_sync
from apps.hooks.schemas.ai_sync_schema import SyncEnvelope


def _envelope(data: dict) -> SyncEnvelope:
    return SyncEnvelope.model_validate(
        {"actionType": 1, "version": "1.0", "timestamp": "2026-08-10T10:00:00Z", "data": data}
    )


def _user(user_id="u1", forms=(("f1", "t1"),)):
    return {
        "userId": user_id,
        "fullName": "Nguyễn Văn A",
        "isAdmin": False,
        "formQueries": [
            {
                "formUuid": form_uuid,
                "tableInfo": {"databaseTableName": table, "fields": [{"id": "a"}]},
                "postgresQuery": f"SELECT * FROM {table}",
            }
            for form_uuid, table in forms
        ],
    }


def _batch(*users) -> dict:
    return {"users": list(users) if users else [_user()]}


def _mark_applied(session, user_id: str, version: int) -> None:
    """Giả lập một bản tin đã áp dụng thành công ở version cho trước."""
    log = create_received_log(
        session, request_id="r", idempotency_key=f"k-{user_id}-{version}",
        action_type=1, schema_version="1.0", user_id=user_id, request_payload={},
    )
    finish_log(session, log, status=SyncStatus.SUCCESS.value, sync_version=version)


def test_dispatch_chi_co_authorization_sync(db_session):
    assert ACTION_HANDLERS[SyncActionType.AUTHORIZATION_SYNC] is handle_authorization_sync
    assert set(ACTION_HANDLERS) == {SyncActionType.AUTHORIZATION_SYNC}


def test_ap_snapshot_1_user_thanh_cong(db_session):
    result = handle_authorization_sync(db_session, _envelope(_batch(_user())), 1000)
    assert result.status is SyncStatus.SUCCESS
    assert len(result.results) == 1
    assert result.results[0].userId == "u1"
    assert result.results[0].status == "SUCCESS"
    assert (result.results[0].applied.upserted, result.results[0].applied.deleted) == (1, 0)
    assert len(get_user_permissions(db_session, "u1")) == 1


def test_ap_snapshot_2_user_deu_thanh_cong(db_session):
    batch = _batch(_user("u1", (("f1", "t1"),)), _user("u2", (("f2", "t2"),)))
    result = handle_authorization_sync(db_session, _envelope(batch), 1000)
    assert result.status is SyncStatus.SUCCESS
    statuses = {r.userId: r.status for r in result.results}
    assert statuses == {"u1": "SUCCESS", "u2": "SUCCESS"}
    assert len(get_user_permissions(db_session, "u1")) == 1
    assert len(get_user_permissions(db_session, "u2")) == 1


def test_users_rong_raise_empty_user_list(db_session):
    with pytest.raises(SyncHookError) as exc:
        handle_authorization_sync(db_session, _envelope({"users": []}), 1000)
    assert exc.value.http_status == 400
    assert exc.value.error_code is SyncErrorCode.EMPTY_USER_LIST


def test_thieu_truong_users_raise_invalid_payload(db_session):
    with pytest.raises(SyncHookError) as exc:
        handle_authorization_sync(db_session, _envelope({}), 1000)
    assert exc.value.http_status == 400
    assert exc.value.error_code is SyncErrorCode.INVALID_PAYLOAD


def test_user_payload_sai_raise_invalid_payload(db_session):
    with pytest.raises(SyncHookError) as exc:
        handle_authorization_sync(db_session, _envelope({"users": [{"userId": "u1"}]}), 1000)
    assert exc.value.http_status == 400
    assert exc.value.error_code is SyncErrorCode.INVALID_PAYLOAD


def test_trung_user_id_raise_duplicate_user_id_khong_ai_duoc_ap_dung(db_session):
    batch = _batch(_user("u1"), _user("u1"))
    with pytest.raises(SyncHookError) as exc:
        handle_authorization_sync(db_session, _envelope(batch), 1000)
    assert exc.value.http_status == 400
    assert exc.value.error_code is SyncErrorCode.DUPLICATE_USER_ID
    assert "u1" in exc.value.message
    assert get_user_permissions(db_session, "u1") == []


def test_trung_form_uuid_trong_1_user_raise_duplicate_form_uuid(db_session):
    data = _user(forms=(("f1", "t1"), ("f1", "t2")))
    with pytest.raises(SyncHookError) as exc:
        handle_authorization_sync(db_session, _envelope(_batch(data)), 1000)
    assert exc.value.http_status == 400
    assert exc.value.error_code is SyncErrorCode.DUPLICATE_FORM_UUID
    assert "f1" in exc.value.message


def test_1_user_loi_cau_truc_thi_ca_batch_khong_ai_duoc_ap_dung(db_session):
    """all-or-nothing: user thứ 2 trùng formUuid thì user thứ 1 (hợp lệ) cũng không được áp dụng."""
    valid_user = _user("u1", (("f1", "t1"),))
    invalid_user = _user("u2", (("f2", "t2"), ("f2", "t3")))
    with pytest.raises(SyncHookError):
        handle_authorization_sync(db_session, _envelope(_batch(valid_user, invalid_user)), 1000)
    assert get_user_permissions(db_session, "u1") == []
    assert get_user_permissions(db_session, "u2") == []


def test_ban_tin_lui_bi_bo_qua(db_session):
    _mark_applied(db_session, "u1", 2000)
    result = handle_authorization_sync(db_session, _envelope(_batch(_user())), 1500)
    assert result.status is SyncStatus.SUCCESS
    assert result.results[0].status == "STALE"
    assert (result.results[0].applied.upserted, result.results[0].applied.deleted) == (0, 0)
    assert get_user_permissions(db_session, "u1") == []


def test_ban_tin_cung_version_bi_bo_qua(db_session):
    _mark_applied(db_session, "u1", 2000)
    result = handle_authorization_sync(db_session, _envelope(_batch(_user())), 2000)
    assert result.results[0].status == "STALE"


def test_ban_tin_moi_hon_duoc_ap_dung(db_session):
    _mark_applied(db_session, "u1", 2000)
    result = handle_authorization_sync(db_session, _envelope(_batch(_user())), 2001)
    assert result.results[0].status == "SUCCESS"
    assert len(get_user_permissions(db_session, "u1")) == 1


def test_stale_tinh_rieng_tung_user_trong_cung_batch(db_session):
    """u1 đã có mốc mới hơn (stale), u2 chưa từng đồng bộ (được áp dụng) — cùng 1 request."""
    _mark_applied(db_session, "u1", 5000)
    batch = _batch(_user("u1", (("f1", "t1"),)), _user("u2", (("f2", "t2"),)))
    result = handle_authorization_sync(db_session, _envelope(batch), 3000)
    statuses = {r.userId: r.status for r in result.results}
    assert statuses == {"u1": "STALE", "u2": "SUCCESS"}
    assert get_user_permissions(db_session, "u1") == []
    assert len(get_user_permissions(db_session, "u2")) == 1


def test_thu_hoi_het_quyen_bang_form_queries_rong(db_session):
    handle_authorization_sync(db_session, _envelope(_batch(_user())), 1000)
    data = _user()
    data["formQueries"] = []
    result = handle_authorization_sync(db_session, _envelope(_batch(data)), 2000)
    assert result.results[0].status == "SUCCESS"
    assert (result.results[0].applied.upserted, result.results[0].applied.deleted) == (0, 1)
    assert get_user_permissions(db_session, "u1") == []
```

- [ ] **Step 3: Chạy test để xác nhận FAIL**

Run: `pytest tests/test_ai_sync_handler.py -v`
Expected: FAIL — `TypeError` khi gọi `AuthorizationSyncData.model_validate(envelope.data)` với payload dạng `{"users": [...]}` (schema cũ không có field `users`), hoặc lỗi tương tự do `HandlerResult` chưa có field `results`.

- [ ] **Step 4: Viết lại `handle_authorization_sync` và `HandlerResult`**

Thay toàn bộ nội dung `backend/apps/hooks/handlers/authorization.py` bằng:

```python
"""Handler cho `actionType = 1` (AUTHORIZATION_SYNC).

Handler nhận BATCH nhiều user (`data.users`). Validate cấu trúc TOÀN BỘ batch trước khi áp dụng bất
kỳ ai (all-or-nothing với lỗi cấu trúc: 1 user sai thì cả batch FAILED). Sau khi qua được bước đó,
lặp áp dụng — mỗi user tự so `sync_version` với mốc đã áp của CHÍNH user đó, nên `STALE` tính riêng
theo từng user chứ không theo cả batch. Không ghi audit log và không biết gì về HTTP — route lo hai
việc đó.
"""

from dataclasses import dataclass, field

from pydantic import ValidationError
from sqlmodel import Session

from apps.hooks.constants import SyncErrorCode, SyncStatus
from apps.hooks.crud.ai_sync_log import get_last_applied_version
from apps.hooks.crud.ai_user_permission import replace_user_permissions
from apps.hooks.errors import SyncHookError
from apps.hooks.schemas.ai_sync_schema import (
    AuthorizationSyncBatch,
    SyncAppliedCounts,
    SyncEnvelope,
    UserSyncResult,
    find_duplicate_form_uuid,
    find_duplicate_user_id,
)


@dataclass
class HandlerResult:
    """Kết quả một lần xử lý bản tin batch, để route dựng response và chốt audit log.

    `status` luôn là `SUCCESS` khi hàm trả về bình thường (không raise) — batch đã qua validate cấu
    trúc và được xử lý xong, bất kể có user nào bên trong bị `STALE`. Chi tiết từng user nằm trong
    `results`.
    """

    status: SyncStatus
    results: list[UserSyncResult] = field(default_factory=list)


def handle_authorization_sync(
    session: Session, envelope: SyncEnvelope, sync_version: int
) -> HandlerResult:
    """Parse payload batch rồi áp FULL SNAPSHOT cho từng user.

    Validate cấu trúc CẢ BATCH trước (schema, `userId` trùng, `formUuid` trùng trong từng user) —
    bất kỳ lỗi nào ở bước này thì raise ngay, KHÔNG áp dụng cho ai. Qua được bước đó mới lặp áp dụng:
    user nào `STALE` (`sync_version` không mới hơn mốc đã áp của CHÍNH user đó) bị bỏ qua, không phải
    lỗi. Toàn bộ vòng lặp áp dụng nằm trong một transaction, commit đúng 1 lần ở cuối — lỗi bất ngờ
    giữa batch phải để caller rollback sạch, không để nửa batch commit dở dang.
    """
    try:
        batch = AuthorizationSyncBatch.model_validate(envelope.data)
    except ValidationError as e:
        raise SyncHookError(400, SyncErrorCode.INVALID_PAYLOAD, str(e)) from e

    if not batch.users:
        raise SyncHookError(400, SyncErrorCode.EMPTY_USER_LIST, "data.users không được rỗng")

    duplicated_user = find_duplicate_user_id(batch.users)
    if duplicated_user:
        raise SyncHookError(
            400,
            SyncErrorCode.DUPLICATE_USER_ID,
            f"userId bị lặp trong payload: {duplicated_user}",
        )

    for user_data in batch.users:
        duplicated_form = find_duplicate_form_uuid(user_data.form_queries)
        if duplicated_form:
            raise SyncHookError(
                400,
                SyncErrorCode.DUPLICATE_FORM_UUID,
                f"formUuid bị lặp trong payload của user {user_data.user_id}: {duplicated_form}",
            )

    results: list[UserSyncResult] = []
    for user_data in batch.users:
        last_applied = get_last_applied_version(session, user_data.user_id)
        if sync_version <= last_applied:
            results.append(UserSyncResult(userId=user_data.user_id, status=SyncStatus.STALE.value))
            continue
        upserted, deleted = replace_user_permissions(
            session,
            user_id=user_data.user_id,
            full_name=user_data.full_name,
            is_admin=user_data.is_admin,
            form_queries=user_data.form_queries,
            sync_version=sync_version,
        )
        results.append(
            UserSyncResult(
                userId=user_data.user_id,
                status=SyncStatus.SUCCESS.value,
                applied=SyncAppliedCounts(upserted=upserted, deleted=deleted),
            )
        )

    session.commit()
    return HandlerResult(status=SyncStatus.SUCCESS, results=results)
```

- [ ] **Step 5: Chạy test để xác nhận PASS**

Run: `pytest tests/test_ai_sync_handler.py -v`
Expected: PASS toàn bộ (hoặc SKIPPED nếu thiếu `SQLBOT_TEST_DB_URL`).

- [ ] **Step 6: Commit**

```bash
git add backend/apps/hooks/constants.py backend/apps/hooks/handlers/authorization.py tests/test_ai_sync_handler.py
git commit -m "feat: handler AI Sync Hook xử lý batch nhiều user, STALE theo từng user"
```

---

### Task 4: Route — trả `results[]`, cập nhật audit log cho batch

**Files:**
- Modify: `backend/apps/hooks/api/ai_sync.py`
- Test: `tests/test_ai_sync_api.py`

**Interfaces:**
- Consumes: `HandlerResult(status, results)` (Task 3), `UserSyncResult`, `SyncAppliedCounts` (Task 1).
- Produces: response JSON có `results: list[{userId, status, applied}]`, `applied` tổng cộng dồn; không còn field `userId` đơn.

- [ ] **Step 1: Sửa `tests/test_ai_sync_api.py` — imports, `VALID_BODY`, `_patch_handler`, các assertion liên quan `userId`/`applied` (test FAIL vì route chưa đổi)**

Sửa khối import ở đầu file:

```python
from apps.hooks.constants import SyncActionType, SyncStatus
from apps.hooks.handlers.authorization import HandlerResult
```

thành:

```python
from apps.hooks.constants import SyncActionType, SyncStatus
from apps.hooks.handlers.authorization import HandlerResult
from apps.hooks.schemas.ai_sync_schema import SyncAppliedCounts, UserSyncResult
```

Sửa `VALID_BODY`:

```python
VALID_BODY = {
    "actionType": 1,
    "version": "1.0",
    "timestamp": "2026-08-10T10:00:00Z",
    "data": {"userId": "usr-1", "fullName": "A", "isAdmin": False, "formQueries": []},
}
```

thành:

```python
VALID_BODY = {
    "actionType": 1,
    "version": "1.0",
    "timestamp": "2026-08-10T10:00:00Z",
    "data": {"users": [{"userId": "usr-1", "fullName": "A", "isAdmin": False, "formQueries": []}]},
}
```

Sửa `_patch_handler`:

```python
def _patch_handler(result=None, error=None):
    handler = MagicMock(
        return_value=result or HandlerResult(user_id="usr-1", status=SyncStatus.SUCCESS, upserted=2, deleted=1)
    )
    if error is not None:
        handler.side_effect = error
    from apps.hooks.handlers import ACTION_HANDLERS

    return patch.dict(ACTION_HANDLERS, {SyncActionType.AUTHORIZATION_SYNC: handler}), handler
```

thành:

```python
def _patch_handler(result=None, error=None):
    handler = MagicMock(
        return_value=result
        or HandlerResult(
            status=SyncStatus.SUCCESS,
            results=[
                UserSyncResult(
                    userId="usr-1", status="SUCCESS",
                    applied=SyncAppliedCounts(upserted=2, deleted=1),
                )
            ],
        )
    )
    if error is not None:
        handler.side_effect = error
    from apps.hooks.handlers import ACTION_HANDLERS

    return patch.dict(ACTION_HANDLERS, {SyncActionType.AUTHORIZATION_SYNC: handler}), handler
```

Sửa `test_thanh_cong_tra_200_va_dem_dung`:

```python
    assert body["userId"] == "usr-1"
```

thành:

```python
    assert body["results"] == [
        {"userId": "usr-1", "status": "SUCCESS", "applied": {"upserted": 2, "deleted": 1}}
    ]
```

Thay toàn bộ `test_ket_qua_stale_tra_200`:

```python
def test_ket_qua_stale_tra_200(client, crud_patches):
    ctx, _ = _patch_handler(result=HandlerResult(user_id="usr-1", status=SyncStatus.STALE))
    with ctx:
        resp = client.post("/api/v1/hooks/ai-sync", json=VALID_BODY, headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["status"] == "STALE"
    assert resp.json()["applied"] == {"upserted": 0, "deleted": 0}
```

thành:

```python
def test_ket_qua_voi_user_stale_tra_200_va_giu_status_stale_trong_results(client, crud_patches):
    ctx, _ = _patch_handler(
        result=HandlerResult(
            status=SyncStatus.SUCCESS,
            results=[UserSyncResult(userId="usr-1", status="STALE")],
        )
    )
    with ctx:
        resp = client.post("/api/v1/hooks/ai-sync", json=VALID_BODY, headers=HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert body["results"] == [
        {"userId": "usr-1", "status": "STALE", "applied": {"upserted": 0, "deleted": 0}}
    ]
    assert body["applied"] == {"upserted": 0, "deleted": 0}
```

Thêm 3 test mới vào cuối file:

```python
def test_body_voi_nhieu_user_tra_ket_qua_tung_user(client, crud_patches):
    ctx, _ = _patch_handler(
        result=HandlerResult(
            status=SyncStatus.SUCCESS,
            results=[
                UserSyncResult(
                    userId="usr-1", status="SUCCESS",
                    applied=SyncAppliedCounts(upserted=1, deleted=0),
                ),
                UserSyncResult(userId="usr-2", status="STALE"),
            ],
        )
    )
    body = dict(
        VALID_BODY,
        data={
            "users": [
                {"userId": "usr-1", "isAdmin": False, "formQueries": []},
                {"userId": "usr-2", "isAdmin": False, "formQueries": []},
            ]
        },
    )
    with ctx:
        resp = client.post("/api/v1/hooks/ai-sync", json=body, headers=HEADERS)
    assert resp.status_code == 200
    r = resp.json()
    assert r["applied"] == {"upserted": 1, "deleted": 0}
    assert [x["userId"] for x in r["results"]] == ["usr-1", "usr-2"]
    assert [x["status"] for x in r["results"]] == ["SUCCESS", "STALE"]


def test_userid_trung_trong_batch_tra_400(client, crud_patches):
    from apps.hooks.constants import SyncErrorCode
    from apps.hooks.errors import SyncHookError

    ctx, _ = _patch_handler(
        error=SyncHookError(400, SyncErrorCode.DUPLICATE_USER_ID, "userId bị lặp trong payload: usr-1")
    )
    body = dict(
        VALID_BODY,
        data={
            "users": [
                {"userId": "usr-1", "isAdmin": False, "formQueries": []},
                {"userId": "usr-1", "isAdmin": False, "formQueries": []},
            ]
        },
    )
    with ctx:
        resp = client.post("/api/v1/hooks/ai-sync", json=body, headers=HEADERS)
    assert resp.status_code == 400
    assert resp.json()["errorCode"] == "DUPLICATE_USER_ID"


def test_users_rong_tra_400(client, crud_patches):
    from apps.hooks.constants import SyncErrorCode
    from apps.hooks.errors import SyncHookError

    ctx, _ = _patch_handler(
        error=SyncHookError(400, SyncErrorCode.EMPTY_USER_LIST, "data.users không được rỗng")
    )
    body = dict(VALID_BODY, data={"users": []})
    with ctx:
        resp = client.post("/api/v1/hooks/ai-sync", json=body, headers=HEADERS)
    assert resp.status_code == 400
    assert resp.json()["errorCode"] == "EMPTY_USER_LIST"
```

- [ ] **Step 2: Chạy test để xác nhận FAIL**

Run: `pytest tests/test_ai_sync_api.py -v`
Expected: FAIL — `TypeError: HandlerResult.__init__() got an unexpected keyword argument 'user_id'` (từ `_patch_handler` gọi `HandlerResult` với field cũ khi route/handler đã đổi shape ở Task 3) hoặc `KeyError`/`AssertionError` trên `body["results"]` (route chưa trả field này).

- [ ] **Step 3: Sửa `backend/apps/hooks/api/ai_sync.py`**

Sửa khối import:

```python
from apps.hooks.schemas.ai_sync_schema import (
    SyncAppliedCounts,
    SyncEnvelope,
    SyncHookResponse,
    to_sync_version,
)
```

thành:

```python
from apps.hooks.schemas.ai_sync_schema import (
    SyncAppliedCounts,
    SyncEnvelope,
    SyncHookResponse,
    UserSyncResult,
    to_sync_version,
)
```

Sửa `_respond`:

```python
def _respond(
    *,
    http_status: int,
    action_type: int,
    status: SyncStatus,
    request_id: str | None = None,
    idempotency_key: str | None = None,
    log_id: str | None = None,
    user_id: str | None = None,
    upserted: int = 0,
    deleted: int = 0,
    error_code: SyncErrorCode | None = None,
    error_message: str | None = None,
) -> JSONResponse:
    """Dựng JSONResponse thống nhất cho mọi nhánh trả về của hook."""
    body = SyncHookResponse(
        requestId=request_id,
        idempotencyKey=idempotency_key,
        actionType=action_type,
        status=status.value,
        logId=log_id,
        userId=user_id,
        applied=SyncAppliedCounts(upserted=upserted, deleted=deleted),
        errorCode=error_code.value if error_code else None,
        errorMessage=error_message,
    )
    return JSONResponse(status_code=http_status, content=body.model_dump())
```

thành:

```python
def _respond(
    *,
    http_status: int,
    action_type: int,
    status: SyncStatus,
    request_id: str | None = None,
    idempotency_key: str | None = None,
    log_id: str | None = None,
    results: list[UserSyncResult] | None = None,
    error_code: SyncErrorCode | None = None,
    error_message: str | None = None,
) -> JSONResponse:
    """Dựng JSONResponse thống nhất cho mọi nhánh trả về của hook.

    `applied` cấp request là tổng cộng dồn `upserted`/`deleted` của tất cả phần tử trong `results`
    — tiện cho SW không phải tự cộng, chi tiết từng user vẫn nằm trong `results`.
    """
    results = results or []
    body = SyncHookResponse(
        requestId=request_id,
        idempotencyKey=idempotency_key,
        actionType=action_type,
        status=status.value,
        logId=log_id,
        results=results,
        applied=SyncAppliedCounts(
            upserted=sum(r.applied.upserted for r in results),
            deleted=sum(r.applied.deleted for r in results),
        ),
        errorCode=error_code.value if error_code else None,
        errorMessage=error_message,
    )
    return JSONResponse(status_code=http_status, content=body.model_dump())
```

Sửa `_duplicate_response`, bỏ dòng `user_id=log.user_id,`:

```python
def _duplicate_response(
    log: AiSyncHookLog, *, request_id: str | None, idempotency_key: str
) -> JSONResponse:
    """Trả kết quả của lần xử lý đầu tiên cho request trùng idempotency key.

    Cố ý trả 200 chứ không 409: SW retry vì timeout mạng (dù lần đầu đã thành công) không nên nhận
    lỗi. `errorCode`/`errorMessage` là của lần đầu, để SW biết lần đầu FAILED hay SUCCESS.
    """
    return _respond(
        http_status=200,
        action_type=log.action_type,
        status=SyncStatus.DUPLICATE,
        request_id=request_id,
        idempotency_key=idempotency_key,
        log_id=str(log.id),
        user_id=log.user_id,
        error_code=SyncErrorCode(log.error_code) if log.error_code else None,
        error_message=log.error_message,
    )
```

thành:

```python
def _duplicate_response(
    log: AiSyncHookLog, *, request_id: str | None, idempotency_key: str
) -> JSONResponse:
    """Trả kết quả của lần xử lý đầu tiên cho request trùng idempotency key.

    Cố ý trả 200 chứ không 409: SW retry vì timeout mạng (dù lần đầu đã thành công) không nên nhận
    lỗi. `errorCode`/`errorMessage` là của lần đầu, để SW biết lần đầu FAILED hay SUCCESS. `results`
    chi tiết từng user của lần đầu KHÔNG được lưu lại (audit log không có cột đó) nên trả rỗng.
    """
    return _respond(
        http_status=200,
        action_type=log.action_type,
        status=SyncStatus.DUPLICATE,
        request_id=request_id,
        idempotency_key=idempotency_key,
        log_id=str(log.id),
        error_code=SyncErrorCode(log.error_code) if log.error_code else None,
        error_message=log.error_message,
    )
```

Thêm hàm `_best_effort_single_user_id` ngay sau `_duplicate_response` (trước hàm `ai_sync`):

```python
def _best_effort_single_user_id(raw_data: Any) -> str | None:
    """Đoán `user_id` để ghi vào audit log RECEIVED trước khi parse đầy đủ.

    Chỉ ghi được khi batch đúng 1 user — nhiều user thì cột `user_id` (đơn) không còn đủ diễn tả,
    để `None` (xem spec `2026-08-11-ai-sync-hook-batch-users-design.md` §3).
    """
    if not isinstance(raw_data, dict):
        return None
    users = raw_data.get("users")
    if not isinstance(users, list) or len(users) != 1:
        return None
    first = users[0]
    if not isinstance(first, dict):
        return None
    user_id = first.get("userId")
    return user_id if isinstance(user_id, str) else None
```

Sửa trong hàm `ai_sync`:

```python
    raw_data = raw.get("data")
    best_effort_user_id = raw_data.get("userId") if isinstance(raw_data, dict) else None
    schema_version = raw.get("version") if isinstance(raw.get("version"), str) else None
```

thành:

```python
    raw_data = raw.get("data")
    best_effort_user_id = _best_effort_single_user_id(raw_data)
    schema_version = raw.get("version") if isinstance(raw.get("version"), str) else None
```

Sửa closure `fail`, bỏ dòng `user_id=log.user_id,`:

```python
    def fail(http_status: int, error_code: SyncErrorCode, message: str) -> JSONResponse:
        """Chốt audit FAILED rồi dựng response lỗi (dùng cho mọi lỗi sau khi đã có dòng log)."""
        finish_log(
            session, log, status=SyncStatus.FAILED.value,
            error_code=error_code.value, error_message=message,
        )
        return _respond(
            http_status=http_status,
            action_type=reported_action_type,
            status=SyncStatus.FAILED,
            request_id=request_id,
            idempotency_key=idempotency_key,
            log_id=str(log.id),
            user_id=log.user_id,
            error_code=error_code,
            error_message=message,
        )
```

thành:

```python
    def fail(http_status: int, error_code: SyncErrorCode, message: str) -> JSONResponse:
        """Chốt audit FAILED rồi dựng response lỗi (dùng cho mọi lỗi sau khi đã có dòng log)."""
        finish_log(
            session, log, status=SyncStatus.FAILED.value,
            error_code=error_code.value, error_message=message,
        )
        return _respond(
            http_status=http_status,
            action_type=reported_action_type,
            status=SyncStatus.FAILED,
            request_id=request_id,
            idempotency_key=idempotency_key,
            log_id=str(log.id),
            error_code=error_code,
            error_message=message,
        )
```

Sửa khối trả kết quả thành công ở cuối hàm `ai_sync`:

```python
    finish_log(session, log, status=result.status.value, sync_version=sync_version)
    return _respond(
        http_status=200,
        action_type=int(action_type),
        status=result.status,
        request_id=request_id,
        idempotency_key=idempotency_key,
        log_id=str(log.id),
        user_id=result.user_id,
        upserted=result.upserted,
        deleted=result.deleted,
    )
```

thành:

```python
    finish_log(session, log, status=result.status.value, sync_version=sync_version)
    return _respond(
        http_status=200,
        action_type=int(action_type),
        status=result.status,
        request_id=request_id,
        idempotency_key=idempotency_key,
        log_id=str(log.id),
        results=result.results,
    )
```

- [ ] **Step 4: Chạy test để xác nhận PASS**

Run: `pytest tests/test_ai_sync_api.py -v`
Expected: PASS toàn bộ.

- [ ] **Step 5: Commit**

```bash
git add backend/apps/hooks/api/ai_sync.py tests/test_ai_sync_api.py
git commit -m "feat: route AI Sync Hook trả results theo từng user trong batch"
```

---

### Task 5: E2E — batch nhiều user chạy hết chuỗi HTTP → DB

**Files:**
- Modify: `tests/test_ai_sync_e2e.py`

**Interfaces:**
- Consumes: toàn bộ chuỗi thật (route Task 4 → handler Task 3 → crud Task 2), không patch gì.

- [ ] **Step 1: Sửa `_body()` để bọc trong `data.users`, sửa 2 assertion phụ thuộc shape cũ**

Sửa hàm `_body`:

```python
def _body(user_id: str, timestamp: str, form_uuids: list[str]) -> dict:
    return {
        "actionType": 1,
        "version": "1.0",
        "timestamp": timestamp,
        "data": {
            "userId": user_id,
            "fullName": "Nguyễn Văn A",
            "isAdmin": False,
            "formQueries": [
                {
                    "formUuid": form_uuid,
                    "tableInfo": {
                        "databaseTableName": "kdl_nhan_khau_row_values",
                        "tableDisplayName": "Dữ liệu Nhân khẩu",
                        "tableDescription": "Bảng lưu trữ thông tin cư trú của công dân",
                        "fields": [
                            {"id": "province_id", "name": "Mã Tỉnh/Thành", "description": "Mã định danh"},
                        ],
                    },
                    "postgresQuery": "SELECT * FROM kdl_nhan_khau_row_values WHERE province_id = '01'",
                    "clickHouseQuery": "SELECT * FROM kdl_nhan_khau_row_values WHERE province_id = '01'",
                }
                for form_uuid in form_uuids
            ],
        },
    }
```

thành:

```python
def _body(user_id: str, timestamp: str, form_uuids: list[str]) -> dict:
    return {
        "actionType": 1,
        "version": "1.0",
        "timestamp": timestamp,
        "data": {
            "users": [
                {
                    "userId": user_id,
                    "fullName": "Nguyễn Văn A",
                    "isAdmin": False,
                    "formQueries": [
                        {
                            "formUuid": form_uuid,
                            "tableInfo": {
                                "databaseTableName": "kdl_nhan_khau_row_values",
                                "tableDisplayName": "Dữ liệu Nhân khẩu",
                                "tableDescription": "Bảng lưu trữ thông tin cư trú của công dân",
                                "fields": [
                                    {"id": "province_id", "name": "Mã Tỉnh/Thành", "description": "Mã định danh"},
                                ],
                            },
                            "postgresQuery": "SELECT * FROM kdl_nhan_khau_row_values WHERE province_id = '01'",
                            "clickHouseQuery": "SELECT * FROM kdl_nhan_khau_row_values WHERE province_id = '01'",
                        }
                        for form_uuid in form_uuids
                    ],
                }
            ]
        },
    }
```

Trong `test_e2e_dong_bo_roi_thu_hoi_va_retry`, sửa:

```python
    assert log.request_payload["data"]["userId"] == "usr-e2e"
```

thành:

```python
    assert log.request_payload["data"]["users"][0]["userId"] == "usr-e2e"
```

Trong `test_e2e_payload_sai_van_de_lai_vet`, sửa:

```python
def test_e2e_payload_sai_van_de_lai_vet(e2e_client, db_session):
    body = {"actionType": 1, "version": "1.0", "timestamp": "2026-08-10T10:00:00Z", "data": {"userId": "u-bad"}}
    resp = e2e_client.post("/api/v1/hooks/ai-sync", json=body, headers=_headers("idem-e2e-badpayload"))
    assert resp.status_code == 400
    assert resp.json()["errorCode"] == "INVALID_PAYLOAD"
    log = get_log_by_idempotency_key(db_session, "idem-e2e-badpayload")
    assert log.status == "FAILED"
    assert log.user_id == "u-bad"
    assert get_user_permissions(db_session, "u-bad") == []
```

thành:

```python
def test_e2e_payload_sai_van_de_lai_vet(e2e_client, db_session):
    body = {
        "actionType": 1, "version": "1.0", "timestamp": "2026-08-10T10:00:00Z",
        "data": {"users": [{"userId": "u-bad"}]},
    }
    resp = e2e_client.post("/api/v1/hooks/ai-sync", json=body, headers=_headers("idem-e2e-badpayload"))
    assert resp.status_code == 400
    assert resp.json()["errorCode"] == "INVALID_PAYLOAD"
    log = get_log_by_idempotency_key(db_session, "idem-e2e-badpayload")
    assert log.status == "FAILED"
    assert log.user_id == "u-bad"
    assert get_user_permissions(db_session, "u-bad") == []
```

- [ ] **Step 2: Chạy lại các test đã sửa để xác nhận vẫn PASS (chưa có test batch mới)**

Run: `pytest tests/test_ai_sync_e2e.py -v`
Expected: PASS toàn bộ (hoặc SKIPPED nếu thiếu `SQLBOT_TEST_DB_URL`).

- [ ] **Step 3: Thêm 2 test e2e mới cho batch thật**

Thêm vào cuối file:

```python
def test_e2e_batch_nhieu_user_1_thanh_cong_1_stale(e2e_client, db_session):
    # usr-stale đã có mốc mới hơn từ 1 bản tin trước đó
    pre = e2e_client.post(
        "/api/v1/hooks/ai-sync",
        json=_body("usr-stale", "2026-08-10T12:00:00Z", ["form-x"]),
        headers=_headers("idem-pre"),
    )
    assert pre.status_code == 200

    batch_body = {
        "actionType": 1,
        "version": "1.0",
        "timestamp": "2026-08-10T10:00:00Z",  # cũ hơn mốc đã áp cho usr-stale
        "data": {
            "users": [
                {
                    "userId": "usr-fresh",
                    "isAdmin": False,
                    "formQueries": [
                        {
                            "formUuid": "form-y",
                            "tableInfo": {"databaseTableName": "t", "fields": [{"id": "a"}]},
                        }
                    ],
                },
                {"userId": "usr-stale", "isAdmin": False, "formQueries": []},
            ]
        },
    }
    resp = e2e_client.post("/api/v1/hooks/ai-sync", json=batch_body, headers=_headers("idem-batch-1"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "SUCCESS"
    results_by_user = {r["userId"]: r["status"] for r in body["results"]}
    assert results_by_user == {"usr-fresh": "SUCCESS", "usr-stale": "STALE"}
    assert body["applied"] == {"upserted": 1, "deleted": 0}
    assert {r.form_uuid for r in get_user_permissions(db_session, "usr-fresh")} == {"form-y"}
    # usr-stale không bị đụng vào — vẫn còn form-x từ bản tin trước
    assert {r.form_uuid for r in get_user_permissions(db_session, "usr-stale")} == {"form-x"}


def test_e2e_batch_1_user_loi_thi_khong_ai_duoc_ap_dung(e2e_client, db_session):
    batch_body = {
        "actionType": 1,
        "version": "1.0",
        "timestamp": "2026-08-10T10:00:00Z",
        "data": {
            "users": [
                {
                    "userId": "usr-ok",
                    "isAdmin": False,
                    "formQueries": [
                        {"formUuid": "form-1", "tableInfo": {"databaseTableName": "t", "fields": []}}
                    ],
                },
                {"userId": "usr-ok", "isAdmin": False, "formQueries": []},  # userId trùng
            ]
        },
    }
    resp = e2e_client.post("/api/v1/hooks/ai-sync", json=batch_body, headers=_headers("idem-batch-bad"))
    assert resp.status_code == 400
    assert resp.json()["errorCode"] == "DUPLICATE_USER_ID"
    assert get_user_permissions(db_session, "usr-ok") == []
```

- [ ] **Step 4: Chạy toàn bộ file để xác nhận PASS**

Run: `pytest tests/test_ai_sync_e2e.py -v`
Expected: PASS toàn bộ (hoặc SKIPPED nếu thiếu `SQLBOT_TEST_DB_URL`).

- [ ] **Step 5: Commit**

```bash
git add tests/test_ai_sync_e2e.py
git commit -m "test: e2e batch nhiều user cho AI Sync Hook"
```

---

### Task 6: Chạy toàn bộ test suite của hook + cập nhật tài liệu API spec

**Files:**
- Modify: `backend/scripts/ai_sync_hook/AI_SYNC_HOOK_API_SPEC.md`

- [ ] **Step 1: Chạy toàn bộ test liên quan hook để xác nhận không có hồi quy**

Run: `pytest tests/test_ai_sync_schema.py tests/test_ai_sync_crud.py tests/test_ai_sync_permission_crud.py tests/test_ai_sync_handler.py tests/test_ai_sync_api.py tests/test_ai_sync_e2e.py tests/test_ai_sync_models.py -v`
Expected: PASS toàn bộ (các test cần DB SKIPPED nếu thiếu `SQLBOT_TEST_DB_URL`, không FAIL).

- [ ] **Step 2: Sửa §1 (ví dụ response) trong `AI_SYNC_HOOK_API_SPEC.md`**

Thay:

```json
{
  "requestId": "req-001",
  "idempotencyKey": "550e8400-e29b-41d4-a716-446655440000",
  "actionType": 1,
  "status": "SUCCESS",
  "logId": "b3f1c2a0-...-uuid",
  "userId": "usr-12345-67890",
  "applied": {"upserted": 2, "deleted": 0},
  "errorCode": null,
  "errorMessage": null
}
```

thành:

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

- [ ] **Step 3: Sửa §4 (payload AUTHORIZATION_SYNC) — đổi tiêu đề mục và ví dụ thành batch**

Thay tiêu đề:

```markdown
## 4. Payload AUTHORIZATION_SYNC (`actionType = 1`)
```

thành:

```markdown
## 4. Payload AUTHORIZATION_SYNC (`actionType = 1`) — batch nhiều user
```

Thay khối JSON ví dụ (bọc payload cũ vào `data.users`):

```json
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
        "tableDescription": "Bảng lưu trữ thông tin cư trú của công dân",
        "fields": [
          {
            "id": "province_id",
            "name": "Mã Tỉnh/Thành",
            "description": "Mã định danh của Tỉnh/Thành phố"
          },
          {
            "id": "full_name",
            "name": "Họ và tên",
            "description": "Tên đầy đủ của công dân"
          }
        ]
      },
      "postgresQuery": "SELECT * FROM kdl_nhan_khau_row_values WHERE province_id = '01'",
      "clickHouseQuery": "SELECT * FROM kdl_nhan_khau_row_values WHERE province_id = '01'"
    }
  ]
}
```

thành:

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
            "tableDescription": "Bảng lưu trữ thông tin cư trú của công dân",
            "fields": [
              {
                "id": "province_id",
                "name": "Mã Tỉnh/Thành",
                "description": "Mã định danh của Tỉnh/Thành phố"
              },
              {
                "id": "full_name",
                "name": "Họ và tên",
                "description": "Tên đầy đủ của công dân"
              }
            ]
          },
          "postgresQuery": "SELECT * FROM kdl_nhan_khau_row_values WHERE province_id = '01'",
          "clickHouseQuery": "SELECT * FROM kdl_nhan_khau_row_values WHERE province_id = '01'"
        }
      ]
    }
  ]
}
```

Thay bảng field ngay dưới ví dụ:

```markdown
| Trường | Kiểu | Bắt buộc | Ghi chú |
|---|---|---|---|
| `userId` | string | Có | Không được rỗng |
| `fullName` | string | Không | |
| `isAdmin` | boolean | Có | |
| `formQueries` | array | Có | **Có thể rỗng** — xem §5 |
| `formQueries[].formUuid` | string | Có | Không được rỗng, không được lặp trong cùng request |
| `formQueries[].tableInfo` | object | Có | |
| `formQueries[].postgresQuery` | string | Không | |
| `formQueries[].clickHouseQuery` | string | Không | |
| `tableInfo.databaseTableName` | string | Có | |
| `tableInfo.tableDisplayName` | string | Không | |
| `tableInfo.tableDescription` | string | Không | |
| `tableInfo.fields` | array | Có | **Có thể rỗng** |
| `fields[].id` | string | Có | |
| `fields[].name` | string | Không | |
| `fields[].description` | string | Không | |

Trùng `formUuid` trong cùng một request nhận **400 `DUPLICATE_FORM_UUID`**.
```

thành:

```markdown
| Trường | Kiểu | Bắt buộc | Ghi chú |
|---|---|---|---|
| `users` | array | Có | **Không được rỗng** — xem §7 mã lỗi `EMPTY_USER_LIST` |
| `users[].userId` | string | Có | Không được rỗng, không được lặp giữa các phần tử trong cùng `users` |
| `users[].fullName` | string | Không | |
| `users[].isAdmin` | boolean | Có | |
| `users[].formQueries` | array | Có | **Có thể rỗng** — xem §5 |
| `formQueries[].formUuid` | string | Có | Không được rỗng, không được lặp trong `formQueries` của CÙNG một user |
| `formQueries[].tableInfo` | object | Có | |
| `formQueries[].postgresQuery` | string | Không | |
| `formQueries[].clickHouseQuery` | string | Không | |
| `tableInfo.databaseTableName` | string | Có | |
| `tableInfo.tableDisplayName` | string | Không | |
| `tableInfo.tableDescription` | string | Không | |
| `tableInfo.fields` | array | Có | **Có thể rỗng** |
| `fields[].id` | string | Có | |
| `fields[].name` | string | Không | |
| `fields[].description` | string | Không | |

`userId` trùng giữa các phần tử trong cùng `users` nhận **400 `DUPLICATE_USER_ID`**. `formUuid` trùng
trong `formQueries` của cùng một user nhận **400 `DUPLICATE_FORM_UUID`** — không liên quan tới user
khác trong cùng batch. `users` rỗng hoặc thiếu nhận **400 `EMPTY_USER_LIST`**.
```

- [ ] **Step 4: Sửa §5 (ngữ nghĩa full snapshot) — làm rõ áp dụng theo TỪNG user trong batch**

Thay đoạn mở đầu:

```markdown
**`formQueries` là toàn bộ quyền hiện tại của user, không phải phần thay đổi.**

- Mỗi lần gọi thành công, SQLBot **thay thế toàn bộ** danh sách quyền của `userId` bằng đúng những
  gì có trong `formQueries`.
```

thành:

```markdown
**`formQueries` là toàn bộ quyền hiện tại của TỪNG user trong `users`, không phải phần thay đổi.**

- Với mỗi user trong `users`, SQLBot **thay thế toàn bộ** danh sách quyền của `userId` đó bằng đúng
  những gì có trong `formQueries` của chính user đó — không liên quan tới `formQueries` của user
  khác trong cùng batch.
```

- [ ] **Step 5: Sửa §7 (bảng lỗi) — thêm 2 mã lỗi mới**

Thay dòng:

```markdown
| 400 | `FAILED` | `DUPLICATE_FORM_UUID` | `formUuid` bị lặp trong cùng request |
```

thành:

```markdown
| 400 | `FAILED` | `DUPLICATE_FORM_UUID` | `formUuid` bị lặp trong `formQueries` của cùng một user |
| 400 | `FAILED` | `DUPLICATE_USER_ID` | `userId` bị lặp giữa các phần tử trong `users` |
| 400 | `FAILED` | `EMPTY_USER_LIST` | `data.users` rỗng hoặc thiếu |
```

Thêm ghi chú ngay dưới bảng (trước dòng `Ngoài bảng trên...`):

```markdown
Lỗi cấu trúc ở BẤT KỲ user nào trong `users` (kể cả `INVALID_PAYLOAD`, `DUPLICATE_FORM_UUID`,
`DUPLICATE_USER_ID`, `EMPTY_USER_LIST`) làm **cả request FAILED** — không user nào trong batch được
áp dụng. Ngược lại, `STALE` được tính RIÊNG theo từng user: xem `results[].status` trong response,
không phải `status` cấp request (§1).
```

- [ ] **Step 6: Sửa §8 (ví dụ curl) — bọc `data` theo batch**

Thay khối `-d '{...}'` trong ví dụ curl:

```bash
  -d '{
    "actionType": 1,
    "version": "1.0",
    "timestamp": "2026-08-10T10:00:00Z",
    "data": {
      "userId": "usr-12345-67890",
      "fullName": "Nguyễn Văn A",
      "isAdmin": false,
      "formQueries": [
        {
          "formUuid": "form-abcd-1234",
          "tableInfo": {
            "databaseTableName": "kdl_nhan_khau_row_values",
            "fields": [{"id": "province_id", "name": "Mã Tỉnh/Thành"}]
          },
          "postgresQuery": "SELECT * FROM kdl_nhan_khau_row_values WHERE province_id = '\''01'\''"
        }
      ]
    }
  }'
```

thành:

```bash
  -d '{
    "actionType": 1,
    "version": "1.0",
    "timestamp": "2026-08-10T10:00:00Z",
    "data": {
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
                "fields": [{"id": "province_id", "name": "Mã Tỉnh/Thành"}]
              },
              "postgresQuery": "SELECT * FROM kdl_nhan_khau_row_values WHERE province_id = '\''01'\''"
            }
          ]
        }
      ]
    }
  }'
```

Thay khối "Response mong đợi" ngay dưới:

```json
{
  "requestId": "...",
  "idempotencyKey": "...",
  "actionType": 1,
  "status": "SUCCESS",
  "logId": "...",
  "userId": "usr-12345-67890",
  "applied": {"upserted": 1, "deleted": 0},
  "errorCode": null,
  "errorMessage": null
}
```

thành:

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

- [ ] **Step 7: Commit**

```bash
git add backend/scripts/ai_sync_hook/AI_SYNC_HOOK_API_SPEC.md
git commit -m "docs: cập nhật API spec AI Sync Hook cho batch nhiều user"
```

---

## Self-Review

**Spec coverage:** mọi mục §2–§7 của [spec](../specs/2026-08-11-ai-sync-hook-batch-users-design.md) có task tương ứng — payload/response (Task 1, 4), all-or-nothing + STALE riêng-user (Task 3), transaction 1-lần-commit (Task 2, 3), mã lỗi mới (Task 3), audit log `user_id` NULL khi >1 user (Task 4), tài liệu (Task 6). Không có mục nào trong spec thiếu task.

**Placeholder scan:** không còn "TBD"/"tương tự Task N" — mọi step có code đầy đủ.

**Type consistency:** `HandlerResult(status: SyncStatus, results: list[UserSyncResult])` định nghĩa ở Task 3, dùng đúng tên field này ở Task 4 (`result.status`, `result.results`) và trong test Task 4/5. `UserSyncResult(userId, status, applied)` định nghĩa ở Task 1, dùng nhất quán ở Task 3 (dựng), Task 4 (đọc để tính tổng), Task 5 (đọc qua JSON response `results[]`).
