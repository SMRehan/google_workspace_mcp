"""
Typed Pydantic schemas for Google Docs batch operations.

These models are used to generate a richer MCP schema for batch_update_doc so
LLMs receive a machine-readable contract instead of a free-form object array.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, BeforeValidator, model_validator


ParagraphBorderEdge = Literal["top", "bottom", "left", "right", "between"]
TableBorderEdge = Literal["top", "bottom", "left", "right"]
NamedStyleType = Literal[
    "NORMAL_TEXT",
    "TITLE",
    "SUBTITLE",
    "HEADING_1",
    "HEADING_2",
    "HEADING_3",
    "HEADING_4",
    "HEADING_5",
    "HEADING_6",
]
DateFormat = Literal[
    "DATE_FORMAT_MONTH_DAY_ABBREVIATED",
    "DATE_FORMAT_MONTH_DAY_FULL",
    "DATE_FORMAT_MONTH_DAY_YEAR_ABBREVIATED",
    "DATE_FORMAT_ISO8601",
]
TimeFormat = Literal[
    "TIME_FORMAT_DISABLED",
    "TIME_FORMAT_HOUR_MINUTE",
    "TIME_FORMAT_HOUR_MINUTE_TIMEZONE",
]


def _coerce_json_str_to_list(v: Any) -> Any:
    """Accept JSON-encoded lists for MCP clients that serialize arrays as strings."""
    if not isinstance(v, str):
        return v

    try:
        parsed = json.loads(v)
    except (json.JSONDecodeError, TypeError):
        return v

    return parsed if isinstance(parsed, list) else v


class StrictDocOperation(BaseModel):
    """Base model for strictly typed high-impact operations."""

    model_config = ConfigDict(extra="forbid")

    tab_id: Optional[str] = Field(
        default=None,
        description="Optional document tab ID to target.",
    )


class SegmentTargetDocOperation(StrictDocOperation):
    """Base model for operations that can target document segments."""

    segment_id: Optional[str] = Field(
        default=None,
        description=(
            "Optional header/footer/footnote segment ID. Use a real ID returned by "
            "inspect_doc_structure; do not guess values like 'kix.header'."
        ),
    )


class InsertionLocationOperation(StrictDocOperation):
    """One explicit location or semantic anchor for an insertion."""

    index: Optional[int] = Field(
        default=None,
        description="Insertion index. Omit when end_of_segment=true.",
    )
    end_of_segment: bool = Field(
        default=False,
        description="Append to the end of the targeted body/segment instead of using index.",
    )
    after_heading: Optional[str] = Field(
        default=None,
        description=(
            "Insert immediately after the heading paragraph with this exact text, "
            "resolved to an index at execution time. Errors when it matches zero or "
            "more than one heading."
        ),
    )
    before_heading: Optional[str] = Field(
        default=None,
        description=(
            "Insert immediately before the heading paragraph with this exact text. "
            "Errors when it matches zero or more than one heading."
        ),
    )
    anchor_text: Optional[str] = Field(
        default=None,
        description=(
            "Insert relative to this literal text, which must occur exactly once "
            "within a single paragraph. Use anchor_position to pick which side."
        ),
    )
    anchor_position: Literal["before", "after"] = Field(
        default="after",
        description="Which side of anchor_text to insert on. Defaults to 'after'.",
    )

    @model_validator(mode="after")
    def validate_location(self) -> "InsertionLocationOperation":
        anchors = [self.after_heading, self.before_heading, self.anchor_text]
        provided = sum(
            [self.index is not None, self.end_of_segment]
            + [anchor is not None for anchor in anchors]
        )
        if provided != 1:
            raise ValueError(
                "Provide exactly one of 'index', 'end_of_segment=true', "
                "'after_heading', 'before_heading' or 'anchor_text'."
            )
        return self


class InsertTextOperation(InsertionLocationOperation, SegmentTargetDocOperation):
    type: Literal["insert_text"]
    text: str = Field(description="Text to insert.")


class ReplaceTextOperation(SegmentTargetDocOperation):
    type: Literal["replace_text"]
    start_index: int
    end_index: int
    text: str = Field(description="Replacement text.")


class DeleteTextOperation(SegmentTargetDocOperation):
    type: Literal["delete_text"]
    start_index: int
    end_index: int


class FormatTextOperation(SegmentTargetDocOperation):
    type: Literal["format_text"]
    start_index: int
    end_index: int
    bold: Optional[bool] = None
    italic: Optional[bool] = None
    underline: Optional[bool] = None
    strikethrough: Optional[bool] = None
    font_size: Optional[float] = None
    font_family: Optional[str] = None
    font_weight: Optional[int] = None
    text_color: Optional[str] = None
    background_color: Optional[str] = None
    link_url: Optional[str] = None
    clear_link: Optional[bool] = None
    baseline_offset: Optional[str] = None
    small_caps: Optional[bool] = None


class UpdateParagraphStyleOperation(SegmentTargetDocOperation):
    type: Literal["update_paragraph_style"]
    start_index: int
    end_index: int
    heading_level: Optional[int] = None
    alignment: Optional[str] = None
    line_spacing: Optional[float] = None
    indent_first_line: Optional[float] = None
    indent_start: Optional[float] = None
    indent_end: Optional[float] = None
    space_above: Optional[float] = None
    space_below: Optional[float] = None
    named_style_type: Optional[str] = None
    direction: Optional[str] = None
    keep_lines_together: Optional[bool] = None
    keep_with_next: Optional[bool] = None
    avoid_widow_and_orphan: Optional[bool] = None
    page_break_before: Optional[bool] = None
    spacing_mode: Optional[str] = None
    shading_color: Optional[str] = None
    border_edges: Optional[list[ParagraphBorderEdge]] = Field(
        default=None,
        min_length=1,
        description="Paragraph border edges to update; omit to update top, bottom, left, and right.",
    )
    border_color: Optional[str] = None
    border_width: Optional[float] = None
    border_padding: Optional[float] = None
    border_dash: Optional[str] = None


class UpdateTableCellStyleOperation(StrictDocOperation):
    type: Literal["update_table_cell_style"]
    table_start_index: int
    background_color: Optional[str] = None
    border_color: Optional[str] = None
    border_width: Optional[float] = None
    padding_top: Optional[float] = None
    padding_bottom: Optional[float] = None
    padding_left: Optional[float] = None
    padding_right: Optional[float] = None
    content_alignment: Optional[str] = None
    row_index: Optional[int] = None
    column_index: Optional[int] = None
    row_span: Optional[int] = None
    column_span: Optional[int] = None
    border_edges: Optional[list[TableBorderEdge]] = Field(
        default=None,
        min_length=1,
        description="Table-cell border edges to update; omit to update all four edges.",
    )


class InsertTableOperation(InsertionLocationOperation, SegmentTargetDocOperation):
    type: Literal["insert_table"]
    rows: int
    columns: int


class InsertTableRowOperation(StrictDocOperation):
    type: Literal["insert_table_row"]
    table_start_index: int
    row_index: int
    insert_below: bool = True


class DeleteTableRowOperation(StrictDocOperation):
    type: Literal["delete_table_row"]
    table_start_index: int
    row_index: int


class InsertTableColumnOperation(StrictDocOperation):
    type: Literal["insert_table_column"]
    table_start_index: int
    column_index: int
    insert_right: bool = True


class DeleteTableColumnOperation(StrictDocOperation):
    type: Literal["delete_table_column"]
    table_start_index: int
    column_index: int


class MergeTableCellsOperation(StrictDocOperation):
    type: Literal["merge_table_cells"]
    table_start_index: int
    row_index: int
    column_index: int
    row_span: int
    column_span: int


class UnmergeTableCellsOperation(StrictDocOperation):
    type: Literal["unmerge_table_cells"]
    table_start_index: int
    row_index: int
    column_index: int
    row_span: int
    column_span: int


class UpdateTableColumnPropertiesOperation(StrictDocOperation):
    type: Literal["update_table_column_properties"]
    table_start_index: int
    column_indices: list[int]
    width: Optional[float] = None
    width_type: Optional[str] = None


class UpdateTableRowStyleOperation(StrictDocOperation):
    type: Literal["update_table_row_style"]
    table_start_index: int
    row_indices: list[int] = Field(
        description="Zero-based row indices to style, e.g. [0] for the header row."
    )
    min_row_height: Optional[float] = Field(
        default=None,
        description="Minimum row height in points.",
    )


class PinTableHeaderRowsOperation(StrictDocOperation):
    type: Literal["pin_table_header_rows"]
    table_start_index: int
    pinned_header_rows_count: int = Field(
        ge=0,
        description="Number of leading rows to pin as a repeating header on each "
        "page. 0 unpins all rows. Use this dedicated request because the "
        "'tableHeader' value reported in TableRowStyle cannot be set through "
        "UpdateTableRowStyleRequest.",
    )


class InsertPageBreakOperation(InsertionLocationOperation):
    type: Literal["insert_page_break"]


class InsertSectionBreakOperation(InsertionLocationOperation):
    type: Literal["insert_section_break"]
    section_type: Literal["CONTINUOUS", "NEXT_PAGE"] = "NEXT_PAGE"


class FindReplaceOperation(StrictDocOperation):
    type: Literal["find_replace"]
    find_text: str
    replace_text: str
    match_case: bool = False


class CreateBulletListOperation(SegmentTargetDocOperation):
    type: Literal["create_bullet_list"]
    start_index: int
    end_index: int
    list_type: Literal["UNORDERED", "ORDERED", "CHECKBOX", "NONE"] = "UNORDERED"
    nesting_level: Optional[int] = None
    paragraph_start_indices: Optional[list[int]] = None
    bullet_preset: Optional[str] = None


class CreateNamedRangeOperation(SegmentTargetDocOperation):
    type: Literal["create_named_range"]
    name: str
    start_index: int
    end_index: int


class ReplaceNamedRangeContentOperation(StrictDocOperation):
    type: Literal["replace_named_range_content"]
    text: str
    named_range_id: Optional[str] = None
    named_range_name: Optional[str] = None

    @model_validator(mode="after")
    def validate_named_range_target(self) -> "ReplaceNamedRangeContentOperation":
        if bool(self.named_range_id) == bool(self.named_range_name):
            raise ValueError(
                "Provide exactly one of 'named_range_id' or 'named_range_name'."
            )
        return self


class DeleteNamedRangeOperation(StrictDocOperation):
    type: Literal["delete_named_range"]
    named_range_id: Optional[str] = None
    named_range_name: Optional[str] = None

    @model_validator(mode="after")
    def validate_named_range_target(self) -> "DeleteNamedRangeOperation":
        if bool(self.named_range_id) == bool(self.named_range_name):
            raise ValueError(
                "Provide exactly one of 'named_range_id' or 'named_range_name'."
            )
        return self


class UpdateDocumentStyleOperation(StrictDocOperation):
    type: Literal["update_document_style"]
    background_color: Optional[str] = None
    margin_top: Optional[float] = None
    margin_bottom: Optional[float] = None
    margin_left: Optional[float] = None
    margin_right: Optional[float] = None
    margin_header: Optional[float] = None
    margin_footer: Optional[float] = None
    page_width: Optional[float] = None
    page_height: Optional[float] = None
    page_number_start: Optional[int] = None
    use_even_page_header_footer: Optional[bool] = None
    use_first_page_header_footer: Optional[bool] = None
    flip_page_orientation: Optional[bool] = None
    document_mode: Optional[Literal["PAGES", "PAGELESS"]] = None


class UpdateSectionStyleOperation(StrictDocOperation):
    type: Literal["update_section_style"]
    start_index: int
    end_index: int
    margin_top: Optional[float] = None
    margin_bottom: Optional[float] = None
    margin_left: Optional[float] = None
    margin_right: Optional[float] = None
    margin_header: Optional[float] = None
    margin_footer: Optional[float] = None
    page_number_start: Optional[int] = None
    use_first_page_header_footer: Optional[bool] = None
    flip_page_orientation: Optional[bool] = None
    content_direction: Optional[Literal["LEFT_TO_RIGHT", "RIGHT_TO_LEFT"]] = None
    column_count: Optional[int] = None
    column_spacing: Optional[float] = None
    column_separator_style: Optional[Literal["NONE", "BETWEEN_EACH_COLUMN"]] = None


class CreateHeaderFooterOperation(StrictDocOperation):
    type: Literal["create_header_footer"]
    section_type: Literal["header", "footer"] = Field(
        description="Which section to create."
    )
    header_footer_type: Literal["DEFAULT", "FIRST_PAGE_ONLY", "EVEN_PAGE"] = Field(
        default="DEFAULT",
        description="Header/footer type to create.",
    )
    section_break_index: Optional[int] = Field(
        default=None,
        description="Optional section break index for section-scoped layouts.",
    )


class InsertImageOperation(InsertionLocationOperation, SegmentTargetDocOperation):
    type: Literal["insert_image"]
    image_uri: str = Field(description="Image URL or resolvable image URI.")
    width: Optional[int] = None
    height: Optional[int] = None


class InsertDocTabOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["insert_doc_tab"]
    title: str
    index: int
    parent_tab_id: Optional[str] = None


class DeleteDocTabOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["delete_doc_tab"]
    tab_id: str


class UpdateDocTabOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["update_doc_tab"]
    tab_id: str
    title: str


class InsertPersonOperation(InsertionLocationOperation, SegmentTargetDocOperation):
    type: Literal["insert_person"]
    email: str = Field(description="Email address the person chip links to.")
    name: Optional[str] = Field(
        default=None,
        description="Display name shown in the chip instead of the email address.",
    )


class InsertDateOperation(InsertionLocationOperation, SegmentTargetDocOperation):
    type: Literal["insert_date"]
    date: str = Field(
        description=(
            "ISO 8601 date ('2026-10-08') or datetime ('2026-10-08T15:00:00Z'). "
            "A bare date is midnight UTC; a naive datetime is treated as UTC."
        )
    )
    date_format: Optional[DateFormat] = Field(
        default=None,
        description="Display format; the API default is DATE_FORMAT_MONTH_DAY_YEAR_ABBREVIATED.",
    )
    time_format: Optional[TimeFormat] = Field(
        default=None,
        description="Time display; the API default is TIME_FORMAT_DISABLED.",
    )
    time_zone_id: Optional[str] = Field(
        default=None,
        description="CLDR time zone such as 'America/New_York' used to display the date.",
    )
    locale: Optional[str] = Field(
        default=None, description="Language code such as 'en' for the display text."
    )


class InsertRichLinkOperation(InsertionLocationOperation, SegmentTargetDocOperation):
    type: Literal["insert_rich_link"]
    uri: str = Field(
        description="URL of a Drive file, YouTube video or Calendar event to show as a smart chip."
    )
    title: Optional[str] = Field(
        default=None,
        description="Chip title; the API fills it from the linked resource when omitted.",
    )


class CreateFootnoteOperation(InsertionLocationOperation):
    type: Literal["create_footnote"]
    text: Optional[str] = Field(
        default=None,
        description=(
            "Text to write into the new footnote. Sent as a follow-up request once "
            "the footnote's segment ID is known."
        ),
    )


class DropdownOptionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_value: str = Field(description="Text shown for this option.")
    option_id: Optional[str] = Field(
        default=None,
        description=(
            "Optional ID matching ^dropdownItem\\.[a-zA-Z0-9_-]{2,14}$ so "
            "insert_dropdown can preselect it; the API generates one when omitted."
        ),
    )


class CreateDropdownDefinitionOperation(StrictDocOperation):
    type: Literal["create_dropdown_definition"]
    options: list[Union[str, DropdownOptionSpec]] = Field(
        min_length=2,
        max_length=50,
        description="2-50 options, each a display string or {display_value, option_id}.",
    )
    dropdown_definition_id: Optional[str] = Field(
        default=None,
        description=(
            "Optional ID matching ^kix\\.[a-zA-Z0-9_-]{2,14}$ so a later "
            "insert_dropdown in the same batch can reference it; the API generates "
            "one when omitted."
        ),
    )
    title: Optional[str] = None


class InsertDropdownOperation(InsertionLocationOperation, SegmentTargetDocOperation):
    type: Literal["insert_dropdown"]
    dropdown_definition_id: str = Field(
        description="ID of an existing dropdown definition (from create_dropdown_definition or inspect_doc_structure)."
    )
    selected_option_id: Optional[str] = Field(
        default=None,
        description="Option to preselect; defaults to the definition's first option.",
    )


class ReplaceImageOperation(StrictDocOperation):
    type: Literal["replace_image"]
    image_object_id: str = Field(
        description="ID of the existing inline or positioned image to replace."
    )
    uri: str = Field(description="URL of the new image (fetched once at replace time).")
    image_replace_method: Optional[Literal["CENTER_CROP"]] = None


class DeleteHeaderOperation(StrictDocOperation):
    type: Literal["delete_header"]
    header_id: str = Field(description="Header ID from inspect_doc_structure.")


class DeleteFooterOperation(StrictDocOperation):
    type: Literal["delete_footer"]
    footer_id: str = Field(description="Footer ID from inspect_doc_structure.")


class UpdateNamedStyleOperation(StrictDocOperation):
    type: Literal["update_named_style"]
    named_style_type: NamedStyleType = Field(
        description="Named style whose definition changes document-wide."
    )
    bold: Optional[bool] = None
    italic: Optional[bool] = None
    underline: Optional[bool] = None
    strikethrough: Optional[bool] = None
    font_size: Optional[float] = None
    font_family: Optional[str] = None
    font_weight: Optional[int] = None
    text_color: Optional[str] = None
    background_color: Optional[str] = None
    baseline_offset: Optional[str] = None
    small_caps: Optional[bool] = None
    alignment: Optional[str] = None
    line_spacing: Optional[float] = None
    indent_first_line: Optional[float] = None
    indent_start: Optional[float] = None
    indent_end: Optional[float] = None
    space_above: Optional[float] = None
    space_below: Optional[float] = None
    direction: Optional[str] = None
    keep_lines_together: Optional[bool] = None
    keep_with_next: Optional[bool] = None
    avoid_widow_and_orphan: Optional[bool] = None
    page_break_before: Optional[bool] = None
    spacing_mode: Optional[str] = None
    shading_color: Optional[str] = None
    border_edges: Optional[list[ParagraphBorderEdge]] = Field(
        default=None,
        min_length=1,
        description="Paragraph border edges to update; omit to update top, bottom, left, and right.",
    )
    border_color: Optional[str] = None
    border_width: Optional[float] = None
    border_padding: Optional[float] = None
    border_dash: Optional[str] = None

    @model_validator(mode="after")
    def validate_has_style_field(self) -> "UpdateNamedStyleOperation":
        style_fields = self.model_dump(exclude={"type", "tab_id", "named_style_type"})
        if not any(value is not None for value in style_fields.values()):
            raise ValueError(
                "update_named_style needs at least one text or paragraph style field."
            )
        return self


class DeletePositionedObjectOperation(StrictDocOperation):
    type: Literal["delete_positioned_object"]
    object_id: str = Field(
        description="Positioned object ID from inspect_doc_structure (floating image or drawing)."
    )


BatchDocOperation = Annotated[
    Union[
        InsertTextOperation,
        DeleteTextOperation,
        ReplaceTextOperation,
        FormatTextOperation,
        UpdateParagraphStyleOperation,
        UpdateTableCellStyleOperation,
        InsertTableOperation,
        InsertTableRowOperation,
        DeleteTableRowOperation,
        InsertTableColumnOperation,
        DeleteTableColumnOperation,
        MergeTableCellsOperation,
        UnmergeTableCellsOperation,
        UpdateTableColumnPropertiesOperation,
        UpdateTableRowStyleOperation,
        PinTableHeaderRowsOperation,
        InsertPageBreakOperation,
        InsertSectionBreakOperation,
        FindReplaceOperation,
        CreateBulletListOperation,
        CreateNamedRangeOperation,
        ReplaceNamedRangeContentOperation,
        DeleteNamedRangeOperation,
        UpdateDocumentStyleOperation,
        UpdateSectionStyleOperation,
        CreateHeaderFooterOperation,
        InsertImageOperation,
        InsertDocTabOperation,
        DeleteDocTabOperation,
        UpdateDocTabOperation,
        InsertPersonOperation,
        InsertDateOperation,
        InsertRichLinkOperation,
        CreateFootnoteOperation,
        CreateDropdownDefinitionOperation,
        InsertDropdownOperation,
        ReplaceImageOperation,
        DeleteHeaderOperation,
        DeleteFooterOperation,
        UpdateNamedStyleOperation,
        DeletePositionedObjectOperation,
    ],
    Field(discriminator="type"),
]

BatchDocOperations = Annotated[
    list[BatchDocOperation],
    BeforeValidator(_coerce_json_str_to_list),
]
