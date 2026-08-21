import secrets
import urllib.parse
from typing import Annotated, Any, Literal

from pydantic import (
    AliasChoices,
    AnyUrl,
    BeforeValidator,
    Field,
    PostgresDsn,
    computed_field,
    field_validator
)
from pydantic_core import MultiHostUrl
from pydantic_settings import BaseSettings, SettingsConfigDict


def parse_cors(v: Any) -> list[str] | str:
    if isinstance(v, str) and not v.startswith("["):
        return [i.strip() for i in v.split(",")]
    elif isinstance(v, list | str):
        return v
    raise ValueError(v)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # Use top level .env file (one level above ./backend/)
        env_file="../.env",
        env_ignore_empty=True,
        extra="ignore",
    )
    PROJECT_NAME: str = "SQLBot"
    #CONTEXT_PATH: str = "/sqlbot"
    CONTEXT_PATH: str = ""
    SECRET_KEY: str = secrets.token_urlsafe(32)
    # 60 minutes * 24 hours * 8 days = 8 days
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 8
    FRONTEND_HOST: str = "http://localhost:5173"

    BACKEND_CORS_ORIGINS: Annotated[
        list[AnyUrl] | str, BeforeValidator(parse_cors)
    ] = []

    # Mở CORS cho mọi origin. Không khai được bằng BACKEND_CORS_ORIGINS='*' vì kiểu là AnyUrl.
    # Bật cờ này thì main.py dùng allow_origin_regex='.*' thay cho allow_origins: Starlette luôn
    # echo lại Origin của request nên vẫn hoạt động với credentials, khác allow_origins=['*'] chỉ
    # trả literal '*' khi request không có cookie.
    CORS_ALLOW_ALL_ORIGINS: bool = False

    @computed_field  # type: ignore[prop-decorator]
    @property
    def all_cors_origins(self) -> list[str]:
        return [str(origin).rstrip("/") for origin in self.BACKEND_CORS_ORIGINS] + [
            self.FRONTEND_HOST
        ]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def API_V1_STR(self) -> str:
        return self.CONTEXT_PATH + "/api/v1"

    POSTGRES_SERVER: str = 'localhost'
    POSTGRES_PORT: int = 5432
    POSTGRES_USER: str = 'root'
    POSTGRES_PASSWORD: str = "Password123@pg"
    POSTGRES_DB: str = "sqlbot"
    SQLBOT_DB_URL: str = ''
    # SQLBOT_DB_URL: str = 'mysql+pymysql://root:Password123%40mysql@127.0.0.1:3306/sqlbot'

    TOKEN_KEY: str = "X-SQLBOT-TOKEN"
    DEFAULT_PWD: str = "SQLBot@123456"
    ASSISTANT_TOKEN_KEY: str = "X-SQLBOT-ASSISTANT-TOKEN"

    CACHE_TYPE: Literal["redis", "memory", "None"] = "memory"
    CACHE_REDIS_URL: str | None = None  # Redis URL, e.g., "redis://[[username]:[password]]@localhost:6379/0"

    LOG_LEVEL: str = "INFO"  # DEBUG, INFO, WARNING, ERROR
    LOG_DIR: str = "logs"
    LOG_FORMAT: str = "%(asctime)s - %(name)s - %(levelname)s:%(lineno)d - %(message)s"
    SQL_DEBUG: bool = False
    BASE_DIR: str = "/opt/sqlbot"
    SCRIPT_DIR: str = f"{BASE_DIR}/scripts"
    UPLOAD_DIR: str = "/opt/sqlbot/data/file"
    SQLBOT_KEY_EXPIRED: int = 100  # License key expiration timestamp, 0 means no expiration
    
    SQLBOT_DOC_ENABLED: bool = True

    @computed_field  # type: ignore[prop-decorator]
    @property
    def SQLALCHEMY_DATABASE_URI(self) -> PostgresDsn | str:
        if self.SQLBOT_DB_URL:
            return self.SQLBOT_DB_URL
        # return MultiHostUrl.build(
        #     scheme="postgresql+psycopg",
        #     username=urllib.parse.quote(self.POSTGRES_USER),
        #     password=urllib.parse.quote(self.POSTGRES_PASSWORD),
        #     host=self.POSTGRES_SERVER,
        #     port=self.POSTGRES_PORT,
        #     path=self.POSTGRES_DB,
        # )
        return f"postgresql+psycopg://{urllib.parse.quote(self.POSTGRES_USER)}:{urllib.parse.quote(self.POSTGRES_PASSWORD)}@{self.POSTGRES_SERVER}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"

    MCP_IMAGE_PATH: str = '/opt/sqlbot/images'
    EXCEL_PATH: str = '/opt/sqlbot/data/excel'
    MCP_IMAGE_HOST: str = 'http://localhost:3000'
    SERVER_IMAGE_HOST: str = 'http://YOUR_SERVE_IP:MCP_PORT/images/'
    SERVER_IMAGE_TIMEOUT: int = 15

    LOCAL_MODEL_PATH: str = '/opt/sqlbot/models'
    DEFAULT_EMBEDDING_MODEL: str = 'shibing624/text2vec-base-chinese'
    EMBEDDING_PROVIDER: str = 'api'  # 'local' or 'api'
    EMBEDDING_API_BASE_URL: str = ''   # e.g. http://192.168.51.250:1999/v1
    EMBEDDING_API_KEY: str = 'not-needed'
    EMBEDDING_API_MODEL: str = ''      # e.g. BAAI/bge-m3
    EMBEDDING_ENABLED: bool = True
    EMBEDDING_DEFAULT_SIMILARITY: float = 0.4
    EMBEDDING_TERMINOLOGY_SIMILARITY: float = EMBEDDING_DEFAULT_SIMILARITY
    EMBEDDING_DATA_TRAINING_SIMILARITY: float = EMBEDDING_DEFAULT_SIMILARITY
    EMBEDDING_DEFAULT_TOP_COUNT: int = 5
    EMBEDDING_TERMINOLOGY_TOP_COUNT: int = EMBEDDING_DEFAULT_TOP_COUNT
    EMBEDDING_DATA_TRAINING_TOP_COUNT: int = EMBEDDING_DEFAULT_TOP_COUNT

    # 是否启用SQL查询行数限制，默认值，可被参数配置覆盖
    GENERATE_SQL_QUERY_LIMIT_ENABLED: bool = True
    GENERATE_SQL_QUERY_HISTORY_ROUND_COUNT: int = 5
    # Trần ký tự cho MỘT câu trả lời của LLM khi đưa vào lịch sử hội thoại. Model đôi khi lặp vô
    # hạn phần suy luận và xuất hàng chục nghìn ký tự; nhét nguyên vào lịch sử là lượt sau vỡ
    # context window rồi hỏng vĩnh viễn vì message độc đã nằm trong chuỗi log.
    LLM_SQL_HISTORY_ANSWER_MAX_CHARS: int = 4000
    # Trần ký tự cho TOÀN BỘ phần lịch sử phát lại vào prompt sinh SQL. Chặn ở đây thay vì tin vào
    # số lượt: một lượt hỏi có thể to bất thường, mà m-schema trong system prompt đã ăn sẵn ~24K.
    LLM_SQL_HISTORY_TOTAL_MAX_CHARS: int = 24000
    # Chỉ giữ khối JSON của pha sinh SQL khi đưa câu trả lời model vào lịch sử, bỏ phần văn xuôi
    # suy luận. Vì đã tắt thinking, văn xuôi rơi hết vào `content` và mang theo cả lời tự kiểm điểm
    # về lần thử hỏng sang các lượt sau — nghi là nguồn nhiễu ngữ cảnh. JSON vẫn chứa nguyên câu SQL
    # nên các giá trị lọc của lượt trước (năm, giới tính…) không mất. Đây là biến đang được đo bằng
    # bộ test HĐND; đừng đổi mặc định mà không kèm số liệu.
    LLM_SQL_HISTORY_JSON_ONLY: bool = False

    # 安全配置：是否允许元数据查询（SHOW/DESCRIBE/DESC/EXPLAIN）
    # 默认关闭，防止通过元数据查询泄露数据库结构
    SQLBOT_ALLOW_METADATA_QUERIES: bool = False

    PARSE_REASONING_BLOCK_ENABLED: bool = True
    DEFAULT_REASONING_CONTENT_START: str = '<think>'
    DEFAULT_REASONING_CONTENT_END: str = '</think>'

    # Tắt pha thinking của LLM. Model reasoning (Qwen3…) có thể kẹt trong vòng lặp suy luận, đốt
    # hết ngân sách token mà không xuất ra content nào — pipeline nhận chuỗi rỗng rồi chết. Tắt
    # thinking khiến model trả thẳng đáp án, nhanh và ổn định hơn nhiều cho tác vụ sinh SQL.
    # Đặt False nếu muốn bật lại thinking cho model hỗ trợ tốt.
    LLM_DISABLE_THINKING: bool = True
    # Payload tiêm vào extra_body để tắt thinking. Mặc định theo chuẩn vLLM/SGLang
    # (chat_template_kwargs.enable_thinking). API kiểu DashScope dùng thẳng {"enable_thinking": false}
    # — đổi biến môi trường này thay vì sửa code khi đổi nhà cung cấp.
    LLM_DISABLE_THINKING_EXTRA_BODY: str = '{"chat_template_kwargs": {"enable_thinking": false}}'

    # Số lần sinh lại SQL trong CÙNG một lượt hỏi khi pha SQL hỏng (LLM từ chối, JSON sai, không
    # trích được tên bảng, SQL chạy lỗi). Lỗi phần lớn là ngẫu nhiên nên thử lại một lần cứu được
    # đa số ca; đặt 0 để tắt. Mỗi lần thử tốn thêm một vòng gọi LLM nên đừng để cao.
    LLM_SQL_MAX_RETRY: int = 1
    # Khi pha SQL hỏng hẳn (hết lượt retry), vẫn cho LLM answer trả lời bằng lời dựa trên lịch sử
    # hội thoại thay vì trả về event `error` cụt lủn. Người dùng hỏi tiếp dạng "trong đó cái nào
    # lớn nhất" thường vẫn trả lời được từ dữ liệu lượt trước mà không cần chạy SQL mới.
    LLM_ANSWER_ON_FAILURE: bool = True
    # Số lượt hỏi-đáp cũ đưa vào prompt fallback. Mỗi lượt kèm câu hỏi, SQL và một mẩu dữ liệu nên
    # tốn token nhanh — 3 lượt là đủ cho các câu hỏi tham chiếu ngược ("trong đó", "vừa rồi").
    LLM_ANSWER_FALLBACK_ROUNDS: int = 3
    # Số dòng dữ liệu tối đa của MỖI lượt cũ đưa vào prompt fallback.
    LLM_ANSWER_FALLBACK_ROWS: int = 20
    # Đưa lịch sử hỏi-đáp vào cả nhánh answer BÌNH THƯỜNG (lượt có dữ liệu mới), không chỉ nhánh
    # fallback. Không dùng chung LLM_ANSWER_FALLBACK_* vì hai nhánh có ngân sách token khác hẳn
    # nhau — nhánh này còn phải chứa `<data>` của chính lượt hiện tại — và vì cần bật/tắt độc lập
    # để đo A/B bằng scripts/eval_text2sql.
    LLM_ANSWER_HISTORY_ENABLED: bool = True
    LLM_ANSWER_HISTORY_ROUNDS: int = 3
    LLM_ANSWER_HISTORY_ROWS: int = 20

    # Chạy pha sinh biểu đồ sau pha answer trên `POST /chat/question`. Thực chất là chọn `finish_step`
    # mặc định của endpoint đó: True -> GENERATE_CHART, False -> GENERATE_ANSWER. Không đụng tới nhánh
    # MCP (tự truyền QUERY_DATA) lẫn nhánh hạ cấp khi pha SQL hỏng hẳn (không có dữ liệu thì biểu đồ
    # vô nghĩa, `run_task` bỏ qua bất kể biến này).
    #
    # Cái giá khi bật: thêm MỘT lượt gọi LLM cho mỗi câu hỏi (2 -> 3). Lượt này chạy song song với pha
    # answer nên độ trễ tới `finish` tăng ít hơn nhiều so với chi phí token. Về hợp đồng stream thì
    # chỉ thêm hai event mốc (`info` + `chart`), và pha biểu đồ hỏng KHÔNG giết lượt hỏi — client
    # vẫn nhận đủ `answer` + `finish`. Xem API_SPEC.md §6.8.
    GENERATE_CHART_ENABLED: bool = True

    # --- Gợi ý câu hỏi tiếp theo ngay trong POST /chat/question ---
    # Sinh gợi ý ngay trong lượt hỏi thay vì bắt client gọi thêm `POST /chat/recommend_questions`.
    # Chạy SONG SONG với pha answer/chart nên gần như không cộng thêm độ trễ tới `finish`, cái giá
    # là thêm MỘT lượt gọi LLM cho mỗi câu hỏi. Chỉ áp dụng cho nhánh in_chat (SSE của UI); nhánh
    # MCP và các `finish_step` dừng sớm (<= QUERY_DATA) không đụng tới.
    #
    # Khác endpoint rời ở hai chỗ, và đó là lý do tồn tại của nó: câu hỏi cũ đưa vào prompt chỉ lấy
    # của CHÍNH user đang hỏi (endpoint rời lấy 20 câu gần nhất của mọi user chung datasource), và
    # kết quả chỉ ghi vào `chat_record` chứ không bao giờ ghi đè gợi ý cấp hội thoại.
    CHAT_INLINE_RECOMMEND_ENABLED: bool = True
    # Số câu gợi ý tối đa. Đây là trần đưa vào prompt, LLM có thể trả ít hơn.
    CHAT_INLINE_RECOMMEND_COUNT: int = 2
    # Số câu hỏi cũ CỦA CHÍNH user đưa vào prompt gợi ý. Chỉ dùng để LLM đoán thói quen hỏi, nhiều
    # hơn không tốt hơn mà chỉ tốn token.
    CHAT_INLINE_RECOMMEND_HISTORY: int = 20

    PG_POOL_SIZE: int = 20
    PG_MAX_OVERFLOW: int = 30
    PG_POOL_RECYCLE: int = 3600
    PG_POOL_PRE_PING: bool = True

    TABLE_EMBEDDING_ENABLED: bool = True
    TABLE_EMBEDDING_COUNT: int = 10
    DS_EMBEDDING_COUNT: int = 10

    ORACLE_CLIENT_PATH: str = '/opt/sqlbot/db_client/oracle_instant_client'

    # --- Luồng import excel bất đồng bộ (POST /datasource/createFromExcelAsync) ---
    # Pool riêng, KHÔNG dùng chung pool 200 thread của embedding: mỗi job nạp cả dataframe vào RAM
    # nên số thread phải nhỏ, còn embedding thì ngược lại.
    EXCEL_IMPORT_WORKERS: int = 4
    # Chu kỳ worker cập nhật heartbeat, và ngưỡng coi job là đã chết. Ngưỡng đặt bằng vài chu kỳ
    # heartbeat chứ không theo độ dài job — đó là điểm khác biệt so với việc chỉ nhìn started_at.
    EXCEL_IMPORT_HEARTBEAT_SECONDS: int = 30
    EXCEL_IMPORT_STALE_SECONDS: int = 180
    # Tổng thời gian chờ advisory lock trong pha đồng bộ, gom từ nhiều lần thử-rồi-ngủ (route không
    # được đứng chờ trong một lời gọi DB đồng bộ — xem ``try_acquire_xact_lock``). Hết hạn thì trả
    # lỗi thay vì xếp hàng vô hạn.
    EXCEL_IMPORT_LOCK_TIMEOUT_MS: int = 5000
    # Chu kỳ vòng quét phục hồi: nhặt job mồ côi, kết liễu job chết, gỡ callback kẹt, dọn file tạm.
    EXCEL_IMPORT_RECOVERY_SECONDS: int = 60
    # File tạm quá hạn này mà không job nào đang dùng thì xóa. Đặt dài vì thư mục này dùng chung với
    # luồng /parseExcel → /importToDb của giao diện web, nơi người dùng có thể để giữa chừng khá lâu.
    EXCEL_IMPORT_TEMP_TTL_HOURS: int = 24

    # --- Callback về hệ ngoài (outbox) ---
    # Để rỗng là tắt hẳn việc gửi callback; job vẫn chạy và vẫn ghi trạng thái.
    AI_CALLBACK_URL: str = ''
    # Mã sự kiện đối tác cấp RIÊNG cho việc báo kết quả nạp excel, không dùng lại cho loại bản tin
    # nào khác — nên phong bì không cần thêm trường phân biệt sự kiện.
    AI_CALLBACK_ACTION_TYPE: int = 999
    # Thư viện HTTP mặc định KHÔNG có timeout; thiếu giá trị này thì một connection treo giữ thread
    # gửi vĩnh viễn và ta quay lại đúng bài toán cạn pool mà outbox sinh ra để tránh.
    AI_CALLBACK_TIMEOUT: int = 15
    AI_CALLBACK_MAX_ATTEMPTS: int = 8
    AI_CALLBACK_POLL_SECONDS: int = 10
    AI_CALLBACK_WORKERS: int = 4
    AI_CALLBACK_BATCH_SIZE: int = 20
    # Giãn cách thử lại: nhân đôi sau mỗi lần hỏng, chặn trên để không bao giờ ngủ quên hàng giờ.
    AI_CALLBACK_BACKOFF_BASE_SECONDS: int = 10
    AI_CALLBACK_BACKOFF_CAP_SECONDS: int = 1800
    # Header xác thực gửi kèm, dạng "Tên: giá trị". Đối tác đã xác nhận cổng nhận callback KHÔNG cần
    # xác thực, nên mặc định rỗng; giữ lại biến để sau này họ đổi ý thì không phải sửa code.
    AI_CALLBACK_AUTH_HEADER: str = ''

    # --- Tải file nguồn từ presigned URL: phần DÙNG CHUNG cho mọi luồng tải ---
    # Ba biến FILE_DOWNLOAD_* dưới đây chi phối CẢ HAI tính năng đang tải file từ presigned URL:
    # import excel bất đồng bộ (POST /datasource/createFromExcelAsync) và tài liệu .docx đính kèm
    # câu hỏi (`fileUrls` của POST /chat/question). Sửa ở đây là sửa cho cả hai — đó là lý do chúng
    # không còn mang tiền tố EXCEL_.
    #
    # Mỗi biến vẫn nhận TÊN CŨ làm alias để .env của các bản đã deploy không chết lặng: tên không
    # khớp field nào thì `extra="ignore"` nuốt luôn, allowlist thành rỗng, và cả hai tính năng tắt
    # ngóm mà không có lấy một dòng log. Tên mới được ưu tiên khi khai cả hai.
    #
    # Danh sách host được phép tải về, ngăn cách bằng dấu phẩy, có thể kèm cổng. RỖNG LÀ CẤM TẤT
    # CẢ: đây là lớp chống SSRF — server tự đi gọi một URL do client đưa, từ bên trong mạng nội bộ
    # — nên nó phải hỏng theo hướng đóng. Mặc định mở là lớp bảo vệ coi như không tồn tại.
    FILE_DOWNLOAD_ALLOWED_HOSTS: str = Field(
        default='',
        validation_alias=AliasChoices('FILE_DOWNLOAD_ALLOWED_HOSTS',
                                      'EXCEL_DOWNLOAD_ALLOWED_HOSTS'))
    FILE_DOWNLOAD_CONNECT_TIMEOUT: int = Field(
        default=5,
        validation_alias=AliasChoices('FILE_DOWNLOAD_CONNECT_TIMEOUT',
                                      'EXCEL_DOWNLOAD_CONNECT_TIMEOUT'))
    # Timeout cho MỖI lần đọc, không phải cho cả lượt tải.
    FILE_DOWNLOAD_READ_TIMEOUT: int = Field(
        default=30,
        validation_alias=AliasChoices('FILE_DOWNLOAD_READ_TIMEOUT',
                                      'EXCEL_DOWNLOAD_READ_TIMEOUT'))

    # --- Tải file nguồn: phần CHỈ RIÊNG luồng import excel bất đồng bộ ---
    # Giữ tiền tố EXCEL_ vì luồng docx không đụng tới biến nào dưới đây: nó tải thẳng vào RAM ngay
    # trong request nên có trần dung lượng, trần thời gian và hạn ký riêng (nhóm CHAT_DOC_*), lại
    # không thăm dò và không tự tải lại.
    #
    # Trần dung lượng file tải về. Ép ở HAI chỗ: dung lượng nguồn khai lúc thăm dò, và số byte đếm
    # được trong lúc stream — lời khai của nguồn không phải sự thật.
    EXCEL_DOWNLOAD_MAX_MB: int = 100
    # Trần tổng thời gian một lượt tải. Bắt buộc phải có riêng: timeout đọc chỉ bắt được kết nối
    # đứng im, nó bất lực trước nguồn nhỏ giọt đều đặn vài byte mỗi giây — thứ giữ một worker vô hạn.
    EXCEL_DOWNLOAD_TOTAL_TIMEOUT: int = 1800
    # Số lần tải LẠI khi hỏng. Chỉ áp dụng cho lỗi mạng và 5xx; 4xx không bao giờ thử lại vì chữ ký
    # sai hay object không tồn tại thì thử bao nhiêu lần cũng vậy, chỉ đẩy job tới gần hạn ký hơn.
    EXCEL_DOWNLOAD_RETRIES: int = 2
    # Hạn ký còn lại tối thiểu để nhận URL. Job có thể nằm xếp hàng sau các worker đang bận rồi mới
    # tới lượt tải, nên URL sắp hết hạn gần như chắc chắn hỏng ở worker — từ chối sớm còn hơn.
    EXCEL_DOWNLOAD_MIN_TTL_SECONDS: int = 300
    # Thăm dò 1 byte (GET kèm Range) ngay ở pha đồng bộ, để bắt sớm chữ ký sai, object không tồn
    # tại và file vượt trần mà không phải truyền cả file. Tắt đi thì các lỗi đó lùi xuống callback.
    EXCEL_DOWNLOAD_PROBE_ENABLED: bool = True
    EXCEL_DOWNLOAD_PROBE_TIMEOUT: int = 3

    # --- Tài liệu .docx đính kèm lượt hỏi chat (`fileUrls` của POST /chat/question) ---
    # Allowlist host và hai timeout kết nối/đọc lấy từ nhóm FILE_DOWNLOAD_* phía trên (cùng một
    # MinIO, cùng một lớp chống SSRF); rỗng vẫn là cấm tất. Dưới đây chỉ là phần riêng của docx.
    # Số file tối đa MỘT lượt hỏi được đính kèm. Đây là trần an ninh chứ không phải tiện nghi: URL
    # do client đưa mà server tự đi tải, nên không có trần thì một request là một lượt khuếch đại
    # SSRF tùy ý. Đếm trên đúng mảng client gửi (trước khi bỏ trùng) để hợp đồng nói được thành một
    # câu client tự kiểm được. Quá trần thì HTTP 400 mã TOO_MANY_FILES, chưa URL nào bị chạm tới.
    CHAT_DOC_MAX_FILES: int = 5
    # Số thread tối đa dành cho việc tải tài liệu đính kèm, dùng chung cho toàn tiến trình. Phải là
    # pool RIÊNG chứ không phải pool mặc định của `asyncio.to_thread`: pool mặc định (~32 thread)
    # đang gánh gần như mọi lời gọi DB đồng bộ của app, mà một lượt tải docx giữ thread tới
    # CHAT_DOC_DOWNLOAD_TIMEOUT giây. Vài request đính file gặp MinIO chậm là đủ làm mọi endpoint
    # khác đứng hình — triệu chứng sẽ là "backend chết", không phải "đính kèm chậm". Pool riêng đổi
    # kiểu hỏng đó thành "các request đính kèm xếp hàng chờ nhau", thứ khoanh vùng được.
    CHAT_DOC_DOWNLOAD_WORKERS: int = 16
    # Trần dung lượng file tải về, cho MỖI file. Nhỏ hơn hẳn trần excel có chủ ý: docx là zip, trần
    # byte này đồng thời là chốt đầu tiên chống zip bomb (chốt thứ hai là trần ký tự lúc trích).
    CHAT_DOC_MAX_MB: int = 15
    # Trần tổng thời gian tải, cho MỖI file. Tải chạy NGAY TRONG request /chat/question (client
    # đang chờ SSE mở) nên tính bằng chục giây chứ không phải chục phút như luồng excel, và không
    # tự tải lại. Các file trong cùng một lượt được tải SONG SONG nên trần này không cộng dồn.
    CHAT_DOC_DOWNLOAD_TIMEOUT: int = 30
    # Trần ký tự LÚC TRÍCH, cho MỖI file — cũng là trần của bản lưu trong bảng chat_attachment.
    CHAT_DOC_EXTRACT_MAX_CHARS: int = 200_000
    # Trần ký tự của khối tài liệu trong prompt của LƯỢT ĐÍNH FILE (cả pha SQL lẫn pha answer).
    # Là trần TỔNG cho tất cả file của lượt, không phải trần mỗi file: ngân sách context là của cả
    # prompt, nên nó không được phép nở ra theo số file client gửi. Chia đều cho các file, phần một
    # file ngắn không dùng hết thì trả lại cho file dài (xem `split_char_budget`).
    # Chưa đo bằng eval harness — đặt theo ngân sách context, đổi thì nên chạy lại harness.
    CHAT_DOC_PROMPT_MAX_CHARS: int = 30_000
    # Trần ký tự của khối tài liệu khi nó xuất hiện lại trong <history> của pha answer các lượt
    # sau. Cũng là trần TỔNG, tính cho MỖI LƯỢT trong cửa sổ lịch sử. Chặt hơn trần trên vì lịch sử
    # nhiều lượt còn phải nhường chỗ cho <data> của lượt hiện tại.
    CHAT_DOC_HISTORY_MAX_CHARS: int = 10_000

    @field_validator('SQL_DEBUG',
                     'EMBEDDING_ENABLED',
                     'GENERATE_SQL_QUERY_LIMIT_ENABLED',
                     'PARSE_REASONING_BLOCK_ENABLED',
                     'PG_POOL_PRE_PING',
                     'TABLE_EMBEDDING_ENABLED',
                     'LLM_DISABLE_THINKING',
                     'LLM_ANSWER_ON_FAILURE',
                     'LLM_ANSWER_HISTORY_ENABLED',
                     'LLM_SQL_HISTORY_JSON_ONLY',
                     'GENERATE_CHART_ENABLED',
                     'CHAT_INLINE_RECOMMEND_ENABLED',
                     'EXCEL_DOWNLOAD_PROBE_ENABLED',
                     mode='before')
    @classmethod
    def lowercase_bool(cls, v: Any) -> Any:
        """Ép chuỗi trong .env về bool cho các biến khai ở decorator phía trên.

        Chạy ở mode='before' nên nhận nguyên văn chuỗi người vận hành gõ. Thêm biến bool mới thì
        nhớ khai tên nó vào danh sách trên, nếu không `"False"` viết hoa có thể lọt qua thành giá
        trị khác ý định.
        """
        if isinstance(v, str):
            v_lower = v.lower().strip()
            if v_lower == 'true':
                return True
            elif v_lower == 'false':
                return False
        return v


settings = Settings()  # type: ignore
