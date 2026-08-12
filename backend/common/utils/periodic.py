"""Thread chạy một việc lặp lại theo chu kỳ, dừng được ngay khi ứng dụng tắt.

Vì sao không dùng ``while True: sleep(n)``: ``sleep`` không thể đánh thức từ bên ngoài, nên lúc
shutdown ứng dụng phải chờ hết chu kỳ mới thoát được — với chu kỳ hàng phút thì đó là hàng phút
treo ở mỗi lần deploy. ``Event.wait(n)`` chờ đúng như vậy nhưng trả về NGAY khi có người bật cờ.
"""

import threading
from collections.abc import Callable

from common.utils.utils import SQLBotLogUtil


class PeriodicThread:
    """Gọi ``fn`` mỗi ``interval`` giây trong một daemon thread.

    Exception của ``fn`` được nuốt và ghi log, KHÔNG được phép giết thread: một lần DB chớp tắt
    không được làm cả vòng lặp biến mất cho tới lần khởi động lại tiếp theo.

    Đặt daemon để tiến trình vẫn thoát được kể cả khi ai đó quên gọi ``stop``.
    """

    def __init__(self, name: str, interval: int, fn: Callable[[], None], initial_delay: int = 0):
        self._name = name
        self._interval = max(1, interval)
        self._fn = fn
        self._initial_delay = initial_delay
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _run(self) -> None:
        if self._initial_delay and self._stop.wait(self._initial_delay):
            return
        while not self._stop.is_set():
            try:
                self._fn()
            except Exception as e:
                SQLBotLogUtil.exception(f"periodic task {self._name} failed: {e}")
            self._stop.wait(self._interval)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
        self._thread.start()
        SQLBotLogUtil.info(f"periodic task {self._name} started, interval={self._interval}s")

    def stop(self, timeout: float = 5.0) -> None:
        """Bật cờ dừng rồi chờ có giới hạn.

        Chờ có giới hạn chứ không chờ vô hạn: nếu ``fn`` đang kẹt ở một lời gọi mạng thì shutdown
        vẫn phải đi tiếp, thread daemon sẽ chết cùng tiến trình.
        """
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None
