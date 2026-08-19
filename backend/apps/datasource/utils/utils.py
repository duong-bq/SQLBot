import json

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad, pad
import base64

from fastapi import HTTPException

key = b'SQLBot1234567890'

def aes_encrypt(data):
    data = bytes(data,'utf-8')
    cipher = AES.new(key, AES.MODE_ECB)
    data = pad(data, AES.block_size)
    encrypt = cipher.encrypt(data)
    return base64.b64encode(encrypt)

def aes_decrypt(encrypted_data):
    encrypted_data = base64.b64decode(encrypted_data)
    cipher = AES.new(key, AES.MODE_ECB)
    text = cipher.decrypt(encrypted_data)
    decrypted_text = unpad(text, AES.block_size)
    return decrypted_text.decode('utf-8')


def validate_configuration_object(conf: dict, db_type: str | None = None) -> None:
    """Kiểm tra một ``configuration`` dạng object trước khi chấp nhận, ném 422 nếu sai.

    Lý do phải kiểm tay: ``DatasourceConf`` khai mọi field đều có giá trị mặc định và pydantic mặc
    định BỎ QUA key lạ, nên nếu chỉ ``model_validate`` thì gõ nhầm ``hostname`` thay vì ``host`` sẽ
    lọt qua im lặng rồi nổ tận tầng driver dưới dạng "connection is invalid" — đúng thứ mà việc đổi
    sang object lồng nhằm loại bỏ. Ở đây chặn key lạ trước, rồi mới để pydantic ép kiểu.

    Chỉ áp cho nhánh object. Nhánh chuỗi giữ nguyên hành vi cũ để không làm vỡ client đang chạy.

    Có ``db_type`` thì kiểm luôn ``extraJdbc`` theo bảng tham số của loại DB đó, để lỗi hiện ra lúc
    bấm lưu chứ không phải lúc mở kết nối.
    """
    from ..models.datasource import DatasourceConf

    allowed = set(DatasourceConf.model_fields.keys())
    unknown = sorted(set(conf.keys()) - allowed)
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown configuration field(s): {', '.join(unknown)}. "
                   f"Allowed: {', '.join(sorted(allowed))}")
    try:
        DatasourceConf(**conf)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f'Invalid configuration: {e}')
    if db_type:
        from apps.db.extra_params import validate_extra_params
        validate_extra_params(db_type, conf.get('extraJdbc'))


def normalize_configuration(configuration, db_type: str | None = None):
    """Quy trường ``configuration`` client gửi lên về dạng chuỗi mà toàn bộ code phía sau chờ đợi.

    Nhận ba dạng đầu vào:

    - **object lồng** (dạng chính thức, hệ thống tích hợp nên dùng) — được kiểm tra tên field và
      kiểu dữ liệu, sai thì 422 ngay tại biên chứ không để lỗi trôi xuống tầng driver;
    - **chuỗi JSON**;
    - **chuỗi đã mã hóa** mà giao diện web dev của SQLBot tự sinh.

    Hai dạng chuỗi phân biệt bằng cách thử parse JSON chứ không đoán theo tiền tố: đầu ra base64
    không bao giờ parse ra được một dict, nên phép thử không có ca nhập nhằng. Chuỗi không phải JSON
    trả về nguyên trạng — hàm này chỉ NỚI đầu vào, không đổi hành vi lỗi của dữ liệu rác.

    Vì sao vẫn quy về dạng đã mã hóa thay vì lưu JSON trần: mọi chỗ đọc cấu hình phía sau
    (``get_engine``, ``check_connection``, ``get_relations``...) đều gọi ``aes_decrypt``, và
    ``GET /datasource/list`` trả nguyên trường này về client nơi form sửa nguồn của giao diện dev gọi
    ``decrypted()`` lên nó. Đây là chuyện mã hóa lúc lưu, KHÔNG phải một lớp bảo mật — key nằm sẵn
    trong source cả hai phía — nên nó không được để lộ ra hợp đồng API. Client chỉ gửi object.
    """
    if isinstance(configuration, dict):
        validate_configuration_object(configuration, db_type)
        return aes_encrypt(json.dumps(configuration)).decode()
    if not configuration or not isinstance(configuration, str):
        return configuration
    text = configuration.strip()
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return configuration
    if not isinstance(parsed, dict):
        return configuration
    if db_type:
        # nhánh chuỗi JSON cố tình KHÔNG kiểm tên field như nhánh object (giữ nguyên độ nới cũ),
        # nhưng extraJdbc thì vẫn phải kiểm — sai ở đây là sai của người nhập, không phải của client
        from apps.db.extra_params import validate_extra_params
        validate_extra_params(db_type, parsed.get('extraJdbc'))
    return aes_encrypt(text).decode()
