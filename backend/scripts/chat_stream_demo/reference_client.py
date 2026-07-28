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
from typing import Any, Callable, Iterator, Optional

import requests

BASE_URL = os.getenv("SQLBOT_BASE_URL", "http://localhost:8001/api/v1")
USERNAME = os.getenv("SQLBOT_USER", "admin")
PASSWORD = os.getenv("SQLBOT_PWD", "SQLBot@123456")

# Read timeout = số giây stream được phép đứng im, KHÔNG phải tổng thời gian chạy. Pipeline mất
# ~60s nhưng vẫn đẩy dữ liệu liên tục nên 90s là dư. Đặt total timeout sẽ cắt nhầm câu hỏi nặng.
TIMEOUT = (10, 90)


class SQLBotError(RuntimeError):
    """Lỗi do SQLBot trả về, giữ lại HTTP status để caller phân biệt 401 (hết hạn) với lỗi khác."""

    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message


class SQLBotClient:
    """Client tối thiểu cho luồng hỏi đáp: login -> chọn datasource -> tạo hội thoại -> hỏi.

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

    def start_chat(self, datasource_id: int) -> int:
        """Tạo hội thoại mới gắn với một datasource, trả về chat_id (API_SPEC §6).

        Datasource gắn cứng vào hội thoại tại đây nên các câu hỏi sau không khai lại. Giữ nguyên
        chat_id qua nhiều câu hỏi để có ngữ cảnh multi-turn ("còn năm ngoái thì sao?").
        Quyền trên datasource được kiểm ngay tại bước này, không đợi đến lúc hỏi.
        """
        resp = requests.post(
            f"{self.base_url}/chat/start",
            headers=self._headers(),
            json={"datasource": datasource_id, "origin": 0},
            timeout=30,
        )
        return self._unwrap(resp)["id"]

    def ask(self, chat_id: int, question: str) -> Iterator[dict]:
        """Đặt câu hỏi, yield từng event SSE đã parse (API_SPEC §7).

        Bỏ qua dòng trống (dấu ngắt event của SSE) và mọi dòng không bắt đầu bằng 'data:'.
        Không tự dừng ở 'finish' — để caller quyết định, vì caller mới biết cần làm gì sau đó.
        """
        resp = requests.post(
            f"{self.base_url}/chat/question",
            headers=self._headers(),
            json={"chat_id": chat_id, "question": question},
            stream=True,
            timeout=TIMEOUT,
        )
        if resp.status_code != 200:
            raise SQLBotError(resp.status_code, resp.text)
        for line in resp.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            yield json.loads(line[len("data:"):])

    def get_data(self, record_id: int, live: bool = False) -> dict:
        """Lấy số liệu của một record (API_SPEC §8).

        [BẪY] Số liệu KHÔNG nằm trong SSE. Event 'sql-data' chỉ là tín hiệu 'đã chạy xong'; bỏ qua
        bước này thì UI vẽ ra biểu đồ rỗng mà không có lỗi nào báo.

        live=True chạy lại SQL trên DB nghiệp vụ để lấy số mới nhất, chậm hơn và tạo tải lên nguồn
        — chỉ dùng cho nút "Làm mới", đừng gọi định kỳ.
        """
        path = "data_live" if live else "data"
        resp = requests.get(
            f"{self.base_url}/chat/record/{record_id}/{path}",
            headers=self._headers(),
            timeout=60,
        )
        return self._unwrap(resp)


def run_question(
    client: SQLBotClient,
    chat_id: int,
    question: str,
    on_token: Optional[Callable[[str, str], None]] = None,
) -> dict:
    """Chạy trọn một lượt hỏi và trả về {record_id, sql, chart, answer, data, brief}.

    Đây là phần đáng chép nhất của file: nó cho thấy cách ráp các mảnh rời của stream thành một kết
    quả dùng được. Bốn quyết định thiết kế:

    1. Switch trên 'type', KHÔNG dựa vào thứ tự event — trình tự thay đổi tùy hội thoại có gắn sẵn
       datasource hay không, có phải câu hỏi đầu tiên hay không.
    2. Lấy số liệu ngay tại 'sql-data' (giây ~22) thay vì đợi 'finish' (giây ~53) — UI hiện bảng
       sớm hơn 31 giây trong khi LLM vẫn đang sinh biểu đồ.
    3. Bỏ qua nội dung các event token, chỉ chuyển cho on_token nếu caller muốn hiệu ứng gõ chữ.
       Kết quả chính thức luôn được gửi lại nguyên vẹn ở event mốc ('sql', 'chart', 'answer').
    4. 'answer' và 'chart' được sinh SONG SONG nên các event token của chúng đến XEN KẼ nhau và
       không biết bên nào về trước (API_SPEC §7.5). Switch trên 'type' xử lý được việc này mà không
       cần code gì thêm — đó chính là lý do không nên viết state machine theo pha.

    'answer' có thể là None: pha answer hỏng chỉ báo bằng event info 'answer failed' rồi đi tiếp,
    KHÔNG phát event 'error'. Caller phải chịu được giá trị None.
    """
    result: dict = {"record_id": None, "sql": None, "chart": None, "answer": None,
                    "data": None, "brief": None}

    for ev in client.ask(chat_id, question):
        etype = ev.get("type")

        if etype == "id":
            result["record_id"] = ev["id"]

        elif etype in ("sql-result", "chart-result", "answer-result", "datasource-result"):
            if on_token:
                on_token(etype, ev.get("reasoning_content") or ev.get("content") or "")

        elif etype == "brief":
            result["brief"] = ev["brief"]

        elif etype == "sql":
            result["sql"] = ev["content"]

        elif etype == "sql-data":
            result["data"] = client.get_data(result["record_id"])

        elif etype == "chart":
            # Parse 2 lớp: 'content' là CHUỖI chứa JSON, không phải object.
            result["chart"] = json.loads(ev["content"])

        elif etype == "answer":
            # Ngược với 'chart': text thuần, parse 1 lớp là xong.
            result["answer"] = ev["content"]

        elif etype == "error":
            # Lỗi pipeline vẫn về kèm HTTP 200 — chỉ nhận ra được ở đây.
            raise SQLBotError(200, ev.get("content", "unknown pipeline error"))

        elif etype == "finish":
            break

    return result


def normalize_chart(chart: Optional[dict]) -> dict:
    """Quy cấu hình biểu đồ về một dạng ổn định {type, title, category, metrics}.

    BẮT BUỘC phải có bước này. Cấu hình biểu đồ do LLM sinh ra nên schema KHÔNG ổn định — cùng một
    câu hỏi chạy hai lần có thể ra hai dạng khác nhau (đã quan sát được trên thực tế):

      - type='table'          -> có 'columns', KHÔNG có 'axis'
      - type='pie'            -> axis.y là OBJECT, cột phân loại nằm ở axis.series, không có axis.x
      - type='column|bar|line'-> axis.y là MẢNG; cột phân loại có thể ở axis.x HOẶC axis.series
      - nhiều chỉ số          -> thêm axis['multi-quota']

    Truy cập thẳng chart['axis']['x'] sẽ ném KeyError ở lần chạy thứ hai chứ không phải lần đầu —
    kiểu lỗi chỉ lộ ra khi đã lên production.

    Trả về: category = tên cột phân loại (có thể None), metrics = danh sách tên cột chỉ số.
    """
    out = {"type": "table", "title": "", "category": None, "metrics": []}
    if not chart:
        return out
    out["type"] = chart.get("type", "table")
    out["title"] = chart.get("title", "")

    if out["type"] == "table":
        out["metrics"] = [c["value"] for c in chart.get("columns", [])]
        return out

    axis = chart.get("axis") or {}

    # Cột phân loại: ưu tiên x, không có thì lấy series (pie luôn rơi vào nhánh này).
    for key in ("x", "series"):
        node = axis.get(key)
        if isinstance(node, dict) and node.get("value"):
            out["category"] = node["value"]
            break

    # y có thể là object (pie) hoặc mảng (column/bar/line).
    y = axis.get("y")
    if isinstance(y, dict):
        y = [y]
    out["metrics"] = [m["value"] for m in (y or []) if isinstance(m, dict) and m.get("value")]
    return out


def render_text(chart: Optional[dict], data: Optional[dict]) -> str:
    """Render kết quả ra text, minh họa cách ghép cấu hình biểu đồ với số liệu (API_SPEC §9).

    UI thật sẽ đẩy hai thứ này vào thư viện biểu đồ; ở đây in ra chữ cho dễ kiểm chứng. Điểm cần
    thấy: chart chỉ cho biết TÊN CỘT nào vào trục nào, còn giá trị nằm hoàn toàn trong data.
    """
    if not data:
        return "(không có dữ liệu)"
    rows = data.get("data", [])
    fields = data.get("fields", [])
    spec = normalize_chart(chart)

    # Không xác định được cột phân loại hoặc chỉ số thì lùi về in bảng — luôn hiển thị được thứ gì
    # đó, thay vì để người dùng thấy màn hình trắng vì LLM trả cấu hình lạ.
    if spec["type"] == "table" or not spec["category"] or not spec["metrics"]:
        header = " | ".join(fields)
        body = "\n".join(" | ".join(str(r.get(f)) for f in fields) for r in rows)
        return f"{header}\n{body}"

    metric = spec["metrics"][0]
    lines = [f"{spec['title']}  [{spec['type']}]"]
    for row in rows:
        # Giá trị số có thể về dưới dạng string khi quá 15 chữ số có nghĩa -> luôn ép kiểu.
        value = float(row[metric])
        lines.append(f"  {str(row[spec['category']])[:50]:<52} {value:>15,.2f}")
    return "\n".join(lines)


def main() -> None:
    """Chạy demo trọn vẹn 5 bước của luồng tích hợp."""
    client = SQLBotClient()

    print("1. Đăng nhập...")
    client.login()
    print(f"   JWT: {client.token[:40]}...")

    print("2. Danh sách datasource:")
    datasources = client.list_datasources()
    for ds in datasources:
        print(f"   [{ds['id']}] {ds['name']} ({ds['type_name']})")
    ds_id = int(os.getenv("SQLBOT_DS_ID", "5"))

    print(f"3. Tạo hội thoại trên datasource {ds_id}...")
    chat_id = client.start_chat(ds_id)
    print(f"   chat_id={chat_id}")

    question = os.getenv("SQLBOT_QUESTION", "Tổng số thu ngân sách theo từng nhóm nguồn thu")
    print(f"4. Hỏi: {question}")
    print("   (mất ~60 giây)")
    result = run_question(client, chat_id, question)

    print(f"\n   record_id : {result['record_id']}")
    print(f"   tiêu đề   : {result['brief']}")
    print(f"\n   SQL:\n{result['sql']}")
    # In None tường minh thay vì bỏ qua, để lộ ngay khi pha answer hỏng.
    print(f"\n   Trả lời:\n{result['answer']}")
    print(f"\n5. Kết quả ({len(result['data']['data'])} dòng):")
    print(render_text(result["chart"], result["data"]))


if __name__ == "__main__":
    main()
