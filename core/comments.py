"""
Core Comments Module

This module provides reusable comment management functions for Google Workspace applications.
All Google Workspace apps (Docs, Sheets, Slides) use the Drive API for comment operations.
"""

import logging
import asyncio
import os
from typing import Awaitable, Callable, Optional

from mcp.types import ToolAnnotations

from auth.permissions import is_action_denied
from auth.service_decorator import require_google_service, require_multiple_services
from core.server import server
from core.utils import handle_http_errors

logger = logging.getLogger(__name__)


READ_COMMENT_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)

MANAGE_COMMENT_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)


COMMENT_ACTIONS = (
    "create",
    "reply",
    "update",
    "delete",
    "resolve",
    "reopen",
    "list_replies",
    "update_reply",
    "delete_reply",
)

MANAGE_COMMENT_ACTIONS_DOC = """
            Actions:
              - create: Create a new comment. Requires comment_content.
              - reply: Reply to a comment. Requires comment_id and comment_content.
              - update: Edit a comment's text. Requires comment_id and comment_content.
              - delete: Delete a comment and its replies. Requires comment_id.
              - resolve: Mark a comment resolved. Requires comment_id; comment_content
                is an optional note on the resolving reply.
              - reopen: Reopen a resolved comment. Requires comment_id; comment_content
                is an optional note on the reopening reply.
              - list_replies: List the replies on a comment. Requires comment_id.
              - update_reply: Edit a reply's text. Requires comment_id, reply_id and
                comment_content.
              - delete_reply: Delete a reply. Requires comment_id and reply_id.
"""


async def _manage_comment_dispatch(
    service,
    app_name: str,
    file_id: str,
    action: str,
    comment_content: Optional[str] = None,
    comment_id: Optional[str] = None,
    cell: Optional[str] = None,
    sheets_service=None,
    insert_cell_comment: Optional[Callable[..., Awaitable[str]]] = None,
    reply_id: Optional[str] = None,
) -> str:
    """Route comment management actions to the appropriate implementation."""
    action_lower = (action or "").lower().strip()
    if action_lower not in COMMENT_ACTIONS:
        raise ValueError(
            f"Invalid action '{action_lower}'. Must be one of: "
            f"{', '.join(COMMENT_ACTIONS)}."
        )
    if cell and action_lower != "create":
        raise ValueError("cell is only supported for the create action")
    if action_lower in ("delete", "delete_reply") and is_action_denied(
        "drive", action_lower
    ):
        raise ValueError(
            f"The '{action_lower}' action is not allowed under the current permission level."
        )

    if action_lower == "create":
        if not comment_content:
            raise ValueError("comment_content is required for create action")
        if cell:
            if insert_cell_comment is None:
                raise ValueError("insert_cell_comment is required for cell comments")
            return await _create_cell_comment_impl(
                insert_cell_comment, sheets_service, file_id, cell, comment_content
            )
        return await _create_comment_impl(service, app_name, file_id, comment_content)

    if not comment_id:
        raise ValueError(f"comment_id is required for {action_lower} action")

    if action_lower == "reply":
        if not comment_content:
            raise ValueError(
                "comment_id and comment_content are required for reply action"
            )
        return await _reply_to_comment_impl(
            service, app_name, file_id, comment_id, comment_content
        )
    if action_lower == "update":
        if not comment_content:
            raise ValueError("comment_content is required for update action")
        return await _update_comment_impl(
            service, app_name, file_id, comment_id, comment_content
        )
    if action_lower == "delete":
        return await _delete_comment_impl(service, app_name, file_id, comment_id)
    if action_lower == "resolve":
        return await _resolve_comment_impl(
            service, app_name, file_id, comment_id, note=comment_content
        )
    if action_lower == "reopen":
        return await _reopen_comment_impl(
            service, app_name, file_id, comment_id, note=comment_content
        )
    if action_lower == "list_replies":
        return await _list_replies_impl(service, app_name, file_id, comment_id)

    # update_reply / delete_reply
    if not reply_id:
        raise ValueError(f"reply_id is required for {action_lower} action")
    if action_lower == "update_reply":
        if not comment_content:
            raise ValueError("comment_content is required for update_reply action")
        return await _update_reply_impl(
            service, app_name, file_id, comment_id, reply_id, comment_content
        )
    return await _delete_reply_impl(service, app_name, file_id, comment_id, reply_id)


def create_comment_tools(
    app_name: str,
    file_id_param: str,
    insert_cell_comment: Optional[Callable[..., Awaitable[str]]] = None,
):
    """
    Factory function to create comment management tools for a specific Google Workspace app.

    Args:
        app_name: Name of the app (e.g., "document", "spreadsheet", "presentation")
        file_id_param: Parameter name for the file ID (e.g., "document_id", "spreadsheet_id", "presentation_id")
        insert_cell_comment: Spreadsheet only; passed in because gsheets imports this module.

    Returns:
        Dict containing the comment management functions with unique names
    """

    # --- Consolidated tools ---
    list_func_name = f"list_{app_name}_comments"
    manage_func_name = f"manage_{app_name}_comment"
    app_title = app_name.replace("_", " ").title()

    if file_id_param == "document_id":

        @require_google_service("drive", "drive_read")
        @handle_http_errors(list_func_name, is_read_only=True, service_type="drive")
        async def list_comments(
            service,
            user_google_email: str,
            document_id: str,
            max_comments: int | None = None,
        ) -> str:
            """List all comments from a Google Document (optional max_comments to limit results)."""
            return await _read_comments_impl(
                service, app_name, document_id, max_comments=max_comments
            )

        # Use full Drive scope so comment operations remain visible to collaborators.
        @require_google_service("drive", "drive")
        @handle_http_errors(manage_func_name, service_type="drive")
        async def manage_comment(
            service,
            user_google_email: str,
            document_id: str,
            action: str,
            comment_content: Optional[str] = None,
            comment_id: Optional[str] = None,
            reply_id: Optional[str] = None,
        ) -> str:
            """Manage comments on a Google Document.

            Note: comments created via the Drive API are document-level
            (unanchored); only the Google Docs UI can anchor to specific text.
            """
            return await _manage_comment_dispatch(
                service,
                app_name,
                document_id,
                action,
                comment_content,
                comment_id,
                reply_id=reply_id,
            )

    elif file_id_param == "spreadsheet_id":

        @require_google_service("drive", "drive_read")
        @handle_http_errors(list_func_name, is_read_only=True, service_type="drive")
        async def list_comments(
            service,
            user_google_email: str,
            spreadsheet_id: str,
            max_comments: int | None = None,
        ) -> str:
            """List all comments from a Google Spreadsheet (optional max_comments to limit results)."""
            return await _read_comments_impl(
                service, app_name, spreadsheet_id, max_comments=max_comments
            )

        # Use full Drive scope so comment operations remain visible to collaborators.
        # Sheets batchUpdate also accepts it, so cell comments need no extra grant.
        @require_multiple_services(
            [
                {"service_type": "drive", "scopes": "drive", "param_name": "service"},
                {
                    "service_type": "sheets",
                    "scopes": "drive",
                    "param_name": "sheets_service",
                },
            ]
        )
        @handle_http_errors(manage_func_name, service_type="drive")
        async def manage_comment(
            service,
            sheets_service,
            user_google_email: str,
            spreadsheet_id: str,
            action: str,
            comment_content: Optional[str] = None,
            comment_id: Optional[str] = None,
            cell: Optional[str] = None,
            reply_id: Optional[str] = None,
        ) -> str:
            """Manage comments on a Google Spreadsheet.

            For create, pass cell (A1 notation, e.g. 'Sheet1!B2'; defaults to
            the first sheet) to anchor the comment to a cell; otherwise it is a
            file-level comment. cell is only valid with create.
            """
            return await _manage_comment_dispatch(
                service,
                app_name,
                spreadsheet_id,
                action,
                comment_content,
                comment_id,
                cell=cell,
                sheets_service=sheets_service,
                insert_cell_comment=insert_cell_comment,
                reply_id=reply_id,
            )

    elif file_id_param == "presentation_id":

        @require_google_service("drive", "drive_read")
        @handle_http_errors(list_func_name, is_read_only=True, service_type="drive")
        async def list_comments(
            service,
            user_google_email: str,
            presentation_id: str,
            max_comments: int | None = None,
        ) -> str:
            """List all comments from a Google Presentation (optional max_comments to limit results)."""
            return await _read_comments_impl(
                service, app_name, presentation_id, max_comments=max_comments
            )

        # Use full Drive scope so comment operations remain visible to collaborators.
        @require_google_service("drive", "drive")
        @handle_http_errors(manage_func_name, service_type="drive")
        async def manage_comment(
            service,
            user_google_email: str,
            presentation_id: str,
            action: str,
            comment_content: Optional[str] = None,
            comment_id: Optional[str] = None,
            reply_id: Optional[str] = None,
        ) -> str:
            """Manage comments on a Google Presentation.

            Note: The Drive API cannot anchor comments to arbitrary text;
            Slides comments are element-scoped via the API.
            """
            return await _manage_comment_dispatch(
                service,
                app_name,
                presentation_id,
                action,
                comment_content,
                comment_id,
                reply_id=reply_id,
            )

    list_comments.__name__ = list_func_name
    manage_comment.__name__ = manage_func_name
    manage_comment.__doc__ = (manage_comment.__doc__ or "") + MANAGE_COMMENT_ACTIONS_DOC
    server.tool(
        title=f"List {app_title} Comments",
        annotations=READ_COMMENT_ANNOTATIONS,
    )(list_comments)
    server.tool(
        title=f"Manage {app_title} Comment",
        annotations=MANAGE_COMMENT_ANNOTATIONS,
    )(manage_comment)

    return {
        "list_comments": list_comments,
        "manage_comment": manage_comment,
    }


def _format_field(label: str, value: str) -> str:
    """Render a labeled value with continuation lines indented under the value.

    Comment text is free-form and may contain newlines; without the hanging
    indent a line such as "Author: ..." inside a comment body would be
    indistinguishable from a real field.
    """
    return label + ("\n" + " " * len(label)).join(value.splitlines())


async def _read_comments_impl(
    service, app_name: str, file_id: str, max_comments: int | None = None
) -> str:
    """Implementation for reading comments from any Google Workspace file."""
    logger.info(f"[read_{app_name}_comments] Reading comments for {app_name} {file_id}")

    if max_comments is None:
        try:
            max_comments = int(os.getenv("WORKSPACE_MCP_COMMENTS_MAX", "100"))
        except (ValueError, TypeError):
            max_comments = 100

    if max_comments < 0:
        max_comments = 100
    if max_comments == 0:
        return f"No comments found in {app_name} {file_id}"

    comments: list = []
    page_token: str | None = None

    while len(comments) < max_comments:
        remaining = max_comments - len(comments)
        page_size = min(100, remaining)

        kwargs: dict = {
            "fileId": file_id,
            "fields": "nextPageToken,comments(id,content,author,createdTime,modifiedTime,resolved,quotedFileContent,anchor,replies(content,author,id,createdTime,modifiedTime))",
            "pageSize": page_size,
        }
        if page_token is not None:
            kwargs["pageToken"] = page_token

        response = await asyncio.to_thread(service.comments().list(**kwargs).execute)

        page_comments = response.get("comments", [])
        take = min(len(page_comments), max_comments - len(comments))
        comments.extend(page_comments[:take])

        page_token = response.get("nextPageToken")
        if not page_token or len(comments) >= max_comments:
            break

    if not comments:
        return f"No comments found in {app_name} {file_id}"

    output = [f"Found {len(comments)} comments in {app_name} {file_id}:\n"]

    for comment in comments:
        author = comment.get("author", {}).get("displayName", "Unknown")
        content = comment.get("content", "")
        created = comment.get("createdTime", "")
        resolved = comment.get("resolved", False)
        comment_id = comment.get("id", "")
        status = " [RESOLVED]" if resolved else ""

        quoted_text = comment.get("quotedFileContent", {}).get("value", "")
        anchor = comment.get("anchor", "")

        output.append(f"Comment ID: {comment_id}")
        output.append(f"Author: {author}")
        output.append(f"Created: {created}{status}")
        if quoted_text:
            output.append(_format_field("Quoted text: ", quoted_text))
        if anchor:
            # Opaque, file-type specific; Slides lists element objectIds in `targets`.
            output.append(_format_field("Anchor: ", anchor))
        output.append(_format_field("Content: ", content))

        replies = comment.get("replies", [])
        if replies:
            output.append(f"  Replies ({len(replies)}):")
            for reply in replies:
                reply_author = reply.get("author", {}).get("displayName", "Unknown")
                reply_content = reply.get("content", "")
                reply_created = reply.get("createdTime", "")
                reply_id = reply.get("id", "")
                output.append(f"    Reply ID: {reply_id}")
                output.append(f"    Author: {reply_author}")
                output.append(f"    Created: {reply_created}")
                output.append(_format_field("    Content: ", reply_content))

        output.append("")  # Empty line between comments

    return "\n".join(output)


async def _create_comment_impl(
    service, app_name: str, file_id: str, comment_content: str
) -> str:
    """Implementation for creating a comment on any Google Workspace file.

    Note: Comments created via the Drive API appear as document-level comments.
    The Google Drive API does not support anchoring comments to specific text in
    Google Docs; only the Docs UI can create anchored comments.
    """
    logger.info(f"[create_{app_name}_comment] Creating comment in {app_name} {file_id}")

    body = {"content": comment_content}

    comment = await asyncio.to_thread(
        service.comments()
        .create(
            fileId=file_id,
            body=body,
            fields="id,content,author,createdTime,modifiedTime",
        )
        .execute
    )

    comment_id = comment.get("id", "")
    author = comment.get("author", {}).get("displayName", "Unknown")
    created = comment.get("createdTime", "")

    return f"Comment created successfully!\nComment ID: {comment_id}\nAuthor: {author}\nCreated: {created}\n{_format_field('Content: ', comment_content)}"


async def _create_cell_comment_impl(
    insert_cell_comment: Callable[..., Awaitable[str]],
    sheets_service,
    spreadsheet_id: str,
    cell: str,
    comment_content: str,
) -> str:
    """Implementation for creating a cell-anchored comment on a spreadsheet."""
    logger.info(
        f"[create_spreadsheet_comment] Creating comment on {cell} in spreadsheet {spreadsheet_id}"
    )
    comment_id = await insert_cell_comment(
        sheets_service, spreadsheet_id, cell, comment_content
    )
    return f"Comment created successfully on {cell}!\nComment ID: {comment_id}\n{_format_field('Content: ', comment_content)}"


async def _reply_to_comment_impl(
    service, app_name: str, file_id: str, comment_id: str, reply_content: str
) -> str:
    """Implementation for replying to a comment on any Google Workspace file."""
    logger.info(
        f"[reply_to_{app_name}_comment] Replying to comment {comment_id} in {app_name} {file_id}"
    )

    body = {"content": reply_content}

    reply = await asyncio.to_thread(
        service.replies()
        .create(
            fileId=file_id,
            commentId=comment_id,
            body=body,
            fields="id,content,author,createdTime,modifiedTime",
        )
        .execute
    )

    reply_id = reply.get("id", "")
    author = reply.get("author", {}).get("displayName", "Unknown")
    created = reply.get("createdTime", "")

    return f"Reply posted successfully!\nReply ID: {reply_id}\nAuthor: {author}\nCreated: {created}\n{_format_field('Content: ', reply_content)}"


async def _set_comment_resolution_impl(
    service,
    app_name: str,
    file_id: str,
    comment_id: str,
    action: str,
    note: Optional[str] = None,
) -> str:
    """Resolve or reopen a comment by posting a reply carrying an `action`."""
    logger.info(
        f"[{action}_{app_name}_comment] {action.title()}ing comment {comment_id} in {app_name} {file_id}"
    )
    default_note = (
        "This comment has been resolved."
        if action == "resolve"
        else "This comment has been reopened."
    )
    body = {"content": note or default_note, "action": action}

    reply = await asyncio.to_thread(
        service.replies()
        .create(
            fileId=file_id,
            commentId=comment_id,
            body=body,
            fields="id,content,author,createdTime,modifiedTime,action",
        )
        .execute
    )

    reply_id = reply.get("id", "")
    author = reply.get("author", {}).get("displayName", "Unknown")
    created = reply.get("createdTime", "")
    past = "resolved" if action == "resolve" else "reopened"

    return (
        f"Comment {comment_id} has been {past} successfully.\n"
        f"{action.title()} reply ID: {reply_id}\nAuthor: {author}\nCreated: {created}"
    )


async def _resolve_comment_impl(
    service, app_name: str, file_id: str, comment_id: str, note: Optional[str] = None
) -> str:
    """Implementation for resolving a comment on any Google Workspace file."""
    return await _set_comment_resolution_impl(
        service, app_name, file_id, comment_id, "resolve", note=note
    )


async def _reopen_comment_impl(
    service, app_name: str, file_id: str, comment_id: str, note: Optional[str] = None
) -> str:
    """Implementation for reopening a resolved comment on any Google Workspace file."""
    return await _set_comment_resolution_impl(
        service, app_name, file_id, comment_id, "reopen", note=note
    )


async def _update_comment_impl(
    service, app_name: str, file_id: str, comment_id: str, comment_content: str
) -> str:
    """Implementation for editing a comment's text."""
    logger.info(
        f"[update_{app_name}_comment] Updating comment {comment_id} in {app_name} {file_id}"
    )
    comment = await asyncio.to_thread(
        service.comments()
        .update(
            fileId=file_id,
            commentId=comment_id,
            body={"content": comment_content},
            fields="id,content,author,createdTime,modifiedTime",
        )
        .execute
    )
    modified = comment.get("modifiedTime", "")
    return (
        f"Comment updated successfully!\nComment ID: {comment.get('id', comment_id)}\n"
        f"Modified: {modified}\n{_format_field('Content: ', comment_content)}"
    )


async def _delete_comment_impl(
    service, app_name: str, file_id: str, comment_id: str
) -> str:
    """Implementation for deleting a comment (and its replies)."""
    logger.info(
        f"[delete_{app_name}_comment] Deleting comment {comment_id} in {app_name} {file_id}"
    )
    await asyncio.to_thread(
        service.comments().delete(fileId=file_id, commentId=comment_id).execute
    )
    return f"Comment {comment_id} deleted from {app_name} {file_id}."


async def _list_replies_impl(
    service, app_name: str, file_id: str, comment_id: str, max_replies: int = 100
) -> str:
    """Implementation for listing the replies on a comment."""
    logger.info(
        f"[list_{app_name}_comment_replies] Listing replies to {comment_id} in {app_name} {file_id}"
    )
    replies: list = []
    page_token: Optional[str] = None
    while len(replies) < max_replies:
        kwargs: dict = {
            "fileId": file_id,
            "commentId": comment_id,
            "pageSize": min(100, max_replies - len(replies)),
            "fields": "nextPageToken,replies(id,content,author,createdTime,modifiedTime,action,deleted)",
        }
        if page_token:
            kwargs["pageToken"] = page_token
        response = await asyncio.to_thread(service.replies().list(**kwargs).execute)
        page = response.get("replies", [])
        replies.extend(page[: max_replies - len(replies)])
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    if not replies:
        return f"No replies found on comment {comment_id} in {app_name} {file_id}"

    output = [f"Found {len(replies)} replies on comment {comment_id}:", ""]
    for reply in replies:
        output.append(f"Reply ID: {reply.get('id', '')}")
        output.append(
            f"Author: {reply.get('author', {}).get('displayName', 'Unknown')}"
        )
        created = reply.get("createdTime", "")
        action = reply.get("action")
        suffix = f" [{action.upper()}]" if action else ""
        if reply.get("deleted"):
            suffix += " [DELETED]"
        output.append(f"Created: {created}{suffix}")
        output.append(_format_field("Content: ", reply.get("content", "")))
        output.append("")
    output.pop()  # drop the trailing blank separator
    return "\n".join(output)


async def _update_reply_impl(
    service,
    app_name: str,
    file_id: str,
    comment_id: str,
    reply_id: str,
    reply_content: str,
) -> str:
    """Implementation for editing a reply's text."""
    logger.info(
        f"[update_{app_name}_comment_reply] Updating reply {reply_id} on {comment_id} in {app_name} {file_id}"
    )
    reply = await asyncio.to_thread(
        service.replies()
        .update(
            fileId=file_id,
            commentId=comment_id,
            replyId=reply_id,
            body={"content": reply_content},
            fields="id,content,author,createdTime,modifiedTime",
        )
        .execute
    )
    return (
        f"Reply updated successfully!\nReply ID: {reply.get('id', reply_id)}\n"
        f"Modified: {reply.get('modifiedTime', '')}\n"
        f"{_format_field('Content: ', reply_content)}"
    )


async def _delete_reply_impl(
    service, app_name: str, file_id: str, comment_id: str, reply_id: str
) -> str:
    """Implementation for deleting a reply."""
    logger.info(
        f"[delete_{app_name}_comment_reply] Deleting reply {reply_id} on {comment_id} in {app_name} {file_id}"
    )
    await asyncio.to_thread(
        service.replies()
        .delete(fileId=file_id, commentId=comment_id, replyId=reply_id)
        .execute
    )
    return (
        f"Reply {reply_id} deleted from comment {comment_id} in {app_name} {file_id}."
    )
