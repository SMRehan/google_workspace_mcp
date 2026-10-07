"""
Tests for the batch_update_doc operations added for smart chips, footnotes,
dropdowns, image replacement, header/footer deletion, named styles and
positioned objects.

Covers request builders, validate_operation, the Pydantic schemas, and the
batch manager integration (anchors, footnote follow-up write, reply surfacing).
"""

from unittest.mock import Mock

import pytest
from pydantic import TypeAdapter, ValidationError

from gdocs import docs_tools
from gdocs.docs_helpers import (
    INSERTION_OPERATION_TYPES,
    create_create_footnote_request,
    create_delete_footer_request,
    create_delete_header_request,
    create_delete_positioned_object_request,
    create_dropdown_definition_request,
    create_insert_date_request,
    create_insert_dropdown_request,
    create_insert_person_request,
    create_insert_rich_link_request,
    create_replace_image_request,
    create_update_named_style_request,
    iso_date_to_rfc3339,
    validate_operation,
)
from gdocs.managers.batch_operation_manager import BatchOperationManager
from gdocs.operation_schemas import BatchDocOperations

DOC_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcd"
OPERATIONS = TypeAdapter(BatchDocOperations)


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


BODY = {
    "content": [
        _paragraph(1, "Intro\n"),
        _paragraph(7, "Results\n", "HEADING_2"),
        _paragraph(15, "Numbers went up.\n"),
    ]
}


def _service(replies=None):
    service = Mock()
    service.documents.return_value.get.return_value.execute.return_value = {
        "body": BODY,
        "revisionId": "rev1",
    }
    service.documents.return_value.batchUpdate.return_value.execute.return_value = {
        "replies": replies if replies is not None else [{}]
    }
    return service


def _batch_bodies(service):
    return [
        call.kwargs["body"]
        for call in service.documents.return_value.batchUpdate.call_args_list
    ]


class TestInsertPersonRequest:
    def test_at_index_with_name(self):
        request = create_insert_person_request(5, "ana@example.com", name="Ana")
        assert request == {
            "insertPerson": {
                "personProperties": {"email": "ana@example.com", "name": "Ana"},
                "location": {"index": 5},
            }
        }

    def test_end_of_segment_in_header(self):
        request = create_insert_person_request(
            None, "ana@example.com", segment_id="kix.h1", end_of_segment=True
        )
        assert request["insertPerson"]["endOfSegmentLocation"] == {
            "segmentId": "kix.h1"
        }
        assert "name" not in request["insertPerson"]["personProperties"]

    def test_rejects_non_email(self):
        with pytest.raises(ValueError, match="email"):
            create_insert_person_request(5, "not-an-email")


class TestInsertDateRequest:
    @pytest.mark.parametrize(
        "date, expected",
        [
            ("2026-10-08", "2026-10-08T00:00:00Z"),
            ("2026-10-08T15:30:00Z", "2026-10-08T15:30:00Z"),
            ("2026-10-08T15:30:00+02:00", "2026-10-08T13:30:00Z"),
            ("2026-10-08T15:30:00", "2026-10-08T15:30:00Z"),
        ],
    )
    def test_iso_dates_become_rfc3339_utc(self, date, expected):
        assert iso_date_to_rfc3339(date) == expected

    def test_rejects_garbage(self):
        with pytest.raises(ValueError, match="ISO 8601"):
            iso_date_to_rfc3339("next tuesday")

    def test_request_shape(self):
        request = create_insert_date_request(
            7,
            "2026-10-08",
            date_format="date_format_iso8601",
            time_zone_id="America/New_York",
            tab_id="t.1",
        )
        assert request == {
            "insertDate": {
                "dateElementProperties": {
                    "timestamp": "2026-10-08T00:00:00Z",
                    "dateFormat": "DATE_FORMAT_ISO8601",
                    "timeZoneId": "America/New_York",
                },
                "location": {"index": 7, "tabId": "t.1"},
            }
        }

    def test_rejects_unknown_date_format(self):
        with pytest.raises(ValueError, match="date_format"):
            create_insert_date_request(7, "2026-10-08", date_format="DD/MM")


class TestInsertRichLinkRequest:
    def test_request_shape(self):
        request = create_insert_rich_link_request(
            None,
            "https://docs.google.com/document/d/abc/edit",
            title="Spec",
            end_of_segment=True,
        )
        assert request == {
            "insertRichLink": {
                "richLinkProperties": {
                    "uri": "https://docs.google.com/document/d/abc/edit",
                    "title": "Spec",
                },
                "endOfSegmentLocation": {},
            }
        }

    def test_rejects_non_http_uri(self):
        with pytest.raises(ValueError, match="http"):
            create_insert_rich_link_request(1, "ftp://example.com/file")


class TestCreateFootnoteRequest:
    def test_at_index(self):
        assert create_create_footnote_request(12) == {
            "createFootnote": {"location": {"index": 12}}
        }

    def test_end_of_segment_with_tab(self):
        assert create_create_footnote_request(
            None, tab_id="t.1", end_of_segment=True
        ) == {"createFootnote": {"endOfSegmentLocation": {"tabId": "t.1"}}}


class TestDropdownRequests:
    def test_definition_from_strings(self):
        request = create_dropdown_definition_request(["Draft", "Final"], title="Status")
        definition = request["createDropdownDefinition"]["dropdownDefinition"]
        assert definition["dropdownDefinitionProperties"] == {
            "options": [{"displayValue": "Draft"}, {"displayValue": "Final"}],
            "title": "Status",
        }
        assert "dropdownDefinitionId" not in definition

    def test_definition_with_ids_and_tab(self):
        request = create_dropdown_definition_request(
            [
                {"display_value": "Draft", "option_id": "dropdownItem.draft"},
                "Final",
            ],
            dropdown_definition_id="kix.status",
            tab_id="t.1",
        )
        inner = request["createDropdownDefinition"]
        assert inner["tabId"] == "t.1"
        assert inner["dropdownDefinition"]["dropdownDefinitionId"] == "kix.status"
        assert inner["dropdownDefinition"]["dropdownDefinitionProperties"]["options"][
            0
        ] == {"displayValue": "Draft", "optionId": "dropdownItem.draft"}

    @pytest.mark.parametrize(
        "options, message",
        [
            (["Only"], "between 2 and 50"),
            (["A", ""], "display_value"),
            (["A", {"display_value": "B", "option_id": "bad"}], "option_id"),
            (
                [
                    {"display_value": "A", "option_id": "dropdownItem.x1"},
                    {"display_value": "B", "option_id": "dropdownItem.x1"},
                ],
                "duplicated",
            ),
        ],
    )
    def test_definition_rejects_bad_options(self, options, message):
        with pytest.raises(ValueError, match=message):
            create_dropdown_definition_request(options)

    def test_definition_rejects_bad_id(self):
        with pytest.raises(ValueError, match="dropdown_definition_id"):
            create_dropdown_definition_request(
                ["A", "B"], dropdown_definition_id="status"
            )

    def test_insert_dropdown(self):
        request = create_insert_dropdown_request(
            None,
            "kix.status",
            selected_option_id="dropdownItem.draft",
            end_of_segment=True,
        )
        assert request == {
            "insertDropdown": {
                "dropdownDefinitionId": "kix.status",
                "selectedOptionId": "dropdownItem.draft",
                "endOfSegmentLocation": {},
            }
        }

    def test_insert_dropdown_requires_definition_id(self):
        with pytest.raises(ValueError, match="dropdown_definition_id"):
            create_insert_dropdown_request(1, "")


class TestObjectAndSegmentRequests:
    def test_replace_image(self):
        request = create_replace_image_request(
            "kix.img1",
            "https://example.com/new.png",
            image_replace_method="center_crop",
            tab_id="t.1",
        )
        assert request == {
            "replaceImage": {
                "imageObjectId": "kix.img1",
                "uri": "https://example.com/new.png",
                "imageReplaceMethod": "CENTER_CROP",
                "tabId": "t.1",
            }
        }

    def test_replace_image_rejects_bad_method(self):
        with pytest.raises(ValueError, match="image_replace_method"):
            create_replace_image_request(
                "kix.img1", "https://example.com/x.png", image_replace_method="FIT"
            )

    def test_delete_header_footer_positioned_object(self):
        assert create_delete_header_request("kix.h1") == {
            "deleteHeader": {"headerId": "kix.h1"}
        }
        assert create_delete_footer_request("kix.f1", tab_id="t.1") == {
            "deleteFooter": {"footerId": "kix.f1", "tabId": "t.1"}
        }
        assert create_delete_positioned_object_request("kix.po1") == {
            "deletePositionedObject": {"objectId": "kix.po1"}
        }

    @pytest.mark.parametrize(
        "builder",
        [
            create_delete_header_request,
            create_delete_footer_request,
            create_delete_positioned_object_request,
        ],
    )
    def test_delete_requests_require_an_id(self, builder):
        with pytest.raises(ValueError):
            builder("")


class TestUpdateNamedStyleRequest:
    def test_text_and_paragraph_fields(self):
        request = create_update_named_style_request(
            "heading_2",
            bold=True,
            font_size=16,
            alignment="CENTER",
            space_above=12,
            tab_id="t.1",
        )
        inner = request["updateNamedStyle"]
        assert inner["tabId"] == "t.1"
        assert inner["namedStyle"]["namedStyleType"] == "HEADING_2"
        assert inner["namedStyle"]["textStyle"] == {
            "bold": True,
            "fontSize": {"magnitude": 16, "unit": "PT"},
        }
        assert inner["namedStyle"]["paragraphStyle"]["alignment"] == "CENTER"
        assert inner["fields"].split(",") == [
            "namedStyleType",
            "textStyle.bold",
            "textStyle.fontSize",
            "paragraphStyle.alignment",
            "paragraphStyle.spaceAbove",
        ]

    def test_no_style_fields_returns_none(self):
        assert create_update_named_style_request("NORMAL_TEXT") is None

    def test_rejects_unknown_named_style(self):
        with pytest.raises(ValueError, match="named_style_type"):
            create_update_named_style_request("HEADING_9", bold=True)


class TestValidateOperation:
    @pytest.mark.parametrize(
        "operation",
        [
            {"type": "insert_person", "email": "a@b.c", "index": 3},
            {"type": "insert_date", "date": "2026-01-01", "end_of_segment": True},
            {"type": "insert_rich_link", "uri": "https://x", "anchor_text": "foo"},
            {"type": "create_footnote", "after_heading": "Results"},
            {"type": "create_dropdown_definition", "options": ["A", "B"]},
            {"type": "insert_dropdown", "dropdown_definition_id": "kix.a", "index": 2},
            {"type": "replace_image", "image_object_id": "kix.i", "uri": "https://x"},
            {"type": "delete_header", "header_id": "kix.h"},
            {"type": "delete_footer", "footer_id": "kix.f"},
            {"type": "update_named_style", "named_style_type": "TITLE", "bold": True},
            {"type": "delete_positioned_object", "object_id": "kix.p"},
        ],
    )
    def test_accepts_complete_operations(self, operation):
        assert validate_operation(operation) == (True, "")

    @pytest.mark.parametrize(
        "operation, missing",
        [
            ({"type": "insert_person", "index": 1}, "email"),
            ({"type": "insert_date", "index": 1}, "date"),
            ({"type": "insert_rich_link", "index": 1}, "uri"),
            ({"type": "create_dropdown_definition"}, "options"),
            ({"type": "insert_dropdown", "index": 1}, "dropdown_definition_id"),
            ({"type": "replace_image", "uri": "https://x"}, "image_object_id"),
            ({"type": "delete_header"}, "header_id"),
            ({"type": "delete_footer"}, "footer_id"),
            ({"type": "update_named_style", "bold": True}, "named_style_type"),
            ({"type": "delete_positioned_object"}, "object_id"),
        ],
    )
    def test_reports_missing_required_fields(self, operation, missing):
        is_valid, message = validate_operation(operation)
        assert not is_valid
        assert missing in message

    def test_new_insertion_types_need_exactly_one_location(self):
        for op_type in (
            "insert_person",
            "insert_date",
            "insert_rich_link",
            "create_footnote",
            "insert_dropdown",
        ):
            assert op_type in INSERTION_OPERATION_TYPES
        is_valid, message = validate_operation(
            {"type": "create_footnote", "index": 3, "end_of_segment": True}
        )
        assert not is_valid
        assert "exactly one" in message
        is_valid, message = validate_operation(
            {"type": "insert_person", "email": "a@b.c"}
        )
        assert not is_valid


class TestOperationSchemas:
    def test_schema_accepts_every_new_operation(self):
        operations = OPERATIONS.validate_python(
            [
                {"type": "insert_person", "end_of_segment": True, "email": "a@b.c"},
                {
                    "type": "insert_date",
                    "index": 4,
                    "date": "2026-10-08",
                    "date_format": "DATE_FORMAT_ISO8601",
                },
                {"type": "insert_rich_link", "anchor_text": "spec", "uri": "https://x"},
                {"type": "create_footnote", "after_heading": "Results", "text": "note"},
                {
                    "type": "create_dropdown_definition",
                    "options": [
                        "A",
                        {"display_value": "B", "option_id": "dropdownItem.b1"},
                    ],
                },
                {
                    "type": "insert_dropdown",
                    "end_of_segment": True,
                    "dropdown_definition_id": "kix.s",
                },
                {
                    "type": "replace_image",
                    "image_object_id": "kix.i",
                    "uri": "https://x",
                },
                {"type": "delete_header", "header_id": "kix.h"},
                {"type": "delete_footer", "footer_id": "kix.f"},
                {
                    "type": "update_named_style",
                    "named_style_type": "HEADING_1",
                    "italic": True,
                },
                {"type": "delete_positioned_object", "object_id": "kix.p"},
            ]
        )
        assert [op.type for op in operations][:3] == [
            "insert_person",
            "insert_date",
            "insert_rich_link",
        ]

    def test_update_named_style_needs_a_style_field(self):
        with pytest.raises(ValidationError, match="at least one"):
            OPERATIONS.validate_python(
                [{"type": "update_named_style", "named_style_type": "HEADING_1"}]
            )

    def test_insertion_operations_need_one_location(self):
        with pytest.raises(ValidationError, match="exactly one"):
            OPERATIONS.validate_python([{"type": "insert_date", "date": "2026-01-01"}])

    def test_dropdown_definition_needs_two_options(self):
        with pytest.raises(ValidationError):
            OPERATIONS.validate_python(
                [{"type": "create_dropdown_definition", "options": ["Only"]}]
            )

    def test_extra_fields_are_rejected(self):
        with pytest.raises(ValidationError):
            OPERATIONS.validate_python(
                [{"type": "delete_header", "header_id": "kix.h", "bogus": 1}]
            )


class TestBatchManagerIntegration:
    @pytest.mark.asyncio
    async def test_person_chip_anchor_resolves_to_an_index(self):
        service = _service()
        manager = BatchOperationManager(service)
        success, message, _ = await manager.execute_batch_operations(
            DOC_ID,
            [{"type": "insert_person", "after_heading": "Results", "email": "a@b.c"}],
        )
        assert success, message
        request = _batch_bodies(service)[0]["requests"][0]["insertPerson"]
        assert request["location"] == {"index": 15}
        assert request["personProperties"] == {"email": "a@b.c"}

    @pytest.mark.asyncio
    async def test_footnote_text_is_written_in_a_follow_up_batch(self):
        service = _service(replies=[{"createFootnote": {"footnoteId": "kix.fn1"}}])
        manager = BatchOperationManager(service)
        success, message, metadata = await manager.execute_batch_operations(
            DOC_ID,
            [{"type": "create_footnote", "anchor_text": "Numbers", "text": "Source."}],
        )
        assert success, message
        bodies = _batch_bodies(service)
        assert len(bodies) == 2
        assert bodies[0]["requests"] == [
            {"createFootnote": {"location": {"index": 22}}}
        ]
        assert bodies[1]["requests"] == [
            {
                "insertText": {
                    "text": "Source.",
                    "endOfSegmentLocation": {"segmentId": "kix.fn1"},
                }
            }
        ]
        assert metadata["created_footnotes"] == [
            {"footnote_id": "kix.fn1", "tab_id": None, "text_written": True}
        ]
        assert "kix.fn1 (text written)" in message

    @pytest.mark.asyncio
    async def test_footnote_without_text_makes_no_follow_up_call(self):
        service = _service(replies=[{"createFootnote": {"footnoteId": "kix.fn1"}}])
        manager = BatchOperationManager(service)
        success, message, metadata = await manager.execute_batch_operations(
            DOC_ID, [{"type": "create_footnote", "end_of_segment": True}]
        )
        assert success, message
        assert len(_batch_bodies(service)) == 1
        assert metadata["created_footnotes"][0]["text_written"] is False

    @pytest.mark.asyncio
    async def test_footnote_rejects_segment_target(self):
        manager = BatchOperationManager(_service())
        success, message, _ = await manager.execute_batch_operations(
            DOC_ID,
            [{"type": "create_footnote", "index": 3, "segment_id": "kix.h1"}],
        )
        assert not success
        assert "cannot target a segment" in message

    @pytest.mark.asyncio
    async def test_dropdown_definition_ids_are_reported(self):
        service = _service(
            replies=[
                {
                    "createDropdownDefinition": {
                        "dropdownDefinition": {
                            "dropdownDefinitionId": "kix.gen1",
                            "dropdownDefinitionProperties": {
                                "options": [
                                    {
                                        "displayValue": "Draft",
                                        "optionId": "dropdownItem.a",
                                    },
                                    {
                                        "displayValue": "Final",
                                        "optionId": "dropdownItem.b",
                                    },
                                ]
                            },
                        }
                    }
                },
                {},
            ]
        )
        manager = BatchOperationManager(service)
        success, message, metadata = await manager.execute_batch_operations(
            DOC_ID,
            [
                {"type": "create_dropdown_definition", "options": ["Draft", "Final"]},
                {
                    "type": "insert_dropdown",
                    "end_of_segment": True,
                    "dropdown_definition_id": "kix.gen1",
                },
            ],
        )
        assert success, message
        assert (
            metadata["created_dropdown_definitions"][0]["dropdown_definition_id"]
            == "kix.gen1"
        )
        assert "'Draft'=dropdownItem.a" in message
        requests = _batch_bodies(service)[0]["requests"]
        assert "createDropdownDefinition" in requests[0]
        assert requests[1]["insertDropdown"]["dropdownDefinitionId"] == "kix.gen1"

    @pytest.mark.asyncio
    async def test_named_style_and_deletions_build_requests(self):
        service = _service(replies=[{}, {}, {}, {}, {}])
        manager = BatchOperationManager(service)
        success, message, _ = await manager.execute_batch_operations(
            DOC_ID,
            [
                {
                    "type": "update_named_style",
                    "named_style_type": "NORMAL_TEXT",
                    "font_family": "Georgia",
                    "line_spacing": 1.15,
                },
                {"type": "delete_header", "header_id": "kix.h1"},
                {"type": "delete_footer", "footer_id": "kix.f1"},
                {"type": "delete_positioned_object", "object_id": "kix.p1"},
                {
                    "type": "replace_image",
                    "image_object_id": "kix.i1",
                    "uri": "https://example.com/a.png",
                },
            ],
        )
        assert success, message
        requests = _batch_bodies(service)[0]["requests"]
        assert [next(iter(r)) for r in requests] == [
            "updateNamedStyle",
            "deleteHeader",
            "deleteFooter",
            "deletePositionedObject",
            "replaceImage",
        ]
        assert requests[0]["updateNamedStyle"]["fields"] == (
            "namedStyleType,textStyle.weightedFontFamily,paragraphStyle.lineSpacing"
        )

    @pytest.mark.asyncio
    async def test_named_style_without_fields_fails_clearly(self):
        manager = BatchOperationManager(_service())
        success, message, _ = await manager.execute_batch_operations(
            DOC_ID, [{"type": "update_named_style", "named_style_type": "TITLE"}]
        )
        assert not success
        assert "at least one text or paragraph style field" in message

    def test_supported_operations_catalogue_lists_new_types(self):
        catalogue = BatchOperationManager(Mock()).get_supported_operations()[
            "supported_operations"
        ]
        for op_type in (
            "insert_person",
            "insert_date",
            "insert_rich_link",
            "create_footnote",
            "create_dropdown_definition",
            "insert_dropdown",
            "replace_image",
            "delete_header",
            "delete_footer",
            "update_named_style",
            "delete_positioned_object",
        ):
            assert op_type in catalogue

    @pytest.mark.asyncio
    async def test_public_tool_accepts_date_chip_at_end_of_segment(self):
        service = _service()
        result = await _unwrap(docs_tools.batch_update_doc)(
            service=service,
            user_google_email="user@example.com",
            document_id=DOC_ID,
            operations=OPERATIONS.validate_python(
                [{"type": "insert_date", "end_of_segment": True, "date": "2026-10-08"}]
            ),
        )
        assert "Error" not in result
        request = _batch_bodies(service)[0]["requests"][0]["insertDate"]
        assert request["endOfSegmentLocation"] == {}
        assert request["dateElementProperties"]["timestamp"] == "2026-10-08T00:00:00Z"
