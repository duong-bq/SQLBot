"""Các tác vụ nền của nguồn dữ liệu: worker import excel, vòng gửi callback, vòng quét phục hồi.

Gói này gom hai điểm bật/tắt duy nhất cho tầng ứng dụng gọi, để ``main.py`` không phải biết bên
trong có mấy vòng lặp và chúng tên gì.
"""

from .callback_sender import start_callback_sender, stop_callback_sender
from .excel_import_task import shutdown_import_executor
from .excel_recovery import start_excel_recovery, stop_excel_recovery


def start_datasource_background_tasks() -> None:
    """Bật các vòng nền, gọi một lần lúc ứng dụng khởi động.

    Cố ý KHÔNG bọc ``SingleWorkerGuard``: các tác vụ khởi động kia chỉ được chạy một tiến trình vì
    chúng làm cùng một việc trên cùng dữ liệu. Hai vòng ở đây thì ngược lại — chúng tranh việc bằng
    ``FOR UPDATE SKIP LOCKED`` và UPDATE có điều kiện, nên chạy nhiều tiến trình vừa an toàn vừa có
    lợi: mất một tiến trình không làm việc gửi callback và việc phục hồi ngừng lại.
    """
    start_callback_sender()
    start_excel_recovery()


def stop_datasource_background_tasks() -> None:
    """Tắt các vòng nền lúc ứng dụng đóng, theo thứ tự ngược với lúc bật."""
    stop_excel_recovery()
    stop_callback_sender()
    shutdown_import_executor()
