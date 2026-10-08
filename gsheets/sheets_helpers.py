"""
Google Sheets Helper Functions

Shared utilities for Google Sheets operations including A1 parsing and
conditional formatting helpers.
"""

import asyncio
import copy
import difflib
import json
import logging
import re
from typing import Any, List, Optional, Union

from core.utils import UserInputError

logger = logging.getLogger(__name__)

MAX_GRID_METADATA_CELLS = 5000

# Cap rows fetched by read_sheet_values before calling values().get.
# Matches the tool default range (A1:Z1000). Open-ended or oversized A1
# ranges otherwise materialize the full sheet into memory.
MAX_READ_SHEET_ROWS = 1000

A1_PART_REGEX = re.compile(r"^([A-Za-z]*)(\d*)$")
SHEET_TITLE_SAFE_RE = re.compile(r"^[A-Za-z0-9_]+$")
COLUMN_LETTER_REGEX = re.compile(r"^[A-Za-z]+$")
QUOTED_SHEET_ONLY_REGEX = re.compile(r"^'(?:[^']|'')+'$")


def _column_to_index(column: str) -> Optional[int]:
    """Convert column letters (A, B, AA) to zero-based index."""
    if not column or not COLUMN_LETTER_REGEX.fullmatch(column):
        return None
    result = 0
    for char in column.upper():
        result = result * 26 + (ord(char) - ord("A") + 1)
    return result - 1


def _parse_a1_part(
    part: str, pattern: re.Pattern[str] = A1_PART_REGEX
) -> tuple[Optional[int], Optional[int]]:
    """
    Parse a single A1 part like 'B2' or 'C' into zero-based column/row indexes.
    Supports anchors like '$A$1' by stripping the dollar signs.
    """
    clean_part = part.replace("$", "")
    match = pattern.fullmatch(clean_part)
    if not match:
        raise UserInputError(f"Invalid A1 range part: '{part}'.")
    col_letters, row_digits = match.groups()
    col_idx = _column_to_index(col_letters) if col_letters else None
    row_idx = int(row_digits) - 1 if row_digits else None
    return col_idx, row_idx


def _split_sheet_and_range(range_name: str) -> tuple[Optional[str], str]:
    """
    Split an A1 notation into (sheet_name, range_part), handling quoted sheet names.

    Examples:
    - "Sheet1!A1:B2" -> ("Sheet1", "A1:B2")
    - "'My Sheet'!$A$1:$B$10" -> ("My Sheet", "$A$1:$B$10")
    - "A1:B2" -> (None, "A1:B2")
    """
    if "!" not in range_name:
        return None, range_name

    if range_name.startswith("'"):
        closing = range_name.find("'!")
        if closing != -1:
            sheet_name = range_name[1:closing].replace("''", "'")
            a1_range = range_name[closing + 2 :]
            return sheet_name, a1_range

    sheet_name, a1_range = range_name.split("!", 1)
    return sheet_name.strip().strip("'"), a1_range


def _format_a1_part(col_idx: Optional[int], row_idx: Optional[int]) -> str:
    """Build an A1 cell/partial reference from zero-based indexes."""
    col = _index_to_column(col_idx) if col_idx is not None else ""
    row = str(row_idx + 1) if row_idx is not None else ""
    if not col and not row:
        raise UserInputError("A1 range part must include a column and/or row.")
    return f"{col}{row}"


def _format_read_clamp_note(range_name: str, clamped_range: str, max_rows: int) -> str:
    """Describe a rewritten read range and how to continue paging."""
    return (
        f"\n\nNote: Requested range '{range_name}' was clamped to '{clamped_range}' "
        f"(max {max_rows} rows per read). Request a later row window to continue."
    )


def _clamp_a1_read_rows(
    range_name: str, max_rows: int = MAX_READ_SHEET_ROWS
) -> tuple[str, Optional[str]]:
    """
    Rewrite an A1 range so it spans at most ``max_rows`` rows.

    Open-ended ranges (e.g. ``A:Z``, ``Sheet1!A1:Z``) and quoted whole-sheet
    references (e.g. ``'My Sheet'``) are closed to a finite end row. Oversized
    finite ranges are truncated from the start row. Bare identifiers are
    returned unchanged because they can refer to named ranges.

    Returns:
        (range_for_api, note): ``note`` is set when the range was rewritten.
    """
    if max_rows < 1:
        raise ValueError(f"max_rows must be >= 1, got {max_rows}")

    # A quoted sheet title without coordinates unambiguously addresses the
    # entire sheet. Bare identifiers are intentionally not handled here:
    # Google Sheets resolves them as named ranges when a matching name exists.
    if QUOTED_SHEET_ONLY_REGEX.fullmatch(range_name):
        clamped_range = f"{range_name}!1:{max_rows}"
        return clamped_range, _format_read_clamp_note(
            range_name, clamped_range, max_rows
        )

    sheet_name, a1_range = _split_sheet_and_range(range_name)
    if not a1_range:
        return range_name, None

    try:
        if ":" in a1_range:
            start, end = a1_range.split(":", 1)
        else:
            # Bare identifiers without a row (e.g. named ranges) are not A1
            # coordinates we can safely rewrite.
            start = end = a1_range
        start_col, start_row = _parse_a1_part(start)
        end_col, end_row = _parse_a1_part(end)
    except UserInputError:
        logger.warning(
            "Cannot clamp non-A1 sheet range %r; fetching as requested",
            range_name,
        )
        return range_name, None

    if ":" not in a1_range and start_row is None:
        return range_name, None

    effective_start = start_row if start_row is not None else 0
    max_end_row = effective_start + max_rows - 1

    clamped = False
    if start_row is None:
        start_row = effective_start
        clamped = True
    if end_row is None or end_row > max_end_row:
        end_row = max_end_row
        clamped = True

    if not clamped:
        return range_name, None

    range_ref = (
        _format_a1_part(start_col, start_row)
        if start_col == end_col and start_row == end_row
        else f"{_format_a1_part(start_col, start_row)}:{_format_a1_part(end_col, end_row)}"
    )
    if sheet_name is not None:
        clamped_range = f"{_quote_sheet_title_for_a1(sheet_name)}!{range_ref}"
    else:
        clamped_range = range_ref

    note = _format_read_clamp_note(range_name, clamped_range, max_rows)
    return clamped_range, note


def _parse_a1_range(range_name: str, sheets: List[dict]) -> dict:
    """
    Convert an A1-style range (with optional sheet name) into a GridRange.

    Falls back to the first sheet if none is provided.
    """
    sheet_name, a1_range = _split_sheet_and_range(range_name)

    if not sheets:
        raise UserInputError("Spreadsheet has no sheets.")

    target_sheet = None
    if sheet_name:
        for sheet in sheets:
            if sheet.get("properties", {}).get("title") == sheet_name:
                target_sheet = sheet
                break
        if target_sheet is None:
            available_titles = [
                sheet.get("properties", {}).get("title", "Untitled") for sheet in sheets
            ]
            available_list = ", ".join(available_titles) if available_titles else "none"
            raise UserInputError(
                f"Sheet '{sheet_name}' not found in spreadsheet. Available sheets: {available_list}."
            )
    else:
        target_sheet = sheets[0]

    props = target_sheet.get("properties", {})
    sheet_id = props.get("sheetId")

    if not a1_range:
        raise UserInputError("A1-style range must not be empty (e.g., 'A1', 'A1:B10').")

    if ":" in a1_range:
        start, end = a1_range.split(":", 1)
    else:
        start = end = a1_range

    start_col, start_row = _parse_a1_part(start)
    end_col, end_row = _parse_a1_part(end)

    grid_range = {"sheetId": sheet_id}
    if start_row is not None:
        grid_range["startRowIndex"] = start_row
    if start_col is not None:
        grid_range["startColumnIndex"] = start_col
    if end_row is not None:
        grid_range["endRowIndex"] = end_row + 1
    if end_col is not None:
        grid_range["endColumnIndex"] = end_col + 1

    return grid_range


def _parse_hex_color(color: Optional[str]) -> Optional[dict]:
    """
    Convert a hex color like '#RRGGBB' to Sheets API color (0-1 floats).
    """
    if not color:
        return None

    trimmed = color.strip()
    if trimmed.startswith("#"):
        trimmed = trimmed[1:]

    if len(trimmed) != 6:
        raise UserInputError(f"Color '{color}' must be in format #RRGGBB or RRGGBB.")

    try:
        red = int(trimmed[0:2], 16) / 255
        green = int(trimmed[2:4], 16) / 255
        blue = int(trimmed[4:6], 16) / 255
    except ValueError as exc:
        raise UserInputError(f"Color '{color}' is not valid hex.") from exc

    return {"red": red, "green": green, "blue": blue}


def _index_to_column(index: int) -> str:
    """
    Convert a zero-based column index to column letters (0 -> A, 25 -> Z, 26 -> AA).
    """
    if index < 0:
        raise UserInputError(f"Column index must be non-negative, got {index}.")

    result = []
    index += 1  # Convert to 1-based for calculation
    while index:
        index, remainder = divmod(index - 1, 26)
        result.append(chr(ord("A") + remainder))
    return "".join(reversed(result))


def _quote_sheet_title_for_a1(sheet_title: str) -> str:
    """
    Quote a sheet title for use in A1 notation if necessary.

    If the sheet title contains special characters or spaces, it is wrapped in single quotes.
    Any single quotes in the title are escaped by doubling them, as required by Google Sheets.
    """
    if SHEET_TITLE_SAFE_RE.fullmatch(sheet_title or ""):
        return sheet_title
    escaped = (sheet_title or "").replace("'", "''")
    return f"'{escaped}'"


def _format_a1_cell(sheet_title: str, row_index: int, col_index: int) -> str:
    """
    Format a cell reference in A1 notation given a sheet title and zero-based row/column indices.

    Args:
        sheet_title: The title of the sheet.
        row_index: Zero-based row index (0 for first row).
        col_index: Zero-based column index (0 for column A).

    Returns:
        A string representing the cell reference in A1 notation, e.g., 'Sheet1!B2'.
    """
    return f"{_quote_sheet_title_for_a1(sheet_title)}!{_index_to_column(col_index)}{row_index + 1}"


def _coerce_int(value: object, default: int = 0) -> int:
    """
    Safely convert a value to an integer, returning a default value if conversion fails.

    Args:
        value: The value to convert to int.
        default: The value to return if conversion fails (default is 0).

    Returns:
        The integer value of `value`, or `default` if conversion fails.
    """
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _is_sheets_error_token(value: object) -> bool:
    """
    Detect whether a cell value represents a Google Sheets error token (e.g., #ERROR!, #NAME?, #REF!, #N/A).

    Returns True if the value is a string that starts with '#' and ends with '!' or '?', or is exactly '#N/A'.
    """
    if not isinstance(value, str):
        return False
    candidate = value.strip()
    if not candidate.startswith("#"):
        return False
    upper_candidate = candidate.upper()
    if upper_candidate == "#N/A":
        return True
    return upper_candidate.endswith(("!", "?"))


def _values_contain_sheets_errors(values: List[List[object]]) -> bool:
    """
    Check whether a 2D array of cell values contains any Google Sheets error tokens.

    Args:
        values: A 2D list of cell values (as returned from the Sheets API).

    Returns:
        True if any cell contains a Google Sheets error token, False otherwise.
    """
    for row in values:
        for cell in row:
            if _is_sheets_error_token(cell):
                return True
    return False


def _a1_range_for_values(a1_range: str, values: List[List[object]]) -> Optional[str]:
    """
    Compute a tight A1 range for a returned values matrix.

    This helps keep follow-up includeGridData payloads small vs. using a wide requested range.
    Only applies when the A1 range has an explicit starting cell (e.g., 'Sheet1!B2:D10').
    """
    sheet_name, range_part = _split_sheet_and_range(a1_range)
    if not range_part:
        return None

    start_part = range_part.split(":", 1)[0]
    start_col, start_row = _parse_a1_part(start_part)
    if start_col is None or start_row is None:
        return None

    height = len(values)
    width = max((len(row) for row in values), default=0)
    if height <= 0 or width <= 0:
        return None

    end_row = start_row + height - 1
    end_col = start_col + width - 1

    start_label = f"{_index_to_column(start_col)}{start_row + 1}"
    end_label = f"{_index_to_column(end_col)}{end_row + 1}"
    range_ref = (
        start_label if start_label == end_label else f"{start_label}:{end_label}"
    )

    if sheet_name:
        return f"{_quote_sheet_title_for_a1(sheet_name)}!{range_ref}"
    return range_ref


def _a1_range_cell_count(a1_range: str) -> Optional[int]:
    """
    Return cell count for an explicit rectangular A1 range (e.g. A1:C10).

    Returns None when the range is open-ended or otherwise does not include
    both row and column bounds.
    """
    _, range_part = _split_sheet_and_range(a1_range)
    if not range_part:
        return None

    if ":" in range_part:
        start_part, end_part = range_part.split(":", 1)
    else:
        start_part = end_part = range_part

    try:
        start_col, start_row = _parse_a1_part(start_part)
        end_col, end_row = _parse_a1_part(end_part)
    except UserInputError:
        return None

    if None in (start_col, start_row, end_col, end_row):
        return None
    if end_col < start_col or end_row < start_row:
        return None

    return (end_col - start_col + 1) * (end_row - start_row + 1)


def _extract_cell_errors_from_grid(spreadsheet: dict) -> list[dict[str, Optional[str]]]:
    """
    Extracts error information from spreadsheet grid data.

    Iterates through the sheets and their grid data in the provided spreadsheet dictionary,
    collecting all cell errors. Returns a list of dictionaries, each containing:
        - "cell": the A1 notation of the cell with the error,
        - "type": the error type (e.g., "ERROR", "N/A"),
        - "message": the error message, if available.

    Args:
        spreadsheet (dict): The spreadsheet data as returned by the Sheets API with grid data included.

    Returns:
        list[dict[str, Optional[str]]]: List of error details for each cell with an error.
    """
    errors: list[dict[str, Optional[str]]] = []
    for sheet in spreadsheet.get("sheets", []) or []:
        sheet_title = sheet.get("properties", {}).get("title") or "Unknown"
        for grid in sheet.get("data", []) or []:
            start_row = _coerce_int(grid.get("startRow"), default=0)
            start_col = _coerce_int(grid.get("startColumn"), default=0)
            for row_offset, row_data in enumerate(grid.get("rowData", []) or []):
                if not row_data:
                    continue
                for col_offset, cell_data in enumerate(
                    row_data.get("values", []) or []
                ):
                    if not cell_data:
                        continue
                    error_value = (cell_data.get("effectiveValue") or {}).get(
                        "errorValue"
                    ) or None
                    if not error_value:
                        continue
                    errors.append(
                        {
                            "cell": _format_a1_cell(
                                sheet_title,
                                start_row + row_offset,
                                start_col + col_offset,
                            ),
                            "type": error_value.get("type"),
                            "message": error_value.get("message"),
                        }
                    )
    return errors


def _extract_cell_hyperlinks_from_grid(spreadsheet: dict) -> list[dict[str, str]]:
    """
    Extract hyperlink URLs from spreadsheet grid data.

    Returns a list of dictionaries with:
        - "cell": cell A1 reference
        - "url": hyperlink URL

    For rich text cells, this includes URLs from both `CellData.hyperlink`
    and `textFormatRuns[].format.link.uri`.
    """
    hyperlinks: list[dict[str, str]] = []
    for sheet in spreadsheet.get("sheets", []) or []:
        sheet_title = sheet.get("properties", {}).get("title") or "Unknown"
        for grid in sheet.get("data", []) or []:
            start_row = _coerce_int(grid.get("startRow"), default=0)
            start_col = _coerce_int(grid.get("startColumn"), default=0)
            for row_offset, row_data in enumerate(grid.get("rowData", []) or []):
                if not row_data:
                    continue
                for col_offset, cell_data in enumerate(
                    row_data.get("values", []) or []
                ):
                    if not cell_data:
                        continue
                    cell_urls: list[str] = []
                    seen_urls: set[str] = set()

                    hyperlink = cell_data.get("hyperlink")
                    if (
                        isinstance(hyperlink, str)
                        and hyperlink
                        and hyperlink not in seen_urls
                    ):
                        seen_urls.add(hyperlink)
                        cell_urls.append(hyperlink)

                    for text_run in cell_data.get("textFormatRuns", []) or []:
                        if not isinstance(text_run, dict):
                            continue
                        link_uri = (
                            (text_run.get("format") or {}).get("link") or {}
                        ).get("uri")
                        if not isinstance(link_uri, str) or not link_uri:
                            continue
                        if link_uri in seen_urls:
                            continue
                        seen_urls.add(link_uri)
                        cell_urls.append(link_uri)

                    if not cell_urls:
                        continue
                    cell_ref = _format_a1_cell(
                        sheet_title, start_row + row_offset, start_col + col_offset
                    )
                    for url in cell_urls:
                        hyperlinks.append({"cell": cell_ref, "url": url})
    return hyperlinks


async def _fetch_detailed_sheet_errors(
    service, spreadsheet_id: str, a1_range: str
) -> list[dict[str, Optional[str]]]:
    response = await asyncio.to_thread(
        service.spreadsheets()
        .get(
            spreadsheetId=spreadsheet_id,
            ranges=[a1_range],
            includeGridData=True,
            fields="sheets(properties(title),data(startRow,startColumn,rowData(values(effectiveValue(errorValue(type,message))))))",
        )
        .execute
    )
    return _extract_cell_errors_from_grid(response)


async def _fetch_sheet_hyperlinks(
    service, spreadsheet_id: str, a1_range: str
) -> list[dict[str, str]]:
    response = await asyncio.to_thread(
        service.spreadsheets()
        .get(
            spreadsheetId=spreadsheet_id,
            ranges=[a1_range],
            includeGridData=True,
            fields="sheets(properties(title),data(startRow,startColumn,rowData(values(hyperlink,textFormatRuns(format(link(uri)))))))",
        )
        .execute
    )
    return _extract_cell_hyperlinks_from_grid(response)


def _format_sheet_error_section(
    *, errors: list[dict[str, Optional[str]]], range_label: str, max_details: int = 25
) -> str:
    """
    Format a list of cell error information into a human-readable section.

    Args:
        errors: A list of dictionaries, each containing details about a cell error,
            including the cell location, error type, and message.
        range_label: A string label for the range in which the errors occurred.
        max_details: The maximum number of error details to include in the output.
            If the number of errors exceeds this value, the output will be truncated
            and a summary line will indicate how many additional errors were omitted.

    Returns:
        A formatted string listing the cell errors in a human-readable format.
        If there are no errors, returns an empty string.
    """
    # Limit the number of error details to 25 for performance and readability.
    if not errors:
        return ""

    lines = []
    for item in errors[:max_details]:
        cell = item.get("cell") or "(unknown cell)"
        error_type = item.get("type")
        message = item.get("message")
        if error_type and message:
            lines.append(f"- {cell}: {error_type} — {message}")
        elif message:
            lines.append(f"- {cell}: {message}")
        elif error_type:
            lines.append(f"- {cell}: {error_type}")
        else:
            lines.append(f"- {cell}: (unknown error)")

    suffix = (
        f"\n... and {len(errors) - max_details} more errors"
        if len(errors) > max_details
        else ""
    )
    return (
        f"\n\nDetailed cell errors in range '{range_label}':\n"
        + "\n".join(lines)
        + suffix
    )


def _format_sheet_hyperlink_section(
    *, hyperlinks: list[dict[str, str]], range_label: str, max_details: int = 25
) -> str:
    """
    Format a list of cell hyperlinks into a human-readable section.
    """
    if not hyperlinks:
        return ""

    lines = []
    for item in hyperlinks[:max_details]:
        cell = item.get("cell") or "(unknown cell)"
        url = item.get("url") or "(missing url)"
        lines.append(f"- {cell}: {url}")

    suffix = (
        f"\n... and {len(hyperlinks) - max_details} more hyperlinks"
        if len(hyperlinks) > max_details
        else ""
    )
    return f"\n\nHyperlinks in range '{range_label}':\n" + "\n".join(lines) + suffix


def _color_to_hex(color: Optional[dict]) -> Optional[str]:
    """
    Convert a Sheets color object back to #RRGGBB hex string for display.
    """
    if not color:
        return None

    def _component(value: Optional[float]) -> int:
        try:
            # Clamp and round to nearest integer in 0-255
            return max(0, min(255, int(round(float(value or 0) * 255))))
        except (TypeError, ValueError):
            return 0

    red = _component(color.get("red"))
    green = _component(color.get("green"))
    blue = _component(color.get("blue"))
    return f"#{red:02X}{green:02X}{blue:02X}"


def _grid_range_to_a1(grid_range: dict, sheet_titles: dict[int, str]) -> str:
    """
    Convert a GridRange to an A1-like string using known sheet titles.
    Falls back to the sheet ID if the title is unknown.
    """
    sheet_id = grid_range.get("sheetId")
    if sheet_id is None and 0 in sheet_titles:
        sheet_id = 0
    sheet_title = sheet_titles.get(sheet_id, f"Sheet {sheet_id}")

    start_row = grid_range.get("startRowIndex")
    end_row = grid_range.get("endRowIndex")
    start_col = grid_range.get("startColumnIndex")
    end_col = grid_range.get("endColumnIndex")

    # If nothing is specified, treat as the whole sheet.
    if start_row is None and end_row is None and start_col is None and end_col is None:
        return sheet_title

    def row_label(idx: Optional[int]) -> str:
        return str(idx + 1) if idx is not None else ""

    def col_label(idx: Optional[int]) -> str:
        return _index_to_column(idx) if idx is not None else ""

    start_label = f"{col_label(start_col)}{row_label(start_row)}"
    # end indices in GridRange are exclusive; subtract 1 for display
    end_label = f"{col_label(end_col - 1 if end_col is not None else None)}{row_label(end_row - 1 if end_row is not None else None)}"

    if start_label and end_label:
        # Collapse to a single label only for a bounded single cell (both a
        # column and a row). Column-only (e.g. "A") or row-only (e.g. "1")
        # ranges must keep the "start:end" form to stay valid A1 (A:A, 1:1).
        is_single_cell = start_col is not None and start_row is not None
        range_ref = (
            start_label
            if start_label == end_label and is_single_cell
            else f"{start_label}:{end_label}"
        )
    elif start_label:
        range_ref = start_label
    elif end_label:
        range_ref = end_label
    else:
        range_ref = ""

    return f"{sheet_title}!{range_ref}" if range_ref else sheet_title


def _summarize_conditional_rule(
    rule: dict, index: int, sheet_titles: dict[int, str]
) -> str:
    """
    Produce a concise human-readable summary of a conditional formatting rule.
    """
    ranges = rule.get("ranges", [])
    range_labels = [_grid_range_to_a1(rng, sheet_titles) for rng in ranges] or [
        "(no range)"
    ]

    if "booleanRule" in rule:
        boolean_rule = rule["booleanRule"]
        condition = boolean_rule.get("condition", {})
        cond_type = condition.get("type", "UNKNOWN")
        cond_values = [
            val.get("userEnteredValue")
            for val in condition.get("values", [])
            if isinstance(val, dict) and "userEnteredValue" in val
        ]
        value_desc = f" values={cond_values}" if cond_values else ""

        fmt = boolean_rule.get("format", {})
        fmt_parts = []
        bg_hex = _color_to_hex(fmt.get("backgroundColor"))
        if bg_hex:
            fmt_parts.append(f"bg {bg_hex}")
        fg_hex = _color_to_hex(fmt.get("textFormat", {}).get("foregroundColor"))
        if fg_hex:
            fmt_parts.append(f"text {fg_hex}")
        fmt_desc = ", ".join(fmt_parts) if fmt_parts else "no format"

        return f"[{index}] {cond_type}{value_desc} -> {fmt_desc} on {', '.join(range_labels)}"

    if "gradientRule" in rule:
        gradient_rule = rule["gradientRule"]
        points = []
        for point_name in ("minpoint", "midpoint", "maxpoint"):
            point = gradient_rule.get(point_name)
            if not point:
                continue
            color_hex = _color_to_hex(point.get("color"))
            type_desc = point.get("type", point_name)
            value_desc = point.get("value")
            point_desc = type_desc
            if value_desc:
                point_desc += f":{value_desc}"
            if color_hex:
                point_desc += f" {color_hex}"
            points.append(point_desc)
        gradient_desc = " | ".join(points) if points else "gradient"
        return f"[{index}] gradient -> {gradient_desc} on {', '.join(range_labels)}"

    return f"[{index}] (unknown rule) on {', '.join(range_labels)}"


def _format_conditional_rules_section(
    sheet_title: str,
    rules: List[dict],
    sheet_titles: dict[int, str],
    indent: str = "  ",
) -> str:
    """
    Build a multi-line string describing conditional formatting rules for a sheet.
    """
    if not rules:
        return f'{indent}Conditional formats for "{sheet_title}": none.'

    lines = [f'{indent}Conditional formats for "{sheet_title}" ({len(rules)}):']
    for idx, rule in enumerate(rules):
        lines.append(
            f"{indent}  {_summarize_conditional_rule(rule, idx, sheet_titles)}"
        )
    return "\n".join(lines)


CONDITION_TYPES = {
    "NUMBER_GREATER",
    "NUMBER_GREATER_THAN_EQ",
    "NUMBER_LESS",
    "NUMBER_LESS_THAN_EQ",
    "NUMBER_EQ",
    "NUMBER_NOT_EQ",
    "TEXT_CONTAINS",
    "TEXT_NOT_CONTAINS",
    "TEXT_STARTS_WITH",
    "TEXT_ENDS_WITH",
    "TEXT_EQ",
    "DATE_BEFORE",
    "DATE_ON_OR_BEFORE",
    "DATE_AFTER",
    "DATE_ON_OR_AFTER",
    "DATE_EQ",
    "DATE_NOT_EQ",
    "DATE_BETWEEN",
    "DATE_NOT_BETWEEN",
    "NOT_BLANK",
    "BLANK",
    "CUSTOM_FORMULA",
    "ONE_OF_RANGE",
}

GRADIENT_POINT_TYPES = {"MIN", "MAX", "NUMBER", "PERCENT", "PERCENTILE"}


async def _fetch_sheets_with_rules(
    service, spreadsheet_id: str
) -> tuple[List[dict], dict[int, str]]:
    """
    Fetch sheets with titles and conditional format rules in a single request.
    """
    response = await asyncio.to_thread(
        service.spreadsheets()
        .get(
            spreadsheetId=spreadsheet_id,
            fields="sheets(properties(sheetId,title),conditionalFormats)",
        )
        .execute
    )
    sheets = response.get("sheets", []) or []
    sheet_titles: dict[int, str] = {}
    for sheet in sheets:
        props = sheet.get("properties", {})
        sid = props.get("sheetId")
        if sid is not None:
            sheet_titles[sid] = props.get("title", f"Sheet {sid}")
    return sheets, sheet_titles


def _select_sheet(sheets: List[dict], sheet_name: Optional[str]) -> dict:
    """
    Select a sheet by name, or default to the first sheet if name is not provided.
    """
    if not sheets:
        raise UserInputError("Spreadsheet has no sheets.")

    if sheet_name is None:
        return sheets[0]

    for sheet in sheets:
        if sheet.get("properties", {}).get("title") == sheet_name:
            return sheet

    available_titles = [
        sheet.get("properties", {}).get("title", "Untitled") for sheet in sheets
    ]
    raise UserInputError(
        f"Sheet '{sheet_name}' not found. Available sheets: {', '.join(available_titles)}."
    )


def _parse_condition_values(
    condition_values: Optional[Union[str, List[Union[str, int, float]]]],
) -> Optional[List[Union[str, int, float]]]:
    """
    Normalize and validate condition_values into a list of strings/numbers.
    """
    parsed = condition_values
    if isinstance(parsed, str):
        try:
            parsed = json.loads(parsed)
        except json.JSONDecodeError as exc:
            raise UserInputError(
                "condition_values must be a list or a JSON-encoded list (e.g., '[\"=$B2>1000\"]')."
            ) from exc

    if parsed is not None and not isinstance(parsed, list):
        parsed = [parsed]

    if parsed:
        for idx, val in enumerate(parsed):
            if not isinstance(val, (str, int, float)):
                raise UserInputError(
                    f"condition_values[{idx}] must be a string or number, got {type(val).__name__}."
                )

    return parsed


def _parse_gradient_points(
    gradient_points: Optional[Union[str, List[dict]]],
) -> Optional[List[dict]]:
    """
    Normalize gradient points into a list of dicts with type/value/color.
    Each point must have a 'type' (MIN, MAX, NUMBER, PERCENT, PERCENTILE) and a color.
    """
    if gradient_points is None:
        return None

    parsed = gradient_points
    if isinstance(parsed, str):
        try:
            parsed = json.loads(parsed)
        except json.JSONDecodeError as exc:
            raise UserInputError(
                "gradient_points must be a list or JSON-encoded list of points "
                '(e.g., \'[{"type":"MIN","color":"#ffffff"}, {"type":"MAX","color":"#ff0000"}]\').'
            ) from exc

    if not isinstance(parsed, list):
        raise UserInputError("gradient_points must be a list of point objects.")

    if len(parsed) < 2 or len(parsed) > 3:
        raise UserInputError("Provide 2 or 3 gradient points (min/max or min/mid/max).")

    normalized_points: List[dict] = []
    for idx, point in enumerate(parsed):
        if not isinstance(point, dict):
            raise UserInputError(
                f"gradient_points[{idx}] must be an object with type/color."
            )

        point_type = point.get("type")
        if not point_type or point_type.upper() not in GRADIENT_POINT_TYPES:
            raise UserInputError(
                f"gradient_points[{idx}].type must be one of {sorted(GRADIENT_POINT_TYPES)}."
            )
        color_raw = point.get("color")
        color_dict = (
            _parse_hex_color(color_raw)
            if not isinstance(color_raw, dict)
            else color_raw
        )
        if not color_dict:
            raise UserInputError(f"gradient_points[{idx}].color is required.")

        normalized = {"type": point_type.upper(), "color": color_dict}
        if "value" in point and point["value"] is not None:
            normalized["value"] = str(point["value"])
        normalized_points.append(normalized)

    return normalized_points


def _build_boolean_rule(
    ranges: List[dict],
    condition_type: str,
    condition_values: Optional[List[Union[str, int, float]]],
    background_color: Optional[str],
    text_color: Optional[str],
) -> tuple[dict, str]:
    """
    Build a Sheets boolean conditional formatting rule payload.
    Returns the rule and the normalized condition type.
    """
    if not background_color and not text_color:
        raise UserInputError(
            "Provide at least one of background_color or text_color for the rule format."
        )

    cond_type_normalized = condition_type.upper()
    if cond_type_normalized not in CONDITION_TYPES:
        raise UserInputError(
            f"condition_type must be one of {sorted(CONDITION_TYPES)}."
        )

    condition = {"type": cond_type_normalized}
    if condition_values:
        condition["values"] = [
            {"userEnteredValue": str(value)} for value in condition_values
        ]

    bg_color_parsed = _parse_hex_color(background_color)
    text_color_parsed = _parse_hex_color(text_color)

    format_obj = {}
    if bg_color_parsed:
        format_obj["backgroundColor"] = bg_color_parsed
    if text_color_parsed:
        format_obj["textFormat"] = {"foregroundColor": text_color_parsed}

    return (
        {
            "ranges": ranges,
            "booleanRule": {
                "condition": condition,
                "format": format_obj,
            },
        },
        cond_type_normalized,
    )


def _build_gradient_rule(
    ranges: List[dict],
    gradient_points: List[dict],
) -> dict:
    """
    Build a Sheets gradient conditional formatting rule payload.
    """
    rule_body: dict = {"ranges": ranges, "gradientRule": {}}
    if len(gradient_points) == 2:
        rule_body["gradientRule"]["minpoint"] = gradient_points[0]
        rule_body["gradientRule"]["maxpoint"] = gradient_points[1]
    else:
        rule_body["gradientRule"]["minpoint"] = gradient_points[0]
        rule_body["gradientRule"]["midpoint"] = gradient_points[1]
        rule_body["gradientRule"]["maxpoint"] = gradient_points[2]
    return rule_body


def _extract_cell_notes_from_grid(spreadsheet: dict) -> list[dict[str, str]]:
    """
    Extract cell notes from spreadsheet grid data.

    Returns a list of dictionaries with:
        - "cell": cell A1 reference
        - "note": the note text
    """
    notes: list[dict[str, str]] = []
    for sheet in spreadsheet.get("sheets", []) or []:
        sheet_title = sheet.get("properties", {}).get("title") or "Unknown"
        for grid in sheet.get("data", []) or []:
            start_row = _coerce_int(grid.get("startRow"), default=0)
            start_col = _coerce_int(grid.get("startColumn"), default=0)
            for row_offset, row_data in enumerate(grid.get("rowData", []) or []):
                if not row_data:
                    continue
                for col_offset, cell_data in enumerate(
                    row_data.get("values", []) or []
                ):
                    if not cell_data:
                        continue
                    note = cell_data.get("note")
                    if not note:
                        continue
                    notes.append(
                        {
                            "cell": _format_a1_cell(
                                sheet_title,
                                start_row + row_offset,
                                start_col + col_offset,
                            ),
                            "note": note,
                        }
                    )
    return notes


async def _fetch_sheet_notes(
    service, spreadsheet_id: str, a1_range: str
) -> list[dict[str, str]]:
    """Fetch cell notes for the given range via spreadsheets.get with includeGridData."""
    response = await asyncio.to_thread(
        service.spreadsheets()
        .get(
            spreadsheetId=spreadsheet_id,
            ranges=[a1_range],
            includeGridData=True,
            fields="sheets(properties(title),data(startRow,startColumn,rowData(values(note))))",
        )
        .execute
    )
    return _extract_cell_notes_from_grid(response)


def _format_sheet_notes_section(
    *, notes: list[dict[str, str]], range_label: str, max_details: int = 25
) -> str:
    """
    Format a list of cell notes into a human-readable section.
    """
    if not notes:
        return ""

    lines = []
    for item in notes[:max_details]:
        cell = item.get("cell") or "(unknown cell)"
        note = item.get("note") or "(empty note)"
        lines.append(f"- {cell}: {note}")

    suffix = (
        f"\n... and {len(notes) - max_details} more notes"
        if len(notes) > max_details
        else ""
    )
    return f"\n\nCell notes in range '{range_label}':\n" + "\n".join(lines) + suffix


async def _fetch_cell_formulas(
    service,
    spreadsheet_id: str,
    resolved_range: str,
) -> tuple[str, List[List[object]]]:
    """Fetch formula strings for cells in the given range.

    Makes a second values().get() call with valueRenderOption="FORMULA" and
    returns a formatted section listing any cells whose value starts with "=".
    Cells containing plain values are silently skipped.

    Returns an empty section and empty values list if the request fails.
    """
    try:
        result = await asyncio.to_thread(
            service.spreadsheets()
            .values()
            .get(
                spreadsheetId=spreadsheet_id,
                range=resolved_range,
                valueRenderOption="FORMULA",
            )
            .execute
        )
    except Exception as exc:
        logger.warning(
            "[read_sheet_values] Failed fetching formula values for range '%s': %s",
            resolved_range,
            exc,
        )
        return "", []

    formula_values = result.get("values", [])
    formulas: list[dict[str, str]] = []

    sheet_name, range_part = _split_sheet_and_range(resolved_range)
    start_part = range_part.split(":")[0] if ":" in range_part else range_part
    start_col_idx, start_row_idx = _parse_a1_part(start_part)
    base_col = start_col_idx if start_col_idx is not None else 0
    base_row = start_row_idx if start_row_idx is not None else 0

    for row_offset, formula_row in enumerate(formula_values):
        for col_offset, cell_value in enumerate(formula_row):
            if isinstance(cell_value, str) and cell_value.startswith("="):
                abs_col = base_col + col_offset
                abs_row = base_row + row_offset
                cell_ref = f"{_index_to_column(abs_col)}{abs_row + 1}"
                if sheet_name:
                    cell_ref = f"{_quote_sheet_title_for_a1(sheet_name)}!{cell_ref}"
                formulas.append({"cell": cell_ref, "formula": cell_value})

    return (
        _format_sheet_formula_section(formulas=formulas, range_label=resolved_range),
        formula_values,
    )


def _format_sheet_formula_section(
    *, formulas: list[dict[str, str]], range_label: str, max_details: int = 50
) -> str:
    """Format a list of formula cells into a human-readable section."""
    if not formulas:
        return ""

    lines = []
    for item in formulas[:max_details]:
        cell = item.get("cell") or "(unknown cell)"
        formula = item.get("formula") or "(empty formula)"
        lines.append(f"- {cell}: {formula}")

    suffix = (
        f"\n... and {len(formulas) - max_details} more formula cells"
        if len(formulas) > max_details
        else ""
    )
    return f"\n\nFormula cells in range '{range_label}':\n" + "\n".join(lines) + suffix


async def _fetch_grid_metadata(
    service,
    spreadsheet_id: str,
    resolved_range: str,
    values: List[List[object]],
    include_hyperlinks: bool = False,
    include_notes: bool = False,
    include_smart_chips: bool = False,
) -> tuple[str, str, str]:
    """Fetch hyperlinks, notes, and/or smart chips for a range via a single spreadsheets.get call.

    Computes tight range bounds, enforces the cell-count cap, builds a combined
    ``fields`` selector so only one API round-trip is needed when multiple flags are
    ``True``, then parses the response into formatted output sections.

    Returns:
        (hyperlink_section, notes_section, smart_chips_section) — each is an empty string when the
        corresponding flag is ``False`` or no data was found.
    """
    if not include_hyperlinks and not include_notes and not include_smart_chips:
        return "", "", ""

    tight_range = _a1_range_for_values(resolved_range, values)
    if not tight_range:
        logger.info(
            "[read_sheet_values] Skipping grid metadata fetch for range '%s': "
            "unable to determine tight bounds",
            resolved_range,
        )
        return "", "", ""

    cell_count = _a1_range_cell_count(tight_range) or sum(len(row) for row in values)
    if cell_count > MAX_GRID_METADATA_CELLS:
        logger.info(
            "[read_sheet_values] Skipping grid metadata fetch for large range "
            "'%s' (%d cells > %d limit)",
            tight_range,
            cell_count,
            MAX_GRID_METADATA_CELLS,
        )
        return "", "", ""

    # Build a combined fields selector so we hit the API at most once.
    value_fields: list[str] = []
    if include_hyperlinks:
        value_fields.extend(["hyperlink", "textFormatRuns(format(link(uri)))"])
    if include_notes:
        value_fields.append("note")
    if include_smart_chips:
        value_fields.append("chipRuns")

    fields = (
        "sheets(properties(title),data(startRow,startColumn,"
        f"rowData(values({','.join(value_fields)}))))"
    )

    try:
        response = await asyncio.to_thread(
            service.spreadsheets()
            .get(
                spreadsheetId=spreadsheet_id,
                ranges=[tight_range],
                includeGridData=True,
                fields=fields,
            )
            .execute
        )
    except Exception as exc:
        logger.warning(
            "[read_sheet_values] Failed fetching grid metadata for range '%s': %s",
            tight_range,
            exc,
        )
        return "", "", ""

    hyperlink_section = ""
    if include_hyperlinks:
        hyperlinks = _extract_cell_hyperlinks_from_grid(response)
        hyperlink_section = _format_sheet_hyperlink_section(
            hyperlinks=hyperlinks, range_label=tight_range
        )

    notes_section = ""
    if include_notes:
        notes = _extract_cell_notes_from_grid(response)
        notes_section = _format_sheet_notes_section(
            notes=notes, range_label=tight_range
        )

    smart_chips_section = ""
    if include_smart_chips:
        smart_chips = _extract_cell_smart_chips_from_grid(response)
        smart_chips_section = _format_sheet_smart_chips_section(
            smart_chips=smart_chips, range_label=tight_range
        )

    return hyperlink_section, notes_section, smart_chips_section


def _extract_cell_smart_chips_from_grid(spreadsheet: dict) -> list[dict[str, str]]:
    """
    Extract Drive and People smart chips from spreadsheet grid data.

    Returns a list of dictionaries with:
        - "cell": cell A1 reference
        - "type": "drive" | "person"
        - "value": Drive link URI or person email
    """
    smart_chips: list[dict[str, str]] = []
    for sheet in spreadsheet.get("sheets", []) or []:
        sheet_title = sheet.get("properties", {}).get("title") or "Unknown"
        for grid in sheet.get("data", []) or []:
            start_row = _coerce_int(grid.get("startRow"), default=0)
            start_col = _coerce_int(grid.get("startColumn"), default=0)
            for row_offset, row_data in enumerate(grid.get("rowData", []) or []):
                if not row_data:
                    continue
                for col_offset, cell_data in enumerate(
                    row_data.get("values", []) or []
                ):
                    if not cell_data:
                        continue
                    for chip_run in cell_data.get("chipRuns") or []:
                        # Reads also return plain-text runs, which carry an empty chip.
                        chip = chip_run.get("chip") or {}
                        if "richLinkProperties" in chip:
                            chip_type = "drive"
                            value = chip["richLinkProperties"].get("uri")
                        elif "personProperties" in chip:
                            chip_type = "person"
                            value = chip["personProperties"].get("email")
                        else:
                            continue
                        smart_chips.append(
                            {
                                "cell": _format_a1_cell(
                                    sheet_title,
                                    start_row + row_offset,
                                    start_col + col_offset,
                                ),
                                "type": chip_type,
                                "value": value or "",
                            }
                        )
    return smart_chips


def _format_sheet_smart_chips_section(
    *, smart_chips: list[dict[str, str]], range_label: str, max_details: int = 25
) -> str:
    """Format a list of smart chips into a human-readable section."""
    if not smart_chips:
        return ""

    labels = {"drive": "Drive Chip", "person": "Person Chip"}
    lines = [
        f"- {item['cell']}: [{labels[item['type']]}] {item['value']}"
        for item in smart_chips[:max_details]
    ]
    suffix = (
        f"\n... and {len(smart_chips) - max_details} more smart chips"
        if len(smart_chips) > max_details
        else ""
    )
    return f"\n\nSmart Chips in range '{range_label}':\n" + "\n".join(lines) + suffix


def _parse_single_chip_properties(
    item: Any, default_type: Optional[str] = None
) -> tuple[str, dict]:
    """Parse a single chip item (str or dict) into (chip_type, chip_properties)."""
    if item is None or item == "":
        raise UserInputError("Empty chip item cannot be parsed.")

    chip_type = default_type.lower() if default_type else None
    uri = None
    email = None

    if isinstance(item, dict):
        if "type" in item:
            chip_type = str(item["type"]).lower()
        uri = item.get("uri") or item.get("url")
        email = item.get("email")
        if not uri and not email:
            if "folder_id" in item:
                uri = f"https://drive.google.com/drive/folders/{item['folder_id']}"
                chip_type = "drive"
            elif "file_id" in item:
                uri = f"https://drive.google.com/file/d/{item['file_id']}/view"
                chip_type = "drive"
            elif "id" in item:
                uri = f"https://drive.google.com/open?id={item['id']}"
                chip_type = "drive"
    elif isinstance(item, str):
        val = item.strip()
        if not chip_type:
            if val.startswith(("http://", "https://")) or "drive.google.com" in val:
                chip_type = "drive"
                uri = val
            elif "@" in val and not val.startswith("http"):
                chip_type = "person"
                email = val.replace("mailto:", "").strip()
            elif len(val) >= 20 and re.match(r"^[A-Za-z0-9_-]+$", val):
                chip_type = "drive"
                uri = f"https://drive.google.com/open?id={val}"
            else:
                raise UserInputError(
                    f"Cannot infer chip type from '{val}'. Pass a Drive URL or ID, "
                    "an email address, or set chip_type explicitly."
                )
        elif chip_type == "drive":
            if val.startswith(("http://", "https://")):
                uri = val
            else:
                uri = f"https://drive.google.com/open?id={val}"
        elif chip_type == "person":
            email = val.replace("mailto:", "").strip()
    else:
        raise UserInputError(
            f"Unsupported chip item type: {type(item).__name__} ({item})"
        )

    if chip_type == "drive":
        if not uri:
            raise UserInputError(
                f"Drive chip requires a URI or file/folder ID, got: {item}"
            )
        return "drive", {"richLinkProperties": {"uri": uri}}
    elif chip_type == "person":
        if not email:
            raise UserInputError(f"Person chip requires an email address, got: {item}")
        return "person", {"personProperties": {"email": email}}
    else:
        raise UserInputError(
            f"Unknown chip_type '{chip_type}'. Supported types: 'drive', 'person'."
        )


def _create_chip_cell_data(
    item: Any, default_type: Optional[str] = None
) -> Optional[dict]:
    """Construct a CellData dictionary containing smart chip(s) for Sheets API.

    Args:
        item: A single chip (str or dict), or a list of chips for a single cell.
        default_type: Optional default chip type ("drive" or "person").

    Returns:
        A CellData dictionary with userEnteredValue and chipRuns, or None if item is empty.
    """
    if item is None or item == "" or item == []:
        return None

    # Multi-chips in a single cell: list or tuple of chip items
    if isinstance(item, (list, tuple)):
        active_items = [x for x in item if x is not None and x != ""]
        if not active_items:
            return None
        chip_runs = []
        for idx, sub_item in enumerate(active_items):
            _, chip_props = _parse_single_chip_properties(sub_item, default_type)
            chip_runs.append(
                {
                    "startIndex": idx * 2,
                    "chip": chip_props,
                }
            )
        return {
            "userEnteredValue": {"stringValue": " ".join(["@"] * len(active_items))},
            "chipRuns": chip_runs,
        }

    _, chip_props = _parse_single_chip_properties(item, default_type)
    return {
        "userEnteredValue": {"stringValue": "@"},
        "chipRuns": [
            {
                "startIndex": 0,
                "chip": chip_props,
            }
        ],
    }


def _normalize_chips_input(
    chips: Any,
    start_row: Optional[int],
    end_row: Optional[int],
    start_col: Optional[int],
    end_col: Optional[int],
    default_chip_type: Optional[str] = None,
) -> list[tuple[int, int, dict]]:
    """Parse input chips and map each chip to its (row_idx, col_idx, cell_data).

    Args:
        chips: Raw chips parameter (string, list, list of lists, dict).
        start_row: Zero-based start row index.
        end_row: Zero-based end row index (inclusive).
        start_col: Zero-based start column index.
        end_col: Zero-based end column index (inclusive).
        default_chip_type: Optional chip type override ("drive", "person").

    Returns:
        List of (row_idx, col_idx, cell_data) tuples.
    """
    if isinstance(chips, str):
        try:
            parsed = json.loads(chips)
            if isinstance(parsed, (list, dict, str)):
                chips = parsed
        except (json.JSONDecodeError, ValueError):
            pass

    start_r = start_row if start_row is not None else 0
    start_c = start_col if start_col is not None else 0

    updates: list[tuple[int, int, dict]] = []

    is_single_row = (
        start_row is not None and end_row is not None and start_row == end_row
    )
    is_single_col = (
        start_col is not None and end_col is not None and start_col == end_col
    )
    is_single_cell = is_single_row and is_single_col

    # Case 1: Single cell target (e.g. F3 or F3:F3)
    # Any chips provided (single item, list of chips, or nested list [[c1, c2]]) go into this single cell
    if is_single_cell:
        if isinstance(chips, list) and len(chips) == 1 and isinstance(chips[0], list):
            chips = chips[0]
        cell_data = _create_chip_cell_data(chips, default_chip_type)
        if cell_data is not None:
            updates.append((start_r, start_c, cell_data))
        return updates

    num_cols = (end_col - start_c + 1) if end_col is not None else None
    num_rows = (end_row - start_r + 1) if end_row is not None else None
    is_2d_grid = (num_cols is None or num_cols > 1) and (
        num_rows is None or num_rows > 1
    )

    # Case 2: 2D table grid (where chips is a list of rows)
    if (
        is_2d_grid
        and isinstance(chips, list)
        and len(chips) > 0
        and isinstance(chips[0], list)
    ):
        if end_row is not None and (start_r + len(chips) - 1) > end_row:
            raise UserInputError(
                f"2D chips row count ({len(chips)}) exceeds target range row bound ({num_rows} rows)."
            )
        for r_offset, row in enumerate(chips):
            r_idx = start_r + r_offset
            if end_col is not None and (start_c + len(row) - 1) > end_col:
                raise UserInputError(
                    f"2D chips column count ({len(row)}) exceeds target range column bound ({num_cols} cols)."
                )
            for c_offset, item in enumerate(row):
                c_idx = start_c + c_offset
                cell_data = _create_chip_cell_data(item, default_chip_type)
                if cell_data is not None:
                    updates.append((r_idx, c_idx, cell_data))
        return updates

    # Case 3: 1D list of items (each item can be a single chip or a list of chips for that cell)
    if isinstance(chips, list):
        if is_single_row and not is_single_col:
            # Horizontal fill (e.g. A1:C1)
            if end_col is not None and (start_c + len(chips) - 1) > end_col:
                raise UserInputError(
                    f"Number of chips ({len(chips)}) exceeds horizontal range capacity ({end_col - start_c + 1} cells)."
                )
            for c_offset, item in enumerate(chips):
                c_idx = start_c + c_offset
                cell_data = _create_chip_cell_data(item, default_chip_type)
                if cell_data is not None:
                    updates.append((start_r, c_idx, cell_data))
        elif is_single_col or (end_row is None and end_col is None):
            # Vertical fill (e.g. F3:F23 or F3:F)
            if end_row is not None and (start_r + len(chips) - 1) > end_row:
                raise UserInputError(
                    f"Number of chips ({len(chips)}) exceeds vertical range capacity ({end_row - start_r + 1} cells)."
                )
            for r_offset, item in enumerate(chips):
                r_idx = start_r + r_offset
                cell_data = _create_chip_cell_data(item, default_chip_type)
                if cell_data is not None:
                    updates.append((r_idx, start_c, cell_data))
        else:
            # 2D range in row-major order
            ncols = num_cols if num_cols is not None else 1
            nrows = num_rows if num_rows is not None else 1
            max_capacity = ncols * nrows
            if end_row is not None and len(chips) > max_capacity:
                raise UserInputError(
                    f"Number of chips ({len(chips)}) exceeds 2D range capacity ({max_capacity} cells)."
                )
            for i, item in enumerate(chips):
                r_idx = start_r + (i // ncols)
                c_idx = start_c + (i % ncols)
                cell_data = _create_chip_cell_data(item, default_chip_type)
                if cell_data is not None:
                    updates.append((r_idx, c_idx, cell_data))
        return updates

    # Case 4: Single item (string or dict) for a multi-cell range -> fill all cells in range
    single_cell_data = _create_chip_cell_data(chips, default_chip_type)
    if single_cell_data is None:
        return []

    if end_row is not None and end_col is not None:
        for r_idx in range(start_r, end_row + 1):
            for c_idx in range(start_c, end_col + 1):
                updates.append((r_idx, c_idx, single_cell_data))
    else:
        updates.append((start_r, start_c, single_cell_data))

    return updates


def _find_named_range(
    named_ranges: List[dict],
    target_id: Optional[str] = None,
    target_name: Optional[str] = None,
) -> Optional[dict]:
    """Find a named range in a list of named ranges by ID or name.

    Matches by target_id first if provided, then by target_name (exact match first,
    then case-insensitive match).
    """
    if target_id:
        target_id_str = str(target_id).strip()
        for nr in named_ranges:
            if nr.get("namedRangeId") == target_id_str:
                return nr

    if target_name:
        name_clean = target_name.strip()
        name_lower = name_clean.lower()
        # Exact match
        for nr in named_ranges:
            if nr.get("name") == name_clean:
                return nr
        # Case-insensitive match fallback
        for nr in named_ranges:
            if (nr.get("name") or "").strip().lower() == name_lower:
                return nr

    return None


def _format_named_ranges_list(
    named_ranges: List[dict],
    sheet_titles: dict[int, str],
    spreadsheet_id: str,
    user_google_email: str,
) -> str:
    """Format a list of named ranges into a human-readable markdown table."""
    if not named_ranges:
        return f"No named ranges found in spreadsheet '{spreadsheet_id}' for {user_google_email}."

    header = (
        f"Found {len(named_ranges)} named range(s) in spreadsheet '{spreadsheet_id}' for {user_google_email}:\n\n"
        "| Name | Range | Named Range ID |\n"
        "| :--- | :--- | :--- |\n"
    )
    rows = []
    for nr in named_ranges:
        nr_name = nr.get("name", "(unnamed)")
        nr_id = nr.get("namedRangeId", "(unknown)")
        grid_range = nr.get("range", {})
        a1_repr = _grid_range_to_a1(grid_range, sheet_titles)
        rows.append(f"| {nr_name} | {a1_repr} | {nr_id} |")

    return header + "\n".join(rows)


def _build_insert_comment_request(
    cell: str, sheets: List[dict], comment_content: str
) -> dict:
    """Build an insertComment request anchored to a single A1 cell."""
    grid_range = _parse_a1_range(cell, sheets)
    start_row = grid_range.get("startRowIndex")
    start_col = grid_range.get("startColumnIndex")
    is_single_cell = (
        start_row is not None
        and start_col is not None
        and grid_range.get("endRowIndex") == start_row + 1
        and grid_range.get("endColumnIndex") == start_col + 1
    )
    if not is_single_cell:
        raise UserInputError(
            f"cell must reference a single cell (e.g., 'Sheet1!B2'), got '{cell}'."
        )
    return {
        "insertComment": {
            "content": comment_content,
            "coordinate": {
                "sheetId": grid_range["sheetId"],
                "rowIndex": start_row,
                "columnIndex": start_col,
            },
        }
    }


async def _insert_cell_comment(
    sheets_service, spreadsheet_id: str, cell: str, comment_content: str
) -> str:
    """Create a comment anchored to a cell via the Sheets API; return its ID."""
    metadata = await asyncio.to_thread(
        sheets_service.spreadsheets()
        .get(spreadsheetId=spreadsheet_id, fields="sheets(properties(sheetId,title))")
        .execute
    )
    request = _build_insert_comment_request(
        cell, metadata.get("sheets", []), comment_content
    )

    response = await asyncio.to_thread(
        sheets_service.spreadsheets()
        .batchUpdate(spreadsheetId=spreadsheet_id, body={"requests": [request]})
        .execute
    )

    replies = response.get("replies") or [{}]
    thread = replies[0].get("insertComment", {}).get("commentThread", {})
    comment_id = thread.get("commentId")
    if not comment_id:
        raise RuntimeError(
            f"Sheets API did not return a comment ID for {cell}; check list_spreadsheet_comments before retrying."
        )
    return comment_id


# ---------------------------------------------------------------------------
# Shared fetch / range helpers for the batchUpdate-backed tools
# ---------------------------------------------------------------------------


async def _fetch_sheets(
    service,
    spreadsheet_id: str,
    fields: str = "sheets(properties(sheetId,title))",
) -> List[dict]:
    """Fetch the spreadsheet's sheets with the given partial-response fields."""
    response = await asyncio.to_thread(
        service.spreadsheets().get(spreadsheetId=spreadsheet_id, fields=fields).execute
    )
    return response.get("sheets", []) or []


def _sheet_titles_by_id(sheets: List[dict]) -> dict[int, str]:
    """Map sheetId -> title for every sheet that has an ID."""
    titles: dict[int, str] = {}
    for sheet in sheets:
        props = sheet.get("properties", {}) or {}
        sid = props.get("sheetId")
        if sid is not None:
            titles[sid] = props.get("title", f"Sheet {sid}")
    return titles


def _resolve_scope_grid_range(
    range_name: Optional[str], sheet_name: Optional[str], sheets: List[dict]
) -> dict:
    """
    Resolve a tool's (range_name, sheet_name) pair to a GridRange.

    - range_name with a sheet prefix wins outright.
    - range_name without a prefix is scoped to sheet_name (or the first sheet).
    - No range_name means the whole of sheet_name (or the first sheet).
    """
    if range_name:
        if sheet_name and "!" not in range_name:
            range_name = f"{_quote_sheet_title_for_a1(sheet_name)}!{range_name}"
        return _parse_a1_range(range_name, sheets)
    sheet = _select_sheet(sheets, sheet_name)
    return {"sheetId": sheet["properties"]["sheetId"]}


def _grid_coordinate_from_a1(cell: str, sheets: List[dict]) -> dict:
    """Convert a single-cell A1 reference (e.g. 'Sheet1!D2') to a GridCoordinate."""
    grid = _parse_a1_range(cell, sheets)
    start_row = grid.get("startRowIndex")
    start_col = grid.get("startColumnIndex")
    if start_row is None or start_col is None:
        raise UserInputError(
            f"'{cell}' must be a single cell with both a column and a row (e.g. 'D2')."
        )
    if (
        grid.get("endRowIndex", start_row + 1) != start_row + 1
        or grid.get("endColumnIndex", start_col + 1) != start_col + 1
    ):
        raise UserInputError(f"'{cell}' must be a single cell, not a range.")
    return {"sheetId": grid["sheetId"], "rowIndex": start_row, "columnIndex": start_col}


def _parse_json_list(value: Any, name: str) -> List[Any]:
    """Accept a list or a JSON-encoded list; anything else is a UserInputError."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise UserInputError(
                f"{name} must be a list or a JSON-encoded list."
            ) from exc
    if not isinstance(value, list):
        raise UserInputError(f"{name} must be a list, got {type(value).__name__}.")
    return value


def _resolve_column_index(column: Any, grid_range: dict, label: str) -> int:
    """
    Turn a column given as letters ('B') or a zero-based index (1) into the
    absolute zero-based column index, checking it lies inside grid_range.
    """
    if isinstance(column, bool) or column is None:
        raise UserInputError(f"{label}: 'column' is required (letters or index).")
    if isinstance(column, int):
        index = column
    elif isinstance(column, str) and column.strip():
        text = column.strip()
        index = _column_to_index(text)
        if index is None:
            if text.isdigit():
                index = int(text)
            else:
                raise UserInputError(
                    f"{label}: column '{column}' is not valid letters or an index."
                )
    else:
        raise UserInputError(f"{label}: column must be letters or a zero-based index.")
    if index < 0:
        raise UserInputError(f"{label}: column index must be non-negative.")
    start = grid_range.get("startColumnIndex")
    end = grid_range.get("endColumnIndex")
    if (start is not None and index < start) or (end is not None and index >= end):
        raise UserInputError(
            f"{label}: column {_index_to_column(index)} is outside the range's columns."
        )
    return index


# ---------------------------------------------------------------------------
# batch_update_sheet passthrough
# ---------------------------------------------------------------------------

# Every request type accepted by spreadsheets.batchUpdate, taken from the
# `Request` schema of the Sheets v4 discovery document.
BATCH_UPDATE_REQUEST_TYPES = frozenset(
    {
        "addBanding",
        "addChart",
        "addCommentReply",
        "addConditionalFormatRule",
        "addDataSource",
        "addDimensionGroup",
        "addFilterView",
        "addNamedRange",
        "addProtectedRange",
        "addSheet",
        "addSlicer",
        "addTable",
        "appendCells",
        "appendDimension",
        "autoFill",
        "autoResizeDimensions",
        "cancelDataSourceRefresh",
        "clearBasicFilter",
        "copyPaste",
        "createDeveloperMetadata",
        "cutPaste",
        "deleteBanding",
        "deleteComment",
        "deleteCommentReply",
        "deleteConditionalFormatRule",
        "deleteDataSource",
        "deleteDeveloperMetadata",
        "deleteDimension",
        "deleteDimensionGroup",
        "deleteDuplicates",
        "deleteEmbeddedObject",
        "deleteFilterView",
        "deleteNamedRange",
        "deleteProtectedRange",
        "deleteRange",
        "deleteSheet",
        "deleteTable",
        "duplicateFilterView",
        "duplicateSheet",
        "findReplace",
        "insertComment",
        "insertDimension",
        "insertRange",
        "mergeCells",
        "moveDimension",
        "pasteData",
        "randomizeRange",
        "refreshDataSource",
        "repeatCell",
        "setBasicFilter",
        "setDataValidation",
        "sortRange",
        "textToColumns",
        "trimWhitespace",
        "unmergeCells",
        "updateBanding",
        "updateBorders",
        "updateCells",
        "updateChartSpec",
        "updateCommentPost",
        "updateConditionalFormatRule",
        "updateDataSource",
        "updateDeveloperMetadata",
        "updateDimensionGroup",
        "updateDimensionProperties",
        "updateEmbeddedObjectBorder",
        "updateEmbeddedObjectPosition",
        "updateFilterView",
        "updateNamedRange",
        "updateProtectedRange",
        "updateSheetProperties",
        "updateSlicerSpec",
        "updateSpreadsheetProperties",
        "updateTable",
    }
)


def _validate_batch_update_requests(requests: Any) -> List[dict]:
    """
    Check that `requests` is a non-empty list of single-key dicts whose key is
    a known batchUpdate request type. Returns the (possibly JSON-decoded) list.
    """
    if isinstance(requests, str):
        try:
            requests = json.loads(requests)
        except json.JSONDecodeError as exc:
            raise UserInputError(
                "requests must be a list of request objects or a JSON-encoded list."
            ) from exc
    if not isinstance(requests, list) or not requests:
        raise UserInputError("requests must be a non-empty list of request objects.")

    for idx, request in enumerate(requests):
        if not isinstance(request, dict) or len(request) != 1:
            raise UserInputError(
                f"requests[{idx}] must be an object with exactly one key naming the "
                'request type, e.g. {"addSheet": {"properties": {"title": "Q3"}}}.'
            )
        (request_type,) = request
        if request_type not in BATCH_UPDATE_REQUEST_TYPES:
            close = difflib.get_close_matches(
                str(request_type), BATCH_UPDATE_REQUEST_TYPES, n=3
            )
            hint = (
                f" Did you mean: {', '.join(close)}?"
                if close
                else f" Known types: {', '.join(sorted(BATCH_UPDATE_REQUEST_TYPES))}."
            )
            raise UserInputError(
                f"requests[{idx}]: unknown batchUpdate request type '{request_type}'."
                + hint
            )
        if not isinstance(request[request_type], dict):
            raise UserInputError(
                f"requests[{idx}].{request_type} must be an object, "
                f"got {type(request[request_type]).__name__}."
            )
    return requests


def _summarize_batch_update_reply(request_type: str, reply: Any) -> str:
    """One-line, ID-bearing summary of a single batchUpdate reply."""
    body = reply.get(request_type) if isinstance(reply, dict) else None
    if not body:
        return "completed"

    if request_type in ("addSheet", "duplicateSheet"):
        props = body.get("properties", {}) or {}
        return (
            f"sheet '{props.get('title', 'Untitled')}' (sheetId {props.get('sheetId')})"
        )
    if request_type == "addNamedRange":
        named = body.get("namedRange", {}) or {}
        return f"named range '{named.get('name')}' (namedRangeId {named.get('namedRangeId')})"
    if request_type in ("addFilterView", "duplicateFilterView"):
        view = body.get("filter", {}) or {}
        return f"filter view '{view.get('title')}' (filterViewId {view.get('filterViewId')})"
    if request_type == "addProtectedRange":
        protected = body.get("protectedRange", {}) or {}
        return f"protected range (protectedRangeId {protected.get('protectedRangeId')})"
    if request_type == "addChart":
        chart = body.get("chart", {}) or {}
        return f"chart (chartId {chart.get('chartId')})"
    if request_type == "addBanding":
        banded = body.get("bandedRange", {}) or {}
        return f"banded range (bandedRangeId {banded.get('bandedRangeId')})"
    if request_type == "addSlicer":
        slicer = body.get("slicer", {}) or {}
        return f"slicer (slicerId {slicer.get('slicerId')})"
    if request_type == "addTable":
        table = body.get("table", {}) or {}
        return f"table '{table.get('name')}' (tableId {table.get('tableId')})"
    if request_type in ("addDimensionGroup", "deleteDimensionGroup"):
        return f"{len(body.get('dimensionGroups', []) or [])} dimension group(s) now present"
    if request_type == "findReplace":
        return _format_find_replace_reply(body)
    if request_type == "deleteDuplicates":
        return f"{body.get('duplicatesRemovedCount', 0)} duplicate row(s) removed"
    if request_type == "trimWhitespace":
        return f"{body.get('cellsChangedCount', 0)} cell(s) changed"
    if request_type == "updateConditionalFormatRule":
        return f"rule now at index {body.get('newIndex')}"
    if request_type == "deleteConditionalFormatRule":
        return "conditional format rule deleted"
    if request_type == "createDeveloperMetadata":
        meta = body.get("developerMetadata", {}) or {}
        return f"developer metadata (metadataId {meta.get('metadataId')})"
    if request_type == "updateEmbeddedObjectPosition":
        return "embedded object repositioned"
    if request_type in ("addDataSource", "updateDataSource"):
        source = body.get("dataSource", {}) or {}
        return f"data source (dataSourceId {source.get('dataSourceId')})"
    return "completed"


def _summarize_batch_update_replies(
    requests: List[dict], replies: List[Any]
) -> List[str]:
    """Pair each request with its reply and summarize them in order."""
    lines: List[str] = []
    for idx, request in enumerate(requests, 1):
        (request_type,) = request
        reply = replies[idx - 1] if idx - 1 < len(replies) else {}
        lines.append(
            f"  Request {idx} ({request_type}): "
            f"{_summarize_batch_update_reply(request_type, reply)}"
        )
    return lines


# ---------------------------------------------------------------------------
# format_sheet_range: borders and merges
# ---------------------------------------------------------------------------

BORDER_STYLES = frozenset(
    {"DOTTED", "DASHED", "SOLID", "SOLID_MEDIUM", "SOLID_THICK", "DOUBLE", "NONE"}
)
BORDER_SIDES = ("top", "bottom", "left", "right", "innerHorizontal", "innerVertical")
BORDER_SIDE_GROUPS = {
    "all": BORDER_SIDES,
    "outer": BORDER_SIDES[:4],
    "inner": BORDER_SIDES[4:],
}
_BORDER_SIDE_ALIASES = {
    "top": "top",
    "bottom": "bottom",
    "left": "left",
    "right": "right",
    "innerhorizontal": "innerHorizontal",
    "inner_horizontal": "innerHorizontal",
    "horizontal": "innerHorizontal",
    "innervertical": "innerVertical",
    "inner_vertical": "innerVertical",
    "vertical": "innerVertical",
}
MERGE_TYPES = {"all": "MERGE_ALL", "columns": "MERGE_COLUMNS", "rows": "MERGE_ROWS"}


def _normalize_border_sides(border_sides: Optional[Union[str, List[str]]]) -> List[str]:
    """Expand side names and the 'all'/'outer'/'inner' groups into UpdateBorders keys."""
    if border_sides is None or border_sides == []:
        return list(BORDER_SIDES)
    if isinstance(border_sides, str):
        try:
            border_sides = json.loads(border_sides)
        except json.JSONDecodeError:
            border_sides = [part for part in border_sides.split(",") if part.strip()]
    if not isinstance(border_sides, list):
        raise UserInputError("border_sides must be a list of side names.")

    sides: List[str] = []
    for raw in border_sides:
        key = str(raw).strip().lower()
        if key in BORDER_SIDE_GROUPS:
            resolved = list(BORDER_SIDE_GROUPS[key])
        elif key in _BORDER_SIDE_ALIASES:
            resolved = [_BORDER_SIDE_ALIASES[key]]
        else:
            raise UserInputError(
                f"Unknown border side '{raw}'. Use top, bottom, left, right, "
                "innerHorizontal, innerVertical, or the groups all, outer, inner."
            )
        for side in resolved:
            if side not in sides:
                sides.append(side)
    return sides


def _build_update_borders_request(
    grid_range: dict,
    border_style: str,
    border_color: Optional[str],
    border_sides: Optional[Union[str, List[str]]],
) -> dict:
    """Build an updateBorders request applying one border to the chosen sides."""
    style = (border_style or "").strip().upper()
    if style not in BORDER_STYLES:
        raise UserInputError(f"border_style must be one of {sorted(BORDER_STYLES)}.")
    border: dict = {"style": style}
    color = _parse_hex_color(border_color)
    if color:
        border["color"] = color
    request: dict = {"range": grid_range}
    for side in _normalize_border_sides(border_sides):
        request[side] = dict(border)
    return {"updateBorders": request}


def _build_merge_cells_request(grid_range: dict, merge: str) -> dict:
    """Build a mergeCells request from merge='all'|'columns'|'rows'."""
    key = (merge or "").strip().lower()
    merge_type = MERGE_TYPES.get(key) or (
        key.upper() if key.upper() in MERGE_TYPES.values() else None
    )
    if merge_type is None:
        raise UserInputError("merge must be one of 'all', 'columns' or 'rows'.")
    return {"mergeCells": {"range": grid_range, "mergeType": merge_type}}


# ---------------------------------------------------------------------------
# Sort specs and filter specs (sort_sheet_range, manage_sheet_filter)
# ---------------------------------------------------------------------------

SORT_ORDERS = {
    "ASCENDING": "ASCENDING",
    "ASC": "ASCENDING",
    "DESCENDING": "DESCENDING",
    "DESC": "DESCENDING",
}

# Full BooleanCondition.type enum from the discovery document (minus UNSPECIFIED).
BOOLEAN_CONDITION_TYPES = frozenset(
    {
        "NUMBER_GREATER",
        "NUMBER_GREATER_THAN_EQ",
        "NUMBER_LESS",
        "NUMBER_LESS_THAN_EQ",
        "NUMBER_EQ",
        "NUMBER_NOT_EQ",
        "NUMBER_BETWEEN",
        "NUMBER_NOT_BETWEEN",
        "TEXT_CONTAINS",
        "TEXT_NOT_CONTAINS",
        "TEXT_STARTS_WITH",
        "TEXT_ENDS_WITH",
        "TEXT_EQ",
        "TEXT_NOT_EQ",
        "TEXT_IS_EMAIL",
        "TEXT_IS_URL",
        "DATE_EQ",
        "DATE_NOT_EQ",
        "DATE_BEFORE",
        "DATE_AFTER",
        "DATE_ON_OR_BEFORE",
        "DATE_ON_OR_AFTER",
        "DATE_BETWEEN",
        "DATE_NOT_BETWEEN",
        "DATE_IS_VALID",
        "ONE_OF_RANGE",
        "ONE_OF_LIST",
        "BLANK",
        "NOT_BLANK",
        "CUSTOM_FORMULA",
        "BOOLEAN",
        "FILTER_EXPRESSION",
    }
)
RELATIVE_DATES = frozenset(
    {"PAST_YEAR", "PAST_MONTH", "PAST_WEEK", "YESTERDAY", "TODAY", "TOMORROW"}
)


def _condition_values_payload(
    values: Optional[List[Union[str, int, float]]], condition_type: str
) -> List[dict]:
    """Wrap raw values as ConditionValue objects; date types accept relative dates."""
    payload: List[dict] = []
    for value in values or []:
        text = str(value)
        if (
            condition_type.startswith("DATE_")
            and text.strip().upper() in RELATIVE_DATES
        ):
            payload.append({"relativeDate": text.strip().upper()})
        else:
            payload.append({"userEnteredValue": text})
    return payload


def _build_boolean_condition(
    condition_type: str, condition_values: Any, label: str = "condition_type"
) -> dict:
    """Validate a BooleanCondition type and build {type, values}."""
    normalized = (condition_type or "").strip().upper()
    if normalized not in BOOLEAN_CONDITION_TYPES:
        raise UserInputError(
            f"{label} '{condition_type}' is not a valid condition type. "
            f"Valid types: {', '.join(sorted(BOOLEAN_CONDITION_TYPES))}."
        )
    values = _parse_condition_values(condition_values)
    condition: dict = {"type": normalized}
    payload = _condition_values_payload(values, normalized)
    if payload:
        condition["values"] = payload
    return condition


def _parse_sort_specs(sort_specs: Any, grid_range: dict) -> List[dict]:
    """
    Parse [{"column": "B", "order": "DESCENDING"}, ...] into SortSpec objects.
    Columns are absolute (sheet) columns given as letters or zero-based indexes.
    """
    specs = _parse_json_list(sort_specs, "sort_specs")
    if not specs:
        raise UserInputError("sort_specs must contain at least one sort spec.")
    result: List[dict] = []
    for idx, spec in enumerate(specs):
        label = f"sort_specs[{idx}]"
        if isinstance(spec, str) and spec.strip():
            spec = {"column": spec}
        if not isinstance(spec, dict):
            raise UserInputError(
                f"{label} must be an object like {{'column': 'B', 'order': 'ASCENDING'}}."
            )
        column_index = _resolve_column_index(spec.get("column"), grid_range, label)
        order_raw = str(spec.get("order", "ASCENDING")).strip().upper()
        order = SORT_ORDERS.get(order_raw)
        if order is None:
            raise UserInputError(
                f"{label}: order must be ASCENDING or DESCENDING, got '{spec.get('order')}'."
            )
        result.append({"dimensionIndex": column_index, "sortOrder": order})
    return result


def _parse_filter_specs(filter_specs: Any, grid_range: dict) -> List[dict]:
    """
    Parse [{"column": "B", "hidden_values": [...], "condition_type": ...,
    "condition_values": [...]}, ...] into FilterSpec objects.
    """
    specs = _parse_json_list(filter_specs, "filter_specs")
    result: List[dict] = []
    for idx, spec in enumerate(specs):
        label = f"filter_specs[{idx}]"
        if not isinstance(spec, dict):
            raise UserInputError(
                f"{label} must be an object with 'column' and hidden_values and/or "
                "condition_type."
            )
        column_index = _resolve_column_index(spec.get("column"), grid_range, label)
        criteria: dict = {}
        hidden = spec.get("hidden_values")
        if hidden is not None:
            hidden_list = _parse_json_list(hidden, f"{label}.hidden_values")
            criteria["hiddenValues"] = [str(value) for value in hidden_list]
        if spec.get("condition_type"):
            criteria["condition"] = _build_boolean_condition(
                spec["condition_type"],
                spec.get("condition_values"),
                label=f"{label}.condition_type",
            )
        if not criteria:
            raise UserInputError(
                f"{label} needs hidden_values and/or condition_type to filter on."
            )
        result.append({"columnIndex": column_index, "filterCriteria": criteria})
    return result


def _build_filter_body(
    grid_range: dict,
    sort_specs: Any,
    filter_specs: Any,
    title: Optional[str] = None,
    filter_view_id: Optional[int] = None,
) -> tuple[dict, List[str]]:
    """Build a BasicFilter/FilterView body plus the list of fields it sets."""
    body: dict = {"range": grid_range}
    fields = ["range"]
    if filter_view_id is not None:
        body["filterViewId"] = filter_view_id
    if title is not None:
        body["title"] = title
        fields.append("title")
    if sort_specs is not None:
        body["sortSpecs"] = _parse_sort_specs(sort_specs, grid_range)
        fields.append("sortSpecs")
    if filter_specs is not None:
        body["filterSpecs"] = _parse_filter_specs(filter_specs, grid_range)
        fields.append("filterSpecs")
    return body, fields


def _describe_filter_criteria(filter_obj: dict) -> str:
    """Count the sort and filter specs on a BasicFilter/FilterView."""
    parts: List[str] = []
    sort_specs = filter_obj.get("sortSpecs") or []
    if sort_specs:
        parts.append(f"{len(sort_specs)} sort spec(s)")
    filter_specs = filter_obj.get("filterSpecs") or []
    criteria = filter_obj.get("criteria") or {}
    filtered = len(filter_specs) or len(criteria)
    if filtered:
        parts.append(f"{filtered} filtered column(s)")
    return ", ".join(parts) if parts else "no criteria"


def _format_filter_views_list(sheets: List[dict], sheet_titles: dict[int, str]) -> str:
    """List the basic filter and filter views of every sheet."""
    lines: List[str] = []
    for sheet in sheets:
        props = sheet.get("properties", {}) or {}
        title = props.get("title", "Untitled")
        basic = sheet.get("basicFilter")
        views = sheet.get("filterViews") or []
        if not basic and not views:
            continue
        lines.append(f"Sheet '{title}':")
        if basic:
            lines.append(
                f"  Basic filter on {_grid_range_to_a1(basic.get('range', {}), sheet_titles)}"
                f" ({_describe_filter_criteria(basic)})"
            )
        for view in views:
            lines.append(
                f"  Filter view '{view.get('title', 'Untitled')}' "
                f"(ID {view.get('filterViewId')}) on "
                f"{_grid_range_to_a1(view.get('range', {}), sheet_titles)} "
                f"({_describe_filter_criteria(view)})"
            )
    return "\n".join(lines) if lines else "No basic filters or filter views found."


# ---------------------------------------------------------------------------
# manage_sheet_protection
# ---------------------------------------------------------------------------


def _build_protected_range(
    grid_range: Optional[dict],
    description: Optional[str],
    warning_only: Optional[bool],
    editors: Optional[List[str]],
    editor_groups: Optional[List[str]],
    domain_users_can_edit: Optional[bool],
    unprotected_ranges: Optional[List[dict]],
    protected_range_id: Optional[int] = None,
) -> tuple[dict, List[str]]:
    """Build a ProtectedRange body plus the field mask of what was supplied."""
    body: dict = {}
    fields: List[str] = []
    if protected_range_id is not None:
        body["protectedRangeId"] = protected_range_id
    if grid_range is not None:
        body["range"] = grid_range
        fields.append("range")
    if description is not None:
        body["description"] = description
        fields.append("description")
    if warning_only is not None:
        body["warningOnly"] = bool(warning_only)
        fields.append("warningOnly")

    editors_body: dict = {}
    if editors is not None:
        editors_body["users"] = [
            str(e).strip() for e in _parse_json_list(editors, "editors")
        ]
    if editor_groups is not None:
        editors_body["groups"] = [
            str(g).strip() for g in _parse_json_list(editor_groups, "editor_groups")
        ]
    if domain_users_can_edit is not None:
        editors_body["domainUsersCanEdit"] = bool(domain_users_can_edit)
    if editors_body:
        if warning_only:
            raise UserInputError(
                "A warning-only protection cannot have editors: set warning_only=False "
                "or omit editors, editor_groups and domain_users_can_edit."
            )
        body["editors"] = editors_body
        fields.append("editors")
    if unprotected_ranges is not None:
        body["unprotectedRanges"] = unprotected_ranges
        fields.append("unprotectedRanges")
    return body, fields


def _format_protected_ranges_list(
    sheets: List[dict], sheet_titles: dict[int, str]
) -> str:
    """List every protected range with its editors and warning mode."""
    lines: List[str] = []
    for sheet in sheets:
        props = sheet.get("properties", {}) or {}
        protected = sheet.get("protectedRanges") or []
        if not protected:
            continue
        lines.append(f"Sheet '{props.get('title', 'Untitled')}':")
        for item in protected:
            if item.get("range"):
                scope = _grid_range_to_a1(item["range"], sheet_titles)
            elif item.get("namedRangeId"):
                scope = f"named range {item['namedRangeId']}"
            else:
                scope = "whole sheet"
            mode = "warning only" if item.get("warningOnly") else "restricted"
            editors = item.get("editors") or {}
            who: List[str] = []
            if editors.get("users"):
                who.append("users: " + ", ".join(editors["users"]))
            if editors.get("groups"):
                who.append("groups: " + ", ".join(editors["groups"]))
            if editors.get("domainUsersCanEdit"):
                who.append("domain users can edit")
            line = (
                f"  ID {item.get('protectedRangeId')}: {scope} [{mode}]"
                + (f" - {item['description']}" if item.get("description") else "")
                + (f" ({'; '.join(who)})" if who else "")
            )
            if item.get("unprotectedRanges"):
                unprotected = ", ".join(
                    _grid_range_to_a1(r, sheet_titles)
                    for r in item["unprotectedRanges"]
                )
                line += f" except {unprotected}"
            lines.append(line)
    return "\n".join(lines) if lines else "No protected ranges found."


# ---------------------------------------------------------------------------
# manage_sheet_validation
# ---------------------------------------------------------------------------

VALIDATION_RULE_TYPES = (
    "list",
    "range",
    "checkbox",
    "number",
    "date",
    "text",
    "custom_formula",
)
_CONDITION_ARITY = {
    "NUMBER_BETWEEN": 2,
    "NUMBER_NOT_BETWEEN": 2,
    "DATE_BETWEEN": 2,
    "DATE_NOT_BETWEEN": 2,
    "DATE_IS_VALID": 0,
    "TEXT_IS_EMAIL": 0,
    "TEXT_IS_URL": 0,
}


def _build_data_validation_rule(
    rule_type: Optional[str],
    values: Any,
    condition_type: Optional[str],
    strict: bool,
    show_dropdown: bool,
    input_message: Optional[str],
) -> dict:
    """Build a DataValidationRule for manage_sheet_validation action='set'."""
    kind = (rule_type or "").strip().lower()
    if kind not in VALIDATION_RULE_TYPES:
        raise UserInputError(
            f"rule_type must be one of {', '.join(VALIDATION_RULE_TYPES)}."
        )
    parsed_values = _parse_condition_values(values) or []

    if kind == "list":
        if not parsed_values:
            raise UserInputError("rule_type='list' needs at least one entry in values.")
        condition = {
            "type": "ONE_OF_LIST",
            "values": [{"userEnteredValue": str(v)} for v in parsed_values],
        }
    elif kind == "range":
        if len(parsed_values) != 1:
            raise UserInputError(
                "rule_type='range' needs exactly one value: the source range as a "
                "formula, e.g. '=Lists!A2:A20'."
            )
        source = str(parsed_values[0]).strip()
        if not source.startswith("="):
            source = "=" + source
        condition = {"type": "ONE_OF_RANGE", "values": [{"userEnteredValue": source}]}
    elif kind == "checkbox":
        if parsed_values and len(parsed_values) != 2:
            raise UserInputError(
                "rule_type='checkbox' takes no values, or exactly two: "
                "[checked_value, unchecked_value]."
            )
        condition = {"type": "BOOLEAN"}
        if parsed_values:
            condition["values"] = [{"userEnteredValue": str(v)} for v in parsed_values]
    elif kind == "custom_formula":
        if len(parsed_values) != 1 or not str(parsed_values[0]).startswith("="):
            raise UserInputError(
                "rule_type='custom_formula' needs exactly one value: a formula "
                "starting with '=', e.g. '=A1>0'."
            )
        condition = {
            "type": "CUSTOM_FORMULA",
            "values": [{"userEnteredValue": str(parsed_values[0])}],
        }
    else:
        prefix = kind.upper() + "_"
        normalized = (condition_type or "").strip().upper()
        if (
            not normalized.startswith(prefix)
            or normalized not in BOOLEAN_CONDITION_TYPES
        ):
            options = sorted(t for t in BOOLEAN_CONDITION_TYPES if t.startswith(prefix))
            raise UserInputError(
                f"rule_type='{kind}' needs condition_type to be one of: {', '.join(options)}."
            )
        expected = _CONDITION_ARITY.get(normalized, 1)
        if len(parsed_values) != expected:
            raise UserInputError(
                f"condition_type {normalized} needs exactly {expected} value(s) in values, "
                f"got {len(parsed_values)}."
            )
        condition = {"type": normalized}
        payload = _condition_values_payload(parsed_values, normalized)
        if payload:
            condition["values"] = payload

    rule: dict = {"condition": condition, "strict": bool(strict)}
    if kind in ("list", "range"):
        rule["showCustomUi"] = bool(show_dropdown)
    if input_message:
        rule["inputMessage"] = input_message
    return rule


# ---------------------------------------------------------------------------
# manage_sheet_chart
# ---------------------------------------------------------------------------

BASIC_CHART_TYPES = ("BAR", "COLUMN", "LINE", "AREA", "SCATTER")
CHART_TYPES = BASIC_CHART_TYPES + ("PIE",)
BASIC_LEGEND_POSITIONS = frozenset(
    {"BOTTOM_LEGEND", "LEFT_LEGEND", "RIGHT_LEGEND", "TOP_LEGEND", "NO_LEGEND"}
)
PIE_LEGEND_POSITIONS = BASIC_LEGEND_POSITIONS | {"LABELED_LEGEND"}
DEFAULT_CHART_WIDTH_PX = 600
DEFAULT_CHART_HEIGHT_PX = 371


def _normalize_chart_type(chart_type: Optional[str]) -> str:
    normalized = (chart_type or "").strip().upper()
    if normalized not in CHART_TYPES:
        raise UserInputError(f"chart_type must be one of {', '.join(CHART_TYPES)}.")
    return normalized


def _normalize_legend_position(
    legend_position: Optional[str], pie: bool
) -> Optional[str]:
    """Accept 'BOTTOM' or 'BOTTOM_LEGEND' (case-insensitive); None passes through."""
    if legend_position is None:
        return None
    normalized = legend_position.strip().upper()
    if not normalized.endswith("_LEGEND"):
        normalized = f"{normalized}_LEGEND"
    if normalized == "NONE_LEGEND":
        normalized = "NO_LEGEND"
    allowed = PIE_LEGEND_POSITIONS if pie else BASIC_LEGEND_POSITIONS
    if normalized not in allowed:
        raise UserInputError(f"legend_position must be one of {sorted(allowed)}.")
    return normalized


def _chart_data(grid_range: dict) -> dict:
    return {"sourceRange": {"sources": [grid_range]}}


def _build_chart_spec(
    chart_type: str,
    title: Optional[str],
    subtitle: Optional[str],
    domain_grid: dict,
    series_grids: List[dict],
    legend_position: Optional[str],
    header_count: Optional[int],
) -> dict:
    """Build a ChartSpec for a basic chart (BAR/COLUMN/LINE/AREA/SCATTER) or a PIE chart."""
    normalized = _normalize_chart_type(chart_type)
    if not series_grids:
        raise UserInputError("series_ranges must contain at least one range.")
    spec: dict = {}
    if title is not None:
        spec["title"] = title
    if subtitle is not None:
        spec["subtitle"] = subtitle

    if normalized == "PIE":
        if len(series_grids) != 1:
            raise UserInputError("A PIE chart takes exactly one series range.")
        pie: dict = {
            "domain": _chart_data(domain_grid),
            "series": _chart_data(series_grids[0]),
        }
        legend = _normalize_legend_position(legend_position, pie=True)
        if legend:
            pie["legendPosition"] = legend
        spec["pieChart"] = pie
        return spec

    basic: dict = {
        "chartType": normalized,
        "domains": [{"domain": _chart_data(domain_grid)}],
        "series": [
            {"series": _chart_data(grid), "targetAxis": "LEFT_AXIS"}
            for grid in series_grids
        ],
    }
    if normalized == "BAR":
        for entry in basic["series"]:
            entry["targetAxis"] = "BOTTOM_AXIS"
    legend = _normalize_legend_position(legend_position, pie=False)
    if legend:
        basic["legendPosition"] = legend
    if header_count is not None:
        if isinstance(header_count, bool) or header_count < 0:
            raise UserInputError("header_count must be a non-negative integer.")
        basic["headerCount"] = header_count
    spec["basicChart"] = basic
    return spec


def _build_overlay_position(
    anchor: dict, width: Optional[int], height: Optional[int]
) -> dict:
    """OverlayPosition from a GridCoordinate anchor plus optional pixel size."""
    for name, value in (("width", width), ("height", height)):
        if value is not None and (isinstance(value, bool) or value <= 0):
            raise UserInputError(f"{name} must be a positive number of pixels.")
    overlay: dict = {"anchorCell": anchor}
    if width is not None:
        overlay["widthPixels"] = width
    if height is not None:
        overlay["heightPixels"] = height
    return overlay


def _build_chart_position(
    anchor: Optional[dict], width: Optional[int], height: Optional[int]
) -> dict:
    """EmbeddedObjectPosition: anchored overlay when a cell is given, else a new sheet."""
    if anchor is None:
        return {"newSheet": True}
    return {
        "overlayPosition": _build_overlay_position(
            anchor,
            width if width is not None else DEFAULT_CHART_WIDTH_PX,
            height if height is not None else DEFAULT_CHART_HEIGHT_PX,
        )
    }


def _build_embedded_object_position_update(
    object_id: int, anchor: Optional[dict], width: Optional[int], height: Optional[int]
) -> dict:
    """updateEmbeddedObjectPosition touching only the supplied overlay fields."""
    fields: List[str] = []
    if anchor is not None:
        fields.append("anchorCell")
    if width is not None:
        fields.append("widthPixels")
    if height is not None:
        fields.append("heightPixels")
    if not fields:
        raise UserInputError(
            "action='move' needs anchor_cell and/or width/height to change."
        )
    overlay = _build_overlay_position(anchor or {}, width, height)
    if anchor is None:
        overlay.pop("anchorCell", None)
    return {
        "updateEmbeddedObjectPosition": {
            "objectId": object_id,
            "newPosition": {"overlayPosition": overlay},
            "fields": ",".join(fields),
        }
    }


def _chart_type_of(spec: dict) -> str:
    """Human label for a ChartSpec's chart kind."""
    if "basicChart" in spec:
        return (spec["basicChart"] or {}).get("chartType", "BASIC")
    for key in spec:
        if key.endswith("Chart"):
            return key[: -len("Chart")].upper()
    return "UNKNOWN"


def _find_chart(sheets: List[dict], chart_id: int) -> dict:
    """Locate an EmbeddedChart by chartId across all sheets."""
    for sheet in sheets:
        for chart in sheet.get("charts") or []:
            if chart.get("chartId") == chart_id:
                return chart
    raise UserInputError(f"No chart with chart_id {chart_id} in this spreadsheet.")


def _describe_chart_position(position: dict, sheet_titles: dict[int, str]) -> str:
    overlay = position.get("overlayPosition")
    if overlay:
        anchor = overlay.get("anchorCell", {}) or {}
        title = sheet_titles.get(
            anchor.get("sheetId"), f"Sheet {anchor.get('sheetId')}"
        )
        cell = _format_a1_cell(
            title, anchor.get("rowIndex", 0), anchor.get("columnIndex", 0)
        )
        size = ""
        if overlay.get("widthPixels") or overlay.get("heightPixels"):
            size = f", {overlay.get('widthPixels', '?')}x{overlay.get('heightPixels', '?')}px"
        return f"anchored at {cell}{size}"
    if position.get("sheetId") is not None:
        title = sheet_titles.get(position["sheetId"], f"Sheet {position['sheetId']}")
        return f"own sheet '{title}'"
    return "position unknown"


def _format_charts_list(sheets: List[dict], sheet_titles: dict[int, str]) -> str:
    """List every embedded chart with id, title, type and placement."""
    lines: List[str] = []
    for sheet in sheets:
        charts = sheet.get("charts") or []
        if not charts:
            continue
        props = sheet.get("properties", {}) or {}
        lines.append(f"Sheet '{props.get('title', 'Untitled')}':")
        for chart in charts:
            spec = chart.get("spec", {}) or {}
            lines.append(
                f"  Chart {chart.get('chartId')}: '{spec.get('title', '')}' "
                f"[{_chart_type_of(spec)}] "
                f"{_describe_chart_position(chart.get('position', {}) or {}, sheet_titles)}"
            )
    return "\n".join(lines) if lines else "No charts found."


def _apply_chart_spec_updates(
    spec: dict,
    title: Optional[str],
    subtitle: Optional[str],
    chart_type: Optional[str],
    legend_position: Optional[str],
) -> dict:
    """Copy a ChartSpec and apply the supplied title/subtitle/type/legend changes."""
    updated = copy.deepcopy(spec)
    changed = False
    if title is not None:
        updated["title"] = title
        changed = True
    if subtitle is not None:
        updated["subtitle"] = subtitle
        changed = True
    is_pie = "pieChart" in updated
    if chart_type is not None:
        normalized = _normalize_chart_type(chart_type)
        if is_pie or normalized == "PIE" or "basicChart" not in updated:
            raise UserInputError(
                "chart_type can only switch between BAR, COLUMN, LINE, AREA and SCATTER "
                "on an existing basic chart. Delete and re-add the chart to change to "
                "or from PIE."
            )
        updated["basicChart"]["chartType"] = normalized
        changed = True
    if legend_position is not None:
        legend = _normalize_legend_position(legend_position, pie=is_pie)
        target_key = "pieChart" if is_pie else "basicChart"
        if target_key not in updated:
            raise UserInputError(
                "legend_position is only supported on basic and pie charts."
            )
        updated[target_key]["legendPosition"] = legend
        changed = True
    if not changed:
        raise UserInputError(
            "action='update' needs at least one of title, subtitle, chart_type or "
            "legend_position."
        )
    return updated


# ---------------------------------------------------------------------------
# find_replace_sheet
# ---------------------------------------------------------------------------


def _build_find_replace_request(
    find: str,
    replacement: str,
    scope: dict,
    match_case: bool,
    match_entire_cell: bool,
    search_by_regex: bool,
    include_formulas: bool,
) -> dict:
    """Build a findReplace request; `scope` is {range}, {sheetId} or {allSheets}."""
    if find is None or find == "":
        raise UserInputError("find must be a non-empty string.")
    if search_by_regex:
        try:
            re.compile(find)
        except re.error as exc:
            raise UserInputError(
                f"find is not a valid regular expression: {exc}"
            ) from exc
    body: dict = {
        "find": find,
        "replacement": replacement if replacement is not None else "",
        "matchCase": bool(match_case),
        "matchEntireCell": bool(match_entire_cell),
        "searchByRegex": bool(search_by_regex),
        "includeFormulas": bool(include_formulas),
    }
    body.update(scope)
    return {"findReplace": body}


def _format_find_replace_reply(reply: dict) -> str:
    """Summarize a FindReplaceResponse."""
    reply = reply or {}
    return (
        f"{reply.get('occurrencesChanged', 0)} occurrence(s) changed across "
        f"{reply.get('valuesChanged', 0)} value cell(s) and "
        f"{reply.get('formulasChanged', 0)} formula cell(s) in "
        f"{reply.get('rowsChanged', 0)} row(s) on {reply.get('sheetsChanged', 0)} sheet(s)"
    )


# ---------------------------------------------------------------------------
# read_sheet_values: multi-range (batchGet) formatting
# ---------------------------------------------------------------------------


def _format_rows(values: List[List[Any]]) -> List[str]:
    """Render rows as 'Row  n: [...]' lines, padding short rows to the first row's width."""
    lines: List[str] = []
    width = len(values[0]) if values else 0
    for i, row in enumerate(values, 1):
        padded = row + [""] * max(0, width - len(row))
        lines.append(f"Row {i:2d}: {padded}")
    return lines


def _format_value_range_block(
    index: int, requested_range: str, value_range: dict, clamp_note: Optional[str]
) -> str:
    """Format one ValueRange from values.batchGet as a titled block."""
    values = value_range.get("values", []) or []
    resolved = value_range.get("range", requested_range)
    header = f"Range {index} '{requested_range}'"
    if resolved != requested_range:
        header += f" (resolved to '{resolved}')"
    if not values:
        block = f"{header}: no data found."
    else:
        block = f"{header}: {len(values)} rows\n" + "\n".join(_format_rows(values))
    if clamp_note:
        block += clamp_note
    return block
