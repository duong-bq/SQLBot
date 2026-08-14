"""
Demo endpoint chat THƯỜNG: POST /api/v1/chat/question

Đặc điểm cần thấy ở output:
  - Xác thực bằng header X-SQLBOT-TOKEN (middleware xử lý), body chỉ có chat_id + question.
  - Response luôn là SSE (text/event-stream), mỗi event là JSON có trường "type"
    (id / question / datasource / sql-result / sql / sql-data / answer-result / chart / finish).
  - Không có JSON tổng hợp cuối cùng: client phải tự ráp các mảnh theo "type".

Ghi ra:
  output/01_standard.sse.txt    — stream thô kèm timestamp
  output/01_standard.events.json — các event đã parse
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
    auth_headers,
    dump_stream,
    get_token,
    start_standard_chat,
    summarize_sse,
    truncate,
    write_json,
)


def ask_standard(token: str, chat_id: int, question: str) -> list[str]:
    """Gọi /chat/question và ghi lại toàn bộ SSE.

    stream=True ở phía requests là bắt buộc: mặc định requests gom hết body rồi mới trả, làm mất
    thông tin về thời điểm từng event tới — thứ chính là điều demo muốn đo.
    """
    resp = requests.post(
        f"{BASE_URL}/chat/question",
        headers=auth_headers(token),
        json={"chat_id": chat_id, "question": question},
        stream=True,
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    print(f"  Content-Type: {resp.headers.get('content-type')}")
    return dump_stream(
        resp,
        OUTPUT_DIR / "01_standard.sse.txt",
        f"CHAT THƯỜNG — POST /chat/question | chat_id={chat_id} | câu hỏi: {question}",
    )


def group_events(events: list[dict]) -> list[tuple[str, int, str]]:
    """Gộp các event LIÊN TIẾP cùng type thành (type, số lượng, mẫu nội dung).

    Cần gộp vì pha sinh SQL và pha trả lời stream theo từng token: một lượt hỏi đẻ ra hàng nghìn
    event 'sql-result'/'answer-result' giống hệt nhau về cấu trúc, in hết ra thì trôi mất các mốc
    thật sự đáng nhìn (id / question / sql / sql-data / chart / finish).
    """
    grouped: list[tuple[str, int, str]] = []
    for ev in events:
        etype = ev.get("type", "?")
        detail = (
            ev.get("content")
            or ev.get("question")
            or ev.get("brief")
            or ev.get("msg")
            or ev.get("datasource_name")
            or ""
        )
        if grouped and grouped[-1][0] == etype:
            prev_type, prev_count, prev_sample = grouped[-1]
            grouped[-1] = (prev_type, prev_count + 1, prev_sample or detail)
        else:
            grouped.append((etype, 1, detail))
    return grouped


def main() -> None:
    """Chạy trọn một lượt hỏi qua luồng chat thường và tóm tắt các event nhận được."""
    print("=" * 78)
    print("CHAT THƯỜNG (/api/v1/chat/question)")
    print("=" * 78)

    token = get_token()
    print(f"  JWT: {token[:40]}...")

    chat_id = start_standard_chat(token, DATASOURCE_ID)
    print(f"  chat_id={chat_id} (datasource={DATASOURCE_ID} gắn sẵn vào hội thoại)")
    print(f"  Câu hỏi: {QUESTION}\n")

    lines = ask_standard(token, chat_id, QUESTION)
    events = summarize_sse(lines)
    write_json(events, OUTPUT_DIR / "01_standard.events.json")

    print(f"\n  Tổng {len(lines)} dòng thô -> {len(events)} event SSE")
    print("  Thứ tự type (gộp các event liên tiếp cùng type):")
    for etype, count, sample in group_events(events):
        label = f"{etype} x{count}" if count > 1 else etype
        print(f"    {label:<26} {truncate(sample, 80)}")

    print(f"\n  Đã ghi: {OUTPUT_DIR / '01_standard.sse.txt'}")
    print(f"  Đã ghi: {OUTPUT_DIR / '01_standard.events.json'}")


if __name__ == "__main__":
    main()
