"""
Google Keep MCP Tools

This module provides MCP tools for interacting with the Google Keep API
(notes, checklists, attachments and note permissions).

The Google Keep API is available to Google Workspace accounts only; it does
not work for consumer @gmail.com accounts.
"""

import logging
from typing import Optional

from mcp import Resource
from mcp.types import ToolAnnotations

from auth.permissions import is_action_denied
from auth.service_decorator import require_google_service
from core.server import server
from core.utils import DictList, StringList, UserInputError, handle_http_errors
from gkeep.keep_helpers import (
    LIST_NOTES_PAGE_SIZE_DEFAULT,
    LIST_NOTES_PAGE_SIZE_MAX,
    build_create_note_request,
    build_permission_requests,
    download_attachment as download_attachment_impl,
    execute_keep_request,
    format_note_detail,
    format_notes_list,
    format_permission_line,
    format_permissions,
    normalize_attachment_name,
    normalize_note_name,
    normalize_permission_names,
)

logger = logging.getLogger(__name__)


@server.tool(
    title="List Keep Notes",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@require_google_service("keep", "keep_read")  # type: ignore
@handle_http_errors("list_keep_notes", is_read_only=True, service_type="keep")  # type: ignore
async def list_keep_notes(
    service: Resource,
    user_google_email: str,
    filter: Optional[str] = None,
    page_size: int = LIST_NOTES_PAGE_SIZE_DEFAULT,
    page_token: Optional[str] = None,
) -> str:
    """
    List Google Keep notes as a compact summary (name, title, kind, item counts,
    attachment count, update time). Google Workspace accounts only.

    Args:
        user_google_email (str): The user's Google email address. Required.
        filter (Optional[str]): AIP-160 filter string passed straight to the API.
            Fields: create_time, update_time, trash_time, trashed.
            Example: "trashed=false". When omitted the API applies its default
            trashed filter.
        page_size (int): Notes per page (default 20, max 100).
        page_token (Optional[str]): Token from a previous page.

    Returns:
        str: One entry per note plus a next page token when more exist.
    """
    logger.info(f"[list_keep_notes] Invoked. Email: '{user_google_email}'")

    if page_size is None or page_size < 1 or page_size > LIST_NOTES_PAGE_SIZE_MAX:
        raise UserInputError(
            f"'page_size' must be between 1 and {LIST_NOTES_PAGE_SIZE_MAX}."
        )

    params = {"pageSize": page_size}
    if filter:
        params["filter"] = filter
    if page_token:
        params["pageToken"] = page_token

    result = await execute_keep_request(service.notes().list(**params))
    notes = result.get("notes", [])
    logger.info(f"[list_keep_notes] Found {len(notes)} notes for {user_google_email}")
    return format_notes_list(
        notes, user_google_email, filter, result.get("nextPageToken")
    )


@server.tool(
    title="Get Keep Note",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@require_google_service("keep", "keep_read")  # type: ignore
@handle_http_errors("get_keep_note", is_read_only=True, service_type="keep")  # type: ignore
async def get_keep_note(
    service: Resource,
    user_google_email: str,
    note_id: str,
    download_attachment: Optional[str] = None,
    mime_type: Optional[str] = None,
    include_base64: bool = False,
) -> str:
    """
    Get one Google Keep note with its full text or checklist (nested items with
    check marks), attachments and permissions; optionally download an
    attachment. Google Workspace accounts only.

    Args:
        user_google_email (str): The user's Google email address. Required.
        note_id (str): Note resource name ("notes/abc123") or bare id ("abc123").
        download_attachment (Optional[str]): Attachment to download instead of
            rendering the note: its full name ("notes/abc/attachments/xyz") or
            bare id. Text-like types are returned inline; binaries report size.
        mime_type (Optional[str]): MIME type to download the attachment as. Must
            be one the attachment lists; may be omitted when it lists exactly one.
        include_base64 (bool): For binary attachments, include the content as
            standard base64 (default False).

    Returns:
        str: The rendered note, or the attachment description/content.
    """
    logger.info(
        f"[get_keep_note] Invoked. Email: '{user_google_email}', Note: '{note_id}'"
    )
    note_name = normalize_note_name(note_id)
    note = await execute_keep_request(service.notes().get(name=note_name))

    if download_attachment:
        attachment_name = normalize_attachment_name(note_name, download_attachment)
        return await download_attachment_impl(
            service, note, attachment_name, mime_type, include_base64
        )

    return format_note_detail(note)


@server.tool(
    title="Manage Keep Note",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
@require_google_service("keep", "keep")  # type: ignore
@handle_http_errors("manage_keep_note", service_type="keep")  # type: ignore
async def manage_keep_note(
    service: Resource,
    user_google_email: str,
    action: str,
    note_id: Optional[str] = None,
    title: Optional[str] = None,
    text: Optional[str] = None,
    list_items: Optional[DictList] = None,
) -> str:
    """
    Create or delete a Google Keep note. Google Workspace accounts only.

    Args:
        user_google_email (str): The user's Google email address. Required.
        action (str): "create" or "delete".
        note_id (Optional[str]): Note to delete ("notes/abc123" or "abc123").
            Required for "delete". Deletion is immediate and cannot be undone;
            the caller must own the note.
        title (Optional[str]): Title for "create".
        text (Optional[str]): Body for a text note ("create"). Mutually exclusive
            with list_items.
        list_items (Optional[List[dict]]): Body for a checklist note ("create"):
            a list of {"text": str, "checked": bool, "children": [items]}.
            Mutually exclusive with text.

    Returns:
        str: Confirmation with the note name.
    """
    logger.info(
        f"[manage_keep_note] Invoked. Email: '{user_google_email}', Action: '{action}'"
    )

    valid_actions = ("create", "delete")
    if action not in valid_actions:
        raise UserInputError(
            f"Invalid action '{action}'. Must be one of: {', '.join(valid_actions)}"
        )
    if is_action_denied("keep", action):
        raise UserInputError(
            f"The '{action}' action is not allowed under the current permission level."
        )

    if action == "create":
        body = build_create_note_request(title, text, list_items)
        note = await execute_keep_request(service.notes().create(body=body))
        return (
            f"Created Keep note '{note.get('title') or '(untitled)'}' "
            f"(ID: {note.get('name', 'N/A')}) for {user_google_email}."
        )

    # action == "delete"
    if not note_id:
        raise UserInputError("'note_id' is required for the 'delete' action.")
    note_name = normalize_note_name(note_id)
    await execute_keep_request(service.notes().delete(name=note_name))
    return f"Deleted Keep note {note_name} for {user_google_email}."


@server.tool(
    title="Manage Keep Note Permissions",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
@require_google_service("keep", "keep")  # type: ignore
@handle_http_errors("manage_keep_note_permissions", service_type="keep")  # type: ignore
async def manage_keep_note_permissions(
    service: Resource,
    user_google_email: str,
    action: str,
    note_id: str,
    emails: Optional[StringList] = None,
    permission_names: Optional[StringList] = None,
) -> str:
    """
    List, grant or revoke collaborator access on a Google Keep note. Google
    Workspace accounts only; the caller must own the note to grant or revoke.

    Args:
        user_google_email (str): The user's Google email address. Required.
        action (str): "list", "grant" or "revoke".
        note_id (str): Note resource name ("notes/abc123") or bare id.
        emails (Optional[List[str]]): Email addresses to grant WRITER access.
            Required for "grant".
        permission_names (Optional[List[str]]): Permissions to revoke, as full
            names ("notes/abc/permissions/xyz") or bare ids. Required for
            "revoke". Use "list" to find them.

    Returns:
        str: The permissions on the note, or a confirmation of the change.
    """
    logger.info(
        f"[manage_keep_note_permissions] Invoked. Email: '{user_google_email}', "
        f"Action: '{action}', Note: '{note_id}'"
    )

    valid_actions = ("list", "grant", "revoke")
    if action not in valid_actions:
        raise UserInputError(
            f"Invalid action '{action}'. Must be one of: {', '.join(valid_actions)}"
        )
    if is_action_denied("keep", action):
        raise UserInputError(
            f"The '{action}' action is not allowed under the current permission level."
        )
    note_name = normalize_note_name(note_id)

    if action == "list":
        note = await execute_keep_request(service.notes().get(name=note_name))
        return format_permissions(note_name, note.get("permissions") or [])

    if action == "grant":
        if not emails:
            raise UserInputError("'emails' is required for the 'grant' action.")
        requests = build_permission_requests(note_name, emails)
        result = await execute_keep_request(
            service.notes()
            .permissions()
            .batchCreate(parent=note_name, body={"requests": requests})
        )
        granted = result.get("permissions") or []
        lines = [
            f"Granted WRITER access on {note_name} to {len(granted)} principal(s):"
        ]
        lines.extend(format_permission_line(p) for p in granted)
        return "\n".join(lines)

    # action == "revoke"
    if not permission_names:
        raise UserInputError("'permission_names' is required for the 'revoke' action.")
    names = normalize_permission_names(note_name, permission_names)
    await execute_keep_request(
        service.notes()
        .permissions()
        .batchDelete(parent=note_name, body={"names": names})
    )
    return f"Revoked {len(names)} permission(s) on {note_name}:\n" + "\n".join(
        f"- {name}" for name in names
    )
