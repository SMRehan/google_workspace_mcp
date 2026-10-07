"""
Google Keep helpers

Business logic for the Keep tools: request execution with Workspace-only
error hints, note rendering (text and checklist bodies), request body
construction, attachment download, and permission formatting.

The Google Keep API is available to Google Workspace accounts only; consumer
Gmail accounts receive a 403 from every endpoint.
"""

import asyncio
import base64
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from fastmcp.exceptions import ToolError
from googleapiclient.errors import HttpError

from core.file_limits import FileTooLargeError, download_media_bytes
from core.utils import UserInputError

logger = logging.getLogger(__name__)

KEEP_WORKSPACE_ONLY_HINT = (
    "The Google Keep API is available to Google Workspace accounts only; it does "
    "not work for consumer @gmail.com accounts. Make sure the Keep API is enabled "
    "in the Google Cloud project and that the account belongs to a Workspace "
    "organization whose admin has allowed Keep API access."
)

LIST_NOTES_PAGE_SIZE_DEFAULT = 20
LIST_NOTES_PAGE_SIZE_MAX = 100

_PERMISSION_ROLE = "WRITER"
_NOTE_NAME_RE = re.compile(r"^notes/[^/]+$")
_ATTACHMENT_NAME_RE = re.compile(r"^notes/[^/]+/attachments/[^/]+$")
_PERMISSION_NAME_RE = re.compile(r"^notes/[^/]+/permissions/[^/]+$")
_UNAVAILABLE_403_RE = re.compile(
    r"not (?:available|enabled|supported)|accessNotConfigured|SERVICE_DISABLED"
    r"|PERMISSION_DENIED|consumer",
    re.IGNORECASE,
)
_TEXT_LIKE_MIME_TYPES = {"application/json", "application/xml"}


# --- request execution ---------------------------------------------------------


def is_keep_unavailable_error(error: HttpError) -> bool:
    """Whether a 403 looks like 'Keep API not available for this account'."""
    status = getattr(getattr(error, "resp", None), "status", None)
    if status != 403:
        return False
    return bool(_UNAVAILABLE_403_RE.search(str(error)))


async def execute_keep_request(request: Any) -> Any:
    """Run a googleapiclient request off the event loop.

    A 403 that reads like the API being unavailable is re-raised as a
    ``ToolError`` carrying the Workspace-only hint, which ``handle_http_errors``
    passes through untouched. Every other ``HttpError`` propagates unchanged so
    the standard decorator messaging still applies.
    """
    try:
        return await asyncio.to_thread(request.execute)
    except HttpError as error:
        if is_keep_unavailable_error(error):
            raise ToolError(f"{error}. {KEEP_WORKSPACE_ONLY_HINT}") from error
        raise


# --- resource names -------------------------------------------------------------


def normalize_note_name(note_id: str) -> str:
    """Accept ``notes/<id>`` or a bare ``<id>`` and return ``notes/<id>``."""
    if not isinstance(note_id, str) or not note_id.strip():
        raise UserInputError("'note_id' is required (e.g. 'notes/abc123' or 'abc123').")
    name = note_id.strip()
    if "/" not in name:
        name = f"notes/{name}"
    if not _NOTE_NAME_RE.match(name):
        raise UserInputError(
            f"Invalid note_id '{note_id}'. Expected 'notes/<id>' or a bare '<id>'."
        )
    return name


def normalize_attachment_name(note_name: str, attachment: str) -> str:
    """Resolve an attachment reference to ``notes/<n>/attachments/<a>``.

    ``attachment`` may be a full resource name, an ``attachments/<a>`` suffix,
    or a bare attachment id; the latter two are scoped to ``note_name``.
    """
    if not isinstance(attachment, str) or not attachment.strip():
        raise UserInputError("'download_attachment' must be an attachment name.")
    ref = attachment.strip()
    if ref.startswith("notes/"):
        name = ref
    elif ref.startswith("attachments/"):
        name = f"{note_name}/{ref}"
    else:
        name = f"{note_name}/attachments/{ref}"
    if not _ATTACHMENT_NAME_RE.match(name):
        raise UserInputError(
            f"Invalid attachment name '{attachment}'. Expected "
            "'notes/<note>/attachments/<attachment>' or a bare attachment id."
        )
    if not name.startswith(f"{note_name}/"):
        raise UserInputError(
            f"Attachment '{attachment}' does not belong to note '{note_name}'."
        )
    return name


def normalize_permission_names(note_name: str, names: List[str]) -> List[str]:
    """Resolve permission references to ``notes/<n>/permissions/<p>`` names."""
    resolved: List[str] = []
    for raw in names:
        if not isinstance(raw, str) or not raw.strip():
            raise UserInputError("Each permission name must be a non-empty string.")
        ref = raw.strip()
        if ref.startswith("notes/"):
            name = ref
        elif ref.startswith("permissions/"):
            name = f"{note_name}/{ref}"
        else:
            name = f"{note_name}/permissions/{ref}"
        if not _PERMISSION_NAME_RE.match(name) or not name.startswith(f"{note_name}/"):
            raise UserInputError(
                f"Invalid permission name '{raw}' for note '{note_name}'. Expected "
                "'notes/<note>/permissions/<permission>' or a bare permission id."
            )
        resolved.append(name)
    return resolved


# --- note inspection ------------------------------------------------------------


def note_kind(note: Dict[str, Any]) -> str:
    """Classify a note body as ``text``, ``list`` or ``empty``."""
    body = note.get("body") or {}
    if "list" in body:
        return "list"
    if "text" in body:
        return "text"
    return "empty"


def count_list_items(items: Optional[List[Dict[str, Any]]]) -> Tuple[int, int]:
    """Return ``(total, checked)`` across a checklist, including nested items."""
    total = 0
    checked = 0
    for item in items or []:
        total += 1
        if item.get("checked"):
            checked += 1
        sub_total, sub_checked = count_list_items(item.get("childListItems"))
        total += sub_total
        checked += sub_checked
    return total, checked


def _note_text(note: Dict[str, Any]) -> str:
    return ((note.get("body") or {}).get("text") or {}).get("text", "") or ""


def _note_list_items(note: Dict[str, Any]) -> List[Dict[str, Any]]:
    return ((note.get("body") or {}).get("list") or {}).get("listItems") or []


# --- rendering ------------------------------------------------------------------


def format_note_summary(note: Dict[str, Any]) -> str:
    """One compact entry for the list view."""
    title = note.get("title") or "(untitled)"
    kind = note_kind(note)
    if kind == "list":
        total, checked = count_list_items(_note_list_items(note))
        kind_part = f"list ({checked}/{total} checked)"
    elif kind == "text":
        kind_part = f"text ({len(_note_text(note))} chars)"
    else:
        kind_part = "empty"
    attachments = note.get("attachments") or []
    line = f"- {title} (ID: {note.get('name', 'N/A')})\n"
    line += f"  Kind: {kind_part}"
    if attachments:
        line += f" | Attachments: {len(attachments)}"
    if note.get("trashed"):
        line += " | TRASHED"
    line += f"\n  Updated: {note.get('updateTime', 'N/A')}"
    return line


def format_notes_list(
    notes: List[Dict[str, Any]],
    user_google_email: str,
    filter_expr: Optional[str],
    next_page_token: Optional[str],
) -> str:
    """Render the ``notes.list`` response."""
    header = f"Keep notes for {user_google_email}"
    if filter_expr:
        header += f" (filter: {filter_expr})"
    if not notes:
        text = f"No Keep notes found for {user_google_email}."
        if filter_expr:
            text = (
                f"No Keep notes matched filter '{filter_expr}' for {user_google_email}."
            )
        if next_page_token:
            text += f"\nMore results may follow.\nNext page token: {next_page_token}"
        return text
    lines = [f"{header}:"]
    lines.extend(format_note_summary(note) for note in notes)
    if next_page_token:
        lines.append(f"\nNext page token: {next_page_token}")
    return "\n".join(lines)


def render_list_items(items: List[Dict[str, Any]], indent: int = 0) -> List[str]:
    """Render checklist items as ``[x]``/``[ ]`` lines, nesting children."""
    lines: List[str] = []
    for item in items:
        mark = "[x]" if item.get("checked") else "[ ]"
        text = (item.get("text") or {}).get("text", "") or ""
        lines.append(f"{'  ' * indent}{mark} {text}")
        children = item.get("childListItems") or []
        if children:
            lines.extend(render_list_items(children, indent + 1))
    return lines


def format_permission_line(permission: Dict[str, Any]) -> str:
    """One line for a permission entry."""
    principal = permission.get("email")
    if not principal:
        for key in ("user", "group"):
            principal = (permission.get(key) or {}).get("email")
            if principal:
                break
    if not principal and "family" in permission:
        principal = "(family)"
    principal = principal or "(unknown)"
    line = f"- {principal}: {permission.get('role', 'ROLE_UNSPECIFIED')}"
    line += f" (ID: {permission.get('name', 'N/A')})"
    if permission.get("deleted"):
        line += " [deleted account]"
    return line


def format_permissions(note_name: str, permissions: List[Dict[str, Any]]) -> str:
    """Render the permissions attached to a note."""
    if not permissions:
        return f"No permissions listed on {note_name}."
    lines = [f"Permissions on {note_name}:"]
    lines.extend(format_permission_line(p) for p in permissions)
    return "\n".join(lines)


def format_note_detail(note: Dict[str, Any]) -> str:
    """Full rendering of a note: metadata, body, attachments, permissions."""
    title = note.get("title") or "(untitled)"
    kind = note_kind(note)
    lines = [
        f"Title: {title}",
        f"ID: {note.get('name', 'N/A')}",
        f"Kind: {kind}",
        f"Created: {note.get('createTime', 'N/A')}",
        f"Updated: {note.get('updateTime', 'N/A')}",
    ]
    if note.get("trashed"):
        lines.append(f"Trashed: yes (trashTime: {note.get('trashTime', 'N/A')})")

    lines.append("")
    if kind == "list":
        items = _note_list_items(note)
        total, checked = count_list_items(items)
        lines.append(f"Checklist ({checked}/{total} checked):")
        lines.extend(render_list_items(items) or ["(no items)"])
    elif kind == "text":
        lines.append("Text:")
        lines.append(_note_text(note) or "(empty)")
    else:
        lines.append("Body: (empty)")

    attachments = note.get("attachments") or []
    lines.append("")
    if attachments:
        lines.append(f"Attachments ({len(attachments)}):")
        for attachment in attachments:
            mime_types = ", ".join(attachment.get("mimeType") or []) or "unknown"
            lines.append(f"- {attachment.get('name', 'N/A')} [{mime_types}]")
        lines.append(
            "Use get_keep_note with download_attachment=<attachment name> and "
            "mime_type=<one of the listed types> to fetch an attachment."
        )
    else:
        lines.append("Attachments: none")

    permissions = note.get("permissions") or []
    lines.append("")
    lines.append(format_permissions(note.get("name", "note"), permissions))
    return "\n".join(lines)


# --- request bodies -------------------------------------------------------------


def _build_list_item(raw: Any, path: str) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise UserInputError(
            f"{path} must be an object like "
            "{'text': 'Milk', 'checked': false, 'children': [...]}."
        )
    text = raw.get("text")
    if not isinstance(text, str) or not text.strip():
        raise UserInputError(f"{path}.text must be a non-empty string.")
    checked = raw.get("checked", False)
    if not isinstance(checked, bool):
        raise UserInputError(f"{path}.checked must be true or false.")
    item: Dict[str, Any] = {"text": {"text": text}, "checked": checked}
    children = raw.get("children")
    if children is None:
        children = raw.get("childListItems")
    if children:
        if not isinstance(children, list):
            raise UserInputError(f"{path}.children must be a list of items.")
        item["childListItems"] = [
            _build_list_item(child, f"{path}.children[{i}]")
            for i, child in enumerate(children)
        ]
    return item


def build_note_body(
    text: Optional[str], list_items: Optional[List[Dict[str, Any]]]
) -> Dict[str, Any]:
    """Build a Keep ``Section`` from either plain text or checklist items."""
    has_text = text is not None and text != ""
    has_list = bool(list_items)
    if has_text and has_list:
        raise UserInputError(
            "Provide either 'text' or 'list_items' for a note body, not both."
        )
    if has_text:
        return {"text": {"text": text}}
    if has_list:
        if not isinstance(list_items, list):
            raise UserInputError("'list_items' must be a list of items.")
        return {
            "list": {
                "listItems": [
                    _build_list_item(item, f"list_items[{i}]")
                    for i, item in enumerate(list_items)
                ]
            }
        }
    raise UserInputError(
        "A note needs a body: pass 'text' for a text note or 'list_items' for a "
        "checklist."
    )


def build_create_note_request(
    title: Optional[str],
    text: Optional[str],
    list_items: Optional[List[Dict[str, Any]]],
) -> Dict[str, Any]:
    """Build the ``notes.create`` request body."""
    body: Dict[str, Any] = {"body": build_note_body(text, list_items)}
    if title:
        body["title"] = title
    return body


def build_permission_requests(
    note_name: str, emails: List[str]
) -> List[Dict[str, Any]]:
    """Build ``CreatePermissionRequest`` entries granting WRITER to each email."""
    requests: List[Dict[str, Any]] = []
    seen = set()
    for raw in emails:
        if not isinstance(raw, str) or "@" not in raw:
            raise UserInputError(f"Invalid email address: {raw!r}.")
        email = raw.strip().lower()
        if email in seen:
            continue
        seen.add(email)
        requests.append(
            {
                "parent": note_name,
                "permission": {"email": email, "role": _PERMISSION_ROLE},
            }
        )
    if not requests:
        raise UserInputError("At least one email is required for 'grant'.")
    return requests


# --- attachments ----------------------------------------------------------------


def is_text_like_mime_type(mime_type: str) -> bool:
    """Whether attachment bytes of this type can be shown as text."""
    lowered = (mime_type or "").lower()
    return (
        lowered.startswith("text/")
        or lowered in _TEXT_LIKE_MIME_TYPES
        or lowered.endswith(("+json", "+xml"))
    )


def find_attachment(
    note: Dict[str, Any], attachment_name: str
) -> Optional[Dict[str, Any]]:
    """Locate an attachment entry on a fetched note by resource name."""
    for attachment in note.get("attachments") or []:
        if attachment.get("name") == attachment_name:
            return attachment
    return None


def resolve_attachment_mime_type(
    attachment: Dict[str, Any], requested: Optional[str]
) -> str:
    """Pick the MIME type to download, validating against the attachment."""
    available = attachment.get("mimeType") or []
    if requested:
        if available and requested not in available:
            raise UserInputError(
                f"Attachment {attachment.get('name')} is not available as "
                f"'{requested}'. Available types: {', '.join(available)}."
            )
        return requested
    if len(available) == 1:
        return available[0]
    if not available:
        raise UserInputError(
            "The attachment lists no MIME types; pass 'mime_type' explicitly."
        )
    raise UserInputError(
        "Attachment is available in several MIME types; pass 'mime_type' as one "
        f"of: {', '.join(available)}."
    )


async def download_attachment(
    service: Any,
    note: Dict[str, Any],
    attachment_name: str,
    mime_type: Optional[str],
    include_base64: bool,
) -> str:
    """Download one attachment via ``media.download`` with ``alt=media``.

    googleapiclient exposes the ``alt=media`` form of ``keep.media.download``
    as ``media().download_media(...)``; passing ``alt="media"`` to the plain
    ``download(...)`` is silently rewritten to ``alt=json`` by the library.

    Text-like types are decoded and returned inline. Binary types return the
    size, and the base64 payload only when ``include_base64`` is set. The
    download honors ``WORKSPACE_MCP_MAX_FILE_BYTES`` through the shared
    file-limit helpers.
    """
    attachment = find_attachment(note, attachment_name)
    if attachment is None:
        known = (
            ", ".join(a.get("name", "?") for a in (note.get("attachments") or []))
            or "none"
        )
        raise UserInputError(
            f"Attachment '{attachment_name}' not found on note "
            f"'{note.get('name')}'. Known attachments: {known}."
        )
    resolved_mime = resolve_attachment_mime_type(attachment, mime_type)

    request = service.media().download_media(
        name=attachment_name, mimeType=resolved_mime
    )
    try:
        content = await download_media_bytes(
            request,
            file_name=attachment_name.rsplit("/", 1)[-1],
            file_id=attachment_name,
            kind="attachment",
        )
    except FileTooLargeError as error:
        return str(error)

    size = len(content)
    lines = [
        f"Attachment: {attachment_name}",
        f"MIME type: {resolved_mime}",
        f"Size: {size:,} bytes",
    ]
    if is_text_like_mime_type(resolved_mime):
        lines.append("")
        lines.append(content.decode("utf-8", errors="replace"))
    elif include_base64:
        lines.append("")
        lines.append("Base64 content:")
        lines.append(base64.b64encode(content).decode("ascii"))
    else:
        lines.append(
            "Binary content not shown; pass include_base64=True to receive it "
            "as standard base64."
        )
    return "\n".join(lines)
