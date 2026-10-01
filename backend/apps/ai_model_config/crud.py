"""Đọc ghi bảng ``ai_model_config``. Không commit: để chung transaction với bên gọi."""

from datetime import datetime, timezone
from urllib.parse import urlsplit

from sqlalchemy import delete, update
from sqlmodel import Session, select

from apps.ai_model_config.errors import (
    CODE_SECRET_UNSTABLE,
    ModelConfigError,
    invalid,
)
from apps.ai_model_config.models import (
    MODEL_TYPES,
    ORIGIN_CUSTOM,
    ORIGIN_WORKSPACE,
    SCOPE_DATASOURCE,
    SCOPE_WORKSPACE,
    AiModelConfig,
)
from apps.ai_model_config.schemas import ModelSetIn, mask_key
from common.core.config import settings
from common.utils.aes_crypto import sqlbot_aes_decrypt, sqlbot_aes_encrypt


def _now() -> datetime:
    """Thời điểm hiện tại, có múi giờ."""
    return datetime.now(timezone.utc)


def ensure_secret_stable() -> None:
    """Chặn ghi khoá khi ``SECRET_KEY`` không được đặt tường minh.

    Không đặt thì ``SECRET_KEY`` là chuỗi ngẫu nhiên sinh lúc khởi động: khoá mã hoá hôm nay sẽ
    không giải được sau lần khởi động lại, và mọi chat dùng cấu hình này sẽ hỏng (không rơi về
    model chung). Chặn ngay lúc ghi để lỗi lộ ra ở Gateway, không lộ ra ở người dùng cuối.
    """
    if "SECRET_KEY" not in settings.model_fields_set:
        raise ModelConfigError(
            CODE_SECRET_UNSTABLE,
            503,
            "SQLBot chưa đặt SECRET_KEY cố định nên không lưu được khoá model.",
        )


def encrypt_key(plain: str) -> str:
    """Mã hoá khoá model; kiểm ``SECRET_KEY`` trước, xem ``ensure_secret_stable``."""
    ensure_secret_stable()
    return sqlbot_aes_encrypt(plain)


def _key_matches(row: AiModelConfig, plain: str) -> bool:
    """So khoá đã lưu với khoá mới; giải mã hỏng thì coi như khác để ghi đè.

    Ghi đè là đường phục hồi sau khi đổi ``SECRET_KEY``: Gateway đẩy lại là dòng workspace đọc
    được trở lại.
    """
    try:
        return sqlbot_aes_decrypt(row.api_key_enc) == plain
    except Exception:
        return False


def list_rows(session: Session, scope: str, scope_id: int) -> list[AiModelConfig]:
    """Các dòng cấu hình của một workspace hoặc một datasource."""
    stmt = select(AiModelConfig).where(
        AiModelConfig.scope == scope, AiModelConfig.scope_id == scope_id
    )
    return list(session.exec(stmt).all())


def get_row(
    session: Session, scope: str, scope_id: int, model_type: str
) -> AiModelConfig | None:
    """Dòng cấu hình của một loại model trong một phạm vi, hoặc None."""
    stmt = select(AiModelConfig).where(
        AiModelConfig.scope == scope,
        AiModelConfig.scope_id == scope_id,
        AiModelConfig.model_type == model_type,
    )
    return session.exec(stmt).first()


def put_workspace_models(session: Session, oid: int, model_set: ModelSetIn) -> bool:
    """Thay toàn bộ cấu hình workspace; trả True nếu có gì thay đổi.

    Thiếu khối nào thì xoá dòng loại đó (loại đó về model chung). Idempotent: giá trị không đổi
    thì không ghi, không đổi ``updated_at``, nên cache giải mã và cache embedder không bị xoá
    oan. Đổi ``base_url`` hoặc ``model`` thì xoá ``dim`` vì có thể đã sang model khác số chiều;
    chỉ đổi khoá thì giữ.
    """
    existing = {row.model_type: row for row in list_rows(session, SCOPE_WORKSPACE, oid)}
    wanted = dict(model_set.blocks())
    changed = False
    now = _now()
    for model_type in MODEL_TYPES:
        row = existing.get(model_type)
        block = wanted.get(model_type)
        if block is None:
            if row is not None:
                session.delete(row)
                changed = True
            continue
        plain = block.api_key.get_secret_value()
        if row is None:
            session.add(AiModelConfig(
                scope=SCOPE_WORKSPACE,
                scope_id=oid,
                oid=oid,
                model_type=model_type,
                base_url=block.endpoint,
                model=block.model,
                api_key_enc=encrypt_key(plain),
                api_key_hint=mask_key(plain),
                created_at=now,
                updated_at=now,
            ))
            changed = True
            continue
        same_target = row.base_url == block.endpoint and row.model == block.model
        if same_target and _key_matches(row, plain):
            continue
        if not same_target:
            row.dim = None
        row.base_url = block.endpoint
        row.model = block.model
        row.api_key_enc = encrypt_key(plain)
        row.api_key_hint = mask_key(plain)
        row.updated_at = now
        session.add(row)
        changed = True
    session.flush()
    return changed


def _host_port(url: str) -> tuple[str | None, int | None]:
    """Cặp (host, port) của URL, port mặc định theo scheme."""
    parts = urlsplit(url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return parts.hostname, port


def check_custom_hosts(session: Session, oid: int, model_set: ModelSetIn) -> None:
    """Cấu hình tuỳ chỉnh chỉ được trỏ tới host mà cấu hình workspace đang dùng.

    Không cho trỏ ra host lạ: SQLBot sẽ gửi dữ liệu nghiệp vụ (schema, câu hỏi) tới bất kỳ URL
    nào được khai, nên tuỳ chỉnh chỉ được đổi model và khoá trên cùng cổng LLM. Workspace chưa có
    cấu hình thì không có gì để so, nên từ chối luôn.
    """
    if model_set.is_empty():
        return
    allowed = {_host_port(row.base_url) for row in list_rows(session, SCOPE_WORKSPACE, oid)}
    if not allowed:
        raise invalid(
            "models", "Workspace chưa có cấu hình model nên không nhận cấu hình tuỳ chỉnh."
        )
    for model_type, block in model_set.blocks():
        if _host_port(block.endpoint) not in allowed:
            url_key = "url" if model_type == "rerank" else "base_url"
            raise invalid(
                f"models.{model_type}.{url_key}",
                "Host của cấu hình tuỳ chỉnh phải trùng host cấu hình workspace.",
            )


def pin_datasource_models(
    session: Session, ds_id: int, oid: int, model_set: ModelSetIn
) -> None:
    """Chốt cấu hình model cho datasource vừa tạo, trong cùng transaction với dòng datasource.

    Từng loại: có khối tuỳ chỉnh thì ghi dòng ``custom``; không có mà workspace có thì CHÉP dòng
    workspace thành dòng ``workspace``; không có cả hai thì không ghi, loại đó dùng model chung
    suốt đời datasource. Chép giá trị chứ không tham chiếu, nên đổi cấu hình workspace về sau
    không đụng datasource đã có. Phải gọi trước khi embedding nền chạy.
    """
    check_custom_hosts(session, oid, model_set)
    custom = dict(model_set.blocks())
    workspace_rows = {
        row.model_type: row for row in list_rows(session, SCOPE_WORKSPACE, oid)
    }
    now = _now()
    for model_type in MODEL_TYPES:
        block = custom.get(model_type)
        if block is not None:
            plain = block.api_key.get_secret_value()
            row = AiModelConfig(
                base_url=block.endpoint,
                model=block.model,
                api_key_enc=encrypt_key(plain),
                api_key_hint=mask_key(plain),
                origin=ORIGIN_CUSTOM,
            )
        elif model_type in workspace_rows:
            source = workspace_rows[model_type]
            row = AiModelConfig(
                base_url=source.base_url,
                model=source.model,
                api_key_enc=source.api_key_enc,
                api_key_hint=source.api_key_hint,
                origin=ORIGIN_WORKSPACE,
            )
        else:
            continue
        row.scope = SCOPE_DATASOURCE
        row.scope_id = ds_id
        row.oid = oid
        row.model_type = model_type
        row.created_at = now
        row.updated_at = now
        session.add(row)
    session.flush()


def delete_scope(session: Session, scope: str, scope_id: int) -> None:
    """Xoá mọi dòng cấu hình của một workspace hoặc một datasource."""
    session.exec(
        delete(AiModelConfig).where(
            AiModelConfig.scope == scope, AiModelConfig.scope_id == scope_id
        )
    )


def record_dim(session: Session, row_id: int, dim: int) -> None:
    """Ghi số chiều vector lần đầu; đã có giá trị thì không ghi đè.

    ``WHERE dim IS NULL`` để hai luồng embedding chạy song song không ghi đè lẫn nhau. Không đổi
    ``updated_at``: số chiều không làm cache embedder cũ sai.
    """
    session.exec(
        update(AiModelConfig)
        .where(AiModelConfig.id == row_id, AiModelConfig.dim.is_(None))
        .values(dim=dim)
    )
