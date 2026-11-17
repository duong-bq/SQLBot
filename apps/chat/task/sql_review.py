"""Bước khám phá (explore) và kiểm chứng (verify) quanh bước sinh SQL.

Plan: ``docs/superpowers/plans/2026-11-16-explore-verify-sql.md``. Hai bước là workflow cố định do
code điều phối, mỗi bước một lần gọi LLM trả JSON, không dùng tool calling:

- **Explore** chạy một lần trước bước sinh SQL. Model xin xem thêm bảng ngoài prompt (lấy từ
  catalog) và xin chạy truy vấn dò (probe). Kết quả thành khối ``<exploration>`` cho bước sinh SQL.
- **Verify** chạy sau mỗi lần SQL chạy thành công, trước pha answer/chart. Model trả ``accept``
  hoặc ``revise`` kèm danh sách vấn đề và probe để lấy thêm bằng chứng.

Ràng buộc chung:

- **Hỏng kỹ thuật thì đi tiếp.** LLM lỗi hay JSON không đọc được thì explore trả khối rỗng, verify
  trả ``accept``: luồng chạy như khi tắt bước đó. ``revise`` là một quyết định, không phải lỗi.
- **Không phát event SSE.** Mọi dấu vết nằm ở ``chat_log`` dưới operate riêng.
- **Không vào lịch sử.** Message của hai bước chỉ nằm trong lần gọi của chúng. Khối
  ``<exploration>`` đi vào hội thoại SQL dưới dạng message hệ thống, nên lượt sau lọc bỏ.
- **Probe đi đúng đường thực thi của SQL chính** (``LLMService.run_probes``): kiểm bảng trên AST,
  áp luật dòng và scope SW, rồi mới chạy. Module này chỉ thêm các ràng buộc riêng của probe: ép
  LIMIT trên AST và chặn cột ngoài quyền.
"""

import json
from dataclasses import dataclass, replace
from typing import Any, Optional

import orjson
import sqlglot
from langchain_core.messages import HumanMessage
from sqlglot import exp
from sqlmodel import Session

from apps.chat.curd.chat import end_log, start_log, trigger_log_error
from apps.chat.models.chat_model import (
    AIPromptMessage,
    HumanPromptMessage,
    OperationEnum,
    SystemPromptMessage,
)
from apps.chat.task.prompt_budget import is_prompt_rejection
from apps.db.db import get_sqlglot_dialect
from apps.template.template import get_base_template
from common.core.config import settings
from common.utils.utils import SQLBotLogUtil, extract_json_object, prepare_for_orjson

VERDICT_ACCEPT = 'accept'
VERDICT_REVISE = 'revise'

# Dialect sqlglot để viết lại probe. `get_sqlglot_dialect` chỉ khai vài loại vì nó chỉ dùng để
# đọc tên bảng; ở đây phải SINH lại câu lệnh nên cần đúng cú pháp giới hạn dòng của từng loại.
_WRITE_DIALECTS = {
    'pg': 'postgres',
    'excel': 'postgres',
    'kingbase': 'postgres',
    'redshift': 'redshift',
    'oracle': 'oracle',
    'dm': 'oracle',
    'ck': 'clickhouse',
}

# Số ký tự tối đa của một giá trị trong hồ sơ cột và kết quả probe đưa vào prompt.
_VALUE_MAX_CHARS = 200


@dataclass(frozen=True)
class Probe:
    """Một truy vấn dò model xin chạy."""

    id: str
    purpose: str
    sql: str


@dataclass(frozen=True)
class ProbeResult:
    """Kết quả một probe: ``error`` khác rỗng thì ``fields``/``rows`` rỗng."""

    probe: Probe
    fields: tuple = ()
    rows: tuple = ()
    error: str = ''


@dataclass(frozen=True)
class ExploreRequest:
    """Đầu ra đã chuẩn hoá của explore."""

    tables: tuple[str, ...] = ()
    probes: tuple[Probe, ...] = ()


@dataclass(frozen=True)
class VerifyDecision:
    """Quyết định của verify, đã an toàn để hành động theo.

    ``forced`` khác rỗng nghĩa là verify không cho ra kết luận dùng được (``'unparsed'``,
    ``'error'``) và quyết định bị ép về ``accept``.
    """

    verdict: str
    issues: tuple[str, ...] = ()
    probes: tuple[Probe, ...] = ()
    forced: str = ''

    @property
    def revise(self) -> bool:
        """Verify có yêu cầu sinh lại SQL không."""
        return self.verdict == VERDICT_REVISE


def _template() -> dict:
    """Lấy block prompt ``sql_review`` từ template gốc."""
    return get_base_template()['template']['sql_review']


def _parse_probes(raw: Any, prefix: str) -> tuple[Probe, ...]:
    """Chuẩn hoá danh sách probe; bỏ phần tử hỏng và cắt theo ``LLM_SQL_PROBE_MAX``."""
    if not isinstance(raw, list):
        return ()
    probes = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        sql = item.get('sql')
        if not isinstance(sql, str) or not sql.strip():
            continue
        probe_id = str(item.get('id') or f'{prefix}{index + 1}')
        probes.append(Probe(id=probe_id, purpose=str(item.get('purpose') or ''), sql=sql.strip()))
    return tuple(probes[:max(0, settings.LLM_SQL_PROBE_MAX)])


def parse_explore_answer(text: str) -> Optional[ExploreRequest]:
    """Đọc JSON của explore; không có JSON hợp lệ thì trả ``None``."""
    json_str = extract_json_object(text or '', required_keys=('probes',)) \
        or extract_json_object(text or '', required_keys=('tables',))
    if json_str is None:
        return None
    try:
        data = orjson.loads(json_str)
    except Exception:
        return None
    tables = data.get('tables')
    tables = tuple(str(t) for t in tables if isinstance(t, str)) if isinstance(tables, list) else ()
    return ExploreRequest(tables=tables, probes=_parse_probes(data.get('probes'), 'p'))


def parse_verify_answer(text: str) -> VerifyDecision:
    """Đọc JSON của verify; mọi đường hỏng đều thành ``accept`` có ghi ``forced``."""
    json_str = extract_json_object(text or '', required_keys=('verdict',))
    if json_str is None:
        return VerifyDecision(verdict=VERDICT_ACCEPT, forced='unparsed')
    try:
        data = orjson.loads(json_str)
    except Exception:
        return VerifyDecision(verdict=VERDICT_ACCEPT, forced='unparsed')
    verdict = str(data.get('verdict') or '').strip().lower()
    if verdict not in (VERDICT_ACCEPT, VERDICT_REVISE):
        return VerifyDecision(verdict=VERDICT_ACCEPT, forced='unparsed')
    issues = data.get('issues')
    issues = tuple(str(i) for i in issues if str(i).strip()) if isinstance(issues, list) else ()
    return VerifyDecision(verdict=verdict, issues=issues,
                          probes=_parse_probes(data.get('probes'), 'v'))


def probe_dialect(ds_type: str) -> Optional[str]:
    """Dialect sqlglot dùng để đọc và viết lại probe của một loại datasource."""
    return get_sqlglot_dialect(ds_type) or _WRITE_DIALECTS.get(ds_type)


def _star_projection(select: exp.Select) -> bool:
    """Danh sách cột của ``select`` có ``*`` hoặc ``bang.*`` không."""
    for item in select.expressions:
        if isinstance(item, exp.Star):
            return True
        if isinstance(item, exp.Column) and isinstance(item.this, exp.Star):
            return True
    return False


def check_probe_columns(tree: exp.Expression, permitted_columns: dict[str, set]) -> None:
    """Chặn probe chạm cột ngoài quyền; vi phạm thì ném ``ValueError``.

    Kết quả probe đi thẳng vào prompt, nên cột bị tắt hoặc ngoài quyền SW không được lọt ra
    qua probe. Hai luật: không chọn ``*`` (trả về mọi cột vật lý, kể cả cột ẩn), và mọi tên cột
    tham chiếu phải là cột được phép của một bảng được phép hoặc là bí danh do chính câu lệnh đặt.
    So theo tên cột, không theo bảng: bí danh bảng làm việc gắn cột với bảng không còn tất định.
    """
    for select in tree.find_all(exp.Select):
        if _star_projection(select):
            raise ValueError('Probe không được chọn * ; hãy liệt kê đúng các cột cần xem')
    # So không phân biệt hoa thường: identifier không nháy được CSDL chuẩn hoá theo cách riêng,
    # còn sqlglot giữ nguyên cách model viết.
    allowed = set()
    for columns in permitted_columns.values():
        allowed |= {c.lower() for c in columns}
    allowed |= {a.alias.lower() for a in tree.find_all(exp.Alias) if a.alias}
    for alias in tree.find_all(exp.TableAlias):
        allowed |= {c.name.lower() for c in alias.columns}
    unknown = sorted({c.name for c in tree.find_all(exp.Column)
                      if c.name and not isinstance(c.this, exp.Star)
                      and c.name.lower() not in allowed})
    if unknown:
        raise ValueError(f"Probe dùng cột không có trong schema được phép: {', '.join(unknown)}")


def limit_probe_sql(sql: str, ds_type: str, max_rows: int,
                    permitted_columns: Optional[dict[str, set]] = None) -> str:
    """Ép giới hạn số dòng của probe trên AST, trả về câu lệnh đã viết lại.

    Không tin vào prompt: probe là câu model tự viết, chạy trên CSDL nguồn, kết quả nhồi vào
    prompt. Câu đã có LIMIT nhỏ hơn thì giữ. Chỉ nhận đúng một câu truy vấn; parse hỏng thì ném
    lỗi, probe đó được báo lỗi cho model chứ không làm hỏng lượt.
    """
    dialect = probe_dialect(ds_type)
    statements = [s for s in sqlglot.parse(sql, dialect=dialect) if s]
    if len(statements) != 1:
        raise ValueError('Probe phải là đúng một câu truy vấn')
    tree = statements[0]
    if not isinstance(tree, exp.Query):
        raise ValueError('Probe phải là câu truy vấn SELECT')
    if permitted_columns is not None:
        check_probe_columns(tree, permitted_columns)
    current = tree.args.get('limit')
    current_value = None
    if isinstance(current, exp.Limit) and isinstance(current.expression, exp.Literal):
        try:
            current_value = int(current.expression.name)
        except ValueError:
            current_value = None
    if current_value is None or current_value > max_rows:
        tree = tree.limit(max_rows)
    return tree.sql(dialect=dialect)


def _short(value: Any) -> Any:
    """Cắt giá trị chuỗi quá dài trước khi đưa vào prompt."""
    if isinstance(value, str) and len(value) > _VALUE_MAX_CHARS:
        return value[:_VALUE_MAX_CHARS] + '…'
    return value


def profile_columns(fields: list, rows: list) -> list[dict]:
    """Hồ sơ từng cột trên TOÀN BỘ kết quả: số NULL, số giá trị khác nhau, min/max.

    Verify chỉ thấy vài dòng đầu; hồ sơ cho nó đánh giá được phần còn lại (cột toàn NULL, một giá
    trị lặp khắp nơi, khoảng giá trị vô lý). Cột có kiểu lẫn lộn không so được thì bỏ min/max.
    """
    profiles = []
    for name in fields or []:
        values = [row.get(name) for row in rows or [] if isinstance(row, dict)]
        present = [v for v in values if v is not None]
        profile = {'column': name, 'nulls': len(values) - len(present)}
        try:
            profile['distinct'] = len({orjson.dumps(prepare_for_orjson(v)) for v in present})
        except Exception:
            profile['distinct'] = None
        if present:
            try:
                profile['min'] = _short(min(present))
                profile['max'] = _short(max(present))
            except TypeError:
                pass
        profiles.append(profile)
    return profiles


def _is_zero(value: Any) -> bool:
    """Giá trị bằng 0, kể cả số đã bị đổi thành chuỗi."""
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return value == 0
    if isinstance(value, str):
        try:
            return float(value) == 0
        except ValueError:
            return False
    return False


def result_hints(fields: list, rows: list) -> list[str]:
    """Các dấu hiệu tất định đáng ngờ. Chỉ là gợi ý cho verify, không quyết định gì."""
    hints = []
    rows = rows or []
    if not rows:
        hints.append('Truy vấn trả về 0 dòng.')
        return hints
    if len(rows) == 1:
        row = rows[0]
        numeric = [name for name in fields or [] if _is_zero(row.get(name))]
        if numeric and len(numeric) == len(fields or []):
            hints.append('Kết quả là một dòng và mọi cột đều bằng 0.')
    for name in fields or []:
        if all(isinstance(r, dict) and r.get(name) is None for r in rows):
            hints.append(f'Cột {name} toàn NULL.')
    return hints


def render_probe_results(results: list[ProbeResult]) -> str:
    """Render kết quả các probe thành các khối ``<probe>`` cho prompt."""
    parts = []
    for item in results:
        body = f'<purpose>{item.probe.purpose}</purpose>\n<sql>\n{item.probe.sql}\n</sql>\n'
        if item.error:
            body += f'<error>{item.error}</error>\n'
        else:
            rows = [{k: _short(v) for k, v in row.items()} for row in item.rows]
            body += (f'<result rows="{len(rows)}">\n'
                     f'{json.dumps(prepare_for_orjson(rows), ensure_ascii=False, default=str)}\n'
                     f'</result>\n')
        parts.append(f'<probe id="{item.probe.id}">\n{body}</probe>')
    return '\n'.join(parts)


def render_exploration(extra_schema: str, extra_sample: str, results: list[ProbeResult]) -> str:
    """Dựng khối ``<exploration>``; không có gì để kể thì trả chuỗi rỗng."""
    parts = []
    if extra_schema:
        parts.append(f'<extra-schema>\n{extra_schema}</extra-schema>')
    if extra_sample:
        parts.append(f'<extra-sample-data>\n{extra_sample}\n</extra-sample-data>')
    if results:
        parts.append(render_probe_results(results))
    if not parts:
        return ''
    return '<exploration>\n' + '\n'.join(parts) + '\n</exploration>'


def render_verify_issues(issues: tuple[str, ...]) -> str:
    """Danh sách vấn đề của verify, mỗi vấn đề một dòng gạch đầu dòng."""
    return '\n'.join(f'- {issue}' for issue in issues)


def _log_messages(messages: list) -> list[dict]:
    """Chuyển message sang dạng lưu ``chat_log``, giống các pha khác."""
    return [{'type': msg.type,
             'sqlbot_system': getattr(msg, 'sqlbot_system', False) is True,
             'content': msg.content} for msg in messages]


def _stream_text(llm: Any, messages: list) -> tuple[str, str, dict]:
    """Gọi LLM, gom content, reasoning và token usage."""
    # Import muộn: `llm.py` import module này ở cấp module.
    from apps.chat.task.llm import process_stream

    text = ''
    thinking = ''
    token_usage: dict = {}
    for chunk in process_stream(llm.stream(messages), token_usage):
        if chunk.get('content'):
            text += chunk.get('content')
        if chunk.get('reasoning_content'):
            thinking += chunk.get('reasoning_content')
    return text, thinking, token_usage


def _explore_message(llm_service: Any) -> HumanMessage:
    """Message explore nối sau tiền tố hội thoại SQL, dựng lại sau mỗi nấc rút gọn."""
    schema_prompt = llm_service.schema_prompt
    catalog = schema_prompt.m_schema.render_catalog() if schema_prompt else ''
    content = _template()['explore'].format(
        catalog=catalog or '(không còn bảng nào ngoài <m-schema>)',
        question=llm_service.chat_question.question_for_prompt(),
        max_probes=settings.LLM_SQL_PROBE_MAX,
    )
    return HumanMessage(content=content)


def _drop_catalog(llm_service: Any) -> None:
    """Bỏ catalog khỏi schema prompt sau explore để thang rút gọn của pha SQL bỏ qua nấc đó."""
    schema_prompt = llm_service.schema_prompt
    if schema_prompt is not None and schema_prompt.m_schema.catalog:
        llm_service.schema_prompt = replace(
            schema_prompt, m_schema=replace(schema_prompt.m_schema, catalog=()))


def explore(llm_service: Any, _session: Session) -> str:
    """Chạy explore cho lượt hiện tại, trả về khối ``<exploration>`` (rỗng nếu không có gì).

    Prompt là tiền tố hội thoại SQL (system, rule, schema, thuật ngữ, ví dụ, lịch sử) cộng một
    message explore. Dùng chung tiền tố vì explore cần đúng ngữ cảnh đó, và model phía sau có
    prefix cache thì lần sinh SQL ngay sau gần như không tốn thêm cho phần đã đọc.

    Model từ chối prompt (400) thì đi đúng thang rút gọn của pha SQL, nấc đầu là bỏ catalog.
    Mọi lỗi khác thì trả rỗng: lượt chạy như khi tắt explore.
    """
    log = None
    catalog_tables = {}
    if llm_service.schema_prompt is not None:
        catalog_tables = {t.name: t for t in llm_service.schema_prompt.m_schema.catalog}
    try:
        prefix = list(llm_service.sql_message)
        message = _explore_message(llm_service)
        log = start_log(session=_session,
                        ai_modal_id=llm_service.chat_question.ai_modal_id,
                        ai_modal_name=llm_service.chat_question.ai_modal_name,
                        operate=OperationEnum.EXPLORE_SQL,
                        record_id=llm_service.record.id,
                        full_message=_log_messages(prefix + [message]))
        while True:
            try:
                text, thinking, token_usage = _stream_text(llm_service.llm, prefix + [message])
                break
            except Exception as e:
                if not is_prompt_rejection(e):
                    raise
                llm_service._reduce_schema_prompt(e)
                message = _explore_message(llm_service)

        request = parse_explore_answer(text)
        exploration = ''
        if request is not None:
            tables = [t for t in dict.fromkeys(request.tables) if t in catalog_tables]
            extra_schema = ''.join(catalog_tables[t].render() for t in tables)
            extra_sample = llm_service.load_sample_data(_session, tables) if tables else ''
            results = llm_service.run_probes(_session, list(request.probes))
            exploration = render_exploration(extra_schema, extra_sample, results)

        end_log(session=_session, log=log,
                full_message=_log_messages(prefix + [message, AIPromptMessage(content=text)])
                + [{'type': 'exploration', 'sqlbot_system': True, 'content': exploration}],
                reasoning_content=thinking, token_usage=token_usage)
        if request is None:
            trigger_log_error(session=_session, log=log)
        SQLBotLogUtil.info(
            f'Explore finished for record {llm_service.record.id} '
            f'({len(exploration)} chars of exploration)',
            extra={'record_id': llm_service.record.id, 'chars': len(exploration)},
        )
        return exploration
    except Exception as e:
        SQLBotLogUtil.error(f'Explore failed, continuing without it ({type(e).__name__}: {e})',
                            extra={'error': type(e).__name__})
        if log is not None:
            try:
                trigger_log_error(session=_session, log=log)
            except Exception:
                pass
        return ''
    finally:
        _drop_catalog(llm_service)


def build_verify_prompts(llm_service: Any, sql: str, result: dict, schema: str,
                         exploration: str) -> tuple[str, str]:
    """Dựng cặp (system, user) prompt của verify từ kết quả đã chạy."""
    fields = list(result.get('fields') or [])
    rows = list(result.get('data') or [])
    sample = [{k: _short(v) for k, v in row.items()} for row in
              rows[:max(0, settings.LLM_SQL_VERIFY_SAMPLE_ROWS)] if isinstance(row, dict)]
    question = llm_service.chat_question
    template = _template()
    system = template['verify_system'].format(max_probes=settings.LLM_SQL_PROBE_MAX)
    hints = result_hints(fields, rows)
    user = template['verify_user'].format(
        question=question.question_for_prompt(),
        engine=question.engine,
        schema=schema,
        terminologies=question.terminologies or '',
        exploration=exploration or '',
        sql=sql,
        row_count=len(rows),
        columns=json.dumps(fields, ensure_ascii=False),
        rows=json.dumps(prepare_for_orjson(sample), ensure_ascii=False, default=str),
        profile=json.dumps(prepare_for_orjson(profile_columns(fields, rows)),
                           ensure_ascii=False, default=str),
        hints='\n'.join(hints) or 'Không có.',
    )
    return system, user


def verify(llm_service: Any, _session: Session, sql: str, result: dict, tables: list,
           exploration: str) -> VerifyDecision:
    """Chạy verify cho một kết quả SQL vừa chạy thành công.

    Không ném exception ra ngoài: hỏng ở đây thành ``accept`` (``forced='error'``), tức kết quả
    đi tiếp như khi tắt verify. Dùng ``thinking_llm`` vì đây là bước cần suy luận.
    """
    log = None
    try:
        schema = llm_service.used_tables_schema(_session, tables)
        sys_prompt, user_prompt = build_verify_prompts(llm_service, sql, result, schema,
                                                       exploration)
        messages = [SystemPromptMessage(content=sys_prompt), HumanPromptMessage(content=user_prompt)]
        log = start_log(session=_session,
                        ai_modal_id=llm_service.chat_question.ai_modal_id,
                        ai_modal_name=llm_service.chat_question.ai_modal_name,
                        operate=OperationEnum.VERIFY_SQL,
                        record_id=llm_service.record.id,
                        full_message=_log_messages(messages))
        text, thinking, token_usage = _stream_text(llm_service.thinking_llm, messages)
        decision = parse_verify_answer(text)
        messages.append(AIPromptMessage(content=text))
        end_log(session=_session, log=log, full_message=_log_messages(messages),
                reasoning_content=thinking, token_usage=token_usage)
        if decision.forced:
            trigger_log_error(session=_session, log=log)
        SQLBotLogUtil.info(
            f'Verify returned {decision.verdict} for record {llm_service.record.id} '
            f'({len(decision.issues)} issues, forced {decision.forced or "none"})',
            extra={'record_id': llm_service.record.id, 'verdict': decision.verdict,
                   'forced': decision.forced or None},
        )
        return decision
    except Exception as e:
        SQLBotLogUtil.error(f'Verify failed, accepting the result ({type(e).__name__}: {e})',
                            extra={'error': type(e).__name__})
        if log is not None:
            try:
                trigger_log_error(session=_session, log=log)
            except Exception:
                pass
        return VerifyDecision(verdict=VERDICT_ACCEPT, forced='error')
