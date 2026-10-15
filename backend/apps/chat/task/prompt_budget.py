"""Thang rút gọn khối thông tin CSDL của prompt sinh SQL khi model từ chối vì vượt context.

Cùng hướng với fallback embedding (``DsEmbedder.embed``): không đếm token trước — SQLBot không biết
context của model phía sau LiteLLM — mà gửi bản đầy đủ, bị từ chối (400) thì bớt dần rồi gửi lại.

Các nấc áp dụng dồn, theo thứ tự trong ``SCHEMA_REDUCTION_STEPS``:

1. bỏ sample data;
2. bỏ chú thích cột của bảng kéo theo quan hệ;
3. bỏ hẳn bảng kéo theo quan hệ, cùng các dòng FK chạm tới chúng;
4. bỏ chú thích cột của các bảng được chọn (top embedding, hoặc mọi bảng khi embedding tắt/lỗi).

Mỗi nấc là hàm thuần nhận ``SchemaPrompt`` và trả bản mới, hoặc ``None`` khi không có gì để bỏ —
nấc đó bị nhảy qua mà không tốn một lần gọi LLM. Chú thích bảng được giữ ở mọi nấc.

Rút gọn chỉ đụng tới prompt: whitelist bảng của tầng kiểm tra AST giữ nguyên, nên SQL dùng một
bảng kéo theo đã bị bỏ khỏi prompt vẫn hợp lệ. Mọi nấc chỉ bớt thông tin trên M-Schema đã lọc quyền
SW, không nấc nào mở lại bảng hay cột ngoài quyền.
"""

from collections.abc import Callable
from dataclasses import dataclass, replace

from apps.ai_model_config.errors import _chain, classify_model_error
from apps.datasource.m_schema import MSchema


@dataclass(frozen=True)
class SchemaPrompt:
    """Phần prompt sinh SQL có thể rút gọn: M-Schema và sample data."""

    m_schema: MSchema
    sample_data: str = ''


class SchemaReductionExhausted(Exception):
    """Đã rút gọn hết các nấc mà model vẫn từ chối prompt.

    Cố ý không kế thừa ``SingleMessageError``: vòng thử lại của pha SQL bắt lớp đó, mà gọi lại với
    cùng prompt chỉ tốn thêm một lần bị từ chối.
    """

    def __init__(self, applied_steps: list[str]):
        """Ghi lại các nấc đã áp dụng để đưa vào thông điệp lỗi."""
        self.applied_steps = list(applied_steps)
        super().__init__(
            'Thông tin CSDL vượt quá context của model kể cả khi đã rút gọn tối đa ('
            + ', '.join(_STEP_LABELS.get(name, name) for name in self.applied_steps)
            + '). Hãy thu hẹp phạm vi bảng của nguồn dữ liệu hoặc dùng model có context lớn hơn.'
        )


def drop_sample_data(prompt: SchemaPrompt) -> SchemaPrompt | None:
    """Bỏ toàn bộ sample data."""
    if not prompt.sample_data:
        return None
    return replace(prompt, sample_data='')


def _strip_field_comments(prompt: SchemaPrompt, pulled: bool) -> SchemaPrompt | None:
    """Bỏ chú thích cột của các bảng có cờ ``pulled`` đúng bằng tham số."""
    tables = prompt.m_schema.tables
    if not any(t.pulled == pulled and t.has_field_comments for t in tables):
        return None
    tables = tuple(t.without_field_comments() if t.pulled == pulled else t for t in tables)
    return replace(prompt, m_schema=replace(prompt.m_schema, tables=tables))


def drop_pulled_comments(prompt: SchemaPrompt) -> SchemaPrompt | None:
    """Bỏ chú thích cột của các bảng kéo theo quan hệ."""
    return _strip_field_comments(prompt, pulled=True)


def drop_pulled_tables(prompt: SchemaPrompt) -> SchemaPrompt | None:
    """Bỏ các bảng kéo theo quan hệ và mọi dòng FK có một đầu là bảng bị bỏ.

    FK giữa hai bảng được chọn vẫn giữ: chúng là gợi ý JOIN rẻ nhất mà model còn dùng được.
    """
    m_schema = prompt.m_schema
    dropped = {t.name for t in m_schema.tables if t.pulled}
    if not dropped:
        return None
    tables = tuple(t for t in m_schema.tables if not t.pulled)
    foreign_keys = tuple(fk for fk in m_schema.foreign_keys if not dropped.intersection(fk.tables))
    return replace(prompt, m_schema=replace(m_schema, tables=tables, foreign_keys=foreign_keys))


def drop_selected_comments(prompt: SchemaPrompt) -> SchemaPrompt | None:
    """Bỏ chú thích cột của các bảng được chọn."""
    return _strip_field_comments(prompt, pulled=False)


SCHEMA_REDUCTION_STEPS: tuple[tuple[str, Callable[[SchemaPrompt], SchemaPrompt | None]], ...] = (
    ('drop_sample_data', drop_sample_data),
    ('drop_pulled_comments', drop_pulled_comments),
    ('drop_pulled_tables', drop_pulled_tables),
    ('drop_selected_comments', drop_selected_comments),
)

_STEP_LABELS = {
    'drop_sample_data': 'bỏ sample data',
    'drop_pulled_comments': 'bỏ chú thích cột của bảng kéo theo quan hệ',
    'drop_pulled_tables': 'bỏ bảng kéo theo quan hệ',
    'drop_selected_comments': 'bỏ chú thích cột của bảng được chọn',
}


def next_reduction(prompt: SchemaPrompt, start: int) -> tuple[int, str, SchemaPrompt] | None:
    """Tìm nấc đầu tiên có tác dụng tính từ vị trí ``start`` của thang.

    Trả ``(vị trí kế tiếp để tìm lần sau, tên nấc, prompt đã rút gọn)``; hết nấc thì trả ``None``.
    """
    for index in range(start, len(SCHEMA_REDUCTION_STEPS)):
        name, step = SCHEMA_REDUCTION_STEPS[index]
        reduced = step(prompt)
        if reduced is not None:
            return index + 1, name, reduced
    return None


def is_prompt_rejection(exc: BaseException) -> bool:
    """Lỗi có phải model từ chối nội dung prompt (400) chứ không phải lỗi khác của model.

    400 đã được phân loại (vd LiteLLM báo hết ngân sách) thì không phải do prompt, rút gọn không
    cứu được. Không soi chữ trong thông điệp để nhận "context length": mỗi backend (vLLM, LiteLLM,
    OpenAI) viết một kiểu; 400 do lý do khác thì cùng lắm tốn thêm vài lần gọi rồi vẫn báo lỗi.
    """
    import openai

    if classify_model_error(exc) is not None:
        return False
    return any(isinstance(item, openai.BadRequestError) for item in _chain(exc))
