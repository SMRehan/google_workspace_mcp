"""Unit tests for manage_gmail_settings and manage_gmail_sharing_settings."""

import os
import sys
from unittest.mock import Mock

import pytest
from fastmcp.exceptions import ToolError
from googleapiclient.errors import HttpError

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from auth.scopes import GMAIL_SETTINGS_BASIC_SCOPE, GMAIL_SETTINGS_SHARING_SCOPE
from core.utils import UserInputError
from gmail.gmail_management_helpers import parse_time_to_epoch_ms
from gmail.gmail_management_tools import (
    manage_gmail_settings,
    manage_gmail_sharing_settings,
)

EMAIL = "user@example.com"


def _unwrap(tool):
    """Unwrap FunctionTool + decorators to the original async function."""
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


class _FakeResp:
    def __init__(self, status):
        self.status = status
        self.reason = "err"


def _settings_service():
    service = Mock()
    settings = service.users().settings()
    settings.getVacation().execute.return_value = {
        "enableAutoReply": False,
        "responseSubject": "Away",
        "responseBodyPlainText": "Back soon",
        "restrictToContacts": False,
        "restrictToDomain": False,
    }
    settings.getImap().execute.return_value = {
        "enabled": True,
        "autoExpunge": True,
        "expungeBehavior": "archive",
        "maxFolderSize": 0,
    }
    settings.getPop().execute.return_value = {
        "accessWindow": "disabled",
        "disposition": "leaveInInbox",
    }
    settings.getLanguage().execute.return_value = {"displayLanguage": "en-GB"}
    settings.getAutoForwarding().execute.return_value = {"enabled": False}
    settings.sendAs().list().execute.return_value = {
        "sendAs": [
            {
                "sendAsEmail": EMAIL,
                "displayName": "Me",
                "isPrimary": True,
                "isDefault": True,
                "signature": "<b>Me</b>",
            },
            {
                "sendAsEmail": "alias@example.com",
                "treatAsAlias": True,
                "verificationStatus": "accepted",
            },
        ]
    }
    settings.forwardingAddresses().list().execute.return_value = {
        "forwardingAddresses": [
            {"forwardingEmail": "fwd@example.com", "verificationStatus": "accepted"}
        ]
    }
    # Update endpoints echo their body back, like the real API.
    for name in ("updateVacation", "updateImap", "updatePop", "updateLanguage"):
        endpoint = getattr(settings, name)
        endpoint.side_effect = lambda userId, body: Mock(
            execute=Mock(return_value=dict(body))
        )
    # Configuring the chain above records calls; forget them.
    service.reset_mock()
    return service


async def _run(service, **kwargs):
    return await _unwrap(manage_gmail_settings)(
        service=service, user_google_email=EMAIL, **kwargs
    )


def test_basic_tool_requests_only_settings_basic():
    assert manage_gmail_settings._required_google_scopes == [GMAIL_SETTINGS_BASIC_SCOPE]


def test_sharing_tool_requests_only_settings_sharing():
    assert manage_gmail_sharing_settings._required_google_scopes == [
        GMAIL_SETTINGS_SHARING_SCOPE
    ]


class TestGet:
    @pytest.mark.asyncio
    async def test_get_reports_every_section(self):
        result = await _run(_settings_service(), action="get")
        for line in (
            "Vacation responder:",
            "Status: disabled",
            "Subject: Away",
            "Body: plain",
            "IMAP:",
            "Status: enabled | Auto-expunge: True | Expunge behavior: archive",
            "POP:",
            "Access window: disabled | Disposition: leaveInInbox",
            "Display language: en-GB",
            "Auto-forwarding:",
            "To: (none)",
            f"- {EMAIL} (Me) [primary, default] | signature: yes",
            "- alias@example.com [alias] | signature: no | verification: accepted",
            "Forwarding addresses:",
            "- fwd@example.com (accepted)",
        ):
            assert line in result, line

    @pytest.mark.asyncio
    async def test_get_shows_dates_and_tolerates_one_failing_read(self):
        service = _settings_service()
        settings = service.users().settings()
        settings.getVacation().execute.return_value["startTime"] = "1767225600000"
        settings.forwardingAddresses().list().execute.side_effect = HttpError(
            _FakeResp(403), b"{}"
        )
        result = await _run(service, action="get")
        assert "Start: 2026-01-01T00:00:00+00:00" in result
        assert "unavailable (HTTP 403)" in result
        assert "Display language: en-GB" in result


class TestVacation:
    @pytest.mark.asyncio
    async def test_update_merges_into_current_settings(self):
        service = _settings_service()
        result = await _run(
            service,
            action="update_vacation",
            enable=True,
            vacation_body="<p>Out</p>",
            vacation_body_format="html",
            vacation_start_date="2026-01-01",
            vacation_end_date="2026-01-07",
            restrict_to_contacts=True,
        )
        body = service.users().settings().updateVacation.call_args.kwargs["body"]
        assert body["enableAutoReply"] is True
        assert body["responseSubject"] == "Away"  # carried over
        assert body["responseBodyHtml"] == "<p>Out</p>"
        assert "responseBodyPlainText" not in body
        assert body["startTime"] == "1767225600000"
        assert body["endTime"] == "1767830399999"
        assert body["restrictToContacts"] is True
        assert body["restrictToDomain"] is False
        assert "Status: enabled" in result
        assert "Start: 2026-01-01T00:00:00+00:00" in result
        assert "End: 2026-01-07T23:59:59.999000+00:00" in result

    @pytest.mark.asyncio
    async def test_disable_and_clear_dates(self):
        service = _settings_service()
        current = service.users().settings().getVacation().execute.return_value
        current["startTime"] = "1"
        current["endTime"] = "2"
        await _run(
            service,
            action="update_vacation",
            enable=False,
            vacation_start_date="",
            vacation_end_date="",
        )
        body = service.users().settings().updateVacation.call_args.kwargs["body"]
        assert body["enableAutoReply"] is False
        assert "startTime" not in body and "endTime" not in body

    @pytest.mark.asyncio
    async def test_start_after_end_rejected(self):
        service = _settings_service()
        with pytest.raises(UserInputError, match="start_date"):
            await _run(
                service,
                action="update_vacation",
                vacation_start_date="2026-02-01",
                vacation_end_date="2026-01-01",
            )
        service.users().settings().updateVacation.assert_not_called()

    @pytest.mark.asyncio
    async def test_nothing_to_update_rejected(self):
        with pytest.raises(UserInputError, match="at least one"):
            await _run(_settings_service(), action="update_vacation")

    def test_parse_time_accepts_dates_and_timestamps(self):
        assert parse_time_to_epoch_ms("2026-01-01", "x") == "1767225600000"
        assert parse_time_to_epoch_ms("2026-01-01T00:00:00Z", "x") == "1767225600000"
        assert parse_time_to_epoch_ms("2026-01-01T01:00:00+01:00", "x") == (
            "1767225600000"
        )
        with pytest.raises(UserInputError, match="YYYY-MM-DD"):
            parse_time_to_epoch_ms("next monday", "vacation_start_date")


class TestImapPopLanguage:
    @pytest.mark.asyncio
    async def test_update_imap_merges_fields(self):
        service = _settings_service()
        result = await _run(
            service,
            action="update_imap",
            enable=False,
            imap_expunge_behavior="trash",
            imap_max_folder_size=1000,
        )
        body = service.users().settings().updateImap.call_args.kwargs["body"]
        assert body == {
            "enabled": False,
            "autoExpunge": True,
            "expungeBehavior": "trash",
            "maxFolderSize": 1000,
        }
        assert "Status: disabled" in result

    @pytest.mark.asyncio
    async def test_update_imap_validation(self):
        with pytest.raises(UserInputError, match="at least one"):
            await _run(_settings_service(), action="update_imap")
        with pytest.raises(UserInputError, match="max_folder_size"):
            await _run(
                _settings_service(), action="update_imap", imap_max_folder_size=-1
            )

    @pytest.mark.asyncio
    async def test_update_pop(self):
        service = _settings_service()
        result = await _run(service, action="update_pop", pop_access_window="allMail")
        body = service.users().settings().updatePop.call_args.kwargs["body"]
        assert body == {"accessWindow": "allMail", "disposition": "leaveInInbox"}
        assert "Access window: allMail" in result

    @pytest.mark.asyncio
    async def test_update_pop_requires_a_field(self):
        with pytest.raises(UserInputError, match="pop_access_window"):
            await _run(_settings_service(), action="update_pop")

    @pytest.mark.asyncio
    async def test_update_language(self):
        service = _settings_service()
        result = await _run(service, action="update_language", display_language=" fr ")
        service.users().settings().updateLanguage.assert_called_with(
            userId="me", body={"displayLanguage": "fr"}
        )
        assert "fr" in result

    @pytest.mark.asyncio
    async def test_update_language_requires_value(self):
        with pytest.raises(UserInputError, match="display_language"):
            await _run(_settings_service(), action="update_language")

    @pytest.mark.asyncio
    async def test_signature_is_not_a_basic_action(self):
        with pytest.raises(UserInputError, match="Invalid action"):
            await _run(_settings_service(), action="update_signature")


def _sharing_service(scopes):
    service = Mock()
    if scopes is None:
        service._http = None
    else:
        service._http.credentials.scopes = list(scopes)
    settings = service.users().settings()
    settings.sendAs().patch().execute.return_value = {
        "sendAsEmail": EMAIL,
        "signature": "<b>Sig</b>",
    }
    settings.updateAutoForwarding().execute.return_value = {
        "enabled": True,
        "emailAddress": "fwd@example.com",
        "disposition": "archive",
    }
    # Configuring the chain above records calls; forget them.
    service.reset_mock()
    return service


async def _run_sharing(service, **kwargs):
    return await _unwrap(manage_gmail_sharing_settings)(
        service=service, user_google_email=EMAIL, **kwargs
    )


class TestSharingSettings:
    @pytest.mark.asyncio
    async def test_signature_refused_when_credentials_lack_sharing_scope(self):
        service = _sharing_service([GMAIL_SETTINGS_BASIC_SCOPE])
        with pytest.raises(ToolError, match="gmail.settings.sharing") as excinfo:
            await _run_sharing(service, action="update_signature", signature="x")
        assert "Nothing was changed" in str(excinfo.value)
        service.users().settings().sendAs().patch.assert_not_called()

    @pytest.mark.asyncio
    async def test_signature_patches_default_send_as(self):
        service = _sharing_service([GMAIL_SETTINGS_SHARING_SCOPE])
        result = await _run_sharing(
            service, action="update_signature", signature="<b>Sig</b>"
        )
        service.users().settings().sendAs().patch.assert_called_with(
            userId="me", sendAsEmail=EMAIL, body={"signature": "<b>Sig</b>"}
        )
        assert f"Signature set (10 characters) for {EMAIL}" in result

    @pytest.mark.asyncio
    async def test_signature_can_be_cleared_on_alias(self):
        service = _sharing_service([GMAIL_SETTINGS_SHARING_SCOPE])
        service.users().settings().sendAs().patch().execute.return_value = {
            "sendAsEmail": "alias@example.com"
        }
        result = await _run_sharing(
            service,
            action="update_signature",
            send_as_email="alias@example.com",
            signature="",
        )
        service.users().settings().sendAs().patch.assert_called_with(
            userId="me", sendAsEmail="alias@example.com", body={"signature": ""}
        )
        assert "Signature cleared for alias@example.com" in result

    @pytest.mark.asyncio
    async def test_signature_requires_value(self):
        with pytest.raises(UserInputError, match="signature is required"):
            await _run_sharing(
                _sharing_service([GMAIL_SETTINGS_SHARING_SCOPE]),
                action="update_signature",
            )

    @pytest.mark.asyncio
    async def test_signature_403_from_api_is_explained(self):
        service = _sharing_service(None)
        service.users().settings().sendAs().patch().execute.side_effect = HttpError(
            _FakeResp(403), b"{}"
        )
        with pytest.raises(ToolError, match="HTTP 403") as excinfo:
            await _run_sharing(service, action="update_signature", signature="x")
        assert GMAIL_SETTINGS_SHARING_SCOPE in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_auto_forwarding_enable(self):
        service = _sharing_service([GMAIL_SETTINGS_SHARING_SCOPE])
        result = await _run_sharing(
            service,
            action="update_auto_forwarding",
            enable=True,
            forward_to="fwd@example.com",
            forward_disposition="archive",
        )
        service.users().settings().updateAutoForwarding.assert_called_with(
            userId="me",
            body={
                "enabled": True,
                "emailAddress": "fwd@example.com",
                "disposition": "archive",
            },
        )
        assert "Status: enabled | To: fwd@example.com | Disposition: archive" in result

    @pytest.mark.asyncio
    async def test_auto_forwarding_disable_sends_only_enabled_false(self):
        service = _sharing_service([GMAIL_SETTINGS_SHARING_SCOPE])
        service.users().settings().updateAutoForwarding().execute.return_value = {
            "enabled": False
        }
        result = await _run_sharing(
            service, action="update_auto_forwarding", enable=False
        )
        service.users().settings().updateAutoForwarding.assert_called_with(
            userId="me", body={"enabled": False}
        )
        assert "Status: disabled" in result

    @pytest.mark.asyncio
    async def test_auto_forwarding_validation(self):
        service = _sharing_service([GMAIL_SETTINGS_SHARING_SCOPE])
        with pytest.raises(UserInputError, match="enable is required"):
            await _run_sharing(service, action="update_auto_forwarding")
        with pytest.raises(UserInputError, match="forward_to is required"):
            await _run_sharing(service, action="update_auto_forwarding", enable=True)
        service.users().settings().updateAutoForwarding.assert_not_called()

    @pytest.mark.asyncio
    async def test_auto_forwarding_refused_without_sharing_scope(self):
        service = _sharing_service([GMAIL_SETTINGS_BASIC_SCOPE])
        with pytest.raises(ToolError, match="gmail.settings.sharing"):
            await _run_sharing(
                service,
                action="update_auto_forwarding",
                enable=True,
                forward_to="fwd@example.com",
            )
        service.users().settings().updateAutoForwarding.assert_not_called()
