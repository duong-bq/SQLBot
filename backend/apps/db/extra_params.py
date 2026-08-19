"""Lớp dịch và kiểm tra cho ``extraJdbc`` — chuỗi tham số kết nối phụ người dùng nhập trên form.

Tên field là di sản của SQLBot upstream: ở đây không có JDBC nào cả, mọi kết nối đều đi qua driver
Python. Người tích hợp thường mang thói quen JDBC sang (``encrypt=true``, ``loginTimeout=30``), nên
module này nhận cả tên JDBC lẫn tên thật của driver, quy về tên thật rồi ép kiểu.

Vì sao cần ép kiểu chứ không chỉ ánh xạ tên: giá trị nhập trên form luôn là chuỗi, mà chuỗi rỗng-
khác-rỗng nào trong Python cũng là giá trị đúng. Không ép kiểu thì ``read_only=false`` truyền vào
driver dưới dạng ``"false"`` sẽ BẬT chế độ chỉ đọc — sai ngược ý người nhập mà không có lỗi nào báo.
Đây là loại hỏng im lặng, tệ hơn hẳn một lỗi 400.

Ba nhóm tên được xử lý khác nhau:

- **``params``** — tên hợp lệ, kèm kiểu để ép và (với enum) tập giá trị cho phép.
- **``aliases``** — tên theo thói quen JDBC, quy về tên thật; có thể kèm bảng dịch giá trị
  (``encrypt=true`` → ``encryption=require``).
- **``reserved``** — tên bị từ chối, kèm câu hướng dẫn. Gồm hai loại: tên hệ thống đã tự truyền
  (điền vào là lỗi "nhiều giá trị cho cùng tham số", hoặc tệ hơn là bị ghi đè im lặng), và tên phá
  hỏng giao kèo với tầng đọc kết quả — tầng đó duyệt dòng theo vị trí và tự đoán kiểu cho bytes, nên
  ``as_dict`` (dòng thành dict) và ``use_unicode=false`` (cột chuỗi thành bytes, chuỗi ngắn bị đoán
  nhầm thành số) đều làm dữ liệu sai lặng lẽ chứ không ném lỗi. ``local_infile`` thì mở đường đọc file.

Loại DB không khai spec (dm, redshift, hive, kingbase, es) chạy ở chế độ nới: chỉ tách cặp key-value
và giữ nguyên chuỗi, đúng như hành vi cũ. Chưa khảo sát đủ tham số của những driver đó để khóa
whitelist, mà khóa nhầm thì làm hỏng nguồn dữ liệu đang chạy.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Tuple
from urllib.parse import quote

from fastapi import HTTPException

from apps.db.constant import DB

_BOOL_TRUE = {'true', '1', 'yes', 'on'}
_BOOL_FALSE = {'false', '0', 'no', 'off'}


@dataclass(frozen=True)
class Param:
    """Một tham số hợp lệ: ``kind`` là 'str' | 'int' | 'bool' | 'enum', enum thì kèm ``choices``.

    ``emit`` chuẩn hóa giá trị trước khi giao cho driver, dùng khi driver kén cách viết mà người
    nhập thì không có lý do gì phải biết: FreeTDS nhận ``UTF-8`` nhưng chết với ``utf-8``, còn
    PyMySQL nhận ``utf8mb4`` chứ không hiểu tên IANA. Không có ``emit`` thì enum trả về bản
    lowercase, vì phần lớn enum còn lại (``encryption``, ``sslmode``) vốn viết thường.
    """
    kind: str
    choices: Tuple[str, ...] = ()
    emit: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Alias:
    """Tên theo thói quen JDBC. ``values`` dịch luôn giá trị khi hai bên không cùng bảng từ vựng."""
    target: str
    values: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Spec:
    """Bộ luật cho một loại DB. ``reserved`` ánh xạ tên bị cấm sang câu giải thích cho người dùng."""
    params: Dict[str, Param]
    aliases: Dict[str, Alias] = field(default_factory=dict)
    reserved: Dict[str, str] = field(default_factory=dict)


# Chỉ nhận họ UTF-8: đã đo trên MySQL 8, mọi bộ ký tự khác đều vỡ khi câu SQL có tiếng Việt —
# hoặc UnicodeEncodeError ngay tại client, hoặc rớt kết nối với thông điệp không đọc ra nguyên nhân.
_UTF8_MYSQL = Param('enum', ('utf8mb4', 'utf8', 'utf-8'),
                    {'utf8mb4': 'utf8mb4', 'utf8': 'utf8mb4', 'utf-8': 'utf8mb4'})

_MYSQL_PARAMS: Dict[str, Param] = {
    'charset': _UTF8_MYSQL,
    'collation': Param('str'),
    'sql_mode': Param('str'),
    'init_command': Param('str'),
    'program_name': Param('str'),
    'bind_address': Param('str'),
    'read_default_file': Param('str'),
    'read_default_group': Param('str'),
    'max_allowed_packet': Param('int'),
    'read_timeout': Param('int'),
    'write_timeout': Param('int'),
    'client_flag': Param('int'),
    'ssl_ca': Param('str'),
    'ssl_cert': Param('str'),
    'ssl_key': Param('str'),
    'ssl_key_password': Param('str'),
    'ssl_disabled': Param('bool'),
    'ssl_verify_cert': Param('bool'),
    'ssl_verify_identity': Param('bool'),
}

_MYSQL_RESERVED: Dict[str, str] = {
    'host': "use the 'host' field",
    'port': "use the 'port' field",
    'user': "use the 'username' field",
    'password': "use the 'username'/'password' fields",
    'passwd': "use the 'username'/'password' fields",
    'db': "use the 'database' field",
    'database': "use the 'database' field",
    'unix_socket': 'not supported, SQLBot connects over TCP',
    'compress': 'not supported, the MySQL driver rejects the connection when it is set',
    'connect_timeout': "use the 'timeout' field",
    'ssl': "use the 'ssl' field",
    'autocommit': 'transactions are managed by SQLBot',
    'server_timezone': "not supported; set the session time zone instead, "
                       "e.g. init_command=SET time_zone='+07:00'",
    'auto_reconnect': 'not supported; reconnects are handled by the connection pool',
    'use_unicode': 'not allowed, text columns would come back as bytes and be misread as numbers',
    'binary_prefix': 'not allowed, it changes how binary values are escaped',
    'cursorclass': 'not settable as text',
    'conv': 'not settable as text',
    'auth_plugin_map': 'not settable as text',
}

_MYSQL_ALIASES: Dict[str, Alias] = {
    'usessl': Alias('ssl'),
    'requiressl': Alias('ssl'),
    'connecttimeout': Alias('connect_timeout'),
    'sockettimeout': Alias('read_timeout'),
    # MySQL gọi bộ ký tự theo tên riêng của nó, không theo tên IANA mà JDBC dùng
    'characterencoding': Alias('charset', {'utf-8': 'utf8mb4', 'utf8': 'utf8mb4'}),
    'allowloadlocalinfile': Alias('local_infile'),
    'servertimezone': Alias('server_timezone'),
    'sessionvariables': Alias('init_command'),
    'usecompression': Alias('compress'),
    'maxallowedpacket': Alias('max_allowed_packet'),
    'autoreconnect': Alias('auto_reconnect'),
}

_SQLSERVER_SPEC = Spec(
    params={
        'login_timeout': Param('int'),
        # FreeTDS tra tên bộ ký tự qua iconv và phân biệt hoa thường: 'UTF-8' chạy, 'utf-8' rớt
        # kết nối. Người nhập không việc gì phải biết chuyện đó, nên nhận mọi cách viết rồi tự
        # chuẩn hóa. Chỉ giữ họ UTF-8 vì các bộ ký tự khác đều vỡ với tiếng Việt.
        'charset': Param('enum', ('utf-8', 'utf8'), {'utf-8': 'UTF-8', 'utf8': 'UTF-8'}),
        'appname': Param('str'),
        'read_only': Param('bool'),
        'use_datetime2': Param('bool'),
        'arraysize': Param('int'),
    },
    aliases={
        'encrypt': Alias('encryption', {'true': 'require', 'false': 'off'}),
        'trustservercertificate': Alias('trust_server_certificate'),
        'logintimeout': Alias('login_timeout'),
        'applicationname': Alias('appname'),
        'applicationintent': Alias('read_only', {'readonly': 'true', 'readwrite': 'false'}),
        'databasename': Alias('database'),
        'integratedsecurity': Alias('integrated_security'),
        'querytimeout': Alias('timeout'),
    },
    reserved={
        'server': "use the 'host' field",
        'host': "use the 'host' field",
        'port': "use the 'port' field",
        'user': "use the 'username' field",
        'password': "use the 'username'/'password' fields",
        'database': "use the 'database' field",
        'timeout': "use the 'timeout' field",
        'tds_version': "use the 'lowVersion' field",
        'as_dict': 'not allowed, it changes the row type SQLBot expects',
        'encryption': 'not supported, the driver ignores it and the connection stays unencrypted',
        'autocommit': 'transactions are managed by SQLBot',
        'conn_properties': 'not allowed, it would run arbitrary SQL on every connect',
        'trust_server_certificate': 'not needed, server certificates are not verified',
        'integrated_security': 'not supported, use SQL Server authentication',
    },
)

_PG_SPEC = Spec(
    params={
        'sslmode': Param('enum', ('disable', 'allow', 'prefer', 'require', 'verify-ca', 'verify-full')),
        'sslrootcert': Param('str'),
        'sslcert': Param('str'),
        'sslkey': Param('str'),
        'sslpassword': Param('str'),
        'application_name': Param('str'),
        'client_encoding': Param('str'),
        'keepalives': Param('int'),
        'keepalives_idle': Param('int'),
        'keepalives_interval': Param('int'),
        'keepalives_count': Param('int'),
        'target_session_attrs': Param('enum', ('any', 'read-write', 'read-only', 'primary',
                                               'standby', 'prefer-standby')),
        'gssencmode': Param('enum', ('disable', 'prefer', 'require')),
    },
    aliases={
        'ssl': Alias('sslmode', {'true': 'require', 'false': 'disable'}),
        'applicationname': Alias('application_name'),
        'logintimeout': Alias('connect_timeout'),
    },
    reserved={
        'host': "use the 'host' field",
        'hostaddr': "use the 'host' field",
        'port': "use the 'port' field",
        'user': "use the 'username' field",
        'password': "use the 'username'/'password' fields",
        'dbname': "use the 'database' field",
        'database': "use the 'database' field",
        'connect_timeout': "use the 'timeout' field",
        'options': "reserved for schema selection, use the 'dbSchema' field",
    },
)

_ORACLE_SPEC = Spec(
    params={
        'wallet_location': Param('str'),
        'wallet_password': Param('str'),
        'config_dir': Param('str'),
        'ssl_server_cert_dn': Param('str'),
        'ssl_server_dn_match': Param('bool'),
        'edition': Param('str'),
        'program': Param('str'),
        'machine': Param('str'),
        'terminal': Param('str'),
        'osuser': Param('str'),
        'driver_name': Param('str'),
        'connection_id_prefix': Param('str'),
        'events': Param('bool'),
        'externalauth': Param('bool'),
        'disable_oob': Param('bool'),
        'expire_time': Param('int'),
        'retry_count': Param('int'),
        'retry_delay': Param('int'),
        'tcp_connect_timeout': Param('int'),
        'stmtcachesize': Param('int'),
        'sdu': Param('int'),
    },
    aliases={
        'servicename': Alias('service_name'),
    },
    reserved={
        'dsn': 'built by SQLBot from the connection fields',
        'host': "use the 'host' field",
        'port': "use the 'port' field",
        'user': "use the 'username' field",
        'password': "use the 'username'/'password' fields",
        'service_name': "use the 'database' field together with mode=service_name",
        'sid': "use the 'database' field together with mode=sid",
        'mode': "use the 'mode' field",
        'protocol': 'built by SQLBot from the connection fields',
    },
)

# ClickHouse là ngoại lệ: dialect đọc protocol/endpoint từ query string của URL để dựng địa chỉ HTTP
# TRƯỚC khi driver nhận kwargs, nên tham số của nó bắt buộc phải nằm trong URL (xem build_extra_query).
_CK_SPEC = Spec(
    params={
        'protocol': Param('enum', ('http', 'https')),
        'endpoint': Param('str'),
        'verify': Param('bool'),
        'engine_reflection': Param('bool'),
    },
    reserved={
        'host': "use the 'host' field",
        'port': "use the 'port' field",
        'user': "use the 'username' field",
        'password': "use the 'username'/'password' fields",
        'database': "use the 'database' field",
    },
)


def _mysql_family_spec(extra_reserved: Dict[str, str]) -> Spec:
    """Dựng spec cho một DB nói giao thức MySQL, trừ bớt các tham số mà nhánh đó tự truyền sẵn."""
    params = {k: v for k, v in _MYSQL_PARAMS.items() if k not in extra_reserved}
    return Spec(params=params, aliases=dict(_MYSQL_ALIASES),
                reserved={**_MYSQL_RESERVED, **extra_reserved})


# Doris/StarRocks dùng chung driver với MySQL nhưng code tự truyền thêm read_timeout, nên tham số
# này chuyển từ nhóm hợp lệ sang nhóm bị cấm.
_DORIS_RESERVED = {'read_timeout': 'set by SQLBot for this datasource type'}

_SPECS: Dict[str, Spec] = {
    'mysql': _mysql_family_spec({}),
    'doris': _mysql_family_spec(_DORIS_RESERVED),
    'starrocks': _mysql_family_spec(_DORIS_RESERVED),
    'sqlserver': _SQLSERVER_SPEC,
    'pg': _PG_SPEC,
    'excel': _PG_SPEC,
    'oracle': _ORACLE_SPEC,
    'ck': _CK_SPEC,
}


def _spec_for(db_type: str) -> Spec | None:
    """Trả spec của loại DB, kèm các tham số bị cấm vì lý do an toàn khai trong ``DB.illegalParams``.

    Trả ``None`` cho loại chưa khai spec — gọi bên ngoài hiểu là chế độ nới.
    """
    if not db_type:
        return None
    spec = _SPECS.get(db_type.strip().lower())
    if spec is None:
        return None
    try:
        illegal = DB.get_db(db_type).illegalParams or []
    except ValueError:
        illegal = []
    if not illegal:
        return spec
    reserved = {**spec.reserved, **{name.lower(): 'not allowed for security reasons' for name in illegal}}
    return Spec(params={k: v for k, v in spec.params.items() if k not in reserved},
                aliases=spec.aliases, reserved=reserved)


def _split_pairs(raw: str):
    """Tách chuỗi ``k=v&k=v`` thành từng cặp, cắt ở dấu ``=`` ĐẦU TIÊN.

    Cắt ở dấu đầu tiên chứ không tách hết: giá trị hoàn toàn có thể chứa ``=`` (chuỗi base64, tham
    số lồng nhau), và bản cũ tách hết nên từ chối oan những giá trị đó.
    """
    for segment in raw.split('&'):
        segment = segment.strip()
        if not segment:
            continue
        key, sep, value = segment.partition('=')
        key, value = key.strip(), value.strip()
        if not sep or not key or not value:
            raise HTTPException(
                status_code=400,
                detail=f"Malformed extra parameter '{segment}': expected key=value")
        yield key, value


def _coerce(name: str, value: str, param: Param) -> Any:
    """Ép giá trị chuỗi về đúng kiểu Python mà driver chờ đợi, sai thì 400 kèm giá trị hợp lệ.

    Enum còn đi thêm một bước chuẩn hóa qua ``Param.emit`` — xem docstring của ``Param``.
    """
    if param.kind == 'bool':
        low = value.lower()
        if low in _BOOL_TRUE:
            return True
        if low in _BOOL_FALSE:
            return False
        raise HTTPException(
            status_code=400,
            detail=f"Extra parameter '{name}' must be a boolean (true/false), got '{value}'")
    if param.kind == 'int':
        try:
            return int(value)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Extra parameter '{name}' must be an integer, got '{value}'")
    if param.kind == 'enum':
        low = value.lower()
        if low not in param.choices:
            raise HTTPException(
                status_code=400,
                detail=f"Extra parameter '{name}' must be one of: "
                       f"{', '.join(param.choices)}; got '{value}'")
        return param.emit.get(low, low)
    return value


def parse_extra_params(db_type: str, raw: str | None) -> Dict[str, Any]:
    """Đọc ``extraJdbc`` thành dict tham số đã đúng tên và đúng kiểu để truyền thẳng cho driver.

    Ném 400 khi tên lạ, tên bị cấm, hoặc giá trị sai kiểu — cố tình chọn 400 chứ không phải 500 vì
    đây là lỗi dữ liệu người dùng nhập, và thông điệp phải đủ để họ tự sửa mà không cần hỏi ai.
    """
    if not raw:
        return {}
    spec = _spec_for(db_type)
    result: Dict[str, Any] = {}
    for key, value in _split_pairs(raw):
        if spec is None:  # loại DB chưa khai spec: giữ nguyên hành vi cũ
            result[key] = value
            continue
        low = key.lower()
        alias = spec.aliases.get(low)
        if alias is not None:
            value = alias.values.get(value.lower(), value)
            low = alias.target
        if low in spec.reserved:
            raise HTTPException(
                status_code=400,
                detail=f"Extra parameter '{key}' is not allowed: {spec.reserved[low]}")
        param = spec.params.get(low)
        if param is None:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown extra parameter '{key}' for {db_type}. "
                       f"Allowed: {', '.join(sorted(spec.params))}")
        result[low] = _coerce(low, value, param)
    return result


def build_extra_query(db_type: str, raw: str | None) -> str:
    """Dựng phần query string đã kiểm tra và escape để nối vào URL kết nối (chỉ ClickHouse còn dùng).

    Các loại DB khác truyền tham số thẳng cho driver dưới dạng keyword argument đã đúng kiểu; nhét
    vào URL thì giá trị lại thành chuỗi và phụ thuộc việc dialect có ép kiểu hộ hay không.
    """
    params = parse_extra_params(db_type, raw)
    if not params:
        return ''
    parts = []
    for name, value in params.items():
        if isinstance(value, bool):
            rendered = 'true' if value else 'false'
        else:
            rendered = str(value)
        parts.append(f"{quote(name, safe='')}={quote(rendered, safe='')}")
    return '&'.join(parts)


def validate_extra_params(db_type: str, raw: str | None) -> None:
    """Kiểm tra sớm ngay tại biên API để người dùng thấy lỗi lúc bấm lưu, không phải lúc kết nối."""
    parse_extra_params(db_type, raw)
