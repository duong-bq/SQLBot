import { request } from '@/utils/request'

export const datasourceApi = {
  check: (data: any) => request.post('/datasource/check', data),
  check_by_id: (id: any) => request.get(`/datasource/check/${id}`),
  relationGet: (id: any) => request.get(`/table_relation/get/${id}`),
  relationSave: (dsId: any, data: any) => request.post(`/table_relation/save/${dsId}`, data),
  add: (data: any) => request.post('/datasource/add', data),
  importToDb: (data: any) => request.post('/datasource/importToDb', data),
  list: () => request.get('/datasource/list'),
  update: (data: any) => request.post('/datasource/update', data),
  delete: (id: number, name: string) => request.post(`/datasource/delete/${id}/${name}`),
  getTables: (id: number) => request.get(`/datasource/getTables/${id}`),
  getTablesByConf: (data: any) => request.post('/datasource/getTablesByConf', data),
  getFields: (id: number, table_name: string) =>
    request.get(`/datasource/getFields/${id}/${table_name}`),
  execSql: (id: number | string, sql: string) =>
    request.post(`/datasource/execSql/${id}`, { sql: sql }),
  chooseTables: (id: number, data: any) => request.post(`/datasource/chooseTables/${id}`, data),
  tableList: (id: number) => request.get(`/datasource/tableList/${id}`),
  // Chữ ký giữ nguyên dạng cũ (nhận object lọc) để chỗ gọi khỏi phải sửa; điều kiện lọc nay đi
  // bằng query param vì endpoint đã chuyển sang GET.
  fieldList: (id: number, data = { fieldName: '' }) =>
    request.get(`/datasource/fieldList/${id}`, {
      params: data.fieldName ? { fieldName: data.fieldName } : {},
    }),
  edit: (data: any) => request.post('/datasource/editLocalComment', data),
  // data vẫn là { table, fields } như trước, nhưng backend chỉ cần table.id.
  previewData: (id: number, data: any) =>
    request.get(`/datasource/previewData/${id}`, { params: { table_id: data?.table?.id } }),
  saveTable: (data: any) => request.post('/datasource/editTable', data),
  saveField: (data: any) => request.post('/datasource/editField', data),
  getDs: (id: number) => request.get(`/datasource/get/${id}`),
  cancelRequests: () => request.cancelRequests(),
  getSchema: (data: any) => request.post('/datasource/getSchemaByConf', data),
  syncFields: (id: number) => request.post(`/datasource/syncFields/${id}`),
  exportDsSchema: (id: any) =>
    request.get(`/datasource/exportDsSchema/${id}`, {
      responseType: 'blob',
      requestOptions: { customError: true },
    }),
}
