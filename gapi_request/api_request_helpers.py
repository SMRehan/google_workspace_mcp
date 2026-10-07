"""
Business logic for the ``google_api_request`` tool.

Resolves a service name to the discovery client this server already knows how
to build, decides which scopes and policy (enabled services, read-only mode,
granular permissions) apply, authenticates through the same code path as the
typed tools, walks a dotted method path across ``googleapiclient`` Resource
objects, executes the request and renders the result.
"""

import asyncio
import base64
import functools
import hashlib
import inspect
import json
import logging
import re
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import auth.scopes as scopes_module
from auth.permissions import get_permissions, get_scopes_for_permission
from auth.scopes import TOOL_READONLY_SCOPES_MAP, TOOL_SCOPES_MAP
from auth.service_decorator import (
    SERVICE_CONFIGS,
    _remove_user_email_arg_from_docstring,
    _user_email_is_managed,
    require_google_service,
)
from core.file_limits import download_media_bytes
from core.utils import UserInputError

logger = logging.getLogger(__name__)

TOOL_NAME = "google_api_request"

DEFAULT_MAX_OUTPUT_CHARS = 60_000
MAX_METHOD_DEPTH = 6
MEDIA_SUFFIX = "_media"

# Tool keys (as used by --tools, TOOL_SCOPES_MAP and --permissions) whose
# discovery service name differs. Every other key is the same in both maps.
TOOL_KEY_TO_SERVICE: Dict[str, str] = {
    "contacts": "people",
    "appscript": "script",
    "search": "customsearch",
}
SERVICE_TO_TOOL_KEY: Dict[str, str] = {v: k for k, v in TOOL_KEY_TO_SERVICE.items()}

# Keyword arguments the discovery client treats specially. Uploads need a
# MediaUpload object that cannot be expressed as JSON, so they are refused.
_UPLOAD_PARAMS = frozenset({"media_body", "media_mime_type"})

_VERSION_RE = re.compile(r"v\d+[a-z0-9_]*")
_SEGMENT_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*")

_MAGIC_BYTES: Tuple[Tuple[bytes, str], ...] = (
    (b"%PDF", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF8", "image/gif"),
    (b"PK\x03\x04", "application/zip"),
    (b"{", "application/json"),
    (b"[", "application/json"),
    (b"<?xml", "application/xml"),
)


# ---------------------------------------------------------------------------
# Service resolution and policy
# ---------------------------------------------------------------------------


def accepted_service_names() -> List[str]:
    """Return every name ``resolve_service`` accepts, sorted."""
    return sorted(set(SERVICE_CONFIGS) | set(TOOL_KEY_TO_SERVICE))


def resolve_service(service: str) -> Tuple[str, str]:
    """Map a user-supplied service name to ``(service_config_key, tool_key)``.

    ``service_config_key`` indexes ``SERVICE_CONFIGS`` (the discovery API name);
    ``tool_key`` indexes ``TOOL_SCOPES_MAP`` / ``--permissions``.
    """
    name = (service or "").strip().lower()
    if name in TOOL_KEY_TO_SERVICE:
        return TOOL_KEY_TO_SERVICE[name], name
    if name in SERVICE_CONFIGS:
        return name, SERVICE_TO_TOOL_KEY.get(name, name)
    raise UserInputError(
        f"Unknown service '{service}'. Accepted names: "
        f"{', '.join(accepted_service_names())}."
    )


def resolve_version(service_key: str, version: Optional[str]) -> str:
    """Return ``version`` when given (validated), else the server default."""
    if version is None or not str(version).strip():
        return SERVICE_CONFIGS[service_key]["version"]
    candidate = str(version).strip()
    if not _VERSION_RE.fullmatch(candidate):
        raise UserInputError(
            f"Invalid API version '{version}' for {service_key}; expected something "
            f"like '{SERVICE_CONFIGS[service_key]['version']}'."
        )
    return candidate


def get_enabled_tool_keys() -> Optional[List[str]]:
    """Return the enabled tool keys set by main.py, or ``None`` when unrestricted."""
    enabled = scopes_module._ENABLED_TOOLS
    return None if enabled is None else list(enabled)


def ensure_service_enabled(tool_key: str, service_key: str) -> None:
    """Refuse services the operator did not enable for this server process."""
    enabled = get_enabled_tool_keys()
    if enabled is None or tool_key in enabled:
        return
    usable = sorted(
        k for k in enabled if k in SERVICE_CONFIGS or k in TOOL_KEY_TO_SERVICE
    )
    raise UserInputError(
        f"Service '{service_key}' is not enabled on this server, so "
        f"{TOOL_NAME} cannot reach it. Enabled services: "
        f"{', '.join(usable) if usable else 'none'}. Start the server with "
        f"--tools {tool_key} (or a tier/permission that includes it) to allow it."
    )


def is_service_read_only(tool_key: str) -> bool:
    """True under global ``--read-only`` or a ``<service>:readonly`` permission."""
    if scopes_module.is_read_only_mode():
        return True
    permissions = get_permissions() or {}
    return permissions.get(tool_key) == "readonly"


def required_scopes_for(tool_key: str) -> List[str]:
    """Scopes this server itself requested for ``tool_key`` at auth time.

    Mirrors ``auth.scopes.get_scopes_for_tools``: granular permissions win,
    then read-only mode, then the full map. Credentials issued through this
    server therefore always satisfy the result; nothing wider is ever asked.
    """
    permissions = get_permissions()
    if permissions is not None and tool_key in permissions:
        return get_scopes_for_permission(tool_key, permissions[tool_key])
    if scopes_module.is_read_only_mode():
        return list(TOOL_READONLY_SCOPES_MAP.get(tool_key, []))
    return list(TOOL_SCOPES_MAP.get(tool_key, []))


# ---------------------------------------------------------------------------
# Authentication (reuses the typed tools' decorator at call time)
# ---------------------------------------------------------------------------


async def call_with_service(
    service_key: str,
    version: str,
    scopes: List[str],
    user_google_email: Optional[str],
    callback: Callable[[Any, str], Awaitable[str]],
) -> str:
    """Authenticate exactly as ``@require_google_service`` would, then run ``callback``.

    The decorator needs its service type at decoration time, and this tool only
    knows it at call time, so the decoration happens here per call. Everything
    else (OAuth 2.0 vs 2.1 detection, trusted-gateway and service-account modes,
    scope errors, token-refresh messages, connection recycling) is the
    decorator's own code, not a copy of it.
    """

    @require_google_service(service_key, scopes, version)
    async def google_api_request(service: Any, user_google_email: str) -> str:
        return await callback(service, user_google_email)

    return await google_api_request(user_google_email=user_google_email)


def accept_managed_user_email(func: Callable) -> Callable:
    """Hide ``user_google_email`` from the schema when the server resolves it.

    ``@require_google_service`` drops the parameter in OAuth 2.1 / trusted-
    gateway mode because the verified identity supplies it. This tool is not
    decorated at import time, so it applies the same treatment itself. Apply
    directly under ``@server.tool``.
    """
    if not _user_email_is_managed():
        return func

    sig = inspect.signature(func)

    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        kwargs.setdefault("user_google_email", None)
        return await func(*args, **kwargs)

    wrapper.__signature__ = sig.replace(
        parameters=[p for p in sig.parameters.values() if p.name != "user_google_email"]
    )
    if func.__doc__:
        wrapper.__doc__ = _remove_user_email_arg_from_docstring(func.__doc__)
    return wrapper


# ---------------------------------------------------------------------------
# Method walking
# ---------------------------------------------------------------------------


def _describe(resource: Any) -> Tuple[Optional[Dict], Optional[Dict]]:
    """Return ``(resources, methods)`` from a Resource's discovery description.

    Both are ``None`` when the object carries no dict description, in which
    case callers fall back to plain attribute access without validation.
    """
    desc = getattr(resource, "_resourceDesc", None)
    if not isinstance(desc, dict):
        return None, None
    return dict(desc.get("resources") or {}), dict(desc.get("methods") or {})


def available_names(resource: Any) -> Tuple[List[str], List[str]]:
    """List the collections and callable methods one level below ``resource``."""
    resources, methods = _describe(resource)
    if resources is None:
        names = sorted(
            n
            for n in dir(resource)
            if not n.startswith("_")
            and not n.endswith("_next")
            and callable(getattr(resource, n, None))
        )
        return [], names
    method_names: List[str] = []
    for name, desc in methods.items():
        if hasattr(resource, name):
            method_names.append(name)
        if desc.get("supportsMediaDownload") and hasattr(resource, name + MEDIA_SUFFIX):
            method_names.append(name + MEDIA_SUFFIX)
    return sorted(resources), sorted(method_names)


def _format_available(resource: Any, path_so_far: str) -> str:
    collections, methods = available_names(resource)
    where = f"under '{path_so_far}'" if path_so_far else "at the service root"
    parts = []
    if collections:
        parts.append(f"collections: {', '.join(collections)}")
    if methods:
        parts.append(f"methods: {', '.join(methods)}")
    return f"Available {where} -> " + ("; ".join(parts) if parts else "nothing")


def split_method_path(method: str) -> List[str]:
    """Validate and split ``'a.b.c'`` into segments safe for ``getattr``."""
    raw = (method or "").strip()
    if not raw:
        raise UserInputError(
            "'method' is required, e.g. 'events.list' or 'users.messages.get'."
        )
    segments = raw.split(".")
    if len(segments) > MAX_METHOD_DEPTH:
        raise UserInputError(
            f"'method' has {len(segments)} segments; at most {MAX_METHOD_DEPTH} are allowed."
        )
    for seg in segments:
        if not _SEGMENT_RE.fullmatch(seg):
            raise UserInputError(
                f"Invalid method segment '{seg}' in '{method}'. Segments are "
                "dotted collection and method names such as 'spreadsheets.values.get'."
            )
    return segments


def walk_method(
    service: Any, method: str
) -> Tuple[Callable[..., Any], Optional[Dict[str, Any]], bool]:
    """Resolve a dotted method path on a discovery Resource.

    Returns ``(bound_method, method_description_or_None, is_media)``. Raises
    ``UserInputError`` naming what is available at the level that failed.
    """
    segments = split_method_path(method)
    resource = service
    walked: List[str] = []

    for seg in segments[:-1]:
        resources, _methods = _describe(resource)
        if resources is not None and seg not in resources:
            raise UserInputError(
                f"'{seg}' is not a collection in '{method}'. "
                + _format_available(resource, ".".join(walked))
            )
        factory = getattr(resource, seg, None)
        if not callable(factory):
            raise UserInputError(
                f"'{seg}' is not a collection in '{method}'. "
                + _format_available(resource, ".".join(walked))
            )
        resource = factory()
        walked.append(seg)

    last = segments[-1]
    is_media = last.endswith(MEDIA_SUFFIX)
    base_name = last[: -len(MEDIA_SUFFIX)] if is_media else last

    _resources, methods = _describe(resource)
    desc: Optional[Dict[str, Any]] = None
    if methods is not None:
        desc = methods.get(base_name)
        if desc is None or (is_media and not desc.get("supportsMediaDownload")):
            raise UserInputError(
                f"'{last}' is not a method in '{method}'. "
                + _format_available(resource, ".".join(walked))
            )

    bound = getattr(resource, last, None)
    if not callable(bound):
        raise UserInputError(
            f"'{last}' is not a method in '{method}'. "
            + _format_available(resource, ".".join(walked))
        )
    return bound, desc, is_media


def http_method_of(desc: Optional[Dict[str, Any]]) -> Optional[str]:
    """The HTTP verb a discovery method description declares, upper-cased."""
    if not desc:
        return None
    verb = desc.get("httpMethod")
    return str(verb).upper() if verb else None


def ensure_read_only_allows(
    service_key: str, method: str, desc: Optional[Dict[str, Any]]
) -> None:
    """Under read-only policy, allow only methods the API declares as GET."""
    verb = http_method_of(desc)
    if verb == "GET":
        return
    if verb is None:
        raise UserInputError(
            f"{service_key} is read-only on this server and the HTTP verb of "
            f"'{method}' could not be determined, so the call is refused."
        )
    raise UserInputError(
        f"{service_key} is read-only on this server (--read-only or "
        f"--permissions {service_key}:readonly); '{method}' is HTTP {verb} and "
        "only GET methods are allowed."
    )


# ---------------------------------------------------------------------------
# Request construction and execution
# ---------------------------------------------------------------------------


def _parameter_hint(desc: Optional[Dict[str, Any]]) -> str:
    if not desc:
        return ""
    names = sorted((desc.get("parameters") or {}).keys())
    parts = []
    if names:
        parts.append(f"accepted params: {', '.join(names)}")
    parts.append("accepts a body" if "request" in desc else "does not accept a body")
    return " (" + "; ".join(parts) + ")"


def build_request(
    bound: Callable[..., Any],
    method: str,
    params: Optional[Dict[str, Any]],
    body: Optional[Dict[str, Any]],
    desc: Optional[Dict[str, Any]],
) -> Any:
    """Call the discovery method to obtain an ``HttpRequest`` (nothing is sent yet)."""
    if params is not None and not isinstance(params, dict):
        raise UserInputError("'params' must be a JSON object of query/path parameters.")
    kwargs: Dict[str, Any] = dict(params or {})
    uploads = _UPLOAD_PARAMS & set(kwargs)
    if uploads:
        raise UserInputError(
            f"{sorted(uploads)} are not supported by {TOOL_NAME}; media uploads "
            "need the typed tools (create_drive_file, update_drive_file, ...)."
        )
    if body is not None:
        if "body" in kwargs:
            raise UserInputError(
                "Pass the request body either as 'body' or inside 'params', not both."
            )
        if not isinstance(body, dict):
            raise UserInputError("'body' must be a JSON object.")
        kwargs["body"] = body
    try:
        return bound(**kwargs)
    except TypeError as exc:
        raise UserInputError(
            f"Could not build '{method}': {exc}{_parameter_hint(desc)}"
        ) from exc


def _capture_status(request: Any) -> Dict[str, Any]:
    """Wrap the request's postproc so the HTTP status is visible after execute()."""
    captured: Dict[str, Any] = {}
    original = getattr(request, "postproc", None)
    if not callable(original):
        return captured

    def postproc(resp, content):
        captured["status"] = getattr(resp, "status", None)
        return original(resp, content)

    try:
        request.postproc = postproc
    except AttributeError:
        pass
    return captured


async def execute_request(request: Any, is_media: bool) -> Tuple[Any, Optional[int]]:
    """Run the request off the event loop; media goes through the size-capped downloader."""
    if is_media:
        data = await download_media_bytes(request, kind="media")
        return data, None
    captured = _capture_status(request)
    result = await asyncio.to_thread(request.execute)
    return result, captured.get("status")


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def guess_mime_type(data: bytes, params: Optional[Dict[str, Any]]) -> str:
    """Best-effort MIME type: the export mimeType param, else magic bytes, else text/octet."""
    requested = (params or {}).get("mimeType")
    if requested:
        return str(requested)
    head = data[:16].lstrip()
    for magic, mime in _MAGIC_BYTES:
        if head.startswith(magic):
            return mime
    try:
        data[:4096].decode("utf-8")
    except UnicodeDecodeError:
        return "application/octet-stream"
    return "text/plain"


def describe_media(
    data: bytes,
    params: Optional[Dict[str, Any]],
    return_base64: bool,
    max_output_chars: int,
) -> Dict[str, Any]:
    """Summarise downloaded bytes; include base64 only when asked and it fits."""
    summary: Dict[str, Any] = {
        "bytes": len(data),
        "mime_type_guess": guess_mime_type(data, params),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    if not return_base64:
        summary["note"] = (
            "Binary content omitted; pass return_base64=true to include it."
        )
        return summary
    encoded = base64.b64encode(data).decode("ascii")
    if len(encoded) > max_output_chars:
        summary["note"] = (
            f"base64 omitted: {len(encoded):,} characters exceed max_output_chars="
            f"{max_output_chars:,}. Raise max_output_chars or use a typed download tool."
        )
        return summary
    summary["base64"] = encoded
    return summary


def render_payload(payload: Any) -> str:
    """Pretty-print an API response; empty bodies and non-JSON values stay readable."""
    if payload is None or payload == "" or payload == b"":
        return "(empty response body)"
    if isinstance(payload, bytes):
        try:
            payload = payload.decode("utf-8")
        except UnicodeDecodeError:
            return f"({len(payload):,} bytes of non-UTF-8 content)"
    try:
        return json.dumps(payload, indent=2, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(payload)


def truncate(text: str, max_output_chars: int) -> str:
    """Cut ``text`` to ``max_output_chars`` with a note that says how much was dropped."""
    if len(text) <= max_output_chars:
        return text
    return (
        text[:max_output_chars]
        + f"\n\n[Output truncated: showing {max_output_chars:,} of {len(text):,} "
        "characters. Narrow the request (fields, pageSize/maxResults, ranges) or "
        "raise max_output_chars.]"
    )


def format_result(
    service_key: str,
    method: str,
    payload: Any,
    status: Optional[int],
    max_output_chars: int,
) -> str:
    """Header line plus the rendered body, truncated to ``max_output_chars``."""
    status_text = f"HTTP {status}" if status else "HTTP ok"
    header = f"{service_key}.{method} → {status_text}"
    return header + "\n" + truncate(render_payload(payload), max_output_chars)


def resolve_max_output_chars(value: Optional[int]) -> int:
    """Apply the default and reject non-positive limits."""
    if value is None:
        return DEFAULT_MAX_OUTPUT_CHARS
    try:
        limit = int(value)
    except (TypeError, ValueError) as exc:
        raise UserInputError("'max_output_chars' must be a positive integer.") from exc
    if limit < 1:
        raise UserInputError("'max_output_chars' must be a positive integer.")
    return limit


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def perform_api_request(
    user_google_email: Optional[str],
    service: str,
    method: str,
    params: Optional[Dict[str, Any]],
    body: Optional[Dict[str, Any]],
    version: Optional[str],
    return_base64: bool,
    max_output_chars: Optional[int],
) -> str:
    """Validate, authenticate, walk, execute and render one generic API call."""
    service_key, tool_key = resolve_service(service)
    api_version = resolve_version(service_key, version)
    segments = split_method_path(method)
    limit = resolve_max_output_chars(max_output_chars)
    ensure_service_enabled(tool_key, service_key)
    read_only = is_service_read_only(tool_key)
    scopes = required_scopes_for(tool_key)
    normalized_method = ".".join(segments)

    logger.info(
        "[%s] %s %s/%s %s%s",
        TOOL_NAME,
        user_google_email,
        service_key,
        api_version,
        normalized_method,
        " (read-only)" if read_only else "",
    )

    async def run(google_service: Any, _email: str) -> str:
        bound, desc, is_media = walk_method(google_service, normalized_method)
        if read_only:
            ensure_read_only_allows(service_key, normalized_method, desc)
        request = build_request(bound, normalized_method, params, body, desc)
        payload, status = await execute_request(request, is_media)
        if is_media:
            payload = describe_media(payload, params, return_base64, limit)
        return format_result(service_key, normalized_method, payload, status, limit)

    return await call_with_service(
        service_key, api_version, scopes, user_google_email, run
    )
