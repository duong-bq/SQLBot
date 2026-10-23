"""Test cho hai prompt người dùng sửa được qua AI Gateway: SQL Playbook và Answer Persona.

Gateway resolve bộ prompt theo kho của datasource và gửi kèm body `/chat/ask` dưới khoá `prompts`.
Các rủi ro test này canh:

- **Vắng prompt thì phải y như cũ.** Web UI và MCP không bao giờ gửi `prompts`; prompt dựng ra cho
  họ phải giữ đủ câu vai trò và bốn quy tắc giọng văn, chỉ là giờ chúng nằm trong persona mặc định.
- **Nội dung người dùng làm vỡ template.** Prompt là `str.format`; dấu `{}` do người dùng viết mà
  bị ghép vào chuỗi template thì `format` ném KeyError ngay giữa lượt hỏi.
- **Lộ nội dung prompt vào log.** Chỉ được log `name` và `version_ref`.
"""

# Import sqlbot_xpack TRƯỚC: apps.chat.* nằm trong một vòng import lẫn nhau, chỉ gỡ được khi xpack
# là module khởi động vòng đó.
import sqlbot_xpack  # noqa: F401  isort:skip

import asyncio

import pytest

from apps.chat.api import chat as chat_api
from apps.chat.models.chat_model import ChatAskQuestion
from apps.chat.models.chat_model import ChatQuestion
from apps.chat.utils import gateway_prompts
from apps.chat.utils.gateway_prompts import ANSWER_PERSONA_PROMPT
from apps.chat.utils.gateway_prompts import SQL_PLAYBOOK_PROMPT
from apps.chat.utils.gateway_prompts import GatewayPrompt
from apps.chat.utils.gateway_prompts import pick_gateway_prompts
from apps.template.template import get_prompt_default

DEFAULT_ROLE = 'Bạn là trợ lý hỏi dữ liệu thông minh.'
# Bốn quy tắc giọng văn đã chuyển từ <Rules> sang persona mặc định (D3).
STYLE_RULES = [
    'trả lời bằng đúng ngôn ngữ đó',
    'Trả lời thẳng vào câu hỏi ngay ở câu đầu tiên',
    'nêu bật những điểm đáng chú ý',
    'tối đa khoảng 3 đến 5 câu',
]
SECRET = 'NOI-DUNG-KHONG-DUOC-LOG'


def envelope(name, content, version_ref='v3', **extra):
    """Một phần tử `prompts` theo đúng khuôn Gateway gửi, cộng khoá tuỳ ý."""
    return {'name': name, 'version_ref': version_ref, 'prompt_type': 'text',
            'content': content, 'config': None, **extra}


def question(**fields) -> ChatQuestion:
    """ChatQuestion tối thiểu cho việc render prompt."""
    return ChatQuestion(chat_id=1, question='Doanh thu năm nay?', **fields)


def sql_slot(q: ChatQuestion):
    """Nội dung khe quy tắc bổ sung của pha SQL, hoặc None khi khe bị bỏ."""
    return q.sql_sys_question('pg').get('custom_prompt')


@pytest.fixture
def logs(monkeypatch):
    """Ghi lại mọi dòng log của module prompt Gateway."""
    lines = []
    monkeypatch.setattr(gateway_prompts.SQLBotLogUtil, 'info',
                        lambda msg, *a, **k: lines.append(('info', msg)))
    monkeypatch.setattr(gateway_prompts.SQLBotLogUtil, 'warning',
                        lambda msg, *a, **k: lines.append(('warning', msg)))
    return lines


class TestNhanPrompts:
    def test_body_nhan_prompts_co_khoa_la(self):
        """Phần tử có khoá lạ vẫn parse được; Gateway thêm trường mới không làm vỡ SQLBot."""
        body = ChatAskQuestion(chat_id=1, question='q', prompts=[
            envelope(SQL_PLAYBOOK_PROMPT, 'a', label='production', moi=1)])
        assert body.prompts[0].name == SQL_PLAYBOOK_PROMPT
        assert body.prompts[0].content == 'a'

    def test_body_khong_co_prompts(self):
        assert ChatAskQuestion(chat_id=1, question='q').prompts is None

    def test_tra_theo_ten_bo_qua_ten_la(self, logs):
        prompts = [GatewayPrompt(**envelope('[SQLBot] Query Generation', 'cu')),
                   GatewayPrompt(**envelope(ANSWER_PERSONA_PROMPT, 'persona')),
                   GatewayPrompt(**envelope(SQL_PLAYBOOK_PROMPT, 'playbook'))]
        assert pick_gateway_prompts(prompts) == ('playbook', 'persona')
        assert [level for level, _ in logs] == ['info']

    def test_thieu_ten_thi_mot_dong_canh_bao_khong_lo_noi_dung(self, logs):
        prompts = [GatewayPrompt(**envelope(SQL_PLAYBOOK_PROMPT, SECRET, version_ref='v7'))]
        assert pick_gateway_prompts(prompts) == (SECRET, '')
        assert len(logs) == 1
        level, msg = logs[0]
        assert level == 'warning'
        assert ANSWER_PERSONA_PROMPT in msg and 'v7' in msg
        assert SECRET not in msg

    def test_route_ask_ghi_prompts_vao_question(self, monkeypatch, logs):
        """`/chat/ask` chép hai prompt sang ChatQuestion trước khi vào pipeline."""
        captured = {}

        async def fake_resolve(session, chat_id, req):
            return question()

        async def fake_inner(session, user, q, *a, **k):
            captured['q'] = q
            return 'ok'

        monkeypatch.setattr(chat_api, '_resolve_chat_question', fake_resolve)
        monkeypatch.setattr(chat_api, 'question_answer_inner', fake_inner)
        body = ChatAskQuestion(chat_id=1, question='q', prompts=[
            envelope(SQL_PLAYBOOK_PROMPT, 'playbook'), envelope(ANSWER_PERSONA_PROMPT, 'persona')])
        asyncio.run(chat_api.ask.__wrapped__(None, None, body, None, resolved_chat_id=1))
        assert (captured['q'].sql_playbook, captured['q'].answer_persona) == ('playbook', 'persona')


class TestPersona:
    @pytest.mark.parametrize('render', ['answer_sys_question', 'answer_fallback_sys_question'])
    def test_persona_thay_cau_vai_tro(self, render):
        prompt = getattr(question(answer_persona='Em là trợ lý của Sở, gọi người dùng là anh/chị.'),
                         render)()
        assert 'Em là trợ lý của Sở' in prompt
        assert DEFAULT_ROLE not in prompt
        assert prompt.index('<persona>') < prompt.index('Em là trợ lý') < prompt.index('</persona>')

    @pytest.mark.parametrize('render', ['answer_sys_question', 'answer_fallback_sys_question'])
    @pytest.mark.parametrize('persona', ['', '  ', '<!-- chỉ có chú thích -->'])
    def test_vang_persona_dung_ban_mac_dinh(self, render, persona):
        prompt = getattr(question(answer_persona=persona), render)()
        assert DEFAULT_ROLE in prompt
        for rule in STYLE_RULES:
            assert rule in prompt

    def test_quy_tac_giong_van_khong_con_trong_rules(self):
        """Persona đã giữ bốn quy tắc; để thêm một bản trong <Rules> thì người dùng xoá không được."""
        prompt = question(answer_persona='Persona riêng.').answer_sys_question()
        rules = prompt[prompt.index('<Rules>'):prompt.index('</Rules>')]
        for rule in STYLE_RULES:
            assert rule not in rules
        assert 'theo <Rules>' in rules

    @pytest.mark.parametrize('render', ['answer_sys_question', 'answer_fallback_sys_question'])
    def test_dau_ngoac_nhon_khong_vo_template(self, render):
        prompt = getattr(question(answer_persona='Gọi {user} là {}'), render)()
        assert 'Gọi {user} là {}' in prompt


class TestSqlPlaybook:
    def test_playbook_thang_custom_prompt(self):
        slot = sql_slot(question(sql_playbook='Lọc năm bằng cột nam_bc.', custom_prompt='xpack'))
        assert 'Lọc năm bằng cột nam_bc.' in slot
        assert 'xpack' not in slot

    def test_playbook_rong_lui_ve_custom_prompt(self):
        assert 'xpack' in sql_slot(question(sql_playbook='<!-- x -->', custom_prompt='xpack'))

    def test_ca_hai_rong_thi_bo_khe(self):
        assert sql_slot(question()) is None

    def test_ban_mac_dinh_v0_khong_tao_khe(self):
        """v0 của playbook chỉ là chú thích hướng dẫn, nên gửi v0 thì prompt y như không gửi."""
        assert sql_slot(question(sql_playbook=get_prompt_default('sql_playbook.md'))) is None

    def test_dau_ngoac_nhon_khong_vo_template(self):
        assert 'WHERE x IN {1, 2}' in sql_slot(question(sql_playbook='WHERE x IN {1, 2}'))
