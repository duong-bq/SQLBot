"""Bảng job của luồng import excel bất đồng bộ, kiêm luôn outbox cho callback về hệ ngoài.

Nguyên tắc chi phối toàn bộ thiết kế bảng này: worker chạy ở tiến trình khác, transaction khác, có
thể ở lần khởi động khác của ứng dụng. Mọi thứ worker cần — và mọi thứ cần để dọn dẹp sau khi
worker chết — đều phải nằm trong DB, không được chỉ tồn tại trong RAM của request.

Bảng này KHÔNG tách riêng phần outbox: một dòng vừa là hồ sơ công việc, vừa là lá thư còn nợ. Với
quan hệ một job ↔ một callback thì tách hai bảng chỉ thêm một phép join mà không mua được gì.

Theo khuôn của ``ai_sync_hook_logs``: index khai ngay trong ``__table_args__`` (không chỉ trong
migration) để test tích hợp dựng bảng bằng metadata vẫn có đủ ràng buộc thật.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Column,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlmodel import Field, SQLModel


class ExcelImportJobStatus:
    """Trạng thái xử lý của bản thân job import."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"

    # Job chưa tới đích: đây là tập mà vòng quét phục hồi quan tâm.
    ACTIVE = (PENDING, RUNNING)


class CallbackStatus:
    """Trạng thái của lá thư callback trong outbox.

    ``NONE`` là lúc job chưa xong nên chưa nợ ai cái gì. ``SENDING`` là đã được một tiến trình nhận
    việc nhưng chưa biết kết quả — trạng thái này bắt buộc phải có vì lời gọi HTTP nằm NGOÀI
    transaction (giữ transaction mở suốt thời gian chờ mạng là giữ khóa dòng và một connection
    Postgres vô ích). Cái giá là dòng ``SENDING`` có thể kẹt khi tiến trình chết giữa chừng, nên
    vòng quét phục hồi phải đưa dòng ``SENDING`` quá cũ về lại ``PENDING``.

    ``DEAD`` là đã thử đủ số lần cho phép; dừng lại để không đốt chu kỳ, và để một câu truy vấn
    giám sát đếm được. Endpoint bắn lại thủ công chỉ việc đưa nó về ``PENDING`` với
    ``next_attempt_at = now()``.
    """

    NONE = "none"
    PENDING = "pending"
    SENDING = "sending"
    SENT = "sent"
    DEAD = "dead"

    # Trạng thái mà vòng gửi có thể nhận việc.
    CLAIMABLE = (PENDING,)


class ExcelImportJob(SQLModel, table=True):
    """Một lần import excel bất đồng bộ.

    Vì sao không có cột ``job_id`` riêng: ``ds_id`` đã là khóa đối chiếu với hệ ngoài (``externalId``
    trong payload callback chính là dsId), đồng thời ràng buộc duy nhất trên nó chặn luôn việc hai
    job cùng chạy cho một nguồn dữ liệu.
    """

    __tablename__ = "excel_import_jobs"
    __table_args__ = (
        UniqueConstraint("ds_id", name="uq_excel_import_jobs_ds_id"),
        # Vòng gửi callback quét theo cặp này mỗi chu kỳ.
        Index("idx_excel_import_jobs_callback", "callback_status", "next_attempt_at"),
        # Vòng phục hồi job chết quét theo cặp này.
        Index("idx_excel_import_jobs_status_heartbeat", "status", "heartbeat_at"),
    )

    id: int = Field(sa_column=Column(BigInteger, Identity(always=True), nullable=False, primary_key=True))
    ds_id: int = Field(sa_column=Column(BigInteger, nullable=False))
    oid: int = Field(sa_column=Column(BigInteger, nullable=False))
    create_by: int = Field(sa_column=Column(BigInteger, nullable=True))

    # --- Đầu vào của worker. Worker không thấy biến trong RAM của request nên phải chép xuống đây.
    # Nơi file NẰM hoặc SẼ NẰM. Từ khi bytes tới bằng đường tải từ presigned URL, pha đồng bộ chỉ
    # đặt trước cái tên còn worker mới là bên ghi ra file — nên trong khoảng giữa hai việc đó, cột
    # này trỏ tới một file chưa tồn tại. Đặt tên sẵn thay vì để rỗng là cố ý: giữ được ràng buộc
    # NOT NULL, và mọi chỗ đang đọc cột này (nhất là nhánh dọn file của job chết trong
    # ``excel_recovery``) không phải rải thêm phép kiểm rỗng.
    file_path: str = Field(sa_column=Column(Text, nullable=False))
    # URL nguồn do hệ ngoài cung cấp. NULL nghĩa là job cũ theo đường multipart — nhánh đó vẫn phải
    # chạy được, vì lúc deploy có thể còn job đang ``pending`` tạo từ bản trước.
    #
    # CẢNH BÁO: với presigned URL thì chuỗi này CHÍNH LÀ một credential có hạn. Không bao giờ log
    # nguyên văn; dùng phần host cộng object key (xem ``remote_file.RemoteSource.safe_label``).
    file_url: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    # Nguyên văn danh sách sheet client gửi, CHƯA đối chiếu với file — pha đồng bộ không còn cầm
    # file nên không kiểm được nữa. Việc đối chiếu là của worker, sau khi tải xong.
    sheet_names: list[str] = Field(default_factory=list, sa_column=Column(JSONB, nullable=False))
    ds_name: str = Field(sa_column=Column(String(128), nullable=False))

    # --- Vòng đời job.
    status: str = Field(
        default=ExcelImportJobStatus.PENDING,
        sa_column=Column(String(30), nullable=False, server_default="pending"),
    )
    # hostname:pid của tiến trình đang giữ job. Cần để lúc shutdown một tiến trình chỉ đánh dấu
    # đúng job của chính nó, và để log truy được job chạy ở đâu.
    worker_id: str | None = Field(default=None, sa_column=Column(String(128), nullable=True))
    # Worker còn sống thì cập nhật mốc này định kỳ. Đây là thứ phân biệt "job chết" với "job chạy
    # lâu" — chỉ có started_at thì mọi ngưỡng đều sai: ngắn quá thì giết oan job đang import file
    # lớn, dài quá thì job chết thật phải nằm chờ hàng giờ.
    heartbeat_at: datetime | None = Field(default=None, sa_column=Column(TIMESTAMP(timezone=True), nullable=True))
    created_at: datetime | None = Field(
        default=None, sa_column=Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
    )
    started_at: datetime | None = Field(default=None, sa_column=Column(TIMESTAMP(timezone=True), nullable=True))
    finished_at: datetime | None = Field(default=None, sa_column=Column(TIMESTAMP(timezone=True), nullable=True))

    # --- Dấu vết dọn dẹp.
    # Tên các bảng vật lý đã tạo, GHI NGAY sau khi tạo xong từng bảng chứ không đợi cuối job.
    # Không được trông vào ``configuration.sheets`` của nguồn dữ liệu như ``delete_ds`` đang làm:
    # cột đó chỉ có nội dung ở giữa worker, nên worker chết sau khi tạo 2/3 bảng thì hai bảng kia
    # không còn dấu vết nào. Tên bảng mang 10 ký tự hash ngẫu nhiên, mất tên là mồ côi vĩnh viễn.
    created_tables: list[str] = Field(default_factory=list, sa_column=Column(JSONB, nullable=False))

    # --- Kết quả. error_code cho máy phân loại, error_message cho người đọc.
    error_code: str | None = Field(default=None, sa_column=Column(String(100), nullable=True))
    error_message: str | None = Field(default=None, sa_column=Column(Text, nullable=True))

    # --- Outbox callback.
    callback_status: str = Field(
        default=CallbackStatus.NONE,
        sa_column=Column(String(30), nullable=False, server_default="none"),
    )
    callback_attempts: int = Field(default=0, sa_column=Column(Integer, nullable=False, server_default="0"))
    # Lịch thử lại nằm ở đây chứ không nằm trong sleep() của một thread: sleep bốc hơi cùng tiến
    # trình khi deploy hay OOM, còn cột này sống sót qua mọi lần khởi động lại.
    next_attempt_at: datetime | None = Field(default=None, sa_column=Column(TIMESTAMP(timezone=True), nullable=True))
    callback_claimed_at: datetime | None = Field(
        default=None, sa_column=Column(TIMESTAMP(timezone=True), nullable=True)
    )
    callback_last_error: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    callback_payload: dict[str, Any] | None = Field(default=None, sa_column=Column(JSONB, nullable=True))
