"""Giải mã khoá model có cache, dùng chung cho LLM và embedding.

Bản sync ``sqlbot_aes_decrypt`` vì hàm này chạy cả trong thread nền (embedding, worker Excel).
"""

import threading

from apps.ai_model_config.errors import ModelConfigUnreadable
from apps.ai_model_config.models import AiModelConfig
from common.utils.aes_crypto import sqlbot_aes_decrypt

_DECRYPT_CACHE_MAX = 1024

_lock = threading.Lock()
_decrypted: dict[tuple, str] = {}


def cache_key(row: AiModelConfig) -> tuple:
    """Khoá cache theo ``(id, updated_at)``: dòng đổi nội dung là tự rơi khỏi cache.

    Không bao giờ dùng khoá thô làm khoá cache.
    """
    return row.id, row.updated_at


def decrypt_row_key(row: AiModelConfig) -> str:
    """Giải mã khoá của dòng, có cache; hỏng thì ném ``ModelConfigUnreadable``.

    Cache vì mỗi lần giải mã tốn khoảng 35 ms, mà job embedding gọi theo từng bảng.
    """
    key = cache_key(row)
    with _lock:
        plain = _decrypted.get(key)
    if plain is not None:
        return plain
    try:
        plain = sqlbot_aes_decrypt(row.api_key_enc)
    except Exception:
        raise ModelConfigUnreadable(row.id) from None
    if not plain:
        raise ModelConfigUnreadable(row.id)
    with _lock:
        if len(_decrypted) >= _DECRYPT_CACHE_MAX:
            _decrypted.clear()
        _decrypted[key] = plain
    return plain
