"""Unit tests for the manage_calendar_acl tool and its helpers."""

import os
import sys
from unittest.mock import Mock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from core.utils import UserInputError
from gcalendar.calendar_helpers import format_acl_rule
from gcalendar.calendar_tools import manage_calendar_acl

EMAIL = "user@example.com"
CAL = "abc123@group.calendar.google.com"


def _unwrap(tool):
    """Unwrap FunctionTool + decorators to the original async function."""
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


async def _manage(service, **kwargs):
    return await _unwrap(manage_calendar_acl)(
        service=service, user_google_email=EMAIL, **kwargs
    )


def _rule(rule_id="user:alice@example.com", role="writer", scope=None):
    if scope is None:
        scope_type, _, value = rule_id.partition(":")
        scope = {"type": scope_type}
        if value:
            scope["value"] = value
    return {"id": rule_id, "role": role, "scope": scope, "etag": '"1"'}


def _acl_service(items, next_page_token=None):
    response = {"items": items}
    if next_page_token is not None:
        response["nextPageToken"] = next_page_token
    service = Mock()
    service.acl().list().execute = Mock(return_value=response)
    return service


def test_format_acl_rule_shows_role_scope_and_id():
    assert (
        format_acl_rule(_rule())
        == "- writer: user alice@example.com (rule ID: user:alice@example.com)"
    )
    assert format_acl_rule(_rule("default", "reader")) == (
        "- reader: default (rule ID: default)"
    )


class TestValidation:
    @pytest.mark.asyncio
    async def test_invalid_action(self):
        with pytest.raises(UserInputError, match="Invalid action 'share'"):
            await _manage(Mock(), action="share")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("action", ["get", "update", "delete"])
    async def test_rule_id_required(self, action):
        with pytest.raises(UserInputError, match="rule_id is required"):
            await _manage(Mock(), action=action, calendar_id=CAL, role="reader")

    @pytest.mark.asyncio
    async def test_delete_respects_permission_denials(self):
        service = Mock()
        with patch(
            "gcalendar.calendar_tools.is_action_denied", return_value=True
        ) as denied:
            with pytest.raises(UserInputError, match="not allowed"):
                await _manage(
                    service, action="delete", calendar_id=CAL, rule_id="user:x@y.z"
                )
        denied.assert_called_once_with("calendar", "delete_acl")
        service.acl().delete.assert_not_called()


class TestList:
    @pytest.mark.asyncio
    async def test_lists_rules_and_forwards_paging(self):
        service = _acl_service(
            [_rule(), _rule("user:user@example.com", "owner")], next_page_token="n1"
        )

        result = await _manage(
            service,
            action="list",
            calendar_id=CAL,
            max_results=5,
            page_token="p0",
            show_deleted=True,
        )

        assert service.acl().list.call_args.kwargs == {
            "calendarId": CAL,
            "maxResults": 5,
            "pageToken": "p0",
            "showDeleted": True,
        }
        assert "Successfully listed 2 sharing rules" in result
        assert "writer: user alice@example.com" in result
        assert "owner: user user@example.com" in result
        assert "Next page token: n1" in result

    @pytest.mark.asyncio
    async def test_default_request_has_no_optional_parameters(self):
        service = _acl_service([_rule()])
        await _manage(service, action="list")
        assert service.acl().list.call_args.kwargs == {"calendarId": "primary"}

    @pytest.mark.asyncio
    async def test_empty_page_with_more_behind_it(self):
        service = _acl_service([], next_page_token="n2")
        result = await _manage(service, action="list", calendar_id=CAL)
        assert "No sharing rules found" not in result
        assert "Next page token: n2" in result

    @pytest.mark.asyncio
    async def test_no_rules(self):
        service = _acl_service([])
        result = await _manage(service, action="list", calendar_id=CAL)
        assert "No sharing rules found" in result

    @pytest.mark.asyncio
    async def test_rejects_non_positive_max_results(self):
        with pytest.raises(UserInputError, match="positive"):
            await _manage(Mock(), action="list", max_results=0)


class TestGet:
    @pytest.mark.asyncio
    async def test_fetches_one_rule(self):
        service = Mock()
        service.acl().get().execute = Mock(return_value=_rule())
        result = await _manage(
            service, action="get", calendar_id=CAL, rule_id="user:alice@example.com"
        )
        assert service.acl().get.call_args.kwargs == {
            "calendarId": CAL,
            "ruleId": "user:alice@example.com",
        }
        assert "writer: user alice@example.com" in result


class TestInsert:
    @pytest.mark.asyncio
    async def test_shares_with_a_user_and_forwards_notifications_flag(self):
        service = Mock()
        service.acl().insert().execute = Mock(return_value=_rule())

        result = await _manage(
            service,
            action="insert",
            calendar_id=CAL,
            scope_type="user",
            scope_value=" alice@example.com ",
            role="writer",
            send_notifications=False,
        )

        assert service.acl().insert.call_args.kwargs == {
            "calendarId": CAL,
            "body": {
                "role": "writer",
                "scope": {"type": "user", "value": "alice@example.com"},
            },
            "sendNotifications": False,
        }
        assert "Successfully shared calendar" in result
        assert "rule ID: user:alice@example.com" in result

    @pytest.mark.asyncio
    async def test_notifications_flag_omitted_when_not_given(self):
        service = Mock()
        service.acl().insert().execute = Mock(return_value=_rule())
        await _manage(
            service,
            action="insert",
            calendar_id=CAL,
            scope_type="domain",
            scope_value="example.com",
            role="reader",
        )
        kwargs = service.acl().insert.call_args.kwargs
        assert "sendNotifications" not in kwargs
        assert kwargs["body"]["scope"] == {"type": "domain", "value": "example.com"}

    @pytest.mark.asyncio
    async def test_public_default_scope_takes_no_value(self):
        service = Mock()
        service.acl().insert().execute = Mock(return_value=_rule("default", "reader"))
        await _manage(
            service,
            action="insert",
            calendar_id=CAL,
            scope_type="default",
            role="reader",
        )
        assert service.acl().insert.call_args.kwargs["body"] == {
            "role": "reader",
            "scope": {"type": "default"},
        }

    @pytest.mark.asyncio
    async def test_public_scope_rejects_value_and_write_roles(self):
        with pytest.raises(UserInputError, match="scope_value must be omitted"):
            await _manage(
                Mock(),
                action="insert",
                scope_type="default",
                scope_value="x",
                role="reader",
            )
        with pytest.raises(UserInputError, match="Public access"):
            await _manage(Mock(), action="insert", scope_type="default", role="writer")

    @pytest.mark.asyncio
    async def test_validates_scope_type_value_and_role(self):
        with pytest.raises(UserInputError, match="Invalid scope_type"):
            await _manage(
                Mock(),
                action="insert",
                scope_type="team",
                scope_value="x",
                role="reader",
            )
        with pytest.raises(UserInputError, match="scope_value is required"):
            await _manage(Mock(), action="insert", scope_type="group", role="reader")
        with pytest.raises(UserInputError, match="Invalid role 'admin'"):
            await _manage(
                Mock(),
                action="insert",
                scope_type="user",
                scope_value="x",
                role="admin",
            )


class TestUpdate:
    @pytest.mark.asyncio
    async def test_patches_the_role(self):
        service = Mock()
        service.acl().patch().execute = Mock(return_value=_rule(role="reader"))

        result = await _manage(
            service,
            action="update",
            calendar_id=CAL,
            rule_id="user:alice@example.com",
            role="reader",
            send_notifications=True,
        )

        assert service.acl().patch.call_args.kwargs == {
            "calendarId": CAL,
            "ruleId": "user:alice@example.com",
            "body": {"role": "reader"},
            "sendNotifications": True,
        }
        assert "Successfully updated sharing rule" in result
        assert "reader: user alice@example.com" in result

    @pytest.mark.asyncio
    async def test_requires_a_valid_role(self):
        with pytest.raises(UserInputError, match="Invalid role None"):
            await _manage(Mock(), action="update", rule_id="user:alice@example.com")

    @pytest.mark.asyncio
    async def test_refuses_to_demote_the_users_own_rule(self):
        service = Mock()
        with pytest.raises(UserInputError, match="own access"):
            await _manage(
                service,
                action="update",
                calendar_id=CAL,
                rule_id=f"user:{EMAIL.upper()}",
                role="reader",
            )
        service.acl().patch.assert_not_called()

    @pytest.mark.asyncio
    async def test_reaffirming_own_owner_role_is_allowed(self):
        service = Mock()
        service.acl().patch().execute = Mock(
            return_value=_rule(f"user:{EMAIL}", "owner")
        )
        await _manage(
            service,
            action="update",
            calendar_id=CAL,
            rule_id=f"user:{EMAIL}",
            role="owner",
        )
        service.acl().patch.assert_called()


class TestDelete:
    @pytest.mark.asyncio
    async def test_deletes_a_rule(self):
        service = Mock()
        service.acl().delete().execute = Mock(return_value=None)

        result = await _manage(
            service, action="delete", calendar_id=CAL, rule_id="group:team@example.com"
        )

        assert service.acl().delete.call_args.kwargs == {
            "calendarId": CAL,
            "ruleId": "group:team@example.com",
        }
        assert "Successfully deleted sharing rule 'group:team@example.com'" in result

    @pytest.mark.asyncio
    async def test_refuses_to_delete_the_users_own_rule(self):
        service = Mock()
        with pytest.raises(UserInputError, match="own access rule"):
            await _manage(
                service, action="delete", calendar_id=CAL, rule_id=f"user:{EMAIL}"
            )
        service.acl().delete.assert_not_called()
