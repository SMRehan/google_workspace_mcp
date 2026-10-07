"""
Google Drive Helper Functions

Shared utilities for Google Drive operations including permission checking,
remote content download, and import-time format conversion.
"""

import asyncio
import base64
import binascii
import hashlib
import io
import json
import logging
import re
import uuid
import zipfile
import zlib
from pathlib import Path
from tempfile import NamedTemporaryFile, SpooledTemporaryFile
from typing import List, Dict, Any, Awaitable, BinaryIO, Callable, Optional, Tuple
from urllib.parse import urlencode, urlparse
from urllib.request import url2pathname
from weakref import WeakValueDictionary

import httpx
from googleapiclient.errors import HttpError
from googleapiclient.http import HttpRequest, MediaIoBaseDownload, MediaIoBaseUpload

from auth.permissions import is_action_denied
from auth.service_decorator import require_google_service
from core.file_limits import (
    FileTooLargeError,
    download_media_bytes,
    ensure_within_file_size_limit,
)
from core.http_utils import (
    redact_url as _redact_url,
    ssrf_safe_stream as _ssrf_safe_stream,
)
from core.utils import (
    GOOGLE_API_WRITE_RETRIES,
    IMAGE_MIME_TYPES,
    OfficeXmlExtractionError,
    UserInputError,
    encode_image_content,
    extract_office_xml_text,
    extract_pdf_text,
    local_file_access_enabled,
    validate_file_path,
)

logger = logging.getLogger(__name__)

VALID_SHARE_ROLES = {"reader", "commenter", "writer"}
VALID_SHARE_TYPES = {"user", "group", "domain", "anyone"}


def check_public_link_permission(permissions: List[Dict[str, Any]]) -> bool:
    """
    Check if file has 'anyone with the link' permission.

    Args:
        permissions: List of permission objects from Google Drive API

    Returns:
        bool: True if file has public link sharing enabled
    """
    return any(
        p.get("type") == "anyone" and p.get("role") in ["reader", "writer", "commenter"]
        for p in permissions
    )


def list_all_permissions(service, file_id: str) -> List[Dict[str, Any]]:
    """
    Return the complete permission set for a file, including Shared Drive items.

    files.get() does NOT populate the inline `permissions` field (nor the `shared`
    boolean) for items that live in a Shared Drive, even when supportsAllDrives=True
    is passed. permissions.list() is the only reliable source in that case. This
    helper paginates and always sets supportsAllDrives=True.
    """
    permissions: List[Dict[str, Any]] = []
    page_token = None
    while True:
        resp = (
            service.permissions()
            .list(
                fileId=file_id,
                supportsAllDrives=True,
                pageSize=100,
                pageToken=page_token,
                fields=(
                    "nextPageToken, permissions(id, type, role, emailAddress, "
                    "domain, expirationTime, permissionDetails, allowFileDiscovery)"
                ),
            )
            .execute()
        )
        permissions.extend(resp.get("permissions", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return permissions


def derive_shared_state(
    file_metadata: Dict[str, Any], permissions: List[Dict[str, Any]]
) -> bool:
    """
    Determine whether a file is shared.

    The Drive API omits the `shared` boolean for Shared Drive items, so derive it
    from the driveId and the resolved permission set instead of trusting `shared`.
    """
    if file_metadata.get("shared"):
        return True
    if file_metadata.get("driveId"):
        return True
    if any(p.get("type") in ("anyone", "domain") for p in permissions):
        return True
    return len([p for p in permissions if p.get("type") in ("user", "group")]) > 1


def format_public_sharing_error(file_name: str, file_id: str) -> str:
    """
    Format error message for files without public sharing.

    Args:
        file_name: Name of the file
        file_id: Google Drive file ID

    Returns:
        str: Formatted error message
    """
    return (
        f"❌ Permission Error: '{file_name}' not shared publicly. "
        f"Set 'Anyone with the link' → 'Viewer' in Google Drive sharing. "
        f"File: https://drive.google.com/file/d/{file_id}/view"
    )


def get_drive_image_url(file_id: str) -> str:
    """
    Get the correct Drive URL format for publicly shared images.

    Args:
        file_id: Google Drive file ID

    Returns:
        str: URL for embedding Drive images
    """
    return f"https://drive.google.com/uc?export=view&id={file_id}"


def validate_share_role(role: str) -> None:
    """
    Validate that the role is valid for sharing.

    Args:
        role: The permission role to validate

    Raises:
        ValueError: If role is not reader, commenter, or writer
    """
    if role not in VALID_SHARE_ROLES:
        raise ValueError(
            f"Invalid role '{role}'. Must be one of: {', '.join(sorted(VALID_SHARE_ROLES))}"
        )


def validate_share_type(share_type: str) -> None:
    """
    Validate that the share type is valid.

    Args:
        share_type: The type of sharing to validate

    Raises:
        ValueError: If share_type is not user, group, domain, or anyone
    """
    if share_type not in VALID_SHARE_TYPES:
        raise ValueError(
            f"Invalid share_type '{share_type}'. Must be one of: {', '.join(sorted(VALID_SHARE_TYPES))}"
        )


RFC3339_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$"
)


def validate_expiration_time(expiration_time: str) -> None:
    """
    Validate that expiration_time is in RFC 3339 format.

    Args:
        expiration_time: The expiration time string to validate

    Raises:
        ValueError: If expiration_time is not valid RFC 3339 format
    """
    if not RFC3339_PATTERN.match(expiration_time):
        raise ValueError(
            f"Invalid expiration_time '{expiration_time}'. "
            "Must be RFC 3339 format (e.g., '2025-01-15T00:00:00Z')"
        )


def format_permission_info(permission: Dict[str, Any]) -> str:
    """
    Format a permission object for display.

    Args:
        permission: Permission object from Google Drive API

    Returns:
        str: Human-readable permission description with ID
    """
    perm_type = permission.get("type", "unknown")
    role = permission.get("role", "unknown")
    perm_id = permission.get("id", "")

    if perm_type == "anyone":
        base = f"Anyone with the link ({role}) [id: {perm_id}]"
    elif perm_type == "user":
        email = permission.get("emailAddress", "unknown")
        base = f"User: {email} ({role}) [id: {perm_id}]"
    elif perm_type == "group":
        email = permission.get("emailAddress", "unknown")
        base = f"Group: {email} ({role}) [id: {perm_id}]"
    elif perm_type == "domain":
        domain = permission.get("domain", "unknown")
        base = f"Domain: {domain} ({role}) [id: {perm_id}]"
    else:
        base = f"{perm_type} ({role}) [id: {perm_id}]"

    extras = []
    if permission.get("expirationTime"):
        extras.append(f"expires: {permission['expirationTime']}")

    perm_details = permission.get("permissionDetails", [])
    if perm_details:
        for detail in perm_details:
            if detail.get("inherited") and detail.get("inheritedFrom"):
                extras.append(f"inherited from: {detail['inheritedFrom']}")
                break

    if extras:
        return f"{base} | {', '.join(extras)}"
    return base


# Matches an explicit trashed clause anywhere in a Drive query, e.g. "trashed = true".
# Drive accepts both = and != on boolean fields, so `trashed != false` is just as much
# a caller-supplied filter as `trashed = true`. Used both for structured-query detection
# and to avoid double-adding a trashed filter.
TRASHED_CLAUSE_PATTERN = re.compile(r"\btrashed\s*!?=\s*(true|false)\b", re.IGNORECASE)

# Matches a Drive query string literal, including backslash escapes, in either quote
# style: 'a\'b' or "a\"b". Used to blank out literals before looking for operators, so
# text that merely *looks* like a predicate inside a value is not mistaken for one.
QUERY_STRING_LITERAL_PATTERN = re.compile(r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"")


def has_explicit_trashed_clause(query: str) -> bool:
    """Return True when `query` contains a real `trashed =`/`!=` `true/false` predicate.

    Quoted values are blanked out first, so a search for a filename that happens to
    contain the text (``name contains 'trashed=false'``) is not read as the caller
    having already filtered by trash state.
    """
    without_literals = QUERY_STRING_LITERAL_PATTERN.sub("''", query)
    return bool(TRASHED_CLAUSE_PATTERN.search(without_literals))


# Drive API v2 query field names and their v3 replacements, see
# https://developers.google.com/drive/api/guides/v2-to-v3-reference
DRIVE_V2_TO_V3_QUERY_FIELD_MAP = {
    "title": "name",
    "createddate": "createdTime",
    "modifieddate": "modifiedTime",
    "lastviewedbymedate": "viewedByMeTime",
}

# Literals are matched first so field names inside quoted values are left alone.
_DRIVE_V2_FIELD_OR_LITERAL_PATTERN = re.compile(
    rf"({QUERY_STRING_LITERAL_PATTERN.pattern})"
    rf"|\b({'|'.join(DRIVE_V2_TO_V3_QUERY_FIELD_MAP)})\b",
    re.IGNORECASE,
)


def normalize_drive_query_v2_compat(query: str) -> str:
    """Rewrite Drive API v2 field names outside string literals to their v3 names.

    ``title contains 'title'`` becomes ``name contains 'title'``.
    """
    return _DRIVE_V2_FIELD_OR_LITERAL_PATTERN.sub(
        lambda m: m.group(1) or DRIVE_V2_TO_V3_QUERY_FIELD_MAP[m.group(2).lower()],
        query,
    )


# Precompiled regex patterns for Drive query detection
DRIVE_QUERY_PATTERNS = [
    re.compile(r'\b\w+\s*(=|!=|>|<)\s*[\'"].*?[\'"]', re.IGNORECASE),  # field = 'value'
    re.compile(r"\b\w+\s*(=|!=|>|<)\s*\d+", re.IGNORECASE),  # field = number
    re.compile(r"\bcontains\b", re.IGNORECASE),  # contains operator
    re.compile(r"\bin\s+parents\b", re.IGNORECASE),  # in parents
    re.compile(r"\bhas\s*\{", re.IGNORECASE),  # has {properties}
    TRASHED_CLAUSE_PATTERN,  # trashed =/!= true/false
    re.compile(r"\bstarred\s*=\s*(true|false)\b", re.IGNORECASE),  # starred=true/false
    re.compile(
        r'[\'"][^\'"]+[\'"]\s+in\s+parents', re.IGNORECASE
    ),  # 'parentId' in parents
    re.compile(r"\bfullText\s+contains\b", re.IGNORECASE),  # fullText contains
    re.compile(r"\bname\s*(=|contains)\b", re.IGNORECASE),  # name = or name contains
    re.compile(r"\bmimeType\s*(=|!=)\b", re.IGNORECASE),  # mimeType operators
]


def build_drive_list_params(
    query: str,
    page_size: int,
    drive_id: Optional[str] = None,
    include_items_from_all_drives: bool = True,
    corpora: Optional[str] = None,
    page_token: Optional[str] = None,
    detailed: bool = True,
    include_permissions: bool = False,
    order_by: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Helper function to build common list parameters for Drive API calls.

    Args:
        query: The search query string
        page_size: Maximum number of items to return
        drive_id: Optional shared drive ID
        include_items_from_all_drives: Whether to include items from all drives
        corpora: Optional corpus specification. Defaults to 'drive' when drive_id
                 is set, otherwise 'allDrives' when include_items_from_all_drives is True.
        page_token: Optional page token for pagination (from a previous nextPageToken)
        detailed: Whether to request size, modifiedTime, and webViewLink fields.
                  Defaults to True to preserve existing behavior.
        include_permissions: Whether detailed results should include file ACL fields.
        order_by: Optional sort order. Comma-separated list of sort keys.
                  Valid keys: 'createdTime', 'folder', 'modifiedByMeTime', 'modifiedTime',
                  'name', 'name_natural', 'quotaBytesUsed', 'recency', 'sharedWithMeTime',
                  'starred', 'viewedByMeTime'. Add 'desc' modifier to reverse (e.g., 'modifiedTime desc').
                  Example: 'folder,modifiedTime desc,name'

    Returns:
        Dictionary of parameters for Drive API list calls
    """
    if detailed:
        permission_fields = (
            ", permissions(id, type, role)" if include_permissions else ""
        )
        fields = (
            "nextPageToken, incompleteSearch, files(id, name, mimeType, webViewLink, iconLink,"
            " modifiedTime, createdTime, size, driveId,"
            " lastModifyingUser(displayName, emailAddress)"
            f"{permission_fields})"
        )
    else:
        fields = "nextPageToken, incompleteSearch, files(id, name, mimeType)"
    list_params = {
        "q": query,
        "pageSize": page_size,
        "fields": fields,
        "supportsAllDrives": True,
        "includeItemsFromAllDrives": include_items_from_all_drives,
    }

    if page_token:
        list_params["pageToken"] = page_token

    if order_by is not None:
        normalized_order_by = order_by.strip()
        if normalized_order_by:
            list_params["orderBy"] = normalized_order_by

    if drive_id:
        list_params["driveId"] = drive_id
        list_params["corpora"] = corpora or "drive"
    elif corpora:
        list_params["corpora"] = corpora
    elif include_items_from_all_drives:
        # The API's default 'user' corpus skips shared drives the user belongs to
        # even when includeItemsFromAllDrives is set.
        list_params["corpora"] = "allDrives"

    return list_params


INCOMPLETE_SEARCH_WARNING = (
    "WARNING: Google Drive did not search every corpus (incompleteSearch), so these "
    "results may be incomplete. Narrow the search (e.g. a specific shared drive or "
    "the 'user' corpus) to find the rest."
)


def flag_incomplete_search(text: str, response: Dict[str, Any]) -> str:
    """Append a warning when a files.list response reports incompleteSearch.

    Searching the 'allDrives' corpus can skip some shared drives; without this the
    caller would present a partial result set as complete.
    """
    if response.get("incompleteSearch"):
        return f"{text}\n{INCOMPLETE_SEARCH_WARNING}"
    return text


GOOGLE_APPS_MIME_PREFIX = "application/vnd.google-apps."
SHORTCUT_MIME_TYPE = "application/vnd.google-apps.shortcut"
FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"

# RFC 6838 token-style MIME type validation (safe for Drive query interpolation).
MIME_TYPE_PATTERN = re.compile(r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+$")

# Mapping from friendly type names to Google Drive MIME types.
# Raw MIME type strings (containing '/') are always accepted as-is.
FILE_TYPE_MIME_MAP: Dict[str, str] = {
    "folder": "application/vnd.google-apps.folder",
    "folders": "application/vnd.google-apps.folder",
    "document": "application/vnd.google-apps.document",
    "doc": "application/vnd.google-apps.document",
    "documents": "application/vnd.google-apps.document",
    "docs": "application/vnd.google-apps.document",
    "spreadsheet": "application/vnd.google-apps.spreadsheet",
    "sheet": "application/vnd.google-apps.spreadsheet",
    "spreadsheets": "application/vnd.google-apps.spreadsheet",
    "sheets": "application/vnd.google-apps.spreadsheet",
    "presentation": "application/vnd.google-apps.presentation",
    "presentations": "application/vnd.google-apps.presentation",
    "slide": "application/vnd.google-apps.presentation",
    "slides": "application/vnd.google-apps.presentation",
    "form": "application/vnd.google-apps.form",
    "forms": "application/vnd.google-apps.form",
    "drawing": "application/vnd.google-apps.drawing",
    "drawings": "application/vnd.google-apps.drawing",
    "pdf": "application/pdf",
    "pdfs": "application/pdf",
    "shortcut": "application/vnd.google-apps.shortcut",
    "shortcuts": "application/vnd.google-apps.shortcut",
    "script": "application/vnd.google-apps.script",
    "scripts": "application/vnd.google-apps.script",
    "site": "application/vnd.google-apps.site",
    "sites": "application/vnd.google-apps.site",
    "jam": "application/vnd.google-apps.jam",
    "jamboard": "application/vnd.google-apps.jam",
    "jamboards": "application/vnd.google-apps.jam",
}


def resolve_file_type_mime(file_type: str) -> str:
    """
    Resolve a friendly file type name or raw MIME type string to a Drive MIME type.

    If `file_type` contains '/' it is returned as-is (treated as a raw MIME type).
    Otherwise it is looked up in FILE_TYPE_MIME_MAP.

    Args:
        file_type: A friendly name ('folder', 'document', 'pdf', …) or a raw MIME
                   type string ('application/vnd.google-apps.document', …).

    Returns:
        str: The resolved MIME type string.

    Raises:
        ValueError: If the value is not a recognised friendly name and contains no '/'.
    """
    normalized = file_type.strip()
    if not normalized:
        raise ValueError("file_type cannot be empty.")

    if "/" in normalized:
        normalized_mime = normalized.lower()
        if not MIME_TYPE_PATTERN.fullmatch(normalized_mime):
            raise ValueError(
                f"Invalid MIME type '{file_type}'. Expected format like 'application/pdf'."
            )
        return normalized_mime
    lower = normalized.lower()
    if lower not in FILE_TYPE_MIME_MAP:
        valid = ", ".join(sorted(FILE_TYPE_MIME_MAP.keys()))
        raise ValueError(
            f"Unknown file_type '{file_type}'. Pass a MIME type directly (e.g. "
            f"'application/pdf') or use one of the friendly names: {valid}"
        )
    return FILE_TYPE_MIME_MAP[lower]


BASE_SHORTCUT_FIELDS = (
    "id, mimeType, parents, shortcutDetails(targetId, targetMimeType)"
)


async def resolve_drive_item(
    service,
    file_id: str,
    *,
    extra_fields: Optional[str] = None,
    max_depth: int = 5,
    follow_shortcuts: bool = True,
) -> Tuple[str, Dict[str, Any]]:
    """
    Fetch Drive item metadata and optionally resolve shortcuts to their targets.

    By default shortcuts are followed so content-oriented callers operate on the real
    item. Set ``follow_shortcuts=False`` for resource-local metadata mutations (rename,
    move, trash, star, description, etc.) that must act on the shortcut itself.

    Returns the selected file ID and its metadata. Raises if shortcut targets loop
    or exceed max_depth to avoid infinite recursion.
    """
    current_id = file_id
    depth = 0
    fields = BASE_SHORTCUT_FIELDS
    if extra_fields:
        fields = f"{fields}, {extra_fields}"

    while True:
        metadata = await asyncio.to_thread(
            service.files()
            .get(fileId=current_id, fields=fields, supportsAllDrives=True)
            .execute
        )
        mime_type = metadata.get("mimeType")
        if not follow_shortcuts or mime_type != SHORTCUT_MIME_TYPE:
            return current_id, metadata

        shortcut_details = metadata.get("shortcutDetails") or {}
        target_id = shortcut_details.get("targetId")
        if not target_id:
            raise Exception(f"Shortcut '{current_id}' is missing target details.")

        depth += 1
        if depth > max_depth:
            raise Exception(
                f"Shortcut resolution exceeded {max_depth} hops starting from '{file_id}'."
            )
        current_id = target_id


async def resolve_folder_id(
    service,
    folder_id: str,
    *,
    max_depth: int = 5,
) -> str:
    """
    Resolve a folder ID that might be a shortcut and ensure the final target is a folder.
    """
    resolved_id, metadata = await resolve_drive_item(
        service,
        folder_id,
        max_depth=max_depth,
    )
    mime_type = metadata.get("mimeType")
    if mime_type != FOLDER_MIME_TYPE:
        raise Exception(
            f"Resolved ID '{resolved_id}' (from '{folder_id}') is not a folder; mimeType={mime_type}."
        )
    return resolved_id


async def place_file_in_folder(
    drive_service: Any,
    file_id: str,
    folder_id: str,
    *,
    tool_name: str = "place_file_in_folder",
) -> str:
    """
    Move ``file_id`` into ``folder_id`` (ID or shortcut), removing its other parents.

    The Docs and Sheets create endpoints accept no parent, so new files land in
    My Drive root and must be re-parented afterwards. Returns the resolved folder ID.
    """
    resolved_folder_id = await resolve_folder_id(drive_service, folder_id)
    existing = await asyncio.to_thread(
        drive_service.files()
        .get(fileId=file_id, fields="parents", supportsAllDrives=True)
        .execute
    )
    # Skip the destination so a file already there is not both added and removed.
    remove_parents = ",".join(
        parent for parent in existing.get("parents", []) if parent != resolved_folder_id
    )
    await asyncio.to_thread(
        drive_service.files()
        .update(
            fileId=file_id,
            addParents=resolved_folder_id,
            removeParents=remove_parents,
            fields="id, parents",
            supportsAllDrives=True,
        )
        .execute
    )
    logger.info(
        f"[{tool_name}] Moved file {file_id} into folder {resolved_folder_id} "
        f"(removed parents: '{remove_parents}')"
    )
    return resolved_folder_id


@require_google_service("drive", "drive_file")
async def place_created_file_in_folder(
    service,
    user_google_email: str,
    file_id: str,
    folder_id: str,
    tool_name: str = "place_created_file_in_folder",
) -> str:
    """
    Authenticate Drive on demand, then run ``place_file_in_folder``.

    Kept off the calling tool's decorator so the ``folder_id="root"`` default
    needs no Drive scope (and no full-Drive scope under domain-wide delegation).
    """
    return await place_file_in_folder(service, file_id, folder_id, tool_name=tool_name)


async def move_new_file_to_folder(
    user_google_email: str,
    file_id: str,
    folder_id: Optional[str],
    tool_name: str,
) -> str:
    """
    Move a just-created file into ``folder_id`` and return a note for the reply.

    A failed move is reported, not raised: the file already exists in My Drive
    root, so the caller still needs its ID.
    """
    if not folder_id or folder_id == "root":
        return ""
    try:
        await place_created_file_in_folder(
            user_google_email=user_google_email,
            file_id=file_id,
            folder_id=folder_id,
            tool_name=tool_name,
        )
    except Exception as e:
        logger.warning(
            f"[{tool_name}] Created file {file_id} but could not move it into "
            f"folder '{folder_id}': {e}"
        )
        return (
            f" WARNING: left in My Drive root - could not move it into folder "
            f"'{folder_id}': {e}"
        )
    return f" Placed in folder '{folder_id}'."


async def _create_drive_folder_impl(
    service,
    user_google_email: str,
    folder_name: str,
    parent_folder_id: str = "root",
) -> str:
    """Internal implementation for create_drive_folder. Used by tests."""
    resolved_folder_id = await resolve_folder_id(service, parent_folder_id)
    file_metadata = {
        "name": folder_name,
        "parents": [resolved_folder_id],
        "mimeType": FOLDER_MIME_TYPE,
    }
    created_file = await asyncio.to_thread(
        service.files()
        .create(
            body=file_metadata,
            fields="id, name, webViewLink",
            supportsAllDrives=True,
        )
        .execute
    )
    link = created_file.get("webViewLink", "")
    return (
        f"Successfully created folder '{created_file.get('name', folder_name)}' (ID: {created_file.get('id', 'N/A')}) "
        f"in folder '{parent_folder_id}' for {user_google_email}. Link: {link}"
    )


DOWNLOAD_CHUNK_SIZE_BYTES = 256 * 1024  # 256 KB
UPLOAD_CHUNK_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB (Google recommended minimum)
MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB safety limit for URL downloads

MAX_INLINE_BASE64_BYTES = 100 * 1024 * 1024  # 100 MB decoded payload ceiling
MAX_ZIP_MEMBER_COUNT = 10_000
MAX_ZIP_UNCOMPRESSED_BYTES = 500 * 1024 * 1024  # 500 MB total uncompressed
MAX_ZIP_COMPRESSION_RATIO = 100

# Office Open XML and OpenDocument payloads are ZIP containers. Drive accepts
# malformed bytes at upload time and may only surface the corruption minutes later
# when its Docs/Sheets/Slides importer opens the file, so validate inline payloads
# before making a write request.
ZIP_CONTAINER_REQUIRED_MEMBERS = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": {
        "[Content_Types].xml",
        "word/document.xml",
    },
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {
        "[Content_Types].xml",
        "xl/workbook.xml",
    },
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": {
        "[Content_Types].xml",
        "ppt/presentation.xml",
    },
    "application/vnd.oasis.opendocument.text": {
        "mimetype",
        "META-INF/manifest.xml",
    },
    "application/vnd.oasis.opendocument.spreadsheet": {
        "mimetype",
        "META-INF/manifest.xml",
    },
    "application/vnd.oasis.opendocument.presentation": {
        "mimetype",
        "META-INF/manifest.xml",
    },
}


def _decode_base64_upload(
    base64_content: str,
    *,
    tool_name: str,
    mime_type: str,
    expected_sha256: Optional[str] = None,
) -> bytes:
    """Decode and validate an inline binary upload before sending it to Drive."""
    max_encoded_len = ((MAX_INLINE_BASE64_BYTES + 2) // 3) * 4
    if len(base64_content) > max_encoded_len:
        estimated_decoded_size = len(base64_content) * 3 // 4
        raise ValueError(
            f"[{tool_name}] Inline payload exceeds the "
            f"{MAX_INLINE_BASE64_BYTES // (1024 * 1024)} MB limit "
            f"(estimated {estimated_decoded_size // (1024 * 1024)} MB). "
            "Upload via 'fileUrl' instead."
        )

    try:
        file_data = base64.b64decode(base64_content, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("'base64_content' must be valid standard base64.") from exc

    if len(file_data) > MAX_INLINE_BASE64_BYTES:
        raise ValueError(
            f"[{tool_name}] Inline payload exceeds the "
            f"{MAX_INLINE_BASE64_BYTES // (1024 * 1024)} MB limit "
            f"({len(file_data) // (1024 * 1024)} MB). "
            "Upload via 'fileUrl' instead."
        )

    if expected_sha256 is not None:
        normalized_sha256 = expected_sha256.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", normalized_sha256):
            raise ValueError(
                "'base64_sha256' must be a 64-character hexadecimal SHA-256."
            )
        actual_sha256 = hashlib.sha256(file_data).hexdigest()
        if actual_sha256 != normalized_sha256:
            raise ValueError(
                f"[{tool_name}] Inline binary payload failed its SHA-256 integrity check. "
                "The base64 content was altered or truncated before reaching the server."
            )

    required_members = ZIP_CONTAINER_REQUIRED_MEMBERS.get(mime_type)
    if required_members is not None:
        try:
            with zipfile.ZipFile(io.BytesIO(file_data)) as archive:
                members = archive.infolist()
                if len(members) > MAX_ZIP_MEMBER_COUNT:
                    raise ValueError(
                        f"[{tool_name}] Inline {mime_type} archive contains "
                        f"{len(members)} members (limit {MAX_ZIP_MEMBER_COUNT})."
                    )
                total_uncompressed = sum(m.file_size for m in members)
                if total_uncompressed > MAX_ZIP_UNCOMPRESSED_BYTES:
                    raise ValueError(
                        f"[{tool_name}] Inline {mime_type} archive uncompressed size "
                        f"({total_uncompressed // (1024 * 1024)} MB) exceeds the "
                        f"{MAX_ZIP_UNCOMPRESSED_BYTES // (1024 * 1024)} MB limit."
                    )
                if (
                    len(file_data) > 0
                    and total_uncompressed // max(len(file_data), 1)
                    > MAX_ZIP_COMPRESSION_RATIO
                ):
                    raise ValueError(
                        f"[{tool_name}] Inline {mime_type} archive compression ratio "
                        f"exceeds {MAX_ZIP_COMPRESSION_RATIO}x."
                    )
                names = set(archive.namelist())
                missing = required_members - names
                if missing:
                    raise ValueError(
                        f"[{tool_name}] Inline {mime_type} payload is missing required "
                        f"archive members: {', '.join(sorted(missing))}."
                    )
                corrupt_member = archive.testzip()
                if corrupt_member is not None:
                    raise ValueError(
                        f"[{tool_name}] Inline {mime_type} payload contains a corrupt "
                        f"archive member: {corrupt_member}."
                    )
        except ValueError:
            raise
        except (zipfile.BadZipFile, EOFError, RuntimeError, zlib.error) as exc:
            raise ValueError(
                f"[{tool_name}] Inline {mime_type} payload is not a valid, intact archive."
            ) from exc

    return file_data


def _use_resumable_upload(size: int) -> bool:
    """Use Drive's resumable protocol only when a simple upload is too large."""
    return size > UPLOAD_CHUNK_SIZE_BYTES


RESUMABLE_UPLOAD_BASE = "https://www.googleapis.com/upload/drive/v3/files"


async def initiate_resumable_upload_session(
    service,
    *,
    upload_mime_type: str,
    file_metadata: Optional[Dict[str, Any]] = None,
    file_id: Optional[str] = None,
    query: Optional[Dict[str, str]] = None,
) -> str:
    """Open a Drive resumable upload session and return its pre-authorized URL.

    ``file_id`` absent: POST creates a new file; present: PATCH replaces that
    file's content. ``file_metadata`` and ``query`` (e.g. ``addParents``) take
    effect only when the upload completes. The caller PUTs the bytes to the
    returned URL with no Authorization header, so the payload never passes
    through this server; only this initiation call uses the user's authorized
    transport.
    """
    path = f"/{file_id}" if file_id else ""
    params = {"uploadType": "resumable", "supportsAllDrives": "true", **(query or {})}
    url = f"{RESUMABLE_UPLOAD_BASE}{path}?{urlencode(params)}"
    # service._http is the discovery Resource's private handle on the user's
    # AuthorizedHttp. Use HttpRequest for the same bounded retry policy as other
    # uploads, keeping the response headers because initiation has an empty body.
    # Retrying only opens another session; no file is written until the client PUT.
    request = HttpRequest(
        service._http,
        postproc=lambda response, content: (response, content),
        uri=url,
        method="PATCH" if file_id else "POST",
        body=json.dumps(file_metadata or {}),
        headers={
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": upload_mime_type,
        },
    )
    response, content = await asyncio.to_thread(
        request.execute, num_retries=GOOGLE_API_WRITE_RETRIES
    )
    status = int(response.status)
    if status not in (200, 201):
        # HttpError so handle_http_errors reports it like any other Drive failure.
        raise HttpError(response, content, uri=url)
    upload_url = response.get("location")
    if not upload_url:
        raise Exception(
            "Resumable upload session was created but Google returned no session URL."
        )
    return upload_url


def reject_sources_with_upload_url(verb: str, **sources: Any) -> None:
    """Refuse return_upload_url alongside any inline or server-side source."""
    given = [f"'{name}'" for name, value in sources.items() if value is not None]
    if given:
        raise ValueError(
            f"return_upload_url {verb} via a resumable PUT; do not also pass "
            f"{', '.join(given)}."
        )


def upload_url_not_offered_error(routes: tuple[str, ...]) -> UserInputError:
    """Refuse ``return_upload_url`` while local file access is enabled.

    FastMCP already rejects the hidden parameter, so this reaches only direct
    callers. ``routes`` are the source parameters the calling tool has.
    """
    named = ", ".join(f"'{name}'" for name in routes[:-1]) + f" or '{routes[-1]}'"
    return UserInputError(
        "Upload URLs are offered only when local file access is disabled on this "
        "server (WORKSPACE_MCP_DISABLE_LOCAL_FILES=true); pass the file via "
        f"{named}."
    )


def resumable_upload_result(summary: str, upload_url: str, mime_type: str) -> str:
    return (
        f"{summary}\n\n"
        f"Upload URL (no Authorization header required):\n{upload_url}\n\n"
        "Send the bytes with a single PUT, e.g.:\n"
        f"  curl -H 'Content-Type: {mime_type}' --upload-file \"/path/to/file\" '{upload_url}'\n\n"
        "The session takes one upload; an interrupted PUT can be resumed with "
        "Content-Range until Google expires the session (about a week). The file "
        "is created or updated only when the upload completes, and "
        "the final PUT's response body is the file's metadata (including its id)."
    )


async def _stream_url_with_validation(
    url: str, write_chunk: Optional[Callable[[bytes], Awaitable[None]]] = None
) -> Tuple[int, Optional[str]]:
    """Stream a remote file with shared status and size validation."""
    total_bytes = 0
    redacted_url = _redact_url(url)

    async with _ssrf_safe_stream(url) as resp:
        if resp.status_code != 200:
            request = getattr(resp, "request", None)
            if request is None:
                parsed_url = urlparse(url)
                request = httpx.Request("GET", f"{parsed_url.scheme}://{redacted_url}")
            raise httpx.HTTPStatusError(
                f"Failed to fetch file from URL: {redacted_url} (status {resp.status_code})",
                request=request,
                response=resp,
            )

        content_type = resp.headers.get("Content-Type")
        async for chunk in resp.aiter_bytes(chunk_size=DOWNLOAD_CHUNK_SIZE_BYTES):
            total_bytes += len(chunk)
            if total_bytes > MAX_DOWNLOAD_BYTES:
                raise ValueError(
                    f"Download from {redacted_url} exceeded {MAX_DOWNLOAD_BYTES} byte limit "
                    f"({total_bytes} bytes)"
                )
            if write_chunk is not None:
                await write_chunk(chunk)

    return total_bytes, content_type


async def _download_url_to_bytes(url: str) -> Tuple[BinaryIO, Optional[str]]:
    """Download a remote file into a spooled temporary file with bounded streaming."""
    spool = SpooledTemporaryFile(max_size=UPLOAD_CHUNK_SIZE_BYTES)
    try:

        async def _collect(chunk: bytes) -> None:
            await asyncio.to_thread(spool.write, chunk)

        _total_bytes, content_type = await _stream_url_with_validation(url, _collect)
        await asyncio.to_thread(spool.seek, 0)
        return spool, content_type
    except Exception:
        spool.close()
        raise


# Bytes held in memory per streamed download chunk.
STREAMED_DOWNLOAD_CHUNK_SIZE_BYTES = 8 * 1024 * 1024


def _media_request(service, file_id: str, export_mime_type: Optional[str]):
    """Build the media request, exporting native Google types when requested."""
    return (
        service.files().export_media(fileId=file_id, mimeType=export_mime_type)
        if export_mime_type
        else service.files().get_media(fileId=file_id, supportsAllDrives=True)
    )


async def _download_file_bytes(
    service, file_id: str, export_mime_type: Optional[str] = None
) -> bytes:
    """Download a Drive file's bytes, exporting native Google types when requested.

    Buffers the whole file in memory. Only use this for payloads that are about
    to be parsed as text anyway; anything that ends up on disk should go through
    _download_file_to_temp instead.
    """
    return await download_media_bytes(
        _media_request(service, file_id, export_mime_type)
    )


async def _download_file_to_temp(
    service, file_id: str, export_mime_type: Optional[str] = None
) -> Path:
    """Stream a Drive file to a temporary file and return its path.

    Peak memory is one chunk rather than one copy of the file, so downloading a
    multi-gigabyte file no longer exhausts RAM (see #994). The caller owns the
    returned path and must move or delete it.
    """
    tmp_file = NamedTemporaryFile(prefix="wsmcp_dl_", delete=False)
    tmp_path = Path(tmp_file.name)
    loop = asyncio.get_event_loop()
    try:
        with tmp_file:
            downloader = MediaIoBaseDownload(
                tmp_file,
                _media_request(service, file_id, export_mime_type),
                chunksize=STREAMED_DOWNLOAD_CHUNK_SIZE_BYTES,
            )
            done = False
            while not done:
                _status, done = await loop.run_in_executor(None, downloader.next_chunk)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return tmp_path


# Mapping of file extensions to source MIME types for Google Docs conversion
GOOGLE_DOCS_IMPORT_FORMATS = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".text": "text/plain",
    ".html": "text/html",
    ".htm": "text/html",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".rtf": "application/rtf",
    ".odt": "application/vnd.oasis.opendocument.text",
}

# Mapping of file extensions to source MIME types for Google Slides conversion
GOOGLE_SLIDES_IMPORT_FORMATS = {
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".ppt": "application/vnd.ms-powerpoint",
    ".odp": "application/vnd.oasis.opendocument.presentation",
}

# Mapping of file extensions to source MIME types for Google Sheets conversion
GOOGLE_SHEETS_IMPORT_FORMATS = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".ods": "application/vnd.oasis.opendocument.spreadsheet",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
}

GOOGLE_DOCS_MIME_TYPE = "application/vnd.google-apps.document"
GOOGLE_SLIDES_MIME_TYPE = "application/vnd.google-apps.presentation"
GOOGLE_SHEETS_MIME_TYPE = "application/vnd.google-apps.spreadsheet"

IMPORT_FORMATS_BY_GOOGLE_MIME_TYPE = {
    GOOGLE_DOCS_MIME_TYPE: GOOGLE_DOCS_IMPORT_FORMATS,
    GOOGLE_SHEETS_MIME_TYPE: GOOGLE_SHEETS_IMPORT_FORMATS,
    GOOGLE_SLIDES_MIME_TYPE: GOOGLE_SLIDES_IMPORT_FORMATS,
}


def native_replace_format_map(target_mime_type: str) -> Optional[Dict[str, str]]:
    """Import allowlist for replacing a native file's content; None if not native.

    Raises for a native Google type Drive cannot import content into.
    """
    format_map = IMPORT_FORMATS_BY_GOOGLE_MIME_TYPE.get(target_mime_type)
    if format_map is None and target_mime_type.startswith(GOOGLE_APPS_MIME_PREFIX):
        supported_targets = ", ".join(IMPORT_FORMATS_BY_GOOGLE_MIME_TYPE)
        raise ValueError(
            "Content replacement is not supported for this Google Apps type "
            f"({target_mime_type}). Editable Google types: {supported_targets}."
        )
    return format_map


# Source MIME types safe to build from an in-memory `content` string. Binary
# Office/OpenDocument formats must come from file_path/file_url; UTF-8 encoding
# their bytes from a string would corrupt the upload and its conversion.
TEXT_BASED_IMPORT_MIME_TYPES = {
    "text/plain",
    "text/markdown",
    "text/html",
    "text/csv",
    "text/tab-separated-values",
    "application/rtf",
}


def _is_text_like_mime_type(mime_type: str) -> bool:
    """Whether an in-memory `content` string can safely be uploaded as this MIME type."""
    return (
        mime_type.startswith("text/")
        or mime_type in TEXT_BASED_IMPORT_MIME_TYPES
        or mime_type.endswith(("+json", "+xml"))
        or mime_type in {"application/json", "application/xml"}
    )


def _detect_source_format(
    file_name: str,
    content: Optional[str] = None,
    format_map: Optional[Dict[str, str]] = None,
) -> str:
    """
    Detect the source MIME type from a file extension.

    Uses ``format_map`` (defaults to the Google Docs format map) and falls back to
    text/markdown for markdown-looking content, else text/plain.
    """
    if format_map is None:
        format_map = GOOGLE_DOCS_IMPORT_FORMATS

    ext = Path(file_name).suffix.lower()
    if ext in format_map:
        return format_map[ext]

    if content and (content.startswith("#") or "```" in content or "**" in content):
        return "text/markdown"

    return "text/plain"


async def _resolve_import_media(
    *,
    tool_name: str,
    file_name: str,
    content: Optional[str],
    file_path: Optional[str],
    file_url: Optional[str],
    source_format: Optional[str],
    base64_content: Optional[str] = None,
    base64_sha256: Optional[str] = None,
    format_map: Optional[Dict[str, str]] = None,
    passthrough_mime_type: Optional[str] = None,
) -> Tuple[MediaIoBaseUpload, str, Optional[BinaryIO]]:
    """
    Resolve a content source into an upload ``MediaIoBaseUpload`` and source MIME type.

    Exactly one of ``content``, ``file_path``, ``file_url``, or
    ``base64_content`` must be provided.
    The source bytes are uploaded with their *source* MIME type so the Drive API can
    convert them into the destination Google Apps format. ``format_map`` is the
    extension → source MIME allowlist used for detection and validation.

    Pass ``passthrough_mime_type`` instead of ``format_map`` for non-Google targets
    (a raw .md, .txt, .pdf, ...): there is nothing to convert, so the bytes upload
    verbatim under that MIME type and no source allowlist applies.

    Returns ``(media, source_mime_type, closeable)``; when the source is a remote URL,
    ``closeable`` is the download stream the caller must close after upload (else None).
    """
    # Name file_path only where the tool schema advertises it.
    path_source = "'file_path', " if local_file_access_enabled() else ""
    source_count = sum(
        1 for x in (content, file_path, file_url, base64_content) if x is not None
    )
    if source_count == 0:
        raise ValueError(
            f"You must provide one of: 'content', {path_source}'file_url', or "
            "'base64_content'."
        )
    if source_count > 1:
        raise ValueError(
            f"Provide only one of: 'content', {path_source}'file_url', or "
            "'base64_content'."
        )
    if base64_sha256 is not None and base64_content is None:
        raise ValueError("'base64_sha256' can only be used with 'base64_content'.")

    # Determine source MIME type from the explicit hint or auto-detection.
    if passthrough_mime_type:
        source_mime_type = passthrough_mime_type
    elif source_format:
        format_key = f".{source_format.lower().lstrip('.')}"
        if format_key not in format_map:
            raise ValueError(
                f"Unsupported source_format: '{source_format}'. "
                f"Supported: {', '.join(ext.lstrip('.') for ext in format_map.keys())}"
            )
        source_mime_type = format_map[format_key]
    else:
        detection_name = file_path or file_name
        if file_url is not None:
            detection_name = urlparse(file_url).path or file_url
        source_mime_type = _detect_source_format(detection_name, content, format_map)

    logger.info(f"[{tool_name}] Detected source MIME type: {source_mime_type}")

    file_data: bytes
    remote_file_data: Optional[BinaryIO] = None

    if content is not None:
        if not _is_text_like_mime_type(source_mime_type):
            binary_sources = (
                "'file_path' or 'file_url'" if path_source else "'file_url'"
            )
            raise ValueError(
                f"[{tool_name}] 'content' is only valid for text-based source formats, "
                f"but the source resolves to '{source_mime_type}' (a binary format). "
                f"Provide {binary_sources} for binary formats instead."
            )
        file_data = content.encode("utf-8")
        logger.info(f"[{tool_name}] Using content: {len(file_data)} bytes")

    elif base64_content is not None:
        file_data = _decode_base64_upload(
            base64_content,
            tool_name=tool_name,
            mime_type=source_mime_type,
            expected_sha256=base64_sha256,
        )
        logger.info(
            f"[{tool_name}] Decoded inline binary content: {len(file_data)} bytes"
        )

    elif file_path is not None:
        parsed_url = urlparse(file_path)
        if parsed_url.scheme == "file":
            raw_path = parsed_url.path or ""
            netloc = parsed_url.netloc
            if netloc and netloc.lower() != "localhost":
                raw_path = f"//{netloc}{raw_path}"
            actual_path = url2pathname(raw_path)
        elif parsed_url.scheme == "":
            actual_path = file_path
        else:
            raise ValueError(
                f"file_path should be a local path or file:// URL, got: {file_path}"
            )

        path_obj = validate_file_path(actual_path)
        if not path_obj.exists():
            raise FileNotFoundError(f"File not found: {actual_path}")
        if not path_obj.is_file():
            raise ValueError(f"Path is not a file: {actual_path}")

        file_data = await asyncio.to_thread(path_obj.read_bytes)
        logger.info(f"[{tool_name}] Read local file: {len(file_data)} bytes")

        # Re-detect from the real file extension when no explicit hint was given.
        if not source_format and not passthrough_mime_type:
            source_mime_type = _detect_source_format(actual_path, None, format_map)

    else:  # file_url is not None
        parsed_url = urlparse(file_url)
        if parsed_url.scheme not in ("http", "https"):
            raise ValueError(f"file_url must be http:// or https://, got: {file_url}")

        remote_file_data, remote_content_type = await _download_url_to_bytes(file_url)

        # Prefer the response Content-Type, falling back to URL-based detection.
        if not source_format and not passthrough_mime_type:
            ct_base = (remote_content_type or "").split(";", 1)[0].strip()
            if ct_base and ct_base in format_map.values():
                source_mime_type = ct_base
            else:
                source_mime_type = _detect_source_format(
                    parsed_url.path or file_url, None, format_map
                )

    # Enforce the allowlist on the final resolved MIME type so auto-detection can't
    # upload an unsupported source (e.g. text/plain from an unknown extension).
    # Passthrough uploads have no conversion target, so nothing to enforce.
    if not passthrough_mime_type and source_mime_type not in format_map.values():
        if remote_file_data is not None:
            remote_file_data.close()
        raise ValueError(
            f"[{tool_name}] Detected source MIME type '{source_mime_type}' is not "
            f"supported by this tool. Supported source formats: "
            f"{', '.join(ext.lstrip('.') for ext in sorted(format_map.keys()))}."
        )

    upload_stream = (
        remote_file_data if remote_file_data is not None else io.BytesIO(file_data)
    )
    if remote_file_data is not None:
        current_position = remote_file_data.tell()
        remote_file_data.seek(0, io.SEEK_END)
        upload_size = remote_file_data.tell()
        remote_file_data.seek(current_position)
    else:
        upload_size = len(file_data)

    media = MediaIoBaseUpload(
        upload_stream,
        mimetype=source_mime_type,  # Source format drives Drive's auto-conversion
        resumable=_use_resumable_upload(upload_size),
        chunksize=UPLOAD_CHUNK_SIZE_BYTES,
    )
    return media, source_mime_type, remote_file_data


CONTENT_UPDATE_MODES = ("replace", "append", "prepend")
_CONTENT_UPDATE_LOCKS: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


def _get_content_update_lock(file_id: str) -> asyncio.Lock:
    """Return a cached lock for updates within one event loop and process.

    This does not coordinate updates across processes, replicas, or other Drive clients.
    """
    lock = _CONTENT_UPDATE_LOCKS.get(file_id)
    if lock is None:
        lock = asyncio.Lock()
        _CONTENT_UPDATE_LOCKS[file_id] = lock
    return lock


def _splice_content(existing: str, addition: str, mode: str) -> str:
    """Join new text onto existing text, guaranteeing a newline at the seam."""
    head, tail = (existing, addition) if mode == "append" else (addition, existing)
    separator = (
        "\n" if head and not head.endswith("\n") and not tail.startswith("\n") else ""
    )
    return f"{head}{separator}{tail}"


# Organizer lookups share a Google API service; its HTTP transport is not thread-safe.
SHARED_DRIVE_ORGANIZER_CONCURRENCY_LIMIT = 1


async def _list_shared_drives_impl(
    service,
    user_google_email: str,
    page_size: int = 100,
    page_token: Optional[str] = None,
    query: Optional[str] = None,
    include_organizers: bool = False,
) -> str:
    """List shared drives available to the authenticated user."""
    logger.info(
        f"[list_shared_drives] Invoked. Email: '{user_google_email}', page_size: {page_size}, "
        f"include_organizers: {include_organizers}"
    )

    list_params: Dict[str, Any] = {
        "pageSize": min(max(page_size, 1), 100),
        "fields": (
            "drives(id, name, createdTime, hidden, "
            "restrictions, capabilities(canManageMembers, canEdit)), "
            "nextPageToken"
        ),
    }
    if page_token:
        list_params["pageToken"] = page_token
    if query:
        list_params["q"] = query

    results = await asyncio.to_thread(service.drives().list(**list_params).execute)
    drives = results.get("drives", [])
    if not drives:
        return f"No shared drives found for {user_google_email}."

    if include_organizers:

        async def _fetch_organizers(d: Dict[str, Any]) -> None:
            try:
                permissions = []
                next_permission_page_token = None

                while True:
                    list_kwargs: Dict[str, Any] = {
                        "fileId": d["id"],
                        "supportsAllDrives": True,
                        "useDomainAdminAccess": False,
                        "fields": (
                            "nextPageToken, "
                            "permissions(emailAddress, displayName, role, type, domain)"
                        ),
                        "pageSize": 100,
                    }
                    if next_permission_page_token:
                        list_kwargs["pageToken"] = next_permission_page_token

                    perms = await asyncio.to_thread(
                        service.permissions().list(**list_kwargs).execute
                    )
                    permissions.extend(perms.get("permissions", []))

                    next_permission_page_token = perms.get("nextPageToken")
                    if not next_permission_page_token:
                        break

                d["_organizers"] = [
                    p for p in permissions if p.get("role") == "organizer"
                ]
            except HttpError as e:
                status = getattr(e, "status_code", None)
                if status is None and getattr(e, "resp", None) is not None:
                    status = getattr(e.resp, "status", None)
                reason = getattr(e, "reason", None) or str(e)
                d["_organizers_error"] = f"{status or 'unknown'}: {reason}"

        organizer_fetch_sem = asyncio.Semaphore(
            SHARED_DRIVE_ORGANIZER_CONCURRENCY_LIMIT
        )

        async def _bounded_fetch_organizers(d: Dict[str, Any]) -> None:
            async with organizer_fetch_sem:
                await _fetch_organizers(d)

        await asyncio.gather(*[_bounded_fetch_organizers(d) for d in drives])

    next_token = results.get("nextPageToken")
    parts = [f"Found {len(drives)} shared drives for {user_google_email}:"]
    for d in drives:
        caps = d.get("capabilities") or {}
        rest = d.get("restrictions") or {}
        cap_flags = ", ".join(k for k, v in caps.items() if v) or "none"
        rest_flags = ", ".join(k for k, v in rest.items() if v) or "none"
        hidden = " [hidden]" if d.get("hidden") else ""
        parts.append(
            f'- Name: "{d["name"]}" (ID: {d["id"]}, Created: {d.get("createdTime", "N/A")}){hidden} '
            f"Capabilities: {cap_flags}; Restrictions: {rest_flags}"
        )
        if include_organizers:
            err = d.get("_organizers_error")
            if err:
                parts.append(f"  Organizers: <error: {err}>")
            else:
                organizers = d.get("_organizers", [])
                if not organizers:
                    parts.append("  Organizers: <none returned>")
                else:
                    for o in organizers:
                        identifier = (
                            o.get("emailAddress")
                            or o.get("domain")
                            or o.get("displayName")
                            or o.get("type", "?")
                        )
                        display = o.get("displayName")
                        kind = o.get("type", "?")
                        suffix = (
                            f' ("{display}")'
                            if display and display != identifier
                            else ""
                        )
                        parts.append(f"  Organizer ({kind}): {identifier}{suffix}")
    if next_token:
        parts.append(f"nextPageToken: {next_token}")
    return "\n".join(parts)


async def _import_with_conversion(
    service,
    *,
    tool_name: str,
    target_label: str,
    id_label: str,
    target_mime_type: str,
    format_map: Dict[str, str],
    user_google_email: str,
    file_name: str,
    content: Optional[str],
    file_path: Optional[str],
    file_url: Optional[str],
    source_format: Optional[str],
    folder_id: str,
    base64_content: Optional[str],
    base64_sha256: Optional[str],
    inline_params: tuple[str, ...] = ("content", "base64_content"),
    return_upload_url: bool = False,
) -> str:
    """
    Shared implementation for the import_to_google_* tools.

    Uploads source bytes (from content, a local file, or a remote URL) as media
    while ``body.mimeType`` is the Google Apps ``target_mime_type``, letting Drive
    auto-convert the Office/OpenDocument/text source into native Google format.

    Args:
        tool_name: Logging prefix and the tool's registered name (for messages).
        target_label: Human-readable destination name (e.g. "Google Doc").
        id_label: Label for the created file's ID in the confirmation message.
        target_mime_type: The ``application/vnd.google-apps.*`` destination type.
        format_map: Extension -> source MIME type allowlist for this destination.
        inline_params: The inline-source parameters the calling tool exposes
            (Slides takes binary formats only, so it has no ``content``), so the
            return_upload_url refusal names only routes that exist on that tool.
        return_upload_url: Open a resumable upload session and return its URL
            instead of uploading a source from this server.
    """
    logger.info(
        f"[{tool_name}] Invoked. Email: '{user_google_email}', "
        f"file_name_len={len(file_name) if file_name else 0}, "
        f"Source Format: '{source_format}', Folder ID: '{folder_id}'"
    )
    logger.debug(f"[{tool_name}] File Name: '{file_name}'")

    if return_upload_url:
        if local_file_access_enabled():
            raise upload_url_not_offered_error(("file_path", *inline_params))
        reject_sources_with_upload_url(
            "uploads the source",
            content=content,
            file_path=file_path,
            file_url=file_url,
            base64_content=base64_content,
            base64_sha256=base64_sha256,
        )
        supported = ", ".join(ext.lstrip(".") for ext in format_map)
        if not source_format:
            raise ValueError(
                "source_format is required with return_upload_url so the upload's "
                f"Content-Type is known (one of: {supported})."
            )
        source_mime_type = format_map.get(f".{source_format.lower().lstrip('.')}")
        if source_mime_type is None:
            raise ValueError(
                f"Unsupported source_format: '{source_format}'. Supported: {supported}."
            )
        doc_name = Path(file_name).stem if Path(file_name).suffix else file_name
        # body.mimeType is the Google Apps target; the PUT carries the source type,
        # so Drive converts exactly as it does for an inline import.
        upload_url = await initiate_resumable_upload_session(
            service,
            upload_mime_type=source_mime_type,
            file_metadata={
                "name": doc_name,
                "parents": [await resolve_folder_id(service, folder_id)],
                "mimeType": target_mime_type,
            },
        )
        logger.info(f"[{tool_name}] Returned resumable upload URL.")
        return resumable_upload_result(
            f"Resumable upload session created to import '{doc_name}' as {target_label} "
            f"({source_mime_type} → {target_mime_type}, folder '{folder_id}', "
            f"for {user_google_email}).",
            upload_url,
            source_mime_type,
        )

    media, source_mime_type, remote_file_data = await _resolve_import_media(
        tool_name=tool_name,
        file_name=file_name,
        content=content,
        file_path=file_path,
        file_url=file_url,
        source_format=source_format,
        base64_content=base64_content,
        base64_sha256=base64_sha256,
        format_map=format_map,
    )

    # Clean up file name (remove extension since it becomes a Google Apps file)
    doc_name = Path(file_name).stem if Path(file_name).suffix else file_name

    # Resolve folder
    resolved_folder_id = await resolve_folder_id(service, folder_id)

    # File metadata - destination is the Google Apps target format
    file_metadata = {
        "name": doc_name,
        "parents": [resolved_folder_id],
        "mimeType": target_mime_type,  # Target format = Google Apps type
    }

    # Upload with conversion
    logger.info(
        f"[{tool_name}] Uploading to Google Drive with conversion: "
        f"{source_mime_type} → {target_mime_type}"
    )
    try:
        created_file = await asyncio.to_thread(
            service.files()
            .create(
                body=file_metadata,
                media_body=media,
                fields="id, name, webViewLink, mimeType",
                supportsAllDrives=True,
            )
            .execute,
            num_retries=GOOGLE_API_WRITE_RETRIES,
        )
    finally:
        if remote_file_data is not None:
            remote_file_data.close()

    result_mime = created_file.get("mimeType", "unknown")
    if result_mime != target_mime_type:
        logger.warning(
            f"[{tool_name}] Conversion may have failed. "
            f"Expected {target_mime_type}, got {result_mime}"
        )

    link = created_file.get("webViewLink", "No link available")
    doc_id = created_file.get("id", "N/A")

    confirmation = (
        f"✅ Successfully imported '{doc_name}' as {target_label}\n"
        f"   {id_label}: {doc_id}\n"
        f"   Source format: {source_mime_type}\n"
        f"   Folder: {folder_id}\n"
        f"   Link: {link}"
    )

    logger.info(f"[{tool_name}] Success. Link: {link}")
    return confirmation


# ---------------------------------------------------------------------------
# Drive file management (trash / move / rename / star / download restriction)
# ---------------------------------------------------------------------------

FILE_MANAGEMENT_ACTIONS = (
    "trash",
    "untrash",
    "delete",
    "empty_trash",
    "move",
    "rename",
    "star",
    "unstar",
    "set_description",
    "restrict_download",
    "allow_download",
)

# Simple boolean / string field toggles handled by a single files().update call.
_FILE_FIELD_ACTIONS: Dict[str, Tuple[str, Any]] = {
    "trash": ("trashed", True),
    "untrash": ("trashed", False),
    "star": ("starred", True),
    "unstar": ("starred", False),
    "restrict_download": ("copyRequiresWriterPermission", True),
    "allow_download": ("copyRequiresWriterPermission", False),
}

_FILE_UPDATE_FIELDS = (
    "id, name, mimeType, parents, trashed, starred, description, "
    "copyRequiresWriterPermission, webViewLink, driveId"
)


def _validate_action(
    tool_name: str, action: str, valid_actions: Tuple[str, ...]
) -> str:
    """Normalise *action*, reject unknown ones and ones denied by permissions."""
    normalized = (action or "").strip().lower()
    if normalized not in valid_actions:
        raise UserInputError(
            f"Invalid action '{action}'. Must be one of: {', '.join(valid_actions)}"
        )
    if is_action_denied("drive", normalized):
        raise UserInputError(
            f"The '{normalized}' action is not allowed under the current permission level."
        )
    return normalized


def _format_bytes(size: Any) -> str:
    """Render a byte count as a human readable string (keeps the raw count)."""
    try:
        count = int(size)
    except (TypeError, ValueError):
        return "unknown"
    value = float(count)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{count:,} B"
            return f"{value:,.1f} {unit} ({count:,} bytes)"
        value /= 1024
    return f"{count:,} B"


def _file_summary_lines(file_metadata: Dict[str, Any]) -> List[str]:
    """Standard metadata lines for a Drive file response."""
    lines = [
        f"Name: {file_metadata.get('name', 'Unknown')}",
        f"ID: {file_metadata.get('id', 'Unknown')}",
    ]
    if file_metadata.get("mimeType"):
        lines.append(f"Type: {file_metadata['mimeType']}")
    parents = file_metadata.get("parents") or []
    if parents:
        lines.append(f"Parents: {', '.join(parents)}")
    if file_metadata.get("driveId"):
        lines.append(f"Shared drive: {file_metadata['driveId']}")
    flags = []
    if file_metadata.get("trashed"):
        flags.append("trashed")
    if file_metadata.get("starred"):
        flags.append("starred")
    if file_metadata.get("copyRequiresWriterPermission"):
        flags.append("download restricted for readers/commenters")
    if flags:
        lines.append(f"Flags: {', '.join(flags)}")
    if file_metadata.get("description"):
        lines.append(f"Description: {file_metadata['description']}")
    if file_metadata.get("webViewLink"):
        lines.append(f"Link: {file_metadata['webViewLink']}")
    return lines


async def _update_file_metadata(
    service, file_id: str, body: Dict[str, Any], **extra_params: Any
) -> Dict[str, Any]:
    """Run files().update with shared-drive support and a stable field set."""
    params: Dict[str, Any] = {
        "fileId": file_id,
        "body": body,
        "supportsAllDrives": True,
        "fields": _FILE_UPDATE_FIELDS,
    }
    params.update(extra_params)
    return await asyncio.to_thread(service.files().update(**params).execute)


async def _manage_drive_file_impl(
    service,
    user_google_email: str,
    action: str,
    file_id: Optional[str] = None,
    new_name: Optional[str] = None,
    new_parent_folder_id: Optional[str] = None,
    description: Optional[str] = None,
    drive_id: Optional[str] = None,
) -> str:
    """Apply a file-management action to a Drive file, folder or shortcut.

    Shortcuts are NOT followed: trashing, renaming or moving a shortcut acts on
    the shortcut itself, which is what a user pointing at it expects.
    """
    normalized = _validate_action("manage_drive_file", action, FILE_MANAGEMENT_ACTIONS)
    logger.info(
        f"[manage_drive_file] Invoked. Email: '{user_google_email}', "
        f"Action: '{normalized}', File ID: '{file_id}'"
    )

    if normalized == "empty_trash":
        params: Dict[str, Any] = {}
        if drive_id:
            params["driveId"] = drive_id
        await asyncio.to_thread(service.files().emptyTrash(**params).execute)
        scope = f"shared drive {drive_id}" if drive_id else "My Drive"
        return f"Trash emptied for {scope} ({user_google_email}). Items were permanently deleted."

    if not file_id:
        raise UserInputError(f"file_id is required for the '{normalized}' action.")

    if normalized == "delete":
        await asyncio.to_thread(
            service.files().delete(fileId=file_id, supportsAllDrives=True).execute
        )
        return (
            f"File {file_id} permanently deleted (bypassed the trash). "
            "This cannot be undone."
        )

    if normalized in _FILE_FIELD_ACTIONS:
        field, value = _FILE_FIELD_ACTIONS[normalized]
        updated = await _update_file_metadata(service, file_id, {field: value})
        verb = {
            "trash": "moved to trash",
            "untrash": "restored from trash",
            "star": "starred",
            "unstar": "unstarred",
            "restrict_download": "download/copy/print restricted for readers and commenters",
            "allow_download": "download/copy/print allowed for readers and commenters",
        }[normalized]
        return "\n".join([f"File {verb}.", *_file_summary_lines(updated)])

    if normalized == "rename":
        if not new_name or not new_name.strip():
            raise UserInputError("new_name is required for the 'rename' action.")
        updated = await _update_file_metadata(
            service, file_id, {"name": new_name.strip()}
        )
        return "\n".join(["File renamed.", *_file_summary_lines(updated)])

    if normalized == "set_description":
        if description is None:
            raise UserInputError(
                "description is required for the 'set_description' action "
                "(pass an empty string to clear it)."
            )
        updated = await _update_file_metadata(
            service, file_id, {"description": description}
        )
        return "\n".join(["Description updated.", *_file_summary_lines(updated)])

    # move
    if not new_parent_folder_id:
        raise UserInputError("new_parent_folder_id is required for the 'move' action.")
    _resolved_id, current = await resolve_drive_item(
        service, file_id, extra_fields="name", follow_shortcuts=False
    )
    target_folder_id = await resolve_folder_id(service, new_parent_folder_id)
    current_parents = current.get("parents") or []
    if current_parents == [target_folder_id]:
        return "\n".join(
            [
                f"File is already in folder {target_folder_id}; nothing to do.",
                *_file_summary_lines({**current, "id": file_id}),
            ]
        )
    extra: Dict[str, Any] = {"addParents": target_folder_id}
    if current_parents:
        extra["removeParents"] = ",".join(current_parents)
    updated = await _update_file_metadata(service, file_id, {}, **extra)
    previous = ", ".join(current_parents) if current_parents else "(none)"
    return "\n".join(
        [
            f"File moved from {previous} to {target_folder_id}.",
            *_file_summary_lines(updated),
        ]
    )


# ---------------------------------------------------------------------------
# About (quota, user, limits)
# ---------------------------------------------------------------------------

ABOUT_FIELDS = (
    "user(displayName, emailAddress, permissionId), "
    "storageQuota(limit, usage, usageInDrive, usageInDriveTrash), "
    "maxUploadSize, canCreateDrives, importFormats, exportFormats"
)


async def _get_drive_about_impl(service, user_google_email: str) -> str:
    """Summarise the Drive account: quota, identity, upload cap and conversions."""
    logger.info(f"[get_drive_about] Invoked. Email: '{user_google_email}'")
    about = await asyncio.to_thread(service.about().get(fields=ABOUT_FIELDS).execute)

    user = about.get("user") or {}
    quota = about.get("storageQuota") or {}
    limit = quota.get("limit")
    usage = quota.get("usage")
    lines = [
        f"Drive account for {user_google_email}:",
        f"User: {user.get('displayName', 'Unknown')} <{user.get('emailAddress', 'unknown')}>"
        + (
            f" (permission ID: {user['permissionId']})"
            if user.get("permissionId")
            else ""
        ),
        "",
        "Storage quota:",
        f"  Total used: {_format_bytes(usage)}",
        f"  Limit: {_format_bytes(limit) if limit is not None else 'unlimited'}",
    ]
    if limit is not None and usage is not None:
        try:
            pct = (int(usage) / int(limit)) * 100 if int(limit) else 0.0
            lines.append(f"  Used: {pct:.1f}%")
        except (TypeError, ValueError, ZeroDivisionError):
            pass
    lines.append(f"  Used in Drive: {_format_bytes(quota.get('usageInDrive'))}")
    lines.append(f"  Used in trash: {_format_bytes(quota.get('usageInDriveTrash'))}")
    lines.append("")
    lines.append(f"Max upload size: {_format_bytes(about.get('maxUploadSize'))}")
    lines.append(
        f"Can create shared drives: {'yes' if about.get('canCreateDrives') else 'no'}"
    )
    import_formats = about.get("importFormats") or {}
    export_formats = about.get("exportFormats") or {}
    lines.append(
        f"Import conversions: {len(import_formats)} source types "
        "(e.g. Office/CSV/text -> Google Docs/Sheets/Slides)"
    )
    lines.append(
        f"Export conversions: {len(export_formats)} Google types "
        "(Docs/Sheets/Slides/Drawings -> PDF, Office, text, ...)"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Revisions
# ---------------------------------------------------------------------------

REVISION_ACTIONS = ("list", "get", "keep_forever", "unkeep", "delete", "download")

REVISION_FIELDS = (
    "id, modifiedTime, mimeType, size, md5Checksum, keepForever, "
    "originalFilename, published, publishedLink, exportLinks, "
    "lastModifyingUser(displayName, emailAddress)"
)


def _revision_user(revision: Dict[str, Any]) -> str:
    user = revision.get("lastModifyingUser") or {}
    name = user.get("displayName")
    email = user.get("emailAddress")
    if name and email:
        return f"{name} <{email}>"
    return name or email or "Unknown"


def _format_revision(revision: Dict[str, Any], indent: str = "") -> List[str]:
    lines = [
        f"{indent}Revision ID: {revision.get('id', 'Unknown')}",
        f"{indent}  Modified: {revision.get('modifiedTime', 'Unknown')} by {_revision_user(revision)}",
    ]
    if revision.get("size") is not None:
        lines.append(f"{indent}  Size: {_format_bytes(revision['size'])}")
    if revision.get("mimeType"):
        lines.append(f"{indent}  Type: {revision['mimeType']}")
    if revision.get("originalFilename"):
        lines.append(f"{indent}  Original filename: {revision['originalFilename']}")
    if revision.get("md5Checksum"):
        lines.append(f"{indent}  MD5: {revision['md5Checksum']}")
    lines.append(
        f"{indent}  Keep forever: {'yes' if revision.get('keepForever') else 'no'}"
    )
    if revision.get("published"):
        lines.append(
            f"{indent}  Published: yes"
            + (
                f" ({revision['publishedLink']})"
                if revision.get("publishedLink")
                else ""
            )
        )
    export_links = revision.get("exportLinks") or {}
    if export_links:
        lines.append(f"{indent}  Export formats: {', '.join(sorted(export_links))}")
    return lines


def _render_revision_content(content: bytes, mime_type: str) -> str:
    """Turn downloaded revision bytes into something a text channel can carry."""
    if mime_type in IMAGE_MIME_TYPES:
        return encode_image_content(content, mime_type)
    if mime_type == "application/pdf":
        text = extract_pdf_text(content)
        if text:
            return text
        return f"[Could not extract text from PDF ({len(content)} bytes); it may be scanned/image-only.]"
    if mime_type.startswith("application/vnd.openxmlformats-officedocument."):
        try:
            text = extract_office_xml_text(content, mime_type)
        except OfficeXmlExtractionError as e:
            text = f"[Could not read '{mime_type}' revision: {e}]"
        if text:
            return text
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        digest = hashlib.md5(content).hexdigest()
        return (
            f"[Binary content for mimeType '{mime_type}' - {len(content)} bytes, "
            f"md5 {digest}. Use the Drive UI or exportLinks to retrieve it as a file.]"
        )


async def _manage_drive_revisions_impl(
    service,
    user_google_email: str,
    file_id: str,
    action: str,
    revision_id: Optional[str] = None,
    page_size: int = 100,
    page_token: Optional[str] = None,
) -> str:
    """List, inspect, pin, delete or download the revisions of a Drive file."""
    normalized = _validate_action("manage_drive_revisions", action, REVISION_ACTIONS)
    logger.info(
        f"[manage_drive_revisions] Invoked. Email: '{user_google_email}', "
        f"File ID: '{file_id}', Action: '{normalized}', Revision: '{revision_id}'"
    )
    if not file_id:
        raise UserInputError("file_id is required.")

    if normalized == "list":
        params: Dict[str, Any] = {
            "fileId": file_id,
            "pageSize": min(max(int(page_size or 100), 1), 1000),
            "fields": f"nextPageToken, revisions({REVISION_FIELDS})",
        }
        if page_token:
            params["pageToken"] = page_token
        response = await asyncio.to_thread(service.revisions().list(**params).execute)
        revisions = response.get("revisions") or []
        if not revisions:
            return (
                f"No revisions found for file {file_id}. Google-native files only "
                "expose revisions to users with edit access; binary files keep up to "
                "100 revisions for 30 days unless keep_forever is set."
            )
        lines = [f"Found {len(revisions)} revisions for file {file_id}:", ""]
        for revision in revisions:
            lines.extend(_format_revision(revision))
            lines.append("")
        next_token = response.get("nextPageToken")
        if next_token:
            lines.append(f"nextPageToken: {next_token}")
        return "\n".join(lines).rstrip()

    if not revision_id:
        raise UserInputError(f"revision_id is required for the '{normalized}' action.")

    if normalized == "get":
        revision = await asyncio.to_thread(
            service.revisions()
            .get(fileId=file_id, revisionId=revision_id, fields=REVISION_FIELDS)
            .execute
        )
        return "\n".join([f"Revision of file {file_id}:", *_format_revision(revision)])

    if normalized in ("keep_forever", "unkeep"):
        keep = normalized == "keep_forever"
        revision = await asyncio.to_thread(
            service.revisions()
            .update(
                fileId=file_id,
                revisionId=revision_id,
                body={"keepForever": keep},
                fields=REVISION_FIELDS,
            )
            .execute
        )
        status = (
            "will be kept forever"
            if keep
            else "is no longer pinned (subject to Drive's automatic purge)"
        )
        return "\n".join(
            [f"Revision {revision_id} {status}.", *_format_revision(revision)]
        )

    if normalized == "delete":
        await asyncio.to_thread(
            service.revisions().delete(fileId=file_id, revisionId=revision_id).execute
        )
        return (
            f"Revision {revision_id} of file {file_id} permanently deleted. "
            "The head revision and Google-native file revisions cannot be deleted."
        )

    # download
    revision = await asyncio.to_thread(
        service.revisions()
        .get(fileId=file_id, revisionId=revision_id, fields=REVISION_FIELDS)
        .execute
    )
    mime_type = revision.get("mimeType", "") or ""
    header = [
        f"Revision {revision_id} of file {file_id}",
        f"Modified: {revision.get('modifiedTime', 'Unknown')} by {_revision_user(revision)}",
        f"Type: {mime_type or 'unknown'}",
    ]
    if mime_type.startswith(GOOGLE_APPS_MIME_PREFIX):
        export_links = revision.get("exportLinks") or {}
        lines = header + [
            "",
            "Google-native revisions have no raw bytes to download via the Drive API "
            "(revisions.get_media is only for binary files).",
        ]
        if export_links:
            lines.append(
                "Export this revision with an authenticated GET to one of these links:"
            )
            for export_mime, link in sorted(export_links.items()):
                lines.append(f"  - {export_mime}: {link}")
        else:
            lines.append(
                "No exportLinks were returned for this revision; open the file's "
                "version history in the Google editor instead."
            )
        return "\n".join(lines)

    label = revision.get("originalFilename") or f"revision {revision_id}"
    try:
        ensure_within_file_size_limit(
            revision.get("size"), file_name=label, file_id=file_id, kind="revision"
        )
        content = await download_media_bytes(
            service.revisions().get_media(fileId=file_id, revisionId=revision_id),
            file_name=label,
            file_id=file_id,
            kind="revision",
        )
    except FileTooLargeError as e:
        return str(e)
    body = await asyncio.to_thread(_render_revision_content, content, mime_type)
    header.append(f"Size: {_format_bytes(len(content))}")
    return "\n".join(header) + "\n\n--- CONTENT ---\n" + body


# ---------------------------------------------------------------------------
# Changes feed
# ---------------------------------------------------------------------------

CHANGES_ACTIONS = ("get_start_token", "list")

CHANGES_FIELDS = (
    "nextPageToken, newStartPageToken, "
    "changes(changeType, fileId, driveId, removed, time, "
    "file(id, name, mimeType, trashed, parents), drive(id, name))"
)


def _format_change(change: Dict[str, Any]) -> str:
    change_type = change.get("changeType") or change.get("type") or "file"
    removed = bool(change.get("removed"))
    when = change.get("time", "unknown time")
    if change_type == "drive":
        drive = change.get("drive") or {}
        drive_id = change.get("driveId") or drive.get("id") or "unknown"
        if removed:
            return (
                f"- [{when}] shared drive {drive_id} removed (deleted or access lost)"
            )
        return f'- [{when}] shared drive "{drive.get("name", "Unknown")}" (ID: {drive_id}) changed'
    file_id = change.get("fileId", "unknown")
    if removed:
        return f"- [{when}] file {file_id} removed (deleted permanently or access lost)"
    file_info = change.get("file") or {}
    flags = " [trashed]" if file_info.get("trashed") else ""
    drive_suffix = (
        f", shared drive {change['driveId']}" if change.get("driveId") else ""
    )
    return (
        f'- [{when}] "{file_info.get("name", "Unknown")}" (ID: {file_id}, '
        f"Type: {file_info.get('mimeType', 'unknown')}{drive_suffix}){flags}"
    )


async def _list_drive_changes_impl(
    service,
    user_google_email: str,
    action: str,
    page_token: Optional[str] = None,
    drive_id: Optional[str] = None,
    page_size: int = 100,
    include_removed: bool = True,
    restrict_to_my_drive: bool = False,
) -> str:
    """Fetch a start token or one page of changes from the Drive changes feed."""
    normalized = _validate_action("list_drive_changes", action, CHANGES_ACTIONS)
    logger.info(
        f"[list_drive_changes] Invoked. Email: '{user_google_email}', "
        f"Action: '{normalized}', Drive ID: '{drive_id}'"
    )

    if normalized == "get_start_token":
        params: Dict[str, Any] = {"supportsAllDrives": True}
        if drive_id:
            params["driveId"] = drive_id
        response = await asyncio.to_thread(
            service.changes().getStartPageToken(**params).execute
        )
        token = response.get("startPageToken", "")
        scope = f"shared drive {drive_id}" if drive_id else "all drives"
        return (
            f"Start page token for {scope}: {token}\n"
            "Store it and pass it as page_token with action='list' to receive "
            "every change made from now on."
        )

    if not page_token:
        raise UserInputError(
            "page_token is required for the 'list' action; obtain one with "
            "action='get_start_token' or from a previous 'list' response."
        )
    params = {
        "pageToken": page_token,
        "pageSize": min(max(int(page_size or 100), 1), 1000),
        "includeRemoved": bool(include_removed),
        "supportsAllDrives": True,
        "includeItemsFromAllDrives": True,
        "fields": CHANGES_FIELDS,
    }
    if drive_id:
        params["driveId"] = drive_id
    if restrict_to_my_drive:
        params["restrictToMyDrive"] = True
    response = await asyncio.to_thread(service.changes().list(**params).execute)
    changes = response.get("changes") or []
    lines = [f"{len(changes)} change(s) since token {page_token}:"]
    lines.extend(_format_change(c) for c in changes)
    next_token = response.get("nextPageToken")
    new_start = response.get("newStartPageToken")
    lines.append("")
    if next_token:
        lines.append(
            f"More changes available. nextPageToken: {next_token} "
            "(pass it as page_token to continue)."
        )
    if new_start:
        lines.append(
            f"End of changes reached. newStartPageToken: {new_start} "
            "(store it for the next poll)."
        )
    return "\n".join(lines).rstrip()


# ---------------------------------------------------------------------------
# Shared drives
# ---------------------------------------------------------------------------

SHARED_DRIVE_ACTIONS = ("list", "get", "create", "update", "delete", "hide", "unhide")

SHARED_DRIVE_RESTRICTION_KEYS = frozenset(
    {
        "adminManagedRestrictions",
        "copyRequiresWriterPermission",
        "domainUsersOnly",
        "driveMembersOnly",
        "sharingFoldersRequiresOrganizerPermission",
        "downloadRestriction",
    }
)

SHARED_DRIVE_FIELDS = (
    "id, name, createdTime, hidden, colorRgb, themeId, orgUnitId, restrictions, "
    "capabilities(canAddChildren, canChangeDriveMembersOnlyRestriction, "
    "canDeleteDrive, canEdit, canManageMembers, canRenameDrive, canShare, "
    "canTrashChildren)"
)


def _format_shared_drive(drive: Dict[str, Any]) -> List[str]:
    lines = [
        f"Name: {drive.get('name', 'Unknown')}",
        f"ID: {drive.get('id', 'Unknown')}",
    ]
    if drive.get("createdTime"):
        lines.append(f"Created: {drive['createdTime']}")
    lines.append(f"Hidden: {'yes' if drive.get('hidden') else 'no'}")
    if drive.get("orgUnitId"):
        lines.append(f"Org unit: {drive['orgUnitId']}")
    restrictions = drive.get("restrictions") or {}
    active = [k for k, v in restrictions.items() if v]
    lines.append(f"Restrictions: {', '.join(sorted(active)) if active else 'none'}")
    caps = drive.get("capabilities") or {}
    active_caps = [k for k, v in caps.items() if v]
    if active_caps:
        lines.append(f"Capabilities: {', '.join(sorted(active_caps))}")
    return lines


def _validate_shared_drive_restrictions(
    restrictions: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    if not isinstance(restrictions, dict):
        raise UserInputError(
            "restrictions must be an object mapping restriction names to booleans."
        )
    unknown = set(restrictions) - SHARED_DRIVE_RESTRICTION_KEYS
    if unknown:
        raise UserInputError(
            f"Unknown restriction(s): {', '.join(sorted(unknown))}. Valid keys: "
            f"{', '.join(sorted(SHARED_DRIVE_RESTRICTION_KEYS))}"
        )
    cleaned: Dict[str, Any] = {}
    for key, value in restrictions.items():
        if key == "downloadRestriction":
            if not isinstance(value, dict):
                raise UserInputError(
                    "downloadRestriction must be an object, e.g. "
                    '{"restrictedForReaders": true, "restrictedForWriters": false}.'
                )
            cleaned[key] = value
        elif isinstance(value, bool):
            cleaned[key] = value
        else:
            raise UserInputError(f"Restriction '{key}' must be true or false.")
    return cleaned


async def _manage_shared_drive_impl(
    service,
    user_google_email: str,
    action: str,
    drive_id: Optional[str] = None,
    name: Optional[str] = None,
    restrictions: Optional[Dict[str, Any]] = None,
    page_size: int = 100,
    page_token: Optional[str] = None,
    query: Optional[str] = None,
    use_domain_admin_access: bool = False,
    allow_item_deletion: bool = False,
) -> str:
    """List, inspect, create, update, delete, hide or unhide shared drives."""
    normalized = _validate_action("manage_shared_drive", action, SHARED_DRIVE_ACTIONS)
    logger.info(
        f"[manage_shared_drive] Invoked. Email: '{user_google_email}', "
        f"Action: '{normalized}', Drive ID: '{drive_id}'"
    )

    if normalized == "list":
        return await _list_shared_drives_impl(
            service,
            user_google_email,
            page_size=page_size,
            page_token=page_token,
            query=query,
        )

    if normalized == "create":
        if not name or not name.strip():
            raise UserInputError("name is required for the 'create' action.")
        body: Dict[str, Any] = {"name": name.strip()}
        if restrictions is not None:
            body["restrictions"] = _validate_shared_drive_restrictions(restrictions)
        request_id = str(uuid.uuid4())
        drive = await asyncio.to_thread(
            service.drives()
            .create(requestId=request_id, body=body, fields=SHARED_DRIVE_FIELDS)
            .execute
        )
        return "\n".join(
            [
                f"Shared drive created (request ID {request_id}).",
                *_format_shared_drive(drive),
            ]
        )

    if not drive_id:
        raise UserInputError(f"drive_id is required for the '{normalized}' action.")

    admin: Dict[str, Any] = (
        {"useDomainAdminAccess": True} if use_domain_admin_access else {}
    )

    if normalized == "get":
        drive = await asyncio.to_thread(
            service.drives()
            .get(driveId=drive_id, fields=SHARED_DRIVE_FIELDS, **admin)
            .execute
        )
        return "\n".join(["Shared drive:", *_format_shared_drive(drive)])

    if normalized == "update":
        body = {}
        if name is not None and name.strip():
            body["name"] = name.strip()
        if restrictions is not None:
            body["restrictions"] = _validate_shared_drive_restrictions(restrictions)
        if not body:
            raise UserInputError(
                "Provide name and/or restrictions for the 'update' action."
            )
        drive = await asyncio.to_thread(
            service.drives()
            .update(driveId=drive_id, body=body, fields=SHARED_DRIVE_FIELDS, **admin)
            .execute
        )
        return "\n".join(["Shared drive updated.", *_format_shared_drive(drive)])

    if normalized == "delete":
        params: Dict[str, Any] = {"driveId": drive_id, **admin}
        if allow_item_deletion:
            params["allowItemDeletion"] = True
        await asyncio.to_thread(service.drives().delete(**params).execute)
        note = (
            " Its contents were deleted too (allow_item_deletion=True)."
            if allow_item_deletion
            else " Only empty shared drives can be deleted without allow_item_deletion."
        )
        return f"Shared drive {drive_id} permanently deleted.{note}"

    if normalized == "hide":
        drive = await asyncio.to_thread(
            service.drives().hide(driveId=drive_id, fields=SHARED_DRIVE_FIELDS).execute
        )
        return "\n".join(
            ["Shared drive hidden from the default view.", *_format_shared_drive(drive)]
        )

    drive = await asyncio.to_thread(
        service.drives().unhide(driveId=drive_id, fields=SHARED_DRIVE_FIELDS).execute
    )
    return "\n".join(
        ["Shared drive restored to the default view.", *_format_shared_drive(drive)]
    )
