from apps.template.template import get_base_template


def get_route_template():
    """Lấy block prompt `route` (cổng định tuyến câu hỏi) từ template gốc.

    Tách riêng khỏi `sql` dù cùng nói về việc "có truy vấn được không": prompt `sql` nhận cả
    m-schema và phải sinh ra câu lệnh, còn prompt này chỉ nhận tên bảng và chỉ phân loại. Gộp lại
    thì cổng lại tốn đúng ngân sách ngữ cảnh mà nó sinh ra để tiết kiệm.
    """
    template = get_base_template()
    return template['template']['route']
