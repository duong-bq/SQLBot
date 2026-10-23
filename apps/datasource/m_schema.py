"""M-Schema dạng cấu trúc: dựng một lần, render ra chuỗi prompt, rút gọn được theo từng phần.

Trước đây M-Schema được nối thẳng thành chuỗi, nên muốn bỏ bớt một phần (chú thích cột, bảng kéo
theo quan hệ) chỉ còn cách parse ngược chuỗi. Giữ cấu trúc cho phép các nấc rút gọn prompt
(``apps/chat/task/prompt_budget.py``) làm việc trên dữ liệu, rồi render lại.

Bất biến: ``MSchema.render()`` sinh đúng từng byte chuỗi mà ``get_table_schema`` từng nối tay.
"""

from dataclasses import dataclass, replace
from typing import Any


@dataclass(frozen=True)
class MSchemaField:
    """Một cột đã in vào M-Schema (đã qua lọc quyền cột SW)."""

    name: str
    type: Any
    comment: str = ''

    def render(self) -> str:
        """Render cột thành ``(ten:kieu)`` hoặc ``(ten:kieu, chu thich)``."""
        if self.comment:
            return f"({self.name}:{self.type}, {self.comment})"
        return f"({self.name}:{self.type})"


@dataclass(frozen=True)
class MSchemaTable:
    """Một bảng trong M-Schema.

    ``title`` là nhãn in sau ``# Table:`` (có thể kèm tên schema, tuỳ loại datasource), còn
    ``name`` là tên bảng trần — chính là tên dùng trong whitelist AST và trong dòng FK.
    ``pulled`` đánh dấu bảng không được embedding chọn mà được kéo vào theo quan hệ.
    """

    name: str
    title: str
    comment: str = ''
    fields: tuple[MSchemaField, ...] = ()
    pulled: bool = False

    def render(self) -> str:
        """Render bảng thành khối ``# Table: ...`` kèm danh sách cột trong ngoặc vuông."""
        head = f"# Table: {self.title}"
        head += f", {self.comment}\n[\n" if self.comment else '\n[\n'
        return head + ",\n".join(f.render() for f in self.fields) + '\n]\n'

    @property
    def has_field_comments(self) -> bool:
        """Có ít nhất một cột mang chú thích hay không."""
        return any(f.comment for f in self.fields)

    def without_field_comments(self) -> 'MSchemaTable':
        """Bản sao bỏ chú thích của mọi cột; chú thích bảng vẫn giữ."""
        return replace(self, fields=tuple(replace(f, comment='') for f in self.fields))


@dataclass(frozen=True)
class MSchemaForeignKey:
    """Một dòng khối Foreign keys, kèm tên hai bảng ở hai đầu.

    Giữ tên bảng riêng thay vì tách lại từ ``line``: tên bảng có thể chứa dấu chấm.
    """

    line: str
    tables: tuple[str, str]


@dataclass(frozen=True)
class MSchema:
    """Toàn bộ M-Schema của một lượt hỏi.

    Thứ tự ``tables`` là thứ tự in: bảng embedding chọn trước, bảng kéo theo quan hệ sau.
    """

    db_name: Any
    tables: tuple[MSchemaTable, ...] = ()
    foreign_keys: tuple[MSchemaForeignKey, ...] = ()

    def render(self) -> str:
        """Render thành chuỗi đặt vào ``<m-schema>`` của prompt."""
        text = f"【DB_ID】 {self.db_name}\n【Schema】\n"
        text += ''.join(t.render() for t in self.tables)
        if self.foreign_keys:
            text += '【Foreign keys】\n'
            text += ''.join(f"{fk.line}\n" for fk in self.foreign_keys)
        return text

    @property
    def table_names(self) -> list[str]:
        """Tên các bảng theo thứ tự in."""
        return [t.name for t in self.tables]
