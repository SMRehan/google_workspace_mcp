"""Helpers for the Gmail management tools.

Business logic for message/thread/draft actions, mailbox settings, the user
profile and the history feed. The tool module (``gmail_management_tools``)
stays thin and delegates here.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from datetime import datetime, timezone
from email.message import EmailMessage
from email.policy import SMTP
from typing import Any, Dict, Iterable, List, Literal, Mapping, Optional, Sequence

from fastmcp.exceptions import ToolError as ToolExecutionError
from googleapiclient.errors import HttpError

from auth.scopes import (
    GMAIL_FULL_SCOPE,
    GMAIL_SETTINGS_SHARING_SCOPE,
    has_required_scopes,
)
from core.utils import GOOGLE_API_WRITE_RETRIES, UserInputError
from gmail.gmail_helpers import GMAIL_BATCH_MODIFY_LIMIT, _http_error_status

logger = logging.getLogger(__name__)

# Label changes behind the convenience message/thread actions:
# action -> (addLabelIds, removeLabelIds)
LABEL_ACTIONS: Dict[str, tuple[List[str], List[str]]] = {
    "archive": ([], ["INBOX"]),
    "mark_read": ([], ["UNREAD"]),
    "mark_unread": (["UNREAD"], []),
    "star": (["STARRED"], []),
    "unstar": ([], ["STARRED"]),
    "mark_important": (["IMPORTANT"], []),
    "mark_not_important": ([], ["IMPORTANT"]),
}

MESSAGE_ACTIONS = (
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
)
THREAD_ACTIONS = ("list", "modify", "trash", "untrash", "archive", "delete")
DRAFT_ACTIONS = ("list", "get", "update", "send", "delete")
SETTINGS_ACTIONS = (
    "get",
    "update_vacation",
    "update_imap",
    "update_pop",
    "update_language",
)
SHARING_SETTINGS_ACTIONS = ("update_signature", "update_auto_forwarding")
HISTORY_TYPES = ("messageAdded", "messageDeleted", "labelAdded", "labelRemoved")
IMAP_EXPUNGE_BEHAVIORS = ("archive", "trash", "deleteForever")
POP_ACCESS_WINDOWS = ("disabled", "fromNowOn", "allMail")
MAIL_DISPOSITIONS = ("leaveInInbox", "archive", "trash", "markRead")

DRAFT_HEADERS = (
    "From",
    "To",
    "Cc",
    "Bcc",
    "Subject",
    "Date",
    "In-Reply-To",
    "References",
    "Message-ID",
)

# How much of a draft body / history page to render before truncating.
DRAFT_BODY_PREVIEW_LIMIT = 20000
HISTORY_DEFAULT_MAX_RESULTS = 100
LIST_DEFAULT_MAX_RESULTS = 20
LIST_MAX_RESULTS_CAP = 500

FULL_SCOPE_HINT = (
    f"Permanent deletion requires the full Gmail scope ({GMAIL_FULL_SCOPE}), "
    "which this server does not request by default. Nothing was deleted. Use "
    "the 'trash' action instead, or re-authenticate after adding that scope."
)
SHARING_SCOPE_HINT = (
    f"This setting requires the {GMAIL_SETTINGS_SHARING_SCOPE} scope, which "
    "this server does not request by default. Nothing was changed. "
    "Re-authenticate after adding that scope to the Gmail consent."
)


# --------------------------------------------------------------------------
# Generic helpers
# --------------------------------------------------------------------------


def resolve_id_list(
    single: Optional[str], many: Optional[Sequence[str]], label: str
) -> List[str]:
    """Merge a single ID and a list of IDs into one de-duplicated list.

    Raises ``UserInputError`` when neither is given or every entry is blank.
    """
    ids: List[str] = []
    for candidate in [single, *(many or [])]:
        if candidate is None:
            continue
        value = str(candidate).strip()
        if value and value not in ids:
            ids.append(value)
    if not ids:
        raise UserInputError(f"Provide {label}_id or a non-empty {label}_ids list.")
    return ids


def format_id_list(ids: Sequence[str], limit: int = 10) -> str:
    """Join IDs for a result line, truncating very long lists."""
    shown = ", ".join(ids[:limit])
    if len(ids) > limit:
        shown += f", ... (+{len(ids) - limit} more)"
    return shown


def chunked(items: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def credential_scopes(service: Any) -> Optional[set]:
    """Scopes on the credentials behind ``service``, or None when unknown."""
    credentials = getattr(getattr(service, "_http", None), "credentials", None)
    granted = getattr(credentials, "scopes", None)
    if isinstance(granted, (list, tuple, set, frozenset)):
        return set(granted)
    return None


def ensure_credential_scope(service: Any, scope: str, hint: str) -> None:
    """Refuse up front when the credentials are known to lack ``scope``.

    Checked in the tool body rather than the decorator so the other actions
    of the same tool keep working with the narrower scope the tool declares.
    """
    granted = credential_scopes(service)
    if granted is not None and not has_required_scopes(granted, [scope]):
        raise ToolExecutionError(hint)


async def execute_with_scope_hint(request: Any, hint: str) -> Any:
    """Execute a request, turning a 403 into a clear scope explanation."""
    try:
        return await asyncio.to_thread(request.execute)
    except HttpError as error:
        if _http_error_status(error) == 403:
            raise ToolExecutionError(f"Gmail refused the request (HTTP 403). {hint}")
        raise


def _epoch_ms_to_iso(value: Any) -> str:
    try:
        millis = int(value)
    except (TypeError, ValueError):
        return str(value)
    return datetime.fromtimestamp(millis / 1000, tz=timezone.utc).isoformat()


def parse_time_to_epoch_ms(value: str, field: str, end_of_day: bool = False) -> str:
    """Parse ``YYYY-MM-DD`` or an ISO 8601 timestamp into Gmail's epoch-ms string.

    A bare date is taken as midnight UTC (or 23:59:59.999 UTC with
    ``end_of_day``). Naive timestamps are treated as UTC.
    """
    raw = (value or "").strip()
    if not raw:
        raise UserInputError(f"{field} must not be empty.")
    try:
        if len(raw) == 10:
            parsed = datetime.strptime(raw, "%Y-%m-%d")
            if end_of_day:
                parsed = parsed.replace(
                    hour=23, minute=59, second=59, microsecond=999000
                )
        else:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise UserInputError(
            f"{field} must be YYYY-MM-DD or an ISO 8601 timestamp, got '{value}'."
        )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return str(int(parsed.timestamp() * 1000))


def _validate_max_results(max_results: int) -> int:
    if max_results is None or max_results < 1:
        raise UserInputError("max_results must be at least 1.")
    return min(int(max_results), LIST_MAX_RESULTS_CAP)


# --------------------------------------------------------------------------
# Messages
# --------------------------------------------------------------------------


async def modify_message_labels(
    service: Any,
    message_ids: Sequence[str],
    add_label_ids: Sequence[str],
    remove_label_ids: Sequence[str],
) -> None:
    """Apply a label change to one message (modify) or many (batchModify)."""
    body: Dict[str, Any] = {}
    if add_label_ids:
        body["addLabelIds"] = list(add_label_ids)
    if remove_label_ids:
        body["removeLabelIds"] = list(remove_label_ids)
    if not body:
        raise UserInputError("Nothing to change: no labels to add or remove.")

    if len(message_ids) == 1:
        await asyncio.to_thread(
            service.users()
            .messages()
            .modify(userId="me", id=message_ids[0], body=body)
            .execute,
            num_retries=GOOGLE_API_WRITE_RETRIES,
        )
        return

    for chunk in chunked(message_ids, GMAIL_BATCH_MODIFY_LIMIT):
        await asyncio.to_thread(
            service.users()
            .messages()
            .batchModify(userId="me", body={"ids": list(chunk), **body})
            .execute,
            num_retries=GOOGLE_API_WRITE_RETRIES,
        )


async def trash_messages(
    service: Any, message_ids: Sequence[str], untrash: bool = False
) -> None:
    """Move messages to or out of Trash via the dedicated endpoints."""
    messages = service.users().messages()
    for message_id in message_ids:
        request = (
            messages.untrash(userId="me", id=message_id)
            if untrash
            else messages.trash(userId="me", id=message_id)
        )
        await asyncio.to_thread(request.execute, num_retries=GOOGLE_API_WRITE_RETRIES)


async def delete_messages_permanently(service: Any, message_ids: Sequence[str]) -> None:
    """Permanently delete messages. Needs https://mail.google.com/."""
    ensure_credential_scope(service, GMAIL_FULL_SCOPE, FULL_SCOPE_HINT)
    messages = service.users().messages()
    if len(message_ids) == 1:
        await execute_with_scope_hint(
            messages.delete(userId="me", id=message_ids[0]), FULL_SCOPE_HINT
        )
        return
    for chunk in chunked(message_ids, GMAIL_BATCH_MODIFY_LIMIT):
        await execute_with_scope_hint(
            messages.batchDelete(userId="me", body={"ids": list(chunk)}),
            FULL_SCOPE_HINT,
        )


async def run_message_action(service: Any, action: str, message_ids: List[str]) -> str:
    """Dispatch a ``manage_gmail_message`` action and describe the outcome."""
    count = len(message_ids)
    noun = "message" if count == 1 else f"{count} messages"
    shown = format_id_list(message_ids)

    if action in LABEL_ACTIONS:
        add_ids, remove_ids = LABEL_ACTIONS[action]
        await modify_message_labels(service, message_ids, add_ids, remove_ids)
        changes = []
        if add_ids:
            changes.append(f"added {', '.join(add_ids)}")
        if remove_ids:
            changes.append(f"removed {', '.join(remove_ids)}")
        return f"Applied '{action}' to {noun} ({'; '.join(changes)}).\nIDs: {shown}"

    if action == "trash":
        await trash_messages(service, message_ids)
        return f"Moved {noun} to Trash.\nIDs: {shown}"

    if action == "untrash":
        await trash_messages(service, message_ids, untrash=True)
        return f"Restored {noun} from Trash.\nIDs: {shown}"

    if action == "delete":
        await delete_messages_permanently(service, message_ids)
        return f"Permanently deleted {noun}. This cannot be undone.\nIDs: {shown}"

    raise UserInputError(
        f"Invalid action '{action}'. Must be one of: {', '.join(MESSAGE_ACTIONS)}"
    )


# --------------------------------------------------------------------------
# Threads
# --------------------------------------------------------------------------


async def list_threads(
    service: Any,
    query: Optional[str],
    label_ids: Optional[Sequence[str]],
    max_results: int,
    page_token: Optional[str],
    include_spam_trash: bool,
) -> str:
    """List threads matching a query and/or labels, one line per thread."""
    params: Dict[str, Any] = {
        "userId": "me",
        "maxResults": _validate_max_results(max_results),
        "includeSpamTrash": bool(include_spam_trash),
    }
    if query:
        params["q"] = query
    if label_ids:
        params["labelIds"] = list(label_ids)
    if page_token:
        params["pageToken"] = page_token

    response = await asyncio.to_thread(service.users().threads().list(**params).execute)
    threads = response.get("threads") or []
    next_token = response.get("nextPageToken")

    lines = [
        f"Found {len(threads)} thread(s)"
        + (
            f" (estimated total: {response['resultSizeEstimate']})"
            if "resultSizeEstimate" in response
            else ""
        )
        + (f" for query: {query}" if query else "")
        + (f" with labels: {', '.join(label_ids)}" if label_ids else "")
    ]
    for thread in threads:
        snippet = " ".join((thread.get("snippet") or "").split())
        if len(snippet) > 140:
            snippet = snippet[:137] + "..."
        lines.append(
            f"- Thread ID: {thread.get('id')} | History ID: {thread.get('historyId', '')}"
            + (f" | {snippet}" if snippet else "")
        )
    if next_token:
        lines.append(f"\nMore results available. Next page token: {next_token}")
    elif not threads:
        lines.append("No threads matched.")
    return "\n".join(lines)


async def modify_threads(
    service: Any,
    thread_ids: Sequence[str],
    add_label_ids: Sequence[str],
    remove_label_ids: Sequence[str],
) -> None:
    """Apply a label change to each thread (no batch endpoint exists)."""
    body: Dict[str, Any] = {}
    if add_label_ids:
        body["addLabelIds"] = list(add_label_ids)
    if remove_label_ids:
        body["removeLabelIds"] = list(remove_label_ids)
    if not body:
        raise UserInputError(
            "At least one of add_label_ids or remove_label_ids must be provided."
        )
    for thread_id in thread_ids:
        await asyncio.to_thread(
            service.users()
            .threads()
            .modify(userId="me", id=thread_id, body=body)
            .execute,
            num_retries=GOOGLE_API_WRITE_RETRIES,
        )


async def trash_threads(
    service: Any, thread_ids: Sequence[str], untrash: bool = False
) -> None:
    threads = service.users().threads()
    for thread_id in thread_ids:
        request = (
            threads.untrash(userId="me", id=thread_id)
            if untrash
            else threads.trash(userId="me", id=thread_id)
        )
        await asyncio.to_thread(request.execute, num_retries=GOOGLE_API_WRITE_RETRIES)


async def delete_threads_permanently(service: Any, thread_ids: Sequence[str]) -> None:
    """Permanently delete threads. Needs https://mail.google.com/."""
    ensure_credential_scope(service, GMAIL_FULL_SCOPE, FULL_SCOPE_HINT)
    threads = service.users().threads()
    for thread_id in thread_ids:
        await execute_with_scope_hint(
            threads.delete(userId="me", id=thread_id), FULL_SCOPE_HINT
        )


async def run_thread_action(
    service: Any,
    action: str,
    thread_ids: List[str],
    add_label_ids: Optional[Sequence[str]] = None,
    remove_label_ids: Optional[Sequence[str]] = None,
) -> str:
    """Dispatch a non-list ``manage_gmail_thread`` action."""
    count = len(thread_ids)
    noun = "thread" if count == 1 else f"{count} threads"
    shown = format_id_list(thread_ids)

    if action == "modify":
        await modify_threads(
            service, thread_ids, add_label_ids or [], remove_label_ids or []
        )
        changes = []
        if add_label_ids:
            changes.append(f"Added labels: {', '.join(add_label_ids)}")
        if remove_label_ids:
            changes.append(f"Removed labels: {', '.join(remove_label_ids)}")
        return f"Updated labels on {noun}.\n{'; '.join(changes)}\nIDs: {shown}"

    if action == "archive":
        await modify_threads(service, thread_ids, [], ["INBOX"])
        return f"Archived {noun} (removed INBOX).\nIDs: {shown}"

    if action == "trash":
        await trash_threads(service, thread_ids)
        return f"Moved {noun} to Trash.\nIDs: {shown}"

    if action == "untrash":
        await trash_threads(service, thread_ids, untrash=True)
        return f"Restored {noun} from Trash.\nIDs: {shown}"

    if action == "delete":
        await delete_threads_permanently(service, thread_ids)
        return f"Permanently deleted {noun}. This cannot be undone.\nIDs: {shown}"

    raise UserInputError(
        f"Invalid action '{action}'. Must be one of: {', '.join(THREAD_ACTIONS)}"
    )


# --------------------------------------------------------------------------
# Drafts
# --------------------------------------------------------------------------


def extract_headers(payload: Mapping[str, Any], names: Sequence[str]) -> Dict[str, str]:
    """Pull the named headers out of a message payload (case-insensitive)."""
    wanted = {name.lower(): name for name in names}
    found: Dict[str, str] = {}
    for header in payload.get("headers") or []:
        key = str(header.get("name", "")).lower()
        if key in wanted:
            found[wanted[key]] = header.get("value", "")
    return found


def _decode_body_data(data: str) -> str:
    try:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode(
            "utf-8", errors="ignore"
        )
    except (ValueError, TypeError) as exc:  # pragma: no cover - defensive
        logger.warning("Failed to decode body part: %s", exc)
        return ""


def extract_bodies(payload: Mapping[str, Any]) -> Dict[str, str]:
    """Return the first text/plain and text/html bodies in a payload tree."""
    text_body = ""
    html_body = ""
    queue: List[Mapping[str, Any]] = [payload]
    while queue:
        part = queue.pop(0)
        mime_type = part.get("mimeType", "")
        data = (part.get("body") or {}).get("data")
        if data:
            if mime_type == "text/plain" and not text_body:
                text_body = _decode_body_data(data)
            elif mime_type == "text/html" and not html_body:
                html_body = _decode_body_data(data)
        queue.extend(part.get("parts") or [])
    return {"text": text_body, "html": html_body}


def extract_attachment_names(payload: Mapping[str, Any]) -> List[str]:
    names: List[str] = []
    queue: List[Mapping[str, Any]] = [payload]
    while queue:
        part = queue.pop(0)
        filename = part.get("filename")
        if filename and (part.get("body") or {}).get("attachmentId"):
            names.append(filename)
        queue.extend(part.get("parts") or [])
    return names


async def list_drafts(
    service: Any, query: Optional[str], max_results: int, page_token: Optional[str]
) -> str:
    """List drafts with subject/recipient metadata, one block per draft."""
    params: Dict[str, Any] = {
        "userId": "me",
        "maxResults": _validate_max_results(max_results),
    }
    if query:
        params["q"] = query
    if page_token:
        params["pageToken"] = page_token

    response = await asyncio.to_thread(service.users().drafts().list(**params).execute)
    drafts = response.get("drafts") or []
    next_token = response.get("nextPageToken")

    lines = [
        f"Found {len(drafts)} draft(s)" + (f" for query: {query}" if query else "")
    ]
    for draft in drafts:
        draft_id = draft.get("id")
        detail = await asyncio.to_thread(
            service.users()
            .drafts()
            .get(userId="me", id=draft_id, format="metadata")
            .execute
        )
        message = detail.get("message") or {}
        headers = extract_headers(message.get("payload") or {}, DRAFT_HEADERS)
        lines.append(
            f"- Draft ID: {draft_id} | Message ID: {message.get('id', '')} | "
            f"Thread ID: {message.get('threadId', '')}\n"
            f"  Subject: {headers.get('Subject') or '(no subject)'} | "
            f"To: {headers.get('To') or '(none)'} | Date: {headers.get('Date', '')}"
        )
    if next_token:
        lines.append(f"\nMore results available. Next page token: {next_token}")
    elif not drafts:
        lines.append("No drafts found.")
    return "\n".join(lines)


async def fetch_draft(service: Any, draft_id: str) -> Dict[str, Any]:
    return await asyncio.to_thread(
        service.users().drafts().get(userId="me", id=draft_id, format="full").execute
    )


def format_draft(draft: Mapping[str, Any]) -> str:
    """Render a full draft: headers, body and attachment names."""
    message = draft.get("message") or {}
    payload = message.get("payload") or {}
    headers = extract_headers(payload, DRAFT_HEADERS)
    bodies = extract_bodies(payload)
    body = bodies["text"] or bodies["html"] or "[No body]"
    if len(body) > DRAFT_BODY_PREVIEW_LIMIT:
        body = body[:DRAFT_BODY_PREVIEW_LIMIT] + "\n\n[Content truncated...]"
    body_kind = "text" if bodies["text"] else ("html" if bodies["html"] else "none")

    lines = [
        f"Draft ID: {draft.get('id')}",
        f"Message ID: {message.get('id', '')}",
        f"Thread ID: {message.get('threadId', '')}",
        f"From: {headers.get('From', '')}",
        f"To: {headers.get('To', '')}",
        f"Cc: {headers.get('Cc', '')}",
        f"Bcc: {headers.get('Bcc', '')}",
        f"Subject: {headers.get('Subject', '')}",
    ]
    if headers.get("In-Reply-To"):
        lines.append(f"In-Reply-To: {headers['In-Reply-To']}")
    attachments = extract_attachment_names(payload)
    if attachments:
        lines.append(f"Attachments: {', '.join(attachments)}")
    lines.append(f"\n--- BODY ({body_kind}) ---\n{body}")
    return "\n".join(lines)


def build_draft_mime(
    subject: str,
    body: str,
    body_format: Literal["plain", "html"],
    to: Optional[str],
    cc: Optional[str],
    bcc: Optional[str],
    from_header: Optional[str],
    in_reply_to: Optional[str],
    references: Optional[str],
) -> str:
    """Build a urlsafe-base64 RFC 822 message for drafts.update."""
    message = EmailMessage(policy=SMTP)
    message["Subject"] = subject or ""
    if to:
        message["To"] = to
    if cc:
        message["Cc"] = cc
    if bcc:
        message["Bcc"] = bcc
    if from_header:
        message["From"] = from_header
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
    if references:
        message["References"] = references
    message.set_content(
        body or "", subtype="html" if body_format == "html" else "plain"
    )
    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")


async def update_draft(
    service: Any,
    draft_id: str,
    subject: Optional[str],
    body: Optional[str],
    body_format: Optional[Literal["plain", "html"]],
    to: Optional[str],
    cc: Optional[str],
    bcc: Optional[str],
) -> str:
    """Replace a draft's content, keeping its thread and any field not given.

    Gmail's drafts.update replaces the whole MIME message, so fields the caller
    leaves out are copied from the current draft. Attachments on the current
    draft are not carried over.
    """
    if all(value is None for value in (subject, body, to, cc, bcc)):
        raise UserInputError(
            "Provide at least one of subject, body, to, cc or bcc to update."
        )

    current = await fetch_draft(service, draft_id)
    message = current.get("message") or {}
    payload = message.get("payload") or {}
    headers = extract_headers(payload, DRAFT_HEADERS)
    bodies = extract_bodies(payload)

    if body is None:
        if bodies["text"] or not bodies["html"]:
            new_body, new_format = bodies["text"], "plain"
        else:
            new_body, new_format = bodies["html"], "html"
    else:
        new_body, new_format = body, body_format or "plain"

    raw = build_draft_mime(
        subject=headers.get("Subject", "") if subject is None else subject,
        body=new_body,
        body_format=new_format,
        to=headers.get("To") if to is None else to,
        cc=headers.get("Cc") if cc is None else cc,
        bcc=headers.get("Bcc") if bcc is None else bcc,
        from_header=headers.get("From"),
        in_reply_to=headers.get("In-Reply-To"),
        references=headers.get("References"),
    )
    draft_body: Dict[str, Any] = {"id": draft_id, "message": {"raw": raw}}
    if message.get("threadId"):
        draft_body["message"]["threadId"] = message["threadId"]

    updated = await asyncio.to_thread(
        service.users()
        .drafts()
        .update(userId="me", id=draft_id, body=draft_body)
        .execute,
        num_retries=GOOGLE_API_WRITE_RETRIES,
    )
    changed = [
        name
        for name, value in (
            ("subject", subject),
            ("body", body),
            ("to", to),
            ("cc", cc),
            ("bcc", bcc),
        )
        if value is not None
    ]
    note = ""
    if extract_attachment_names(payload):
        note = (
            "\nNote: attachments on the previous draft version were not carried over."
        )
    return (
        f"Draft updated. Draft ID: {updated.get('id', draft_id)}\n"
        f"Changed: {', '.join(changed)}{note}"
    )


async def send_draft(service: Any, draft_id: str) -> str:
    sent = await asyncio.to_thread(
        service.users().drafts().send(userId="me", body={"id": draft_id}).execute,
        num_retries=GOOGLE_API_WRITE_RETRIES,
    )
    return (
        f"Draft {draft_id} sent.\nMessage ID: {sent.get('id', '')}\n"
        f"Thread ID: {sent.get('threadId', '')}"
    )


async def delete_draft(service: Any, draft_id: str) -> str:
    await asyncio.to_thread(
        service.users().drafts().delete(userId="me", id=draft_id).execute,
        num_retries=GOOGLE_API_WRITE_RETRIES,
    )
    return f"Draft {draft_id} deleted."


# --------------------------------------------------------------------------
# Profile
# --------------------------------------------------------------------------


def format_profile(profile: Mapping[str, Any]) -> str:
    return (
        f"Email: {profile.get('emailAddress', '')}\n"
        f"Messages total: {profile.get('messagesTotal', 0)}\n"
        f"Threads total: {profile.get('threadsTotal', 0)}\n"
        f"History ID: {profile.get('historyId', '')}"
    )


# --------------------------------------------------------------------------
# Settings (gmail.settings.basic)
# --------------------------------------------------------------------------


async def _settings_call(request_factory) -> Any:
    """Run one settings read, returning an error string instead of raising."""
    try:
        return await asyncio.to_thread(request_factory().execute)
    except HttpError as error:
        status = _http_error_status(error)
        return {"_error": f"unavailable (HTTP {status})"}


def _onoff(value: Any) -> str:
    return "enabled" if value else "disabled"


def format_settings_overview(
    vacation: Mapping[str, Any],
    imap: Mapping[str, Any],
    pop: Mapping[str, Any],
    language: Mapping[str, Any],
    auto_forwarding: Mapping[str, Any],
    send_as: Mapping[str, Any],
    forwarding: Mapping[str, Any],
) -> str:
    lines = ["Gmail settings"]

    lines.append("\nVacation responder:")
    if "_error" in vacation:
        lines.append(f"  {vacation['_error']}")
    else:
        lines.append(f"  Status: {_onoff(vacation.get('enableAutoReply'))}")
        lines.append(f"  Subject: {vacation.get('responseSubject') or '(none)'}")
        body_kind = (
            "html"
            if vacation.get("responseBodyHtml")
            else ("plain" if vacation.get("responseBodyPlainText") else "none")
        )
        lines.append(f"  Body: {body_kind}")
        if vacation.get("startTime"):
            lines.append(f"  Start: {_epoch_ms_to_iso(vacation['startTime'])}")
        if vacation.get("endTime"):
            lines.append(f"  End: {_epoch_ms_to_iso(vacation['endTime'])}")
        lines.append(
            f"  Restrict to contacts: {bool(vacation.get('restrictToContacts'))} | "
            f"Restrict to domain: {bool(vacation.get('restrictToDomain'))}"
        )

    lines.append("\nIMAP:")
    if "_error" in imap:
        lines.append(f"  {imap['_error']}")
    else:
        lines.append(
            f"  Status: {_onoff(imap.get('enabled'))} | Auto-expunge: "
            f"{bool(imap.get('autoExpunge'))} | Expunge behavior: "
            f"{imap.get('expungeBehavior', 'expungeBehaviorUnspecified')} | "
            f"Max folder size: {imap.get('maxFolderSize', 0)}"
        )

    lines.append("\nPOP:")
    if "_error" in pop:
        lines.append(f"  {pop['_error']}")
    else:
        lines.append(
            f"  Access window: {pop.get('accessWindow', 'accessWindowUnspecified')} | "
            f"Disposition: {pop.get('disposition', 'dispositionUnspecified')}"
        )

    lines.append("\nLanguage:")
    if "_error" in language:
        lines.append(f"  {language['_error']}")
    else:
        lines.append(f"  Display language: {language.get('displayLanguage', '')}")

    lines.append("\nAuto-forwarding:")
    if "_error" in auto_forwarding:
        lines.append(f"  {auto_forwarding['_error']}")
    else:
        lines.append(
            f"  Status: {_onoff(auto_forwarding.get('enabled'))} | To: "
            f"{auto_forwarding.get('emailAddress') or '(none)'} | Disposition: "
            f"{auto_forwarding.get('disposition', 'dispositionUnspecified')}"
        )

    lines.append("\nSend-as addresses:")
    if "_error" in send_as:
        lines.append(f"  {send_as['_error']}")
    else:
        entries = send_as.get("sendAs") or []
        if not entries:
            lines.append("  (none)")
        for entry in entries:
            flags = []
            if entry.get("isPrimary"):
                flags.append("primary")
            if entry.get("isDefault"):
                flags.append("default")
            if entry.get("treatAsAlias"):
                flags.append("alias")
            name = entry.get("displayName")
            lines.append(
                f"  - {entry.get('sendAsEmail', '')}"
                + (f" ({name})" if name else "")
                + (f" [{', '.join(flags)}]" if flags else "")
                + f" | signature: {'yes' if entry.get('signature') else 'no'}"
                + (
                    f" | verification: {entry['verificationStatus']}"
                    if entry.get("verificationStatus")
                    else ""
                )
            )

    lines.append("\nForwarding addresses:")
    if "_error" in forwarding:
        lines.append(f"  {forwarding['_error']}")
    else:
        entries = forwarding.get("forwardingAddresses") or []
        if not entries:
            lines.append("  (none)")
        for entry in entries:
            lines.append(
                f"  - {entry.get('forwardingEmail', '')} "
                f"({entry.get('verificationStatus', 'verificationStatusUnspecified')})"
            )
    return "\n".join(lines)


async def get_settings_overview(service: Any) -> str:
    """Read every basic-scope setting; a failing read is reported, not fatal."""
    settings = service.users().settings()
    vacation = await _settings_call(lambda: settings.getVacation(userId="me"))
    imap = await _settings_call(lambda: settings.getImap(userId="me"))
    pop = await _settings_call(lambda: settings.getPop(userId="me"))
    language = await _settings_call(lambda: settings.getLanguage(userId="me"))
    auto_forwarding = await _settings_call(
        lambda: settings.getAutoForwarding(userId="me")
    )
    send_as = await _settings_call(lambda: settings.sendAs().list(userId="me"))
    forwarding = await _settings_call(
        lambda: settings.forwardingAddresses().list(userId="me")
    )
    return format_settings_overview(
        vacation, imap, pop, language, auto_forwarding, send_as, forwarding
    )


async def update_vacation(
    service: Any,
    enable: Optional[bool],
    subject: Optional[str],
    body: Optional[str],
    body_format: Literal["plain", "html"],
    start_date: Optional[str],
    end_date: Optional[str],
    restrict_to_contacts: Optional[bool],
    restrict_to_domain: Optional[bool],
) -> str:
    """Update the vacation responder, carrying over fields not provided."""
    if all(
        value is None
        for value in (
            enable,
            subject,
            body,
            start_date,
            end_date,
            restrict_to_contacts,
            restrict_to_domain,
        )
    ):
        raise UserInputError("Provide at least one vacation field to update.")

    settings = service.users().settings()
    current = await asyncio.to_thread(settings.getVacation(userId="me").execute)
    new_settings: Dict[str, Any] = dict(current)

    if enable is not None:
        new_settings["enableAutoReply"] = bool(enable)
    if subject is not None:
        new_settings["responseSubject"] = subject
    if body is not None:
        new_settings.pop("responseBodyHtml", None)
        new_settings.pop("responseBodyPlainText", None)
        key = "responseBodyHtml" if body_format == "html" else "responseBodyPlainText"
        new_settings[key] = body
    if start_date is not None:
        if start_date.strip():
            new_settings["startTime"] = parse_time_to_epoch_ms(start_date, "start_date")
        else:
            new_settings.pop("startTime", None)
    if end_date is not None:
        if end_date.strip():
            new_settings["endTime"] = parse_time_to_epoch_ms(
                end_date, "end_date", end_of_day=True
            )
        else:
            new_settings.pop("endTime", None)
    if restrict_to_contacts is not None:
        new_settings["restrictToContacts"] = bool(restrict_to_contacts)
    if restrict_to_domain is not None:
        new_settings["restrictToDomain"] = bool(restrict_to_domain)

    if (
        new_settings.get("startTime")
        and new_settings.get("endTime")
        and int(new_settings["startTime"]) > int(new_settings["endTime"])
    ):
        raise UserInputError("start_date must not be after end_date.")

    updated = await asyncio.to_thread(
        settings.updateVacation(userId="me", body=new_settings).execute,
        num_retries=GOOGLE_API_WRITE_RETRIES,
    )
    lines = [
        "Vacation responder updated.",
        f"Status: {_onoff(updated.get('enableAutoReply'))}",
        f"Subject: {updated.get('responseSubject') or '(none)'}",
    ]
    if updated.get("startTime"):
        lines.append(f"Start: {_epoch_ms_to_iso(updated['startTime'])}")
    if updated.get("endTime"):
        lines.append(f"End: {_epoch_ms_to_iso(updated['endTime'])}")
    lines.append(
        f"Restrict to contacts: {bool(updated.get('restrictToContacts'))} | "
        f"Restrict to domain: {bool(updated.get('restrictToDomain'))}"
    )
    return "\n".join(lines)


async def update_imap(
    service: Any,
    enabled: Optional[bool],
    auto_expunge: Optional[bool],
    expunge_behavior: Optional[str],
    max_folder_size: Optional[int],
) -> str:
    if all(
        v is None for v in (enabled, auto_expunge, expunge_behavior, max_folder_size)
    ):
        raise UserInputError("Provide at least one IMAP field to update.")
    if expunge_behavior is not None and expunge_behavior not in IMAP_EXPUNGE_BEHAVIORS:
        raise UserInputError(
            f"expunge_behavior must be one of: {', '.join(IMAP_EXPUNGE_BEHAVIORS)}"
        )
    if max_folder_size is not None and max_folder_size < 0:
        raise UserInputError(
            "max_folder_size must be 0 (no limit) or a positive number."
        )

    settings = service.users().settings()
    current = await asyncio.to_thread(settings.getImap(userId="me").execute)
    new_settings: Dict[str, Any] = dict(current)
    if enabled is not None:
        new_settings["enabled"] = bool(enabled)
    if auto_expunge is not None:
        new_settings["autoExpunge"] = bool(auto_expunge)
    if expunge_behavior is not None:
        new_settings["expungeBehavior"] = expunge_behavior
    if max_folder_size is not None:
        new_settings["maxFolderSize"] = int(max_folder_size)

    updated = await asyncio.to_thread(
        settings.updateImap(userId="me", body=new_settings).execute,
        num_retries=GOOGLE_API_WRITE_RETRIES,
    )
    return (
        "IMAP settings updated.\n"
        f"Status: {_onoff(updated.get('enabled'))} | Auto-expunge: "
        f"{bool(updated.get('autoExpunge'))} | Expunge behavior: "
        f"{updated.get('expungeBehavior', 'expungeBehaviorUnspecified')} | "
        f"Max folder size: {updated.get('maxFolderSize', 0)}"
    )


async def update_pop(
    service: Any, access_window: Optional[str], disposition: Optional[str]
) -> str:
    if access_window is None and disposition is None:
        raise UserInputError("Provide pop_access_window and/or pop_disposition.")
    if access_window is not None and access_window not in POP_ACCESS_WINDOWS:
        raise UserInputError(
            f"pop_access_window must be one of: {', '.join(POP_ACCESS_WINDOWS)}"
        )
    if disposition is not None and disposition not in MAIL_DISPOSITIONS:
        raise UserInputError(
            f"pop_disposition must be one of: {', '.join(MAIL_DISPOSITIONS)}"
        )

    settings = service.users().settings()
    current = await asyncio.to_thread(settings.getPop(userId="me").execute)
    new_settings: Dict[str, Any] = dict(current)
    if access_window is not None:
        new_settings["accessWindow"] = access_window
    if disposition is not None:
        new_settings["disposition"] = disposition

    updated = await asyncio.to_thread(
        settings.updatePop(userId="me", body=new_settings).execute,
        num_retries=GOOGLE_API_WRITE_RETRIES,
    )
    return (
        "POP settings updated.\n"
        f"Access window: {updated.get('accessWindow', 'accessWindowUnspecified')} | "
        f"Disposition: {updated.get('disposition', 'dispositionUnspecified')}"
    )


async def update_language(service: Any, display_language: Optional[str]) -> str:
    if not display_language or not display_language.strip():
        raise UserInputError(
            "display_language is required, e.g. 'en', 'en-GB', 'fr', 'ja'."
        )
    updated = await asyncio.to_thread(
        service.users()
        .settings()
        .updateLanguage(userId="me", body={"displayLanguage": display_language.strip()})
        .execute,
        num_retries=GOOGLE_API_WRITE_RETRIES,
    )
    return f"Display language updated to: {updated.get('displayLanguage', display_language)}"


# --------------------------------------------------------------------------
# Sharing settings (gmail.settings.sharing)
# --------------------------------------------------------------------------


async def update_signature(
    service: Any, send_as_email: str, signature: Optional[str]
) -> str:
    """Set (or clear, with "") the signature on a send-as address."""
    if signature is None:
        raise UserInputError(
            "signature is required. Pass an empty string to clear the signature."
        )
    if not send_as_email or not send_as_email.strip():
        raise UserInputError("send_as_email is required.")
    ensure_credential_scope(service, GMAIL_SETTINGS_SHARING_SCOPE, SHARING_SCOPE_HINT)
    updated = await execute_with_scope_hint(
        service.users()
        .settings()
        .sendAs()
        .patch(
            userId="me",
            sendAsEmail=send_as_email.strip(),
            body={"signature": signature},
        ),
        SHARING_SCOPE_HINT,
    )
    state = "cleared" if not signature else f"set ({len(signature)} characters)"
    return f"Signature {state} for {updated.get('sendAsEmail', send_as_email)}."


async def update_auto_forwarding(
    service: Any,
    enabled: Optional[bool],
    forward_to: Optional[str],
    disposition: Optional[str],
) -> str:
    """Enable or disable auto-forwarding to a verified forwarding address."""
    if enabled is None:
        raise UserInputError("enable is required (true or false).")
    if disposition is not None and disposition not in MAIL_DISPOSITIONS:
        raise UserInputError(
            f"forward_disposition must be one of: {', '.join(MAIL_DISPOSITIONS)}"
        )
    body: Dict[str, Any] = {"enabled": bool(enabled)}
    if enabled:
        if not forward_to or not forward_to.strip():
            raise UserInputError(
                "forward_to is required when enabling auto-forwarding and must be "
                "a verified forwarding address (see manage_gmail_settings get)."
            )
        body["emailAddress"] = forward_to.strip()
        body["disposition"] = disposition or "leaveInInbox"
    ensure_credential_scope(service, GMAIL_SETTINGS_SHARING_SCOPE, SHARING_SCOPE_HINT)
    updated = await execute_with_scope_hint(
        service.users().settings().updateAutoForwarding(userId="me", body=body),
        SHARING_SCOPE_HINT,
    )
    return (
        "Auto-forwarding updated.\n"
        f"Status: {_onoff(updated.get('enabled'))} | To: "
        f"{updated.get('emailAddress') or '(none)'} | Disposition: "
        f"{updated.get('disposition', 'dispositionUnspecified')}"
    )


# --------------------------------------------------------------------------
# History
# --------------------------------------------------------------------------


def _label_list(labels: Any) -> str:
    return ", ".join(labels) if labels else "(none)"


def format_history(response: Mapping[str, Any], start_history_id: str) -> str:
    """Summarize a history.list page by change type."""
    records = response.get("history") or []
    added: List[str] = []
    deleted: List[str] = []
    labels_added: List[str] = []
    labels_removed: List[str] = []

    for record in records:
        record_id = record.get("id", "")
        for item in record.get("messagesAdded") or []:
            message = item.get("message") or {}
            added.append(
                f"  - [{record_id}] message {message.get('id', '')} "
                f"(thread {message.get('threadId', '')}) labels: "
                f"{_label_list(message.get('labelIds'))}"
            )
        for item in record.get("messagesDeleted") or []:
            message = item.get("message") or {}
            deleted.append(
                f"  - [{record_id}] message {message.get('id', '')} "
                f"(thread {message.get('threadId', '')})"
            )
        for item in record.get("labelsAdded") or []:
            message = item.get("message") or {}
            labels_added.append(
                f"  - [{record_id}] message {message.get('id', '')} "
                f"+{_label_list(item.get('labelIds'))}"
            )
        for item in record.get("labelsRemoved") or []:
            message = item.get("message") or {}
            labels_removed.append(
                f"  - [{record_id}] message {message.get('id', '')} "
                f"-{_label_list(item.get('labelIds'))}"
            )

    lines = [
        f"History since {start_history_id}: {len(records)} record(s)",
        f"Messages added: {len(added)} | Messages deleted: {len(deleted)} | "
        f"Labels added: {len(labels_added)} | Labels removed: {len(labels_removed)}",
    ]
    if response.get("historyId"):
        lines.append(f"Current history ID: {response['historyId']}")
    for title, items in (
        ("Messages added", added),
        ("Messages deleted", deleted),
        ("Labels added", labels_added),
        ("Labels removed", labels_removed),
    ):
        if items:
            lines.append(f"\n{title}:")
            lines.extend(items)
    if response.get("nextPageToken"):
        lines.append(
            f"\nMore results available. Next page token: {response['nextPageToken']}"
        )
    elif not records:
        lines.append("No changes recorded since that history ID.")
    return "\n".join(lines)


async def list_history(
    service: Any,
    start_history_id: str,
    history_types: Optional[Sequence[str]],
    label_id: Optional[str],
    max_results: int,
    page_token: Optional[str],
) -> str:
    """Call history.list and summarize the page."""
    start = (start_history_id or "").strip()
    if not start.isdigit():
        raise UserInputError(
            "start_history_id must be a numeric history ID (see get_gmail_profile)."
        )
    if history_types:
        invalid = [t for t in history_types if t not in HISTORY_TYPES]
        if invalid:
            raise UserInputError(
                f"Invalid history_types {invalid}. Allowed: {', '.join(HISTORY_TYPES)}"
            )

    params: Dict[str, Any] = {
        "userId": "me",
        "startHistoryId": start,
        "maxResults": _validate_max_results(max_results),
    }
    if history_types:
        params["historyTypes"] = list(history_types)
    if label_id:
        params["labelId"] = label_id
    if page_token:
        params["pageToken"] = page_token

    try:
        response = await asyncio.to_thread(
            service.users().history().list(**params).execute
        )
    except HttpError as error:
        if _http_error_status(error) == 404:
            raise UserInputError(
                f"start_history_id {start} is too old or unknown; Gmail keeps "
                "history for a limited window. Get a fresh history ID from "
                "get_gmail_profile and re-sync from there."
            )
        raise
    return format_history(response, start)
