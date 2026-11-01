import base64
import hashlib
import inspect
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
from urllib.parse import urlparse

from fastapi import Request
from common.core.config import settings
from typing import Optional

import jwt
import orjson
from jwt.exceptions import InvalidTokenError

from common.core import security
from common.telemetry import configure_logging


def generate_password_reset_token(email: str) -> str:
    delta = timedelta(hours=settings.EMAIL_RESET_TOKEN_EXPIRE_HOURS)
    now = datetime.now(timezone.utc)
    expires = now + delta
    exp = expires.timestamp()
    encoded_jwt = jwt.encode(
        {"exp": exp, "nbf": now, "sub": email},
        settings.SECRET_KEY,
        algorithm=security.ALGORITHM,
    )
    return encoded_jwt


def verify_password_reset_token(token: str) -> str | None:
    try:
        decoded_token = jwt.decode(
            token, settings.SECRET_KEY, algorithms=[security.ALGORITHM]
        )
        return str(decoded_token["sub"])
    except InvalidTokenError:
        return None


def deepcopy_ignore_extra(src, dest):
    import copy
    for attr in vars(src):
        if hasattr(dest, attr):
            src_value = getattr(src, attr)
            dest_value = copy.deepcopy(src_value)  # deep copy
            setattr(dest, attr, dest_value)
    return dest


def scan_json_candidates(text) -> list:
    """Quét text, trả về MỌI chuỗi con là JSON hợp lệ (cân bằng ngoặc), theo thứ tự xuất hiện.

    Quét thủ công thay vì regex vì JSON lồng nhau nhiều tầng không biểu diễn được bằng regex.
    Mỗi ứng viên đều được orjson.loads() để xác thực, nên chuỗi trả về chắc chắn parse được.

    Tách riêng khỏi extract_nested_json để bên gọi chọn được ứng viên theo schema thay vì
    buộc phải lấy cái đầu tiên — xem extract_json_object.
    """
    stack = []
    start_index = -1
    results = []

    for i, char in enumerate(text):
        if char in '{[':
            if not stack:  # 记录起始位置
                start_index = i
            stack.append(char)
        elif char in '}]':
            if stack and ((char == '}' and stack[-1] == '{') or (char == ']' and stack[-1] == '[')):
                stack.pop()
                if not stack:  # 栈空时截取完整JSON
                    json_str = text[start_index:i + 1]
                    try:
                        orjson.loads(json_str)  # 验证有效性
                        results.append(json_str)
                    except:
                        pass
            else:
                stack = []  # 括号不匹配则重置
    return results


def extract_nested_json(text):
    """Trả về chuỗi JSON hợp lệ ĐẦU TIÊN tìm được trong text, hoặc None.

    Giữ nguyên hành vi cũ cho các chỗ gọi chưa cần lọc theo schema. Với đáp án của LLM thì nên
    dùng extract_json_object: khi model viết thêm văn xuôi (hoặc reasoning tràn sang content),
    mẩu JSON đầu tiên thường là bản nháp giữa chừng chứ không phải đáp án cuối.
    """
    results = scan_json_candidates(text)
    if len(results) > 0 and results[0]:
        return results[0]
    return None


def extract_json_object(text, required_keys=None, prefer_last: bool = True) -> Optional[str]:
    """Trích JSON đáp án của LLM, chọn theo schema thay vì theo vị trí.

    Lý do tồn tại: LLM hay kèm văn xuôi quanh JSON, và khi tắt thinking thì phần suy luận có thể
    rơi thẳng vào content. Trong đoạn suy luận đó thường có sẵn JSON nháp hợp lệ (vd mảng tên bảng
    ["ten_bang"]), nên lấy "cái đầu tiên" sẽ bốc nhầm bản nháp rồi nổ khi truy cập khóa.

    required_keys: tập khóa bắt buộc — chỉ nhận dict chứa đủ các khóa này, nhờ đó loại được mảng
    và các object lạc. prefer_last=True vì đáp án chính thức luôn nằm sau phần suy luận.

    Trả về chuỗi JSON (chưa parse) để bên gọi tự orjson.loads, hoặc None nếu không có ứng viên nào
    khớp. Không có ứng viên khớp thì KHÔNG lùi về ứng viên bất kỳ — thà báo lỗi rõ còn hơn chạy
    tiếp với dữ liệu sai.
    """
    candidates = scan_json_candidates(text)
    if not candidates:
        return None

    if required_keys:
        matched = []
        for candidate in candidates:
            try:
                parsed = orjson.loads(candidate)
            except Exception:
                continue
            if isinstance(parsed, dict) and all(key in parsed for key in required_keys):
                matched.append(candidate)
        candidates = matched

    if not candidates:
        return None
    return candidates[-1] if prefer_last else candidates[0]

def string_to_numeric_hash(text: str, bits: Optional[int] = 64) -> int:
    hash_bytes = hashlib.sha256(text.encode()).digest()
    hash_num = int.from_bytes(hash_bytes, byteorder='big')
    max_bigint = 2**63 - 1
    return hash_num % max_bigint


class _StripUrlQueryFilter(logging.Filter):
    """Cắt query string khỏi URL trong dòng log ``HTTP Request: GET <url>`` của httpx.

    httpx tự log nguyên URL ở mức INFO. Với presigned URL (import excel, tài liệu docx), chữ ký nằm
    trong query string và là credential, nên không được ghi ra log. Chỉ cắt query, giữ host và path
    để log vẫn dùng được khi gỡ lỗi.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(_strip_url_query(arg) for arg in record.args)
        return True


def _strip_url_query(arg):
    """Trả về URL đã bỏ phần sau ``?``; tham số không phải URL thì giữ nguyên."""
    text = str(arg)
    if "://" in text and "?" in text:
        return text.split("?", 1)[0] + "?<redacted>"
    return arg


def setup_logging():
    """Cấu hình log theo chuẩn chung: một dòng JSON mỗi sự kiện ra stdout, agent của cluster đọc và gửi đi.

    Không còn file log xoay vòng trong container (mất khi pod khởi động lại, agent không đọc). Chỉ logger của
    ứng dụng theo `LOG_LEVEL`; thư viện bên thứ ba ở WARNING, riêng httpx/httpcore vì chúng log URL (presigned URL
    mang chữ ký trong query string). Bộ lọc cắt query vẫn gắn cho httpx phòng khi ai đó hạ mức của nó.
    """
    configure_logging(
        "sqlbot",
        level=settings.LOG_LEVEL,
        logger_names=("apps", "common", "main", "__main__"),
        quiet_loggers=("httpx", "httpcore"),
    )
    logging.getLogger("httpx").addFilter(_StripUrlQueryFilter())

    # SQL: chỉ bật khi gỡ lỗi; lời gọi SQL đi theo handler chung, cùng định dạng.
    if settings.LOG_LEVEL == "DEBUG" and settings.SQL_DEBUG:
        logging.getLogger('sqlalchemy.engine').setLevel(logging.DEBUG)


setup_logging()


class CallerLogger(logging.Logger):
    def __init__(self, logger: logging.Logger):
        self.logger = logger
        super().__init__(logger.name, logger.level)

    def _log(self, level, msg, args, exc_info=None, extra=None, stacklevel=3):
        if self.logger.isEnabledFor(level):
            self.logger._log(level, msg, args, exc_info=exc_info, extra=extra, stacklevel=stacklevel)

class SQLBotLogUtil:
    
    @staticmethod
    def _get_logger() -> logging.Logger:
        frame = inspect.currentframe()
        try:
            caller_frame = frame.f_back.f_back
            module_name = caller_frame.f_globals.get('__name__', '__main__')
            return CallerLogger(logging.getLogger(module_name))
        finally:
            del frame
        

    @staticmethod
    def debug(msg: str, *args, **kwargs):
        logger = SQLBotLogUtil._get_logger()
        if logger.isEnabledFor(logging.DEBUG):
            logger._log(logging.DEBUG, msg, args, **kwargs)

    @staticmethod
    def info(msg: str, *args, **kwargs):
        logger = SQLBotLogUtil._get_logger()
        if logger.isEnabledFor(logging.INFO):
            logger._log(logging.INFO, msg, args, **kwargs)

    @staticmethod
    def warning(msg: str, *args, **kwargs):
        logger = SQLBotLogUtil._get_logger()
        if logger.isEnabledFor(logging.WARNING):
            logger._log(logging.WARNING, msg, args, **kwargs)

    @staticmethod
    def error(msg: str, *args, exc_info: Optional[bool] = None, **kwargs):
        logger = SQLBotLogUtil._get_logger()
        if logger.isEnabledFor(logging.ERROR):
            logger._log(
                logging.ERROR, 
                msg, 
                args, 
                exc_info=exc_info if exc_info is not None else True,
                **kwargs
            )

    @staticmethod
    def exception(msg: str, *args, **kwargs):
        logger = SQLBotLogUtil._get_logger()
        if logger.isEnabledFor(logging.ERROR):
            logger._log(logging.ERROR, msg, args, exc_info=True, **kwargs)

    @staticmethod
    def critical(msg: str, *args, **kwargs):
        logger = SQLBotLogUtil._get_logger()
        if logger.isEnabledFor(logging.CRITICAL):
            logger._log(logging.CRITICAL, msg, args, **kwargs)
            
def prepare_for_orjson(data):
    if not data:
        return data
    if isinstance(data, bytes):
        return base64.b64encode(data).decode('utf-8')
    elif isinstance(data, dict):
        return {k: prepare_for_orjson(v) for k, v in data.items()}
    elif isinstance(data, (list, tuple)):
        return [prepare_for_orjson(item) for item in data]
    else:
        return data
        
    
def prepare_model_arg(origin_arg: str):
    if not isinstance(origin_arg, str):
        return origin_arg
    if not origin_arg.strip()[0] in {'{', '['}:
        return origin_arg
    try:
        return json.loads(origin_arg)
    except:
        return origin_arg
    
def get_origin_from_referer(request: Request):
    referer = request.headers.get("referer")
    if not referer:
        return None
    
    try:
        parsed = urlparse(referer)
        if not parsed.scheme or not parsed.hostname:
            return None
        port = parsed.port
        if port:
            if (parsed.scheme == "http" and port != 80) or \
               (parsed.scheme == "https" and port != 443):
                return f"{parsed.scheme}://{parsed.hostname}:{port}"
        
        return f"{parsed.scheme}://{parsed.hostname}"
    except Exception as e:
        SQLBotLogUtil.error(f"Could not parse the Referer header: {e}")
        return referer

def origin_match_domain(origin: str, domain: str) -> bool:
    if not origin or not domain:
        return False
    origin_normalized = origin.rstrip('/')
    
    for d in re.split(r'[,;]', domain):
        if d.strip().rstrip('/') == origin_normalized:
            return True
    
    return False

def get_domain_list(domain: str) -> list[str]:
    domains = []
    if not domain:
        return domains
    for d in re.split(r'[,;]', domain):
        d_clean = d.strip().rstrip('/')
        if d_clean:
            domains.append(d_clean)
    return domains
    

def equals_ignore_case(str1: str, *args: str) -> bool:
    if str1 is None:
        return None in args
    for arg in args:
        if arg is None:
            continue
        if str1.casefold() == arg.casefold():
            return True
    return False