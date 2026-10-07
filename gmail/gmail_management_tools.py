"""
Gmail management MCP tools.

Message, thread and draft actions, mailbox settings, the user profile and the
history feed. Logic lives in ``gmail.gmail_management_helpers``.
"""

import asyncio
import logging
from typing import Literal, Optional

from mcp.types import ToolAnnotations

from auth.permissions import is_action_denied
from auth.service_decorator import require_google_service
from core.server import server
from core.utils import StringList, UserInputError, handle_http_errors
from gmail.gmail_management_helpers import (
    DRAFT_ACTIONS,
    HISTORY_DEFAULT_MAX_RESULTS,
    LIST_DEFAULT_MAX_RESULTS,
    MESSAGE_ACTIONS,
    SETTINGS_ACTIONS,
    SHARING_SETTINGS_ACTIONS,
    THREAD_ACTIONS,
    delete_draft,
    fetch_draft,
    format_draft,
    format_profile,
    get_settings_overview,
    list_drafts,
    list_history,
    list_threads,
    resolve_id_list,
    run_message_action,
    run_thread_action,
    send_draft,
    update_auto_forwarding,
    update_draft,
    update_imap,
    update_language,
    update_pop,
    update_signature,
    update_vacation,
)

logger = logging.getLogger(__name__)


def _check_action(service_name: str, action: str, valid: tuple) -> None:
    """Reject unknown actions and actions denied by --permissions."""
    if action not in valid:
        raise UserInputError(
            f"Invalid action '{action}'. Must be one of: {', '.join(valid)}"
        )
    if is_action_denied(service_name, action):
        raise UserInputError(
            f"The '{action}' action is not allowed under the current permission level."
        )


@server.tool(
    title="Manage Gmail Message",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@require_google_service("gmail", "gmail_modify")
@handle_http_errors("manage_gmail_message", service_type="gmail")
async def manage_gmail_message(
    service,
    user_google_email: str,
    action: Literal[
        "trash",
        "untrash",
        "archive",
        "mark_read",
        "mark_unread",
        "star",
        "unstar",
        "mark_important",
        "mark_not_important",
        "delete",
    ],
    message_id: Optional[str] = None,
    message_ids: Optional[StringList] = None,
) -> str:
    """
    Applies a mailbox action to one or more Gmail messages.

    Label-based actions (archive, mark_read, mark_unread, star, unstar,
    mark_important, mark_not_important) use messages.modify for one message and
    messages.batchModify for several. trash/untrash move messages in and out of
    Trash and are reversible. delete is PERMANENT and bypasses Trash: it needs
    the full Gmail scope (https://mail.google.com/), which this server does not
    request by default, so it fails with a clear scope error unless that scope
    was granted. Prefer trash.

    Args:
        user_google_email (str): The user's Google email address. Required.
        action (str): One of trash, untrash, archive (remove INBOX), mark_read, mark_unread, star, unstar, mark_important, mark_not_important, delete (permanent; requires https://mail.google.com/).
        message_id (Optional[str]): A single message ID. Provide this or message_ids.
        message_ids (Optional[List[str]]): Several message IDs to act on together. Label actions are batched (up to 1000 per request).

    Returns:
        str: Confirmation listing the action and the affected message IDs.
    """
    logger.info(
        f"[manage_gmail_message] Invoked. Email: '{user_google_email}', Action: '{action}'"
    )
    _check_action("gmail", action, MESSAGE_ACTIONS)
    ids = resolve_id_list(message_id, message_ids, "message")
    return await run_message_action(service, action, ids)


@server.tool(
    title="Manage Gmail Draft",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
@require_google_service("gmail", "gmail_compose")
@handle_http_errors("manage_gmail_draft", service_type="gmail")
async def manage_gmail_draft(
    service,
    user_google_email: str,
    action: Literal["list", "get", "update", "send", "delete"],
    draft_id: Optional[str] = None,
    query: Optional[str] = None,
    max_results: int = LIST_DEFAULT_MAX_RESULTS,
    page_token: Optional[str] = None,
    subject: Optional[str] = None,
    body: Optional[str] = None,
    body_format: Optional[Literal["plain", "html"]] = None,
    to: Optional[str] = None,
    cc: Optional[str] = None,
    bcc: Optional[str] = None,
) -> str:
    """
    Lists, reads, updates, sends or deletes Gmail drafts.

    Use draft_gmail_message to create a draft. update replaces the draft's
    content while keeping its thread; fields you leave out are copied from the
    current draft, but attachments on the current draft are not carried over.
    send sends the draft as-is and returns the sent message ID.

    Args:
        user_google_email (str): The user's Google email address. Required.
        action (str): One of list, get, update, send, delete.
        draft_id (Optional[str]): Draft ID. Required for get, update, send and delete.
        query (Optional[str]): Gmail search query to filter drafts (list only), e.g. "subject:report".
        max_results (int): Page size for list (default 20, max 500).
        page_token (Optional[str]): Page token from a previous list call.
        subject (Optional[str]): New subject (update only).
        body (Optional[str]): New body (update only). Replaces the existing body.
        body_format (Optional[str]): 'plain' or 'html' for the new body (update only). Defaults to 'plain' when body is given.
        to (Optional[str]): New To recipients, comma separated (update only). Pass "" to clear.
        cc (Optional[str]): New Cc recipients (update only). Pass "" to clear.
        bcc (Optional[str]): New Bcc recipients (update only). Pass "" to clear.

    Returns:
        str: Draft listing, full draft content, or a confirmation of the change.
    """
    logger.info(
        f"[manage_gmail_draft] Invoked. Email: '{user_google_email}', Action: '{action}'"
    )
    _check_action("gmail", action, DRAFT_ACTIONS)

    if action == "list":
        return await list_drafts(service, query, max_results, page_token)

    if not draft_id or not draft_id.strip():
        raise UserInputError(f"'draft_id' is required for the '{action}' action.")
    draft_id = draft_id.strip()

    if action == "get":
        return format_draft(await fetch_draft(service, draft_id))
    if action == "update":
        return await update_draft(
            service, draft_id, subject, body, body_format, to, cc, bcc
        )
    if action == "send":
        return await send_draft(service, draft_id)
    return await delete_draft(service, draft_id)


@server.tool(
    title="Manage Gmail Thread",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@require_google_service("gmail", "gmail_modify")
@handle_http_errors("manage_gmail_thread", service_type="gmail")
async def manage_gmail_thread(
    service,
    user_google_email: str,
    action: Literal["list", "modify", "trash", "untrash", "archive", "delete"],
    thread_id: Optional[str] = None,
    thread_ids: Optional[StringList] = None,
    query: Optional[str] = None,
    label_ids: Optional[StringList] = None,
    max_results: int = LIST_DEFAULT_MAX_RESULTS,
    page_token: Optional[str] = None,
    include_spam_trash: bool = False,
    add_label_ids: Optional[StringList] = None,
    remove_label_ids: Optional[StringList] = None,
) -> str:
    """
    Lists Gmail threads or applies an action to whole threads.

    modify adds/removes labels on every message in the thread; archive removes
    INBOX; trash/untrash are reversible. delete is PERMANENT and needs the full
    Gmail scope (https://mail.google.com/), which this server does not request
    by default, so it fails with a clear scope error unless that scope was
    granted. Use get_gmail_thread_content to read a thread.

    Args:
        user_google_email (str): The user's Google email address. Required.
        action (str): One of list, modify, trash, untrash, archive, delete (permanent; requires https://mail.google.com/).
        thread_id (Optional[str]): A single thread ID. Provide this or thread_ids for every action except list.
        thread_ids (Optional[List[str]]): Several thread IDs to act on together.
        query (Optional[str]): Gmail search query (list only), e.g. "from:alice newer_than:7d".
        label_ids (Optional[List[str]]): Only list threads carrying all of these label IDs (list only).
        max_results (int): Page size for list (default 20, max 500).
        page_token (Optional[str]): Page token from a previous list call.
        include_spam_trash (bool): Include threads from Spam and Trash in list results.
        add_label_ids (Optional[List[str]]): Label IDs to add (modify only).
        remove_label_ids (Optional[List[str]]): Label IDs to remove (modify only).

    Returns:
        str: Thread listing or a confirmation listing the affected thread IDs.
    """
    logger.info(
        f"[manage_gmail_thread] Invoked. Email: '{user_google_email}', Action: '{action}'"
    )
    _check_action("gmail", action, THREAD_ACTIONS)

    if action == "list":
        return await list_threads(
            service, query, label_ids, max_results, page_token, include_spam_trash
        )

    ids = resolve_id_list(thread_id, thread_ids, "thread")
    return await run_thread_action(
        service, action, ids, add_label_ids, remove_label_ids
    )


@server.tool(
    title="Get Gmail Profile",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@require_google_service("gmail", "gmail_read")
@handle_http_errors("get_gmail_profile", is_read_only=True, service_type="gmail")
async def get_gmail_profile(service, user_google_email: str) -> str:
    """
    Returns the Gmail profile: email address, message and thread totals, and
    the current history ID (the starting point for list_gmail_history).

    Args:
        user_google_email (str): The user's Google email address. Required.

    Returns:
        str: Email address, messagesTotal, threadsTotal and historyId.
    """
    logger.info(f"[get_gmail_profile] Invoked. Email: '{user_google_email}'")
    profile = await asyncio.to_thread(service.users().getProfile(userId="me").execute)
    return format_profile(profile)


@server.tool(
    title="Manage Gmail Settings",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@require_google_service("gmail", "gmail_settings_basic")
@handle_http_errors("manage_gmail_settings", service_type="gmail")
async def manage_gmail_settings(
    service,
    user_google_email: str,
    action: Literal[
        "get", "update_vacation", "update_imap", "update_pop", "update_language"
    ],
    enable: Optional[bool] = None,
    vacation_subject: Optional[str] = None,
    vacation_body: Optional[str] = None,
    vacation_body_format: Literal["plain", "html"] = "plain",
    vacation_start_date: Optional[str] = None,
    vacation_end_date: Optional[str] = None,
    restrict_to_contacts: Optional[bool] = None,
    restrict_to_domain: Optional[bool] = None,
    imap_auto_expunge: Optional[bool] = None,
    imap_expunge_behavior: Optional[
        Literal["archive", "trash", "deleteForever"]
    ] = None,
    imap_max_folder_size: Optional[int] = None,
    pop_access_window: Optional[Literal["disabled", "fromNowOn", "allMail"]] = None,
    pop_disposition: Optional[
        Literal["leaveInInbox", "archive", "trash", "markRead"]
    ] = None,
    display_language: Optional[str] = None,
) -> str:
    """
    Reads or updates Gmail mailbox settings that need only gmail.settings.basic.

    get returns the vacation responder, IMAP, POP, display language,
    auto-forwarding status, send-as addresses (primary/default flags and
    whether a signature is set) and forwarding addresses. Updates merge the
    given fields into the current settings. Signature and auto-forwarding
    changes live in manage_gmail_sharing_settings.

    Args:
        user_google_email (str): The user's Google email address. Required.
        action (str): One of get, update_vacation, update_imap, update_pop, update_language.
        enable (Optional[bool]): update_vacation: turn the auto-reply on/off. update_imap: enable/disable IMAP.
        vacation_subject (Optional[str]): Auto-reply subject (update_vacation).
        vacation_body (Optional[str]): Auto-reply body (update_vacation). Replaces the existing body.
        vacation_body_format (str): 'plain' or 'html' for vacation_body. Defaults to 'plain'.
        vacation_start_date (Optional[str]): First day/time of the auto-reply as YYYY-MM-DD or ISO 8601 (UTC when no offset). Pass "" to clear.
        vacation_end_date (Optional[str]): Last day/time as YYYY-MM-DD (inclusive, end of day UTC) or ISO 8601. Pass "" to clear.
        restrict_to_contacts (Optional[bool]): Only auto-reply to people in Contacts (update_vacation).
        restrict_to_domain (Optional[bool]): Only auto-reply to people in the same Workspace domain (update_vacation).
        imap_auto_expunge (Optional[bool]): Immediately expunge messages deleted over IMAP (update_imap).
        imap_expunge_behavior (Optional[str]): What happens to messages deleted over IMAP when auto-expunge is off: archive, trash or deleteForever (update_imap).
        imap_max_folder_size (Optional[int]): Max messages per IMAP folder; 0 means no limit (update_imap).
        pop_access_window (Optional[str]): Which messages POP can download: disabled, fromNowOn or allMail (update_pop).
        pop_disposition (Optional[str]): What Gmail does with messages after POP access: leaveInInbox, archive, trash or markRead (update_pop).
        display_language (Optional[str]): RFC 3066 language tag such as 'en', 'en-GB', 'fr' (update_language).

    Returns:
        str: The settings overview or a confirmation of the updated setting.
    """
    logger.info(
        f"[manage_gmail_settings] Invoked. Email: '{user_google_email}', Action: '{action}'"
    )
    _check_action("gmail", action, SETTINGS_ACTIONS)

    if action == "get":
        return await get_settings_overview(service)
    if action == "update_vacation":
        return await update_vacation(
            service,
            enable,
            vacation_subject,
            vacation_body,
            vacation_body_format,
            vacation_start_date,
            vacation_end_date,
            restrict_to_contacts,
            restrict_to_domain,
        )
    if action == "update_imap":
        return await update_imap(
            service,
            enable,
            imap_auto_expunge,
            imap_expunge_behavior,
            imap_max_folder_size,
        )
    if action == "update_pop":
        return await update_pop(service, pop_access_window, pop_disposition)
    return await update_language(service, display_language)


@server.tool(
    title="Manage Gmail Sharing Settings",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@require_google_service("gmail", "gmail_settings_sharing")
@handle_http_errors("manage_gmail_sharing_settings", service_type="gmail")
async def manage_gmail_sharing_settings(
    service,
    user_google_email: str,
    action: Literal["update_signature", "update_auto_forwarding"],
    send_as_email: Optional[str] = None,
    signature: Optional[str] = None,
    enable: Optional[bool] = None,
    forward_to: Optional[str] = None,
    forward_disposition: Optional[
        Literal["leaveInInbox", "archive", "trash", "markRead"]
    ] = None,
) -> str:
    """
    Updates Gmail settings that require the gmail.settings.sharing scope:
    send-as signatures and auto-forwarding.

    This server does not request gmail.settings.sharing by default, so this
    tool reports a clear scope error until the user re-consents with that
    scope. All other settings are in manage_gmail_settings.

    Args:
        user_google_email (str): The user's Google email address. Required.
        action (str): update_signature or update_auto_forwarding.
        send_as_email (Optional[str]): Send-as address whose signature to change (update_signature). Defaults to user_google_email.
        signature (Optional[str]): New signature as HTML (update_signature). Pass "" to clear.
        enable (Optional[bool]): Turn auto-forwarding on or off (update_auto_forwarding). Required.
        forward_to (Optional[str]): Destination address when enabling; must already be a verified forwarding address (update_auto_forwarding).
        forward_disposition (Optional[str]): What happens to forwarded mail: leaveInInbox (default), archive, trash or markRead (update_auto_forwarding).

    Returns:
        str: Confirmation of the updated setting.
    """
    logger.info(
        f"[manage_gmail_sharing_settings] Invoked. Email: '{user_google_email}', Action: '{action}'"
    )
    _check_action("gmail", action, SHARING_SETTINGS_ACTIONS)

    if action == "update_signature":
        return await update_signature(
            service, send_as_email or user_google_email, signature
        )
    return await update_auto_forwarding(
        service, enable, forward_to, forward_disposition
    )


@server.tool(
    title="List Gmail History",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@require_google_service("gmail", "gmail_read")
@handle_http_errors("list_gmail_history", is_read_only=True, service_type="gmail")
async def list_gmail_history(
    service,
    user_google_email: str,
    start_history_id: str,
    history_types: Optional[StringList] = None,
    label_id: Optional[str] = None,
    max_results: int = HISTORY_DEFAULT_MAX_RESULTS,
    page_token: Optional[str] = None,
) -> str:
    """
    Lists mailbox changes since a history ID: messages added or deleted and
    labels added or removed. Get a starting history ID from get_gmail_profile
    (or from a previous call's "Current history ID") and store it for the
    next incremental sync.

    Args:
        user_google_email (str): The user's Google email address. Required.
        start_history_id (str): Return changes after this history ID. Gmail keeps roughly a week of history; an expired ID yields an input error asking for a fresh one.
        history_types (Optional[List[str]]): Restrict to some of messageAdded, messageDeleted, labelAdded, labelRemoved.
        label_id (Optional[str]): Only return changes to messages carrying this label ID.
        max_results (int): Maximum history records per page (default 100, max 500).
        page_token (Optional[str]): Page token from a previous call.

    Returns:
        str: Counts by change type, the changed message IDs, the current history ID and a next page token when more exist.
    """
    logger.info(
        f"[list_gmail_history] Invoked. Email: '{user_google_email}', Start: '{start_history_id}'"
    )
    return await list_history(
        service, start_history_id, history_types, label_id, max_results, page_token
    )
