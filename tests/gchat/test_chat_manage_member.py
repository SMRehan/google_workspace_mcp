"""Unit tests for manage_chat_member (spaces.members.*)."""

import os
import sys
from unittest.mock import Mock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from core.utils import UserInputError
from gchat.chat_tools import manage_chat_member

EMAIL = "me@example.com"


def _unwrap(tool):
    fn = getattr(tool, "fn", tool)
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


def _membership(name="spaces/S/members/1", role="ROLE_MEMBER", state="JOINED"):
    return {
        "name": name,
        "role": role,
        "state": state,
        "member": {"name": "users/1", "displayName": "Ann", "type": "HUMAN"},
    }


async def _run(service, **kwargs):
    return await _unwrap(manage_chat_member)(
        service=service, user_google_email=EMAIL, **kwargs
    )


@pytest.mark.asyncio
async def test_list_members_formats_and_pages():
    service = Mock()
    service.spaces().members().list().execute.return_value = {
        "memberships": [
            _membership(),
            _membership("spaces/S/members/2", "ROLE_MANAGER"),
        ],
        "nextPageToken": "tok",
    }
    result = await _run(service, action="list", space_id="spaces/S")
    assert "Found 2 member(s)" in result
    assert "Ann [users/1] (HUMAN) - role: ROLE_MEMBER, state: JOINED" in result
    assert "page_token='tok'" in result
    service.spaces().members().list.assert_called_with(parent="spaces/S", pageSize=100)


@pytest.mark.asyncio
async def test_list_members_builds_filter_and_forwards_paging():
    service = Mock()
    service.spaces().members().list().execute.return_value = {"memberships": []}
    result = await _run(
        service,
        action="list",
        space_id="spaces/S",
        role="role_manager",
        member_type="human",
        page_token="tok",
        show_invited=True,
        page_size=10,
    )
    assert "No members found" in result
    service.spaces().members().list.assert_called_with(
        parent="spaces/S",
        pageSize=10,
        filter='role = "ROLE_MANAGER" AND member.type = "HUMAN"',
        pageToken="tok",
        showInvited=True,
    )


@pytest.mark.asyncio
async def test_get_member_by_space_and_email_alias():
    service = Mock()
    service.spaces().members().get().execute.return_value = _membership()
    result = await _run(
        service, action="get", space_id="spaces/S", member="ann@example.com"
    )
    assert "Membership details" in result
    service.spaces().members().get.assert_called_with(
        name="spaces/S/members/ann@example.com"
    )


@pytest.mark.asyncio
async def test_get_member_by_membership_name():
    service = Mock()
    service.spaces().members().get().execute.return_value = _membership()
    await _run(service, action="get", membership_name="spaces/S/members/1")
    service.spaces().members().get.assert_called_with(name="spaces/S/members/1")


@pytest.mark.asyncio
async def test_add_member_defaults_to_human_and_single_call():
    service = Mock()
    service.spaces().members().create().execute.return_value = _membership()
    result = await _run(
        service, action="add", space_id="spaces/S", member="ann@example.com"
    )
    assert result.startswith("Added users/ann@example.com to space 'spaces/S'")
    service.spaces().members().create.assert_called_with(
        parent="spaces/S",
        body={"member": {"name": "users/ann@example.com", "type": "HUMAN"}},
    )
    service.spaces().members().patch.assert_not_called()


@pytest.mark.asyncio
async def test_add_member_reports_invited_state():
    service = Mock()
    service.spaces().members().create().execute.return_value = _membership(
        state="INVITED"
    )
    result = await _run(service, action="add", space_id="spaces/S", member="users/1")
    assert result.startswith("Invited users/1")


@pytest.mark.asyncio
async def test_add_manager_patches_role_after_create():
    service = Mock()
    service.spaces().members().create().execute.return_value = _membership()
    service.spaces().members().patch().execute.return_value = _membership(
        role="ROLE_MANAGER"
    )
    result = await _run(
        service,
        action="add",
        space_id="spaces/S",
        member="ann@example.com",
        role="ROLE_MANAGER",
    )
    assert "role: ROLE_MANAGER" in result
    service.spaces().members().patch.assert_called_with(
        name="spaces/S/members/1", updateMask="role", body={"role": "ROLE_MANAGER"}
    )


@pytest.mark.asyncio
async def test_add_app_member_is_bot():
    service = Mock()
    service.spaces().members().create().execute.return_value = _membership()
    await _run(service, action="add", space_id="spaces/S", member="app")
    service.spaces().members().create.assert_called_with(
        parent="spaces/S", body={"member": {"name": "users/app", "type": "BOT"}}
    )


@pytest.mark.asyncio
async def test_update_role():
    service = Mock()
    service.spaces().members().patch().execute.return_value = _membership(
        role="ROLE_MANAGER"
    )
    result = await _run(
        service,
        action="update_role",
        membership_name="spaces/S/members/1",
        role="ROLE_MANAGER",
    )
    assert "Updated role to ROLE_MANAGER" in result
    service.spaces().members().patch.assert_called_with(
        name="spaces/S/members/1", updateMask="role", body={"role": "ROLE_MANAGER"}
    )


@pytest.mark.asyncio
async def test_update_role_requires_role():
    service = Mock()
    with pytest.raises(UserInputError, match="'role' is required"):
        await _run(service, action="update_role", membership_name="spaces/S/members/1")
    service.spaces.assert_not_called()


@pytest.mark.asyncio
async def test_remove_member():
    service = Mock()
    result = await _run(service, action="remove", space_id="spaces/S", member="users/1")
    assert result == "Removed membership spaces/S/members/1."
    service.spaces().members().delete.assert_called_with(name="spaces/S/members/1")


@pytest.mark.asyncio
async def test_remove_denied_under_permission_level(monkeypatch):
    import gchat.chat_helpers as helpers

    monkeypatch.setattr(helpers, "is_action_denied", lambda svc, act: act == "remove")
    service = Mock()
    with pytest.raises(UserInputError, match="not allowed"):
        await _run(service, action="remove", membership_name="spaces/S/members/1")
    service.spaces.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs, match",
    [
        ({"action": "bogus"}, "Invalid action"),
        ({"action": "list"}, "'space_id' is required"),
        ({"action": "list", "space_id": "S"}, "Invalid space_id"),
        ({"action": "add", "space_id": "spaces/S"}, "'member' is required"),
        ({"action": "get"}, "either 'membership_name'"),
        (
            {"action": "get", "membership_name": "spaces/S/messages/M"},
            "Invalid membership_name",
        ),
        ({"action": "list", "space_id": "spaces/S", "role": "OWNER"}, "Invalid role"),
        (
            {"action": "list", "space_id": "spaces/S", "member_type": "GROUP"},
            "Invalid member_type",
        ),
    ],
)
async def test_input_validation(kwargs, match):
    service = Mock()
    with pytest.raises(UserInputError, match=match):
        await _run(service, **kwargs)
    service.spaces.assert_not_called()
