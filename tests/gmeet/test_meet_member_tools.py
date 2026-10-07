"""Unit tests for manage_meet_member: list/get/add/update_role/remove/batch_update
against a Mock Meet service, including member-name scoping and paging."""

import os
import sys
from unittest.mock import Mock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from core.utils import UserInputError
from gmeet.meet_tools import manage_meet_member

EMAIL = "user@example.com"
SPACE = "spaces/jQCFfuBOdN5z"


def _unwrap(tool):
    """Unwrap FunctionTool + decorators to the original async function."""
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


def _member(member_id="m1", email="ada@example.com", role="COHOST"):
    return {"name": f"{SPACE}/members/{member_id}", "email": email, "role": role}


def _service(members=None, next_page_token=None, single=None):
    response = {"members": members if members is not None else [_member()]}
    if next_page_token is not None:
        response["nextPageToken"] = next_page_token
    single = single or _member()
    service = Mock()
    members_api = service.spaces().members()
    members_api.list().execute = Mock(return_value=response)
    members_api.get().execute = Mock(return_value=single)
    members_api.create().execute = Mock(return_value=single)
    members_api.patch().execute = Mock(return_value=single)
    members_api.delete().execute = Mock(return_value={})
    members_api.batchUpdate().execute = Mock(return_value={"members": [single]})
    for method in ("list", "get", "create", "patch", "delete", "batchUpdate"):
        getattr(members_api, method).reset_mock()
    return service


def _members_api(service):
    return service.spaces().members()


async def _run(service, **kwargs):
    kwargs.setdefault("space_name", SPACE)
    return await _unwrap(manage_meet_member)(
        service=service, user_google_email=EMAIL, **kwargs
    )


class TestList:
    @pytest.mark.asyncio
    async def test_lists_members_of_the_space(self):
        service = _service()

        result = await _run(service, action="list")

        assert _members_api(service).list.call_args.kwargs == {"parent": SPACE}
        assert (
            "Members of spaces/jQCFfuBOdN5z for user@example.com (1 on this page)"
            in (result)
        )
        assert "- ada@example.com | role: COHOST | spaces/jQCFfuBOdN5z/members/m1" in (
            result
        )
        assert "Next page token" not in result

    @pytest.mark.asyncio
    async def test_normalizes_meeting_code_and_forwards_paging(self):
        service = _service(next_page_token="tok-2")

        result = await _run(
            service,
            action="list",
            space_name="abc-mnop-xyz",
            page_size=25,
            page_token="tok-1",
        )

        assert _members_api(service).list.call_args.kwargs == {
            "parent": "spaces/abc-mnop-xyz",
            "pageSize": 25,
            "pageToken": "tok-1",
        }
        assert "Next page token: tok-2" in result

    @pytest.mark.asyncio
    async def test_empty_page_with_more_behind_it(self):
        service = _service(members=[], next_page_token="tok-2")

        result = await _run(service, action="list")

        assert "No members found on this page, but more pages remain." in result
        assert "Next page token: tok-2" in result

    @pytest.mark.asyncio
    async def test_genuinely_empty_space(self):
        service = _service(members=[])

        result = await _run(service, action="list")

        assert "No members found." in result


class TestGet:
    @pytest.mark.asyncio
    async def test_scopes_a_bare_member_id_to_the_space(self):
        service = _service()

        result = await _run(service, action="get", member_name="m1")

        assert _members_api(service).get.call_args.kwargs == {
            "name": f"{SPACE}/members/m1"
        }
        assert "Member for user@example.com" in result

    @pytest.mark.asyncio
    async def test_accepts_a_full_member_name(self):
        service = _service()

        await _run(service, action="get", member_name="spaces/other/members/m9")

        assert _members_api(service).get.call_args.kwargs == {
            "name": "spaces/other/members/m9"
        }

    @pytest.mark.asyncio
    async def test_requires_member_name(self):
        service = _service()

        with pytest.raises(UserInputError, match="member_name"):
            await _run(service, action="get")

        _members_api(service).get.assert_not_called()


class TestAdd:
    @pytest.mark.asyncio
    async def test_adds_by_email_with_role(self):
        service = _service()

        result = await _run(
            service, action="add", email=" ada@example.com ", role="cohost"
        )

        assert _members_api(service).create.call_args.kwargs == {
            "parent": SPACE,
            "body": {"email": "ada@example.com", "role": "COHOST"},
        }
        assert "Member added to spaces/jQCFfuBOdN5z" in result

    @pytest.mark.asyncio
    async def test_role_is_optional(self):
        service = _service()

        await _run(service, action="add", email="ada@example.com")

        assert _members_api(service).create.call_args.kwargs["body"] == {
            "email": "ada@example.com"
        }

    @pytest.mark.asyncio
    async def test_requires_email(self):
        service = _service()

        with pytest.raises(UserInputError, match="'email' is required"):
            await _run(service, action="add")

        _members_api(service).create.assert_not_called()

    @pytest.mark.asyncio
    async def test_rejects_unknown_role(self):
        service = _service()

        with pytest.raises(UserInputError, match="Invalid role"):
            await _run(service, action="add", email="a@b.c", role="OWNER")

        _members_api(service).create.assert_not_called()


class TestUpdateRole:
    @pytest.mark.asyncio
    async def test_patches_role_with_a_role_mask(self):
        service = _service()

        result = await _run(
            service, action="update_role", member_name="m1", role="ROLE_UNSPECIFIED"
        )

        assert _members_api(service).patch.call_args.kwargs == {
            "name": f"{SPACE}/members/m1",
            "updateMask": "role",
            "body": {"role": "ROLE_UNSPECIFIED"},
        }
        assert "Member role updated for user@example.com" in result

    @pytest.mark.asyncio
    async def test_requires_role(self):
        service = _service()

        with pytest.raises(UserInputError, match="'role' is required"):
            await _run(service, action="update_role", member_name="m1")

        _members_api(service).patch.assert_not_called()


class TestRemove:
    @pytest.mark.asyncio
    async def test_deletes_the_member(self):
        service = _service()

        result = await _run(service, action="remove", member_name="m1")

        assert _members_api(service).delete.call_args.kwargs == {
            "name": f"{SPACE}/members/m1"
        }
        assert f"Removed member {SPACE}/members/m1" in result


class TestBatchUpdate:
    @pytest.mark.asyncio
    async def test_builds_one_request_per_member_with_role_masks(self):
        service = _service()

        result = await _run(
            service,
            action="batch_update",
            member_names=["m1", "spaces/jQCFfuBOdN5z/members/m2"],
            role="COHOST",
        )

        assert _members_api(service).batchUpdate.call_args.kwargs == {
            "parent": SPACE,
            "body": {
                "requests": [
                    {
                        "member": {"name": f"{SPACE}/members/m1", "role": "COHOST"},
                        "updateMask": "role",
                    },
                    {
                        "member": {"name": f"{SPACE}/members/m2", "role": "COHOST"},
                        "updateMask": "role",
                    },
                ],
                "updateMask": "role",
            },
        }
        assert "Updated 1 member role(s) in spaces/jQCFfuBOdN5z" in result

    @pytest.mark.asyncio
    async def test_requires_member_names(self):
        service = _service()

        with pytest.raises(UserInputError, match="member_names"):
            await _run(service, action="batch_update", role="COHOST")

        _members_api(service).batchUpdate.assert_not_called()

    @pytest.mark.asyncio
    async def test_requires_role(self):
        service = _service()

        with pytest.raises(UserInputError, match="'role' is required"):
            await _run(service, action="batch_update", member_names=["m1"])

    @pytest.mark.asyncio
    async def test_caps_at_500_members(self):
        service = _service()

        with pytest.raises(UserInputError, match="at most 500"):
            await _run(
                service,
                action="batch_update",
                member_names=[f"m{i}" for i in range(501)],
                role="COHOST",
            )

        _members_api(service).batchUpdate.assert_not_called()


class TestActionValidation:
    @pytest.mark.asyncio
    async def test_invalid_action(self):
        service = _service()

        with pytest.raises(UserInputError, match="Invalid action 'promote'"):
            await _run(service, action="promote")

    @pytest.mark.asyncio
    async def test_denied_action_under_granular_permissions(self):
        service = _service()

        with patch("gmeet.meet_tools.is_action_denied", return_value=True) as denied:
            with pytest.raises(UserInputError, match="not allowed under the current"):
                await _run(service, action="remove", member_name="m1")

        denied.assert_called_once_with("meet", "remove")
        _members_api(service).delete.assert_not_called()
