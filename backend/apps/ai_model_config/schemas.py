"""Schema vào/ra của cấu hình model.

Khối llm và embedding dùng ``base_url`` kiểu OpenAI; khối rerank dùng ``url`` đầy đủ, giống khối
Gateway gửi cho LightRAG. Trường lạ bị bỏ qua để Gateway thêm trường về sau không làm vỡ SQLBot.
"""

import json
from typing import Any, Iterator, Optional
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator

from apps.ai_model_config.errors import invalid
from apps.ai_model_config.models import (
    MODEL_EMBEDDING,
    MODEL_LLM,
    MODEL_RERANK,
    AiModelConfig,
)


def _check_http_url(value: str) -> str:
    """Chỉ nhận URL http(s) có host; bỏ dấu ``/`` thừa ở cuối."""
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("URL phải là http(s) và có host.")
    return value.rstrip("/")


def _check_api_key(value: SecretStr) -> SecretStr:
    """Khoá không được rỗng, chỉ có khoảng trắng, hay dài bất thường."""
    plain = value.get_secret_value()
    if not plain.strip() or len(plain) > 4096:
        raise ValueError("api_key không hợp lệ.")
    return value


class _EndpointBase(BaseModel):
    """Phần chung của một khối model: tên model và khoá."""

    model_config = ConfigDict(extra="ignore", protected_namespaces=())

    model: str = Field(min_length=1, max_length=255)
    api_key: SecretStr

    @field_validator("api_key")
    @classmethod
    def check_api_key(cls, value: SecretStr) -> SecretStr:
        """Xem ``_check_api_key``."""
        return _check_api_key(value)


class OpenAIEndpointIn(_EndpointBase):
    """Khối llm hoặc embedding: gọi qua API tương thích OpenAI tại ``base_url``."""

    base_url: str = Field(min_length=1, max_length=2048)

    @field_validator("base_url")
    @classmethod
    def check_base_url(cls, value: str) -> str:
        """Xem ``_check_http_url``."""
        return _check_http_url(value)

    @property
    def endpoint(self) -> str:
        """URL lưu vào cột ``base_url``."""
        return self.base_url


class RerankEndpointIn(_EndpointBase):
    """Khối rerank: ``url`` là địa chỉ đầy đủ của endpoint rerank."""

    url: str = Field(min_length=1, max_length=2048)

    @field_validator("url")
    @classmethod
    def check_url(cls, value: str) -> str:
        """Xem ``_check_http_url``."""
        return _check_http_url(value)

    @property
    def endpoint(self) -> str:
        """URL lưu vào cột ``base_url``."""
        return self.url


class ModelSetIn(BaseModel):
    """Bộ model gửi lên; mỗi khối tuỳ chọn."""

    model_config = ConfigDict(extra="ignore")

    llm: Optional[OpenAIEndpointIn] = None
    embedding: Optional[OpenAIEndpointIn] = None
    rerank: Optional[RerankEndpointIn] = None

    def blocks(self) -> Iterator[tuple[str, _EndpointBase]]:
        """Lần lượt trả ``(model_type, khối)`` cho các khối có gửi."""
        for model_type in (MODEL_LLM, MODEL_EMBEDDING, MODEL_RERANK):
            block = getattr(self, model_type)
            if block is not None:
                yield model_type, block

    def is_empty(self) -> bool:
        """True khi không có khối nào."""
        return next(self.blocks(), None) is None


def parse_model_set(raw: Any, root: str = "models") -> ModelSetIn:
    """Đọc bộ model từ dict, chuỗi JSON (trường form) hoặc None.

    Validate thủ công thay vì để FastAPI làm: handler 422 mặc định dội lại ``input`` (có
    ``api_key``) trong response. Lỗi chỉ nêu đường dẫn trường, ví dụ ``models.llm.api_key``.
    """
    if raw is None:
        return ModelSetIn()
    if isinstance(raw, str):
        if not raw.strip():
            return ModelSetIn()
        try:
            raw = json.loads(raw)
        except ValueError:
            raise invalid(root, "models phải là chuỗi JSON hợp lệ.") from None
    if not isinstance(raw, dict):
        raise invalid(root, "models phải là object.")
    try:
        return ModelSetIn.model_validate(raw)
    except ValidationError as exc:
        loc = exc.errors()[0].get("loc", ())
        field = ".".join([root, *(str(part) for part in loc)])
        # ``from None``: không giữ ValidationError gốc, trong đó có ``input`` chứa khoá.
        raise invalid(field) from None


def mask_key(api_key: str) -> str:
    """Trả 4 ký tự cuối để đối soát; khoá quá ngắn thì không lộ ký tự nào."""
    return api_key[-4:] if len(api_key) >= 12 else ""


def serialize_rows(rows: list[AiModelConfig]) -> dict:
    """Dựng khối đã che khoá từ các dòng cấu hình, theo đúng hình dạng khối gửi lên."""
    out = {}
    for row in rows:
        url_key = "url" if row.model_type == MODEL_RERANK else "base_url"
        block = {url_key: row.base_url, "model": row.model, "api_key_hint": row.api_key_hint}
        if row.model_type == MODEL_EMBEDDING:
            block["dim"] = row.dim
        if row.origin is not None:
            block["origin"] = row.origin
        out[row.model_type] = block
    return out
