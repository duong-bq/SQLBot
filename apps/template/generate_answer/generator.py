from apps.template.template import get_base_template


def get_answer_template():
    """Lấy block prompt `answer` từ template gốc.

    Tách riêng khỏi `analysis` dù hai prompt trông giống nhau: `analysis` chỉ nhận fields + data nên
    chỉ nhận xét chung về một bảng số, còn `answer` nhận thêm question + sql để trả lời đúng thứ
    người dùng hỏi.
    """
    template = get_base_template()
    return template['template']['answer']
