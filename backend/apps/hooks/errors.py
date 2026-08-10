"""Exception nghiệp vụ của hook, mang sẵn HTTP status và errorCode để route khỏi phải map lại."""

from apps.hooks.constants import SyncErrorCode


class SyncHookError(Exception):
    """Lỗi có thể trả thẳng cho SW.

    Mọi lỗi *không* thuộc loại này khi lọt ra khỏi handler đều bị route coi là INTERNAL_ERROR (500)
    — tức là bug, không phải lỗi hợp đồng.
    """

    def __init__(self, http_status: int, error_code: SyncErrorCode, message: str) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.error_code = error_code
        self.message = message
