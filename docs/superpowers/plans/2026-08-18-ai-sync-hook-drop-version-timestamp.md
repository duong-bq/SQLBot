# AI Sync Hook — bỏ version/timestamp, bọc payload Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bỏ hẳn `version`/`timestamp` khỏi bản tin AI Sync Hook cho MỌI actionType, chỉ còn
`{actionType, payload}`; bỏ hẳn cơ chế chống bản tin lùi (STALE) vốn dựa vào `timestamp`.

**Architecture:** Đây là mở rộng của thay đổi đã áp riêng cho `actionType=4` ở commit `92aaaff11`
(payload thay `data`, version/timestamp optional) — áp dụng cùng khuôn cho `actionType=1`
(AUTHORIZATION_SYNC), rồi dọn sạch mọi mã STALE không còn dùng tới. `SyncEnvelope` mất 2 field, đổi
tên field `data` → `payload` (không còn nhận key `data`). Cột `sync_version` trên
`ai_sync_hook_logs`/`ai_user_permissions` giữ nguyên (không migration DB) nhưng đổi nguồn giá trị từ
"SW khai báo qua `timestamp`" sang "server tự sinh lúc xử lý" — thuần thông tin, không còn dùng để
so sánh chống lùi.

**Tech Stack:** Python, FastAPI, Pydantic v2, SQLModel, pytest.

**Spec:** [docs/superpowers/specs/2026-08-18-ai-sync-hook-drop-version-timestamp-design.md](../specs/2026-08-18-ai-sync-hook-drop-version-timestamp-design.md)

## Global Constraints

- Không tạo migration DB mới — cột `sync_version`/`schema_version` trên `ai_sync_hook_logs` và
  `ai_user_permissions` giữ nguyên kiểu, chỉ đổi nguồn/ ý nghĩa giá trị ghi vào.
- Envelope chỉ còn nhận đúng 2 key ở JSON gốc: `actionType`, `payload`. Không còn `version`,
  `timestamp`, không còn nhận key `data` (kể cả như alias phụ).
- Mọi hàm mới/sửa phải có docstring tiếng Việt (quy ước CLAUDE.md của repo).
- Sau khi cả 8 task xong, chạy lại toàn bộ `pytest tests/test_ai_sync_*.py` phải xanh — các task
  giữa chừng có thể để lại test Ở FILE KHÁC tạm đỏ vì đổi shape envelope ảnh hưởng xuyên suốt nhiều
  file cùng lúc (xem ghi chú cuối mỗi task).

---

## Task 1: Schema — bỏ version/timestamp, đổi `data` → `payload`

**Files:**
- Modify: `backend/apps/hooks/schemas/ai_sync_schema.py:1-38` (class `SyncEnvelope` + import)
- Test: `tests/test_ai_sync_schema.py:1-77`

**Interfaces:**
- Produces: `SyncEnvelope.payload: dict[str, Any]` (thay `SyncEnvelope.data`), `SyncEnvelope` không
  còn thuộc tính `version`/`timestamp`. Mọi task sau dùng `envelope.payload` thay vì `envelope.data`.

- [ ] **Step 1: Sửa test trước — thay toàn bộ test liên quan `SyncEnvelope`**

Trong `tests/test_ai_sync_schema.py`, thay khối từ `def test_envelope_hop_le()` (dòng 45) đến hết
`def test_envelope_nhan_ca_payload_lan_data()` (dòng 76) bằng:

```python
def test_envelope_hop_le():
    env = SyncEnvelope.model_validate({"actionType": 1, "payload": VALID_DATA})
    assert env.action_type == 1
    assert env.payload["userId"] == "usr-12345-67890"
    assert not hasattr(env, "version")
    assert not hasattr(env, "timestamp")
    assert not hasattr(env, "data")


@pytest.mark.parametrize("missing", ["actionType", "payload"])
def test_envelope_thieu_truong_bat_buoc(missing):
    body = {"actionType": 1, "payload": VALID_DATA}
    body.pop(missing)
    with pytest.raises(ValidationError):
        SyncEnvelope.model_validate(body)


def test_envelope_khong_con_nhan_key_data():
    """Key `data` (tên gốc trước khi đổi sang `payload`) không còn được chấp nhận."""
    with pytest.raises(ValidationError):
        SyncEnvelope.model_validate({"actionType": 4, "data": {"datasourceIds": ["50"]}})


def test_envelope_bo_qua_version_va_timestamp_neu_sw_van_gui():
    """SW gửi thừa `version`/`timestamp` (chưa kịp bỏ ở phía họ) thì hook bỏ qua âm thầm nhờ
    `extra="ignore"`, không lỗi, không có tác dụng."""
    env = SyncEnvelope.model_validate(
        {"actionType": 1, "version": "1.0", "timestamp": "2026-08-10T10:00:00Z", "payload": VALID_DATA}
    )
    assert not hasattr(env, "version")
    assert not hasattr(env, "timestamp")
```

Đồng thời sửa import ở đầu file: xoá `timedelta, timezone` khỏi
`from datetime import datetime, timedelta, timezone` nếu không còn dùng ở chỗ khác trong file — file
này vẫn dùng `timedelta`/`timezone` ở các test `to_sync_version` (dòng 173-185), nên **giữ nguyên**
dòng import đó, không sửa.

- [ ] **Step 2: Chạy test để thấy FAIL**

Run: `pytest tests/test_ai_sync_schema.py -v`
Expected: FAIL — `SyncEnvelope` hiện vẫn còn field `version`/`timestamp` bắt buộc kiểu khác, và vẫn
nhận key `data`, nên các assertion mới (`not hasattr`, `ValidationError` khi gửi `data`) không khớp
hành vi hiện tại.

- [ ] **Step 3: Sửa `SyncEnvelope`**

Trong `backend/apps/hooks/schemas/ai_sync_schema.py`, thay toàn bộ class `SyncEnvelope` (dòng 13-37)
bằng:

```python
class SyncEnvelope(BaseModel):
    """Phần vỏ chung của mọi bản tin, không phụ thuộc actionType.

    `action_type` ở đây chỉ khai kiểu int cho đủ hình; việc phân biệt "không phải integer" với
    "integer nhưng chưa hỗ trợ" do `resolve_action_type` làm TRƯỚC khi validate envelope, vì hai ca
    đó trả HTTP status khác nhau (400 vs 422) và cần ghi audit khác nhau.

    Bản tin chỉ còn đúng 2 trường: `actionType` và `payload`. Không còn `version`/`timestamp` (hợp
    đồng cũ dùng `timestamp` làm mốc chống bản tin lùi STALE — cơ chế đó đã bỏ hẳn, xem
    `handlers/authorization.py`) và không còn nhận key `data` (tên gốc trước khi đổi sang `payload`).

    `extra="ignore"`: SW gửi thừa trường nào (kể cả `version`/`timestamp` cũ) thì hook bỏ qua âm
    thầm, không vỡ.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    action_type: int = Field(alias="actionType")
    payload: dict[str, Any] = Field(alias="payload")
```

Và sửa import: bỏ `AliasChoices` khỏi dòng `from pydantic import AliasChoices, BaseModel, ConfigDict, Field`
(không còn dùng), thành:

```python
from pydantic import BaseModel, ConfigDict, Field
```

- [ ] **Step 4: Chạy lại test để thấy PASS**

Run: `pytest tests/test_ai_sync_schema.py -v`
Expected: PASS toàn bộ.

Ghi chú: `to_sync_version()` không đổi ở task này (vẫn nhận `datetime`, trả epoch millis) — các test
`test_to_sync_version_*` không bị ảnh hưởng và vẫn PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/apps/hooks/schemas/ai_sync_schema.py tests/test_ai_sync_schema.py
git commit -m "refactor: envelope AI Sync Hook chỉ còn actionType + payload"
```

---

## Task 2: Bỏ `SyncStatus.STALE` khỏi constants

**Files:**
- Modify: `backend/apps/hooks/constants.py:29-45` (class `SyncStatus`)

**Interfaces:**
- Consumes: không phụ thuộc task nào.
- Produces: `SyncStatus` không còn member `STALE`. Task 4 dựa vào việc member này đã biến mất để
  không còn cách nào sinh ra kết quả STALE.

Không có test file riêng khoá `SyncStatus.STALE` ngoài `tests/test_ai_sync_crud.py` (sẽ sửa ở Task
3) và `tests/test_ai_sync_handler.py`/`tests/test_ai_sync_api.py` (sẽ sửa ở Task 4/6) — task này chỉ
sửa nguồn, chưa cần chạy full suite (sẽ đỏ tạm thời ở các file chưa sửa, xem Global Constraints).

- [ ] **Step 1: Sửa `SyncStatus`**

Trong `backend/apps/hooks/constants.py`, thay class `SyncStatus` (dòng 29-45) bằng:

```python
class SyncStatus(str, Enum):
    """Trạng thái xử lý một bản tin, ghi vào `ai_sync_hook_logs.status`.

    `PROCESSING` được giữ để khớp tập trạng thái của DDL gốc nhưng luồng xử lý ĐỒNG BỘ hiện tại
    không bao giờ set nó — một request đi thẳng từ RECEIVED sang trạng thái cuối cùng. Nó chỉ có ý
    nghĩa nếu sau này chuyển sang xử lý nền.
    """

    RECEIVED = "RECEIVED"
    PROCESSING = "PROCESSING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    DUPLICATE = "DUPLICATE"
```

- [ ] **Step 2: Xác nhận file constants tự nó chưa vỡ**

Run: `python -c "from apps.hooks.constants import SyncStatus; print(list(SyncStatus))"`
Expected: In ra danh sách 5 member, không có `STALE`.

- [ ] **Step 3: Commit**

```bash
git add backend/apps/hooks/constants.py
git commit -m "refactor: bỏ SyncStatus.STALE — không còn cơ chế chống bản tin lùi"
```

---

## Task 3: crud/ai_sync_log.py — bỏ `get_last_applied_version`

**Files:**
- Modify: `backend/apps/hooks/crud/ai_sync_log.py:57-94` (`finish_log` docstring + xoá
  `get_last_applied_version`)
- Test: `tests/test_ai_sync_crud.py:1-97`

**Interfaces:**
- Consumes: không phụ thuộc Task 1/2 về mặt code (file này không import `SyncEnvelope` hay
  `SyncStatus.STALE`), nhưng logic-wise đi cùng nhóm dọn STALE.
- Produces: `crud/ai_sync_log.py` không còn export `get_last_applied_version`. Task 4 dựa vào việc
  hàm này đã biến mất để xác nhận handler hết chỗ gọi.

- [ ] **Step 1: Sửa test trước**

Trong `tests/test_ai_sync_crud.py`:

Sửa import ở đầu file (dòng 10-15), bỏ `get_last_applied_version`:

```python
from apps.hooks.crud.ai_sync_log import (
    create_received_log,
    finish_log,
    get_log_by_idempotency_key,
)
```

Sửa hằng `PAYLOAD` (dòng 19) — đổi key `data` → `payload`:

```python
PAYLOAD = {"actionType": 1, "payload": {"userId": "u1"}}
```

Xoá hẳn 3 test sau (dòng 79-97): `test_get_last_applied_version_tra_0_khi_chua_co_gi`,
`test_get_last_applied_version_chi_tinh_ban_success`, `test_get_last_applied_version_tach_theo_user`.

- [ ] **Step 2: Chạy test để thấy FAIL**

Run: `SQLBOT_TEST_DB_URL=<url> pytest tests/test_ai_sync_crud.py -v`
Expected: FAIL ở bước import (`ImportError: cannot import name 'get_last_applied_version'` — thực ra
hàm vẫn còn tồn tại lúc này, nên import vẫn OK; FAIL thật sự chỉ xảy ra sau Step 3. Nếu môi trường
không có `SQLBOT_TEST_DB_URL`, test tự skip — trong trường hợp đó xác nhận bước fail bằng cách đọc
lại diff, không chặn tiến độ task).

Ghi chú: vì bước xoá test đi trước bước xoá hàm nguồn, thứ tự RED thực tế ở đây là "trước khi sửa
nguồn thì test vẫn xanh với hàm cũ" — chuyển thẳng sang Step 3 để tạo đúng vòng RED→GREEN: xoá hàm
nguồn trước, chạy lại thấy các test vừa xoá không còn tồn tại nên không FAIL, các test còn lại vẫn
PASS. Coi Step 2 là bước xác nhận diff test đã đúng, không phải RED bắt buộc.

- [ ] **Step 3: Xoá `get_last_applied_version` và sửa docstring `finish_log`**

Trong `backend/apps/hooks/crud/ai_sync_log.py`, sửa docstring `finish_log` (dòng 66-69) thành:

```python
    """Chốt kết quả xử lý vào dòng audit và commit.

    `sync_version` chỉ được ghi khi biết (envelope đã parse được) — thuần thông tin để tra cứu lần
    đồng bộ gần nhất, không còn dùng để chống bản tin lùi.
    """
```

Xoá hẳn hàm `get_last_applied_version` (dòng 83-94, kể cả blank line thừa để lại).

- [ ] **Step 4: Chạy lại test để thấy PASS**

Run: `SQLBOT_TEST_DB_URL=<url> pytest tests/test_ai_sync_crud.py -v`
Expected: PASS toàn bộ (hoặc SKIP nếu không có `SQLBOT_TEST_DB_URL` — chấp nhận được, DB thật
không bắt buộc phải sẵn có trong môi trường chạy plan này).

- [ ] **Step 5: Commit**

```bash
git add backend/apps/hooks/crud/ai_sync_log.py tests/test_ai_sync_crud.py
git commit -m "refactor: bỏ get_last_applied_version — hết chỗ dùng sau khi bỏ STALE"
```

---

## Task 4: handlers/authorization.py — bỏ nhánh STALE, dùng `envelope.payload`

**Files:**
- Modify: `backend/apps/hooks/handlers/authorization.py` (toàn file)
- Test: `tests/test_ai_sync_handler.py` (toàn file)

**Interfaces:**
- Consumes: `SyncEnvelope.payload` (Task 1), `SyncStatus` không còn `STALE` (Task 2), không còn
  `get_last_applied_version` (Task 3).
- Produces: `handle_authorization_sync(session, envelope, sync_version) -> HandlerResult` — chữ ký
  không đổi, nhưng mọi user trong batch giờ LUÔN được áp dụng (không còn kết quả `"STALE"`).

- [ ] **Step 1: Sửa test trước**

Thay toàn bộ nội dung `tests/test_ai_sync_handler.py` bằng:

```python
"""Test handler actionType=1: validate batch, áp snapshot cho từng user."""

import pytest

from apps.hooks.constants import SyncActionType, SyncErrorCode, SyncStatus
from apps.hooks.crud.ai_user_permission import get_user_permissions
from apps.hooks.errors import SyncHookError
from apps.hooks.handlers import ACTION_HANDLERS
from apps.hooks.handlers.authorization import handle_authorization_sync
from apps.hooks.schemas.ai_sync_schema import SyncEnvelope


def _envelope(data: dict) -> SyncEnvelope:
    return SyncEnvelope.model_validate({"actionType": 1, "payload": data})


def _user(user_id="u1", forms=(("f1", "t1"),)):
    return {
        "userId": user_id,
        "fullName": "Nguyễn Văn A",
        "isAdmin": False,
        "formQueries": [
            {
                "formUuid": form_uuid,
                "tableInfo": {
                    "databaseTableName": table,
                    "queries": [{"datasourceId": f"ds-{form_uuid}", "datasourceType": "postgresql", "query": f"SELECT * FROM {table}"}],
                },
            }
            for form_uuid, table in forms
        ],
    }


def _batch(*users) -> dict:
    return {"users": list(users) if users else [_user()]}


def test_dispatch_chi_co_authorization_sync(db_session):
    assert ACTION_HANDLERS[SyncActionType.AUTHORIZATION_SYNC] is handle_authorization_sync
    assert set(ACTION_HANDLERS) == {SyncActionType.AUTHORIZATION_SYNC, SyncActionType.DATASOURCE_SYNC}


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


def test_trung_datasource_id_trong_1_form_raise_duplicate_datasource_id(db_session):
    data = {
        "userId": "u1", "isAdmin": False,
        "formQueries": [{
            "formUuid": "f1",
            "tableInfo": {
                "databaseTableName": "t1",
                "queries": [
                    {"datasourceId": "d1", "datasourceType": "postgresql", "query": "SELECT 1"},
                    {"datasourceId": "d1", "datasourceType": "clickhouse", "query": "SELECT 2"},
                ],
            },
        }],
    }
    with pytest.raises(SyncHookError) as exc:
        handle_authorization_sync(db_session, _envelope(_batch(data)), 1000)
    assert exc.value.http_status == 400
    assert exc.value.error_code is SyncErrorCode.DUPLICATE_DATASOURCE_ID
    assert "d1" in exc.value.message
    assert get_user_permissions(db_session, "u1") == []


def test_1_user_loi_cau_truc_thi_ca_batch_khong_ai_duoc_ap_dung(db_session):
    """all-or-nothing: user thứ 2 trùng formUuid thì user thứ 1 (hợp lệ) cũng không được áp dụng."""
    valid_user = _user("u1", (("f1", "t1"),))
    invalid_user = _user("u2", (("f2", "t2"), ("f2", "t3")))
    with pytest.raises(SyncHookError):
        handle_authorization_sync(db_session, _envelope(_batch(valid_user, invalid_user)), 1000)
    assert get_user_permissions(db_session, "u1") == []
    assert get_user_permissions(db_session, "u2") == []


def test_ban_tin_cu_hon_van_duoc_ap_dung(db_session):
    """Không còn STALE: 1 user được áp lại với sync_version NHỎ HƠN lần trước vẫn thành công —
    thứ tự thời gian không còn ý nghĩa, chỉ còn snapshot mới nhất thắng."""
    handle_authorization_sync(db_session, _envelope(_batch(_user())), 2000)
    result = handle_authorization_sync(db_session, _envelope(_batch(_user())), 1000)
    assert result.results[0].status == "SUCCESS"
    assert len(get_user_permissions(db_session, "u1")) == 1


def test_thu_hoi_het_quyen_bang_form_queries_rong(db_session):
    handle_authorization_sync(db_session, _envelope(_batch(_user())), 1000)
    data = _user()
    data["formQueries"] = []
    result = handle_authorization_sync(db_session, _envelope(_batch(data)), 2000)
    assert result.results[0].status == "SUCCESS"
    assert (result.results[0].applied.upserted, result.results[0].applied.deleted) == (0, 1)
    assert get_user_permissions(db_session, "u1") == []
```

Ghi chú thay đổi so với bản gốc: bỏ `_mark_applied` (không còn dùng), bỏ 4 test STALE
(`test_ban_tin_lui_bi_bo_qua`, `test_ban_tin_cung_version_bi_bo_qua`,
`test_ban_tin_moi_hon_duoc_ap_dung`, `test_stale_tinh_rieng_tung_user_trong_cung_batch`), thêm
`test_ban_tin_cu_hon_van_duoc_ap_dung` chứng minh KHÔNG còn chặn theo thứ tự, sửa
`test_dispatch_chi_co_authorization_sync` để phản ánh đúng `ACTION_HANDLERS` hiện có 2 actionType
(bản gốc của test này thiếu `DATASOURCE_SYNC` — lỗi có sẵn từ trước, tiện sửa luôn ở đây).

- [ ] **Step 2: Chạy test để thấy FAIL**

Run: `SQLBOT_TEST_DB_URL=<url> pytest tests/test_ai_sync_handler.py -v`
Expected: FAIL — `_envelope()` gọi `SyncEnvelope.model_validate({"actionType": 1, "payload": data})`
nhưng handler vẫn đọc `envelope.data` (không tồn tại nữa sau Task 1) → `AttributeError`; và nhánh
STALE trong nguồn vẫn gọi `get_last_applied_version` (đã xoá ở Task 3) → `ImportError` khi nạp
module.

- [ ] **Step 3: Sửa `handlers/authorization.py`**

Thay toàn bộ nội dung file `backend/apps/hooks/handlers/authorization.py` bằng:

```python
"""Handler cho `actionType = 1` (AUTHORIZATION_SYNC).

Handler nhận BATCH nhiều user (`payload.users`). Validate cấu trúc TOÀN BỘ batch trước khi áp dụng
bất kỳ ai (all-or-nothing với lỗi cấu trúc: 1 user sai thì cả batch FAILED). Sau khi qua được bước
đó, mỗi user trong batch LUÔN được áp full snapshot — không còn kiểm chống bản tin lùi (STALE), bản
tin nào tới sau cũng ghi đè bản trước bất kể thứ tự thời gian thật. Không ghi audit log và không
biết gì về HTTP — route lo hai việc đó.
"""

from dataclasses import dataclass, field

from pydantic import ValidationError
from sqlmodel import Session

from apps.hooks.constants import SyncErrorCode, SyncStatus
from apps.hooks.crud.ai_user_permission import replace_user_permissions
from apps.hooks.errors import SyncHookError
from apps.hooks.schemas.ai_sync_schema import (
    AuthorizationSyncBatch,
    SyncAppliedCounts,
    SyncEnvelope,
    SyncResultItem,
    UserSyncResult,
    find_duplicate_datasource_id,
    find_duplicate_form_uuid,
    find_duplicate_user_id,
)


@dataclass
class HandlerResult:
    """Kết quả một lần xử lý bản tin batch, để route dựng response và chốt audit log.

    `status` luôn là `SUCCESS` khi hàm trả về bình thường (không raise) — batch đã qua validate cấu
    trúc và được xử lý xong. Chi tiết từng phần tử (user hoặc datasource, tùy actionType) nằm trong
    `results`.
    """

    status: SyncStatus
    results: list[SyncResultItem] = field(default_factory=list)


def handle_authorization_sync(
    session: Session, envelope: SyncEnvelope, sync_version: int
) -> HandlerResult:
    """Parse payload batch rồi áp FULL SNAPSHOT cho từng user.

    Validate cấu trúc CẢ BATCH trước (schema, `userId` trùng, `formUuid` trùng trong từng user) —
    bất kỳ lỗi nào ở bước này thì raise ngay, KHÔNG áp dụng cho ai. Qua được bước đó thì mọi user đều
    được áp dụng — không còn nhánh STALE. Toàn bộ vòng lặp áp dụng nằm trong một transaction, commit
    đúng 1 lần ở cuối — lỗi bất ngờ giữa batch phải để caller rollback sạch, không để nửa batch
    commit dở dang.
    """
    try:
        batch = AuthorizationSyncBatch.model_validate(envelope.payload)
    except ValidationError as e:
        raise SyncHookError(400, SyncErrorCode.INVALID_PAYLOAD, str(e)) from e

    if not batch.users:
        raise SyncHookError(400, SyncErrorCode.EMPTY_USER_LIST, "payload.users không được rỗng")

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
        for form in user_data.form_queries:
            duplicated_datasource = find_duplicate_datasource_id(form.table_info.queries)
            if duplicated_datasource:
                raise SyncHookError(
                    400,
                    SyncErrorCode.DUPLICATE_DATASOURCE_ID,
                    f"datasourceId bị lặp trong payload của user {user_data.user_id}, "
                    f"form {form.form_uuid}: {duplicated_datasource}",
                )

    results: list[UserSyncResult] = []
    for user_data in batch.users:
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

- [ ] **Step 4: Chạy lại test để thấy PASS**

Run: `SQLBOT_TEST_DB_URL=<url> pytest tests/test_ai_sync_handler.py -v`
Expected: PASS toàn bộ (hoặc SKIP nếu không có `SQLBOT_TEST_DB_URL`).

- [ ] **Step 5: Commit**

```bash
git add backend/apps/hooks/handlers/authorization.py tests/test_ai_sync_handler.py
git commit -m "refactor: authorization handler bỏ nhánh STALE, dùng envelope.payload"
```

---

## Task 5: handlers/datasource_sync.py — dùng `envelope.payload`

**Files:**
- Modify: `backend/apps/hooks/handlers/datasource_sync.py:1-67`

**Interfaces:**
- Consumes: `SyncEnvelope.payload` (Task 1).
- Produces: không đổi chữ ký `handle_datasource_sync`.

Không có test file riêng ở mức handler cho DATASOURCE_SYNC (chỉ được test gián tiếp qua
`tests/test_ai_sync_api.py`, sẽ sửa ở Task 6) — task này chỉ cần sửa nguồn rồi xác nhận import OK.

- [ ] **Step 1: Sửa nguồn**

Trong `backend/apps/hooks/handlers/datasource_sync.py`:

Dòng 45 (trong docstring `handle_datasource_sync`), đổi `` `data.datasourceIds` `` →
`` `payload.datasourceIds` ``.

Dòng 60, đổi:

```python
        payload = DatasourceSyncData.model_validate(envelope.data)
```

thành:

```python
        payload = DatasourceSyncData.model_validate(envelope.payload)
```

Dòng 66, đổi thông báo lỗi `"data.datasourceIds không được rỗng"` →
`"payload.datasourceIds không được rỗng"`.

- [ ] **Step 2: Xác nhận module nạp được**

Run: `python -c "from apps.hooks.handlers.datasource_sync import handle_datasource_sync; print('ok')"`
Expected: In `ok`, không lỗi import.

- [ ] **Step 3: Commit**

```bash
git add backend/apps/hooks/handlers/datasource_sync.py
git commit -m "refactor: datasource_sync handler dùng envelope.payload"
```

---

## Task 6: api/ai_sync.py — payload extraction, bỏ yêu cầu version/timestamp, sync_version server-side

**Files:**
- Modify: `backend/apps/hooks/api/ai_sync.py` (toàn file)
- Test: `tests/test_ai_sync_api.py` (toàn file)

**Interfaces:**
- Consumes: `SyncEnvelope.payload` (Task 1), `to_sync_version(datetime) -> int` (không đổi chữ ký,
  chỉ đổi input truyền vào từ `envelope.timestamp` sang `datetime.now(timezone.utc)`).
- Produces: route `POST /api/v1/hooks/ai-sync` chấp nhận body `{actionType, payload}`, luôn tính
  `sync_version` (không còn `None`), không còn trả lỗi 400 vì thiếu `version`/`timestamp`.

- [ ] **Step 1: Sửa test trước**

Thay toàn bộ nội dung `tests/test_ai_sync_api.py` bằng:

```python
"""Test tầng giao thức của route hook: xác thực JWT + quyền ws_admin, idempotency, bảng lỗi.

Patch tầng crud và handler nên KHÔNG cần DB — test này chỉ kiểm hợp đồng HTTP. Nghiệp vụ đã có
test riêng chạy trên Postgres thật.

Hook dùng chung cơ chế xác thực của SQLBot (`require_permissions` đọc `request.state.current_user`
qua contextvar của `RequestContextMiddleware`), không có static token riêng. App test dựng một
middleware giả lập để gán `current_user` — mô phỏng đúng việc `TokenMiddleware` thật làm sau khi
giải mã JWT.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware

from apps.hooks.constants import SyncActionType, SyncStatus
from apps.hooks.handlers.authorization import HandlerResult
from apps.hooks.schemas.ai_sync_schema import SyncAppliedCounts, UserSyncResult

VALID_BODY = {
    "actionType": 1,
    "payload": {"users": [{"userId": "usr-1", "fullName": "A", "isAdmin": False, "formQueries": []}]},
}
HEADERS = {"X-Request-ID": "req-1", "X-Idempotency-Key": "idem-1"}


def _user(*, is_admin: bool = False, weight: int = 1, oid: int = 1):
    """Đối tượng tối giản đủ 3 thuộc tính `require_permissions` cần đọc."""
    return SimpleNamespace(isAdmin=is_admin, weight=weight, oid=oid)


class _FakeAuthMiddleware(BaseHTTPMiddleware):
    """Gán `request.state.current_user`, mô phỏng việc TokenMiddleware thật làm sau khi giải mã JWT.

    `user=None` mô phỏng request chưa xác thực (không có `X-SQLBOT-TOKEN` hợp lệ).
    """

    def __init__(self, app, user):
        super().__init__(app)
        self.user = user

    async def dispatch(self, request, call_next):
        if self.user is not None:
            request.state.current_user = self.user
        return await call_next(request)


def _build_app(user) -> FastAPI:
    """App tối giản: router hook + đúng 2 middleware mà `require_permissions` cần, + exception
    handler giống `main.py` để lỗi thiếu quyền (Exception thường) trả 500 như trên app thật."""
    from apps.hooks.api import ai_sync
    from apps.system.schemas.permission import RequestContextMiddleware
    from common.core.db import get_session
    from common.core.response_middleware import exception_handler

    app = FastAPI()
    app.include_router(ai_sync.router, prefix="/api/v1")
    app.dependency_overrides[get_session] = lambda: MagicMock()
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(_FakeAuthMiddleware, user=user)
    app.add_exception_handler(StarletteHTTPException, exception_handler.http_exception_handler)
    app.add_exception_handler(Exception, exception_handler.global_exception_handler)
    return app


@pytest.fixture()
def client():
    """Client với user đã đăng nhập và có quyền ws_admin — ca mặc định cho các test nghiệp vụ."""
    return TestClient(_build_app(_user(weight=1)), raise_server_exceptions=False)


def _fake_log(status=SyncStatus.RECEIVED.value, error_code=None, error_message=None):
    log = MagicMock()
    log.id = uuid.UUID("11111111-1111-1111-1111-111111111111")
    log.action_type = 1
    log.status = status
    log.error_code = error_code
    log.error_message = error_message
    log.user_id = "usr-1"
    return log


@pytest.fixture()
def crud_patches():
    """Patch 3 hàm crud log mà route dùng; mặc định: chưa có log trùng, tạo log OK."""
    with (
        patch("apps.hooks.api.ai_sync.get_log_by_idempotency_key", return_value=None) as get_log,
        patch("apps.hooks.api.ai_sync.create_received_log", return_value=_fake_log()) as create_log,
        patch("apps.hooks.api.ai_sync.finish_log", side_effect=lambda s, log, **kw: log) as finish,
    ):
        yield {"get_log": get_log, "create_log": create_log, "finish_log": finish}


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


def test_thanh_cong_tra_200_va_dem_dung(client, crud_patches):
    ctx, handler = _patch_handler()
    with ctx:
        resp = client.post("/api/v1/hooks/ai-sync", json=VALID_BODY, headers=HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert body["actionType"] == 1
    assert body["results"] == [
        {"userId": "usr-1", "status": "SUCCESS", "applied": {"upserted": 2, "deleted": 1}}
    ]
    assert body["requestId"] == "req-1"
    assert body["idempotencyKey"] == "idem-1"
    assert body["applied"] == {"upserted": 2, "deleted": 1}
    assert body["errorCode"] is None
    # sync_version giờ sinh từ thời điểm server xử lý — chỉ còn kiểm kiểu, không kiểm giá trị cố định
    assert isinstance(handler.call_args[0][2], int)
    assert handler.call_args[0][2] > 0
    # response KHÔNG bị bọc envelope {code,data,msg}
    assert "code" not in body and "data" not in body


def test_chua_dang_nhap_tra_401_va_khong_ghi_log(crud_patches):
    """`request.state.current_user` không tồn tại — đúng ca TokenMiddleware chưa xác thực được."""
    client = TestClient(_build_app(user=None), raise_server_exceptions=False)
    resp = client.post("/api/v1/hooks/ai-sync", json=VALID_BODY, headers=HEADERS)
    assert resp.status_code == 401
    crud_patches["create_log"].assert_not_called()


def test_user_thuong_khong_du_quyen_tra_loi(crud_patches):
    """`weight=0` và không phải admin: `require_permissions` raise Exception thường → 500.

    Đây là quy ước chung của SQLBot (không có 403 riêng cho thiếu quyền), không phải hành vi
    riêng của hook — xem `BACKEND_ARCHITECTURE.md` §7 và "bẫy" ở `OPERATIONS.md`.
    """
    client = TestClient(_build_app(_user(weight=0, is_admin=False)), raise_server_exceptions=False)
    resp = client.post("/api/v1/hooks/ai-sync", json=VALID_BODY, headers=HEADERS)
    assert resp.status_code == 500
    crud_patches["create_log"].assert_not_called()


def test_admin_toan_cuc_van_goi_duoc_du_weight_0(client, crud_patches):
    """`isAdmin=True` bỏ qua kiểm tra role — khớp hành vi `require_permissions` dùng chung toàn app."""
    ctx, handler = _patch_handler()
    admin_client = TestClient(_build_app(_user(weight=0, is_admin=True)), raise_server_exceptions=False)
    with ctx:
        resp = admin_client.post("/api/v1/hooks/ai-sync", json=VALID_BODY, headers=HEADERS)
    assert resp.status_code == 200
    handler.assert_called_once()


def test_thieu_idempotency_key_tra_400(client, crud_patches):
    resp = client.post("/api/v1/hooks/ai-sync", json=VALID_BODY, headers={"X-Request-ID": "req-1"})
    assert resp.status_code == 400
    assert resp.json()["errorCode"] == "MISSING_IDEMPOTENCY_KEY"
    crud_patches["create_log"].assert_not_called()


def test_body_khong_phai_json_tra_400(client, crud_patches):
    resp = client.post(
        "/api/v1/hooks/ai-sync", content=b"khong-phai-json",
        headers=dict(HEADERS, **{"Content-Type": "application/json"}),
    )
    assert resp.status_code == 400
    assert resp.json()["errorCode"] == "MALFORMED_JSON"
    crud_patches["create_log"].assert_not_called()


def test_body_json_nhung_khong_phai_object_tra_400(client, crud_patches):
    resp = client.post("/api/v1/hooks/ai-sync", json=[1, 2, 3], headers=HEADERS)
    assert resp.status_code == 400
    assert resp.json()["errorCode"] == "MALFORMED_JSON"


@pytest.mark.parametrize("action_type", [2, 3, 5, 6])
def test_action_type_chua_implement_tra_422_va_co_ghi_log(client, crud_patches, action_type):
    body = dict(VALID_BODY, actionType=action_type)
    resp = client.post("/api/v1/hooks/ai-sync", json=body, headers=HEADERS)
    assert resp.status_code == 422
    assert resp.json()["errorCode"] == "UNSUPPORTED_ACTION_TYPE"
    assert resp.json()["actionType"] == action_type
    crud_patches["create_log"].assert_called_once()
    assert crud_patches["finish_log"].call_args.kwargs["status"] == SyncStatus.FAILED.value


@pytest.mark.parametrize("action_type", ["1", 99, None, 0, True])
def test_action_type_khong_hop_le_tra_422(client, crud_patches, action_type):
    body = dict(VALID_BODY, actionType=action_type)
    resp = client.post("/api/v1/hooks/ai-sync", json=body, headers=HEADERS)
    assert resp.status_code == 422
    assert resp.json()["errorCode"] == "UNKNOWN_ACTION_TYPE"
    crud_patches["create_log"].assert_called_once()


def test_envelope_thieu_payload_tra_400(client, crud_patches):
    body = {"actionType": 1}
    resp = client.post("/api/v1/hooks/ai-sync", json=body, headers=HEADERS)
    assert resp.status_code == 400
    assert resp.json()["errorCode"] == "INVALID_ENVELOPE"
    crud_patches["create_log"].assert_called_once()


def test_envelope_gui_key_data_cu_khong_con_hop_le(client, crud_patches):
    """Key `data` (tên gốc trước khi đổi sang `payload`) không còn được chấp nhận."""
    body = {"actionType": 1, "data": {"users": []}}
    resp = client.post("/api/v1/hooks/ai-sync", json=body, headers=HEADERS)
    assert resp.status_code == 400
    assert resp.json()["errorCode"] == "INVALID_ENVELOPE"


def test_trung_idempotency_key_tra_200_duplicate_va_khong_xu_ly_lai(client, crud_patches):
    crud_patches["get_log"].return_value = _fake_log(
        status=SyncStatus.SUCCESS.value, error_code=None, error_message=None
    )
    ctx, handler = _patch_handler()
    with ctx:
        resp = client.post("/api/v1/hooks/ai-sync", json=VALID_BODY, headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["status"] == "DUPLICATE"
    assert resp.json()["logId"] == "11111111-1111-1111-1111-111111111111"
    handler.assert_not_called()
    crud_patches["create_log"].assert_not_called()


def test_race_idempotency_integrity_error_tra_200_duplicate(client, crud_patches):
    crud_patches["create_log"].side_effect = IntegrityError("insert", {}, Exception("dup"))
    crud_patches["get_log"].side_effect = [None, _fake_log(status=SyncStatus.SUCCESS.value)]
    ctx, handler = _patch_handler()
    with ctx:
        resp = client.post("/api/v1/hooks/ai-sync", json=VALID_BODY, headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["status"] == "DUPLICATE"
    handler.assert_not_called()


def test_handler_raise_sync_hook_error_duoc_dich_dung(client, crud_patches):
    from apps.hooks.constants import SyncErrorCode
    from apps.hooks.errors import SyncHookError

    ctx, _ = _patch_handler(
        error=SyncHookError(400, SyncErrorCode.INVALID_PAYLOAD, "userId rỗng")
    )
    with ctx:
        resp = client.post("/api/v1/hooks/ai-sync", json=VALID_BODY, headers=HEADERS)
    assert resp.status_code == 400
    assert resp.json()["errorCode"] == "INVALID_PAYLOAD"
    assert resp.json()["errorMessage"] == "userId rỗng"
    assert crud_patches["finish_log"].call_args.kwargs["status"] == SyncStatus.FAILED.value


def test_handler_loi_khong_luong_truoc_tra_500(client, crud_patches):
    ctx, _ = _patch_handler(error=RuntimeError("bug"))
    with ctx:
        resp = client.post("/api/v1/hooks/ai-sync", json=VALID_BODY, headers=HEADERS)
    assert resp.status_code == 500
    assert resp.json()["errorCode"] == "INTERNAL_ERROR"
    assert crud_patches["finish_log"].call_args.kwargs["status"] == SyncStatus.FAILED.value


def test_body_voi_nhieu_user_tra_ket_qua_tung_user(client, crud_patches):
    ctx, _ = _patch_handler(
        result=HandlerResult(
            status=SyncStatus.SUCCESS,
            results=[
                UserSyncResult(
                    userId="usr-1", status="SUCCESS",
                    applied=SyncAppliedCounts(upserted=1, deleted=0),
                ),
                UserSyncResult(
                    userId="usr-2", status="SUCCESS",
                    applied=SyncAppliedCounts(upserted=1, deleted=0),
                ),
            ],
        )
    )
    body = dict(
        VALID_BODY,
        payload={
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
    assert r["applied"] == {"upserted": 2, "deleted": 0}
    assert [x["userId"] for x in r["results"]] == ["usr-1", "usr-2"]
    assert [x["status"] for x in r["results"]] == ["SUCCESS", "SUCCESS"]


def test_userid_trung_trong_batch_tra_400(client, crud_patches):
    from apps.hooks.constants import SyncErrorCode
    from apps.hooks.errors import SyncHookError

    ctx, _ = _patch_handler(
        error=SyncHookError(400, SyncErrorCode.DUPLICATE_USER_ID, "userId bị lặp trong payload: usr-1")
    )
    body = dict(
        VALID_BODY,
        payload={
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
        error=SyncHookError(400, SyncErrorCode.EMPTY_USER_LIST, "payload.users không được rỗng")
    )
    body = dict(VALID_BODY, payload={"users": []})
    with ctx:
        resp = client.post("/api/v1/hooks/ai-sync", json=body, headers=HEADERS)
    assert resp.status_code == 400
    assert resp.json()["errorCode"] == "EMPTY_USER_LIST"


def test_path_hooks_khong_nam_trong_whitelist():
    """`/hooks/*` phải đi qua TokenMiddleware như mọi endpoint khác — không có ngoại lệ riêng."""
    from common.utils.whitelist import whiteUtils

    assert whiteUtils.is_whitelisted("/api/v1/hooks/ai-sync") is False


def test_action_type_4_nhan_payload_dung_hop_dong(client, crud_patches):
    """Hợp đồng của bên tích hợp DATASOURCE_SYNC: chỉ `actionType` + `payload`."""
    from apps.hooks.handlers import ACTION_HANDLERS
    from apps.hooks.schemas.ai_sync_schema import DatasourceSyncResult

    handler = MagicMock(
        return_value=HandlerResult(
            status=SyncStatus.SUCCESS,
            results=[DatasourceSyncResult(
                datasourceId="50", status="SUCCESS",
                applied=SyncAppliedCounts(upserted=102, deleted=1),
            )],
        )
    )
    body = {"actionType": 4, "payload": {"datasourceIds": ["50"]}}
    with patch.dict(ACTION_HANDLERS, {SyncActionType.DATASOURCE_SYNC: handler}):
        resp = client.post("/api/v1/hooks/ai-sync", json=body, headers=HEADERS)

    assert resp.status_code == 200
    assert resp.json()["status"] == "SUCCESS"
    assert resp.json()["applied"] == {"upserted": 102, "deleted": 1}
    assert handler.call_args.args[1].payload == {"datasourceIds": ["50"]}
    # sync_version luôn được ghi (server tự sinh), không còn None
    assert crud_patches["finish_log"].call_args.kwargs["sync_version"] is not None
```

- [ ] **Step 2: Chạy test để thấy FAIL**

Run: `pytest tests/test_ai_sync_api.py -v`
Expected: FAIL — route hiện vẫn đọc `raw.get("data")`, vẫn bắt buộc `version`/`timestamp` cho
actionType 1 (trả 400 kể cả khi body hợp lệ theo shape mới), và `sync_version` vẫn có thể là `None`.

- [ ] **Step 3: Sửa `api/ai_sync.py`**

Sửa import ở đầu file — thêm `datetime, timezone`:

```python
import asyncio
from datetime import datetime, timezone
from typing import Any
```

Sửa đoạn trích `raw_data`/`schema_version` (dòng gốc 192-194):

```python
    raw_data = raw.get("data")
    best_effort_user_id = _best_effort_single_user_id(raw_data)
    schema_version = raw.get("version") if isinstance(raw.get("version"), str) else None
```

thành:

```python
    raw_data = raw.get("payload")
    best_effort_user_id = _best_effort_single_user_id(raw_data)
```

Sửa lệnh gọi `create_received_log` (bỏ dòng `schema_version=schema_version,`, thay bằng
`schema_version=None,` — cột vẫn tồn tại, không còn nguồn để điền):

```python
    try:
        log = create_received_log(
            session,
            request_id=request_id,
            idempotency_key=idempotency_key,
            action_type=logged_action_type,
            schema_version=None,
            user_id=best_effort_user_id if isinstance(best_effort_user_id, str) else None,
            request_payload=raw,
        )
```

Xoá hẳn khối kiểm `version`/`timestamp` bắt buộc cho AUTHORIZATION_SYNC (dòng gốc 256-268) và đổi
cách tính `sync_version` (dòng gốc 270-272). Đoạn từ:

```python
    # `version`/`timestamp` optional ở model nhưng bắt buộc cho AUTHORIZATION_SYNC: thiếu
    ...
    sync_version = to_sync_version(envelope.timestamp) if envelope.timestamp is not None else None
    handler = ACTION_HANDLERS[action_type]
```

thay bằng:

```python
    # Không còn `timestamp` do SW gửi (đã bỏ khỏi envelope) — `sync_version` giờ là mốc server tự
    # sinh tại thời điểm xử lý, thuần thông tin để tra cứu lần đồng bộ gần nhất, không còn dùng để
    # chống bản tin lùi (cơ chế đó đã bỏ, xem handlers/authorization.py).
    sync_version = to_sync_version(datetime.now(timezone.utc))
    handler = ACTION_HANDLERS[action_type]
```

Sửa lệnh gọi handler (dòng gốc 280), bỏ `or 0` vì `sync_version` không còn là `None`:

```python
        result = await asyncio.to_thread(handler, session, envelope, sync_version)
```

- [ ] **Step 4: Chạy lại test để thấy PASS**

Run: `pytest tests/test_ai_sync_api.py -v`
Expected: PASS toàn bộ.

- [ ] **Step 5: Commit**

```bash
git add backend/apps/hooks/api/ai_sync.py tests/test_ai_sync_api.py
git commit -m "feat: hook AI Sync chỉ nhận actionType + payload, bỏ chống bản tin lùi"
```

---

## Task 7: tests/test_ai_sync_e2e.py — cập nhật kịch bản đầu-cuối

**Files:**
- Test: `tests/test_ai_sync_e2e.py` (toàn file)

**Interfaces:**
- Consumes: toàn bộ thay đổi Task 1-6 (đây là test tích hợp thật, không patch gì).

- [ ] **Step 1: Viết lại toàn bộ file**

Thay toàn bộ nội dung `tests/test_ai_sync_e2e.py` bằng:

```python
"""Test đầu-cuối: HTTP thật + Postgres thật, không patch gì.

Đây là test duy nhất chứng minh cả chuỗi hoạt động: route → audit log → bảng quyền. Các test khác
hoặc chỉ kiểm giao thức (patch crud), hoặc chỉ kiểm nghiệp vụ (gọi trực tiếp handler).
"""

import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from apps.hooks.crud.ai_sync_log import get_log_by_idempotency_key
from apps.hooks.crud.ai_user_permission import get_user_permissions


class _FakeAuthMiddleware(BaseHTTPMiddleware):
    """Gán `request.state.current_user`, mô phỏng việc TokenMiddleware thật làm sau khi giải mã JWT."""

    async def dispatch(self, request, call_next):
        request.state.current_user = SimpleNamespace(isAdmin=False, weight=1, oid=1)
        return await call_next(request)


def _body(user_id: str, form_uuids: list[str]) -> dict:
    return {
        "actionType": 1,
        "payload": {
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
                                "queries": [
                                    {"datasourceId": "ds-pg", "datasourceType": "postgresql", "query": "SELECT * FROM kdl_nhan_khau_row_values WHERE province_id = '01'"},
                                ],
                            },
                        }
                        for form_uuid in form_uuids
                    ],
                }
            ]
        },
    }


@pytest.fixture()
def e2e_client(db_session):
    """App thật (router hook) nối vào đúng session Postgres của fixture db_session.

    Có đủ `RequestContextMiddleware` + middleware giả lập user đã đăng nhập (`ws_admin`) để
    `require_permissions` trên route hoạt động đúng như trên app thật.
    """
    from apps.hooks.api import ai_sync
    from apps.system.schemas.permission import RequestContextMiddleware
    from common.core.db import get_session

    app = FastAPI()
    app.include_router(ai_sync.router, prefix="/api/v1")
    app.dependency_overrides[get_session] = lambda: db_session
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(_FakeAuthMiddleware)
    return TestClient(app)


def _headers(key: str) -> dict:
    return {"X-Request-ID": str(uuid.uuid4()), "X-Idempotency-Key": key}


def test_e2e_dong_bo_roi_thu_hoi_va_retry(e2e_client, db_session):
    # 1. Bản tin đầu: cấp 2 form
    resp = e2e_client.post(
        "/api/v1/hooks/ai-sync",
        json=_body("usr-e2e", ["form-1", "form-2"]),
        headers=_headers("idem-e2e-1"),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "SUCCESS"
    assert resp.json()["applied"] == {"upserted": 2, "deleted": 0}
    assert {r.form_uuid for r in get_user_permissions(db_session, "usr-e2e")} == {"form-1", "form-2"}
    log = get_log_by_idempotency_key(db_session, "idem-e2e-1")
    assert log.status == "SUCCESS"
    assert log.sync_version is not None
    assert log.processed_at is not None
    assert log.request_payload["payload"]["users"][0]["userId"] == "usr-e2e"

    # 2. Retry đúng bản tin đó: DUPLICATE, không xử lý lại
    again = e2e_client.post(
        "/api/v1/hooks/ai-sync",
        json=_body("usr-e2e", ["form-1", "form-2"]),
        headers=_headers("idem-e2e-1"),
    )
    assert again.json()["status"] == "DUPLICATE"

    # 3. Bản tin mới hơn chỉ còn form-2: form-1 bị thu hồi
    newer = e2e_client.post(
        "/api/v1/hooks/ai-sync",
        json=_body("usr-e2e", ["form-2"]),
        headers=_headers("idem-e2e-2"),
    )
    assert newer.json()["applied"] == {"upserted": 1, "deleted": 1}
    assert {r.form_uuid for r in get_user_permissions(db_session, "usr-e2e")} == {"form-2"}

    # 4. Thu hồi hết quyền
    revoke = _body("usr-e2e", [])
    revoked = e2e_client.post("/api/v1/hooks/ai-sync", json=revoke, headers=_headers("idem-e2e-3"))
    assert revoked.json()["status"] == "SUCCESS"
    assert get_user_permissions(db_session, "usr-e2e") == []


def test_e2e_action_type_chua_ho_tro_van_de_lai_vet(e2e_client, db_session):
    body = _body("usr-e2e-2", [])
    body["actionType"] = 3
    resp = e2e_client.post("/api/v1/hooks/ai-sync", json=body, headers=_headers("idem-e2e-unsup"))
    assert resp.status_code == 422
    assert resp.json()["errorCode"] == "UNSUPPORTED_ACTION_TYPE"
    log = get_log_by_idempotency_key(db_session, "idem-e2e-unsup")
    assert log.status == "FAILED"
    assert log.action_type == 3
    assert log.error_code == "UNSUPPORTED_ACTION_TYPE"


def test_e2e_payload_sai_van_de_lai_vet(e2e_client, db_session):
    body = {
        "actionType": 1,
        "payload": {"users": [{"userId": "u-bad"}]},
    }
    resp = e2e_client.post("/api/v1/hooks/ai-sync", json=body, headers=_headers("idem-e2e-badpayload"))
    assert resp.status_code == 400
    assert resp.json()["errorCode"] == "INVALID_PAYLOAD"
    log = get_log_by_idempotency_key(db_session, "idem-e2e-badpayload")
    assert log.status == "FAILED"
    assert log.user_id == "u-bad"
    assert get_user_permissions(db_session, "u-bad") == []


def test_e2e_batch_nhieu_user_deu_thanh_cong(e2e_client, db_session):
    batch_body = {
        "actionType": 1,
        "payload": {
            "users": [
                {
                    "userId": "usr-fresh",
                    "isAdmin": False,
                    "formQueries": [
                        {
                            "formUuid": "form-y",
                            "tableInfo": {
                                "databaseTableName": "t",
                                "queries": [{"datasourceId": "d1", "datasourceType": "postgresql", "query": "SELECT 1"}],
                            },
                        }
                    ],
                },
                {"userId": "usr-two", "isAdmin": False, "formQueries": []},
            ]
        },
    }
    resp = e2e_client.post("/api/v1/hooks/ai-sync", json=batch_body, headers=_headers("idem-batch-1"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "SUCCESS"
    results_by_user = {r["userId"]: r["status"] for r in body["results"]}
    assert results_by_user == {"usr-fresh": "SUCCESS", "usr-two": "SUCCESS"}
    assert body["applied"] == {"upserted": 1, "deleted": 0}
    assert {r.form_uuid for r in get_user_permissions(db_session, "usr-fresh")} == {"form-y"}


def test_e2e_batch_1_user_loi_thi_khong_ai_duoc_ap_dung(e2e_client, db_session):
    batch_body = {
        "actionType": 1,
        "payload": {
            "users": [
                {
                    "userId": "usr-ok",
                    "isAdmin": False,
                    "formQueries": [
                        {
                            "formUuid": "form-1",
                            "tableInfo": {
                                "databaseTableName": "t",
                                "queries": [{"datasourceId": "d1", "datasourceType": "postgresql", "query": "SELECT 1"}],
                            },
                        }
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

Thay đổi so với bản gốc: `_body()` bỏ tham số `timestamp`, mọi body bỏ key `version`/`timestamp` và
đổi `data` → `payload`; bỏ hẳn 2 bước kịch bản dựa vào STALE (bước 4 "bản tin CŨ gửi lại bị chặn" và
bước 6 "sau khi thu hồi hết, bản tin cũ vẫn không được áp ngược") khỏi
`test_e2e_dong_bo_roi_thu_hoi_va_retry`; đổi tên và viết lại
`test_e2e_batch_nhieu_user_1_thanh_cong_1_stale` thành `test_e2e_batch_nhieu_user_deu_thanh_cong`
(không còn kịch bản STALE, cả 2 user đều SUCCESS).

- [ ] **Step 2: Chạy test để thấy trạng thái trước sửa (tham chiếu)**

Run: `SQLBOT_TEST_DB_URL=<url> pytest tests/test_ai_sync_e2e.py -v`
Expected: FAIL — file test cũ gửi body có `version`/`timestamp`/`data` theo shape cũ, không khớp
route đã đổi ở Task 6 (route sẽ coi `data` là field lạ bị `extra="ignore"` bỏ qua rồi báo thiếu
`payload` → 400 khác với kỳ vọng cũ 200).

Vì bước này sửa test TRƯỚC KHI xác nhận FAIL rõ ràng theo đúng cú pháp cũ, coi bước xác nhận RED là:
chạy lại đúng file test CŨ (trước khi ghi đè) trên route MỚI (đã xong Task 6) — nếu môi trường không
có `SQLBOT_TEST_DB_URL` thì bỏ qua bước này, chuyển thẳng sang Step 3 và dựa vào Step 3 để xác nhận
GREEN.

- [ ] **Step 3: Chạy lại test (bản mới) để thấy PASS**

Run: `SQLBOT_TEST_DB_URL=<url> pytest tests/test_ai_sync_e2e.py -v`
Expected: PASS toàn bộ (hoặc SKIP nếu không có `SQLBOT_TEST_DB_URL`).

- [ ] **Step 4: Commit**

```bash
git add tests/test_ai_sync_e2e.py
git commit -m "test: cập nhật kịch bản e2e AI Sync Hook theo envelope mới, bỏ kịch bản STALE"
```

---

## Task 8: Xác nhận toàn bộ test suite AI Sync Hook xanh

**Files:** Không sửa file nào — chỉ chạy kiểm tra tổng hợp.

**Interfaces:**
- Consumes: toàn bộ Task 1-7.

- [ ] **Step 1: Chạy full bộ test không cần DB**

Run: `pytest tests/test_ai_sync_schema.py tests/test_ai_sync_handler.py tests/test_ai_sync_api.py tests/test_ai_sync_constants.py tests/test_ai_sync_models.py -v`
Expected: PASS toàn bộ (test_ai_sync_handler.py cần `SQLBOT_TEST_DB_URL` như các task trước, các file
còn lại không cần DB).

- [ ] **Step 2: Chạy full bộ test cần DB thật (nếu môi trường có `SQLBOT_TEST_DB_URL`)**

Run: `SQLBOT_TEST_DB_URL=<url> pytest tests/test_ai_sync_crud.py tests/test_ai_sync_handler.py tests/test_ai_sync_e2e.py -v`
Expected: PASS toàn bộ.

- [ ] **Step 3: Grep xác nhận không còn sót tham chiếu STALE/version/timestamp/`.data` cũ**

Run: `grep -rn "SyncStatus.STALE\|get_last_applied_version\|envelope\.data\b" backend/apps/hooks tests`
Expected: Không có kết quả nào (empty output).

- [ ] **Step 4: Không commit gì ở bước này** — nếu Step 1-3 đều xanh, plan coi như hoàn tất. Nếu có
      sai lệch, quay lại đúng task tương ứng để sửa rồi lặp lại Task 8.

---

## Sau khi plan hoàn tất — nhắc việc ngoài phạm vi plan này

Theo quy ước `CLAUDE.md` của repo ("Quy ước giữ tài liệu sống"): sau khi merge xong thay đổi này,
**đề xuất với user** (không tự sửa) rà soát các tài liệu sau vì hợp đồng bản tin đã đổi:

- `backend/scripts/ai_sync_hook/AI_SYNC_HOOK_API_SPEC.md` — mô tả shape envelope
  (`version`/`timestamp`/`data`) cần cập nhật thành `{actionType, payload}`, và mục nói về STALE cần
  bỏ hoặc đánh dấu đã ngừng dùng.
- `docs/superpowers/specs/2026-08-10-ai-sync-hook-design.md` và
  `docs/superpowers/specs/2026-08-11-ai-sync-hook-batch-users-design.md` — spec lịch sử, cân nhắc
  thêm ghi chú "đã thay đổi, xem plan `2026-08-18-ai-sync-hook-drop-version-timestamp.md`" thay vì
  sửa nội dung gốc (giữ tính lịch sử).
- `docs/OPERATIONS.md` / `docs/BACKEND_ARCHITECTURE.md` — nếu có đoạn nào mô tả cơ chế STALE hoặc
  cột `sync_version` như một cơ chế chống trùng, cần sửa thành "thuần thông tin".

Đây không phải một task trong plan (cần user quyết định trước khi sửa tài liệu), nhưng phải nhắc lại
ngay sau khi Task 8 xong.
