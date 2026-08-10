"""
Helper dùng chung cho demo so sánh streaming giữa 2 endpoint chat của SQLBot.

Khác với harness eval_text2sql/ (gọi pipeline in-process), demo này gọi **qua HTTP thật** vì mục tiêu
là nhìn thấy đúng những byte mà server đẩy ra dây — in-process sẽ mất luôn phần đóng gói SSE.

Cách lấy JWT: dùng POST /mcp/access_token thay vì /login/access-token, vì endpoint login chính chạy
username/password qua sqlbot_decrypt (mã hóa phía frontend, hàm giải mã nằm trong package biên dịch
sẵn sqlbot_xpack) nên không gọi thẳng bằng script được. JWT hai đường sinh ra là như nhau.

Chạy (từ thư mục backend/):
    .venv/bin/python scripts/chat_stream_demo/01_chat_standard.py
    .venv/bin/python scripts/chat_stream_demo/02_chat_mcp.py
"""

import json
import os
import time
from pathlib import Path
from typing import Any, Iterator, Optional

import requests

BASE_URL = os.getenv("SQLBOT_BASE_URL", "http://localhost:8001/api/v1")
USERNAME = os.getenv("SQLBOT_USER", "admin")
PASSWORD = os.getenv("SQLBOT_PWD", "SQLBot@123456")

# Datasource "DataLaoCai" — thu/chi ngân sách tỉnh Lào Cai (core_datasource.id trong DB metadata).
DATASOURCE_ID = int(os.getenv("SQLBOT_DS_ID", "5"))
QUESTION = os.getenv(
    "SQLBOT_QUESTION",
    "Tổng số thu ngân sách theo từng nhóm nguồn thu",
)

OUTPUT_DIR = Path(__file__).resolve().parent / "output"

# Số giây stream được phép "đứng im" trước khi client bỏ cuộc. Không phải tổng thời gian chạy:
# requests tính read-timeout cho từng lần đọc chunk, nên pipeline chạy lâu vẫn ổn miễn là còn đẩy
# dữ liệu ra đều. Cần có vì pha sinh chart đôi khi treo hẳn (LLM rơi vào vòng lặp lặp từ).
READ_TIMEOUT = int(os.getenv("SQLBOT_READ_TIMEOUT", "60"))
HTTP_TIMEOUT = (10, READ_TIMEOUT)


def unwrap(resp: requests.Response) -> Any:
    """Bóc lớp vỏ {code, data, msg} mà response_middleware bọc quanh mọi response JSON.

    Không phải endpoint nào cũng bị bọc (response streaming đi thẳng), nên hàm trả về nguyên
    payload nếu không thấy cấu trúc vỏ — tránh giả định sai làm mất dữ liệu.
    """
    resp.raise_for_status()
    payload = resp.json()
    if isinstance(payload, dict) and set(payload.keys()) >= {"code", "data"}:
        return payload["data"]
    return payload


def get_token() -> str:
    """Lấy JWT qua /mcp/access_token (nhận mật khẩu plaintext).

    Token trả về dùng được cho CẢ hai luồng: gắn vào header X-SQLBOT-TOKEN cho API thường, và
    nhét vào body cho API /mcp/* — vì cả hai đều do create_access_token() sinh ra với payload
    giống hệt nhau (id/account/oid/exp).
    """
    resp = requests.post(
        f"{BASE_URL}/mcp/access_token",
        json={"username": USERNAME, "password": PASSWORD},
        timeout=30,
    )
    return unwrap(resp)["access_token"]


def auth_headers(token: str) -> dict:
    """Header xác thực cho các API thường.

    Tên header là X-SQLBOT-TOKEN (settings.TOKEN_KEY) chứ không phải Authorization, và giá trị
    BẮT BUỘC có tiền tố 'Bearer ' — thiếu tiền tố thì middleware trả 'Token schema error!'.
    """
    return {"X-SQLBOT-TOKEN": f"Bearer {token}", "Content-Type": "application/json"}


def stream_lines(resp: requests.Response) -> Iterator[tuple[float, str]]:
    """Đọc response streaming, trả (giây kể từ lúc bắt đầu, dòng thô).

    Mốc thời gian là thứ duy nhất chứng minh dữ liệu về dần chứ không phải về một cục, nên phải
    đo ngay tại vòng lặp đọc. Giữ nguyên cả dòng rỗng vì SSE dùng dòng rỗng làm dấu ngắt event.
    """
    started = time.perf_counter()
    for raw in resp.iter_lines(decode_unicode=True):
        yield time.perf_counter() - started, "" if raw is None else raw


def dump_stream(resp: requests.Response, out_path: Path, title: str) -> list[str]:
    """Ghi toàn bộ stream ra file kèm timestamp, trả về danh sách dòng thô để phân tích tiếp.

    Ghi theo kiểu mở file một lần rồi flush từng dòng: nếu pipeline treo giữa chừng thì phần đã
    nhận vẫn nằm trong file, không mất trắng.

    Bắt riêng ReadTimeout thay vì để nó nổ ra ngoài: pha sinh chart có thể treo (LLM rơi vào vòng
    lặp lặp từ), mà phần stream nhận được trước đó vẫn là dữ liệu hợp lệ cần giữ để so sánh.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    with out_path.open("w", encoding="utf-8") as f:
        f.write(f"# {title}\n")
        f.write(f"# {'-' * 70}\n")
        f.write("# Cột 1 = giây kể từ lúc request được gửi | Cột 2 = dòng thô nhận được\n\n")
        try:
            for elapsed, line in stream_lines(resp):
                lines.append(line)
                f.write(f"[{elapsed:7.3f}s] {line}\n")
                f.flush()
        except requests.exceptions.ReadTimeout:
            note = f"[!] STREAM ĐỨNG IM QUÁ {READ_TIMEOUT}s — dừng đọc (server vẫn có thể đang chạy)"
            f.write(f"\n{note}\n")
            print(f"  {note}")
    return lines


def write_json(obj: Any, out_path: Path) -> None:
    """Ghi object ra file JSON đọc được bằng mắt (indent + giữ nguyên tiếng Việt)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def start_standard_chat(token: str, datasource_id: int) -> int:
    """Tạo hội thoại cho luồng chat thường qua POST /chat/start, trả chat_id.

    Ở luồng này datasource được gắn NGAY vào hội thoại lúc tạo, nên câu hỏi sau đó không cần
    khai lại — khác hẳn luồng MCP (xem 02_chat_mcp.py) nơi datasource_id đi kèm từng câu hỏi.
    """
    resp = requests.post(
        f"{BASE_URL}/chat/start",
        headers=auth_headers(token),
        json={"datasource": datasource_id, "origin": 0},
        timeout=30,
    )
    return unwrap(resp)["id"]


def start_mcp_chat(token: str) -> int:
    """Tạo hội thoại cho luồng MCP qua POST /mcp/mcp_start, trả chat_id.

    mcp_start gọi create_chat(CreateChat(origin=1)) không kèm datasource — đó là lý do
    mcp_question phải nhận datasource_id riêng cho từng câu hỏi.
    """
    resp = requests.post(
        f"{BASE_URL}/mcp/mcp_start",
        json={"token": token},
        timeout=30,
    )
    return unwrap(resp)["chat_id"]


def summarize_sse(lines: list[str]) -> list[dict]:
    """Gom các dòng SSE 'data:{...}' thành danh sách event đã parse, bỏ qua dòng ngắt.

    Chỉ dùng cho luồng chat thường (in_chat=True) — luồng MCP stream ra markdown thuần nên
    parse JSON sẽ hỏng; đó chính là điểm khác biệt mà demo muốn cho thấy.
    """
    events: list[dict] = []
    for line in lines:
        if not line.startswith("data:"):
            continue
        body = line[len("data:"):].strip()
        if not body:
            continue
        try:
            events.append(json.loads(body))
        except json.JSONDecodeError:
            events.append({"_unparsed": body})
    return events


def truncate(value: Optional[str], limit: int = 300) -> str:
    """Rút gọn chuỗi dài khi in ra console để log không bị trôi."""
    if value is None:
        return ""
    text = str(value)
    return text if len(text) <= limit else text[:limit] + f"... (+{len(text) - limit} ký tự)"
