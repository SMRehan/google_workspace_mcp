"""Unit tests for the move, quick_add, instances and import actions of manage_event."""

import os
import sys
from unittest.mock import Mock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from core.utils import UserInputError
from gcalendar.calendar_helpers import (
    import_event,
    list_event_instances,
    move_event,
    quick_add_event,
)
from gcalendar.calendar_tools import manage_event

EMAIL = "user@example.com"


def _unwrap(tool):
    """Unwrap FunctionTool + decorators to the original async function."""
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


def _event(event_id="evt-1", summary="Standup"):
    return {
        "id": event_id,
        "summary": summary,
        "htmlLink": f"https://calendar.google.com/event?eid={event_id}",
        "start": {"dateTime": "2026-04-06T09:00:00Z"},
        "end": {"dateTime": "2026-04-06T09:15:00Z"},
    }


async def _manage(service, **kwargs):
    return await _unwrap(manage_event)(
        service=service, user_google_email=EMAIL, **kwargs
    )


# -- move --------------------------------------------------------------------


class TestMove:
    @pytest.mark.asyncio
    async def test_calls_events_move_with_destination_and_send_updates(self):
        service = Mock()
        service.events().move().execute = Mock(return_value=_event())

        result = await _manage(
            service,
            action="move",
            event_id="evt-1",
            calendar_id="primary",
            destination_calendar_id="team@group.calendar.google.com",
            send_updates="none",
        )

        kwargs = service.events().move.call_args.kwargs
        assert kwargs == {
            "calendarId": "primary",
            "eventId": "evt-1",
            "destination": "team@group.calendar.google.com",
            "sendUpdates": "none",
        }
        assert "Successfully moved event 'Standup'" in result
        assert "team@group.calendar.google.com" in result

    @pytest.mark.asyncio
    async def test_send_updates_defaults_to_all(self):
        service = Mock()
        service.events().move().execute = Mock(return_value=_event())
        await _manage(
            service, action="move", event_id="evt-1", destination_calendar_id="other"
        )
        assert service.events().move.call_args.kwargs["sendUpdates"] == "all"

    @pytest.mark.asyncio
    async def test_requires_event_id_and_destination(self):
        with pytest.raises(ValueError, match="event_id is required for move"):
            await _manage(Mock(), action="move", destination_calendar_id="other")
        with pytest.raises(ValueError, match="destination_calendar_id is required"):
            await _manage(Mock(), action="move", event_id="evt-1")

    @pytest.mark.asyncio
    async def test_rejects_same_source_and_destination(self):
        service = Mock()
        with pytest.raises(UserInputError, match="must differ"):
            await move_event(service, EMAIL, "evt-1", "primary", "primary")
        service.events().move.assert_not_called()

    @pytest.mark.asyncio
    async def test_denied_under_permissions(self):
        service = Mock()
        with patch(
            "gcalendar.calendar_tools.is_action_denied", return_value=True
        ) as denied:
            with pytest.raises(UserInputError, match="not allowed"):
                await _manage(
                    service,
                    action="move",
                    event_id="evt-1",
                    destination_calendar_id="other",
                )
        denied.assert_called_once_with("calendar", "move")
        service.events().move.assert_not_called()


# -- quick_add ---------------------------------------------------------------


class TestQuickAdd:
    @pytest.mark.asyncio
    async def test_calls_quick_add_and_echoes_parsed_times(self):
        service = Mock()
        service.events().quickAdd().execute = Mock(
            return_value=_event("qa-1", "Lunch with Sam")
        )

        result = await _manage(
            service,
            action="quick_add",
            text="Lunch with Sam tomorrow at 1pm",
            send_updates="externalOnly",
        )

        kwargs = service.events().quickAdd.call_args.kwargs
        assert kwargs == {
            "calendarId": "primary",
            "text": "Lunch with Sam tomorrow at 1pm",
            "sendUpdates": "externalOnly",
        }
        assert "quick-added event 'Lunch with Sam'" in result
        assert "Event ID: qa-1" in result
        assert "Parsed start: 2026-04-06T09:00:00Z" in result

    @pytest.mark.asyncio
    async def test_requires_text(self):
        with pytest.raises(ValueError, match="text is required"):
            await _manage(Mock(), action="quick_add")
        with pytest.raises(UserInputError, match="text is required"):
            await quick_add_event(Mock(), EMAIL, "   ")

    @pytest.mark.asyncio
    async def test_invalid_send_updates_is_rejected_before_the_api_call(self):
        service = Mock()
        with pytest.raises(ValueError, match="Invalid send_updates"):
            await _manage(service, action="quick_add", text="x", send_updates="bogus")
        service.events().quickAdd.assert_not_called()


# -- instances ---------------------------------------------------------------


def _instances_service(items, next_page_token=None, summary=None):
    response = {"items": items}
    if next_page_token is not None:
        response["nextPageToken"] = next_page_token
    if summary is not None:
        response["summary"] = summary
    service = Mock()
    service.events().instances().execute = Mock(return_value=response)
    return service


class TestInstances:
    @pytest.mark.asyncio
    async def test_forwards_time_bounds_paging_and_normalises_dates(self):
        service = _instances_service([_event("series_20260406T090000Z")])

        await _manage(
            service,
            action="instances",
            event_id="series",
            calendar_id="work",
            time_min="2026-04-01",
            time_max="2026-05-01T00:00:00",
            max_results=10,
            page_token="tok",
            show_deleted=True,
        )

        kwargs = service.events().instances.call_args.kwargs
        assert kwargs == {
            "calendarId": "work",
            "eventId": "series",
            "timeMin": "2026-04-01T00:00:00Z",
            "timeMax": "2026-05-01T00:00:00Z",
            "maxResults": 10,
            "pageToken": "tok",
            "showDeleted": True,
        }

    @pytest.mark.asyncio
    async def test_omits_optional_parameters_by_default(self):
        service = _instances_service([_event()])
        await _manage(service, action="instances", event_id="series")
        assert service.events().instances.call_args.kwargs == {
            "calendarId": "primary",
            "eventId": "series",
        }

    @pytest.mark.asyncio
    async def test_lists_instances_and_surfaces_next_page_token(self):
        service = _instances_service(
            [_event("s_1", "Standup"), _event("s_2", "Standup")],
            next_page_token="next-1",
            summary="Standup",
        )

        result = await _manage(service, action="instances", event_id="series")

        assert (
            "Successfully retrieved 2 instances of 'Standup' (series ID: series)"
            in result
        )
        assert "ID: s_1" in result and "ID: s_2" in result
        assert "Next page token: next-1" in result

    @pytest.mark.asyncio
    async def test_marks_cancelled_instances(self):
        cancelled = dict(_event("s_1"), status="cancelled")
        service = _instances_service([cancelled])
        result = await _manage(service, action="instances", event_id="series")
        assert "[cancelled]" in result

    @pytest.mark.asyncio
    async def test_empty_page_with_more_behind_it_is_not_reported_as_none(self):
        service = _instances_service([], next_page_token="next-2")
        result = await _manage(service, action="instances", event_id="series")
        assert "No instances of event series found" not in result
        assert "more pages remain" in result
        assert "Next page token: next-2" in result

    @pytest.mark.asyncio
    async def test_genuinely_empty_range(self):
        service = _instances_service([])
        result = await _manage(service, action="instances", event_id="series")
        assert "No instances of event series found" in result

    @pytest.mark.asyncio
    async def test_requires_event_id_and_positive_max_results(self):
        with pytest.raises(ValueError, match="event_id is required for instances"):
            await _manage(Mock(), action="instances")
        with pytest.raises(UserInputError, match="positive"):
            await list_event_instances(Mock(), EMAIL, "series", max_results=0)


# -- import ------------------------------------------------------------------


class TestImport:
    @pytest.mark.asyncio
    async def test_builds_body_with_ical_uid_and_zoned_boundaries(self):
        service = Mock()
        service.events().import_().execute = Mock(
            return_value=dict(_event("imp-1", "Offsite"), iCalUID="abc@example.com")
        )

        result = await _manage(
            service,
            action="import",
            ical_uid="abc@example.com",
            summary="Offsite",
            start_time="2026-06-01T09:00:00",
            end_time="2026-06-01T17:00:00",
            timezone="Europe/Amsterdam",
            description="Annual",
            location="Utrecht",
            attendees=["a@example.com", {"email": "b@example.com"}],
            calendar_id="work",
        )

        kwargs = service.events().import_.call_args.kwargs
        assert kwargs["calendarId"] == "work"
        assert kwargs["body"] == {
            "iCalUID": "abc@example.com",
            "start": {
                "dateTime": "2026-06-01T09:00:00",
                "timeZone": "Europe/Amsterdam",
            },
            "end": {"dateTime": "2026-06-01T17:00:00", "timeZone": "Europe/Amsterdam"},
            "summary": "Offsite",
            "description": "Annual",
            "location": "Utrecht",
            "attendees": [{"email": "a@example.com"}, {"email": "b@example.com"}],
        }
        assert "Successfully imported event 'Offsite'" in result
        assert "iCalUID: abc@example.com" in result

    @pytest.mark.asyncio
    async def test_all_day_import_uses_date_boundaries(self):
        service = Mock()
        service.events().import_().execute = Mock(return_value=_event("imp-2"))
        await _manage(
            service,
            action="import",
            ical_uid="uid-2",
            start_time="2026-06-01",
            end_time="2026-06-02",
        )
        body = service.events().import_.call_args.kwargs["body"]
        assert body["start"] == {"date": "2026-06-01"}
        assert body["end"] == {"date": "2026-06-02"}
        assert "summary" not in body

    @pytest.mark.asyncio
    async def test_requires_ical_uid_and_times(self):
        with pytest.raises(ValueError, match="ical_uid is required"):
            await _manage(
                Mock(), action="import", start_time="2026-06-01", end_time="2026-06-02"
            )
        with pytest.raises(ValueError, match="start_time and end_time are required"):
            await _manage(
                Mock(), action="import", ical_uid="uid", start_time="2026-06-01"
            )
        with pytest.raises(UserInputError, match="ical_uid is required"):
            await import_event(
                Mock(),
                EMAIL,
                "primary",
                "  ",
                {"date": "2026-06-01"},
                {"date": "2026-06-02"},
            )


@pytest.mark.asyncio
async def test_invalid_action_message_lists_the_new_actions():
    with pytest.raises(
        ValueError, match="'move', 'quick_add', 'instances', or 'import'"
    ):
        await _manage(Mock(), action="bogus")
