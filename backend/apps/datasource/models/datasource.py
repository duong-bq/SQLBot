from datetime import datetime
from typing import List, Optional, Union

from pydantic import BaseModel
from sqlalchemy import Column, Text, BigInteger, DateTime, Identity
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import SQLModel, Field

from apps.swagger.i18n import PLACEHOLDER_PREFIX


class DatasourceStatus:
    """Tập giá trị của cột ``core_datasource.status``.

    Trước đây cột này chỉ được ghi cứng ``"Success"`` và không nơi nào đọc. Luồng import excel bất
    đồng bộ làm nó có nghĩa thật: giữa lúc trả 202 và lúc worker xong, nguồn dữ liệu tồn tại nhưng
    chưa dùng được.

    Dữ liệu cũ có thể mang ``NULL`` (cột nullable, không có server default, và chỉ ``create_ds`` /
    ``update_ds`` từng ghi). Vì vậy MỌI phép lọc phải viết theo lối LOẠI TRỪ ``UNUSABLE`` — xem
    ``usable_ds_condition``. Lọc theo ``status == SUCCESS`` sẽ làm biến mất toàn bộ nguồn dữ liệu
    tạo bằng script hoặc bằng bản cũ.
    """

    SUCCESS = "Success"
    IMPORTING = "Importing"
    FAILED = "Failed"

    # Các trạng thái KHÔNG được phép đem ra hỏi đáp.
    UNUSABLE = (IMPORTING, FAILED)


class CoreDatasource(SQLModel, table=True):
    __tablename__ = "core_datasource"
    id: int = Field(sa_column=Column(BigInteger, Identity(always=True), nullable=False, primary_key=True))
    name: str = Field(max_length=128, nullable=False)
    description: str = Field(max_length=512, nullable=True)
    type: str = Field(max_length=64)
    type_name: str = Field(max_length=64, nullable=True)
    configuration: str = Field(sa_column=Column(Text))
    create_time: datetime = Field(sa_column=Column(DateTime(timezone=False), nullable=True))
    create_by: int = Field(sa_column=Column(BigInteger()))
    status: str = Field(max_length=64, nullable=True)
    num: str = Field(max_length=256, nullable=True)
    oid: int = Field(sa_column=Column(BigInteger()))
    table_relation: List = Field(sa_column=Column(JSONB, nullable=True))
    embedding: str = Field(sa_column=Column(Text, nullable=True))
    recommended_config: int = Field(sa_column=Column(BigInteger()))


class CoreTable(SQLModel, table=True):
    __tablename__ = "core_table"
    id: int = Field(sa_column=Column(BigInteger, Identity(always=True), nullable=False, primary_key=True))
    ds_id: int = Field(sa_column=Column(BigInteger()))
    checked: bool = Field(default=True)
    table_name: str = Field(sa_column=Column(Text))
    table_comment: str = Field(sa_column=Column(Text))
    custom_comment: str = Field(sa_column=Column(Text))
    embedding: str = Field(sa_column=Column(Text, nullable=True))


class DsRecommendedProblem(SQLModel, table=True):
    __tablename__ = "ds_recommended_problem"
    id: int = Field(sa_column=Column(BigInteger, Identity(always=True), nullable=False, primary_key=True))
    datasource_id: int = Field(sa_column=Column(BigInteger()))
    question: str = Field(sa_column=Column(Text))
    remark: str = Field(sa_column=Column(Text))
    sort: int = Field(sa_column=Column(BigInteger()))
    create_time: datetime = Field(sa_column=Column(DateTime(timezone=False), nullable=True))
    create_by: int = Field(sa_column=Column(BigInteger()))


class CoreField(SQLModel, table=True):
    __tablename__ = "core_field"
    id: int = Field(sa_column=Column(BigInteger, Identity(always=True), nullable=False, primary_key=True))
    ds_id: int = Field(sa_column=Column(BigInteger()))
    table_id: int = Field(sa_column=Column(BigInteger()))
    checked: bool = Field(default=True)
    field_name: str = Field(sa_column=Column(Text))
    field_type: str = Field(max_length=128, nullable=True)
    field_comment: str = Field(sa_column=Column(Text))
    custom_comment: str = Field(sa_column=Column(Text))
    field_index: int = Field(sa_column=Column(BigInteger()))


# datasource create obj
class CreateDatasource(BaseModel):
    """Body của ``POST /datasource/add``.

    ``configuration`` khai là ``Union[dict, str]`` chứ không phải ``DatasourceConf``: dạng chính thức
    là object lồng, nhưng nhánh chuỗi phải giữ lại cho giao diện web dev vốn tự mã hóa trước khi gửi.
    Khai thẳng ``DatasourceConf`` sẽ làm mọi request của giao diện đó thành 422. Việc kiểm tra tên
    field và ép kiểu của nhánh object nằm ở ``normalize_configuration``, chạy ngay đầu endpoint.

    Thứ tự trong Union có ý nghĩa: pydantic thử ``dict`` trước nên object giữ nguyên là dict, chuỗi
    rơi xuống nhánh sau — đảo lại sẽ khiến object bị ép thành chuỗi và mất kiểm tra.
    """
    id: int = None
    name: str = ''
    description: str = ''
    type: str = ''
    configuration: Union[dict, str] = ''
    create_time: Optional[datetime] = None
    create_by: int = 0
    status: str = ''
    num: str = ''
    oid: int = 1
    tables: List[CoreTable] = []
    recommended_config: int = 1


class RecommendedProblemResponse:
    def __init__(self, datasource_id, recommended_config, questions):
        self.datasource_id = datasource_id
        self.recommended_config = recommended_config
        self.questions = questions

    datasource_id: int = None
    recommended_config: int = None
    questions: str = None


class RecommendedProblemBase(BaseModel):
    datasource_id: int = None
    recommended_config: int = None
    problemInfo: List[DsRecommendedProblem] = []


class RecommendedProblemBaseChat:
    def __init__(self, content):
        self.content = content

    content: List[str] = []


# edit local saved table and fields
class TableObj(BaseModel):
    table: CoreTable = None
    fields: List[CoreField] = []


# datasource config info
class DatasourceConf(BaseModel):
    host: str = ''
    port: int = 0
    username: str = ''
    password: str = ''
    database: str = ''
    driver: str = ''
    extraJdbc: str = ''
    dbSchema: str = ''
    filename: str = ''
    sheets: List = ''
    mode: str = ''
    timeout: int = 30
    lowVersion: bool = False
    ssl: bool = False
    poolSize: int = 5

    def to_dict(self):
        return {
            "host": self.host,
            "port": self.port,
            "username": self.username,
            "password": self.password,
            "database": self.database,
            "driver": self.driver,
            "extraJdbc": self.extraJdbc,
            "dbSchema": self.dbSchema,
            "filename": self.filename,
            "sheets": self.sheets,
            "mode": self.mode,
            "timeout": self.timeout,
            "lowVersion": self.lowVersion,
            "ssl": self.ssl,
            "poolSize": self.poolSize
        }


class TableSchema:
    def __init__(self, attr1, attr2=None):
        self.tableName = attr1
        self.tableComment = attr2 if attr2 is None or isinstance(attr2, str) else attr2.decode("utf-8")

    tableName: str
    tableComment: str


class TableSchemaResponse(BaseModel):
    tableName: str = ''
    tableComment: str | None = ''


class ColumnSchema:
    def __init__(self, attr1, attr2, attr3):
        self.fieldName = attr1
        self.fieldType = attr2
        self.fieldComment = attr3 if attr3 is None or isinstance(attr3, str) else attr3.decode("utf-8")

    fieldName: str
    fieldType: str
    fieldComment: str


class ColumnSchemaResponse(BaseModel):
    fieldName: str | None = ''
    fieldType: str | None = ''
    fieldComment: str | None = ''


class RelationSchema:
    def __init__(self, attr1, attr2, attr3, attr4):
        self.srcTable = attr1
        self.srcColumn = attr2
        self.tgtTable = attr3
        self.tgtColumn = attr4

    srcTable: str
    srcColumn: str
    tgtTable: str
    tgtColumn: str


class TableAndFields:
    def __init__(self, schema, table, fields):
        self.schema = schema
        self.table = table
        self.fields = fields

    schema: str
    table: CoreTable
    fields: List[CoreField]


class FieldObj(BaseModel):
    fieldName: str | None


class PreviewResponse(BaseModel):
    fields: List | None = []
    data: List | None = []
    sql: str | None = ''


class FieldInfo(BaseModel):
    fieldName: object
    fieldType: str


class SheetFields(BaseModel):
    sheetName: str
    fields: List[FieldInfo]


class ImportRequest(BaseModel):
    filePath: str
    sheets: List[SheetFields]


class CreatedExcelTable(BaseModel):
    tableId: int
    tableName: str
    sheetName: str
    rows: int


class CreateFromExcelResponse(BaseModel):
    dsId: int
    name: str
    tables: List[CreatedExcelTable]


class CreateFromExcelUrlRequest(BaseModel):
    """Body của luồng import bất đồng bộ: hệ ngoài đưa ĐƯỜNG DẪN tới file, không đưa bytes.

    Vì sao không còn multipart: file nguồn nằm ở object storage của hệ ngoài, đẩy bytes qua đường
    HTTP giữa hai bên là khoảng thời gian dài nhất và dễ đứt nhất của cả luồng — mà nó lại nằm
    trong pha đồng bộ, nơi client đang giữ kết nối chờ. Chuyển sang URL ký sẵn thì pha đồng bộ chỉ
    còn vài phép kiểm chuỗi, còn việc tải nằm ở worker nền, nơi chậm bao lâu cũng không ai chờ.

    ``fileUrl`` là URL ký sẵn (presigned) — tức là một CREDENTIAL có hạn. Nó bị ghi xuống DB để
    worker dùng lại, nên tuyệt đối không đưa nguyên văn vào log hay vào thông điệp lỗi.

    ``sheetNames`` để trống nghĩa là lấy toàn bộ sheet. Khác bản multipart ở một điểm quan trọng:
    tên sheet KHÔNG còn được đối chiếu ở pha đồng bộ (lúc đó chưa có file trong tay), nên gõ sai
    tên sheet giờ về bằng callback thất bại chứ không phải 400.
    """

    fileUrl: str = Field(..., description=f"{PLACEHOLDER_PREFIX}ds_file_url")
    name: str = Field(..., description=f"{PLACEHOLDER_PREFIX}ds_name")
    sheetNames: List[str] = Field(default_factory=list, description=f"{PLACEHOLDER_PREFIX}ds_sheet_names")
    description: str = Field('', description=f"{PLACEHOLDER_PREFIX}ds_description")


class CreateFromExcelAcceptedResponse(BaseModel):
    """Kết quả pha đồng bộ của luồng import bất đồng bộ: đã NHẬN việc, chưa nạp xong.

    Không có danh sách bảng — lúc trả về chưa bảng nào tồn tại. ``dsId`` là thứ duy nhất cần giữ:
    nó cũng chính là ``externalId`` trong callback báo kết quả sau này.
    """

    dsId: int
    name: str
    status: str


class ExcelImportStatusResponse(BaseModel):
    """Trạng thái một lần import, cho hệ ngoài tự hỏi khi callback thất lạc.

    Callback chỉ bảo đảm gửi ÍT NHẤT một lần và có thể chết hẳn sau khi hết số lần thử; không có
    đường tra cứu chủ động thì hệ ngoài kẹt vĩnh viễn ở trạng thái "đang chờ".
    """

    dsId: int
    name: str
    status: str
    errorCode: str | None = None
    errorMessage: str | None = None
    tableCount: int = 0
