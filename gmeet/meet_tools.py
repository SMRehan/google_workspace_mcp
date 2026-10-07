"""
Google Meet MCP Tools

This module provides MCP tools for the Google Meet REST API (v2): meeting
spaces and their settings, space members (co-hosts), conference records and
the artifacts a conference leaves behind (participants, recordings,
transcripts, smart notes). All logic lives in gmeet.meet_helpers.
"""

import logging
from typing import Optional

from mcp import Resource
from mcp.types import ToolAnnotations

from auth.permissions import is_action_denied
from auth.service_decorator import require_google_service
from core.server import server
from core.utils import StringList, UserInputError, handle_http_errors
from gmeet import meet_helpers

logger = logging.getLogger(__name__)

SPACE_ACTIONS = ("create", "get", "update", "end_active_conference")
MEMBER_ACTIONS = ("list", "get", "add", "update_role", "remove", "batch_update")


def _check_action(action: str, valid: tuple, tool_name: str) -> str:
    """Validate *action* against *valid* and the granular permission level."""
    normalized = str(action).strip().lower()
    if normalized not in valid:
        raise UserInputError(
            f"Invalid action '{action}'. Must be one of: {', '.join(valid)}"
        )
    if is_action_denied("meet", normalized):
        raise UserInputError(
            f"The '{normalized}' action is not allowed under the current permission level."
        )
    logger.info("[%s] Action '%s'", tool_name, normalized)
    return normalized


@server.tool(
    title="Manage Meet Space",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
@require_google_service("meet", ["meet_created", "meet_settings"])  # type: ignore
@handle_http_errors("manage_meet_space", service_type="meet")  # type: ignore
async def manage_meet_space(
    service: Resource,
    user_google_email: str,
    action: str,
    space_name: Optional[str] = None,
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
) -> str:
    """
    Create, inspect, reconfigure a Google Meet space, or end its active conference.

    A space is a persistent meeting link. "create" returns the space name, meeting
    code and meeting URI. Config fields apply to "create" (initial settings) and
    "update" (only the fields given are changed; an updateMask is built for them).

    Args:
        user_google_email (str): The user's Google email address. Required.
        action (str): One of "create", "get", "update", "end_active_conference".
        space_name (Optional[str]): Space to act on, as "spaces/{id}", a meeting code
            such as "abc-mnop-xyz" or a meet.google.com URL. Required for every action
            except "create".
        access_type (Optional[str]): Who can join without knocking: "OPEN", "TRUSTED"
            (same organisation or invited) or "RESTRICTED" (invited only).
        entry_point_access (Optional[str]): "ALL" or "CREATOR_APP_ONLY" (join only via
            the app that created the space).
        moderation (Optional[str]): Host management "ON" or "OFF".
        chat_restriction (Optional[str]): "HOSTS_ONLY" or "NO_RESTRICTION". Needs
            moderation ON.
        present_restriction (Optional[str]): "HOSTS_ONLY" or "NO_RESTRICTION". Needs
            moderation ON.
        reaction_restriction (Optional[str]): "HOSTS_ONLY" or "NO_RESTRICTION". Needs
            moderation ON.
        default_join_as_viewer (Optional[str]): "ON" makes non-hosts join as viewers.
            Needs moderation ON.
        auto_recording (Optional[str]): Auto-record every conference: "ON" or "OFF".
        auto_transcription (Optional[str]): Auto-transcribe: "ON" or "OFF".
        auto_smart_notes (Optional[str]): Auto-generate Gemini notes: "ON" or "OFF".
        attendance_report (Optional[str]): Generate attendance reports: "ON" or "OFF".

    Returns:
        str: The space (name, meeting code, URI, config, active conference) or, for
            "end_active_conference", a confirmation.
    """
    action = _check_action(action, SPACE_ACTIONS, "manage_meet_space")
    config, update_mask = meet_helpers.build_space_config(
        access_type=access_type,
        entry_point_access=entry_point_access,
        moderation=moderation,
        chat_restriction=chat_restriction,
        present_restriction=present_restriction,
        reaction_restriction=reaction_restriction,
        default_join_as_viewer=default_join_as_viewer,
        auto_recording=auto_recording,
        auto_transcription=auto_transcription,
        auto_smart_notes=auto_smart_notes,
        attendance_report=attendance_report,
    )

    if action == "create":
        return await meet_helpers.create_space(service, user_google_email, config)

    if update_mask and action != "update":
        raise UserInputError(
            f"Config fields are only accepted by 'create' and 'update', not '{action}'."
        )

    if action == "get":
        return await meet_helpers.get_space(service, user_google_email, space_name)
    if action == "update":
        return await meet_helpers.update_space(
            service, user_google_email, space_name, config, update_mask
        )
    return await meet_helpers.end_active_conference(
        service, user_google_email, space_name
    )


@server.tool(
    title="Manage Meet Member",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
@require_google_service("meet", "meet_created")  # type: ignore
@handle_http_errors("manage_meet_member", service_type="meet")  # type: ignore
async def manage_meet_member(
    service: Resource,
    user_google_email: str,
    action: str,
    space_name: str,
    member_name: Optional[str] = None,
    member_names: Optional[StringList] = None,
    email: Optional[str] = None,
    role: Optional[str] = None,
    page_size: Optional[int] = None,
    page_token: Optional[str] = None,
) -> str:
    """
    List, inspect, add, re-role or remove members of a Google Meet space.

    Members are people pre-granted a role in the space (for example co-hosts) and
    can join a RESTRICTED space without knocking. Only the space owner can manage
    members. Members are listed one page at a time.

    Args:
        user_google_email (str): The user's Google email address. Required.
        action (str): One of "list", "get", "add", "update_role", "remove",
            "batch_update".
        space_name (str): The space, as "spaces/{id}", a meeting code or a
            meet.google.com URL. Required.
        member_name (Optional[str]): Member id or "spaces/{id}/members/{member}".
            Required for "get", "update_role" and "remove".
        member_names (Optional[List[str]]): Member ids or resource names for
            "batch_update" (max 500); every member gets the same role.
        email (Optional[str]): Email of the person to add. Required for "add".
        role (Optional[str]): "COHOST" or "ROLE_UNSPECIFIED" (plain member). Optional
            for "add"; required for "update_role" and "batch_update".
        page_size (Optional[int]): Members per page for "list".
        page_token (Optional[str]): Continuation token from a previous "list" page.

    Returns:
        str: The member(s) affected, or the requested page of members.
    """
    action = _check_action(action, MEMBER_ACTIONS, "manage_meet_member")

    if action == "list":
        return await meet_helpers.list_members(
            service, user_google_email, space_name, page_size, page_token
        )
    if action == "get":
        return await meet_helpers.get_member(
            service, user_google_email, space_name, member_name
        )
    if action == "add":
        return await meet_helpers.add_member(
            service, user_google_email, space_name, email, role
        )
    if action == "update_role":
        return await meet_helpers.update_member_role(
            service, user_google_email, space_name, member_name, role
        )
    if action == "remove":
        return await meet_helpers.remove_member(
            service, user_google_email, space_name, member_name
        )
    return await meet_helpers.batch_update_members(
        service, user_google_email, space_name, member_names, role
    )


@server.tool(
    title="List Meet Conference Records",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@require_google_service("meet", "meet_read")  # type: ignore
@handle_http_errors(
    "list_meet_conference_records", is_read_only=True, service_type="meet"
)  # type: ignore
async def list_meet_conference_records(
    service: Resource,
    user_google_email: str,
    conference_record: Optional[str] = None,
    space_name: Optional[str] = None,
    meeting_code: Optional[str] = None,
    start_time_min: Optional[str] = None,
    end_time_max: Optional[str] = None,
    page_size: Optional[int] = None,
    page_token: Optional[str] = None,
) -> str:
    """
    List past and active Google Meet conferences, or fetch one conference record.

    A conference record is one call that happened in a space; it is the parent of
    participants, recordings, transcripts and smart notes (see
    get_meet_conference_artifacts). Records with no end time are still running.
    Give conference_record to fetch a single record; otherwise the filters are
    combined with AND and results are paged.

    Args:
        user_google_email (str): The user's Google email address. Required.
        conference_record (Optional[str]): "conferenceRecords/{id}" or the bare id.
            When given, that record is returned and every other filter is ignored.
        space_name (Optional[str]): Only conferences held in this space
            ("spaces/{id}"). Mutually exclusive with meeting_code.
        meeting_code (Optional[str]): Only conferences held under this meeting code
            (e.g. "abc-mnop-xyz"). Mutually exclusive with space_name.
        start_time_min (Optional[str]): Only conferences that started at or after
            this RFC 3339 time, e.g. "2026-01-01T00:00:00Z".
        end_time_max (Optional[str]): Only conferences that ended at or before this
            RFC 3339 time.
        page_size (Optional[int]): Records per page.
        page_token (Optional[str]): Continuation token from a previous page.

    Returns:
        str: One record, or a page of records with a "Next page token" line when
            more pages remain.
    """
    logger.info("[list_meet_conference_records] Invoked for %s", user_google_email)

    if conference_record:
        return await meet_helpers.get_conference_record(
            service, user_google_email, conference_record
        )

    filter_string = meet_helpers.build_conference_records_filter(
        space_name=space_name,
        meeting_code=meeting_code,
        start_time_min=start_time_min,
        end_time_max=end_time_max,
    )
    return await meet_helpers.list_conference_records(
        service, user_google_email, filter_string, page_size, page_token
    )


@server.tool(
    title="Get Meet Conference Artifacts",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@require_google_service("meet", "meet_read")  # type: ignore
@handle_http_errors(
    "get_meet_conference_artifacts", is_read_only=True, service_type="meet"
)  # type: ignore
async def get_meet_conference_artifacts(
    service: Resource,
    user_google_email: str,
    conference_record: str,
    kind: str,
    participant: Optional[str] = None,
    transcript: Optional[str] = None,
    page_size: Optional[int] = None,
    page_token: Optional[str] = None,
) -> str:
    """
    List what a Google Meet conference produced: who attended, recordings,
    transcripts, transcript text, or Gemini smart notes.

    Recordings point at a Drive file; transcripts and smart notes point at a Google
    Doc (read them with the Drive/Docs tools). Transcript entries are the spoken
    text, in order, with timestamps and the participant resource name.

    Args:
        user_google_email (str): The user's Google email address. Required.
        conference_record (str): "conferenceRecords/{id}" or the bare id. Find it
            with list_meet_conference_records. Required.
        kind (str): One of "participants", "participant_sessions", "recordings",
            "transcripts", "transcript_entries", "smart_notes".
        participant (Optional[str]): Participant id or full resource name. Required
            for kind "participant_sessions".
        transcript (Optional[str]): Transcript id or full resource name. Required for
            kind "transcript_entries".
        page_size (Optional[int]): Items per page.
        page_token (Optional[str]): Continuation token from a previous page.

    Returns:
        str: One page of the requested artifacts with a "Next page token" line when
            more pages remain.
    """
    logger.info(
        "[get_meet_conference_artifacts] Invoked for %s, kind=%s",
        user_google_email,
        kind,
    )
    return await meet_helpers.list_conference_artifacts(
        service,
        user_google_email,
        conference_record,
        kind,
        participant,
        transcript,
        page_size,
        page_token,
    )
