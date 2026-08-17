import asyncio
import io
import traceback
from typing import Optional, List

import orjson
import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Path
from fastapi.responses import StreamingResponse
from sqlalchemy import and_, select
from starlette.responses import JSONResponse

from apps.chat.curd.attachment import save_chat_attachment
from apps.chat.curd.chat import delete_chat_with_user, get_chart_data_with_user, get_chat_predict_data_with_user, \
    list_chats, get_chat_with_records, create_chat, get_chat_chart_data, get_chat_predict_data, \
    get_chat_with_records_with_data, get_chat_record_by_id, \
    format_json_data, format_json_list_data, get_chart_config, list_recent_questions, rename_chat_with_user, \
    get_chat_log_history, get_chart_data_with_user_live, resolve_chat_id
from apps.chat.models.chat_model import CreateChat, ChatRecord, RenameChat, ChatQuestion, AxisObj, QuickCommand, \
    ChatInfo, Chat, ChatFinishStep, ChatQuestionBase, SimpleChat, ChatItem
from apps.chat.task.llm import LLMService
from apps.chat.utils.attachment import fetch_docx_attachment, render_attachment_block
from apps.datasource.utils.excel_import import ExcelImportError
from apps.swagger.i18n import PLACEHOLDER_PREFIX
from apps.system.schemas.permission import SqlbotPermission, require_permissions
from common.audit.models.log_model import OperationType, OperationModules
from common.audit.schemas.logger_decorator import LogConfig, system_log
from common.core.config import settings
from common.core.deps import CurrentAssistant, SessionDep, CurrentUser, Trans
from common.utils.command_utils import parse_quick_command
from common.utils.data_format import DataFormat

router = APIRouter(tags=["Data Q&A"], prefix="/chat")


@router.get(
    "/list", response_model=List[ChatItem], summary=f"{PLACEHOLDER_PREFIX}get_chat_list"
)
async def chats(session: SessionDep, current_user: CurrentUser):
    """
    Lấy danh sách toàn bộ hội thoại (chat) của người dùng hiện tại trong workspace.

    Chỉ trả về hội thoại thuộc về ``current_user``. Dùng để hiển thị sidebar lịch sử hội thoại.

    Sắp xếp theo thời điểm hỏi đáp gần nhất (``latest_record_time``), không phải theo thời điểm tạo
    hội thoại: hội thoại cũ vừa được hỏi tiếp phải nổi lên đầu sidebar. Hội thoại chưa có lượt hỏi
    nào thì lấy chính ``create_time`` làm mốc. Vì thế response là ``ChatItem`` (có thêm trường
    ``latest_record_time``) chứ không phải ``Chat`` thuần.
    """
    return list_chats(session, current_user)


@router.get(
    "/{chart_id}", response_model=ChatInfo, summary=f"{PLACEHOLDER_PREFIX}get_chat"
)
async def get_chat(
    session: SessionDep,
    current_user: CurrentUser,
    chart_id: int,
    current_assistant: CurrentAssistant,
    trans: Trans,
):
    """
    Lấy chi tiết một hội thoại theo ``chart_id`` kèm toàn bộ các bản ghi (record) hỏi đáp bên trong.

    Mỗi record gồm câu hỏi, SQL đã sinh, cấu hình biểu đồ... Không kèm dữ liệu kết quả truy vấn
    (dùng ``/with_data`` nếu cần dữ liệu). ``current_assistant`` dùng khi truy cập qua trợ lý nhúng.
    """

    def inner():
        return get_chat_with_records(
            chart_id=chart_id,
            session=session,
            current_user=current_user,
            current_assistant=current_assistant,
            trans=trans,
        )

    return await asyncio.to_thread(inner)


@router.get(
    "/{chart_id}/with_data",
    response_model=ChatInfo,
    summary=f"{PLACEHOLDER_PREFIX}get_chat_with_data",
)
async def get_chat_with_data(
    session: SessionDep,
    current_user: CurrentUser,
    chart_id: int,
    current_assistant: CurrentAssistant,
):
    """
    Lấy chi tiết hội thoại theo ``chart_id`` kèm cả dữ liệu kết quả truy vấn của từng record.

    Giống ``GET /{chart_id}`` nhưng nạp thêm dữ liệu bảng/biểu đồ đã lưu để dựng lại giao diện đầy đủ.
    """

    def inner():
        return get_chat_with_records_with_data(
            chart_id=chart_id,
            session=session,
            current_user=current_user,
            current_assistant=current_assistant,
        )

    return await asyncio.to_thread(inner)


""" @router.get("/record/{chat_record_id}/data", summary=f"{PLACEHOLDER_PREFIX}get_chart_data")
async def chat_record_data(session: SessionDep, chat_record_id: int):
    def inner():
        data = get_chat_chart_data(chat_record_id=chat_record_id, session=session)
        return format_json_data(data)

    return await asyncio.to_thread(inner)


@router.get("/record/{chat_record_id}/predict_data", summary=f"{PLACEHOLDER_PREFIX}get_chart_predict_data")
async def chat_predict_data(session: SessionDep, chat_record_id: int):
    def inner():
        data = get_chat_predict_data(chat_record_id=chat_record_id, session=session)
        return format_json_list_data(data)

    return await asyncio.to_thread(inner) """


@router.get(
    "/record/{chat_record_id}/data", summary=f"{PLACEHOLDER_PREFIX}get_chart_data"
)
async def chat_record_data(
    session: SessionDep, current_user: CurrentUser, chat_record_id: int
):
    """
    Lấy dữ liệu bảng/biểu đồ đã lưu (cache) của một bản ghi hỏi đáp theo ``chat_record_id``.

    Trả về dữ liệu đã được lưu tại thời điểm sinh biểu đồ, không truy vấn lại database.
    Muốn dữ liệu mới nhất thì dùng ``/data_live``.
    """

    def inner():
        data = get_chart_data_with_user(
            chat_record_id=chat_record_id, session=session, current_user=current_user
        )
        return format_json_data(data)

    return await asyncio.to_thread(inner)


@router.get(
    "/record/{chat_record_id}/data_live",
    summary=f"{PLACEHOLDER_PREFIX}get_chart_data_live",
)
async def chat_record_data_live(
    session: SessionDep, current_user: CurrentUser, chat_record_id: int
):
    """
    Chạy lại SQL đã lưu của record để lấy dữ liệu **mới nhất (live)** từ database nghiệp vụ.

    Khác với ``/data`` (đọc dữ liệu cache), endpoint này thực thi lại truy vấn nên phản ánh
    dữ liệu hiện tại. Dùng khi người dùng muốn làm mới kết quả.
    """

    def inner():
        data = get_chart_data_with_user_live(
            chat_record_id=chat_record_id, session=session, current_user=current_user
        )
        return format_json_data(data)

    return await asyncio.to_thread(inner)


@router.get(
    "/record/{chat_record_id}/predict_data",
    summary=f"{PLACEHOLDER_PREFIX}get_chart_predict_data",
)
async def chat_predict_data(
    session: SessionDep, current_user: CurrentUser, chat_record_id: int
):
    """
    Lấy dữ liệu dự đoán (predict) đã lưu của một record theo ``chat_record_id``.

    Trả về chuỗi dữ liệu do LLM dự đoán (xu hướng tương lai) ứng với record đó, ở dạng danh sách.
    """

    def inner():
        data = get_chat_predict_data_with_user(
            chat_record_id=chat_record_id, session=session, current_user=current_user
        )
        return format_json_list_data(data)

    return await asyncio.to_thread(inner)


@router.get(
    "/record/{chat_record_id}/log", summary=f"{PLACEHOLDER_PREFIX}get_record_log"
)
async def chat_record_log(
    session: SessionDep, current_user: CurrentUser, chat_record_id: int
):
    """
    Lấy lịch sử log xử lý của một record: các bước gọi LLM (chọn datasource, sinh SQL, sinh biểu đồ...).

    Dùng để debug/hiển thị chi tiết quá trình hệ thống trả lời câu hỏi.
    """

    def inner():
        return get_chat_log_history(session, chat_record_id, current_user)

    return await asyncio.to_thread(inner)


@router.get(
    "/record/{chat_record_id}/usage", summary=f"{PLACEHOLDER_PREFIX}get_record_usage"
)
async def chat_record_usage(
    session: SessionDep, current_user: CurrentUser, chat_record_id: int
):
    """
    Lấy thống kê mức tiêu thụ token (token usage) của một record.

    Cùng nguồn với ``/log`` nhưng chỉ trả về phần thống kê token của các lần gọi LLM.
    """

    def inner():
        return get_chat_log_history(session, chat_record_id, current_user, True)

    return await asyncio.to_thread(inner)


@router.post("/rename", response_model=str, summary=f"{PLACEHOLDER_PREFIX}rename_chat")
@system_log(
    LogConfig(
        operation_type=OperationType.UPDATE,
        module=OperationModules.CHAT,
        resource_id_expr="chat.id",
    )
)
async def rename(session: SessionDep, current_user: CurrentUser, chat: RenameChat):
    """
    Đổi tên/tiêu đề (brief) của một hội thoại.

    Body ``RenameChat`` gồm ``id`` hội thoại và ``brief`` mới. Có ghi log thao tác (audit).
    """
    try:
        return rename_chat_with_user(
            session=session, current_user=current_user, rename_object=chat
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{chart_id}", response_model=str, summary=f"{PLACEHOLDER_PREFIX}delete_chat")
@system_log(LogConfig(
    operation_type=OperationType.DELETE,
    module=OperationModules.CHAT,
    resource_id_expr="chart_id",
    remark_expr="chat.brief"
))
async def delete(session: SessionDep, current_user: CurrentUser, chart_id: int, chat: SimpleChat):
    try:
        return delete_chat_with_user(
            session=session, current_user=current_user, chart_id=chart_id
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "/start", response_model=ChatInfo, summary=f"{PLACEHOLDER_PREFIX}start_chat"
)
@require_permissions(
    permission=SqlbotPermission(type="ds", keyExpression="create_chat_obj.datasource")
)
@system_log(
    LogConfig(
        operation_type=OperationType.CREATE,
        module=OperationModules.CHAT,
        result_id_expr="id",
    )
)
async def start_chat(
    session: SessionDep, current_user: CurrentUser, create_chat_obj: CreateChat
):
    """
    Tạo một hội thoại mới gắn với một datasource.

    Body ``CreateChat`` chứa ``datasource`` (id nguồn dữ liệu). Có kiểm tra quyền truy cập datasource
    (``require_permissions`` type='ds') trước khi tạo. Trả về ``ChatInfo`` của hội thoại vừa tạo.
    """
    try:
        return create_chat(session, current_user, create_chat_obj)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "/assistant/start",
    response_model=ChatInfo,
    summary=f"{PLACEHOLDER_PREFIX}assistant_start_chat",
)
@system_log(
    LogConfig(
        operation_type=OperationType.CREATE,
        module=OperationModules.CHAT,
        result_id_expr="id",
    )
)
async def start_chat(
    session: SessionDep,
    current_user: CurrentUser,
    current_assistant: CurrentAssistant,
    create_chat_obj: CreateChat = CreateChat(origin=2),
):
    """
    Tạo hội thoại mới từ **trợ lý nhúng (assistant/embedded)**.

    Giống ``/start`` nhưng gắn với ``current_assistant`` và đánh dấu ``origin=2`` (khởi tạo từ assistant
    thay vì giao diện web chính). Datasource có thể được truyền động qua ``create_chat_obj.datasource``.
    """
    try:
        return create_chat(
            session,
            current_user,
            create_chat_obj,
            create_chat_obj and create_chat_obj.datasource,
            current_assistant,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "/recommend_questions/{chat_record_id}",
    summary=f"{PLACEHOLDER_PREFIX}ask_recommend_questions",
)
async def ask_recommend_questions(
    session: SessionDep,
    current_user: CurrentUser,
    chat_record_id: int,
    current_assistant: CurrentAssistant,
    articles_number: Optional[int] = 4,
):
    """
    Sinh các câu hỏi gợi ý tiếp theo dựa trên một record đã có (streaming SSE).

    Dựa vào câu hỏi/ngữ cảnh của ``chat_record_id``, LLM đề xuất tối đa ``articles_number`` câu hỏi
    người dùng có thể hỏi kế tiếp. Trả về ``text/event-stream``; nếu record không tồn tại trả về mảng rỗng.

    Phải tự kiểm tra quyền sở hữu bằng tay (``chat.oid``/``chat.create_by``): route này không có
    ``require_permissions``, mà ``chat_record_id`` là số nguyên tuần tự nên chỉ cần đoán id là đọc
    được câu hỏi của workspace khác. Không đủ nếu chỉ kiểm tra record tồn tại.
    """

    def _return_empty():
        yield (
            "data:"
            + orjson.dumps({"content": "[]", "type": "recommended_question"}).decode()
            + "\n\n"
        )

    try:
        record = get_chat_record_by_id(session, chat_record_id)

        if not record:
            return StreamingResponse(_return_empty(), media_type="text/event-stream")

        chat = session.get(Chat, record.chat_id)
        if not chat:
            return StreamingResponse(_return_empty(), media_type="text/event-stream")
        if chat.oid != current_user.oid:
            raise Exception(
                f"Chat Record with id {chat_record_id} does not belong to current workspace"
            )
        if chat.create_by != current_user.id:
            raise Exception(
                f"Chat Record with id {chat_record_id} not Owned by the current user"
            )

        request_question = ChatQuestion(
            chat_id=record.chat_id, question=record.question if record.question else ""
        )

        llm_service = await LLMService.create(
            session, current_user, request_question, current_assistant, True
        )
        llm_service.set_record(record)
        llm_service.set_articles_number(articles_number)
        llm_service.run_recommend_questions_task_async()
    except Exception as e:
        traceback.print_exc()

        def _err(_e: Exception):
            yield (
                "data:"
                + orjson.dumps({"content": str(_e), "type": "error"}).decode()
                + "\n\n"
            )

        return StreamingResponse(_err(e), media_type="text/event-stream")

    return StreamingResponse(llm_service.await_result(), media_type="text/event-stream")


@router.get(
    "/recent_questions/{datasource_id}",
    response_model=List[str],
    summary=f"{PLACEHOLDER_PREFIX}get_recommend_questions",
)
# @require_permissions(permission=SqlbotPermission(type='ds', keyExpression="datasource_id"))
async def recommend_questions(
    session: SessionDep,
    current_user: CurrentUser,
    datasource_id: int = Path(..., description=f"{PLACEHOLDER_PREFIX}ds_id"),
):
    """
    Lấy danh sách câu hỏi gần đây của người dùng trên một datasource (``datasource_id``).

    Dùng để hiển thị gợi ý "câu hỏi thường hỏi" khi mở một nguồn dữ liệu. Trả về danh sách chuỗi câu hỏi.
    """
    return list_recent_questions(
        session=session, current_user=current_user, datasource_id=datasource_id
    )


def find_base_question(record_id: int, session: SessionDep):
    stmt = select(ChatRecord.question, ChatRecord.regenerate_record_id).where(
        and_(ChatRecord.id == record_id)
    )
    _record = session.execute(stmt).fetchone()
    if not _record:
        raise Exception(f"Cannot find base chat record")
    rec_question, rec_regenerate_record_id = _record
    if rec_regenerate_record_id:
        return find_base_question(rec_regenerate_record_id, session)
    else:
        return rec_question


async def resolve_chat_for_question(
    session: SessionDep,
    current_user: CurrentUser,
    request_question: ChatQuestionBase,
) -> int:
    """Dependency: quy ``chat_id`` client gửi lên (UUID hoặc id nội bộ) về id nội bộ, tạo hội thoại
    nếu UUID đó chưa từng xuất hiện.

    Phải là dependency chứ không phải code trong thân route, vì decorator
    ``require_permissions(type='chat')`` kiểm tra chat_id có nằm trong workspace hay không TRƯỚC khi
    thân route chạy — hội thoại chưa tồn tại sẽ bị chặn ngay ở đó, và một UUID thì không bao giờ
    khớp danh sách id nội bộ. FastAPI resolve dependency trước khi gọi route (tức trước cả decorator),
    nên id trả về từ đây chính là thứ decorator đem đi kiểm quyền.

    Khai lại ``request_question`` cùng tên và cùng kiểu với route để FastAPI dùng chung một body,
    không đòi client gửi hai lần.
    """
    return resolve_chat_id(
        session=session,
        current_user=current_user,
        chat_id=request_question.chat_id,
        datasource_id=request_question.datasource,
    )


def default_finish_step() -> ChatFinishStep:
    """Điểm dừng mặc định của ``POST /chat/question``, quyết định bởi ``GENERATE_CHART_ENABLED``.

    Đọc setting trong thân hàm chứ không đặt làm giá trị mặc định của tham số: giá trị mặc định
    được Python chốt lại một lần lúc import module, nên test nào monkeypatch ``settings`` sẽ không
    có tác dụng và biến sẽ trở thành thứ chỉ đổi được bằng cách sửa code.

    Chỉ chi phối endpoint này. Nhánh MCP tự truyền ``QUERY_DATA`` và không đi qua đây.
    """
    return (
        ChatFinishStep.GENERATE_CHART
        if settings.GENERATE_CHART_ENABLED
        else ChatFinishStep.GENERATE_ANSWER
    )


@router.post("/question", summary=f"{PLACEHOLDER_PREFIX}ask_question")
@require_permissions(
    permission=SqlbotPermission(type="chat", keyExpression="resolved_chat_id")
)
async def question_answer(
    session: SessionDep,
    current_user: CurrentUser,
    request_question: ChatQuestionBase,
    current_assistant: CurrentAssistant,
    resolved_chat_id: int = Depends(resolve_chat_for_question),
):
    """
    Endpoint cốt lõi: người dùng đặt câu hỏi, hệ thống sinh SQL và trả kết quả dạng streaming (SSE).

    Body ``ChatQuestionBase`` gồm ``chat_id``, ``question`` và ``datasource`` (chỉ cần khi ``chat_id``
    là UUID chưa tồn tại — xem ``resolve_chat_for_question``). Có kiểm tra quyền trên hội thoại
    (``require_permissions`` type='chat', đọc trên ``resolved_chat_id`` chứ không phải chat_id thô).
    Bật ``embedding=True`` để dùng RAG (thuật ngữ + SQL mẫu).
    Toàn bộ pipeline Text-to-SQL nằm trong ``LLMService.run_task`` (apps/chat/task/llm.py).
    Hỗ trợ quick command trong câu hỏi (regenerate / analysis / predict) — xem ``question_answer_inner``.

    Điểm dừng của pipeline do ``settings.GENERATE_CHART_ENABLED`` quyết định — xem
    ``default_finish_step``. Bật (mặc định) thì có thêm pha sinh biểu đồ chạy song song với pha
    answer; tắt thì dừng ngay sau câu trả lời bằng lời.

    Kèm ``domainCode`` (mã lĩnh vực phía SW) thì lượt hỏi bị giới hạn trong các bảng thuộc lĩnh
    vực đó theo quyền trong ``ai_user_permissions`` — chỉ có tác dụng với user được SW cấp quyền,
    user khác thì trường này bị bỏ qua. Không gửi trường này = hỏi trên toàn bộ lĩnh vực được cấp;
    gửi null/rỗng = ``HTTP 400`` (code ``invalid_domain_code``); gửi mã không có trong quyền của
    user → event SSE ``error`` kèm danh sách lĩnh vực hợp lệ. Giá trị được lưu vào record để
    ``/regenerate`` giữ nguyên ràng buộc.

    Kèm ``fileUrl`` (presigned URL của file .docx) thì tài liệu được tải và trích text NGAY TẠI
    ĐÂY, trước khi pipeline chạy — client chờ thêm vài giây trước khi stream mở. Nội dung trích
    trở thành ngữ cảnh của câu hỏi (xem ``question_for_prompt``) và được lưu vào bảng
    ``chat_attachment`` (trong ``stream_sql``, sau khi record của lượt được tạo). Lỗi ở bước này
    (URL sai host, hết hạn, file quá to, không phải docx...) trả về ``HTTP 400`` mang ``code`` là mã
    lỗi máy-đọc-được, KHÔNG phải event SSE ``error``: hỏng hóc được phát hiện trước khi stream mở
    nên status code vẫn nói được sự thật, và không có record nào được tạo.
    """
    # Phân biệt "không gửi domainCode" với "gửi null/rỗng": không gửi = hỏi trên toàn bộ lĩnh vực
    # được cấp (hợp lệ); đã gửi thì phải mang giá trị thật — null/rỗng gần như chắc chắn là bug
    # phía client (biến chưa gán) nên chặn ngay bằng HTTP 400 trước khi stream mở, thay vì im lặng
    # coi như không lọc. `model_fields_set` là cách duy nhất phân biệt hai ca này trong pydantic.
    domain_code = None
    if 'domainCode' in request_question.model_fields_set:
        domain_code = (request_question.domainCode or '').strip()
        if not domain_code:
            raise HTTPException(
                status_code=400,
                detail={"code": "invalid_domain_code",
                        "message": "domainCode đã gửi thì không được null/rỗng; muốn hỏi trên "
                                   "toàn bộ lĩnh vực thì bỏ hẳn trường này khỏi request."},
            )

    question = ChatQuestion(
        chat_id=resolved_chat_id,
        question=request_question.question,
        domain_code=domain_code,
    )
    if request_question.fileUrl:
        try:
            # fetch_docx_attachment là hàm đồng bộ (mạng + parse CPU) — phải đẩy ra thread để
            # không chặn event loop.
            attachment = await asyncio.to_thread(
                fetch_docx_attachment, request_question.fileUrl
            )
        except ExcelImportError as e:
            # Trả HTTP 400 chứ không phải event SSE: chưa gửi byte nào của stream nên status code
            # còn nói được sự thật. Dùng lại đúng khuôn của luồng excel (``_bad_request`` trong
            # apps/datasource/api/datasource.py) — cùng bộ mã lỗi thì phải cùng cách trả, kẻo phía
            # đối tác phải viết hai nhánh cho một hỏng hóc.
            raise HTTPException(
                status_code=400, detail={"code": e.error_code, "message": e.message}
            )
        question.attachment_filename = attachment.filename
        question.attachment_content = attachment.content
        question.attachment_truncated = attachment.truncated
        question.attached_doc = render_attachment_block(
            attachment.filename,
            attachment.content,
            attachment.truncated,
            max_chars=settings.CHAT_DOC_PROMPT_MAX_CHARS,
        )

    return await question_answer_inner(
        session,
        current_user,
        question,
        current_assistant,
        finish_step=default_finish_step(),
        embedding=True,
    )


async def question_answer_inner(
    session: SessionDep,
    current_user: CurrentUser,
    request_question: ChatQuestion,
    current_assistant: Optional[CurrentAssistant] = None,
    in_chat: bool = True,
    stream: bool = True,
    finish_step: ChatFinishStep = ChatFinishStep.GENERATE_ANSWER,
    embedding: bool = False,
    return_img: bool = True,
):
    """
    Hàm điều phối nội bộ cho việc trả lời câu hỏi (không phải route trực tiếp).

    Phân tích quick command trong câu hỏi và định tuyến tương ứng:
    - Không có lệnh → sinh SQL bình thường (``stream_sql``).
    - ``/regenerate`` → sinh lại SQL từ câu hỏi gốc của record.
    - ``/analysis`` hoặc ``/predict`` → chạy phân tích/dự đoán trên record (``analysis_or_predict``).
    Được dùng chung cho cả luồng chat trên web và luồng MCP (khác nhau qua ``in_chat``/``stream``).
    """
    try:
        command, text_before_command, record_id, warning_info = parse_quick_command(
            request_question.question
        )
        if command:
            # todo 对话界面下，暂不支持分析和预测，需要改造前端
            if in_chat and (
                command == QuickCommand.ANALYSIS or command == QuickCommand.PREDICT_DATA
            ):
                raise Exception(f"Command: {command.value} temporary not supported")

            if record_id is not None:
                # 排除analysis和predict
                stmt = (
                    select(
                        ChatRecord.id,
                        ChatRecord.chat_id,
                        ChatRecord.analysis_record_id,
                        ChatRecord.predict_record_id,
                        ChatRecord.regenerate_record_id,
                        ChatRecord.first_chat,
                    )
                    .where(and_(ChatRecord.id == record_id))
                    .order_by(ChatRecord.create_time.desc())
                )
                _record = session.execute(stmt).fetchone()
                if not _record:
                    raise Exception(f"Record id: {record_id} does not exist")

                (
                    rec_id,
                    rec_chat_id,
                    rec_analysis_record_id,
                    rec_predict_record_id,
                    rec_regenerate_record_id,
                    rec_first_chat,
                ) = _record

                if rec_chat_id != request_question.chat_id:
                    raise Exception(
                        f"Record id: {record_id} does not belong to this chat"
                    )
                if rec_first_chat:
                    raise Exception(
                        f"Record id: {record_id} does not support this operation"
                    )

                if rec_analysis_record_id:
                    raise Exception("Analysis record does not support this operation")
                if rec_predict_record_id:
                    raise Exception(
                        "Predict data record does not support this operation"
                    )

            else:  # get last record id
                stmt = (
                    select(
                        ChatRecord.id,
                        ChatRecord.chat_id,
                        ChatRecord.regenerate_record_id,
                    )
                    .where(
                        and_(
                            ChatRecord.chat_id == request_question.chat_id,
                            ChatRecord.first_chat == False,
                            ChatRecord.analysis_record_id.is_(None),
                            ChatRecord.predict_record_id.is_(None),
                        )
                    )
                    .order_by(ChatRecord.create_time.desc())
                    .limit(1)
                )
                _record = session.execute(stmt).fetchone()

                if not _record:
                    raise Exception(f"You have not ask any question")

                rec_id, rec_chat_id, rec_regenerate_record_id = _record

            # 没有指定的，就查询上一个
            if not rec_regenerate_record_id:
                rec_regenerate_record_id = rec_id

            # 针对已经是重新生成的提问，需要找到原来的提问是什么
            base_question_text = find_base_question(rec_regenerate_record_id, session)
            text_before_command = (
                text_before_command
                + ("\n" if text_before_command else "")
                + base_question_text
            )

            if command == QuickCommand.REGENERATE:
                request_question.question = text_before_command
                request_question.regenerate_record_id = rec_id
                # Kế thừa mã lĩnh vực của lượt bị sinh lại: /regenerate phải tái lập đúng phạm vi
                # quyền của lượt gốc, không được nới rộng chỉ vì request mới không gửi domainCode.
                if request_question.domain_code is None:
                    request_question.domain_code = session.execute(
                        select(ChatRecord.domain_code).where(ChatRecord.id == rec_id)
                    ).scalar()
                return await stream_sql(
                    session,
                    current_user,
                    request_question,
                    current_assistant,
                    in_chat,
                    stream,
                    finish_step,
                    embedding,
                    return_img,
                )

            elif command == QuickCommand.ANALYSIS:
                return await analysis_or_predict(
                    session,
                    current_user,
                    rec_id,
                    "analysis",
                    current_assistant,
                    in_chat,
                    stream,
                )

            elif command == QuickCommand.PREDICT_DATA:
                return await analysis_or_predict(
                    session,
                    current_user,
                    rec_id,
                    "predict",
                    current_assistant,
                    in_chat,
                    stream,
                )
            else:
                raise Exception(f"Unknown command: {command.value}")
        else:
            return await stream_sql(
                session,
                current_user,
                request_question,
                current_assistant,
                in_chat,
                stream,
                finish_step,
                embedding,
                return_img,
            )
    except Exception as e:
        traceback.print_exc()

        if stream:

            def _err(_e: Exception):
                if in_chat:
                    yield (
                        "data:"
                        + orjson.dumps({"content": str(_e), "type": "error"}).decode()
                        + "\n\n"
                    )
                else:
                    yield f"&#x274c; **ERROR:**\n"
                    yield f"> {str(_e)}\n"

            return StreamingResponse(_err(e), media_type="text/event-stream")
        else:
            return JSONResponse(
                content={"message": str(e)},
                status_code=500,
            )


async def stream_sql(
    session: SessionDep,
    current_user: CurrentUser,
    request_question: ChatQuestion,
    current_assistant: Optional[CurrentAssistant] = None,
    in_chat: bool = True,
    stream: bool = True,
    finish_step: ChatFinishStep = ChatFinishStep.GENERATE_ANSWER,
    embedding: bool = False,
    return_img: bool = True,
):
    """
    Hàm nội bộ khởi chạy pipeline sinh SQL và trả kết quả streaming (hoặc JSON nếu ``stream=False``).

    Khởi tạo ``LLMService``, lưu record câu hỏi, chạy task bất đồng bộ (``run_task_async``) rồi stream kết
    quả về client. Đây là lớp keo giữa endpoint HTTP và ``LLMService.run_task`` — nơi chứa toàn bộ pipeline
    thật (chọn nguồn dữ liệu → sinh SQL → thực thi → sinh biểu đồ).

    Tham số:
        session: Phiên DB (dependency injection). Dùng để tạo ``LLMService`` và lưu record câu hỏi ban đầu.
        current_user: Người dùng hiện tại. Quyết định workspace (``oid``), ngôn ngữ và phạm vi quyền
            (nguồn dữ liệu / lọc dữ liệu theo dòng) áp dụng suốt pipeline.
        request_question: DTO ``ChatQuestion`` chứa đầu vào — chủ yếu ``chat_id``, ``question`` và
            ``datasource_id`` (tùy chọn). Đây cũng là "cục tích lũy": pipeline sẽ nhồi thêm schema,
            thuật ngữ, SQL mẫu... vào chính object này trong lúc chạy.
        current_assistant: Trợ lý ngoài (``CurrentAssistant``) nếu gọi qua kênh assistant/MCP; ``None``
            là chat thường trong app. Chi phối cách lấy nguồn dữ liệu động và cách tính oid/ds_id.
        in_chat: Định dạng đầu ra. ``True`` = SSE cho UI web (mỗi mẩu là ``data:{...type...}\n\n`` với các
            ``type`` như sql-result/chart/finish); ``False`` = định dạng văn bản/markdown cho công cụ ngoài
            (MCP). Khi ``in_chat=True`` thì luôn ép ``stream=True``.
        stream: Kiểu trả về. ``True`` = ``StreamingResponse`` (trả dần); ``False`` = chạy hết rồi gom mẩu
            kết quả cuối và trả về một ``JSONResponse`` (dùng cho MCP đồng bộ). Lỗi cũng theo hai nhánh này.
        finish_step: "Núm vặn" điểm dừng của pipeline, tăng dần:
            ``GENERATE_SQL`` < ``QUERY_DATA`` < ``GENERATE_ANSWER`` < ``GENERATE_CHART``. Mặc định là
            ``GENERATE_ANSWER``: pha sinh biểu đồ đã bị tắt trên toàn hệ thống vì phía tích hợp chỉ
            dùng câu trả lời bằng lời, mà pha đó tốn thêm một lượt gọi LLM đắt đỏ. Muốn bật lại biểu
            đồ thì truyền tường minh ``GENERATE_CHART``. MCP dừng sớm hơn ở ``QUERY_DATA``.
            Lưu ý ``GENERATE_ANSWER`` đứng TRƯỚC ``GENERATE_CHART`` vì pha sinh câu trả lời chữ chạy
            song song với pha sinh biểu đồ, khởi động ngay khi có dữ liệu.
        embedding: Cờ đánh dấu bật RAG/embedding cho phiên, truyền tiếp vào ``LLMService.create``. Lưu ý:
            việc thực sự có dùng embedding để chọn nguồn dữ liệu/schema hay không còn phụ thuộc
            ``settings.TABLE_EMBEDDING_ENABLED`` và cấu hình từng nguồn dữ liệu; mặc định ``False``.
        return_img: Chỉ có ý nghĩa ở nhánh ngoài app (MCP, ``in_chat=False``). ``True`` = sau khi sinh biểu
            đồ sẽ render thành ảnh và trả về ``image_url``; ``False`` = bỏ qua bước tạo ảnh.

    Trả về:
        ``StreamingResponse`` (khi ``stream=True``) phát các mẩu SSE, hoặc ``JSONResponse`` (khi
        ``stream=False``) chứa kết quả cuối; nếu khởi tạo lỗi thì trả lỗi tương ứng theo từng nhánh.
    """
    try:
        llm_service = await LLMService.create(
            session,
            current_user,
            request_question,
            current_assistant,
            embedding=embedding,
        )
        llm_service.init_record(session=session)
        if getattr(request_question, "attachment_content", None):
            # Ghi bản gốc của tài liệu đính kèm, gắn với record VỪA tạo — phải nằm sau init_record
            # vì cần record_id, và trước run_task_async vì pha answer của chính lượt này có thể
            # đọc lại bảng chat_attachment khi dựng lịch sử.
            save_chat_attachment(
                session=session,
                chat_id=request_question.chat_id,
                record_id=llm_service.record.id,
                filename=request_question.attachment_filename,
                content=request_question.attachment_content,
                truncated=request_question.attachment_truncated,
            )
        llm_service.run_task_async(
            in_chat=in_chat,
            stream=stream,
            finish_step=finish_step,
            return_img=return_img,
        )
    except Exception as e:
        traceback.print_exc()

        if stream:

            def _err(_e: Exception):
                yield (
                    "data:"
                    + orjson.dumps({"content": str(_e), "type": "error"}).decode()
                    + "\n\n"
                )

            return StreamingResponse(_err(e), media_type="text/event-stream")
        else:
            return JSONResponse(
                content={"message": str(e)},
                status_code=500,
            )
    if stream:
        return StreamingResponse(
            llm_service.await_result(), media_type="text/event-stream"
        )
    else:
        res = llm_service.await_result()
        raw_data = {}
        for chunk in res:
            if chunk:
                raw_data = chunk
        status_code = 200
        if not raw_data.get("success"):
            status_code = 500

        return JSONResponse(
            content=raw_data,
            status_code=status_code,
        )


@router.post(
    "/record/{chat_record_id}/{action_type}",
    summary=f"{PLACEHOLDER_PREFIX}analysis_or_predict",
)
async def analysis_or_predict_question(
    session: SessionDep,
    current_user: CurrentUser,
    current_assistant: CurrentAssistant,
    chat_record_id: int,
    action_type: str = Path(
        ..., description=f"{PLACEHOLDER_PREFIX}analysis_or_predict_action_type"
    ),
):
    """
    Chạy **phân tích** hoặc **dự đoán** dữ liệu trên kết quả của một record đã có.

    ``action_type`` nhận ``'analysis'`` (LLM phân tích, nhận xét dữ liệu) hoặc ``'predict'``
    (dự đoán xu hướng tương lai). Kết quả trả về dạng streaming SSE.
    """
    return await analysis_or_predict(
        session, current_user, chat_record_id, action_type, current_assistant
    )


async def analysis_or_predict(
    session: SessionDep,
    current_user: CurrentUser,
    chat_record_id: int,
    action_type: str,
    current_assistant: CurrentAssistant,
    in_chat: bool = True,
    stream: bool = True,
):
    try:
        if action_type != "analysis" and action_type != "predict":
            raise Exception(f"Type {action_type} Not Found")
        record: ChatRecord | None = None

        stmt = select(
            ChatRecord.id,
            ChatRecord.question,
            ChatRecord.chat_id,
            ChatRecord.datasource,
            ChatRecord.engine_type,
            ChatRecord.ai_modal_id,
            ChatRecord.create_by,
            ChatRecord.chart,
            ChatRecord.data,
        ).where(and_(ChatRecord.id == chat_record_id))
        result = session.execute(stmt)
        for r in result:
            record = ChatRecord(
                id=r.id,
                question=r.question,
                chat_id=r.chat_id,
                datasource=r.datasource,
                engine_type=r.engine_type,
                ai_modal_id=r.ai_modal_id,
                create_by=r.create_by,
                chart=r.chart,
                data=r.data,
            )

        if not record:
            raise Exception(f"Chat record with id {chat_record_id} not found")

        chat = session.get(Chat, record.chat_id)
        if not chat:
            raise Exception(f"Chat with id {chat.id} not found")
        if chat.oid != current_user.oid:
            raise Exception(f"Chat Record with id {chat_record_id} does not belong to current workspace")
        if chat.create_by != current_user.id:
            raise Exception(f"Chat Record with id {chat_record_id} not Owned by the current user")

        if not record.chart:
            raise Exception(
                f"Chat record with id {chat_record_id} has not generated chart, do not support to analyze it"
            )

        request_question = ChatQuestion(
            chat_id=record.chat_id, question=record.question
        )

        llm_service = await LLMService.create(
            session, current_user, request_question, current_assistant
        )
        llm_service.run_analysis_or_predict_task_async(
            session, action_type, record, in_chat, stream
        )
    except Exception as e:
        traceback.print_exc()
        if stream:

            def _err(_e: Exception):
                if in_chat:
                    yield (
                        "data:"
                        + orjson.dumps({"content": str(_e), "type": "error"}).decode()
                        + "\n\n"
                    )
                else:
                    yield f"&#x274c; **ERROR:**\n"
                    yield f"> {str(_e)}\n"

            return StreamingResponse(_err(e), media_type="text/event-stream")
        else:
            return JSONResponse(
                content={"message": str(e)},
                status_code=500,
            )
    if stream:
        return StreamingResponse(
            llm_service.await_result(), media_type="text/event-stream"
        )
    else:
        res = llm_service.await_result()
        raw_data = {}
        for chunk in res:
            if chunk:
                raw_data = chunk
        status_code = 200
        if not raw_data.get("success"):
            status_code = 500

        return JSONResponse(
            content=raw_data,
            status_code=status_code,
        )


@router.get(
    "/record/{chat_record_id}/excel/export/{chat_id}",
    summary=f"{PLACEHOLDER_PREFIX}export_chart_data",
)
@system_log(
    LogConfig(
        operation_type=OperationType.EXPORT,
        module=OperationModules.CHAT,
        resource_id_expr="chat_id",
    )
)
async def export_excel(
    session: SessionDep,
    current_user: CurrentUser,
    chat_record_id: int,
    chat_id: int,
    trans: Trans,
):
    chat_record = session.get(ChatRecord, chat_record_id)
    if not chat_record:
        raise HTTPException(
            status_code=500, detail=f"ChatRecord with id {chat_record_id} not found"
        )
    if chat_record.create_by != current_user.id:
        raise HTTPException(
            status_code=500,
            detail=f"ChatRecord with id {chat_record_id} not Owned by the current user",
        )
    is_predict_data = chat_record.predict_record_id is not None

    _origin_data = format_json_data(
        get_chat_chart_data(chat_record_id=chat_record_id, session=session)
    )

    _base_field = _origin_data.get("fields")
    _data = _origin_data.get("data")

    if not _data:
        raise HTTPException(
            status_code=500, detail=trans("i18n_excel_export.data_is_empty")
        )

    chart_info = get_chart_config(session, chat_record_id)

    _title = chart_info.get("title") if chart_info.get("title") else "Excel"

    fields = []
    if chart_info.get("columns") and len(chart_info.get("columns")) > 0:
        for column in chart_info.get("columns"):
            fields.append(AxisObj(name=column.get("name"), value=column.get("value")))
    # 处理 axis
    if axis := chart_info.get("axis"):
        # 处理 x 轴
        if x_axis := axis.get("x"):
            if "name" in x_axis or "value" in x_axis:
                fields.append(
                    AxisObj(name=x_axis.get("name"), value=x_axis.get("value"))
                )

        # 处理 y 轴 - 兼容数组和对象格式
        if y_axis := axis.get("y"):
            if isinstance(y_axis, list):
                for column in y_axis:
                    if "name" in column or "value" in column:
                        fields.append(
                            AxisObj(name=column.get("name"), value=column.get("value"))
                        )
            elif isinstance(y_axis, dict) and ("name" in y_axis or "value" in y_axis):
                fields.append(
                    AxisObj(name=y_axis.get("name"), value=y_axis.get("value"))
                )

        # 处理 series
        if series := axis.get("series"):
            if "name" in series or "value" in series:
                fields.append(
                    AxisObj(name=series.get("name"), value=series.get("value"))
                )

    _predict_data = []
    if is_predict_data:
        _predict_data = format_json_list_data(
            get_chat_predict_data(chat_record_id=chat_record_id, session=session)
        )

    def inner():

        data_list = DataFormat.convert_large_numbers_in_object_array(
            obj_array=_data + _predict_data, int_threshold=1e11
        )

        md_data, _fields_list = DataFormat.convert_object_array_for_pandas(
            fields, data_list
        )

        # data, _fields_list, col_formats = LLMService.format_pd_data(fields, _data + _predict_data)

        df = pd.DataFrame(md_data, columns=_fields_list)

        buffer = io.BytesIO()

        with pd.ExcelWriter(
            buffer,
            engine="xlsxwriter",
            engine_kwargs={"options": {"strings_to_numbers": False}},
        ) as writer:
            df.to_excel(writer, sheet_name="Sheet1", index=False)

            # 获取 xlsxwriter 的工作簿和工作表对象
            # workbook = writer.book
            # worksheet = writer.sheets['Sheet1']
            #
            # for col_idx, fmt_type in col_formats.items():
            #     if fmt_type == 'text':
            #         worksheet.set_column(col_idx, col_idx, None, workbook.add_format({'num_format': '@'}))
            #     elif fmt_type == 'number':
            #         worksheet.set_column(col_idx, col_idx, None, workbook.add_format({'num_format': '0'}))

        buffer.seek(0)
        return io.BytesIO(buffer.getvalue())

    result = await asyncio.to_thread(inner)
    return StreamingResponse(
        result,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
