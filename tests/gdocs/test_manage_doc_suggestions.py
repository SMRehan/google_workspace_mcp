"""Tests for manage_doc_suggestions and the suggestion collector."""

from unittest.mock import Mock

import pytest

from core.utils import UserInputError
from gdocs import docs_tools
from gdocs.docs_suggestions import (
    collect_suggestions,
    create_suggestion_action_request,
    format_suggestions,
    normalize_suggestion_ids,
)

DOC_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcd"
EMAIL = "user@example.com"


def _unwrap(tool):
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


def _run(start, text, **extra):
    return {
        "startIndex": start,
        "endIndex": start + len(text),
        "textRun": {"content": text, **extra},
    }


SUGGESTED_DOC = {
    "tabs": [
        {
            "tabProperties": {"tabId": "t.0"},
            "documentTab": {
                "suggestedDocumentStyleChanges": {"sug.docstyle": {}},
                "body": {
                    "content": [
                        {
                            "startIndex": 1,
                            "endIndex": 30,
                            "paragraph": {
                                "suggestedParagraphStyleChanges": {"sug.para": {}},
                                "elements": [
                                    _run(1, "Kept "),
                                    _run(
                                        6,
                                        "added text",
                                        suggestedInsertionIds=["sug.ins"],
                                    ),
                                    _run(16, " old", suggestedDeletionIds=["sug.del"]),
                                    _run(
                                        20,
                                        " styled\n",
                                        suggestedTextStyleChanges={"sug.style": {}},
                                    ),
                                    {
                                        "startIndex": 28,
                                        "endIndex": 29,
                                        "inlineObjectElement": {
                                            "inlineObjectId": "kix.img",
                                            "suggestedInsertionIds": ["sug.img"],
                                        },
                                    },
                                ],
                            },
                        },
                        {
                            "startIndex": 30,
                            "endIndex": 60,
                            "table": {
                                "tableRows": [
                                    {
                                        "startIndex": 31,
                                        "endIndex": 59,
                                        "suggestedInsertionIds": ["sug.row"],
                                        "tableCells": [
                                            {
                                                "startIndex": 32,
                                                "endIndex": 58,
                                                "content": [
                                                    {
                                                        "startIndex": 33,
                                                        "endIndex": 40,
                                                        "paragraph": {
                                                            "elements": [
                                                                _run(
                                                                    33,
                                                                    "in cell",
                                                                    suggestedInsertionIds=[
                                                                        "sug.cell"
                                                                    ],
                                                                )
                                                            ]
                                                        },
                                                    }
                                                ],
                                            }
                                        ],
                                    }
                                ]
                            },
                        },
                    ]
                },
                "footnotes": {
                    "kix.fn1": {
                        "content": [
                            {
                                "startIndex": 1,
                                "endIndex": 8,
                                "paragraph": {
                                    "elements": [
                                        _run(
                                            1,
                                            "footnote",
                                            suggestedDeletionIds=["sug.fn"],
                                        )
                                    ]
                                },
                            }
                        ]
                    }
                },
            },
            "childTabs": [
                {
                    "tabProperties": {"tabId": "t.1"},
                    "documentTab": {
                        "body": {
                            "content": [
                                {
                                    "startIndex": 1,
                                    "endIndex": 6,
                                    "paragraph": {
                                        "elements": [
                                            _run(
                                                1,
                                                "child",
                                                suggestedInsertionIds=["sug.child"],
                                            )
                                        ]
                                    },
                                }
                            ]
                        }
                    },
                }
            ],
        }
    ]
}


class TestCollectSuggestions:
    def test_collects_every_kind_with_location_and_snippet(self):
        by_id = {s["suggestion_id"]: s for s in collect_suggestions(SUGGESTED_DOC)}
        assert by_id["sug.ins"]["types"] == ["insertion"]
        assert by_id["sug.ins"]["start_index"] == 6
        assert by_id["sug.ins"]["end_index"] == 16
        assert by_id["sug.ins"]["snippet"] == "added text"
        assert by_id["sug.ins"]["tab_id"] == "t.0"
        assert by_id["sug.ins"]["segment_id"] is None

        assert by_id["sug.del"]["types"] == ["deletion"]
        assert by_id["sug.style"]["types"] == ["text_style"]
        assert by_id["sug.para"]["types"] == ["paragraph_style"]
        assert by_id["sug.img"]["types"] == ["insertion"]
        assert by_id["sug.img"]["snippet"] == "[inlineObjectElement]"
        assert by_id["sug.row"]["types"] == ["insertion"]
        assert by_id["sug.cell"]["snippet"] == "in cell"
        assert by_id["sug.docstyle"]["types"] == ["document_style"]
        assert by_id["sug.docstyle"]["start_index"] is None

        assert by_id["sug.fn"]["segment_id"] == "kix.fn1"
        assert by_id["sug.child"]["tab_id"] == "t.1"

    def test_same_id_across_runs_is_merged(self):
        doc = {
            "body": {
                "content": [
                    {
                        "startIndex": 1,
                        "endIndex": 11,
                        "paragraph": {
                            "elements": [
                                _run(1, "one ", suggestedInsertionIds=["sug.a"]),
                                _run(
                                    5,
                                    "two\n",
                                    suggestedInsertionIds=["sug.a"],
                                    suggestedTextStyleChanges={"sug.a": {}},
                                ),
                            ]
                        },
                    }
                ]
            }
        }
        suggestions = collect_suggestions(doc)
        assert len(suggestions) == 1
        assert suggestions[0]["types"] == ["insertion", "text_style"]
        assert (suggestions[0]["start_index"], suggestions[0]["end_index"]) == (1, 9)
        assert suggestions[0]["snippet"] == "one two "

    def test_legacy_document_without_tabs(self):
        doc = {
            "body": {
                "content": [
                    {
                        "startIndex": 1,
                        "endIndex": 5,
                        "paragraph": {
                            "elements": [
                                _run(1, "text", suggestedDeletionIds=["sug.legacy"])
                            ]
                        },
                    }
                ]
            }
        }
        assert [s["suggestion_id"] for s in collect_suggestions(doc)] == ["sug.legacy"]

    def test_no_suggestions(self):
        assert (
            collect_suggestions({"tabs": [{"documentTab": {"body": {"content": []}}}]})
            == []
        )


class TestFormatting:
    def test_format_lists_ids_types_and_snippets(self):
        text = format_suggestions(collect_suggestions(SUGGESTED_DOC), DOC_ID)
        assert text.startswith(f"Found 10 suggestion(s) in document {DOC_ID}:")
        assert (
            'suggestion_id=sug.ins type=insertion [6-16] (tab=t.0): "added text"'
            in text
        )
        assert (
            "suggestion_id=sug.fn type=deletion [1-9] (tab=t.0, segment=kix.fn1)"
            in text
        )
        assert "suggestion_id=sug.docstyle type=document_style (tab=t.0)" in text
        assert "action=accept, reject or delete" in text

    def test_format_filters_by_tab(self):
        text = format_suggestions(
            collect_suggestions(SUGGESTED_DOC), DOC_ID, tab_id="t.1"
        )
        assert "Found 1 suggestion(s)" in text
        assert "sug.child" in text
        assert "sug.ins" not in text

    def test_format_empty(self):
        assert format_suggestions([], DOC_ID, tab_id="t.9") == (
            f"No pending suggestions found in document {DOC_ID} in tab t.9."
        )


class TestNormalizeIds:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            (["a", "b", "a"], ["a", "b"]),
            ("a", ["a"]),
            ('["a", "b"]', ["a", "b"]),
            (None, []),
            ("  ", []),
        ],
    )
    def test_accepts_lists_strings_and_json(self, raw, expected):
        assert normalize_suggestion_ids(raw) == expected

    @pytest.mark.parametrize("raw", [[""], [1], {"a": 1}])
    def test_rejects_non_string_ids(self, raw):
        with pytest.raises(ValueError):
            normalize_suggestion_ids(raw)

    def test_action_request_shapes(self):
        assert create_suggestion_action_request("accept", "s1") == {
            "acceptSuggestion": {"suggestionId": "s1"}
        }
        assert create_suggestion_action_request("reject", "s1") == {
            "rejectSuggestion": {"suggestionId": "s1"}
        }
        assert create_suggestion_action_request("delete", "s1") == {
            "deleteSuggestion": {"suggestionId": "s1"}
        }
        with pytest.raises(ValueError):
            create_suggestion_action_request("approve", "s1")


def _service(doc=None):
    service = Mock()
    service.documents.return_value.get.return_value.execute.return_value = (
        doc if doc is not None else SUGGESTED_DOC
    )
    service.documents.return_value.batchUpdate.return_value.execute.return_value = {
        "replies": [{}]
    }
    return service


async def _call(service, **kwargs):
    return await _unwrap(docs_tools.manage_doc_suggestions)(
        service=service, user_google_email=EMAIL, document_id=DOC_ID, **kwargs
    )


class TestTool:
    @pytest.mark.asyncio
    async def test_list_fetches_inline_suggestions_view(self):
        service = _service()
        result = await _call(service, action="list")
        get_kwargs = service.documents.return_value.get.call_args.kwargs
        assert get_kwargs == {
            "documentId": DOC_ID,
            "includeTabsContent": True,
            "suggestionsViewMode": "SUGGESTIONS_INLINE",
        }
        assert "Found 10 suggestion(s)" in result
        service.documents.return_value.batchUpdate.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "action, request_name, verb",
        [
            ("accept", "acceptSuggestion", "Accepted"),
            ("reject", "rejectSuggestion", "Rejected"),
            ("delete", "deleteSuggestion", "Deleted"),
        ],
    )
    async def test_actions_send_one_request_per_id(self, action, request_name, verb):
        service = _service()
        result = await _call(service, action=action, suggestion_ids=["sug.a", "sug.b"])
        body = service.documents.return_value.batchUpdate.call_args.kwargs["body"]
        assert body == {
            "requests": [
                {request_name: {"suggestionId": "sug.a"}},
                {request_name: {"suggestionId": "sug.b"}},
            ]
        }
        assert f"{verb} 2 suggestion(s)" in result
        assert "sug.a, sug.b" in result
        service.documents.return_value.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_accepts_json_encoded_id_list(self):
        service = _service()
        await _call(service, action="accept", suggestion_ids='["sug.a"]')
        body = service.documents.return_value.batchUpdate.call_args.kwargs["body"]
        assert body["requests"] == [{"acceptSuggestion": {"suggestionId": "sug.a"}}]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("ids", [None, [], ""])
    async def test_actions_require_ids(self, ids):
        service = _service()
        with pytest.raises(UserInputError, match="suggestion_ids"):
            await _call(service, action="reject", suggestion_ids=ids)
        service.documents.return_value.batchUpdate.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_ids_and_actions_are_rejected(self):
        with pytest.raises(UserInputError, match="non-empty strings"):
            await _call(_service(), action="accept", suggestion_ids=[""])
        with pytest.raises(UserInputError, match="Invalid action"):
            await _call(_service(), action="approve", suggestion_ids=["x"])
