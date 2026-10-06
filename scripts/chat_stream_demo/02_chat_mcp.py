"""
Demo endpoint chat MCP: POST /api/v1/mcp/mcp_question

Đặc điểm cần thấy ở output:
  - Không dùng header xác thực: route /mcp* nằm trong whitelist nên TokenMiddleware bỏ qua,
    JWT đi trong body cùng chat_id / question / datasource_id / stream / return_img.
  - stream=true  -> SSE nhưng nội dung là MARKDOWN thuần (in_chat=False), không phải JSON theo "type".
  - stream=false -> chạy hết pipeline rồi trả MỘT JSON tổng hợp (record_id/title/sql/data/chart).

Script chạy cả hai chế độ trên cùng một câu hỏi để so sánh trực tiếp.

Ghi ra:
  output/02_mcp_stream.txt   — stream markdown kèm timestamp
  output/02_mcp_nostream.json — JSON tổng hợp khi stream=false
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import requests  # noqa: E402

from _common import (  # noqa: E402
    BASE_URL,
    DATASOURCE_ID,
    HTTP_TIMEOUT,
    OUTPUT_DIR,
    QUESTION,
    dump_stream,
    get_token,
    start_mcp_chat,
    truncate,
    write_json,
)


def build_payload(token: str, chat_id: int, question: str, stream: bool) -> dict:
    """Dựng body cho /mcp/mcp_question.

    return_img=False để bỏ bước gọi image microservice (MCP_IMAGE_HOST) — service đó thường không
    chạy ở môi trường dev và sẽ làm request treo/lỗi, che mất phần khác biệt cần quan sát.
    """
    return {
        "token": token,
        "chat_id": chat_id,
        "question": question,
        "datasource_id": DATASOURCE_ID,
        "stream": stream,
        "lang": "en",
        "return_img": False,
    }


def ask_mcp_stream(token: str, chat_id: int, question: str) -> list[str]:
    """Gọi mcp_question với stream=true và ghi lại stream markdown."""
    resp = requests.post(
        f"{BASE_URL}/mcp/mcp_question",
        json=build_payload(token, chat_id, question, stream=True),
        stream=True,
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    print(f"  Content-Type: {resp.headers.get('content-type')}")
    return dump_stream(
        resp,
        OUTPUT_DIR / "02_mcp_stream.txt",
        f"CHAT MCP (stream=true) — POST /mcp/mcp_question | chat_id={chat_id} | câu hỏi: {question}",
    )


def ask_mcp_json(token: str, chat_id: int, question: str) -> dict:
    """Gọi mcp_question với stream=false, trả về JSON tổng hợp.

    Không dùng unwrap() ở đây: nhánh stream=False trả JSONResponse thẳng từ stream_sql(), không đi
    qua lớp bọc {code,data,msg} như các API thường.
    """
    resp = requests.post(
        f"{BASE_URL}/mcp/mcp_question",
        json=build_payload(token, chat_id, question, stream=False),
        timeout=(10, 600),
    )
    print(f"  HTTP {resp.status_code} | Content-Type: {resp.headers.get('content-type')}")
    return resp.json()


def main() -> None:
    """Chạy câu hỏi qua luồng MCP ở cả hai chế độ stream và non-stream."""
    print("=" * 78)
    print("CHAT MCP (/api/v1/mcp/mcp_question)")
    print("=" * 78)

    token = get_token()
    print(f"  JWT: {token[:40]}... (đi trong BODY, không phải header)")
    print(f"  Câu hỏi: {QUESTION}\n")

    # --- Chế độ 1: stream=true -> markdown ---
    print("--- stream=true (SSE markdown) ---")
    chat_id_stream = start_mcp_chat(token)
    print(f"  chat_id={chat_id_stream} (hội thoại KHÔNG gắn datasource; ds đi theo câu hỏi)")
    lines = ask_mcp_stream(token, chat_id_stream, QUESTION)
    print(f"  Tổng {len(lines)} dòng thô")
    print(f"  Đã ghi: {OUTPUT_DIR / '02_mcp_stream.txt'}")

    # --- Chế độ 2: stream=false -> một JSON tổng hợp ---
    # Dùng chat_id mới để câu hỏi thứ hai không bị coi là lượt tiếp theo của cùng hội thoại
    # (lịch sử multi-turn sẽ được nhồi vào prompt và làm kết quả lệch khỏi lần chạy đầu).
    print("\n--- stream=false (một JSON tổng hợp) ---")
    chat_id_json = start_mcp_chat(token)
    print(f"  chat_id={chat_id_json}")
    result = ask_mcp_json(token, chat_id_json, QUESTION)
    write_json(result, OUTPUT_DIR / "02_mcp_nostream.json")

    print(f"  Các khóa trong JSON: {list(result.keys())}")
    for key in ("success", "record_id", "title", "sql"):
        if key in result:
            print(f"    {key}: {truncate(result[key], 120)}")
    if isinstance(result.get("data"), list):
        print(f"    data: {len(result['data'])} dòng")
    print(f"  Đã ghi: {OUTPUT_DIR / '02_mcp_nostream.json'}")


if __name__ == "__main__":
    main()
