"""Lỗi của tính năng cấu hình model, và phân loại lỗi gọi model cho ``/chat/ask``."""

import re

from fastapi import HTTPException

CODE_WORKSPACE_NOT_FOUND = "WORKSPACE_NOT_FOUND"
CODE_INVALID = "MODEL_CONFIG_INVALID"
CODE_SECRET_UNSTABLE = "MODEL_CONFIG_SECRET_UNSTABLE"

MODEL_UNAUTHORIZED = "model_unauthorized"
MODEL_NOT_FOUND = "model_not_found"
MODEL_QUOTA_EXCEEDED = "model_quota_exceeded"
MODEL_UNAVAILABLE = "model_unavailable"

_SECRET_RE = re.compile(r"sk-[\w-]{6,}")


class ModelConfigError(Exception):
    """Lỗi nghiệp vụ trả về cho Gateway: chỉ nêu ``field``, không bao giờ mang giá trị gửi lên.

    Lý do không mang giá trị: body có ``api_key``, còn ``http_exception_handler`` log nguyên
    ``detail`` và trả nguyên ``detail`` ra response.
    """

    def __init__(self, code: str, status: int, message: str, field: str | None = None):
        """Ghi lại mã lỗi, HTTP status, thông điệp và tên trường gây lỗi (nếu có)."""
        super().__init__(message)
        self.code = code
        self.status = status
        self.message = message
        self.field = field

    def to_http(self) -> HTTPException:
        """Đổi sang ``HTTPException`` với ``detail`` dạng ``{code, field, message}``."""
        detail = {"code": self.code, "message": self.message}
        if self.field is not None:
            detail["field"] = self.field
        return HTTPException(status_code=self.status, detail=detail)


def invalid(field: str, message: str = "Cấu hình model không hợp lệ.") -> ModelConfigError:
    """Dựng lỗi 422 ``MODEL_CONFIG_INVALID`` cho một trường."""
    return ModelConfigError(CODE_INVALID, 422, message, field)


class ModelConfigUnreadable(Exception):
    """Dòng cấu hình có nhưng không giải mã được khoá (thường do ``SECRET_KEY`` đã đổi).

    Cố ý không rơi về model chung (fail-closed): chạy nhầm sang model chung là tiêu tiền và lộ
    dữ liệu ra ngoài phạm vi workspace đã khai báo.
    """

    def __init__(self, row_id: int):
        """Chỉ giữ id dòng để log, không giữ gì liên quan tới khoá."""
        super().__init__(f"Không giải mã được khoá của cấu hình model id={row_id}.")
        self.row_id = row_id


def _chain(exc: BaseException):
    """Duyệt ``exc`` rồi lần theo ``__cause__``/``__context__``, có chặn vòng lặp."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or exc.__context__


def classify_model_error(exc: BaseException) -> str | None:
    """Phân loại lỗi gọi model thành mã ``model_*`` cho Gateway; không nhận ra thì trả None.

    LangChain hay bọc lỗi của openai SDK, nên phải lần theo chuỗi nguyên nhân. Import openai trong
    hàm để module này không kéo SDK vào chỗ chỉ cần ``ModelConfigError``.
    """
    import openai

    for item in _chain(exc):
        if isinstance(item, ModelConfigUnreadable):
            return MODEL_UNAVAILABLE
        if isinstance(item, (openai.AuthenticationError, openai.PermissionDeniedError)):
            return MODEL_UNAUTHORIZED
        if isinstance(item, openai.NotFoundError):
            return MODEL_NOT_FOUND
        if isinstance(item, openai.RateLimitError):
            return MODEL_QUOTA_EXCEEDED
        # LiteLLM báo hết ngân sách bằng 400 có chữ "budget" trong thông điệp.
        if isinstance(item, openai.APIStatusError) and "budget" in str(item).lower():
            return MODEL_QUOTA_EXCEEDED
        if isinstance(
            item,
            (openai.APITimeoutError, openai.APIConnectionError, openai.InternalServerError),
        ):
            return MODEL_UNAVAILABLE
    return None


def attach_model_error_code(result: dict, exc: BaseException) -> dict:
    """Thêm ``code`` vào JSON lỗi khi phân loại được lỗi gọi model; trả lại chính ``result``.

    Thay đổi cộng thêm: client cũ không đọc ``code`` vẫn chạy như trước.
    """
    code = classify_model_error(exc)
    if code is not None:
        result["code"] = code
    return result


def sanitize_secret(text: str) -> str:
    """Che chuỗi trông như virtual key LiteLLM (``sk-...``) trong thông điệp lỗi.

    Thông điệp lỗi của nhà cung cấp đôi khi dội lại khoá, mà chuỗi này đi vào log, vào DB
    (``chat_record.error``) và ra response.
    """
    return _SECRET_RE.sub("sk-***", text) if text else text
