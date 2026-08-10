import type { ChartAxis } from '@/views/chat/component/BaseChart.ts'

/**
 * Dựng cấu hình biểu đồ dạng bảng từ chính dữ liệu trả về, dùng khi bản ghi không có `record.chart`.
 *
 * Backend đã tắt pha sinh biểu đồ trên toàn hệ thống (finish_step mặc định dừng ở GENERATE_ANSWER)
 * nên `record.chart` luôn rỗng với các lượt hỏi mới, trong khi dữ liệu SQL vẫn về đủ. Không có cấu
 * hình thì khối biểu đồ bị ẩn hoàn toàn và người dùng không xem được bảng kết quả.
 *
 * Chỉ dựng đúng `columns` — mọi cột đều là cột thường, không phân trục x/y/series, vì bảng không cần
 * tới trục. Nhờ vậy `ChartComponent` vẽ được bảng mà không phải sửa gì.
 *
 * @param fields Danh sách tên cột lấy từ `record.data.fields`.
 * @returns Object cùng hình dạng với `JSON.parse(record.chart)`, hoặc `undefined` nếu không có cột
 *   nào — để phía gọi phân biệt được "không dựng nổi" với "bảng rỗng".
 */
export function buildTableChartFromFields(fields?: Array<string>):
  | { type: 'table'; title: string; columns: Array<ChartAxis>; axis: Record<string, never> }
  | undefined {
  if (!fields || fields.length === 0) {
    return undefined
  }
  return {
    type: 'table',
    title: '',
    columns: fields.map((field) => ({ name: field, value: field })),
    axis: {},
  }
}
