"""Test bảng mapping actionType và hàm quy đổi giá trị thô sang enum."""

import pytest

from apps.hooks.constants import (
    IMPLEMENTED_ACTION_TYPES,
    SyncActionType,
    resolve_action_type,
)


def test_mapping_action_type_dung_so():
    assert SyncActionType.UNKNOWN == 0
    assert SyncActionType.AUTHORIZATION_SYNC == 1
    assert SyncActionType.USER_SYNC == 2
    assert SyncActionType.ORGANIZATION_SYNC == 3
    assert SyncActionType.DATASOURCE_SYNC == 4
    assert SyncActionType.KNOWLEDGE_SYNC == 5
    assert SyncActionType.DOCUMENT_SYNC == 6


def test_chi_authorization_sync_duoc_implement():
    assert SyncActionType.AUTHORIZATION_SYNC in IMPLEMENTED_ACTION_TYPES
    for action in (
        SyncActionType.USER_SYNC,
        SyncActionType.ORGANIZATION_SYNC,
        SyncActionType.DATASOURCE_SYNC,
        SyncActionType.KNOWLEDGE_SYNC,
        SyncActionType.DOCUMENT_SYNC,
    ):
        assert action not in IMPLEMENTED_ACTION_TYPES


def test_resolve_nhan_integer_hop_le():
    assert resolve_action_type(1) is SyncActionType.AUTHORIZATION_SYNC
    assert resolve_action_type(6) is SyncActionType.DOCUMENT_SYNC


@pytest.mark.parametrize("raw", ["1", 1.0, True, False, None, 0, 7, 99, -1, [], {}])
def test_resolve_tra_none_voi_gia_tri_khong_hop_le(raw):
    # "1" là string nên loại: spec bắt buộc integer.
    # True/False là bool (subclass của int) nên phải loại tường minh.
    # 0 là sentinel UNKNOWN nội bộ, SW không được gửi.
    assert resolve_action_type(raw) is None
