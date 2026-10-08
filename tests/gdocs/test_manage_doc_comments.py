"""Tests for the Docs-native manage_doc_comments tool and its helpers."""

from unittest.mock import Mock

import pytest

from core.utils import UserInputError
from gdocs import docs_tools
from gdocs.docs_comments import (
    CommentInputError,
    create_add_comment_reply_request,
    create_insert_comment_request,
    format_comment_list,
    resolve_comment_range,
)

DOC_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcd"
EMAIL = "user@example.com"


def _unwrap(tool):
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


def _paragraph(start_index, text, named_style="NORMAL_TEXT"):
    return {
        "startIndex": start_index,
        "endIndex": start_index + len(text),
        "paragraph": {
            "elements": [
                {
                    "startIndex": start_index,
                    "endIndex": start_index + len(text),
                    "textRun": {"content": text},
                }
            ],
            "paragraphStyle": {"namedStyleType": named_style},
        },
    }


DOC = {
    "tabs": [
        {
            "tabProperties": {"tabId": "t.0"},
            "documentTab": {
                "body": {
                    "content": [
                        _paragraph(1, "Intro\n"),
                        _paragraph(7, "Numbers went up.\n"),
                    ]
                }
            },
        }
    ]
}


def _docs_service(reply=None):
    service = Mock()
    service.documents.return_value.get.return_value.execute.return_value = DOC
    service.documents.return_value.batchUpdate.return_value.execute.return_value = {
        "replies": [reply if reply is not None else {}]
    }
    return service


def _sent_request(service):
    body = service.documents.return_value.batchUpdate.call_args.kwargs["body"]
    assert "writeControl" not in body
    assert len(body["requests"]) == 1
    return body["requests"][0]


async def _call(docs_service, drive_service=None, **kwargs):
    return await _unwrap(docs_tools.manage_doc_comments)(
        docs_service=docs_service,
        drive_service=drive_service or Mock(),
        user_google_email=EMAIL,
        document_id=DOC_ID,
        **kwargs,
    )


class TestRequestBuilders:
    def test_insert_comment_with_assignee_and_tab(self):
        request = create_insert_comment_request(
            3, 9, "Check this", assignee_email="ana@example.com", tab_id="t.0"
        )
        assert request == {
            "insertComment": {
                "range": {"startIndex": 3, "endIndex": 9, "tabId": "t.0"},
                "content": "Check this",
                "assigneeEmailAddress": "ana@example.com",
            }
        }

    @pytest.mark.parametrize(
        "start, end, message",
        [(0, 5, "at least 1"), (5, 5, "greater than"), ("1", 5, "integers")],
    )
    def test_insert_comment_rejects_bad_ranges(self, start, end, message):
        with pytest.raises(CommentInputError, match=message):
            create_insert_comment_request(start, end, "x")

    def test_insert_comment_rejects_empty_or_oversized_content(self):
        with pytest.raises(CommentInputError, match="content"):
            create_insert_comment_request(1, 5, "   ")
        with pytest.raises(CommentInputError, match="2048"):
            create_insert_comment_request(1, 5, "é" * 1100)

    def test_reply_requires_content_unless_resolving(self):
        with pytest.raises(CommentInputError, match="content"):
            create_add_comment_reply_request("c1")
        request = create_add_comment_reply_request("c1", comment_action="RESOLVE")
        assert request == {
            "addCommentReply": {"commentId": "c1", "post": {"commentAction": "RESOLVE"}}
        }

    def test_reply_rejects_assignee_with_status_change(self):
        with pytest.raises(CommentInputError, match="assignee_email"):
            create_add_comment_reply_request(
                "c1", assignee_email="a@b.c", comment_action="REOPEN"
            )

    def test_resolve_comment_range_from_anchor_text(self):
        assert resolve_comment_range(DOC, "went up", tab_id="t.0") == (15, 22)

    def test_resolve_comment_range_rejects_missing_or_ambiguous_text(self):
        with pytest.raises(CommentInputError, match="not found"):
            resolve_comment_range(DOC, "absent")
        with pytest.raises(CommentInputError, match="matches 2"):
            resolve_comment_range(DOC, "n")
        with pytest.raises(CommentInputError, match="not found in the document"):
            resolve_comment_range(DOC, "Intro", tab_id="t.missing")


class TestCreate:
    @pytest.mark.asyncio
    async def test_create_by_indices_reports_ids(self):
        service = _docs_service(
            reply={
                "insertComment": {
                    "commentThread": {
                        "commentId": "AAAB1",
                        "anchorId": "kix.anchor1",
                        "status": "OPEN",
                        "headPost": {"postId": "AAAB1.p0", "assigneeEmail": "ana@x.y"},
                        "plainTextQuote": "Numbers",
                    }
                }
            }
        )
        result = await _call(
            service,
            action="create",
            start_index=7,
            end_index=14,
            content="Please verify",
            assignee_email="ana@x.y",
        )
        request = _sent_request(service)["insertComment"]
        assert request["range"] == {"startIndex": 7, "endIndex": 14}
        assert request["assigneeEmailAddress"] == "ana@x.y"
        for fragment in (
            "comment_id=AAAB1",
            "post_id=AAAB1.p0",
            "anchor_id=kix.anchor1",
            "assignee=ana@x.y",
            'quote="Numbers"',
        ):
            assert fragment in result
        service.documents.return_value.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_by_anchor_text_resolves_range_from_live_document(self):
        service = _docs_service(
            reply={"insertComment": {"commentThread": {"commentId": "AAAB2"}}}
        )
        result = await _call(
            service, action="create", anchor_text="went up", content="Source?"
        )
        get_kwargs = service.documents.return_value.get.call_args.kwargs
        assert get_kwargs == {"documentId": DOC_ID, "includeTabsContent": True}
        request = _sent_request(service)["insertComment"]
        assert request["range"] == {"startIndex": 15, "endIndex": 22}
        assert "range 15-22" in result
        assert "comment_id=AAAB2" in result

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "kwargs, message",
        [
            ({"content": "x"}, "start_index and end_index, or anchor_text"),
            ({"content": "x", "start_index": 1}, "start_index and end_index"),
            (
                {
                    "content": "x",
                    "start_index": 1,
                    "end_index": 3,
                    "anchor_text": "Intro",
                },
                "not both",
            ),
            ({"start_index": 1, "end_index": 3}, "'content' is required"),
            ({"content": "x", "anchor_text": "zzz"}, "not found"),
        ],
    )
    async def test_create_validation_errors(self, kwargs, message):
        service = _docs_service()
        with pytest.raises(UserInputError, match=message):
            await _call(service, action="create", **kwargs)
        service.documents.return_value.batchUpdate.assert_not_called()


class TestThreadActions:
    @pytest.mark.asyncio
    async def test_reply_with_assignee(self):
        service = _docs_service(
            reply={
                "addCommentReply": {"post": {"postId": "p9", "assigneeEmail": "b@x.y"}}
            }
        )
        result = await _call(
            service,
            action="reply",
            comment_id="c1",
            content="Done",
            assignee_email="b@x.y",
        )
        assert _sent_request(service) == {
            "addCommentReply": {
                "commentId": "c1",
                "post": {"content": "Done", "assigneeEmail": "b@x.y"},
            }
        }
        assert "Replied to comment thread c1" in result
        assert "post_id=p9" in result

    @pytest.mark.asyncio
    async def test_reply_can_resolve_in_the_same_post(self):
        service = _docs_service(reply={"addCommentReply": {"post": {"postId": "p9"}}})
        result = await _call(
            service, action="reply", comment_id="c1", content="Fixed", resolve=True
        )
        assert _sent_request(service)["addCommentReply"]["post"] == {
            "commentAction": "RESOLVE",
            "content": "Fixed",
        }
        assert "and resolved it" in result

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "action, expected, verb",
        [("resolve", "RESOLVE", "Resolved"), ("reopen", "REOPEN", "Reopened")],
    )
    async def test_resolve_and_reopen_without_content(self, action, expected, verb):
        service = _docs_service(reply={"addCommentReply": {"post": {"postId": "p1"}}})
        result = await _call(service, action=action, comment_id="c1")
        assert _sent_request(service) == {
            "addCommentReply": {"commentId": "c1", "post": {"commentAction": expected}}
        }
        assert f"{verb} comment thread c1" in result

    @pytest.mark.asyncio
    async def test_update_post(self):
        service = _docs_service()
        result = await _call(
            service, action="update", comment_id="c1", post_id="p1", content="Edited"
        )
        assert _sent_request(service) == {
            "updateCommentPost": {
                "commentId": "c1",
                "postId": "p1",
                "content": "Edited",
            }
        }
        assert "Updated post p1" in result

    @pytest.mark.asyncio
    async def test_delete_thread_and_reply(self):
        service = _docs_service()
        result = await _call(service, action="delete", comment_id="c1")
        assert _sent_request(service) == {"deleteComment": {"commentId": "c1"}}
        assert "Deleted comment thread c1" in result

        service = _docs_service()
        result = await _call(
            service, action="delete_reply", comment_id="c1", post_id="p2"
        )
        assert _sent_request(service) == {
            "deleteCommentReply": {"commentId": "c1", "postId": "p2"}
        }
        assert "Deleted reply p2" in result

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "kwargs, message",
        [
            ({"action": "reply", "content": "x"}, "comment_id"),
            ({"action": "reply", "comment_id": "c1"}, "content"),
            ({"action": "resolve"}, "comment_id"),
            ({"action": "update", "comment_id": "c1", "content": "x"}, "post_id"),
            ({"action": "delete"}, "comment_id"),
            ({"action": "delete_reply", "comment_id": "c1"}, "post_id"),
        ],
    )
    async def test_missing_identifiers_are_rejected(self, kwargs, message):
        service = _docs_service()
        with pytest.raises(UserInputError, match=message):
            await _call(service, **kwargs)
        service.documents.return_value.batchUpdate.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_action_is_rejected(self):
        with pytest.raises(UserInputError, match="Invalid action"):
            await _call(_docs_service(), action="archive")


class TestList:
    @pytest.mark.asyncio
    async def test_list_paginates_drive_comments_and_shows_ids(self):
        drive_service = Mock()
        drive_service.comments.return_value.list.return_value.execute.side_effect = [
            {
                "comments": [
                    {
                        "id": "AAAB1",
                        "content": "Please verify",
                        "author": {"displayName": "Ana"},
                        "createdTime": "2026-10-08T10:00:00Z",
                        "resolved": False,
                        "anchor": '{"r":"kix.anchor1"}',
                        "assigneeEmailAddress": "bo@x.y",
                        "quotedFileContent": {"value": "Numbers"},
                        "replies": [
                            {
                                "id": "AAAB1.r1",
                                "content": "Looking",
                                "author": {"displayName": "Bo"},
                            }
                        ],
                    }
                ],
                "nextPageToken": "page2",
            },
            {
                "comments": [
                    {
                        "id": "AAAB2",
                        "content": "Old",
                        "author": {"displayName": "Cy"},
                        "resolved": True,
                        "replies": [
                            {"id": "AAAB2.r1", "content": "", "action": "resolve"}
                        ],
                    }
                ]
            },
        ]
        result = await _call(Mock(), drive_service, action="list")

        calls = drive_service.comments.return_value.list.call_args_list
        assert [c.kwargs["pageToken"] for c in calls] == [None, "page2"]
        assert calls[0].kwargs["fileId"] == DOC_ID
        assert "assigneeEmailAddress" in calls[0].kwargs["fields"]
        assert "anchor" in calls[0].kwargs["fields"]
        assert calls[0].kwargs["includeDeleted"] is False

        assert "Found 2 comment thread(s)" in result
        assert "comment_id=AAAB1 [open] by Ana" in result
        assert "assigned to bo@x.y" in result
        assert 'quoted: "Numbers"' in result
        assert "reply post_id=AAAB1.r1 by Bo: Looking" in result
        assert "comment_id=AAAB2 [resolved]" in result
        assert "(resolve)" in result

    @pytest.mark.asyncio
    async def test_list_can_hide_resolved_threads(self):
        drive_service = Mock()
        drive_service.comments.return_value.list.return_value.execute.return_value = {
            "comments": [{"id": "A", "resolved": True, "content": "x"}]
        }
        result = await _call(
            Mock(), drive_service, action="list", include_resolved=False
        )
        assert result == f"No open comments found on document {DOC_ID}."

    def test_format_empty_list(self):
        assert format_comment_list([], "doc1") == "No comments found on document doc1."
