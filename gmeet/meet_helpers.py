"""
Google Meet Helper Functions

Resource-name normalization, request-body and field-mask construction,
filter building, response formatting, and the Meet REST API (v2) calls
behind the tools in meet_tools.py. Everything here operates on plain
dicts/strings plus the injected googleapiclient service object.
"""

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from core.utils import UserInputError

logger = logging.getLogger(__name__)

SPACES_PREFIX = "spaces/"
MEMBERS_SEGMENT = "/members/"
CONFERENCE_RECORDS_PREFIX = "conferenceRecords/"
MEET_URL_PREFIXES = ("https://meet.google.com/", "http://meet.google.com/")

ON_OFF = ("ON", "OFF")
ACCESS_TYPES = ("OPEN", "TRUSTED", "RESTRICTED")
ENTRY_POINT_ACCESS_TYPES = ("ALL", "CREATOR_APP_ONLY")
RESTRICTION_TYPES = ("HOSTS_ONLY", "NO_RESTRICTION")
MEMBER_ROLES = ("ROLE_UNSPECIFIED", "COHOST")

ARTIFACT_KINDS = (
    "participants",
    "participant_sessions",
    "recordings",
    "transcripts",
    "transcript_entries",
    "smart_notes",
)

# attendance_report ON/OFF -> SpaceConfig.attendanceReportGenerationType
_ATTENDANCE_REPORT_VALUES = {"ON": "GENERATE_REPORT", "OFF": "DO_NOT_GENERATE"}

# A maximum of 500 members can be modified in a single members.batchUpdate.
BATCH_UPDATE_MEMBERS_MAX = 500


# ---------------------------------------------------------------------------
# Validation and normalization
# ---------------------------------------------------------------------------


def _validate_choice(value: str, allowed: Tuple[str, ...], field: str) -> str:
    """Return *value* upper-cased if it is one of *allowed*, else raise UserInputError."""
    normalized = str(value).strip().upper()
    if normalized not in allowed:
        raise UserInputError(
            f"Invalid {field} '{value}'. Must be one of: {', '.join(allowed)}"
        )
    return normalized


def _strip_meet_url(value: str) -> str:
    """Drop a meet.google.com URL prefix, surrounding slashes and any query string."""
    for prefix in MEET_URL_PREFIXES:
        if value.startswith(prefix):
            value = value[len(prefix) :]
            break
    return value.strip("/").split("?", 1)[0]


def normalize_space_name(value: Optional[str]) -> str:
    """Return a ``spaces/{space}`` resource name.

    Accepts a full resource name (``spaces/jQCFfuBOdN5z``), a bare space id, a
    meeting code (``abc-mnop-xyz``, matched case-insensitively by the API) or a
    ``https://meet.google.com/abc-mnop-xyz`` URL. Space ids are case sensitive,
    so the value is never re-cased here.
    """
    if value is None or not str(value).strip():
        raise UserInputError("'space_name' is required (space name or meeting code).")
    name = _strip_meet_url(str(value).strip())
    if not name:
        raise UserInputError("'space_name' is required (space name or meeting code).")
    if name.startswith(SPACES_PREFIX):
        return name
    return f"{SPACES_PREFIX}{name}"


def normalize_member_name(space_name: Optional[str], member: Optional[str]) -> str:
    """Return a ``spaces/{space}/members/{member}`` resource name.

    *member* may already be a full member resource name, in which case it is
    returned as-is; otherwise it is treated as a member id under *space_name*.
    """
    if member is None or not str(member).strip():
        raise UserInputError("'member_name' is required for this action.")
    member = str(member).strip()
    if member.startswith(SPACES_PREFIX) and MEMBERS_SEGMENT in member:
        return member
    space = normalize_space_name(space_name)
    return f"{space}{MEMBERS_SEGMENT}{member}"


def normalize_conference_record_name(value: Optional[str]) -> str:
    """Return a ``conferenceRecords/{record}`` resource name from a name or bare id."""
    if value is None or not str(value).strip():
        raise UserInputError("'conference_record' is required.")
    name = str(value).strip()
    if name.startswith(CONFERENCE_RECORDS_PREFIX):
        return name
    return f"{CONFERENCE_RECORDS_PREFIX}{name}"


def _child_name(parent: str, segment: str, value: Optional[str], field: str) -> str:
    """Return ``{parent}/{segment}/{value}`` unless *value* is already a full name."""
    if value is None or not str(value).strip():
        raise UserInputError(f"'{field}' is required for this kind.")
    value = str(value).strip()
    if value.startswith(CONFERENCE_RECORDS_PREFIX) and f"/{segment}/" in value:
        return value
    return f"{parent}/{segment}/{value}"


def _validate_rfc3339(value: str, field: str) -> str:
    """Return *value* stripped if it parses as a full RFC 3339 timestamp."""
    value = str(value).strip()
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise UserInputError(
            f"Invalid {field} '{value}'. Expected an RFC 3339 timestamp such as "
            "'2026-01-01T00:00:00Z'."
        ) from None
    if "T" not in value:
        raise UserInputError(
            f"Invalid {field} '{value}'. Expected a full RFC 3339 timestamp "
            "(date and time), e.g. '2026-01-01T00:00:00Z'."
        )
    return value


def _paging_params(
    page_size: Optional[int], page_token: Optional[str]
) -> Dict[str, Any]:
    """Return pageSize/pageToken request parameters for the values given."""
    params: Dict[str, Any] = {}
    if page_size is not None:
        if page_size < 1:
            raise UserInputError("'page_size' must be a positive integer.")
        params["pageSize"] = page_size
    if page_token:
        params["pageToken"] = page_token
    return params


# ---------------------------------------------------------------------------
# Request construction
# ---------------------------------------------------------------------------


def build_space_config(
    access_type: Optional[str] = None,
    entry_point_access: Optional[str] = None,
    moderation: Optional[str] = None,
    chat_restriction: Optional[str] = None,
    present_restriction: Optional[str] = None,
    reaction_restriction: Optional[str] = None,
    default_join_as_viewer: Optional[str] = None,
    auto_recording: Optional[str] = None,
    auto_transcription: Optional[str] = None,
    auto_smart_notes: Optional[str] = None,
    attendance_report: Optional[str] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    """Build a ``Space.config`` dict plus the matching ``updateMask`` paths.

    Only fields that were provided end up in the config and in the mask, so a
    patch touches nothing else. Returns ``({}, [])`` when nothing was given.
    """
    config: Dict[str, Any] = {}
    paths: List[str] = []

    def _set(path: List[str], value: str) -> None:
        node = config
        for key in path[:-1]:
            node = node.setdefault(key, {})
        node[path[-1]] = value
        paths.append("config." + ".".join(path))

    if access_type is not None:
        _set(["accessType"], _validate_choice(access_type, ACCESS_TYPES, "access_type"))
    if entry_point_access is not None:
        _set(
            ["entryPointAccess"],
            _validate_choice(
                entry_point_access, ENTRY_POINT_ACCESS_TYPES, "entry_point_access"
            ),
        )
    if moderation is not None:
        _set(["moderation"], _validate_choice(moderation, ON_OFF, "moderation"))
    if chat_restriction is not None:
        _set(
            ["moderationRestrictions", "chatRestriction"],
            _validate_choice(chat_restriction, RESTRICTION_TYPES, "chat_restriction"),
        )
    if present_restriction is not None:
        _set(
            ["moderationRestrictions", "presentRestriction"],
            _validate_choice(
                present_restriction, RESTRICTION_TYPES, "present_restriction"
            ),
        )
    if reaction_restriction is not None:
        _set(
            ["moderationRestrictions", "reactionRestriction"],
            _validate_choice(
                reaction_restriction, RESTRICTION_TYPES, "reaction_restriction"
            ),
        )
    if default_join_as_viewer is not None:
        _set(
            ["moderationRestrictions", "defaultJoinAsViewerType"],
            _validate_choice(default_join_as_viewer, ON_OFF, "default_join_as_viewer"),
        )
    if auto_recording is not None:
        _set(
            ["artifactConfig", "recordingConfig", "autoRecordingGeneration"],
            _validate_choice(auto_recording, ON_OFF, "auto_recording"),
        )
    if auto_transcription is not None:
        _set(
            ["artifactConfig", "transcriptionConfig", "autoTranscriptionGeneration"],
            _validate_choice(auto_transcription, ON_OFF, "auto_transcription"),
        )
    if auto_smart_notes is not None:
        _set(
            ["artifactConfig", "smartNotesConfig", "autoSmartNotesGeneration"],
            _validate_choice(auto_smart_notes, ON_OFF, "auto_smart_notes"),
        )
    if attendance_report is not None:
        _set(
            ["attendanceReportGenerationType"],
            _ATTENDANCE_REPORT_VALUES[
                _validate_choice(attendance_report, ON_OFF, "attendance_report")
            ],
        )

    return config, paths


def build_conference_records_filter(
    space_name: Optional[str] = None,
    meeting_code: Optional[str] = None,
    start_time_min: Optional[str] = None,
    end_time_max: Optional[str] = None,
) -> Optional[str]:
    """Build the ``conferenceRecords.list`` filter string.

    Filterable fields are ``space.name``, ``space.meeting_code``, ``start_time``
    and ``end_time``; clauses are joined with ``AND``. Returns ``None`` when no
    criteria were given so the request omits the parameter.
    """
    if space_name and meeting_code:
        raise UserInputError(
            "Provide either 'space_name' or 'meeting_code' to filter conference "
            "records, not both."
        )

    clauses: List[str] = []
    if space_name:
        clauses.append(f'space.name = "{normalize_space_name(space_name)}"')
    if meeting_code:
        code = _strip_meet_url(str(meeting_code).strip())
        if code.startswith(SPACES_PREFIX):
            code = code[len(SPACES_PREFIX) :]
        if not code:
            raise UserInputError("'meeting_code' must not be blank.")
        clauses.append(f'space.meeting_code = "{code}"')
    if start_time_min:
        start = _validate_rfc3339(start_time_min, "start_time_min")
        clauses.append(f'start_time>="{start}"')
    if end_time_max:
        end = _validate_rfc3339(end_time_max, "end_time_max")
        clauses.append(f'end_time<="{end}"')

    return " AND ".join(clauses) if clauses else None


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def format_time(value: Optional[str]) -> str:
    """Render an RFC 3339 timestamp as ``YYYY-MM-DD HH:MM:SS UTC``.

    Non-UTC offsets keep their offset; unparseable values pass through.
    """
    if not value:
        return "N/A"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    offset = parsed.utcoffset()
    if offset is None:
        return parsed.strftime("%Y-%m-%d %H:%M:%S")
    if offset.total_seconds() == 0:
        return parsed.strftime("%Y-%m-%d %H:%M:%S UTC")
    return parsed.strftime("%Y-%m-%d %H:%M:%S %z")


def format_space(space: Dict[str, Any]) -> str:
    """Render a Space resource (name, code, URI, config, active conference)."""
    lines = [
        f"- Space: {space.get('name', 'N/A')}",
        f"- Meeting code: {space.get('meetingCode', 'N/A')}",
        f"- Meeting URI: {space.get('meetingUri', 'N/A')}",
    ]
    active = (space.get("activeConference") or {}).get("conferenceRecord")
    lines.append(
        f"- Active conference: {active}" if active else "- Active conference: none"
    )

    config = space.get("config") or {}
    if config:
        lines.append("- Config:")
        if config.get("accessType"):
            lines.append(f"  - Access type: {config['accessType']}")
        if config.get("entryPointAccess"):
            lines.append(f"  - Entry point access: {config['entryPointAccess']}")
        if config.get("moderation"):
            lines.append(f"  - Moderation: {config['moderation']}")
        restrictions = config.get("moderationRestrictions") or {}
        for key, label in (
            ("chatRestriction", "Chat"),
            ("presentRestriction", "Present"),
            ("reactionRestriction", "Reactions"),
            ("defaultJoinAsViewerType", "Default join as viewer"),
        ):
            if restrictions.get(key):
                lines.append(f"  - {label}: {restrictions[key]}")
        artifacts = config.get("artifactConfig") or {}
        for key, sub_key, label in (
            ("recordingConfig", "autoRecordingGeneration", "Auto recording"),
            (
                "transcriptionConfig",
                "autoTranscriptionGeneration",
                "Auto transcription",
            ),
            ("smartNotesConfig", "autoSmartNotesGeneration", "Auto smart notes"),
        ):
            value = (artifacts.get(key) or {}).get(sub_key)
            if value:
                lines.append(f"  - {label}: {value}")
        if config.get("attendanceReportGenerationType"):
            lines.append(
                f"  - Attendance report: {config['attendanceReportGenerationType']}"
            )

    phone_access = space.get("phoneAccess") or []
    if phone_access:
        lines.append("- Phone access:")
    for phone in phone_access:
        detail = f"  - {phone.get('phoneNumber') or 'N/A'}"
        if phone.get("pin"):
            detail += f" PIN {phone['pin']}"
        if phone.get("regionCode"):
            detail += f" ({phone['regionCode']})"
        lines.append(detail)

    return "\n".join(lines)


def format_member(member: Dict[str, Any]) -> str:
    """Render a Member resource as a single line."""
    name = member.get("name", "N/A")
    email = member.get("email") or "N/A"
    role = member.get("role") or "ROLE_UNSPECIFIED"
    return f"- {email} | role: {role} | {name}"


def format_conference_record(record: Dict[str, Any]) -> str:
    """Render a ConferenceRecord resource."""
    end_time = record.get("endTime")
    status = "ended" if end_time else "active"
    lines = [
        f"- Record: {record.get('name', 'N/A')} ({status})",
        f"  - Space: {record.get('space', 'N/A')}",
        f"  - Start: {format_time(record.get('startTime'))}",
        f"  - End: {format_time(end_time) if end_time else 'in progress'}",
    ]
    if record.get("expireTime"):
        lines.append(f"  - Expires: {format_time(record['expireTime'])}")
    return "\n".join(lines)


def format_person(participant: Dict[str, Any]) -> str:
    """Describe who a Participant is: signed-in, anonymous or phone user."""
    signed_in = participant.get("signedinUser")
    if signed_in:
        display = signed_in.get("displayName") or "Unknown"
        user = signed_in.get("user")
        return f"{display} (signed in{f', {user}' if user else ''})"
    anonymous = participant.get("anonymousUser")
    if anonymous:
        return f"{anonymous.get('displayName') or 'Unknown'} (anonymous)"
    phone = participant.get("phoneUser")
    if phone:
        return f"{phone.get('displayName') or 'Unknown'} (phone)"
    return "Unknown participant"


def format_participant(participant: Dict[str, Any]) -> str:
    """Render a Participant resource."""
    latest_end = participant.get("latestEndTime")
    return "\n".join(
        [
            f"- {format_person(participant)}",
            f"  - Name: {participant.get('name', 'N/A')}",
            f"  - Joined: {format_time(participant.get('earliestStartTime'))}",
            f"  - Left: {format_time(latest_end) if latest_end else 'still in call'}",
        ]
    )


def format_participant_session(session: Dict[str, Any]) -> str:
    """Render a ParticipantSession resource."""
    end_time = session.get("endTime")
    return (
        f"- {session.get('name', 'N/A')}: "
        f"{format_time(session.get('startTime'))} -> "
        f"{format_time(end_time) if end_time else 'ongoing'}"
    )


def _format_destination(destination: Dict[str, Any], id_key: str, label: str) -> str:
    """Render a Drive/Docs destination with its file/document id and export URI."""
    lines = []
    if destination.get(id_key):
        lines.append(f"  - {label}: {destination[id_key]}")
    if destination.get("exportUri"):
        lines.append(f"  - Export URI: {destination['exportUri']}")
    return "\n".join(lines)


def format_recording(recording: Dict[str, Any]) -> str:
    """Render a Recording resource including its Drive destination."""
    lines = [
        f"- Recording: {recording.get('name', 'N/A')}",
        f"  - State: {recording.get('state', 'N/A')}",
        f"  - Start: {format_time(recording.get('startTime'))}",
        f"  - End: {format_time(recording.get('endTime'))}",
    ]
    destination = recording.get("driveDestination") or {}
    if destination:
        lines.append(_format_destination(destination, "file", "Drive file"))
    return "\n".join(line for line in lines if line)


def format_transcript(transcript: Dict[str, Any]) -> str:
    """Render a Transcript resource including its Docs destination."""
    lines = [
        f"- Transcript: {transcript.get('name', 'N/A')}",
        f"  - State: {transcript.get('state', 'N/A')}",
        f"  - Start: {format_time(transcript.get('startTime'))}",
        f"  - End: {format_time(transcript.get('endTime'))}",
    ]
    destination = transcript.get("docsDestination") or {}
    if destination:
        lines.append(_format_destination(destination, "document", "Google Doc"))
    return "\n".join(line for line in lines if line)


def format_transcript_entry(entry: Dict[str, Any]) -> str:
    """Render a TranscriptEntry as ``[time] participant: text``."""
    speaker = entry.get("participant") or "unknown"
    text = (entry.get("text") or "").strip()
    language = entry.get("languageCode")
    suffix = f" [{language}]" if language else ""
    return f"- [{format_time(entry.get('startTime'))}] {speaker}: {text}{suffix}"


def format_smart_note(note: Dict[str, Any]) -> str:
    """Render a SmartNote resource including its Docs destination."""
    lines = [
        f"- Smart note: {note.get('name', 'N/A')}",
        f"  - State: {note.get('state', 'N/A')}",
        f"  - Start: {format_time(note.get('startTime'))}",
        f"  - End: {format_time(note.get('endTime'))}",
    ]
    destination = note.get("docsDestination") or {}
    if destination:
        lines.append(_format_destination(destination, "document", "Google Doc"))
    return "\n".join(line for line in lines if line)


def _format_page(
    header: str,
    items: List[Dict[str, Any]],
    formatter,
    empty_message: str,
    next_page_token: Optional[str],
) -> str:
    """Render a list page: header, items (or an empty note) and the paging hint."""
    lines = [header]
    if items:
        lines.extend(formatter(item) for item in items)
    elif next_page_token:
        lines.append(f"{empty_message} on this page, but more pages remain.")
    else:
        lines.append(f"{empty_message}.")
    if next_page_token:
        lines.append(f"Next page token: {next_page_token}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# API calls: spaces
# ---------------------------------------------------------------------------


async def create_space(service, user_google_email: str, config: Dict[str, Any]) -> str:
    """Call ``spaces.create`` with the optional config and render the new space."""
    body: Dict[str, Any] = {"config": config} if config else {}
    space = await asyncio.to_thread(service.spaces().create(body=body).execute)
    logger.info(
        "[manage_meet_space] Created space %s for %s",
        space.get("name"),
        user_google_email,
    )
    return f"Meet space created for {user_google_email}:\n{format_space(space)}"


async def get_space(service, user_google_email: str, space_name: str) -> str:
    """Call ``spaces.get`` by resource name or meeting code and render the space."""
    name = normalize_space_name(space_name)
    space = await asyncio.to_thread(service.spaces().get(name=name).execute)
    return f"Meet space for {user_google_email}:\n{format_space(space)}"


async def update_space(
    service,
    user_google_email: str,
    space_name: str,
    config: Dict[str, Any],
    update_mask_paths: List[str],
) -> str:
    """Call ``spaces.patch`` with only the config fields that were provided."""
    if not update_mask_paths:
        raise UserInputError(
            "The 'update' action needs at least one config field to change "
            "(access_type, entry_point_access, moderation, chat_restriction, "
            "present_restriction, reaction_restriction, default_join_as_viewer, "
            "auto_recording, auto_transcription, auto_smart_notes, attendance_report)."
        )
    name = normalize_space_name(space_name)
    update_mask = ",".join(update_mask_paths)
    body = {"config": config}
    space = await asyncio.to_thread(
        service.spaces().patch(name=name, updateMask=update_mask, body=body).execute
    )
    logger.info(
        "[manage_meet_space] Updated %s for %s (%s)",
        name,
        user_google_email,
        update_mask,
    )
    return f"Meet space updated for {user_google_email}:\n{format_space(space)}"


async def end_active_conference(
    service, user_google_email: str, space_name: str
) -> str:
    """Call ``spaces.endActiveConference``; ends the running call in the space."""
    name = normalize_space_name(space_name)
    await asyncio.to_thread(
        service.spaces().endActiveConference(name=name, body={}).execute
    )
    logger.info(
        "[manage_meet_space] Ended active conference in %s for %s",
        name,
        user_google_email,
    )
    return f"Ended the active conference in {name} for {user_google_email}."


# ---------------------------------------------------------------------------
# API calls: members
# ---------------------------------------------------------------------------


async def list_members(
    service,
    user_google_email: str,
    space_name: str,
    page_size: Optional[int],
    page_token: Optional[str],
) -> str:
    """Call ``spaces.members.list`` and render one page of members."""
    parent = normalize_space_name(space_name)
    params = {"parent": parent, **_paging_params(page_size, page_token)}
    response = await asyncio.to_thread(
        service.spaces().members().list(**params).execute
    )
    members = response.get("members", [])
    return _format_page(
        f"Members of {parent} for {user_google_email} ({len(members)} on this page):",
        members,
        format_member,
        "No members found",
        response.get("nextPageToken"),
    )


async def get_member(
    service, user_google_email: str, space_name: Optional[str], member_name: str
) -> str:
    """Call ``spaces.members.get`` and render the member."""
    name = normalize_member_name(space_name, member_name)
    member = await asyncio.to_thread(service.spaces().members().get(name=name).execute)
    return f"Member for {user_google_email}:\n{format_member(member)}"


async def add_member(
    service,
    user_google_email: str,
    space_name: str,
    email: Optional[str],
    role: Optional[str],
) -> str:
    """Call ``spaces.members.create`` with ``email`` and optional ``role``."""
    if not email or not str(email).strip():
        raise UserInputError("'email' is required for the 'add' action.")
    parent = normalize_space_name(space_name)
    body: Dict[str, Any] = {"email": str(email).strip()}
    if role is not None:
        body["role"] = _validate_choice(role, MEMBER_ROLES, "role")
    member = await asyncio.to_thread(
        service.spaces().members().create(parent=parent, body=body).execute
    )
    logger.info(
        "[manage_meet_member] Added %s to %s for %s",
        member.get("name"),
        parent,
        user_google_email,
    )
    return f"Member added to {parent} for {user_google_email}:\n{format_member(member)}"


async def update_member_role(
    service,
    user_google_email: str,
    space_name: Optional[str],
    member_name: str,
    role: Optional[str],
) -> str:
    """Call ``spaces.members.patch`` with ``updateMask=role``."""
    if role is None:
        raise UserInputError("'role' is required for the 'update_role' action.")
    name = normalize_member_name(space_name, member_name)
    body = {"role": _validate_choice(role, MEMBER_ROLES, "role")}
    request = service.spaces().members().patch(name=name, updateMask="role", body=body)
    member = await asyncio.to_thread(request.execute)
    return f"Member role updated for {user_google_email}:\n{format_member(member)}"


async def remove_member(
    service, user_google_email: str, space_name: Optional[str], member_name: str
) -> str:
    """Call ``spaces.members.delete``."""
    name = normalize_member_name(space_name, member_name)
    await asyncio.to_thread(service.spaces().members().delete(name=name).execute)
    logger.info("[manage_meet_member] Removed %s for %s", name, user_google_email)
    return f"Removed member {name} for {user_google_email}."


async def batch_update_members(
    service,
    user_google_email: str,
    space_name: str,
    member_names: Optional[List[str]],
    role: Optional[str],
) -> str:
    """Call ``spaces.members.batchUpdate`` to set one role on many members."""
    if not member_names:
        raise UserInputError(
            "'member_names' (list of member ids or resource names) is required for "
            "the 'batch_update' action."
        )
    if role is None:
        raise UserInputError("'role' is required for the 'batch_update' action.")
    if len(member_names) > BATCH_UPDATE_MEMBERS_MAX:
        raise UserInputError(
            f"'batch_update' accepts at most {BATCH_UPDATE_MEMBERS_MAX} members "
            "per call."
        )
    parent = normalize_space_name(space_name)
    role_value = _validate_choice(role, MEMBER_ROLES, "role")
    requests = [
        {
            "member": {
                "name": normalize_member_name(parent, member),
                "role": role_value,
            },
            "updateMask": "role",
        }
        for member in member_names
    ]
    body = {"requests": requests, "updateMask": "role"}
    request = service.spaces().members().batchUpdate(parent=parent, body=body)
    response = await asyncio.to_thread(request.execute)
    members = response.get("members", [])
    lines = [
        f"Updated {len(members)} member role(s) in {parent} for {user_google_email}:"
    ]
    lines.extend(format_member(member) for member in members)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# API calls: conference records
# ---------------------------------------------------------------------------


async def list_conference_records(
    service,
    user_google_email: str,
    filter_string: Optional[str],
    page_size: Optional[int],
    page_token: Optional[str],
) -> str:
    """Call ``conferenceRecords.list`` with the built filter and render one page."""
    params: Dict[str, Any] = _paging_params(page_size, page_token)
    if filter_string:
        params["filter"] = filter_string
    request = service.conferenceRecords().list(**params)
    response = await asyncio.to_thread(request.execute)
    records = response.get("conferenceRecords", [])
    header = f"Conference records for {user_google_email} ({len(records)} on this page)"
    if filter_string:
        header += f" matching: {filter_string}"
    return _format_page(
        header + ":",
        records,
        format_conference_record,
        "No conference records found",
        response.get("nextPageToken"),
    )


async def get_conference_record(
    service, user_google_email: str, conference_record: str
) -> str:
    """Call ``conferenceRecords.get`` and render the record."""
    name = normalize_conference_record_name(conference_record)
    request = service.conferenceRecords().get(name=name)
    record = await asyncio.to_thread(request.execute)
    return (
        f"Conference record for {user_google_email}:\n"
        f"{format_conference_record(record)}"
    )


# ---------------------------------------------------------------------------
# API calls: conference artifacts
# ---------------------------------------------------------------------------


async def list_conference_artifacts(
    service,
    user_google_email: str,
    conference_record: str,
    kind: str,
    participant: Optional[str],
    transcript: Optional[str],
    page_size: Optional[int],
    page_token: Optional[str],
) -> str:
    """List one kind of artifact under a conference record and render one page.

    ``participant_sessions`` needs *participant*; ``transcript_entries`` needs
    *transcript*. Both accept a bare id or the full resource name.
    """
    kind_value = str(kind).strip().lower()
    if kind_value not in ARTIFACT_KINDS:
        raise UserInputError(
            f"Invalid kind '{kind}'. Must be one of: {', '.join(ARTIFACT_KINDS)}"
        )
    record = normalize_conference_record_name(conference_record)
    paging = _paging_params(page_size, page_token)
    records = service.conferenceRecords()

    if kind_value == "participants":
        request = records.participants().list(parent=record, **paging)
        items_key, formatter, label = "participants", format_participant, "Participants"
        scope = record
    elif kind_value == "participant_sessions":
        parent = _child_name(record, "participants", participant, "participant")
        request = (
            records.participants().participantSessions().list(parent=parent, **paging)
        )
        items_key = "participantSessions"
        formatter, label, scope = format_participant_session, "Sessions", parent
    elif kind_value == "recordings":
        request = records.recordings().list(parent=record, **paging)
        items_key, formatter, label = "recordings", format_recording, "Recordings"
        scope = record
    elif kind_value == "transcripts":
        request = records.transcripts().list(parent=record, **paging)
        items_key, formatter, label = "transcripts", format_transcript, "Transcripts"
        scope = record
    elif kind_value == "transcript_entries":
        parent = _child_name(record, "transcripts", transcript, "transcript")
        request = records.transcripts().entries().list(parent=parent, **paging)
        items_key = "transcriptEntries"
        formatter, label, scope = format_transcript_entry, "Transcript entries", parent
    else:  # smart_notes
        request = records.smartNotes().list(parent=record, **paging)
        items_key, formatter, label = "smartNotes", format_smart_note, "Smart notes"
        scope = record

    response = await asyncio.to_thread(request.execute)
    items = response.get(items_key, [])
    return _format_page(
        f"{label} of {scope} for {user_google_email} ({len(items)} on this page):",
        items,
        formatter,
        f"No {label.lower()} found",
        response.get("nextPageToken"),
    )
