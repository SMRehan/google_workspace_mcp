"""Unit tests for manage_chat_space (spaces create/setup/patch/delete/list)."""

import os
import sys
from unittest.mock import Mock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from core.utils import UserInputError
from gchat.chat_tools import manage_chat_space

EMAIL = "me@example.com"


def _unwrap(tool):
    fn = getattr(tool, "fn", tool)
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


def _space(name="spaces/S", display_name="Team", space_type="SPACE", **extra):
    space = {"name": name, "spaceType": space_type}
    if display_name is not None:
        space["displayName"] = display_name
    space.update(extra)
    return space


async def _run(service, **kwargs):
    return await _unwrap(manage_chat_space)(
        service=service, user_google_email=EMAIL, **kwargs
    )


# --- create ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_named_space_with_details():
    service = Mock()
    service.spaces().create().execute.return_value = _space(
        spaceDetails={"description": "d", "guidelines": "g"}, externalUserAllowed=True
    )
    result = await _run(
        service,
        action="create",
        display_name="Team",
        description="d",
        guidelines="g",
        external_user_allowed=True,
    )
    assert result.startswith("Created space.")
    assert "- Description: d" in result
    assert "- Guidelines: g" in result
    assert "- External users allowed: True" in result
    service.spaces().create.assert_called_with(
        body={
            "spaceType": "SPACE",
            "displayName": "Team",
            "spaceDetails": {"description": "d", "guidelines": "g"},
            "externalUserAllowed": True,
        }
    )


@pytest.mark.asyncio
async def test_create_requires_display_name():
    service = Mock()
    with pytest.raises(UserInputError, match="'display_name' is required"):
        await _run(service, action="create")
    service.spaces.assert_not_called()


@pytest.mark.asyncio
async def test_create_group_chat_points_to_setup():
    service = Mock()
    with pytest.raises(UserInputError, match="action='setup'"):
        await _run(service, action="create", space_type="GROUP_CHAT")
    service.spaces.assert_not_called()


@pytest.mark.asyncio
async def test_create_rejects_direct_message_type():
    service = Mock()
    with pytest.raises(UserInputError, match="Invalid space_type"):
        await _run(
            service, action="create", space_type="DIRECT_MESSAGE", display_name="x"
        )
    service.spaces.assert_not_called()


# --- setup -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_setup_space_with_members():
    service = Mock()
    service.spaces().setup().execute.return_value = _space()
    result = await _run(
        service,
        action="setup",
        display_name="Team",
        members=["ann@example.com", "users/2", " "],
    )
    assert "Set up space with 2 initial member(s)" in result
    service.spaces().setup.assert_called_with(
        body={
            "space": {"spaceType": "SPACE", "displayName": "Team"},
            "memberships": [
                {"member": {"name": "users/ann@example.com", "type": "HUMAN"}},
                {"member": {"name": "users/2", "type": "HUMAN"}},
            ],
        }
    )


@pytest.mark.asyncio
async def test_setup_group_chat_has_no_display_name():
    service = Mock()
    service.spaces().setup().execute.return_value = _space(
        display_name=None, space_type="GROUP_CHAT"
    )
    result = await _run(
        service,
        action="setup",
        space_type="group_chat",
        members=["a@example.com", "b@example.com"],
    )
    assert "(unnamed)" in result
    body = service.spaces().setup.call_args.kwargs["body"]
    assert body["space"] == {"spaceType": "GROUP_CHAT"}
    assert len(body["memberships"]) == 2


@pytest.mark.asyncio
async def test_setup_direct_message_sets_single_user_bot_dm_false():
    service = Mock()
    service.spaces().setup().execute.return_value = _space(
        display_name=None, space_type="DIRECT_MESSAGE"
    )
    await _run(
        service, action="setup", space_type="DIRECT_MESSAGE", members=["a@example.com"]
    )
    body = service.spaces().setup.call_args.kwargs["body"]
    assert body["space"] == {"spaceType": "DIRECT_MESSAGE", "singleUserBotDm": False}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs, match",
    [
        ({"space_type": "SPACE"}, "'display_name' is required"),
        (
            {"space_type": "GROUP_CHAT", "display_name": "x", "members": ["a"]},
            "not allowed",
        ),
        ({"space_type": "GROUP_CHAT"}, "at least one entry"),
        ({"space_type": "DIRECT_MESSAGE", "members": ["a", "b"]}, "exactly one entry"),
        (
            {"display_name": "x", "members": [f"u{i}@x.com" for i in range(50)]},
            "at most 49",
        ),
    ],
)
async def test_setup_validation(kwargs, match):
    service = Mock()
    with pytest.raises(UserInputError, match=match):
        await _run(service, action="setup", **kwargs)
    service.spaces.assert_not_called()


# --- update ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_display_name_and_history():
    service = Mock()
    service.spaces().patch().execute.return_value = _space(
        display_name="New", spaceHistoryState="HISTORY_OFF"
    )
    result = await _run(
        service,
        action="update",
        space_id="spaces/S",
        display_name="New",
        history_state="history_off",
    )
    assert "Updated space (fields: display_name, space_history_state)" in result
    assert "- History: HISTORY_OFF" in result
    service.spaces().patch.assert_called_with(
        name="spaces/S",
        updateMask="display_name,space_history_state",
        body={"displayName": "New", "spaceHistoryState": "HISTORY_OFF"},
    )
    service.spaces().get.assert_not_called()


@pytest.mark.asyncio
async def test_update_only_description_keeps_existing_guidelines():
    service = Mock()
    service.spaces().get().execute.return_value = _space(
        spaceDetails={"description": "old", "guidelines": "keep"}
    )
    service.spaces().patch().execute.return_value = _space()
    await _run(service, action="update", space_id="spaces/S", description="new")
    service.spaces().get.assert_called_with(name="spaces/S")
    service.spaces().patch.assert_called_with(
        name="spaces/S",
        updateMask="space_details",
        body={"spaceDetails": {"description": "new", "guidelines": "keep"}},
    )


@pytest.mark.asyncio
async def test_update_both_details_skips_fetch():
    service = Mock()
    service.spaces().patch().execute.return_value = _space()
    await _run(
        service, action="update", space_id="spaces/S", description="d", guidelines="g"
    )
    service.spaces().get.assert_not_called()
    service.spaces().patch.assert_called_with(
        name="spaces/S",
        updateMask="space_details",
        body={"spaceDetails": {"description": "d", "guidelines": "g"}},
    )


@pytest.mark.asyncio
async def test_update_requires_a_field():
    service = Mock()
    with pytest.raises(UserInputError, match="at least one of"):
        await _run(service, action="update", space_id="spaces/S")
    service.spaces().patch.assert_not_called()


@pytest.mark.asyncio
async def test_update_rejects_bad_history_state():
    service = Mock()
    with pytest.raises(UserInputError, match="Invalid history_state"):
        await _run(service, action="update", space_id="spaces/S", history_state="ON")
    service.spaces.assert_not_called()


# --- delete ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_space():
    service = Mock()
    result = await _run(service, action="delete", space_id="spaces/S")
    assert result.startswith("Deleted space spaces/S")
    service.spaces().delete.assert_called_with(name="spaces/S")


@pytest.mark.asyncio
async def test_delete_denied_under_permission_level(monkeypatch):
    import gchat.chat_helpers as helpers

    monkeypatch.setattr(helpers, "is_action_denied", lambda svc, act: act == "delete")
    service = Mock()
    with pytest.raises(UserInputError, match="not allowed"):
        await _run(service, action="delete", space_id="spaces/S")
    service.spaces.assert_not_called()


@pytest.mark.asyncio
async def test_delete_requires_space_id():
    service = Mock()
    with pytest.raises(UserInputError, match="'space_id' is required"):
        await _run(service, action="delete")
    service.spaces.assert_not_called()


# --- search ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_filters_display_names_case_insensitively_across_pages():
    service = Mock()
    service.spaces().list().execute.side_effect = [
        {
            "spaces": [_space("spaces/1", "Data Team"), _space("spaces/2", "Random")],
            "nextPageToken": "p2",
        },
        {
            "spaces": [
                _space("spaces/3", "DATA platform"),
                _space("spaces/4", None, "DIRECT_MESSAGE"),
            ]
        },
    ]
    result = await _run(service, action="search", query="data")
    assert "Found 2 space(s)" in result
    assert "searched 4 joined spaces" in result
    assert "- Data Team (ID: spaces/1, Type: SPACE)" in result
    assert "- DATA platform (ID: spaces/3, Type: SPACE)" in result
    assert "Random" not in result
    assert service.spaces().list.call_args_list[-1].kwargs == {
        "pageSize": 100,
        "pageToken": "p2",
    }


@pytest.mark.asyncio
async def test_search_with_space_type_filter_and_no_match():
    service = Mock()
    service.spaces().list().execute.return_value = {"spaces": [_space()]}
    result = await _run(
        service, action="search", query="zzz", space_type="SPACE", page_size=10
    )
    assert "No spaces with a display name containing 'zzz'" in result
    service.spaces().list.assert_called_with(pageSize=10, filter='spaceType = "SPACE"')


@pytest.mark.asyncio
async def test_search_requires_query():
    service = Mock()
    with pytest.raises(UserInputError, match="'query' is required"):
        await _run(service, action="search")
    service.spaces.assert_not_called()


@pytest.mark.asyncio
async def test_invalid_action():
    service = Mock()
    with pytest.raises(UserInputError, match="Invalid action"):
        await _run(service, action="archive")
    service.spaces.assert_not_called()
