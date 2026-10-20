"""Prompt người dùng sửa được do AI Gateway đẩy xuống trong body `/chat/ask`.

Gateway resolve bộ prompt của dịch vụ `datasource-query` theo kho của datasource rồi gửi kèm
request dưới khoá `prompts` (envelope `{name, version_ref, prompt_type, content, config}`).
SQLBot không tự gọi Gateway để lấy prompt: request không có `prompts` (web UI, MCP) chạy bằng bản
mặc định trong `templates/prompts/`.
"""

import re
from typing import Any, Optional

from pydantic import BaseModel

from common.utils.utils import SQLBotLogUtil

SQL_PLAYBOOK_PROMPT = '[SQLBot] SQL Playbook'
ANSWER_PERSONA_PROMPT = '[SQLBot] Answer Persona'

_COMMENT_RE = re.compile(r'<!--.*?-->', re.DOTALL)


class GatewayPrompt(BaseModel):
    """Một phần tử của `prompts` trong body `/chat/ask`; khoá lạ bị bỏ qua."""

    name: str
    version_ref: Optional[str] = None
    prompt_type: Optional[str] = None
    content: Any = None
    config: Optional[dict] = None


def strip_prompt_comments(text: Optional[str]) -> str:
    """Bỏ các khối chú thích `<!-- ... -->` và khoảng trắng thừa ở hai đầu.

    Gateway không cho lưu prompt rỗng, nên bản mặc định của SQL Playbook chỉ chứa một khối chú
    thích hướng dẫn cách viết. Bỏ chú thích trước khi dùng thì bản đó thành rỗng và SQLBot dựng
    prompt y như khi không có playbook. Người dùng cũng giữ được lời nhắc trong ô sửa mà không tốn
    token.
    """
    if not text:
        return ''
    return _COMMENT_RE.sub('', text).strip()


def pick_gateway_prompts(prompts: list[GatewayPrompt]) -> tuple[str, str]:
    """Tra `prompts` theo tên, trả `(sql_playbook, answer_persona)` dạng chuỗi gốc.

    Tên lạ bị bỏ qua (vd. prompt cũ còn sót trên Gateway). Ghi đúng một dòng log mỗi lời gọi,
    chỉ gồm `name` và `version_ref`, không bao giờ ghi `content`: thiếu tên nào thì dòng đó là
    cảnh báo, vì Gateway đã gửi bộ prompt mà SQLBot phải lùi về bản mặc định cho phần thiếu.
    """
    found: dict[str, GatewayPrompt] = {}
    names = (SQL_PLAYBOOK_PROMPT, ANSWER_PERSONA_PROMPT)
    for prompt in prompts:
        if prompt.name in names and isinstance(prompt.content, str):
            found.setdefault(prompt.name, prompt)
    received = ', '.join(f'{p.name}@{p.version_ref}' for p in prompts)
    missing = [n for n in names if n not in found]
    if missing:
        SQLBotLogUtil.warning(f'gateway prompts missing {missing}; received: {received}')
    else:
        SQLBotLogUtil.info(f'gateway prompts received: {received}')
    playbook = found.get(SQL_PLAYBOOK_PROMPT)
    persona = found.get(ANSWER_PERSONA_PROMPT)
    return (playbook.content if playbook else '', persona.content if persona else '')
