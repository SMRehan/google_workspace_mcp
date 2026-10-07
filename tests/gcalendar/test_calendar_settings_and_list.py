"""Unit tests for get_calendar_settings and the enriched list_calendars output."""

import os
import sys
from unittest.mock import Mock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from gcalendar.calendar_helpers import format_calendar_list_line
from gcalendar.calendar_tools import get_calendar_settings, list_calendars

EMAIL = "user@example.com"


def _unwrap(tool):
    """Unwrap FunctionTool + decorators to the original async function."""
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


class TestGetCalendarSettings:
    @pytest.mark.asyncio
    async def test_lists_all_settings_pages_and_both_palettes(self):
        service = Mock()
        service.settings().list().execute = Mock(
            side_effect=[
                {
                    "items": [{"id": "timezone", "value": "Europe/Amsterdam"}],
                    "nextPageToken": "p2",
                },
                {"items": [{"id": "weekStart", "value": "1"}]},
            ]
        )
        service.colors().get().execute = Mock(
            return_value={
                "updated": "2012-02-14T00:00:00.000Z",
                "calendar": {
                    "1": {"background": "#ac725e", "foreground": "#1d1d1d"},
                    "10": {"background": "#b3dc6c", "foreground": "#1d1d1d"},
                    "2": {"background": "#d06b64", "foreground": "#1d1d1d"},
                },
                "event": {"1": {"background": "#a4bdfc", "foreground": "#1d1d1d"}},
            }
        )

        result = await _unwrap(get_calendar_settings)(
            service=service, user_google_email=EMAIL
        )

        calls = service.settings().list.call_args_list
        # The first call carries no token; the second continues the listing.
        assert calls[-2].kwargs == {}
        assert calls[-1].kwargs == {"pageToken": "p2"}
        assert "Calendar settings for user@example.com:" in result
        assert "- timezone: Europe/Amsterdam" in result
        assert "- weekStart: 1" in result
        assert "Calendar colors (ID: background / foreground):" in result
        assert "Event colors (ID: background / foreground):" in result
        assert "- 1: #a4bdfc / #1d1d1d" in result
        # Numeric IDs sort numerically, not lexically.
        lines = result.splitlines()
        cal_section = lines[
            lines.index("Calendar colors (ID: background / foreground):") :
        ]
        assert cal_section[1].startswith("- 1:")
        assert cal_section[2].startswith("- 2:")
        assert cal_section[3].startswith("- 10:")
        assert "Palette last updated: 2012-02-14T00:00:00.000Z" in result

    @pytest.mark.asyncio
    async def test_colors_can_be_skipped(self):
        service = Mock()
        service.settings().list().execute = Mock(
            return_value={"items": [{"id": "locale", "value": "en"}]}
        )
        result = await _unwrap(get_calendar_settings)(
            service=service, user_google_email=EMAIL, include_colors=False
        )
        service.colors().get.assert_not_called()
        assert "- locale: en" in result
        assert "colors" not in result

    @pytest.mark.asyncio
    async def test_no_settings(self):
        service = Mock()
        service.settings().list().execute = Mock(return_value={})
        service.colors().get().execute = Mock(return_value={})
        result = await _unwrap(get_calendar_settings)(
            service=service, user_google_email=EMAIL
        )
        assert "No settings returned." in result
        assert "- None" in result


class TestListCalendarsFields:
    def test_line_includes_access_role_and_timezone(self):
        line = format_calendar_list_line(
            {
                "id": "user@example.com",
                "summary": "Work",
                "primary": True,
                "accessRole": "owner",
                "timeZone": "Europe/Amsterdam",
                "selected": True,
            }
        )
        assert line == (
            '- "Work" (Primary) (ID: user@example.com) '
            "[access: owner; tz: Europe/Amsterdam]"
        )

    def test_hidden_and_unselected_flags_only_when_set(self):
        line = format_calendar_list_line(
            {
                "id": "x@group.calendar.google.com",
                "summary": "Holidays",
                "summaryOverride": "NL holidays",
                "accessRole": "reader",
                "hidden": True,
                "selected": False,
            }
        )
        assert line == (
            '- "Holidays" (shown as "NL holidays") (hidden, not selected) '
            "(ID: x@group.calendar.google.com) [access: reader]"
        )

    def test_minimal_entry_matches_the_previous_format(self):
        assert (
            format_calendar_list_line({"id": "primary", "summary": "Work"})
            == '- "Work" (ID: primary)'
        )

    @pytest.mark.asyncio
    async def test_tool_output_carries_the_new_fields(self):
        service = Mock()
        service.calendarList().list().execute = Mock(
            return_value={
                "items": [
                    {
                        "id": "primary",
                        "summary": "Work",
                        "primary": True,
                        "accessRole": "owner",
                        "timeZone": "UTC",
                        "selected": True,
                    },
                    {
                        "id": "shared",
                        "summary": "Shared",
                        "accessRole": "freeBusyReader",
                        "hidden": True,
                    },
                ]
            }
        )
        result = await _unwrap(list_calendars)(service=service, user_google_email=EMAIL)
        assert "Successfully listed 2 calendars" in result
        assert "(Primary) (ID: primary) [access: owner; tz: UTC]" in result
        assert '"Shared" (hidden) (ID: shared) [access: freeBusyReader]' in result
