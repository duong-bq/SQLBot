"""Handler cho `actionType = 4` (DATASOURCE_SYNC).

Bản tin không mang trạng thái mới — chỉ là cú hích bảo SQLBot đọc lại catalog DB nguồn của các
datasource được nêu tên rồi mirror metadata (bảng, cột, comment, quan hệ) theo đó. Vì vậy:

- KHÔNG kiểm STALE theo `sync_version`: replay một bản tin cũ chỉ là đọc lại trạng thái HIỆN TẠI
  của DB nguồn lần nữa, vô hại. Chống trùng do retry mạng đã có idempotency key ở tầng route.
- Ngữ nghĩa PER-ITEM chứ không all-or-nothing như AUTHORIZATION_SYNC: các datasource độc lập
  nhau, và `sync_table` bên dưới commit rải rác trong vòng lặp nên vốn không thể gói một
  transaction — nguồn nào hỏng thì item đó FAILED kèm lý do, nguồn khác vẫn chạy tiếp.

Validate cấu trúc cả batch trước khi áp (mảng rỗng, id lặp, id không phải số → lỗi 400, chưa áp
cho ai). Ba loại nguồn bị từ chối per-item: không tồn tại; loại excel (không có DB nguồn ngoài —
đọc "catalog nguồn" của nó sẽ trúng PG nội bộ của SQLBot và mirror nhầm ruột hệ thống); đang
`Importing` (worker đang chạy `sync_table` song song, chen vào là race ghi đè embedding).
"""

from pydantic import ValidationError
from sqlmodel import Session

from apps.datasource.models.datasource import CoreDatasource, DatasourceStatus
from apps.hooks.constants import SyncErrorCode, SyncStatus
from apps.hooks.errors import SyncHookError
from apps.hooks.handlers.authorization import HandlerResult
from apps.hooks.schemas.ai_sync_schema import (
    DatasourceSyncData,
    DatasourceSyncResult,
    SyncAppliedCounts,
    SyncEnvelope,
    find_first_duplicate,
)
from common.utils.utils import SQLBotLogUtil


def _failed(ds_id: str, message: str) -> DatasourceSyncResult:
    """Dựng result FAILED cho một datasource, gom về một chỗ cho thống nhất format."""
    return DatasourceSyncResult(
        datasourceId=ds_id, status=SyncStatus.FAILED.value, message=message
    )


def handle_datasource_sync(
    session: Session, envelope: SyncEnvelope, sync_version: int
) -> HandlerResult:
    """Parse `data.datasourceIds` rồi resync từng datasource theo catalog DB nguồn của nó.

    `sync_version` nhận cho khớp chữ ký chung của ACTION_HANDLERS nhưng cố ý không dùng — xem
    docstring module. `status` tổng thể luôn SUCCESS khi hàm trả về bình thường (batch đã được xử
    lý xong), kết cục từng nguồn nằm trong `results` — cùng hợp đồng với handler authorization.

    `resync_ds_metadata` phải import LAZY trong thân hàm: `apps.datasource.crud.datasource` nằm
    trong vòng import có sẵn của upstream (crud.datasource → sqlbot_xpack → system.api.user →
    system.schemas.permission → crud.datasource), app chỉ boot được nhờ thứ tự nạp trong main.py
    đi qua ngả khác trước — import ở mức module tại đây sẽ vỡ khi module hooks bị nạp trước cụm
    datasource (vd trong test).
    """
    from apps.datasource.crud.datasource import resync_ds_metadata

    try:
        payload = DatasourceSyncData.model_validate(envelope.data)
    except ValidationError as e:
        raise SyncHookError(400, SyncErrorCode.INVALID_PAYLOAD, str(e)) from e

    if not payload.datasource_ids:
        raise SyncHookError(
            400, SyncErrorCode.EMPTY_DATASOURCE_LIST, "data.datasourceIds không được rỗng"
        )

    duplicated = find_first_duplicate(payload.datasource_ids)
    if duplicated:
        raise SyncHookError(
            400,
            SyncErrorCode.DUPLICATE_DATASOURCE_ID,
            f"datasourceId bị lặp trong payload: {duplicated}",
        )

    for raw_id in payload.datasource_ids:
        try:
            int(raw_id)
        except (TypeError, ValueError):
            raise SyncHookError(
                400,
                SyncErrorCode.INVALID_PAYLOAD,
                f"datasourceId phải là số dạng chuỗi: {raw_id!r}",
            )

    results: list[DatasourceSyncResult] = []
    for raw_id in payload.datasource_ids:
        ds = session.get(CoreDatasource, int(raw_id))
        if ds is None:
            results.append(_failed(raw_id, "datasource không tồn tại"))
            continue
        if ds.type and ds.type.lower() == "excel":
            results.append(_failed(raw_id, "nguồn excel không có DB nguồn để đồng bộ lại"))
            continue
        if ds.status == DatasourceStatus.IMPORTING:
            results.append(_failed(raw_id, "nguồn đang nạp dữ liệu (Importing), thử lại sau"))
            continue

        try:
            total, deleted = resync_ds_metadata(session, ds)
        except Exception as e:  # noqa: BLE001 — lỗi một nguồn không được chặn các nguồn còn lại
            session.rollback()
            SQLBotLogUtil.error(
                f"[ai-sync] DATASOURCE_SYNC: resync datasource {raw_id} thất bại: {e}",
                exc_info=True,
            )
            results.append(_failed(raw_id, str(e)))
            continue

        results.append(
            DatasourceSyncResult(
                datasourceId=raw_id,
                status=SyncStatus.SUCCESS.value,
                applied=SyncAppliedCounts(upserted=total, deleted=deleted),
            )
        )

    return HandlerResult(status=SyncStatus.SUCCESS, results=results)
