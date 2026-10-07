"""
Generic Google API MCP tool.

One tool, ``google_api_request``, reaches the REST methods the typed tools do
not wrap, bounded by the scopes, enabled services and read-only policy this
server already enforces. All logic lives in ``api_request_helpers``.
"""

import logging
from typing import Optional

from mcp.types import ToolAnnotations

from core.server import server
from core.utils import JsonDict, handle_http_errors
from gapi_request.api_request_helpers import (
    TOOL_NAME,
    accept_managed_user_email,
    perform_api_request,
)

logger = logging.getLogger(__name__)


@server.tool(
    title="Google API Request",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
@accept_managed_user_email
@handle_http_errors(TOOL_NAME)
async def google_api_request(
    user_google_email: str,
    service: str,
    method: str,
    params: Optional[JsonDict] = None,
    body: Optional[JsonDict] = None,
    version: Optional[str] = None,
    return_base64: bool = False,
    max_output_chars: Optional[int] = None,
) -> str:
    """
    Call any REST method of a Google API this server is configured for.

    Use it for the API methods no typed tool covers. It authenticates with the
    same credentials and scopes as the typed tools for that service, so it can
    never reach a service or scope the server was not started with, and it
    only allows HTTP GET methods when the service is read-only (--read-only or
    --permissions <service>:readonly).

    WARNING: this bypasses the guard rails the typed tools add (input
    validation, confirmation prompts, size and pagination defaults, friendly
    formatting). Prefer a typed tool when one exists; check the method's
    reference documentation before calling anything that writes or deletes.

    Method syntax: a dotted path through the API's collections ending in the
    method name, exactly as the Python discovery client names them. Each
    segment but the last is a collection; the last is the method. Media
    download methods take the '_media' suffix (Drive 'files.get_media',
    'files.export_media') and return size, MIME guess and SHA-256, plus the
    bytes as base64 when return_base64 is true. On an unknown segment the error
    lists the collections and methods available at that level.

    Examples:
      calendar  method='events.move'
                params={'calendarId': 'primary', 'eventId': 'abc123', 'destination': 'work@group.calendar.google.com'}
      gmail     method='users.drafts.list'
                params={'userId': 'me', 'maxResults': 10}
      sheets    method='spreadsheets.values.append'
                params={'spreadsheetId': 'SHEET_ID', 'range': 'Sheet1!A:C', 'valueInputOption': 'USER_ENTERED'}
                body={'values': [['2026-10-08', 'Invoice 42', 1200]]}
      docs      method='documents.batchUpdate'
                params={'documentId': 'DOC_ID'}
                body={'requests': [{'insertText': {'location': {'index': 1}, 'text': 'Hello'}}]}

    Args:
        user_google_email (str): The user's Google email address. Required.
        service (str): Service name: gmail, drive, calendar, docs, sheets, chat,
            forms, slides, tasks, people (alias: contacts), customsearch (alias:
            search), script (alias: appscript), meet, keep.
        method (str): Dotted method path, e.g. 'users.messages.list',
            'spreadsheets.values.batchGet', 'files.export_media'.
        params (Optional[dict]): Path and query parameters, keyed exactly as the
            API documents them (camelCase).
        body (Optional[dict]): JSON request body for POST/PUT/PATCH methods.
        version (Optional[str]): API version override (default: the version the
            typed tools use, e.g. drive v3, sheets v4, calendar v3).
        return_base64 (bool): For '_media' methods, include the downloaded bytes
            as base64. Default False (size, MIME guess and SHA-256 only).
        max_output_chars (Optional[int]): Cap on the returned text (default
            60000). Longer output is truncated with a note.

    Returns:
        str: A header line 'service.method -> HTTP <status>' followed by the
            pretty-printed JSON response.
    """
    return await perform_api_request(
        user_google_email=user_google_email,
        service=service,
        method=method,
        params=params,
        body=body,
        version=version,
        return_base64=return_base64,
        max_output_chars=max_output_chars,
    )
