"""
Docs-native comment thread helpers.

Comment threads are written through documents.batchUpdate (insertComment,
addCommentReply, updateCommentPost, deleteComment, deleteCommentReply), which
unlike the Drive comments API can anchor a comment to an exact index range.
Listing still goes through Drive comments.list, which is the only read that
returns anchors, assignees and replies in one call.
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple

from gdocs.docs_anchors import AnchorResolutionError, resolve_anchor_text
from gdocs.docs_helpers import _build_range
from gdocs.managers.batch_operation_manager import BatchOperationManager

logger = logging.getLogger(__name__)

COMMENT_ACTIONS = (
    "list",
    "create",
    "reply",
    "resolve",
    "reopen",
    "update",
    "delete",
    "delete_reply",
)

# Docs API limit for comment content and assignee addresses (UTF-8 code units).
MAX_COMMENT_CONTENT_BYTES = 2048

DRIVE_COMMENT_FIELDS = (
    "nextPageToken,"
    "comments(id,content,author(displayName,emailAddress),createdTime,"
    "modifiedTime,resolved,anchor,assigneeEmailAddress,quotedFileContent(value),"
    "replies(id,content,author(displayName),createdTime,action,"
    "assigneeEmailAddress))"
)


class CommentInputError(ValueError):
    """Raised for caller mistakes before any API call is made."""


def _require_content(content: Optional[str], action: str) -> str:
    if content is None or not str(content).strip():
        raise CommentInputError(f"'content' is required for the '{action}' action.")
    if len(content.encode("utf-8")) > MAX_COMMENT_CONTENT_BYTES:
        raise CommentInputError(
            f"'content' may not exceed {MAX_COMMENT_CONTENT_BYTES} UTF-8 bytes."
        )
    return content


def _require_id(value: Optional[str], name: str, action: str) -> str:
    if not value or not isinstance(value, str):
        raise CommentInputError(f"'{name}' is required for the '{action}' action.")
    return value


def create_insert_comment_request(
    start_index: int,
    end_index: int,
    content: str,
    assignee_email: Optional[str] = None,
    tab_id: Optional[str] = None,
    segment_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Build an insertComment request anchored to [start_index, end_index).

    Reference:
        https://developers.google.com/workspace/docs/api/reference/rest/v1/documents/request#InsertCommentRequest
    """
    if not isinstance(start_index, int) or not isinstance(end_index, int):
        raise CommentInputError("start_index and end_index must be integers.")
    if start_index < 1:
        raise CommentInputError("start_index must be at least 1.")
    if end_index <= start_index:
        raise CommentInputError("end_index must be greater than start_index.")

    request: Dict[str, Any] = {
        "range": _build_range(start_index, end_index, tab_id, segment_id),
        "content": _require_content(content, "create"),
    }
    if assignee_email:
        request["assigneeEmailAddress"] = assignee_email
    return {"insertComment": request}


def create_add_comment_reply_request(
    comment_id: str,
    content: Optional[str] = None,
    assignee_email: Optional[str] = None,
    comment_action: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Build an addCommentReply request.

    comment_action RESOLVE or REOPEN changes the thread status and makes
    content optional; otherwise content is required. The API rejects an
    assignee together with RESOLVE/REOPEN.

    Reference:
        https://developers.google.com/workspace/docs/api/reference/rest/v1/documents/request#AddCommentReplyRequest
    """
    post: Dict[str, Any] = {}
    if comment_action:
        if assignee_email:
            raise CommentInputError(
                "assignee_email cannot be combined with resolving or reopening."
            )
        post["commentAction"] = comment_action
        if content is not None and str(content).strip():
            post["content"] = _require_content(content, comment_action.lower())
    else:
        post["content"] = _require_content(content, "reply")
    if assignee_email:
        post["assigneeEmail"] = assignee_email
    return {
        "addCommentReply": {
            "commentId": _require_id(comment_id, "comment_id", "reply"),
            "post": post,
        }
    }


def create_update_comment_post_request(
    comment_id: str, post_id: str, content: str
) -> Dict[str, Any]:
    """Build an updateCommentPost request for the head post or a reply."""
    return {
        "updateCommentPost": {
            "commentId": _require_id(comment_id, "comment_id", "update"),
            "postId": _require_id(post_id, "post_id", "update"),
            "content": _require_content(content, "update"),
        }
    }


def create_delete_comment_request(comment_id: str) -> Dict[str, Any]:
    """Build a deleteComment request that removes a whole thread."""
    return {
        "deleteComment": {"commentId": _require_id(comment_id, "comment_id", "delete")}
    }


def create_delete_comment_reply_request(
    comment_id: str, post_id: str
) -> Dict[str, Any]:
    """Build a deleteCommentReply request for one reply post."""
    return {
        "deleteCommentReply": {
            "commentId": _require_id(comment_id, "comment_id", "delete_reply"),
            "postId": _require_id(post_id, "post_id", "delete_reply"),
        }
    }


def resolve_comment_range(
    doc: Dict[str, Any],
    anchor_text: str,
    tab_id: Optional[str] = None,
    segment_id: Optional[str] = None,
) -> Tuple[int, int]:
    """
    Resolve literal anchor text to the index range it occupies.

    The text must occur exactly once within a single paragraph of the targeted
    segment; an ambiguous or missing anchor raises rather than guessing.
    """
    if not anchor_text or not str(anchor_text).strip():
        raise CommentInputError("'anchor_text' must not be empty.")
    body = BatchOperationManager._target_segment(doc, tab_id, segment_id)
    if body is None:
        raise CommentInputError(
            f"Tab '{tab_id}' or segment '{segment_id}' was not found in the document."
        )
    try:
        start = resolve_anchor_text(body, anchor_text, "before")
        end = resolve_anchor_text(body, anchor_text, "after")
    except AnchorResolutionError as e:
        raise CommentInputError(str(e)) from e
    return start, end


def describe_comment_thread(thread: Dict[str, Any]) -> str:
    """One-line summary of a CommentThread from an insertComment reply."""
    head = thread.get("headPost", {})
    parts = [f"comment_id={thread.get('commentId') or 'unknown'}"]
    if head.get("postId"):
        parts.append(f"post_id={head['postId']}")
    if thread.get("anchorId"):
        parts.append(f"anchor_id={thread['anchorId']}")
    if thread.get("status"):
        parts.append(f"status={thread['status']}")
    if head.get("assigneeEmail"):
        parts.append(f"assignee={head['assigneeEmail']}")
    quote = thread.get("plainTextQuote")
    if quote:
        parts.append(f'quote="{quote[:80]}"')
    return ", ".join(parts)


def describe_post(post: Dict[str, Any]) -> str:
    """One-line summary of a Post from an addCommentReply reply."""
    parts = []
    if post.get("postId"):
        parts.append(f"post_id={post['postId']}")
    if post.get("commentAction") and post["commentAction"] not in (
        "COMMENT_ACTION_TYPE_UNSPECIFIED",
        "NO_COMMENT_ACTION_CHANGE",
    ):
        parts.append(f"action={post['commentAction']}")
    if post.get("assigneeEmail"):
        parts.append(f"assignee={post['assigneeEmail']}")
    return ", ".join(parts) if parts else "no post details returned"


async def list_drive_comments(
    drive_service: Any, file_id: str, include_deleted: bool = False
) -> List[Dict[str, Any]]:
    """Fetch every comment thread on a file through Drive comments.list."""
    comments: List[Dict[str, Any]] = []
    page_token: Optional[str] = None
    while True:
        response = await asyncio.to_thread(
            drive_service.comments()
            .list(
                fileId=file_id,
                fields=DRIVE_COMMENT_FIELDS,
                includeDeleted=include_deleted,
                pageSize=100,
                pageToken=page_token,
            )
            .execute
        )
        comments.extend(response.get("comments", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return comments


def _one_line(text: Optional[str], limit: int = 120) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def format_comment_list(
    comments: List[Dict[str, Any]], document_id: str, include_resolved: bool = True
) -> str:
    """Render Drive comment threads with the IDs the write actions need."""
    if not include_resolved:
        comments = [c for c in comments if not c.get("resolved")]
    if not comments:
        scope = "" if include_resolved else " open"
        return f"No{scope} comments found on document {document_id}."

    lines = [f"Found {len(comments)} comment thread(s) on document {document_id}:"]
    for number, comment in enumerate(comments, 1):
        author = comment.get("author", {}).get("displayName", "Unknown")
        status = "resolved" if comment.get("resolved") else "open"
        header = f"\n{number}. comment_id={comment.get('id')} [{status}] by {author}"
        if comment.get("createdTime"):
            header += f" at {comment['createdTime']}"
        if comment.get("assigneeEmailAddress"):
            header += f", assigned to {comment['assigneeEmailAddress']}"
        lines.append(header)
        quote = comment.get("quotedFileContent", {}).get("value")
        if quote:
            lines.append(f'   quoted: "{_one_line(quote, 80)}"')
        if comment.get("anchor"):
            lines.append(f"   anchor: {_one_line(comment['anchor'], 100)}")
        lines.append(f"   {_one_line(comment.get('content'))}")
        for reply in comment.get("replies", []):
            reply_author = reply.get("author", {}).get("displayName", "Unknown")
            action = reply.get("action")
            action_note = f" ({action})" if action else ""
            lines.append(
                f"   - reply post_id={reply.get('id')} by {reply_author}"
                f"{action_note}: {_one_line(reply.get('content'))}"
            )
    return "\n".join(lines)
