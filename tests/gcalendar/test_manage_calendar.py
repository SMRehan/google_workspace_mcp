"""Unit tests for the manage_calendar tool and its helpers."""

import os
import sys
from unittest.mock import Mock, patch

import pytest
from googleapiclient.errors import HttpError

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from core.utils import UserInputError
from gcalendar.calendar_helpers import (
    is_primary_calendar,
    parse_default_reminders,
    parse_notification_settings,
)
from gcalendar.calendar_tools import manage_calendar

EMAIL = "user@example.com"
SECONDARY = "abc123@group.calendar.google.com"


def _unwrap(tool):
    """Unwrap FunctionTool + decorators to the original async function."""
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


async def _manage(service, **kwargs):
    return await _unwrap(manage_calendar)(
        service=service, user_google_email=EMAIL, **kwargs
    )


def _http_error(status):
    return HttpError(Mock(status=status, reason="x"), b"{}")


def _calendar(calendar_id=SECONDARY, **extra):
    data = {
        "id": calendar_id,
        "summary": "Team",
        "description": "Shared team calendar",
        "location": "Utrecht",
        "timeZone": "Europe/Amsterdam",
    }
    data.update(extra)
    return data


def _list_entry(calendar_id=SECONDARY, **extra):
    data = {
        "id": calendar_id,
        "summary": "Team",
        "accessRole": "owner",
        "colorId": "7",
        "backgroundColor": "#42d692",
        "foregroundColor": "#000000",
        "selected": True,
        "defaultReminders": [{"method": "popup", "minutes": 10}],
        "notificationSettings": {
            "notifications": [{"type": "eventCreation", "method": "email"}]
        },
    }
    data.update(extra)
    return data


class TestPrimaryDetection:
    @pytest.mark.parametrize(
        "calendar_id", ["primary", "PRIMARY", " primary ", EMAIL, "User@Example.com"]
    )
    def test_both_spellings_of_the_primary_calendar(self, calendar_id):
        assert is_primary_calendar(calendar_id, EMAIL) is True

    @pytest.mark.parametrize("calendar_id", [SECONDARY, "", None, "other@example.com"])
    def test_everything_else_is_secondary(self, calendar_id):
        assert is_primary_calendar(calendar_id, EMAIL) is False


class TestActionValidation:
    @pytest.mark.asyncio
    async def test_invalid_action(self):
        with pytest.raises(UserInputError, match="Invalid action 'bogus'"):
            await _manage(Mock(), action="bogus")

    @pytest.mark.asyncio
    async def test_blank_calendar_id(self):
        with pytest.raises(UserInputError, match="calendar_id is required"):
            await _manage(Mock(), action="get", calendar_id="  ")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("action", ["delete", "clear", "unsubscribe"])
    async def test_destructive_actions_respect_permission_denials(self, action):
        service = Mock()
        with patch("gcalendar.calendar_tools.is_action_denied", return_value=True):
            with pytest.raises(UserInputError, match="not allowed"):
                await _manage(
                    service, action=action, calendar_id=SECONDARY, confirm=True
                )
        service.calendars().delete.assert_not_called()
        service.calendars().clear.assert_not_called()
        service.calendarList().delete.assert_not_called()


class TestGet:
    @pytest.mark.asyncio
    async def test_combines_calendar_and_list_entry(self):
        service = Mock()
        service.calendars().get().execute = Mock(return_value=_calendar())
        service.calendarList().get().execute = Mock(return_value=_list_entry())

        result = await _manage(service, action="get", calendar_id=SECONDARY)

        assert service.calendars().get.call_args.kwargs == {"calendarId": SECONDARY}
        assert service.calendarList().get.call_args.kwargs == {"calendarId": SECONDARY}
        assert "Calendar 'Team'" in result
        assert f"ID: {SECONDARY}" in result
        assert "Description: Shared team calendar" in result
        assert "Location: Utrecht" in result
        assert "Time zone: Europe/Amsterdam" in result
        assert "Access role: owner" in result
        assert "Primary: no" in result
        assert "Selected: yes" in result
        assert "Color: ID 7, background #42d692, foreground #000000" in result
        assert "Default reminders: popup 10 min" in result
        assert "Notifications: eventCreation (email)" in result

    @pytest.mark.asyncio
    async def test_calendar_not_in_list_is_reported_not_raised(self):
        service = Mock()
        service.calendars().get().execute = Mock(return_value=_calendar())
        service.calendarList().get().execute = Mock(side_effect=_http_error(404))

        result = await _manage(service, action="get", calendar_id=SECONDARY)

        assert "Not in this user's calendar list" in result
        assert "Time zone: Europe/Amsterdam" in result

    @pytest.mark.asyncio
    async def test_other_list_errors_propagate(self):
        service = Mock()
        service.calendars().get().execute = Mock(return_value=_calendar())
        service.calendarList().get().execute = Mock(side_effect=_http_error(403))
        with pytest.raises(HttpError):
            await _manage(service, action="get", calendar_id=SECONDARY)


class TestUpdate:
    @pytest.mark.asyncio
    async def test_patches_only_supplied_fields(self):
        service = Mock()
        service.calendars().patch().execute = Mock(
            return_value=_calendar(summary="Renamed", timeZone="Asia/Tokyo")
        )

        result = await _manage(
            service,
            action="update",
            calendar_id=SECONDARY,
            summary="Renamed",
            time_zone="Asia/Tokyo",
        )

        kwargs = service.calendars().patch.call_args.kwargs
        assert kwargs == {
            "calendarId": SECONDARY,
            "body": {"summary": "Renamed", "timeZone": "Asia/Tokyo"},
        }
        assert "Successfully updated calendar 'Renamed'" in result
        assert "Time zone: Asia/Tokyo" in result

    @pytest.mark.asyncio
    async def test_empty_strings_clear_description_and_location(self):
        service = Mock()
        service.calendars().patch().execute = Mock(return_value=_calendar())
        await _manage(
            service, action="update", calendar_id=SECONDARY, description="", location=""
        )
        body = service.calendars().patch.call_args.kwargs["body"]
        assert body == {"description": "", "location": ""}

    @pytest.mark.asyncio
    async def test_requires_at_least_one_field(self):
        service = Mock()
        with pytest.raises(UserInputError, match="at least one"):
            await _manage(service, action="update", calendar_id=SECONDARY)
        service.calendars().patch.assert_not_called()

    @pytest.mark.asyncio
    async def test_rejects_blank_summary_and_unknown_timezone(self):
        with pytest.raises(UserInputError, match="blank"):
            await _manage(Mock(), action="update", calendar_id=SECONDARY, summary=" ")
        with pytest.raises(UserInputError, match="Unrecognized IANA timezone"):
            await _manage(
                Mock(), action="update", calendar_id=SECONDARY, time_zone="Mars/Olympus"
            )


class TestDelete:
    @pytest.mark.asyncio
    async def test_deletes_a_secondary_calendar(self):
        service = Mock()
        service.calendars().delete().execute = Mock(return_value=None)

        result = await _manage(service, action="delete", calendar_id=SECONDARY)

        assert service.calendars().delete.call_args.kwargs == {"calendarId": SECONDARY}
        assert f"Successfully deleted calendar '{SECONDARY}'" in result

    @pytest.mark.asyncio
    @pytest.mark.parametrize("calendar_id", ["primary", EMAIL])
    async def test_refuses_the_primary_calendar(self, calendar_id):
        service = Mock()
        with pytest.raises(UserInputError, match="primary calendar cannot be deleted"):
            await _manage(service, action="delete", calendar_id=calendar_id)
        service.calendars().delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_default_calendar_id_is_primary_and_therefore_refused(self):
        service = Mock()
        with pytest.raises(UserInputError, match="primary calendar cannot be deleted"):
            await _manage(service, action="delete")
        service.calendars().delete.assert_not_called()


class TestClear:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("calendar_id", ["primary", EMAIL])
    async def test_clears_primary_when_confirmed(self, calendar_id):
        service = Mock()
        service.calendars().clear().execute = Mock(return_value=None)

        result = await _manage(
            service, action="clear", calendar_id=calendar_id, confirm=True
        )

        assert service.calendars().clear.call_args.kwargs == {"calendarId": calendar_id}
        assert "Successfully cleared all events" in result

    @pytest.mark.asyncio
    async def test_refuses_without_explicit_confirm(self):
        service = Mock()
        with pytest.raises(UserInputError, match="confirm=True"):
            await _manage(service, action="clear")
        service.calendars().clear.assert_not_called()

    @pytest.mark.asyncio
    async def test_refuses_secondary_calendars(self):
        service = Mock()
        with pytest.raises(UserInputError, match="Only the primary calendar"):
            await _manage(service, action="clear", calendar_id=SECONDARY, confirm=True)
        service.calendars().clear.assert_not_called()


class TestSubscribe:
    @pytest.mark.asyncio
    async def test_inserts_into_calendar_list(self):
        service = Mock()
        service.calendarList().insert().execute = Mock(
            return_value=_list_entry(accessRole="reader")
        )

        result = await _manage(service, action="subscribe", calendar_id=SECONDARY)

        assert service.calendarList().insert.call_args.kwargs == {
            "body": {"id": SECONDARY}
        }
        assert "Successfully subscribed" in result
        assert "Access role: reader" in result

    @pytest.mark.asyncio
    @pytest.mark.parametrize("calendar_id", ["primary", EMAIL])
    async def test_refuses_the_primary_calendar(self, calendar_id):
        service = Mock()
        with pytest.raises(UserInputError, match="always in the calendar list"):
            await _manage(service, action="subscribe", calendar_id=calendar_id)
        service.calendarList().insert.assert_not_called()


class TestUnsubscribe:
    @pytest.mark.asyncio
    async def test_deletes_the_list_entry_only(self):
        service = Mock()
        service.calendarList().delete().execute = Mock(return_value=None)

        result = await _manage(service, action="unsubscribe", calendar_id=SECONDARY)

        assert service.calendarList().delete.call_args.kwargs == {
            "calendarId": SECONDARY
        }
        service.calendars().delete.assert_not_called()
        assert "removed calendar" in result
        assert "was not deleted" in result

    @pytest.mark.asyncio
    @pytest.mark.parametrize("calendar_id", ["primary", EMAIL])
    async def test_refuses_the_primary_calendar(self, calendar_id):
        service = Mock()
        with pytest.raises(UserInputError, match="cannot be removed"):
            await _manage(service, action="unsubscribe", calendar_id=calendar_id)
        service.calendarList().delete.assert_not_called()


class TestUpdateListEntry:
    @pytest.mark.asyncio
    async def test_patches_color_id_visibility_reminders_and_notifications(self):
        service = Mock()
        service.calendarList().patch().execute = Mock(return_value=_list_entry())

        result = await _manage(
            service,
            action="update_list_entry",
            calendar_id=SECONDARY,
            color_id="7",
            hidden=False,
            selected=True,
            default_reminders=[{"method": "popup", "minutes": 10}],
            notification_settings=[{"type": "eventCreation", "method": "email"}],
            summary_override="Team (mine)",
        )

        kwargs = service.calendarList().patch.call_args.kwargs
        assert kwargs == {
            "calendarId": SECONDARY,
            "body": {
                "colorId": "7",
                "hidden": False,
                "selected": True,
                "defaultReminders": [{"method": "popup", "minutes": 10}],
                "notificationSettings": {
                    "notifications": [{"type": "eventCreation", "method": "email"}]
                },
                "summaryOverride": "Team (mine)",
            },
        }
        assert "Successfully updated calendar list entry" in result

    @pytest.mark.asyncio
    async def test_rgb_colours_set_color_rgb_format(self):
        service = Mock()
        service.calendarList().patch().execute = Mock(return_value=_list_entry())
        await _manage(
            service,
            action="update_list_entry",
            calendar_id=SECONDARY,
            background_color="#0088AA",
            foreground_color="#ffffff",
        )
        kwargs = service.calendarList().patch.call_args.kwargs
        assert kwargs["colorRgbFormat"] is True
        assert kwargs["body"] == {
            "backgroundColor": "#0088aa",
            "foregroundColor": "#ffffff",
        }

    @pytest.mark.asyncio
    async def test_rejects_color_id_together_with_rgb(self):
        with pytest.raises(UserInputError, match="not both"):
            await _manage(
                Mock(),
                action="update_list_entry",
                calendar_id=SECONDARY,
                color_id="3",
                background_color="#0088aa",
            )

    @pytest.mark.asyncio
    async def test_rejects_malformed_hex_colour(self):
        with pytest.raises(UserInputError, match="hex colour"):
            await _manage(
                Mock(),
                action="update_list_entry",
                calendar_id=SECONDARY,
                background_color="blue",
            )

    @pytest.mark.asyncio
    async def test_json_strings_are_accepted_and_empty_lists_clear(self):
        service = Mock()
        service.calendarList().patch().execute = Mock(return_value=_list_entry())
        await _manage(
            service,
            action="update_list_entry",
            calendar_id=SECONDARY,
            default_reminders="[]",
            notification_settings='{"notifications": []}',
        )
        body = service.calendarList().patch.call_args.kwargs["body"]
        assert body == {
            "defaultReminders": [],
            "notificationSettings": {"notifications": []},
        }

    @pytest.mark.asyncio
    async def test_requires_at_least_one_field(self):
        service = Mock()
        with pytest.raises(UserInputError, match="at least one"):
            await _manage(service, action="update_list_entry", calendar_id=SECONDARY)
        service.calendarList().patch.assert_not_called()


class TestListEntryParsers:
    def test_reminders_validate_method_and_minutes(self):
        assert parse_default_reminders(None) is None
        assert parse_default_reminders('[{"method":"email","minutes":30}]') == [
            {"method": "email", "minutes": 30}
        ]
        with pytest.raises(UserInputError, match="Invalid reminder method"):
            parse_default_reminders([{"method": "sms", "minutes": 5}])
        with pytest.raises(UserInputError, match="non-negative integer"):
            parse_default_reminders([{"method": "popup", "minutes": -1}])
        with pytest.raises(UserInputError, match="non-negative integer"):
            parse_default_reminders([{"method": "popup", "minutes": True}])
        with pytest.raises(UserInputError, match="must be a list"):
            parse_default_reminders({"method": "popup", "minutes": 5})
        with pytest.raises(UserInputError, match="valid JSON"):
            parse_default_reminders("[not json")

    def test_notifications_validate_type_and_method(self):
        assert parse_notification_settings(None) is None
        assert parse_notification_settings([{"type": "agenda"}]) == {
            "notifications": [{"type": "agenda", "method": "email"}]
        }
        with pytest.raises(UserInputError, match="Invalid notification type"):
            parse_notification_settings([{"type": "birthday", "method": "email"}])
        with pytest.raises(UserInputError, match="only support the 'email'"):
            parse_notification_settings([{"type": "agenda", "method": "sms"}])
        with pytest.raises(UserInputError, match="must be a list"):
            parse_notification_settings({"wrong_key": []})
        with pytest.raises(UserInputError, match="valid JSON"):
            parse_notification_settings("agenda")
