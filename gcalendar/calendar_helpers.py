"""
Google Calendar Helper Functions

This module provides utility functions for validating Google Calendar tool
inputs, building event request payloads, and formatting event data for display,
plus the API-facing implementations behind the calendar, calendar-list, ACL and
settings management tools.
"""

import asyncio
import contextlib
import datetime
import json
import logging
import re
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Union
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytz
from googleapiclient.discovery import Resource, build
from googleapiclient.errors import HttpError

from core.utils import UserInputError

logger = logging.getLogger(__name__)

_WEEKDAYS = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)

_RFC3339_DATETIME = re.compile(
    r"\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d"
    r"(?:\.\d+)?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)"
)
_GOOGLE_ALL_DAY_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass(frozen=True)
class EventBoundary:
    """One resolved start/end boundary of a Google Calendar event.

    Google returns each boundary as ``{"dateTime": ..., "timeZone": ...}`` (or
    ``{"date": ...}`` for all-day events), and those two fields do NOT agree by
    default. ``events.list`` / ``events.get`` normalize every ``dateTime`` in the
    response into ONE zone -- the ``timeZone`` request parameter, defaulting to the
    calendar's own -- while leaving each boundary's IANA ``timeZone`` untouched. An
    arrival authored as 17:50 Europe/Amsterdam therefore comes back as
    ``2026-08-21T18:50:00+03:00`` on an Asia/Jerusalem calendar: the correct instant,
    rendered in a zone the author never chose.

    This type resolves the pair into a single tz-aware moment expressed in the
    boundary's OWN zone, so cross-timezone events (flights, above all) report the
    wall-clock and offset that were actually authored. The instant is unchanged;
    only its presentation is corrected.
    """

    raw: str
    """The dateTime/date string exactly as Google returned it."""

    local_date: datetime.date
    """Calendar date at this boundary, in its own zone when that zone was resolved."""

    moment: Optional[datetime.datetime] = None
    """Timezone-aware instant, converted when :attr:`timezone_resolved` is true."""

    timezone: Optional[str] = None
    """IANA zone name exactly as Google supplied it, even if locally unresolvable."""

    timezone_resolved: bool = False
    """Whether :attr:`moment` was converted into :attr:`timezone`."""

    is_all_day: bool = False
    is_exclusive_end: bool = False

    @property
    def iso_weekday(self) -> int:
        return self.local_date.isoweekday()

    def isoformat(self) -> str:
        """RFC3339 stamp in this boundary's own zone.

        Falls back to :attr:`raw` verbatim when no IANA zone was resolved. Without a
        resolved zone there is no reliable wall-clock conversion to make, so the value
        Google returned is the best timestamp -- and echoing it unchanged preserves its
        exact spelling (``Z`` rather than ``+00:00``, offset-less values untouched).
        """
        if self.moment is None or not self.timezone_resolved:
            return self.raw
        return self.moment.isoformat()

    def render(self) -> str:
        """Human-readable boundary: local stamp plus zone and weekday evidence."""
        evidence: List[str] = []
        if self.timezone:
            evidence.append(self.timezone)
        evidence.append(f"weekday: {_WEEKDAYS[self.iso_weekday - 1]}")
        evidence.append(f"ISO weekday: {self.iso_weekday}")
        if self.is_exclusive_end:
            evidence.append("exclusive all-day end")
        return f"{self.isoformat()} [{'; '.join(evidence)}]"


def _resolve_zone(boundary: Dict[str, Any]) -> Optional[ZoneInfo]:
    """Return the boundary's IANA zone, or None when absent or unrecognized."""
    tz_name = boundary.get("timeZone")
    if not isinstance(tz_name, str) or not tz_name:
        return None
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.debug(
            "Unrecognized event timeZone %r; keeping returned offset.", tz_name
        )
        return None


def parse_event_boundary(item: Dict[str, Any], field: str) -> Optional[EventBoundary]:
    """Parse one raw Google event boundary into an :class:`EventBoundary`.

    Returns None when the payload is malformed or shaped unexpectedly, so callers
    can fall back to echoing the raw value rather than guessing at a time.
    """
    boundary = item.get(field)
    if not isinstance(boundary, dict):
        return None
    value = boundary.get("dateTime", boundary.get("date"))
    if not isinstance(value, str):
        return None

    if "dateTime" in boundary:
        if _RFC3339_DATETIME.fullmatch(value) is None:
            return None
        try:
            normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
            moment = datetime.datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if moment.tzinfo is None:
            return None
        zone = _resolve_zone(boundary)
        if zone is not None:
            moment = moment.astimezone(zone)
        tz_name = boundary.get("timeZone")
        timezone = tz_name if isinstance(tz_name, str) and tz_name else None
        return EventBoundary(
            raw=value,
            local_date=moment.date(),
            moment=moment,
            timezone=timezone,
            timezone_resolved=zone is not None,
        )

    if _GOOGLE_ALL_DAY_DATE.fullmatch(value) is None:
        return None
    try:
        local_date = datetime.date.fromisoformat(value)
    except ValueError:
        return None
    return EventBoundary(
        raw=value,
        local_date=local_date,
        is_all_day=True,
        is_exclusive_end=(field == "end"),
    )


def _format_event_time(item: Dict[str, Any], field: str) -> str:
    """Format one event boundary, including sparse cancelled exceptions.

    Google may return cancelled instances of an unexpanded recurring series as
    tombstones with no ``start`` or ``end``. Their ``originalStartTime`` is the
    occurrence they exclude, so use it as the start boundary instead of
    failing or presenting an unexplained ``None``.
    """
    boundary_field = field
    if (
        not isinstance(item.get(field), dict)
        and field == "start"
        and item.get("status") == "cancelled"
    ):
        boundary_field = "originalStartTime"

    parsed = parse_event_boundary(item, boundary_field)
    if parsed is None:
        boundary = item.get(boundary_field)
        if isinstance(boundary, dict):
            value = boundary.get("dateTime", boundary.get("date"))
            return value if isinstance(value, str) else "Unavailable"
        return "Unavailable"
    return parsed.render()


def _get_meeting_link(item: Dict[str, Any]) -> str:
    """Extract video meeting link from event conference data or hangoutLink."""
    conference_data = item.get("conferenceData")
    if conference_data and "entryPoints" in conference_data:
        for entry_point in conference_data["entryPoints"]:
            if entry_point.get("entryPointType") == "video":
                uri = entry_point.get("uri", "")
                if uri:
                    return uri
    hangout_link = item.get("hangoutLink", "")
    if hangout_link:
        return hangout_link
    return ""


def _format_attendee_details(
    attendees: List[Dict[str, Any]], indent: str = "  "
) -> str:
    """
      Format attendee details including response status, organizer and optional
      flags, and the attendee's RSVP note (the API's "comment" field).

      Example output format:
      "  user@example.com: accepted
    manager@example.com: declined (organizer)
    optional-person@example.com: tentative (optional)
    guest@example.com: declined (note: "Can't make it")"

      Args:
          attendees: List of attendee dictionaries from Google Calendar API
          indent: Indentation to use for newline-separated attendees (default: "  ")

      Returns:
          Formatted string with attendee details, or "None" if no attendees
    """
    if not attendees:
        return "None"

    attendee_details_list = []
    for a in attendees:
        email = a.get("email", "unknown")
        response_status = a.get("responseStatus", "unknown")
        optional = a.get("optional", False)
        organizer = a.get("organizer", False)

        detail_parts = [f"{email}: {response_status}"]
        if organizer:
            detail_parts.append("(organizer)")
        if optional:
            detail_parts.append("(optional)")
        note = " ".join((a.get("comment") or "").split())
        if note:
            detail_parts.append(f"(note: {json.dumps(note, ensure_ascii=False)})")

        attendee_details_list.append(" ".join(detail_parts))

    return f"\n{indent}".join(attendee_details_list)


def _format_attachment_details(
    attachments: List[Dict[str, Any]], indent: str = "  "
) -> str:
    """
    Format attachment details including file information.


    Args:
        attachments: List of attachment dictionaries from Google Calendar API
        indent: Indentation to use for newline-separated attachments (default: "  ")

    Returns:
        Formatted string with attachment details, or "None" if no attachments
    """
    if not attachments:
        return "None"

    attachment_details_list = []
    for att in attachments:
        title = att.get("title", "Untitled")
        file_url = att.get("fileUrl", "No URL")
        file_id = att.get("fileId", "No ID")
        mime_type = att.get("mimeType", "Unknown")

        attachment_info = (
            f"{title}\n"
            f"{indent}File URL: {file_url}\n"
            f"{indent}File ID: {file_id}\n"
            f"{indent}MIME Type: {mime_type}"
        )
        attachment_details_list.append(attachment_info)

    return f"\n{indent}".join(attachment_details_list)


def _format_person(person: Optional[Dict[str, Any]]) -> Optional[str]:
    """Format a Google Calendar person dict (creator or organizer) for display."""
    if not person:
        return None
    name = (person.get("displayName") or "").strip()
    email = (person.get("email") or "").strip()
    if name and email:
        return f"{name} <{email}>"
    if name:
        return name
    if email:
        return f"<{email}>"
    return None


def _format_event_detail_lines(
    item: Dict[str, Any],
    prefix: str,
    indent: str,
    include_attachments: bool = False,
) -> str:
    """
    Format the shared body of a detailed event, one field per line.

    Both detailed output paths in `get_events` — the single-event lookup and the
    ranged listing — emit the same fields and differ only in line prefix and
    continuation indent. Formatting them here is what keeps the two in step.

    Args:
        item: Event resource from the Google Calendar API
        prefix: Prefix for each field line (e.g. "- " or "  ")
        indent: Continuation indent for multi-line values (attendees, attachments)
        include_attachments: Whether to append attachment details

    Returns:
        Newline-terminated block of detail lines
    """
    lines = [
        f"{prefix}Description: {item.get('description', 'No Description')}",
        f"{prefix}Location: {item.get('location', 'No Location')}",
        f"{prefix}Color ID: {item.get('colorId', 'None')}",
    ]

    recurring_event_id = item.get("recurringEventId")
    if recurring_event_id:
        lines.append(f"{prefix}Recurring Event ID: {recurring_event_id}")

    original_start_time = item.get("originalStartTime")
    is_exception = item.get("status") != "confirmed" or not item.get("start")
    if original_start_time and is_exception:
        lines.append(f"{prefix}Original Start Time: {json.dumps(original_start_time)}")

    recurrence = item.get("recurrence")
    if recurrence:
        # Keep the individual RFC5545 lines lossless and machine-readable. A
        # recurring master may carry RRULE plus RDATE/EXDATE entries whose
        # commas and semicolons make a hand-joined string ambiguous.
        lines.append(f"{prefix}Recurrence: {json.dumps(recurrence)}")

    # eventType and status are omitted at their API defaults, so an ordinary
    # one-off meeting stays as compact as it was before these fields existed.
    event_type = item.get("eventType")
    if event_type and event_type != "default":
        lines.append(f"{prefix}Event Type: {event_type}")

    status = item.get("status")
    if status and status != "confirmed":
        lines.append(f"{prefix}Status: {status}")

    reminders = item.get("reminders") or {}
    if not reminders.get("useDefault", True):
        lines.append(f"{prefix}Reminders: {json.dumps(reminders.get('overrides', []))}")

    visibility = item.get("visibility")
    if visibility and visibility != "default":
        lines.append(f"{prefix}Visibility: {visibility}")

    transparency = item.get("transparency")
    if transparency and transparency != "opaque":
        lines.append(f"{prefix}Transparency: {transparency}")

    creator_str = _format_person(item.get("creator"))
    if creator_str:
        lines.append(f"{prefix}Creator: {creator_str}")

    organizer_str = _format_person(item.get("organizer"))
    if organizer_str:
        lines.append(f"{prefix}Organizer: {organizer_str}")

    meeting_link = _get_meeting_link(item)
    if meeting_link:
        lines.append(f"{prefix}Meeting Link: {meeting_link}")

    attendees = item.get("attendees", [])
    attendee_emails = (
        ", ".join([a.get("email", "") for a in attendees]) if attendees else "None"
    )
    lines.append(f"{prefix}Attendees: {attendee_emails}")
    lines.append(
        f"{prefix}Attendee Details: {_format_attendee_details(attendees, indent=indent)}"
    )

    if include_attachments:
        attachment_details = _format_attachment_details(
            item.get("attachments", []), indent=indent
        )
        lines.append(f"{prefix}Attachments: {attachment_details}")

    return "".join(f"{line}\n" for line in lines)


# ---------------------------------------------------------------------------
# Calendar, calendar-list, ACL and settings management
#
# Everything below talks to the Calendar API on behalf of the thin tool
# wrappers in ``calendar_tools.py``. Each function takes the already-built
# ``service`` and returns the formatted string the tool hands back.
# ---------------------------------------------------------------------------

VALID_SEND_UPDATES = frozenset({"all", "externalOnly", "none"})
VALID_ACL_SCOPE_TYPES = frozenset({"default", "user", "group", "domain"})
VALID_ACL_ROLES = frozenset(
    {
        "none",
        "freeBusyReader",
        "reader",
        "writerWithoutPrivateAccess",
        "writer",
        "owner",
    }
)
VALID_REMINDER_METHODS = frozenset({"email", "popup"})
VALID_NOTIFICATION_TYPES = frozenset(
    {"eventCreation", "eventChange", "eventCancellation", "eventResponse", "agenda"}
)

_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


def is_primary_calendar(calendar_id: Optional[str], user_google_email: str) -> bool:
    """True when ``calendar_id`` names the user's primary calendar.

    The primary calendar answers to the alias ``primary`` and to its real ID,
    which is the account's email address. Guards that refuse (or require) the
    primary calendar must recognise both spellings, or they can be bypassed by
    passing the email instead of the alias.
    """
    if not calendar_id:
        return False
    normalized = calendar_id.strip().lower()
    return normalized == "primary" or normalized == user_google_email.strip().lower()


def _coerce_json(value: Any, param_name: str) -> Any:
    """Accept a JSON-encoded string or an already-decoded object."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise UserInputError(
                f"{param_name} must be valid JSON: {exc.msg} at position {exc.pos}."
            ) from exc
    return value


def parse_default_reminders(
    reminders: Union[str, List[Dict[str, Any]], None],
) -> Optional[List[Dict[str, Any]]]:
    """Validate calendar-list default reminders.

    Each reminder is ``{"method": "email"|"popup", "minutes": int}``. An empty
    list is meaningful (it removes every default reminder), so it is returned as
    ``[]`` rather than collapsed to None.
    """
    if reminders is None:
        return None
    parsed = _coerce_json(reminders, "default_reminders")
    if not isinstance(parsed, list):
        raise UserInputError(
            "default_reminders must be a list of {method, minutes} objects."
        )
    validated: List[Dict[str, Any]] = []
    for reminder in parsed:
        if not isinstance(reminder, dict):
            raise UserInputError(
                "Each default reminder must be an object with 'method' and 'minutes'."
            )
        method = reminder.get("method")
        minutes = reminder.get("minutes")
        if method not in VALID_REMINDER_METHODS:
            raise UserInputError(
                f"Invalid reminder method {method!r}. Must be one of: "
                f"{sorted(VALID_REMINDER_METHODS)}"
            )
        if not isinstance(minutes, int) or isinstance(minutes, bool) or minutes < 0:
            raise UserInputError(
                "Reminder 'minutes' must be a non-negative integer (0-40320)."
            )
        validated.append({"method": method, "minutes": minutes})
    return validated


def parse_notification_settings(
    notifications: Union[str, List[Dict[str, Any]], Dict[str, Any], None],
) -> Optional[Dict[str, Any]]:
    """Validate calendar-list notification settings.

    Accepts either the bare list ``[{"type": ..., "method": "email"}]`` or the
    API's wrapper ``{"notifications": [...]}``. Returns the wrapper form the API
    expects. An empty list disables every notification.
    """
    if notifications is None:
        return None
    parsed = _coerce_json(notifications, "notification_settings")
    if isinstance(parsed, dict):
        parsed = parsed.get("notifications")
    if not isinstance(parsed, list):
        raise UserInputError(
            "notification_settings must be a list of {type, method} objects "
            'or {"notifications": [...]}.'
        )
    validated: List[Dict[str, str]] = []
    for entry in parsed:
        if not isinstance(entry, dict):
            raise UserInputError(
                "Each notification must be an object with 'type' and 'method'."
            )
        ntype = entry.get("type")
        method = entry.get("method", "email")
        if ntype not in VALID_NOTIFICATION_TYPES:
            raise UserInputError(
                f"Invalid notification type {ntype!r}. Must be one of: "
                f"{sorted(VALID_NOTIFICATION_TYPES)}"
            )
        if method != "email":
            raise UserInputError(
                "Calendar notifications only support the 'email' method."
            )
        validated.append({"type": ntype, "method": method})
    return {"notifications": validated}


def _format_reminders(reminders: Optional[List[Dict[str, Any]]]) -> str:
    if not reminders:
        return "None"
    return ", ".join(
        f"{r.get('method', '?')} {r.get('minutes', '?')} min" for r in reminders
    )


def _format_notifications(settings: Optional[Dict[str, Any]]) -> str:
    notifications = (settings or {}).get("notifications") or []
    if not notifications:
        return "None"
    return ", ".join(
        f"{n.get('type', '?')} ({n.get('method', '?')})" for n in notifications
    )


def format_calendar_list_line(cal: Dict[str, Any]) -> str:
    """One-line summary of a calendarList entry for ``list_calendars``.

    Access role and time zone are always shown because they decide what the
    caller can do next (write vs read, which zone offset-free times resolve
    in). ``hidden``/``selected`` are only shown when they deviate from the
    defaults an ordinary visible calendar has, so the common case stays short.
    """
    summary = cal.get("summary", "No Summary")
    override = cal.get("summaryOverride")
    label = f'"{summary}"'
    if override and override != summary:
        label += f' (shown as "{override}")'
    flags: List[str] = []
    if cal.get("primary"):
        flags.append("Primary")
    if cal.get("hidden"):
        flags.append("hidden")
    if cal.get("selected") is False:
        flags.append("not selected")
    if cal.get("deleted"):
        flags.append("deleted")
    line = f"- {label}"
    if flags:
        line += f" ({', '.join(flags)})"
    line += f" (ID: {cal.get('id', 'No ID')})"
    details: List[str] = []
    if cal.get("accessRole"):
        details.append(f"access: {cal['accessRole']}")
    if cal.get("timeZone"):
        details.append(f"tz: {cal['timeZone']}")
    if details:
        line += f" [{'; '.join(details)}]"
    return line


def _format_calendar_resource_lines(cal: Dict[str, Any], prefix: str) -> List[str]:
    """Field lines shared by the Calendar resource (calendars.get/patch)."""
    lines = [f"{prefix}ID: {cal.get('id', 'No ID')}"]
    if cal.get("description"):
        lines.append(f"{prefix}Description: {cal['description']}")
    if cal.get("location"):
        lines.append(f"{prefix}Location: {cal['location']}")
    lines.append(f"{prefix}Time zone: {cal.get('timeZone', 'Unavailable')}")
    return lines


def _format_calendar_list_entry_lines(entry: Dict[str, Any], prefix: str) -> List[str]:
    """Field lines for the user-specific calendarList entry."""
    lines = [
        f"{prefix}Access role: {entry.get('accessRole', 'Unavailable')}",
        f"{prefix}Primary: {'yes' if entry.get('primary') else 'no'}",
        f"{prefix}Hidden: {'yes' if entry.get('hidden') else 'no'}",
        f"{prefix}Selected: {'yes' if entry.get('selected') else 'no'}",
    ]
    if entry.get("summaryOverride"):
        lines.append(f"{prefix}Summary override: {entry['summaryOverride']}")
    color_bits: List[str] = []
    if entry.get("colorId"):
        color_bits.append(f"ID {entry['colorId']}")
    if entry.get("backgroundColor"):
        color_bits.append(f"background {entry['backgroundColor']}")
    if entry.get("foregroundColor"):
        color_bits.append(f"foreground {entry['foregroundColor']}")
    if color_bits:
        lines.append(f"{prefix}Color: {', '.join(color_bits)}")
    lines.append(
        f"{prefix}Default reminders: {_format_reminders(entry.get('defaultReminders'))}"
    )
    lines.append(
        f"{prefix}Notifications: {_format_notifications(entry.get('notificationSettings'))}"
    )
    return lines


def format_calendar_details(
    calendar: Dict[str, Any],
    list_entry: Optional[Dict[str, Any]],
    user_google_email: str,
) -> str:
    """Full description of one calendar for ``manage_calendar(action="get")``."""
    summary = calendar.get("summary", "No Summary")
    lines = [f"Calendar '{summary}' for {user_google_email}:"]
    lines.extend(_format_calendar_resource_lines(calendar, "- "))
    if list_entry is None:
        lines.append(
            "- Not in this user's calendar list (no access role, colour or "
            "notification settings to show; use action='subscribe' to add it)."
        )
    else:
        lines.extend(_format_calendar_list_entry_lines(list_entry, "- "))
    return "\n".join(lines)


def format_acl_rule(rule: Dict[str, Any], prefix: str = "- ") -> str:
    """One line per ACL rule: role, scope and the rule ID needed to change it."""
    scope = rule.get("scope") or {}
    scope_type = scope.get("type", "unknown")
    scope_value = scope.get("value")
    scope_text = scope_type if not scope_value else f"{scope_type} {scope_value}"
    line = f"{prefix}{rule.get('role', 'unknown')}: {scope_text} (rule ID: {rule.get('id', 'No ID')})"
    if rule.get("etag") is not None and rule.get("role") == "none":
        line += " [deleted]"
    return line


def format_settings(items: List[Dict[str, Any]]) -> str:
    if not items:
        return "No settings returned."
    return "\n".join(
        f"- {item.get('id', '?')}: {item.get('value', '')}"
        for item in sorted(items, key=lambda s: str(s.get("id", "")))
    )


def format_colors(colors: Dict[str, Any]) -> str:
    """Render the calendar and event colour palettes with their IDs."""

    def _palette(name: str, palette: Dict[str, Any]) -> List[str]:
        lines = [f"{name} colors (ID: background / foreground):"]
        if not palette:
            lines.append("- None")
            return lines
        for color_id in sorted(palette, key=lambda k: (len(k), k)):
            definition = palette[color_id] or {}
            lines.append(
                f"- {color_id}: {definition.get('background', '?')} / "
                f"{definition.get('foreground', '?')}"
            )
        return lines

    lines = _palette("Calendar", colors.get("calendar") or {})
    lines.append("")
    lines.extend(_palette("Event", colors.get("event") or {}))
    if colors.get("updated"):
        lines.append("")
        lines.append(f"Palette last updated: {colors['updated']}")
    return "\n".join(lines)


def _event_summary_line(item: Dict[str, Any]) -> str:
    """Basic one-line event description, matching ``get_events``' list format."""
    summary = item.get("summary", "No Title")
    start = _format_event_time(item, "start")
    end = _format_event_time(item, "end")
    line = f'- "{summary}" (Starts: {start}, Ends: {end})'
    if item.get("status") == "cancelled":
        line += " [cancelled]"
    meeting_link = _get_meeting_link(item)
    if meeting_link:
        line += f" Meeting: {meeting_link}"
    line += f" ID: {item.get('id', 'No ID')} | Link: {item.get('htmlLink', 'No Link')}"
    return line


def _parse_reminders_json(
    reminders_input: Optional[Union[str, List[Dict[str, Any]]]], function_name: str
) -> List[Dict[str, Any]]:
    """
    Parse reminders from JSON string or list object and validate them.

    Args:
        reminders_input: JSON string containing reminder objects or list of reminder objects
        function_name: Name of calling function for logging

    Returns:
        List of validated reminder objects
    """
    if not reminders_input:
        return []

    # Handle both string (JSON) and list inputs
    if isinstance(reminders_input, str):
        try:
            reminders = json.loads(reminders_input)
            if not isinstance(reminders, list):
                logger.warning(
                    f"[{function_name}] Reminders must be a JSON array, got {type(reminders).__name__}"
                )
                return []
        except json.JSONDecodeError as e:
            logger.warning(f"[{function_name}] Invalid JSON for reminders: {e}")
            return []
    elif isinstance(reminders_input, list):
        reminders = reminders_input
    else:
        logger.warning(
            f"[{function_name}] Reminders must be a JSON string or list, got {type(reminders_input).__name__}"
        )
        return []

    # Validate reminders
    if len(reminders) > 5:
        logger.warning(
            f"[{function_name}] More than 5 reminders provided, truncating to first 5"
        )
        reminders = reminders[:5]

    validated_reminders = []
    for reminder in reminders:
        if (
            not isinstance(reminder, dict)
            or "method" not in reminder
            or "minutes" not in reminder
        ):
            logger.warning(
                f"[{function_name}] Invalid reminder format: {reminder}, skipping"
            )
            continue

        method = reminder["method"].lower()
        if method not in ["popup", "email"]:
            logger.warning(
                f"[{function_name}] Invalid reminder method '{method}', must be 'popup' or 'email', skipping"
            )
            continue

        minutes = reminder["minutes"]
        if not isinstance(minutes, int) or minutes < 0 or minutes > 40320:
            logger.warning(
                f"[{function_name}] Invalid reminder minutes '{minutes}', must be integer 0-40320, skipping"
            )
            continue

        validated_reminders.append({"method": method, "minutes": minutes})

    return validated_reminders


def _apply_transparency_if_valid(
    event_body: Dict[str, Any],
    transparency: Optional[str],
    function_name: str,
) -> None:
    """
    Apply transparency to the event body if the provided value is valid.

    Args:
        event_body: Event payload being constructed.
        transparency: Provided transparency value.
        function_name: Name of the calling function for logging context.
    """
    if transparency is None:
        return

    valid_transparency_values = ["opaque", "transparent"]
    if transparency in valid_transparency_values:
        event_body["transparency"] = transparency
        logger.info(f"[{function_name}] Set transparency to '{transparency}'")
    else:
        logger.warning(
            f"[{function_name}] Invalid transparency value '{transparency}', must be 'opaque' or 'transparent', skipping"
        )


def _apply_visibility_if_valid(
    event_body: Dict[str, Any],
    visibility: Optional[str],
    function_name: str,
) -> None:
    """
    Apply visibility to the event body if the provided value is valid.

    Args:
        event_body: Event payload being constructed.
        visibility: Provided visibility value.
        function_name: Name of the calling function for logging context.
    """
    if visibility is None:
        return

    valid_visibility_values = ["default", "public", "private", "confidential"]
    if visibility in valid_visibility_values:
        event_body["visibility"] = visibility
        logger.info(f"[{function_name}] Set visibility to '{visibility}'")
    else:
        logger.warning(
            f"[{function_name}] Invalid visibility value '{visibility}', must be 'default', 'public', 'private', or 'confidential', skipping"
        )


_EVENT_COLOR_IDS = {
    "lavender": "1",
    "sage": "2",
    "grape": "3",
    "flamingo": "4",
    "banana": "5",
    "tangerine": "6",
    "peacock": "7",
    "graphite": "8",
    "blueberry": "9",
    "basil": "10",
    "tomato": "11",
}


def _resolve_color_id(color: Optional[str]) -> Optional[str]:
    """Resolve an event color name (e.g. "tomato") or ID ("1"-"11") to its colorId."""
    if color is None:
        return None
    normalized = color.strip().lower()
    color_id = _EVENT_COLOR_IDS.get(normalized, normalized)
    if color_id not in _EVENT_COLOR_IDS.values():
        options = ", ".join(f"{name} ({cid})" for name, cid in _EVENT_COLOR_IDS.items())
        raise UserInputError(f"Invalid color_id '{color}'. Must be one of: {options}")
    return color_id


_VALID_AUTO_DECLINE_MODES = {
    "declineAllConflictingInvitations",
    "declineOnlyNewConflictingInvitations",
    "declineNone",
}

_VALID_FOCUS_TIME_CHAT_STATUSES = {
    "available",
    "doNotDisturb",
}


def _validate_auto_decline_mode(mode: Optional[str], function_name: str) -> str:
    """Validate and return auto decline mode, defaulting to declineAllConflictingInvitations.

    Args:
        mode: The auto decline mode to validate.
        function_name: Name of the calling function for error context.

    Returns:
        A valid auto decline mode string.
    """
    if mode is None:
        return "declineAllConflictingInvitations"
    if mode not in _VALID_AUTO_DECLINE_MODES:
        raise UserInputError(
            f"[{function_name}] Invalid auto_decline_mode '{mode}'. "
            f"Must be one of: {', '.join(sorted(_VALID_AUTO_DECLINE_MODES))}"
        )
    return mode


def _preserve_existing_fields(
    event_body: Dict[str, Any],
    existing_event: Dict[str, Any],
    field_mappings: Dict[str, Any],
) -> None:
    """
    Helper function to preserve existing event fields when not explicitly provided.

    Args:
        event_body: The event body being built for the API call
        existing_event: The existing event data from the API
        field_mappings: Dict mapping field names to their new values (None means preserve existing)
    """
    for field_name, new_value in field_mappings.items():
        if new_value is None and field_name in existing_event:
            event_body[field_name] = existing_event[field_name]
            logger.info(f"[modify_event] Preserving existing {field_name}")
        elif new_value is not None:
            event_body[field_name] = new_value


def _naive_to_utc_rfc3339(
    naive: datetime.datetime, param_name: str, timezone: Optional[str]
) -> str:
    """Read a naive datetime as wall time in ``timezone`` (UTC if unset) as RFC3339 UTC."""
    tz = pytz.utc
    if timezone:
        try:
            tz = pytz.timezone(timezone)
        except pytz.exceptions.UnknownTimeZoneError:
            logger.warning(
                f"Could not apply timezone '{timezone}', falling back to UTC for {param_name}"
            )
    try:
        aware = tz.localize(naive).astimezone(datetime.timezone.utc)
    except OverflowError:
        logger.warning(
            f"Could not apply timezone '{timezone}' to {param_name} '{naive.isoformat()}', falling back to UTC"
        )
        aware = naive.replace(tzinfo=datetime.timezone.utc)
    return aware.isoformat().replace("+00:00", "Z")


def _correct_time_format_for_api(
    time_str: Optional[str], param_name: str, timezone: Optional[str] = None
) -> Optional[str]:
    """Normalize a time string into RFC3339 format suitable for the Google Calendar API."""
    if not time_str:
        return None

    # Defensive normalization: some LLM-driven MCP clients double-encode JSON
    # string arguments, passing values like '"2026-05-15T00:00:00Z"'
    time_str = time_str.strip().strip('"').strip("'").strip()
    if not time_str or time_str.lower() in ("null", "none"):
        return None

    logger.info(
        f"_correct_time_format_for_api: Processing {param_name} with value '{time_str}', timezone: '{timezone}'"
    )

    # Handle date-only format (YYYY-MM-DD)
    if len(time_str) == 10 and time_str.count("-") == 2:
        try:
            naive = datetime.datetime.strptime(time_str, "%Y-%m-%d")
        except ValueError:
            logger.warning(
                f"{param_name} '{time_str}' looks like a date but is not valid YYYY-MM-DD. Using as is."
            )
            return time_str
        formatted = _naive_to_utc_rfc3339(naive, param_name, timezone)
        logger.info(
            f"Formatting date-only {param_name} '{time_str}' to RFC3339: '{formatted}'"
        )
        return formatted

    # Handle offset-free date-time format (YYYY-MM-DDTHH:MM:SS)
    if (
        len(time_str) == 19
        and time_str[10] == "T"
        and time_str.count(":") == 2
        and not (
            time_str.endswith("Z") or ("+" in time_str[10:]) or ("-" in time_str[10:])
        )
    ):
        try:
            naive = datetime.datetime.strptime(time_str, "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            logger.warning(
                f"{param_name} '{time_str}' looks like it needs 'Z' but is not valid YYYY-MM-DDTHH:MM:SS. Using as is."
            )
            return time_str
        formatted = _naive_to_utc_rfc3339(naive, param_name, timezone)
        logger.info(f"Formatting {param_name} '{time_str}' to RFC3339: '{formatted}'")
        return formatted

    # If it already has timezone info or doesn't match our patterns, return as is
    logger.info(f"{param_name} '{time_str}' doesn't need formatting, using as is.")
    return time_str


def _build_time_boundary(time_value: str, timezone: Optional[str]) -> Dict[str, str]:
    """Build one Google ``start``/``end`` boundary from a time string and its zone.

    Each boundary carries its OWN ``timeZone``, which is what makes a cross-timezone
    event expressible: a flight departing 13:45 Asia/Jerusalem and landing 17:50
    Europe/Amsterdam is one event whose two ends are authored in different zones.
    Forcing a single zone on both ends silently rewrites one of them -- the arrival
    above becomes 17:50 Israel time, an hour off, with no error raised.
    """
    if "T" not in time_value:
        try:
            if _GOOGLE_ALL_DAY_DATE.fullmatch(time_value) is None:
                raise ValueError(time_value)
            datetime.date.fromisoformat(time_value)
        except ValueError as exc:
            raise UserInputError(
                f"Invalid all-day date {time_value!r}. Use YYYY-MM-DD, for example "
                "'2026-11-10'."
            ) from exc
        return {"date": time_value}
    try:
        parsed = datetime.datetime.fromisoformat(re.sub(r"[zZ]$", "+00:00", time_value))
    except ValueError as exc:
        raise UserInputError(
            f"Invalid RFC3339 timestamp {time_value!r}. Use a value such as "
            "'2026-09-17T11:35:00' or '2026-09-17T11:35:00-07:00'."
        ) from exc
    # `is None` rather than falsy: an explicitly empty zone is an invalid value, not
    # an omitted one, and must reach validation below instead of being treated as
    # "no zone given".
    if timezone is None:
        return {"dateTime": time_value}
    # Reject an unresolvable zone here rather than stripping the caller's offset and
    # forwarding a name Google will refuse. Falling back to the offset instead would
    # be worse than erroring: it silently discards the zone the caller asked for and
    # books the event somewhere else.
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise UserInputError(
            f"Unrecognized IANA timezone {timezone!r}. Use a zone name such as "
            "'America/New_York' or 'Europe/Amsterdam'."
        ) from exc
    if parsed.tzinfo is not None:
        time_value = parsed.astimezone(zone).isoformat()
    return {"dateTime": time_value, "timeZone": timezone}


def _saved_event_times(event: Dict[str, Any]) -> str:
    start = parse_event_boundary(event, "start")
    end = parse_event_boundary(event, "end")
    lines = [
        f"Saved start: {_format_event_time(event, 'start')}",
        f"Saved end: {_format_event_time(event, 'end')}",
    ]
    if start and end:
        if start.moment and end.moment:
            # Subtraction in a shared ZoneInfo uses wall time across DST changes.
            utc = datetime.timezone.utc
            seconds = round(
                (
                    end.moment.astimezone(utc) - start.moment.astimezone(utc)
                ).total_seconds()
            )
            lines.append(
                f"Elapsed duration: {seconds / 60:.15g} minutes ({seconds} seconds)"
            )
        elif start.is_all_day and end.is_all_day:
            days = (end.local_date - start.local_date).days
            unit = "day" if days == 1 else "days"
            lines.append(f"All-day span: {days} {unit} (end date exclusive)")
    return "\n" + "\n".join(lines)


# Friendly provider name -> conferenceSolution display name for the addOn block.
_CONFERENCE_SOLUTION_NAMES = {
    "zoom": "Zoom Meeting",
    "webex": "Webex",
    "teams": "Microsoft Teams",
    "microsoft teams": "Microsoft Teams",
}


def _build_addon_conference_data(
    provider: str,
    uri: str,
    passcode: Optional[str] = None,
    conference_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a Google Calendar ``conferenceData`` block for a third-party add-on.

    Used for providers (Zoom, Webex, Teams, ...) attached via the
    ``conferenceSolution.key.type = "addOn"`` mechanism rather than the native
    ``hangoutsMeet`` create request.
    """
    provider = provider.strip()
    uri = uri.strip()
    name = _CONFERENCE_SOLUTION_NAMES.get(provider.lower(), provider)
    entry_point: Dict[str, Any] = {
        "entryPointType": "video",
        "uri": uri,
        "label": name,
    }
    if passcode:
        entry_point["passcode"] = passcode
    conference_data: Dict[str, Any] = {
        "conferenceSolution": {"key": {"type": "addOn"}, "name": name},
        "entryPoints": [entry_point],
    }
    if conference_id:
        conference_data["conferenceId"] = conference_id
    return conference_data


def _resolve_conference_data(
    conference_data: Optional[Dict[str, Any]],
    conference_provider: Optional[str],
    conference_uri: Optional[str],
    conference_passcode: Optional[str],
    conference_id: Optional[str],
    add_google_meet: Optional[bool],
) -> Optional[Dict[str, Any]]:
    """Resolve the conferencing inputs into a single ``conferenceData`` dict.

    Accepts either a raw ``conference_data`` pass-through payload or the
    higher-level ``conference_provider``/``conference_uri`` helper params, and
    validates that they are not combined with each other or with
    ``add_google_meet``. Returns the resolved payload, or ``None`` if no
    third-party conference was requested.
    """
    helper_used = any(
        [conference_provider, conference_uri, conference_passcode, conference_id]
    )
    if conference_data is not None and helper_used:
        raise UserInputError(
            "Provide either conference_data (raw payload) or the "
            "conference_provider/conference_uri helper params, not both."
        )

    resolved = conference_data
    if helper_used:
        provider = (conference_provider or "").strip()
        uri = (conference_uri or "").strip()
        if not (provider and uri):
            raise UserInputError(
                "conference_provider and conference_uri are both required to "
                "attach a third-party conference."
            )
        resolved = _build_addon_conference_data(
            provider, uri, conference_passcode, conference_id
        )

    if resolved is not None and add_google_meet:
        raise UserInputError(
            "Cannot attach a third-party conference and add_google_meet on the "
            "same event; choose one."
        )
    return resolved


async def _build_attachment_entries(
    service,
    attachments: Union[str, List[str]],
    log_prefix: str,
) -> List[Dict[str, str]]:
    """
    Resolve Drive file IDs or URLs into Calendar attachment objects.

    Falls back to a generic title and MIME type when Drive metadata cannot be
    read, which happens when the calendar credentials carry no Drive scope.
    """
    if isinstance(attachments, str):
        attachments = [a.strip() for a in attachments.split(",") if a.strip()]

    entries: List[Dict[str, str]] = []
    drive_service = None
    try:
        try:
            drive_service = service._http and build("drive", "v3", http=service._http)
        except Exception as e:
            logger.warning(f"Could not build Drive service for MIME type lookup: {e}")

        for att in attachments:
            if att.startswith("https://"):
                # Match /d/<id>, /file/d/<id>, ?id=<id>
                match = re.search(r"(?:/d/|/file/d/|id=)([\w-]+)", att)
                file_id = match.group(1) if match else None
                logger.info(
                    f"[{log_prefix}] Extracted file_id '{file_id}' from attachment URL"
                )
            else:
                file_id = att
                logger.info(
                    f"[{log_prefix}] Using direct file_id '{file_id}' for attachment"
                )
            if not file_id:
                continue

            mime_type = "application/vnd.google-apps.drive-sdk"
            title = "Drive Attachment"
            # Try to get the actual MIME type and filename from Drive
            if drive_service:
                try:
                    file_metadata = await asyncio.to_thread(
                        lambda: (
                            drive_service.files()
                            .get(
                                fileId=file_id,
                                fields="mimeType,name",
                                supportsAllDrives=True,
                            )
                            .execute()
                        )
                    )
                    mime_type = file_metadata.get("mimeType", mime_type)
                    filename = file_metadata.get("name")
                    if filename:
                        title = filename
                        logger.info(
                            f"[{log_prefix}] Using filename '{filename}' as attachment title"
                        )
                    else:
                        logger.info(
                            f"[{log_prefix}] No filename found, using generic title"
                        )
                except Exception as e:
                    logger.warning(f"Could not fetch metadata for file {file_id}: {e}")

            entries.append(
                {
                    "fileUrl": f"https://drive.google.com/open?id={file_id}",
                    "title": title,
                    "mimeType": mime_type,
                }
            )
    finally:
        if drive_service:
            drive_service.close()
    return entries


_SERIES_SERVER_FIELDS = frozenset(
    {
        "created",
        "creator",
        "etag",
        "hangoutLink",
        "htmlLink",
        "iCalUID",
        "id",
        "organizer",
        "sequence",
        "updated",
    }
)


def _truncate_recurrence(recurrence: List[str], split_at: EventBoundary) -> List[str]:
    """Return ``recurrence`` with every RRULE ending just before ``split_at``."""
    if split_at.is_all_day:
        until = (split_at.local_date - datetime.timedelta(days=1)).strftime("%Y%m%d")
    else:
        last_kept = split_at.moment - datetime.timedelta(seconds=1)
        until = last_kept.astimezone(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    truncated = []
    for line in recurrence:
        if line.upper().startswith("RRULE:"):
            parts = [
                part
                for part in line[len("RRULE:") :].split(";")
                if not part.upper().startswith(("COUNT=", "UNTIL="))
            ]
            line = f"RRULE:{';'.join(parts + [f'UNTIL={until}'])}"
        truncated.append(line)
    return truncated


async def _split_recurring_event(
    service,
    calendar_id: str,
    instance_id: str,
    changes: Dict[str, Any],
    send_updates: str,
) -> Dict[str, Any]:
    """Apply ``changes`` to a recurring instance and all later ones; return the new series.

    Ends the original series before the instance, then inserts a copy starting at it,
    restoring the original recurrence if the insert fails.
    """
    events = service.events()
    instance = await asyncio.to_thread(
        lambda: events.get(calendarId=calendar_id, eventId=instance_id).execute()
    )
    series_id = instance.get("recurringEventId")
    split_at = parse_event_boundary(instance, "originalStartTime")
    if not series_id or split_at is None or instance.get("status") == "cancelled":
        raise UserInputError(
            f"Event '{instance_id}' is not an active instance of a recurring series. "
            "Pass a recurring instance ID from get_events."
        )
    series = await asyncio.to_thread(
        lambda: events.get(calendarId=calendar_id, eventId=series_id).execute()
    )
    first = parse_event_boundary(series, "start")
    last = parse_event_boundary(series, "end")
    if first is None or last is None:
        raise Exception(f"Series '{series_id}' has no readable start or end.")
    if (first.moment or first.local_date) == (split_at.moment or split_at.local_date):
        raise UserInputError(
            f"Event '{instance_id}' is the first instance of its series. Update the "
            f"series '{series_id}' directly instead."
        )
    recurrence = series.get("recurrence", [])
    upper_rules = [r.upper() for r in recurrence]
    if any(r.startswith("RDATE") for r in upper_rules):
        raise UserInputError(
            "This series has RDATE entries, which cannot be split. Update the series "
            "directly instead."
        )
    if "recurrence" not in changes and any("COUNT=" in r for r in upper_rules):
        raise UserInputError(
            "This series ends after a fixed COUNT. Pass recurrence for the new series."
        )

    if split_at.is_all_day:
        span = last.local_date - first.local_date
        end = {"date": (split_at.local_date + span).isoformat()}
    else:
        span = last.moment - first.moment
        end = {**series["end"], "dateTime": (split_at.moment + span).isoformat()}
    new_series = {k: v for k, v in series.items() if k not in _SERIES_SERVER_FIELDS}
    new_series.update(
        {
            "id": uuid.uuid4().hex,
            "start": instance["originalStartTime"],
            "end": end,
            **changes,
        }
    )
    for key in ("start", "end"):
        zone = series[key].get("timeZone")
        if zone and "dateTime" in new_series[key]:
            new_series[key] = {"timeZone": zone, **new_series[key]}

    def set_recurrence(rules: List[str]) -> None:
        events.patch(
            calendarId=calendar_id,
            eventId=series_id,
            body={"recurrence": rules},
            sendUpdates=send_updates,
        ).execute()

    await asyncio.to_thread(set_recurrence, _truncate_recurrence(recurrence, split_at))
    try:
        return await asyncio.to_thread(
            lambda: events.insert(
                calendarId=calendar_id,
                body=new_series,
                supportsAttachments=True,
                conferenceDataVersion=1,
                sendUpdates=send_updates,
            ).execute()
        )
    except Exception:
        with contextlib.suppress(Exception):
            return await asyncio.to_thread(
                lambda: events.get(
                    calendarId=calendar_id, eventId=new_series["id"]
                ).execute()
            )
        try:
            await asyncio.to_thread(set_recurrence, recurrence)
        except Exception as restore_error:
            raise Exception(
                f"Could not create the new series or restore series '{series_id}', "
                f"which now ends before instance '{instance_id}'. Restore its "
                f"recurrence to {recurrence}."
            ) from restore_error
        raise


def _normalize_attendees(
    attendees: Optional[List[Union[str, Dict[str, Any]]]],
) -> Optional[List[Dict[str, Any]]]:
    """
    Normalize attendees input to list of attendee objects.

    Accepts either:
    - List of email strings: ["user@example.com", "other@example.com"]
    - List of attendee objects: [{"email": "user@example.com", "responseStatus": "accepted"}]
    - Mixed list of both formats

    Returns list of attendee dicts with at minimum 'email' key.

    Raises:
        UserInputError: If an attendee is neither an email string nor a
            dict with an 'email' key, so it is never silently left off the event.
    """
    if attendees is None:
        return None

    normalized = []
    for att in attendees:
        if isinstance(att, str):
            normalized.append({"email": att})
        elif isinstance(att, dict) and "email" in att:
            normalized.append(att)
        else:
            raise UserInputError(
                "Each attendee must be an email string or an object with an "
                f"'email' key; got {type(att).__name__}"
            )
    return normalized if normalized else None


def _status_event_time_entry(
    time_str: str, event_label: str, timezone: Optional[str] = None
) -> Dict[str, str]:
    """Build a start/end dict for an Out of Office or Focus Time event.

    Google Calendar API requires dateTime (not date) for these event types, so a
    date-only string (YYYY-MM-DD) converts to midnight on that day. Callers pass
    an exclusive end date, so the same conversion serves both boundaries.
    """
    if "T" not in time_str:
        time_str = f"{time_str}T00:00:00"
        logger.info(
            f"[status_event_time_entry] Converted date-only to dateTime: {time_str}"
        )

    has_explicit_offset = time_str.endswith("Z") or bool(
        re.search(r"[+-]\d{2}:\d{2}$", time_str)
    )
    if not has_explicit_offset and not timezone:
        raise UserInputError(
            f"{event_label} events require either a timezone parameter or a "
            "start/end timestamp with an explicit UTC offset."
        )

    entry: Dict[str, str] = {"dateTime": time_str}
    if timezone:
        entry["timeZone"] = timezone
    return entry


def _validate_chat_status(
    chat_status: Optional[str], function_name: str
) -> Optional[str]:
    """Validate chat status for Focus Time events."""
    if chat_status is None:
        return None
    if chat_status not in _VALID_FOCUS_TIME_CHAT_STATUSES:
        raise UserInputError(
            f"[{function_name}] Invalid chat_status '{chat_status}'. "
            f"Must be one of: {', '.join(sorted(_VALID_FOCUS_TIME_CHAT_STATUSES))}"
        )
    return chat_status


_ACL_ROLES = {
    role.lower(): role
    for role in (
        "none",
        "freeBusyReader",
        "reader",
        "writerWithoutPrivateAccess",
        "writer",
        "owner",
    )
}
_ACL_SCOPE_TYPES = ("default", "user", "group", "domain")


def _resolve_acl_role(role: Optional[str], action: str) -> str:
    """Return the canonical Calendar ACL role for a case-insensitive role name."""
    if not role or not role.strip():
        raise UserInputError(f"role is required for {action} action")
    canonical = _ACL_ROLES.get(role.strip().lower())
    if canonical is None:
        raise UserInputError(
            f"Invalid role '{role}'. Must be one of: {', '.join(_ACL_ROLES.values())}"
        )
    return canonical


def _build_acl_scope(scope_type: str, scope_value: Optional[str]) -> Dict[str, str]:
    """Build the ACL scope for a user, group, domain or public (default) grant."""
    scope_type = scope_type.strip().lower()
    if scope_type not in _ACL_SCOPE_TYPES:
        raise UserInputError(
            f"Invalid scope_type '{scope_type}'. Must be one of: {', '.join(_ACL_SCOPE_TYPES)}"
        )
    value = (scope_value or "").strip()
    if scope_type == "default":
        if value:
            raise UserInputError("scope_value must be omitted for scope_type 'default'")
        return {"type": "default"}
    if not value:
        raise UserInputError(f"scope_value is required for scope_type '{scope_type}'")
    return {"type": scope_type, "value": value}


def _format_acl_rule(rule: Dict[str, Any]) -> str:
    """Format one ACL rule as a single readable line."""
    scope = rule.get("scope", {})
    target = " ".join(filter(None, (scope.get("type"), scope.get("value"))))
    return f"- {rule.get('role')}: {target} (Rule ID: {rule.get('id')})"


async def _move_event(
    service: Resource,
    calendar_id: str,
    event_id: str,
    destination_calendar_id: str,
    send_updates: str,
) -> str:
    """Move an event to another calendar and summarize the result.

    Only the organizer's copy of a default event can be moved; the API rejects
    other event types, which surfaces through the tool's HTTP error handling.
    """
    if calendar_id.strip() == destination_calendar_id.strip():
        raise UserInputError(
            "destination_calendar_id must differ from calendar_id; the event is "
            f"already in '{calendar_id}'."
        )
    event = await asyncio.to_thread(
        lambda: (
            service.events()
            .move(
                calendarId=calendar_id,
                eventId=event_id,
                destination=destination_calendar_id,
                sendUpdates=send_updates,
            )
            .execute()
        )
    )
    organizer = event.get("organizer", {}).get("email", destination_calendar_id)
    return (
        f"Moved event '{event.get('summary', 'No Title')}' (ID: {event.get('id', event_id)}) "
        f"from calendar '{calendar_id}' to '{organizer}'. "
        f"Link: {event.get('htmlLink', 'No link available')}"
    )


async def _create_calendar(
    service,
    summary: Optional[str],
    description: Optional[str],
    timezone: Optional[str],
) -> str:
    """Create a secondary calendar and return its summary and ID."""
    if not summary:
        raise UserInputError("summary is required for create action")
    body: Dict[str, Any] = {"summary": summary}
    if description:
        body["description"] = description
    if timezone:
        body["timeZone"] = timezone
    result = await asyncio.to_thread(
        lambda: service.calendars().insert(body=body).execute()
    )
    return f"Created calendar '{result.get('summary', summary)}' (ID: {result['id']})"


async def _manage_calendar_acl(
    service,
    user_google_email: str,
    action: str,
    calendar_id: str,
    role: Optional[str],
    scope_type: str,
    scope_value: Optional[str],
    rule_id: Optional[str],
    send_notifications: bool,
    page_token: Optional[str],
) -> str:
    """List, share, update or remove a calendar's sharing rules and summarize the result."""
    acl = service.acl()

    if action == "list_sharing":
        params: Dict[str, Any] = {"calendarId": calendar_id}
        if page_token:
            params["pageToken"] = page_token
        response = await asyncio.to_thread(lambda: acl.list(**params).execute())
        rules = response.get("items", [])
        lines = [f"Found {len(rules)} sharing rules for calendar '{calendar_id}':"]
        lines.extend(_format_acl_rule(rule) for rule in rules)
        if response.get("nextPageToken"):
            lines.append(f"\nNext page token: {response['nextPageToken']}")
        return "\n".join(lines)

    if action == "share":
        body = {
            "role": _resolve_acl_role(role, action),
            "scope": _build_acl_scope(scope_type, scope_value),
        }
        rule = await asyncio.to_thread(
            lambda: acl.insert(
                calendarId=calendar_id,
                body=body,
                sendNotifications=send_notifications,
            ).execute()
        )
        return f"Shared calendar '{calendar_id}':\n{_format_acl_rule(rule)}"

    if action not in ("update_sharing", "unshare"):
        raise UserInputError(
            f"Invalid action '{action}'. Must be 'create', 'list_sharing', 'share', "
            "'update_sharing', or 'unshare'."
        )
    if not rule_id or not rule_id.strip():
        raise UserInputError(
            f"rule_id is required for {action} action. Use action 'list_sharing' to find it."
        )
    rule_id = rule_id.strip()
    owner = user_google_email.lower()
    if calendar_id.lower() in ("primary", owner) and rule_id.lower() == f"user:{owner}":
        raise UserInputError(
            f"Rule '{rule_id}' is the owner's access to their primary calendar and "
            "cannot be changed or removed."
        )
    if action == "update_sharing":
        request = acl.patch(
            calendarId=calendar_id,
            ruleId=rule_id,
            body={"role": _resolve_acl_role(role, action)},
            sendNotifications=send_notifications,
        )
    else:
        request = acl.delete(calendarId=calendar_id, ruleId=rule_id)
    try:
        rule = await asyncio.to_thread(request.execute)
    except HttpError as error:
        if error.resp.status == 404:
            raise UserInputError(
                f"No sharing rule '{rule_id}' found on calendar '{calendar_id}'. "
                "Use action 'list_sharing' to see rule IDs."
            ) from error
        raise
    if action == "update_sharing":
        return f"Updated sharing on calendar '{calendar_id}':\n{_format_acl_rule(rule)}"
    return f"Removed sharing rule '{rule_id}' from calendar '{calendar_id}'."


# -- manage_event extensions -------------------------------------------------


async def quick_add_event(
    service,
    user_google_email: str,
    text: str,
    calendar_id: str = "primary",
    send_updates: str = "all",
) -> str:
    """Create an event from natural-language text via ``events.quickAdd``."""
    if not text or not text.strip():
        raise UserInputError("text is required for the quick_add action.")
    logger.info(
        f"[manage_event:quick_add] Email: '{user_google_email}', calendar "
        f"'{calendar_id}', text_len={len(text)}"
    )
    created = await asyncio.to_thread(
        lambda: (
            service.events()
            .quickAdd(calendarId=calendar_id, text=text, sendUpdates=send_updates)
            .execute()
        )
    )
    summary = created.get("summary", "No Title")
    lines = [
        f"Successfully quick-added event '{summary}' to calendar '{calendar_id}' "
        f"for {user_google_email}.",
        f"Event ID: {created.get('id', 'No ID')}",
        f"Link: {created.get('htmlLink', 'No Link')}",
    ]
    if created.get("start") or created.get("end"):
        # Quick-add parses the text server-side; echo what Google understood so
        # a misread date or time is visible immediately.
        lines.append(f"Parsed start: {_format_event_time(created, 'start')}")
        lines.append(f"Parsed end: {_format_event_time(created, 'end')}")
    return "\n".join(lines)


async def list_event_instances(
    service,
    user_google_email: str,
    event_id: str,
    calendar_id: str = "primary",
    time_min: Optional[str] = None,
    time_max: Optional[str] = None,
    max_results: Optional[int] = None,
    page_token: Optional[str] = None,
    show_deleted: bool = False,
) -> str:
    """List the occurrences of a recurring event via ``events.instances``.

    ``time_min``/``time_max`` must already be RFC3339 (the tool normalises
    them). Paging follows ``get_events``: a "Next page token" line is appended
    when more instances remain, and an empty page with a token is never
    reported as "no instances".
    """
    params: Dict[str, Any] = {"calendarId": calendar_id, "eventId": event_id}
    if time_min:
        params["timeMin"] = time_min
    if time_max:
        params["timeMax"] = time_max
    if max_results is not None:
        if max_results < 1:
            raise UserInputError("max_results must be a positive integer.")
        params["maxResults"] = max_results
    if page_token:
        params["pageToken"] = page_token
    if show_deleted:
        params["showDeleted"] = True
    logger.info(
        f"[manage_event:instances] Email: '{user_google_email}', event {event_id} "
        f"in '{calendar_id}', timeMin={time_min}, timeMax={time_max}"
    )
    response = await asyncio.to_thread(
        lambda: service.events().instances(**params).execute()
    )
    items = response.get("items", [])
    next_page_token = response.get("nextPageToken")
    pagination = f"\nNext page token: {next_page_token}" if next_page_token else ""
    if not items:
        if next_page_token:
            return (
                f"No instances of event {event_id} on this page in calendar "
                f"'{calendar_id}' for {user_google_email}, and more pages remain."
                f"{pagination}"
            )
        return (
            f"No instances of event {event_id} found in calendar '{calendar_id}' "
            f"for {user_google_email} in the specified time range."
        )
    series_summary = response.get("summary") or items[0].get("summary", "No Title")
    text = (
        f"Successfully retrieved {len(items)} instances of '{series_summary}' "
        f"(series ID: {event_id}) from calendar '{calendar_id}' for {user_google_email}:\n"
        + "\n".join(_event_summary_line(item) for item in items)
    )
    if next_page_token:
        text += f"\n{pagination}"
    return text


async def import_event(
    service,
    user_google_email: str,
    calendar_id: str,
    ical_uid: str,
    start: Dict[str, str],
    end: Dict[str, str],
    summary: Optional[str] = None,
    description: Optional[str] = None,
    location: Optional[str] = None,
    attendees: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Add a private copy of an existing event via ``events.import``.

    Import keeps the supplied ``iCalUID`` so the copy stays linked to the
    original event (e.g. one received by email) instead of becoming a new,
    unrelated event the way ``create`` would. Google sends no notifications
    for imported events.
    """
    if not ical_uid or not ical_uid.strip():
        raise UserInputError("ical_uid is required for the import action.")
    body: Dict[str, Any] = {"iCalUID": ical_uid.strip(), "start": start, "end": end}
    if summary:
        body["summary"] = summary
    if description:
        body["description"] = description
    if location:
        body["location"] = location
    if attendees:
        body["attendees"] = attendees
    logger.info(
        f"[manage_event:import] Email: '{user_google_email}', calendar "
        f"'{calendar_id}', iCalUID present"
    )
    imported = await asyncio.to_thread(
        lambda: service.events().import_(calendarId=calendar_id, body=body).execute()
    )
    return (
        f"Successfully imported event '{imported.get('summary', summary or 'No Title')}' "
        f"into calendar '{calendar_id}' for {user_google_email}.\n"
        f"Event ID: {imported.get('id', 'No ID')}\n"
        f"iCalUID: {imported.get('iCalUID', body['iCalUID'])}\n"
        f"Link: {imported.get('htmlLink', 'No Link')}"
    )


# -- manage_calendar ---------------------------------------------------------


async def get_calendar(service, user_google_email: str, calendar_id: str) -> str:
    """Describe a calendar: ``calendars.get`` plus the user's list entry.

    The list entry (access role, colours, reminders) is optional because a
    calendar the user can read but has not subscribed to has no entry; a 404
    there is expected, not an error.
    """
    calendar = await asyncio.to_thread(
        lambda: service.calendars().get(calendarId=calendar_id).execute()
    )
    list_entry: Optional[Dict[str, Any]] = None
    try:
        list_entry = await asyncio.to_thread(
            lambda: service.calendarList().get(calendarId=calendar_id).execute()
        )
    except HttpError as error:
        if error.resp.status != 404:
            raise
        logger.info(
            f"[manage_calendar:get] '{calendar_id}' is not in the calendar list of "
            f"{user_google_email}; showing the calendar resource only."
        )
    return format_calendar_details(calendar, list_entry, user_google_email)


async def update_calendar(
    service,
    user_google_email: str,
    calendar_id: str,
    summary: Optional[str] = None,
    description: Optional[str] = None,
    location: Optional[str] = None,
    time_zone: Optional[str] = None,
) -> str:
    """Change a calendar's own metadata via ``calendars.patch``."""
    body: Dict[str, Any] = {}
    if summary is not None:
        if not summary.strip():
            raise UserInputError("summary cannot be blank.")
        body["summary"] = summary
    if description is not None:
        body["description"] = description
    if location is not None:
        body["location"] = location
    if time_zone is not None:
        try:
            ZoneInfo(time_zone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise UserInputError(
                f"Unrecognized IANA timezone {time_zone!r}. Use a zone name such as "
                "'America/New_York' or 'Europe/Amsterdam'."
            ) from exc
        body["timeZone"] = time_zone
    if not body:
        raise UserInputError(
            "update requires at least one of summary, description, location or time_zone."
        )
    logger.info(
        f"[manage_calendar:update] Email: '{user_google_email}', calendar "
        f"'{calendar_id}', fields={sorted(body)}"
    )
    updated = await asyncio.to_thread(
        lambda: service.calendars().patch(calendarId=calendar_id, body=body).execute()
    )
    lines = [
        f"Successfully updated calendar '{updated.get('summary', 'No Summary')}' "
        f"for {user_google_email}:"
    ]
    lines.extend(_format_calendar_resource_lines(updated, "- "))
    return "\n".join(lines)


async def delete_calendar(service, user_google_email: str, calendar_id: str) -> str:
    """Permanently delete a secondary calendar via ``calendars.delete``.

    The primary calendar cannot be deleted; refusing it here (under either of
    its names) gives a clear message instead of an API 400 and keeps the guard
    independent of what Google happens to return.
    """
    if is_primary_calendar(calendar_id, user_google_email):
        raise UserInputError(
            "The primary calendar cannot be deleted. Use action='clear' with "
            "confirm=True to remove all of its events instead."
        )
    logger.info(
        f"[manage_calendar:delete] Email: '{user_google_email}', calendar '{calendar_id}'"
    )
    await asyncio.to_thread(
        lambda: service.calendars().delete(calendarId=calendar_id).execute()
    )
    return f"Successfully deleted calendar '{calendar_id}' for {user_google_email}."


async def clear_calendar(
    service, user_google_email: str, calendar_id: str, confirm: bool
) -> str:
    """Delete every event on the primary calendar via ``calendars.clear``.

    Google only supports clearing the primary calendar. This wipes all events
    irreversibly, so an explicit ``confirm=True`` is required on every call.
    """
    if not is_primary_calendar(calendar_id, user_google_email):
        raise UserInputError(
            f"Only the primary calendar can be cleared; '{calendar_id}' is a "
            "secondary calendar. Use action='delete' to remove a secondary calendar."
        )
    if confirm is not True:
        raise UserInputError(
            "clear permanently deletes EVERY event on the primary calendar of "
            f"{user_google_email} and cannot be undone. Pass confirm=True to proceed."
        )
    logger.warning(
        f"[manage_calendar:clear] Clearing primary calendar for '{user_google_email}'"
    )
    await asyncio.to_thread(
        lambda: service.calendars().clear(calendarId=calendar_id).execute()
    )
    return f"Successfully cleared all events from the primary calendar of {user_google_email}."


async def subscribe_calendar(service, user_google_email: str, calendar_id: str) -> str:
    """Add an existing calendar to the user's list via ``calendarList.insert``."""
    if is_primary_calendar(calendar_id, user_google_email):
        raise UserInputError(
            "The primary calendar is always in the calendar list; pass the ID of "
            "another calendar to subscribe to it."
        )
    logger.info(
        f"[manage_calendar:subscribe] Email: '{user_google_email}', calendar '{calendar_id}'"
    )
    entry = await asyncio.to_thread(
        lambda: service.calendarList().insert(body={"id": calendar_id}).execute()
    )
    lines = [
        f"Successfully subscribed {user_google_email} to calendar "
        f"'{entry.get('summary', 'No Summary')}' (ID: {entry.get('id', calendar_id)}):"
    ]
    lines.extend(_format_calendar_list_entry_lines(entry, "- "))
    return "\n".join(lines)


async def unsubscribe_calendar(
    service, user_google_email: str, calendar_id: str
) -> str:
    """Remove a calendar from the user's list via ``calendarList.delete``.

    This only hides the calendar from this user; the calendar and its events
    are untouched and can be re-added with ``subscribe``.
    """
    if is_primary_calendar(calendar_id, user_google_email):
        raise UserInputError(
            "The primary calendar cannot be removed from the calendar list."
        )
    logger.info(
        f"[manage_calendar:unsubscribe] Email: '{user_google_email}', calendar '{calendar_id}'"
    )
    await asyncio.to_thread(
        lambda: service.calendarList().delete(calendarId=calendar_id).execute()
    )
    return (
        f"Successfully removed calendar '{calendar_id}' from the calendar list of "
        f"{user_google_email}. The calendar itself was not deleted."
    )


async def update_calendar_list_entry(
    service,
    user_google_email: str,
    calendar_id: str,
    color_id: Optional[str] = None,
    background_color: Optional[str] = None,
    foreground_color: Optional[str] = None,
    hidden: Optional[bool] = None,
    selected: Optional[bool] = None,
    default_reminders: Union[str, List[Dict[str, Any]], None] = None,
    notification_settings: Union[
        str, List[Dict[str, Any]], Dict[str, Any], None
    ] = None,
    summary_override: Optional[str] = None,
) -> str:
    """Change the user's own view of a calendar via ``calendarList.patch``.

    These fields (colour, visibility, reminders, notifications) are per-user
    and never affect other people who share the calendar.
    """
    body: Dict[str, Any] = {}
    use_rgb = background_color is not None or foreground_color is not None
    if color_id is not None and use_rgb:
        raise UserInputError(
            "Pass either color_id or background_color/foreground_color, not both."
        )
    if color_id is not None:
        body["colorId"] = str(color_id)
    for name, value in (
        ("backgroundColor", background_color),
        ("foregroundColor", foreground_color),
    ):
        if value is None:
            continue
        if not _HEX_COLOR.match(value):
            raise UserInputError(
                f"{name} must be a hex colour such as '#0088aa'; got {value!r}."
            )
        body[name] = value.lower()
    if hidden is not None:
        body["hidden"] = bool(hidden)
    if selected is not None:
        body["selected"] = bool(selected)
    reminders = parse_default_reminders(default_reminders)
    if reminders is not None:
        body["defaultReminders"] = reminders
    notifications = parse_notification_settings(notification_settings)
    if notifications is not None:
        body["notificationSettings"] = notifications
    if summary_override is not None:
        body["summaryOverride"] = summary_override
    if not body:
        raise UserInputError(
            "update_list_entry requires at least one of color_id, background_color, "
            "foreground_color, hidden, selected, default_reminders, "
            "notification_settings or summary_override."
        )
    params: Dict[str, Any] = {"calendarId": calendar_id, "body": body}
    if use_rgb:
        params["colorRgbFormat"] = True
    logger.info(
        f"[manage_calendar:update_list_entry] Email: '{user_google_email}', calendar "
        f"'{calendar_id}', fields={sorted(body)}"
    )
    entry = await asyncio.to_thread(
        lambda: service.calendarList().patch(**params).execute()
    )
    lines = [
        f"Successfully updated calendar list entry for "
        f"'{entry.get('summary', 'No Summary')}' (ID: {entry.get('id', calendar_id)}) "
        f"for {user_google_email}:"
    ]
    lines.extend(_format_calendar_list_entry_lines(entry, "- "))
    return "\n".join(lines)


# -- manage_calendar_acl -----------------------------------------------------


def _is_own_acl_rule(rule_id: str, user_google_email: str) -> bool:
    return rule_id.strip().lower() == f"user:{user_google_email.strip().lower()}"


async def list_acl_rules(
    service,
    user_google_email: str,
    calendar_id: str,
    max_results: Optional[int] = None,
    page_token: Optional[str] = None,
    show_deleted: bool = False,
) -> str:
    """List sharing rules via ``acl.list`` with paging."""
    params: Dict[str, Any] = {"calendarId": calendar_id}
    if max_results is not None:
        if max_results < 1:
            raise UserInputError("max_results must be a positive integer.")
        params["maxResults"] = max_results
    if page_token:
        params["pageToken"] = page_token
    if show_deleted:
        params["showDeleted"] = True
    response = await asyncio.to_thread(lambda: service.acl().list(**params).execute())
    items = response.get("items", [])
    next_page_token = response.get("nextPageToken")
    if not items:
        if next_page_token:
            return (
                f"No sharing rules on this page for calendar '{calendar_id}' "
                f"({user_google_email}), and more pages remain."
                f"\nNext page token: {next_page_token}"
            )
        return f"No sharing rules found for calendar '{calendar_id}' ({user_google_email})."
    text = (
        f"Successfully listed {len(items)} sharing rules for calendar '{calendar_id}' "
        f"({user_google_email}):\n" + "\n".join(format_acl_rule(r) for r in items)
    )
    if next_page_token:
        text += f"\n\nNext page token: {next_page_token}"
    return text


async def get_acl_rule(
    service, user_google_email: str, calendar_id: str, rule_id: str
) -> str:
    """Fetch one sharing rule via ``acl.get``."""
    rule = await asyncio.to_thread(
        lambda: service.acl().get(calendarId=calendar_id, ruleId=rule_id).execute()
    )
    return (
        f"Sharing rule on calendar '{calendar_id}' ({user_google_email}):\n"
        + format_acl_rule(rule)
    )


def _validate_acl_role(role: Optional[str]) -> str:
    if role not in VALID_ACL_ROLES:
        raise UserInputError(
            f"Invalid role {role!r}. Must be one of: {sorted(VALID_ACL_ROLES)}"
        )
    return role


async def insert_acl_rule(
    service,
    user_google_email: str,
    calendar_id: str,
    scope_type: Optional[str],
    scope_value: Optional[str],
    role: Optional[str],
    send_notifications: Optional[bool] = None,
) -> str:
    """Share a calendar via ``acl.insert``.

    ``scope_type='default'`` makes the calendar public and takes no value;
    every other scope type needs the user, group or domain it applies to.
    """
    if scope_type not in VALID_ACL_SCOPE_TYPES:
        raise UserInputError(
            f"Invalid scope_type {scope_type!r}. Must be one of: "
            f"{sorted(VALID_ACL_SCOPE_TYPES)}"
        )
    role = _validate_acl_role(role)
    scope: Dict[str, str] = {"type": scope_type}
    if scope_type == "default":
        if scope_value:
            raise UserInputError(
                "scope_value must be omitted when scope_type is 'default' (public)."
            )
        if role not in ("none", "freeBusyReader", "reader"):
            raise UserInputError(
                "Public access (scope_type='default') can only be 'freeBusyReader', "
                "'reader' or 'none'."
            )
    else:
        if not scope_value or not scope_value.strip():
            raise UserInputError(
                f"scope_value is required when scope_type is '{scope_type}'."
            )
        scope["value"] = scope_value.strip()
    params: Dict[str, Any] = {
        "calendarId": calendar_id,
        "body": {"role": role, "scope": scope},
    }
    if send_notifications is not None:
        params["sendNotifications"] = bool(send_notifications)
    logger.info(
        f"[manage_calendar_acl:insert] Email: '{user_google_email}', calendar "
        f"'{calendar_id}', scope_type={scope_type}, role={role}"
    )
    rule = await asyncio.to_thread(lambda: service.acl().insert(**params).execute())
    return (
        f"Successfully shared calendar '{calendar_id}' ({user_google_email}):\n"
        + format_acl_rule(rule)
    )


async def update_acl_rule(
    service,
    user_google_email: str,
    calendar_id: str,
    rule_id: str,
    role: Optional[str],
    send_notifications: Optional[bool] = None,
) -> str:
    """Change the role of an existing rule via ``acl.patch``."""
    role = _validate_acl_role(role)
    if role != "owner" and _is_own_acl_rule(rule_id, user_google_email):
        raise UserInputError(
            f"Refusing to lower {user_google_email}'s own access on calendar "
            f"'{calendar_id}' to '{role}'; that could lock this account out of a "
            "calendar it manages."
        )
    params: Dict[str, Any] = {
        "calendarId": calendar_id,
        "ruleId": rule_id,
        "body": {"role": role},
    }
    if send_notifications is not None:
        params["sendNotifications"] = bool(send_notifications)
    logger.info(
        f"[manage_calendar_acl:update] Email: '{user_google_email}', calendar "
        f"'{calendar_id}', rule '{rule_id}', role={role}"
    )
    rule = await asyncio.to_thread(lambda: service.acl().patch(**params).execute())
    return (
        f"Successfully updated sharing rule on calendar '{calendar_id}' "
        f"({user_google_email}):\n" + format_acl_rule(rule)
    )


async def delete_acl_rule(
    service, user_google_email: str, calendar_id: str, rule_id: str
) -> str:
    """Revoke a sharing rule via ``acl.delete``."""
    if _is_own_acl_rule(rule_id, user_google_email):
        raise UserInputError(
            f"Refusing to delete {user_google_email}'s own access rule on calendar "
            f"'{calendar_id}'; that could lock this account out of a calendar it manages."
        )
    logger.info(
        f"[manage_calendar_acl:delete] Email: '{user_google_email}', calendar "
        f"'{calendar_id}', rule '{rule_id}'"
    )
    await asyncio.to_thread(
        lambda: service.acl().delete(calendarId=calendar_id, ruleId=rule_id).execute()
    )
    return (
        f"Successfully deleted sharing rule '{rule_id}' from calendar '{calendar_id}' "
        f"for {user_google_email}."
    )


# -- get_calendar_settings ---------------------------------------------------


async def get_settings_and_colors(
    service, user_google_email: str, include_colors: bool = True
) -> str:
    """Read ``settings.list`` (all pages) and optionally ``colors.get``."""
    items: List[Dict[str, Any]] = []
    page_token: Optional[str] = None
    while True:
        params: Dict[str, Any] = {}
        if page_token:
            params["pageToken"] = page_token
        response = await asyncio.to_thread(
            lambda: service.settings().list(**params).execute()
        )
        items.extend(response.get("items", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    text = f"Calendar settings for {user_google_email}:\n{format_settings(items)}"
    if include_colors:
        colors = await asyncio.to_thread(lambda: service.colors().get().execute())
        text += "\n\n" + format_colors(colors)
    return text
