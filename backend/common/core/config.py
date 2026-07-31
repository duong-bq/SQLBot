import secrets
import urllib.parse
from typing import Annotated, Any, Literal

from pydantic import (
    AnyUrl,
    BeforeValidator,
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
    EMBEDDING_PROVIDER: str = 'local'  # 'local' or 'api'
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

    PG_POOL_SIZE: int = 20
    PG_MAX_OVERFLOW: int = 30
    PG_POOL_RECYCLE: int = 3600
    PG_POOL_PRE_PING: bool = True

    TABLE_EMBEDDING_ENABLED: bool = True
    TABLE_EMBEDDING_COUNT: int = 10
    DS_EMBEDDING_COUNT: int = 10

    ORACLE_CLIENT_PATH: str = '/opt/sqlbot/db_client/oracle_instant_client'

    @field_validator('SQL_DEBUG',
                     'EMBEDDING_ENABLED',
                     'GENERATE_SQL_QUERY_LIMIT_ENABLED',
                     'PARSE_REASONING_BLOCK_ENABLED',
                     'PG_POOL_PRE_PING',
                     'TABLE_EMBEDDING_ENABLED',
                     'LLM_DISABLE_THINKING',
                     'LLM_ANSWER_ON_FAILURE',
                     'LLM_SQL_HISTORY_JSON_ONLY',
                     mode='before')
    @classmethod
    def lowercase_bool(cls, v: Any) -> Any:
        """将字符串形式的布尔值转换为Python布尔值"""
        if isinstance(v, str):
            v_lower = v.lower().strip()
            if v_lower == 'true':
                return True
            elif v_lower == 'false':
                return False
        return v


settings = Settings()  # type: ignore
