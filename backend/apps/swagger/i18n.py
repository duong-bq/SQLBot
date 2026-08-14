# Author: Junjun
# Date: 2025/12/11
# i18n.py
import json
from pathlib import Path
from typing import Dict

i18n_list = ["en", "zh"]

# placeholder prefix（trans key prefix）
PLACEHOLDER_PREFIX = "PLACEHOLDER_"

# default lang
DEFAULT_LANG = "en"

LOCALES_DIR = Path(__file__).parent / "locales"
_translations_cache: Dict[str, Dict[str, str]] = {}


def load_translation(lang: str) -> Dict[str, str]:
    """Load translations for the specified language from a JSON file"""
    if lang in _translations_cache:
        return _translations_cache[lang]

    file_path = LOCALES_DIR / f"{lang}.json"
    if not file_path.exists():
        if lang == DEFAULT_LANG:
            raise FileNotFoundError(f"Default language file not found: {file_path}")
        # If the non-default language is missing, fall back to the default language
        return load_translation(DEFAULT_LANG)

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError(f"Translation file {file_path} must be a JSON object")
            _translations_cache[lang] = data
            return data
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in {file_path}: {e}")


# group tags
#
# THỨ TỰ TRONG DANH SÁCH NÀY LÀ THỨ TỰ NHÓM HIỆN TRÊN SWAGGER UI. Nó được đưa vào mảng `tags` cấp
# gốc của tài liệu OpenAPI (main.py: generate_openapi_for_lang). Swagger UI xếp nhóm theo đúng thứ
# tự đó, rồi mới nối các tag KHÔNG có tên ở đây vào cuối — nên tag bị quên vừa rơi xuống đáy vừa
# mất phần mô tả nhóm. Thêm router có `tags=[...]` mới thì nhớ khai một dòng ở đây.
#
# Ba tag cuối danh sách hiện chưa có route nào dùng (`Audit` bị comment ở apps/api.py, hai tag kia
# thuộc sqlbot_xpack và phụ thuộc license). Cố ý GIỮ LẠI: xoá đi thì lúc bật lên chúng lại rơi
# xuống đáy đúng như trường hợp `login` trước đây.
tags_metadata = [
    {
        "name": "login",
        "description": f"{PLACEHOLDER_PREFIX}login_api"
    },
    {
        "name": "Data Q&A",
        "description": f"{PLACEHOLDER_PREFIX}data_qa"
    },
    {
        "name": "Datasource",
        "description": f"{PLACEHOLDER_PREFIX}ds_api"
    },
    {
        "name": "Table Relation",
        "description": f"{PLACEHOLDER_PREFIX}tr_api"
    },
    {
        "name": "AI Sync Hook",
        "description": f"{PLACEHOLDER_PREFIX}ai_sync_hook_api"
    },
    {
        "name": "system_user",
        "description": f"{PLACEHOLDER_PREFIX}system_user_api"
    },
    {
        "name": "system_ws",
        "description": f"{PLACEHOLDER_PREFIX}system_ws_api"
    },
    {
        "name": "system_model",
        "description": f"{PLACEHOLDER_PREFIX}system_model_api"
    },
    {
        "name": "system_assistant",
        "description": f"{PLACEHOLDER_PREFIX}system_assistant_api"
    },
    {
        "name": "system_embedded",
        "description": f"{PLACEHOLDER_PREFIX}system_embedded_api"
    },
    {
        "name": "Terminology",
        "description": f"{PLACEHOLDER_PREFIX}terminology_api"
    },
    {
        "name": "SQL Examples",
        "description": f"{PLACEHOLDER_PREFIX}data_training_api"
    },
    {
        "name": "Data Permission",
        "description": f"{PLACEHOLDER_PREFIX}per_api"
    },
    {
        "name": "System",
        "description": f"{PLACEHOLDER_PREFIX}system_setting_api"
    },
    {
        "name": "recommended problem",
        "description": f"{PLACEHOLDER_PREFIX}recommended_problem_api"
    },
    {
        "name": "System_variable",
        "description": f"{PLACEHOLDER_PREFIX}variable_api"
    },
    {
        "name": "Dashboard",
        "description": f"{PLACEHOLDER_PREFIX}db_api"
    },
    {
        "name": "mcp",
        "description": f"{PLACEHOLDER_PREFIX}mcp_api"
    },
    {
        "name": "system_authentication",
        "description": f"{PLACEHOLDER_PREFIX}system_authentication_api"
    },
    {
        "name": "CustomPrompt",
        "description": f"{PLACEHOLDER_PREFIX}custom_prompt_api"
    },
    {
        "name": "Audit",
        "description": f"{PLACEHOLDER_PREFIX}audit_api"
    }
]


def get_translation(lang: str) -> Dict[str, str]:
    return load_translation(lang)
