"""
Client tham chiếu cho hệ thống bên thứ ba tích hợp SQLBot Chat.

Đây là bản cài đặt đầy đủ luồng mô tả trong API_SPEC.md §2, viết bằng thư viện chuẩn + requests để
dịch sang ngôn ngữ khác được dễ dàng. Mục tiêu là ĐÚNG và ĐỌC ĐƯỢC, không phải tối ưu.

Ba điểm dễ sai nhất được đánh dấu [BẪY] ngay tại chỗ trong code.

Chạy:
    cd backend
    .venv/bin/python scripts/chat_stream_demo/reference_client.py
"""

import json
import os
import uuid
from typing import Any, Callable, Iterator, Optional

import requests

BASE_URL = os.getenv("SQLBOT_BASE_URL", "http://localhost:8001/api/v1")
USERNAME = os.getenv("SQLBOT_USER", "admin")
PASSWORD = os.getenv("SQLBOT_PWD", "SQLBot@123456")

# Read timeout = số giây stream được phép đứng im, KHÔNG phải tổng thời gian chạy. Câu hỏi thường
# xong trong 2-10s nhưng câu nặng có thể lâu hơn. Đặt total timeout sẽ cắt nhầm câu đang chạy tốt.
TIMEOUT = (10, 60)


class SQLBotError(RuntimeError):
    """Lỗi do SQLBot trả về, giữ lại HTTP status để caller phân biệt 401 (hết hạn) với lỗi khác."""

    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message


class SQLBotClient:
    """Client tối thiểu cho luồng hỏi đáp: login -> chọn datasource -> hỏi.

    Giữ token trong instance thay vì truyền qua từng lời gọi, vì hệ thống không có refresh token:
    khi gặp 401 thì cách duy nhất là login lại, và làm việc đó ở một chỗ dễ hơn nhiều.
    """

    def __init__(self, base_url: str = BASE_URL):
        self.base_url = base_url.rstrip("/")
        self.token: Optional[str] = None

    # ---------- hạ tầng ----------

    def _headers(self) -> dict:
        """Header xác thực cho mọi API trừ /login/*.

        [BẪY] Tên header là X-SQLBOT-TOKEN chứ không phải Authorization, và tiền tố 'Bearer ' là
        bắt buộc — thiếu nó server trả 401 'Token schema error!' chứ không phải 'thiếu token'.
        """
        return {
            "X-SQLBOT-TOKEN": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _unwrap(resp: requests.Response) -> Any:
        """Bóc vỏ {code, data, msg} của response thành công, ném SQLBotError nếu thất bại.

        [BẪY] Response của SQLBot bất đối xứng: thành công mới có vỏ bọc, còn lỗi trả về chuỗi JSON
        trần (vd "Incorrect account or password!"). Vì vậy phải kiểm tra status TRƯỚC khi đụng vào
        .data, nếu không client sẽ nổ KeyError đúng lúc gặp lỗi thật.
        """
        if resp.status_code != 200:
            try:
                detail = resp.json()
            except ValueError:
                detail = resp.text
            raise SQLBotError(resp.status_code, str(detail))
        payload = resp.json()
        if isinstance(payload, dict) and "code" in payload and "data" in payload:
            return payload["data"]
        return payload

    # ---------- các bước của luồng ----------

    def login(self) -> str:
        """Đăng nhập lấy JWT (API_SPEC §4).

        [BẪY] Endpoint này nhận form-urlencoded theo chuẩn OAuth2 password flow, KHÔNG nhận JSON.
        Dùng `data=` chứ không phải `json=`; gửi nhầm sẽ nhận HTTP 422 'Field required' rất khó đoán.
        """
        resp = requests.post(
            f"{self.base_url}/login/access-token",
            data={"username": USERNAME, "password": PASSWORD},
            timeout=30,
        )
        self.token = self._unwrap(resp)["access_token"]
        return self.token

    def list_datasources(self) -> list[dict]:
        """Lấy danh sách datasource user có quyền (API_SPEC §5).

        Không hardcode id lấy được ở đây: id khác nhau giữa dev/staging/production.
        """
        resp = requests.get(
            f"{self.base_url}/datasource/list", headers=self._headers(), timeout=30
        )
        return self._unwrap(resp)

    def ask(self, chat_id: str, question: str,
            datasource_id: Optional[int] = None) -> Iterator[dict]:
        """Đặt câu hỏi, yield từng event SSE đã parse (API_SPEC §6).

        Không có bước tạo hội thoại riêng: chat_id là UUID do CLIENT tự sinh. Lần hỏi đầu tiên của
        một chat_id mới phải kèm datasource_id để server dựng hội thoại; các lần sau bỏ trống vì
        datasource đã gắn cứng vào hội thoại và không đổi được nữa.

        Bỏ qua dòng trống (dấu ngắt event của SSE) và mọi dòng không bắt đầu bằng 'data:'.
        Không tự dừng ở 'finish' — để caller quyết định, vì caller mới biết cần làm gì sau đó.
        """
        payload: dict = {"chat_id": chat_id, "question": question}
        if datasource_id is not None:
            payload["datasource"] = datasource_id

        resp = requests.post(
            f"{self.base_url}/chat/question",
            headers=self._headers(),
            json=payload,
            stream=True,
            timeout=TIMEOUT,
        )
        if resp.status_code != 200:
            raise SQLBotError(resp.status_code, resp.text)
        for line in resp.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            yield json.loads(line[len("data:"):])


def run_question(
    client: SQLBotClient,
    chat_id: str,
    question: str,
    datasource_id: Optional[int] = None,
    on_token: Optional[Callable[[str, str], None]] = None,
) -> dict:
    """Chạy trọn một lượt hỏi và trả về {record_id, sql, answer, brief}.

    Đây là phần đáng chép nhất của file: nó cho thấy cách ráp các mảnh rời của stream thành một kết
    quả dùng được. Ba quyết định thiết kế:

    1. Switch trên 'type', KHÔNG dựa vào thứ tự event — trình tự thay đổi tùy có phải câu hỏi đầu
       tiên hay không (chỉ câu đầu mới có 'brief').
    2. Bỏ qua nội dung các event token, chỉ chuyển cho on_token nếu caller muốn hiệu ứng gõ chữ.
       Kết quả chính thức luôn được gửi lại nguyên vẹn ở event mốc ('sql', 'answer').
    3. Lỗi pipeline về kèm HTTP 200 dưới dạng event 'error', nên phải bắt ở đây chứ không thể dựa
       vào status code.

    'answer' có thể là None: pha answer hỏng chỉ báo bằng event info 'answer failed' rồi đi tiếp,
    KHÔNG phát event 'error'. Caller phải chịu được giá trị None.
    """
    result: dict = {"record_id": None, "sql": None, "answer": None, "brief": None}

    for ev in client.ask(chat_id, question, datasource_id):
        etype = ev.get("type")

        if etype == "id":
            result["record_id"] = ev["id"]

        elif etype in ("sql-result", "answer-result"):
            if on_token:
                on_token(etype, ev.get("reasoning_content") or ev.get("content") or "")

        elif etype == "brief":
            result["brief"] = ev["brief"]

        elif etype == "sql":
            result["sql"] = ev["content"]

        elif etype == "answer":
            result["answer"] = ev["content"]

        elif etype == "error":
            # Lỗi pipeline vẫn về kèm HTTP 200 — chỉ nhận ra được ở đây.
            raise SQLBotError(200, ev.get("content", "unknown pipeline error"))

        elif etype == "finish":
            break

    return result


def main() -> None:
    """Chạy demo trọn vẹn luồng 3 bước của tích hợp."""
    client = SQLBotClient()

    print("1. Đăng nhập...")
    client.login()
    print(f"   JWT: {client.token[:40]}...")

    print("2. Danh sách datasource:")
    datasources = client.list_datasources()
    for ds in datasources:
        print(f"   [{ds['id']}] {ds['name']} ({ds['type_name']})")
    ds_id = int(os.getenv("SQLBOT_DS_ID", str(datasources[0]["id"]) if datasources else "1"))

    # chat_id do client tự sinh, dạng UUID. Sinh ngẫu nhiên ở đây cho gọn; hệ thống thật nên lấy
    # UUID gắn với phiên hội thoại của chính mình để hỏi tiếp đúng mạch sau khi người dùng quay lại.
    chat_id = os.getenv("SQLBOT_CHAT_ID", str(uuid.uuid4()))
    print(f"3. Hỏi trên chat_id={chat_id} (tự sinh), datasource={ds_id}")

    question = os.getenv("SQLBOT_QUESTION", "Có tổng số bao nhiêu Nghị quyết")
    print(f"   Câu hỏi 1: {question}")
    # Câu hỏi ĐẦU TIÊN của chat_id mới -> bắt buộc kèm datasource_id.
    result = run_question(client, chat_id, question, datasource_id=ds_id)

    print(f"\n   record_id : {result['record_id']}")
    print(f"   tiêu đề   : {result['brief']}")
    print(f"\n   SQL:\n{result['sql']}")
    # In None tường minh thay vì bỏ qua, để lộ ngay khi pha answer hỏng.
    print(f"\n   Trả lời:\n{result['answer']}")

    # Câu hỏi tiếp theo trên cùng chat_id -> KHÔNG gửi datasource nữa, ngữ cảnh multi-turn tự có.
    follow_up = os.getenv("SQLBOT_QUESTION_2", "Trong đó bảng nào nhiều nhất")
    print(f"\n   Câu hỏi 2 (multi-turn): {follow_up}")
    result2 = run_question(client, chat_id, follow_up)
    print(f"\n   Trả lời:\n{result2['answer']}")


if __name__ == "__main__":
    main()
