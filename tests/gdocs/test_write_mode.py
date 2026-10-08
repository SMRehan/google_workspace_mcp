"""
write_mode plumbing: EDIT must leave the batchUpdate body exactly as before,
SUGGEST must add writeControl.writeMode so the Docs API records suggestions.
"""

from unittest.mock import Mock

import pytest

from core.utils import UserInputError
from gdocs import docs_tools
from gdocs.docs_helpers import build_batch_update_body, validate_write_mode
from gdocs.managers.batch_operation_manager import BatchOperationManager

DOC_ID = "a" * 25
SUGGEST_CONTROL = {"writeMode": "SUGGEST"}


def _unwrap(tool):
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


def _service():
    service = Mock()
    service.documents.return_value.get.return_value.execute.return_value = {
        "body": {"content": []},
        "revisionId": "rev1",
    }
    service.documents.return_value.batchUpdate.return_value.execute.return_value = {
        "replies": [{"replaceAllText": {"occurrencesChanged": 1}}]
    }
    return service


def _bodies(service):
    return [
        call.kwargs["body"]
        for call in service.documents.return_value.batchUpdate.call_args_list
    ]


class TestHelpers:
    def test_edit_body_has_no_write_control(self):
        assert build_batch_update_body([{"x": 1}]) == {"requests": [{"x": 1}]}
        assert build_batch_update_body([{"x": 1}], "edit") == {"requests": [{"x": 1}]}

    def test_suggest_body_adds_write_control(self):
        assert build_batch_update_body([{"x": 1}], "SUGGEST") == {
            "requests": [{"x": 1}],
            "writeControl": SUGGEST_CONTROL,
        }

    @pytest.mark.parametrize("bad", ["REVIEW", "", None, 3])
    def test_invalid_write_mode_is_rejected(self, bad):
        with pytest.raises(ValueError, match="write_mode"):
            validate_write_mode(bad)


class TestTools:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("write_mode", ["EDIT", "SUGGEST"])
    async def test_modify_doc_text(self, write_mode):
        service = _service()
        await _unwrap(docs_tools.modify_doc_text)(
            service=service,
            user_google_email="u@example.com",
            document_id=DOC_ID,
            start_index=1,
            text="Hello",
            write_mode=write_mode,
        )
        body = _bodies(service)[0]
        assert body["requests"][0]["insertText"]["text"] == "Hello"
        assert body.get("writeControl") == (
            SUGGEST_CONTROL if write_mode == "SUGGEST" else None
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("write_mode", ["EDIT", "SUGGEST"])
    async def test_find_and_replace_doc(self, write_mode):
        service = _service()
        result = await _unwrap(docs_tools.find_and_replace_doc)(
            service=service,
            user_google_email="u@example.com",
            document_id=DOC_ID,
            find_text="Q3",
            replace_text="Q4",
            write_mode=write_mode,
        )
        assert "Replaced 1 occurrence" in result
        body = _bodies(service)[0]
        assert "replaceAllText" in body["requests"][0]
        assert body.get("writeControl") == (
            SUGGEST_CONTROL if write_mode == "SUGGEST" else None
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("write_mode", ["EDIT", "SUGGEST"])
    async def test_insert_doc_elements(self, write_mode):
        service = _service()
        await _unwrap(docs_tools.insert_doc_elements)(
            service=service,
            user_google_email="u@example.com",
            document_id=DOC_ID,
            element_type="page_break",
            index=5,
            write_mode=write_mode,
        )
        body = _bodies(service)[0]
        assert "insertPageBreak" in body["requests"][0]
        assert body.get("writeControl") == (
            SUGGEST_CONTROL if write_mode == "SUGGEST" else None
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("write_mode", ["EDIT", "SUGGEST"])
    async def test_update_paragraph_style(self, write_mode):
        service = _service()
        await _unwrap(docs_tools.update_paragraph_style)(
            service=service,
            user_google_email="u@example.com",
            document_id=DOC_ID,
            start_index=1,
            end_index=10,
            heading_level=1,
            write_mode=write_mode,
        )
        body = _bodies(service)[0]
        assert "updateParagraphStyle" in body["requests"][0]
        assert body.get("writeControl") == (
            SUGGEST_CONTROL if write_mode == "SUGGEST" else None
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("write_mode", ["EDIT", "SUGGEST"])
    async def test_batch_update_doc(self, write_mode):
        service = _service()
        result = await _unwrap(docs_tools.batch_update_doc)(
            service=service,
            user_google_email="u@example.com",
            document_id=DOC_ID,
            operations=[
                {"type": "find_replace", "find_text": "a", "replace_text": "b"}
            ],
            write_mode=write_mode,
        )
        assert "Error" not in result
        body = _bodies(service)[0]
        assert body.get("writeControl") == (
            SUGGEST_CONTROL if write_mode == "SUGGEST" else None
        )
        assert ("manage_doc_suggestions" in result) == (write_mode == "SUGGEST")

    @pytest.mark.asyncio
    async def test_batch_update_doc_rejects_bad_write_mode(self):
        service = _service()
        result = await _unwrap(docs_tools.batch_update_doc)(
            service=service,
            user_google_email="u@example.com",
            document_id=DOC_ID,
            operations=[
                {"type": "find_replace", "find_text": "a", "replace_text": "b"}
            ],
            write_mode="TRACK",
        )
        assert result.startswith("Error:")
        assert "write_mode" in result
        service.documents.return_value.batchUpdate.assert_not_called()

    @pytest.mark.asyncio
    async def test_modify_doc_text_rejects_bad_write_mode(self):
        with pytest.raises((UserInputError, ValueError), match="write_mode"):
            await _unwrap(docs_tools.modify_doc_text)(
                service=_service(),
                user_google_email="u@example.com",
                document_id=DOC_ID,
                start_index=1,
                text="Hello",
                write_mode="TRACK",
            )


class TestManagerFollowUp:
    @pytest.mark.asyncio
    async def test_footnote_follow_up_inherits_suggest_mode(self):
        service = _service()
        service.documents.return_value.batchUpdate.return_value.execute.return_value = {
            "replies": [{"createFootnote": {"footnoteId": "kix.fn"}}]
        }
        manager = BatchOperationManager(service)
        success, message, metadata = await manager.execute_batch_operations(
            "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcd",
            [{"type": "create_footnote", "end_of_segment": True, "text": "Note"}],
            write_mode="SUGGEST",
        )
        assert success, message
        assert metadata["write_mode"] == "SUGGEST"
        assert "as tracked suggestions" in message
        bodies = _bodies(service)
        assert len(bodies) == 2
        assert all(body["writeControl"] == SUGGEST_CONTROL for body in bodies)
