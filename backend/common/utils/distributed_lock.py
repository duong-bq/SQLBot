"""Distributed-lock abstraction for process-wide singleton tasks.

The current provider uses PostgreSQL advisory locks. Business callers use
``SingleWorkerGuard``, which delegates lock operations to ``DistributedLock``.
"""

import hashlib
from collections.abc import Callable
from enum import Enum, auto
from functools import wraps
from threading import Lock
from typing import ClassVar, Self

from sqlalchemy import func, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError

from common.core.db import engine
from common.utils.utils import SQLBotLogUtil


class LockStatus(Enum):
    """Process-local state of the single-worker lock acquisition attempt."""

    NOT_ATTEMPTED = auto()
    ACQUIRED = auto()
    NOT_ACQUIRED = auto()


class DistributedLock:
    """A lock held by this process until ``release`` or process shutdown."""

    def __init__(
        self,
        advisory_key: int,
        connection: Connection,
    ) -> None:
        self._connection = connection
        self._advisory_key = advisory_key

    @staticmethod
    def _postgres_advisory_key(key: str) -> int:
        """Convert a readable lock key into PostgreSQL's signed 64-bit key."""
        digest = hashlib.sha256(key.encode("utf-8")).digest()
        return int.from_bytes(digest[:8], byteorder="big", signed=True)

    @classmethod
    def try_acquire(cls, key: str) -> Self | None:
        """Try to acquire a named distributed lock without waiting."""
        advisory_key = cls._postgres_advisory_key(key)
        connection: Connection | None = None
        try:
            connection = engine.connect()
            acquired = connection.execute(
                select(func.pg_try_advisory_lock(advisory_key))
            ).scalar_one()
            # End the implicit transaction; the session-level lock remains held.
            connection.commit()
        except SQLAlchemyError:
            SQLBotLogUtil.exception("Failed to acquire distributed lock: %s", key)
            if connection is not None:
                connection.close()
            return None

        if not acquired:
            connection.close()
            return None

        return cls(advisory_key, connection)

    def release(self) -> None:
        """Release the lock and return its dedicated connection to the pool."""
        if self._connection.closed:
            return

        try:
            self._connection.execute(
                select(func.pg_advisory_unlock(self._advisory_key))
            )
            self._connection.commit()
        finally:
            self._connection.close()


def acquire_xact_lock(session, key: str, timeout_ms: int | None = None) -> None:
    """Chiếm một advisory lock ĐẶT TÊN, tự nhả khi transaction hiện tại kết thúc.

    Khác ``SingleWorkerGuard`` ở bốn điểm, và mỗi điểm đều có lý do:

    - **Khóa theo tên động**, không phải một hằng số toàn cục. Hai thao tác trên hai tên khác nhau
      sinh hai số khóa khác nhau nên không ai chờ ai; chỉ đúng cặp trùng tên mới xếp hàng.
    - **Chờ** (``pg_advisory_xact_lock``) thay vì thử một lần rồi bỏ: mục đích là tuần tự hóa hai
      request, không phải chọn ra một tiến trình.
    - **Cấp transaction**: tự nhả khi commit/rollback/tiến trình chết. Loại cấp session mà quên nhả
      thì khóa kẹt tới khi connection đóng, mà connection trong pool sống rất lâu.
    - Dùng lại chính ``session`` của người gọi, để khóa và các câu lệnh cần bảo vệ nằm chung một
      transaction. Xin khóa trên connection khác là không bảo vệ được gì.

    Dùng để bịt lỗ TOCTOU của ``check_name``: đó chỉ là một câu SELECT, không có ràng buộc unique
    nào đứng sau, nên hai request cùng tên chạy song song đều thấy "chưa có" rồi cùng chèn.
    """
    advisory_key = DistributedLock._postgres_advisory_key(key)
    if timeout_ms:
        # SET LOCAL chỉ có hiệu lực tới hết transaction, không rò sang request sau dùng lại
        # connection này.
        session.execute(text(f"SET LOCAL lock_timeout = '{int(timeout_ms)}ms'"))
    session.execute(select(func.pg_advisory_xact_lock(advisory_key)))


def try_acquire_xact_lock(session, key: str) -> bool:
    """Thử chiếm advisory lock cấp transaction, **không chờ**: trả ``True``/``False`` ngay.

    Sinh ra để dùng trong ``async def``. Bản chờ (``acquire_xact_lock``) chạy trong route bất đồng
    bộ là một câu lệnh DB đồng bộ có thể đứng im vài giây, mà đứng im trong coroutine nghĩa là chặn
    cả event loop — mọi request khác, kể cả các lời gọi mạng đang chờ, đứng theo. Đã đo được hậu quả
    thật: client redis async trong cùng tiến trình văng ``TimeoutError`` khi có vài request cùng tên
    bắn ra một lúc.

    Người gọi tự quyết định chờ bằng cách lặp lại và ``await asyncio.sleep`` giữa hai lần thử — lúc
    đó quyền điều khiển được trả về cho event loop nên không ai bị chặn.

    Thất bại phải ``rollback`` trước khi thử lại: câu SELECT này đã mở một transaction, và khóa đã
    chiếm được (nếu có) chỉ nhả khi transaction kết thúc.
    """
    advisory_key = DistributedLock._postgres_advisory_key(key)
    return bool(session.execute(select(func.pg_try_advisory_xact_lock(advisory_key))).scalar())


class SingleWorkerGuard:
    """Run startup tasks in only one worker process."""

    _LOCK_KEY = "sqlbot:startup:embedding"
    _lock_status: ClassVar[LockStatus] = LockStatus.NOT_ATTEMPTED
    # Keep the acquired lock and its dedicated connection alive.
    _acquired_lock: ClassVar[DistributedLock | None] = None
    _state_mutex = Lock()

    @classmethod
    def _has_acquired_lock(cls) -> bool:
        with cls._state_mutex:
            if cls._lock_status is LockStatus.NOT_ATTEMPTED:
                lock = DistributedLock.try_acquire(cls._LOCK_KEY)
                if lock is None:
                    cls._lock_status = LockStatus.NOT_ACQUIRED
                    SQLBotLogUtil.info(
                        "Current process FAILED to acquire the single-worker lock"
                    )
                else:
                    cls._acquired_lock = lock
                    cls._lock_status = LockStatus.ACQUIRED
                    SQLBotLogUtil.info(
                        "Current process acquired the single-worker lock"
                    )
            return cls._lock_status is LockStatus.ACQUIRED

    @classmethod
    def once(cls, func: Callable[[], None]) -> Callable[[], None]:
        """Run the decorated startup task in only one worker."""

        @wraps(func)
        def wrapper() -> None:
            if not cls._has_acquired_lock():
                return
            func()

        return wrapper

    @classmethod
    def release(cls) -> None:
        """Release the single-worker lock and reset this process's state."""
        with cls._state_mutex:
            try:
                if cls._acquired_lock is not None:
                    cls._acquired_lock.release()
            finally:
                cls._acquired_lock = None
                cls._lock_status = LockStatus.NOT_ATTEMPTED
