"""Tổng hợp số đo của cổng định tuyến câu hỏi từ bảng ``chat_log``.

Đây là công cụ phục vụ **chế độ chỉ đo** (``LLM_ROUTE_ENABLED=true`` +
``LLM_ROUTE_SHADOW=true``): cổng chạy và ghi log trên lưu lượng thật, nhưng quyết định bị ép về
``sql`` nên người dùng không thấy khác biệt gì. Chạy vài ngày rồi đọc báo cáo này để biết phân bố
thật của từng nhóm câu hỏi — con số cần có TRƯỚC khi dám bật nhánh answer.

Vì sao phải đo trên lưu lượng thật: bộ eval offline chỉ vài chục câu, quá nhỏ để suy ra tỷ lệ
chat thật, mà tỷ lệ mới là thứ quyết định cổng có đáng bật hay không.

Cách đọc kết quả:

- Cột ``forced`` là chốt tin cậy. ``unparsed``/``mismatch`` cao nghĩa là model không giữ nổi định
  dạng JSON — đổi model hoặc siết prompt trước khi bàn tới chuyện bật.
- ``category`` là tập đóng nên cộng lại được. Nhóm ``need_data`` chiếm gần hết thì cổng không đáng
  bật: nó chỉ cộng thêm một lượt gọi LLM cho mọi câu hỏi.
- Cột thời gian và token cho biết cái giá của cổng, để so với cái nó tiết kiệm được ở pha SQL.

Chạy từ thư mục ``backend/``::

    .venv/bin/python scripts/route_stats/report.py --days 7
"""

import argparse
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Optional

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import orjson  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlmodel import Session  # noqa: E402

from common.core.db import engine  # noqa: E402
from common.utils.utils import extract_json_object  # noqa: E402

# Giá trị của OperationEnum.ROUTE_QUESTION. Viết thẳng chuỗi thay vì import enum để script chạy
# được cả khi vòng import của apps.chat chưa gỡ xong.
ROUTE_OPERATE = '15'


def parse_reply(messages: Any) -> Optional[dict]:
    """Bóc câu trả lời JSON của cổng ra khỏi cột ``messages`` (JSONB).

    ``messages`` là mảng ``{type, sqlbot_system, content}``; phần tử cuối là lượt ``ai``. Dùng lại
    ``extract_json_object`` thay vì ``orjson.loads`` thẳng vì content có thể còn kèm khối
    ``<think>`` khi model không tắt được thinking.
    """
    if not isinstance(messages, list) or not messages:
        return None
    content = messages[-1].get('content') if isinstance(messages[-1], dict) else None
    if not content:
        return None
    try:
        raw = extract_json_object(content, required_keys={'action'})
        obj = orjson.loads(raw) if raw else None
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--days', type=int, default=7, help='Số ngày gần nhất cần tổng hợp')
    ap.add_argument('--limit', type=int, default=100000, help='Trần số bản ghi đọc về')
    args = ap.parse_args()

    sql = text("""
        SELECT id, pid, messages, token_usage, error,
               EXTRACT(EPOCH FROM (finish_time - start_time)) AS secs
        FROM chat_log
        WHERE operate = :op AND start_time >= now() - make_interval(days => :days)
        ORDER BY id DESC
        LIMIT :limit
    """)
    with Session(engine) as session:
        rows = session.execute(sql, {'op': ROUTE_OPERATE, 'days': args.days,
                                     'limit': args.limit}).mappings().all()

    if not rows:
        print(f'Không có bản ghi cổng định tuyến nào trong {args.days} ngày gần nhất.')
        print('Kiểm lại LLM_ROUTE_ENABLED đã bật chưa.')
        return

    actions: Counter = Counter()
    categories: Counter = Counter()
    errors = 0
    durations: list[float] = []
    tokens = 0
    unreadable = 0

    for r in rows:
        if r['error']:
            errors += 1
        if r['secs'] is not None:
            durations.append(float(r['secs']))
        usage = r['token_usage']
        if isinstance(usage, dict):
            tokens += int(usage.get('total_tokens') or 0)
        obj = parse_reply(r['messages'])
        if obj is None:
            unreadable += 1
            continue
        actions[str(obj.get('action') or '?')] += 1
        categories[str(obj.get('category') or '?')] += 1

    total = len(rows)
    print(f'=== Cổng định tuyến — {args.days} ngày gần nhất, {total} lượt ===\n')

    print('Nhóm câu hỏi (category):')
    for name, n in categories.most_common():
        print(f'  {name:<12} {n:>6}  {n / total:6.1%}')
    if unreadable:
        print(f'  {"(không đọc được)":<12} {unreadable:>6}  {unreadable / total:6.1%}')

    print('\nQuyết định thô của model (action):')
    for name, n in actions.most_common():
        print(f'  {name:<12} {n:>6}  {n / total:6.1%}')

    skippable = sum(n for c, n in categories.items() if c != 'need_data' and c != '?')
    print(f'\nTỷ lệ lượt CÓ THỂ bỏ pha SQL: {skippable}/{total} = {skippable / total:.1%}')

    print(f'\nLượt cổng hỏng (fail-open về sql): {errors} ({errors / total:.1%})')
    if durations:
        durations.sort()
        p50 = durations[len(durations) // 2]
        p95 = durations[min(len(durations) - 1, int(len(durations) * 0.95))]
        print(f'Thời gian cổng: p50 {p50:.2f}s, p95 {p95:.2f}s')
    print(f'Token cổng tiêu tốn: {tokens} ({tokens / total:.0f}/lượt)')


if __name__ == '__main__':
    main()
