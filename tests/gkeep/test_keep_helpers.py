"""Unit tests for gkeep.keep_helpers: rendering, request bodies, names, errors."""

import os
import sys
from unittest.mock import Mock

import pytest
from fastmcp.exceptions import ToolError
from googleapiclient.errors import HttpError

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from core.utils import UserInputError
from gkeep import keep_helpers as h


def _http_error(status, body=b'{"error": {"message": "nope"}}'):
    resp = Mock()
    resp.status = status
    resp.reason = "Forbidden" if status == 403 else "Error"
    return HttpError(resp, body, uri="https://keep.googleapis.com/v1/notes")


def _text_note(**overrides):
    note = {
        "name": "notes/t1",
        "title": "Shopping ideas",
        "body": {"text": {"text": "Buy oat milk\nand bread"}},
        "createTime": "2026-01-01T00:00:00Z",
        "updateTime": "2026-01-02T00:00:00Z",
        "permissions": [
            {"name": "notes/t1/permissions/p1", "email": "me@x.com", "role": "OWNER"}
        ],
    }
    note.update(overrides)
    return note


def _list_note(**overrides):
    note = {
        "name": "notes/l1",
        "title": "Packing",
        "body": {
            "list": {
                "listItems": [
                    {
                        "text": {"text": "Clothes"},
                        "checked": False,
                        "childListItems": [
                            {"text": {"text": "Socks"}, "checked": True},
                            {"text": {"text": "Hat"}, "checked": False},
                        ],
                    },
                    {"text": {"text": "Passport"}, "checked": True},
                ]
            }
        },
        "attachments": [{"name": "notes/l1/attachments/a1", "mimeType": ["image/png"]}],
        "updateTime": "2026-01-03T00:00:00Z",
    }
    note.update(overrides)
    return note


class TestNoteInspection:
    def test_note_kind(self):
        assert h.note_kind(_text_note()) == "text"
        assert h.note_kind(_list_note()) == "list"
        assert h.note_kind({"name": "notes/e"}) == "empty"

    def test_count_list_items_includes_nested(self):
        items = _list_note()["body"]["list"]["listItems"]
        assert h.count_list_items(items) == (4, 2)

    def test_count_list_items_handles_none(self):
        assert h.count_list_items(None) == (0, 0)


class TestRendering:
    def test_summary_for_text_note(self):
        out = h.format_note_summary(_text_note())
        assert "Shopping ideas (ID: notes/t1)" in out
        assert "Kind: text (22 chars)" in out
        assert "Attachments" not in out
        assert "Updated: 2026-01-02T00:00:00Z" in out

    def test_summary_for_list_note_with_attachments_and_trash(self):
        out = h.format_note_summary(_list_note(trashed=True))
        assert "Kind: list (2/4 checked)" in out
        assert "Attachments: 1" in out
        assert "TRASHED" in out

    def test_summary_untitled(self):
        assert "(untitled)" in h.format_note_summary({"name": "notes/x"})

    def test_list_rendering_with_filter_and_token(self):
        out = h.format_notes_list(
            [_text_note(), _list_note()], "u@x.com", "trashed=false", "tok"
        )
        assert out.startswith("Keep notes for u@x.com (filter: trashed=false):")
        assert "notes/t1" in out and "notes/l1" in out
        assert out.rstrip().endswith("Next page token: tok")

    def test_list_rendering_empty(self):
        assert (
            h.format_notes_list([], "u@x.com", None, None)
            == "No Keep notes found for u@x.com."
        )
        out = h.format_notes_list([], "u@x.com", "trashed=true", "tok")
        assert "matched filter 'trashed=true'" in out
        assert "Next page token: tok" in out

    def test_render_list_items_nested_marks(self):
        items = _list_note()["body"]["list"]["listItems"]
        assert h.render_list_items(items) == [
            "[ ] Clothes",
            "  [x] Socks",
            "  [ ] Hat",
            "[x] Passport",
        ]

    def test_detail_text_note(self):
        out = h.format_note_detail(_text_note())
        assert "Title: Shopping ideas" in out
        assert "Kind: text" in out
        assert "Text:\nBuy oat milk\nand bread" in out
        assert "Attachments: none" in out
        assert "- me@x.com: OWNER (ID: notes/t1/permissions/p1)" in out

    def test_detail_list_note_with_attachments(self):
        out = h.format_note_detail(_list_note())
        assert "Checklist (2/4 checked):" in out
        assert "  [x] Socks" in out
        assert "Attachments (1):" in out
        assert "- notes/l1/attachments/a1 [image/png]" in out
        assert "download_attachment=" in out
        assert "No permissions listed on notes/l1." in out

    def test_detail_empty_note_and_trashed(self):
        out = h.format_note_detail(
            {"name": "notes/e", "trashed": True, "trashTime": "2026-02-01T00:00:00Z"}
        )
        assert "Body: (empty)" in out
        assert "Trashed: yes (trashTime: 2026-02-01T00:00:00Z)" in out

    def test_permission_line_fallbacks(self):
        assert "- g@x.com: WRITER" in h.format_permission_line(
            {"group": {"email": "g@x.com"}, "role": "WRITER", "name": "n"}
        )
        assert "(family)" in h.format_permission_line({"family": {}, "name": "n"})
        assert "[deleted account]" in h.format_permission_line(
            {"email": "d@x.com", "deleted": True, "name": "n"}
        )


class TestResourceNames:
    def test_normalize_note_name(self):
        assert h.normalize_note_name("abc") == "notes/abc"
        assert h.normalize_note_name(" notes/abc ") == "notes/abc"

    @pytest.mark.parametrize("bad", ["", "   ", "notes/a/b", "folders/x", None])
    def test_normalize_note_name_rejects(self, bad):
        with pytest.raises(UserInputError):
            h.normalize_note_name(bad)

    def test_normalize_attachment_name(self):
        assert h.normalize_attachment_name("notes/n", "a") == "notes/n/attachments/a"
        assert (
            h.normalize_attachment_name("notes/n", "attachments/a")
            == "notes/n/attachments/a"
        )
        assert (
            h.normalize_attachment_name("notes/n", "notes/n/attachments/a")
            == "notes/n/attachments/a"
        )

    def test_normalize_attachment_name_rejects_other_note(self):
        with pytest.raises(UserInputError, match="does not belong"):
            h.normalize_attachment_name("notes/n", "notes/other/attachments/a")
        with pytest.raises(UserInputError):
            h.normalize_attachment_name("notes/n", "")

    def test_normalize_permission_names(self):
        assert h.normalize_permission_names(
            "notes/n", ["p1", "permissions/p2", "notes/n/permissions/p3"]
        ) == [
            "notes/n/permissions/p1",
            "notes/n/permissions/p2",
            "notes/n/permissions/p3",
        ]

    def test_normalize_permission_names_rejects(self):
        with pytest.raises(UserInputError):
            h.normalize_permission_names("notes/n", ["notes/other/permissions/p"])
        with pytest.raises(UserInputError):
            h.normalize_permission_names("notes/n", [""])


class TestRequestBodies:
    def test_text_body(self):
        assert h.build_create_note_request("T", "hello", None) == {
            "title": "T",
            "body": {"text": {"text": "hello"}},
        }

    def test_list_body_with_children_and_defaults(self):
        body = h.build_create_note_request(
            None,
            None,
            [
                {"text": "Milk"},
                {
                    "text": "Veg",
                    "checked": True,
                    "children": [{"text": "Kale", "checked": False}],
                },
            ],
        )
        assert "title" not in body
        assert body["body"] == {
            "list": {
                "listItems": [
                    {"text": {"text": "Milk"}, "checked": False},
                    {
                        "text": {"text": "Veg"},
                        "checked": True,
                        "childListItems": [
                            {"text": {"text": "Kale"}, "checked": False}
                        ],
                    },
                ]
            }
        }

    def test_list_body_accepts_api_style_children_key(self):
        body = h.build_note_body(
            None, [{"text": "A", "childListItems": [{"text": "B"}]}]
        )
        assert body["list"]["listItems"][0]["childListItems"][0]["text"] == {
            "text": "B"
        }

    def test_body_requires_one_of_text_or_list(self):
        with pytest.raises(UserInputError, match="needs a body"):
            h.build_note_body(None, None)
        with pytest.raises(UserInputError, match="not both"):
            h.build_note_body("x", [{"text": "y"}])

    @pytest.mark.parametrize(
        "items, match",
        [
            (["Milk"], r"list_items\[0\] must be an object"),
            ([{"checked": True}], r"list_items\[0\].text"),
            ([{"text": "a", "checked": "yes"}], r"checked must be true or false"),
            ([{"text": "a", "children": "b"}], r"children must be a list"),
            ([{"text": "a", "children": [{"text": ""}]}], r"children\[0\].text"),
        ],
    )
    def test_list_item_validation(self, items, match):
        with pytest.raises(UserInputError, match=match):
            h.build_note_body(None, items)

    def test_permission_requests_dedupe_and_role(self):
        reqs = h.build_permission_requests(
            "notes/n", ["A@x.com", "a@x.com ", "b@x.com"]
        )
        assert reqs == [
            {"parent": "notes/n", "permission": {"email": "a@x.com", "role": "WRITER"}},
            {"parent": "notes/n", "permission": {"email": "b@x.com", "role": "WRITER"}},
        ]

    def test_permission_requests_reject_bad_email(self):
        with pytest.raises(UserInputError, match="Invalid email"):
            h.build_permission_requests("notes/n", ["nope"])
        with pytest.raises(UserInputError, match="At least one"):
            h.build_permission_requests("notes/n", [])


class TestAttachments:
    def test_is_text_like_mime_type(self):
        assert h.is_text_like_mime_type("text/plain")
        assert h.is_text_like_mime_type("application/json")
        assert h.is_text_like_mime_type("application/ld+json")
        assert not h.is_text_like_mime_type("image/png")
        assert not h.is_text_like_mime_type("")

    def test_resolve_mime_type(self):
        att = {"name": "notes/n/attachments/a", "mimeType": ["image/png"]}
        assert h.resolve_attachment_mime_type(att, None) == "image/png"
        assert h.resolve_attachment_mime_type(att, "image/png") == "image/png"
        with pytest.raises(UserInputError, match="not available as"):
            h.resolve_attachment_mime_type(att, "image/jpeg")

    def test_resolve_mime_type_ambiguous_or_missing(self):
        multi = {"name": "x", "mimeType": ["image/png", "image/jpeg"]}
        with pytest.raises(UserInputError, match="several MIME types"):
            h.resolve_attachment_mime_type(multi, None)
        with pytest.raises(UserInputError, match="lists no MIME types"):
            h.resolve_attachment_mime_type({"name": "x"}, None)

    def test_find_attachment(self):
        note = _list_note()
        assert h.find_attachment(note, "notes/l1/attachments/a1") is not None
        assert h.find_attachment(note, "notes/l1/attachments/zz") is None


class TestErrorHints:
    def test_403_unavailable_is_detected(self):
        err = _http_error(
            403, b'{"error": {"message": "Keep API is not available for this account"}}'
        )
        assert h.is_keep_unavailable_error(err)

    def test_403_other_reason_is_not(self):
        err = _http_error(403, b'{"error": {"message": "Insufficient scope"}}')
        assert not h.is_keep_unavailable_error(err)

    def test_non_403_is_not(self):
        err = _http_error(404, b'{"error": {"message": "not available"}}')
        assert not h.is_keep_unavailable_error(err)

    @pytest.mark.asyncio
    async def test_execute_wraps_unavailable_403_with_workspace_hint(self):
        request = Mock()
        request.execute.side_effect = _http_error(
            403, b'{"error": {"message": "accessNotConfigured"}}'
        )
        with pytest.raises(ToolError, match="Google Workspace accounts only"):
            await h.execute_keep_request(request)

    @pytest.mark.asyncio
    async def test_execute_passes_other_http_errors_through(self):
        request = Mock()
        request.execute.side_effect = _http_error(404)
        with pytest.raises(HttpError):
            await h.execute_keep_request(request)

    @pytest.mark.asyncio
    async def test_execute_returns_result(self):
        request = Mock()
        request.execute.return_value = {"ok": True}
        assert await h.execute_keep_request(request) == {"ok": True}
