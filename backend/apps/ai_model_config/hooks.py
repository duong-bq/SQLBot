"""Điểm móc vào code upstream: mỗi chỗ gọi chỉ tốn một dòng, lỗi đã đổi sẵn sang HTTPException.

Gom về đây để diff trên file upstream nhỏ nhất có thể (giảm conflict khi merge từ upstream).
"""

from typing import Any

from sqlmodel import Session

from apps.ai_model_config.crud import check_custom_hosts, delete_scope, pin_datasource_models
from apps.ai_model_config.errors import ModelConfigError
from apps.ai_model_config.models import SCOPE_DATASOURCE, SCOPE_WORKSPACE
from apps.ai_model_config.schemas import ModelSetIn, parse_model_set


def validate_ds_models(session: Session, oid: int, raw: Any) -> ModelSetIn:
    """Parse trường ``models`` khi tạo datasource và kiểm host, trước mọi tác dụng phụ.

    Gọi sớm, nhất là ở đường Excel: ``import_to_db`` tạo bảng vật lý trước khi có datasource, lỗi
    muộn sẽ để lại bảng mồ côi.
    """
    try:
        model_set = parse_model_set(raw)
        check_custom_hosts(session, oid, model_set)
    except ModelConfigError as exc:
        raise exc.to_http() from None
    return model_set


def pin_ds_models(session: Session, ds_id: int, oid: int, model_set: ModelSetIn) -> None:
    """Chốt cấu hình model cho datasource vừa flush, trước commit; xem ``pin_datasource_models``."""
    try:
        pin_datasource_models(session, ds_id, oid, model_set)
    except ModelConfigError as exc:
        raise exc.to_http() from None


def cleanup_datasource(session: Session, ds_id: int) -> None:
    """Xoá cấu hình model của datasource bị xoá."""
    delete_scope(session, SCOPE_DATASOURCE, ds_id)


def cleanup_workspace(session: Session, oid: int) -> None:
    """Xoá cấu hình model cấp workspace của workspace bị xoá.

    Dòng datasource đi theo vòng đời datasource, không xoá ở đây: SQLBot không xoá datasource khi
    xoá workspace.
    """
    delete_scope(session, SCOPE_WORKSPACE, oid)
