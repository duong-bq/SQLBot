"""Test phần kiểm body của ``PUT /ai-model-config/datasources`` (``apps/ai_model_config/api.py``)."""

# Import sqlbot_xpack TRƯỚC để gỡ vòng import lẫn nhau của upstream.
import sqlbot_xpack  # noqa: F401  isort:skip

import pytest

from apps.ai_model_config.api import _ds_ids
from apps.ai_model_config.errors import ModelConfigError


def test_ds_ids_bo_trung_va_sap_tang():
    assert _ds_ids([3, 1, 3]) == [1, 3]


@pytest.mark.parametrize("raw", [None, [], "1", [0], [-1], [1.5], [True], ["1"]])
def test_ds_ids_sai_thi_422(raw):
    with pytest.raises(ModelConfigError) as info:
        _ds_ids(raw)
    assert info.value.status == 422 and info.value.field == "ds_ids"
