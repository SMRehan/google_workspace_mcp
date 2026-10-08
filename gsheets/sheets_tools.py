"""
Google Sheets MCP Tools

This module provides MCP tools for interacting with Google Sheets API.
"""

import logging
import asyncio
import json
import copy
from typing import Any, Dict, List, Optional, Union

from fastmcp.exceptions import ToolError
from googleapiclient.errors import HttpError
from mcp.types import ToolAnnotations

from auth.service_decorator import require_google_service
from core.server import server
from core.utils import handle_http_errors, UserInputError, StringList
from core.comments import create_comment_tools
from gdrive.drive_helpers import flag_incomplete_search, move_new_file_to_folder
from gsheets.sheets_helpers import (
    CONDITION_TYPES,
    MAX_READ_SHEET_ROWS,
    _a1_range_for_values,
    _clamp_a1_read_rows,
    _column_to_index,
    _build_boolean_rule,
    _build_gradient_rule,
    _fetch_cell_formulas,
    _fetch_detailed_sheet_errors,
    _fetch_grid_metadata,
    _fetch_sheets_with_rules,
    _find_named_range,
    _format_conditional_rules_section,
    _format_named_ranges_list,
    _format_sheet_error_section,
    _insert_cell_comment,
    _normalize_chips_input,
    _parse_a1_range,
    _parse_condition_values,
    _parse_gradient_points,
    _parse_hex_color,
    _select_sheet,
    _values_contain_sheets_errors,
    _apply_chart_spec_updates,
    _build_chart_position,
    _build_chart_spec,
    _build_data_validation_rule,
    _build_embedded_object_position_update,
    _build_filter_body,
    _build_find_replace_request,
    _build_merge_cells_request,
    _build_protected_range,
    _build_update_borders_request,
    _fetch_sheets,
    _find_chart,
    _format_charts_list,
    _format_filter_views_list,
    _format_find_replace_reply,
    _format_protected_ranges_list,
    _format_value_range_block,
    _grid_coordinate_from_a1,
    _index_to_column,
    _parse_json_list,
    _parse_sort_specs,
    _resolve_scope_grid_range,
    _sheet_titles_by_id,
    _summarize_batch_update_replies,
    _validate_batch_update_requests,
)

# Configure module logger
logger = logging.getLogger(__name__)


@server.tool(
    title="List Spreadsheets",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@handle_http_errors("list_spreadsheets", is_read_only=True, service_type="sheets")
@require_google_service("drive", "drive_read")
async def list_spreadsheets(
    service,
    user_google_email: str,
    max_results: int = 25,
    page_token: Optional[str] = None,
    corpora: Optional[str] = None,
    drive_id: Optional[str] = None,
) -> str:
    """
    Lists spreadsheets from Google Drive that the user has access to.

    Args:
        user_google_email (str): The user's Google email address. Required.
        max_results (int): Maximum number of spreadsheets to return. Defaults to 25.
        page_token (Optional[str]): Page token from a previous response's nextPageToken to retrieve the next page of results.
        corpora (Optional[str]): Corpus to search ('user', 'domain', 'drive', 'allDrives').
            Defaults to 'drive' when drive_id is set, otherwise 'allDrives'.
        drive_id (Optional[str]): Shared drive ID to search.

    Returns:
        str: A formatted list of spreadsheet files (name, ID, modified time).
             Includes a nextPageToken line when more results are available.
    """
    logger.info(f"[list_spreadsheets] Invoked. Email: '{user_google_email}'")

    files_response = await asyncio.to_thread(
        service.files()
        .list(
            q="mimeType='application/vnd.google-apps.spreadsheet'",
            pageSize=max_results,
            pageToken=page_token,
            fields="nextPageToken, incompleteSearch, files(id,name,modifiedTime,webViewLink)",
            orderBy="modifiedTime desc",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            corpora=corpora or ("drive" if drive_id else "allDrives"),
            driveId=drive_id,
        )
        .execute
    )

    files = files_response.get("files", [])
    next_token = files_response.get("nextPageToken")
    if not files and not next_token:
        return flag_incomplete_search(
            f"No spreadsheets found for {user_google_email}.", files_response
        )

    spreadsheets_list = [
        f'- "{file["name"]}" (ID: {file["id"]}) | Modified: {file.get("modifiedTime", "Unknown")} | Link: {file.get("webViewLink", "No link")}'
        for file in files
    ]

    text_output = (
        f"Successfully listed {len(files)} spreadsheets for {user_google_email}:\n"
        + "\n".join(spreadsheets_list)
    )
    if next_token:
        text_output += f"\nnextPageToken: {next_token}"

    logger.info(
        f"Successfully listed {len(files)} spreadsheets for {user_google_email}."
    )
    return flag_incomplete_search(text_output, files_response)


@server.tool(
    title="Get Spreadsheet Info",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@handle_http_errors("get_spreadsheet_info", is_read_only=True, service_type="sheets")
@require_google_service("sheets", "sheets_read")
async def get_spreadsheet_info(
    service,
    user_google_email: str,
    spreadsheet_id: str,
) -> str:
    """
    Gets information about a specific spreadsheet including its sheets.

    Args:
        user_google_email (str): The user's Google email address. Required.
        spreadsheet_id (str): The ID of the spreadsheet to get info for. Required.

    Returns:
        str: Formatted spreadsheet information including title, locale, and sheets list.
    """
    logger.info(
        f"[get_spreadsheet_info] Invoked. Email: '{user_google_email}', Spreadsheet ID: {spreadsheet_id}"
    )

    spreadsheet = await asyncio.to_thread(
        service.spreadsheets()
        .get(
            spreadsheetId=spreadsheet_id,
            fields="spreadsheetId,properties(title,locale),sheets(properties(title,sheetId,gridProperties(rowCount,columnCount)),conditionalFormats)",
        )
        .execute
    )

    properties = spreadsheet.get("properties", {})
    title = properties.get("title", "Unknown")
    locale = properties.get("locale", "Unknown")
    sheets = spreadsheet.get("sheets", [])

    sheet_titles = {}
    for sheet in sheets:
        sheet_props = sheet.get("properties", {})
        sid = sheet_props.get("sheetId")
        if sid is not None:
            sheet_titles[sid] = sheet_props.get("title", f"Sheet {sid}")

    sheets_info = []
    for sheet in sheets:
        sheet_props = sheet.get("properties", {})
        sheet_name = sheet_props.get("title", "Unknown")
        sheet_id = sheet_props.get("sheetId", "Unknown")
        grid_props = sheet_props.get("gridProperties", {})
        rows = grid_props.get("rowCount", "Unknown")
        cols = grid_props.get("columnCount", "Unknown")
        rules = sheet.get("conditionalFormats", []) or []

        sheets_info.append(
            f'  - "{sheet_name}" (ID: {sheet_id}) | Size: {rows}x{cols} | Conditional formats: {len(rules)}'
        )
        if rules:
            sheets_info.append(
                _format_conditional_rules_section(
                    sheet_name, rules, sheet_titles, indent="    "
                )
            )

    sheets_section = "\n".join(sheets_info) if sheets_info else "  No sheets found"
    text_output = "\n".join(
        [
            f'Spreadsheet: "{title}" (ID: {spreadsheet_id}) | Locale: {locale}',
            f"Sheets ({len(sheets)}):",
            sheets_section,
        ]
    )

    logger.info(
        f"Successfully retrieved info for spreadsheet {spreadsheet_id} for {user_google_email}."
    )
    return text_output


@server.tool(
    title="Read Sheet Values",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@handle_http_errors("read_sheet_values", is_read_only=True, service_type="sheets")
@require_google_service("sheets", "sheets_read")
async def read_sheet_values(
    service,
    user_google_email: str,
    spreadsheet_id: str,
    range_name: str = "A1:Z1000",
    include_hyperlinks: bool = False,
    include_notes: bool = False,
    include_formulas: bool = False,
    include_smart_chips: bool = False,
    ranges: Optional[List[str]] = None,
) -> str:
    """
    Reads values from a specific range in a Google Sheet, or from several
    ranges at once.

    Args:
        user_google_email (str): The user's Google email address. Required.
        spreadsheet_id (str): The ID of the spreadsheet. Required.
        range_name (str): The range to read (e.g., "Sheet1!A1:D10", "A1:D10").
            Defaults to "A1:Z1000". Open-ended or oversized ranges are clamped to
            at most 1000 rows before the Sheets API request to bound memory use.
            Ignored when `ranges` is given.
        ranges (Optional[List[str]]): Several A1 ranges to read in one
            values.batchGet call (e.g. ["Sheet1!A1:B10", "Summary!A1:D5"]).
            Each range is clamped to 1000 rows and formatted as its own block.
            The include_* options are not supported together with `ranges`.
        include_hyperlinks (bool): If True, also fetch hyperlink metadata for the range.
            Defaults to False to avoid expensive includeGridData requests.
        include_notes (bool): If True, also fetch cell notes for the range.
            Defaults to False to avoid expensive includeGridData requests.
        include_formulas (bool): If True, also fetch raw formula strings for cells that
            contain formulas. Useful for identifying cross-sheet references before writing
            back to a range. Defaults to False to avoid an extra API request.
        include_smart_chips (bool): If True, also fetch smart chips metadata (Drive files/folders
            and People chips) for the range. Defaults to False to avoid expensive includeGridData requests.

    Returns:
        str: The formatted values from the specified range.
    """
    logger.info(
        f"[read_sheet_values] Invoked. Email: '{user_google_email}', Spreadsheet: {spreadsheet_id}, Range: {range_name}"
    )

    if ranges is not None:
        return await _read_sheet_value_ranges(
            service,
            user_google_email,
            spreadsheet_id,
            ranges,
            metadata_requested=any(
                [
                    include_hyperlinks,
                    include_notes,
                    include_formulas,
                    include_smart_chips,
                ]
            ),
        )

    fetch_range, clamp_note = _clamp_a1_read_rows(range_name, MAX_READ_SHEET_ROWS)
    if clamp_note:
        logger.info(
            "[read_sheet_values] Clamped range '%s' -> '%s' (max %s rows)",
            range_name,
            fetch_range,
            MAX_READ_SHEET_ROWS,
        )

    result = await asyncio.to_thread(
        service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=fetch_range)
        .execute
    )

    values = result.get("values", [])
    resolved_range = result.get("range", range_name)

    hyperlink_section, notes_section, smart_chips_section = await _fetch_grid_metadata(
        service,
        spreadsheet_id,
        resolved_range,
        values,
        include_hyperlinks=include_hyperlinks,
        include_notes=include_notes,
        include_smart_chips=include_smart_chips,
    )

    formula_section = ""
    formula_values = []
    if include_formulas:
        formula_section, formula_values = await _fetch_cell_formulas(
            service, spreadsheet_id, resolved_range
        )

    if not values and not formula_values:
        return f"No data found in range '{range_name}' for {user_google_email}." + (
            clamp_note or ""
        )

    if not values:
        logger.info(
            "[read_sheet_values] Range '%s' has formula cells but no displayed values",
            resolved_range,
        )
        return (
            f"No displayed values found in range '{range_name}' in spreadsheet {spreadsheet_id} "
            f"for {user_google_email}. The range contains formula cells."
            + formula_section
            + (clamp_note or "")
        )

    detailed_range = _a1_range_for_values(resolved_range, values) or resolved_range

    detailed_errors_section = ""
    if _values_contain_sheets_errors(values):
        try:
            errors = await _fetch_detailed_sheet_errors(
                service, spreadsheet_id, detailed_range
            )
            detailed_errors_section = _format_sheet_error_section(
                errors=errors, range_label=detailed_range
            )
        except Exception as exc:
            logger.warning(
                "[read_sheet_values] Failed fetching detailed error messages for range '%s': %s",
                detailed_range,
                exc,
            )

    # Format the output as a readable table
    formatted_rows = []
    for i, row in enumerate(values, 1):
        # Pad row with empty strings to show structure
        padded_row = row + [""] * max(0, len(values[0]) - len(row)) if values else row
        formatted_rows.append(f"Row {i:2d}: {padded_row}")

    text_output = (
        f"Successfully read {len(values)} rows from range '{range_name}' in spreadsheet {spreadsheet_id} for {user_google_email}:\n"
        + "\n".join(formatted_rows)
    )
    if clamp_note:
        text_output += clamp_note

    logger.info(f"Successfully read {len(values)} rows for {user_google_email}.")
    return (
        text_output
        + hyperlink_section
        + notes_section
        + formula_section
        + detailed_errors_section
        + smart_chips_section
    )


async def _read_sheet_value_ranges(
    service,
    user_google_email: str,
    spreadsheet_id: str,
    ranges: List[str],
    metadata_requested: bool,
) -> str:
    """read_sheet_values multi-range path: one values.batchGet, one block per range."""
    range_list = [str(r) for r in _parse_json_list(ranges, "ranges") if str(r).strip()]
    if not range_list:
        raise UserInputError("ranges must contain at least one A1 range.")
    if metadata_requested:
        raise UserInputError(
            "include_hyperlinks, include_notes, include_formulas and include_smart_chips "
            "are not supported with `ranges`; read a single range_name for those."
        )

    fetch_ranges = []
    clamp_notes = []
    for requested in range_list:
        fetch_range, clamp_note = _clamp_a1_read_rows(requested, MAX_READ_SHEET_ROWS)
        fetch_ranges.append(fetch_range)
        clamp_notes.append(clamp_note)

    result = await asyncio.to_thread(
        service.spreadsheets()
        .values()
        .batchGet(spreadsheetId=spreadsheet_id, ranges=fetch_ranges)
        .execute
    )
    value_ranges = result.get("valueRanges", []) or []

    blocks = []
    total_rows = 0
    for idx, requested in enumerate(range_list, 1):
        value_range = value_ranges[idx - 1] if idx - 1 < len(value_ranges) else {}
        total_rows += len(value_range.get("values", []) or [])
        blocks.append(
            _format_value_range_block(idx, requested, value_range, clamp_notes[idx - 1])
        )

    logger.info(
        f"Successfully read {total_rows} rows across {len(range_list)} ranges for {user_google_email}."
    )
    return (
        f"Successfully read {len(range_list)} ranges ({total_rows} rows total) from "
        f"spreadsheet {spreadsheet_id} for {user_google_email}:\n\n"
        + "\n\n".join(blocks)
    )


@server.tool(
    title="Modify Sheet Values",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
@handle_http_errors("modify_sheet_values", service_type="sheets")
@require_google_service("sheets", "sheets_write")
async def modify_sheet_values(
    service,
    user_google_email: str,
    spreadsheet_id: str,
    range_name: str,
    values: Optional[Union[str, List[List[str]]]] = None,
    value_input_option: str = "USER_ENTERED",
    clear_values: bool = False,
    chips: Optional[Union[str, dict, List[Any]]] = None,
    chip_type: Optional[str] = None,
    append: bool = False,
) -> str:
    """
    Modifies values in a specific range of a Google Sheet - can write, update, append, or clear
    values, or insert Smart Chips (Drive files/folders or People).

    Args:
        user_google_email (str): The user's Google email address. Required.
        spreadsheet_id (str): The ID of the spreadsheet. Required.
        range_name (str): The range to modify (e.g., "Sheet1!A1:D10", "A1:D10"). Required.
            With append=True this is the table to append to; the API finds the
            first empty row after it (e.g. "Sheet1!A:D" or "Sheet1").
        values (Optional[Union[str, List[List[str]]]]): 2D array of values to write/update. Can be a JSON string or Python list. Required unless clear_values=True or chips is provided.
        value_input_option (str): How to interpret input values ("RAW" or "USER_ENTERED"). Defaults to "USER_ENTERED".
        clear_values (bool): If True, clears the range instead of writing values. Defaults to False.
        append (bool): If True, appends `values` as new rows after the last row of
            the table at range_name (values.append) instead of overwriting the
            range. Cannot be combined with clear_values or chips. Defaults to False.
        chips (Optional[Union[str, dict, List[Any]]]): Smart chip(s) to insert instead of values:
            - A single URL or email string for a single cell (e.g., "https://drive.google.com/drive/folders/123", "user@example.com").
            - A list of URLs or emails for a single cell (multiple chips) or across cells (e.g., ["https://...", "https://..."]).
            - A 2D list of URLs/emails matching a grid range. For a single-row or single-column
              range, each inner list is instead the chips for one cell (e.g., [["a@x.com", "b@x.com"]]
              puts both chips in the first cell of "A1:C1").
            - A dict or list of dicts with explicit properties (e.g., {"type": "drive", "uri": "..."}, {"type": "person", "email": "..."}).
            - A JSON-encoded string representing any of the above formats.
        chip_type (Optional[str]): Explicit chip type if passing raw strings in chips: "drive" (default for URLs/IDs) or "person" (default for emails).

    Returns:
        str: Confirmation message of the successful modification operation.
    """
    if append and clear_values:
        raise UserInputError("append=True cannot be combined with clear_values=True.")
    if chips is not None:
        if values is not None or clear_values:
            raise UserInputError(
                "Provide only one of 'values', 'chips', or 'clear_values'."
            )
        if append:
            raise UserInputError("append=True cannot be combined with chips.")
        return await _write_smart_chips(
            service=service,
            user_google_email=user_google_email,
            spreadsheet_id=spreadsheet_id,
            range_name=range_name,
            chips=chips,
            chip_type=chip_type,
        )

    operation = "clear" if clear_values else ("append" if append else "write")
    logger.info(
        f"[modify_sheet_values] Invoked. Operation: {operation}, Email: '{user_google_email}', Spreadsheet: {spreadsheet_id}, Range: {range_name}"
    )

    # Parse values if it's a JSON string (MCP passes parameters as JSON strings)
    if values is not None and isinstance(values, str):
        try:
            parsed_values = json.loads(values)
            if not isinstance(parsed_values, list):
                raise ValueError(
                    f"Values must be a list, got {type(parsed_values).__name__}"
                )
            # Validate it's a list of lists
            for i, row in enumerate(parsed_values):
                if not isinstance(row, list):
                    raise ValueError(
                        f"Row {i} must be a list, got {type(row).__name__}"
                    )
            values = parsed_values
            logger.info(
                f"[modify_sheet_values] Parsed JSON string to Python list with {len(values)} rows"
            )
        except json.JSONDecodeError as e:
            raise UserInputError(f"Invalid JSON format for values: {e}")
        except ValueError as e:
            raise UserInputError(f"Invalid values structure: {e}")

    if not clear_values and not values:
        raise UserInputError(
            "Either 'values' or 'chips' must be provided, or 'clear_values' must be True."
        )

    if clear_values:
        result = await asyncio.to_thread(
            service.spreadsheets()
            .values()
            .clear(spreadsheetId=spreadsheet_id, range=range_name)
            .execute
        )

        cleared_range = result.get("clearedRange", range_name)
        text_output = f"Successfully cleared range '{cleared_range}' in spreadsheet {spreadsheet_id} for {user_google_email}."
        logger.info(
            f"Successfully cleared range '{cleared_range}' for {user_google_email}."
        )
    else:
        body = {"values": values}
        values_api = service.spreadsheets().values()
        write_kwargs = dict(
            spreadsheetId=spreadsheet_id,
            range=range_name,
            valueInputOption=value_input_option,
            # NOTE: This increases response payload/shape by including `updatedData`, but lets
            # us detect Sheets error tokens (e.g. "#VALUE!", "#REF!") without an extra read.
            includeValuesInResponse=True,
            responseValueRenderOption="FORMATTED_VALUE",
            body=body,
        )

        if append:
            append_result = await asyncio.to_thread(
                values_api.append(**write_kwargs).execute
            )
            # values.append nests the write summary under "updates".
            result = append_result.get("updates", {}) or {}
        else:
            result = await asyncio.to_thread(values_api.update(**write_kwargs).execute)

        updated_cells = result.get("updatedCells", 0)
        updated_rows = result.get("updatedRows", 0)
        updated_columns = result.get("updatedColumns", 0)

        detailed_errors_section = ""
        updated_data = result.get("updatedData") or {}
        updated_values = updated_data.get("values", []) or []
        if updated_values and _values_contain_sheets_errors(updated_values):
            updated_range = result.get("updatedRange", range_name)
            detailed_range = (
                _a1_range_for_values(updated_range, updated_values) or updated_range
            )
            try:
                errors = await _fetch_detailed_sheet_errors(
                    service, spreadsheet_id, detailed_range
                )
                detailed_errors_section = _format_sheet_error_section(
                    errors=errors, range_label=detailed_range
                )
            except Exception as exc:
                logger.warning(
                    "[modify_sheet_values] Failed fetching detailed error messages for range '%s': %s",
                    detailed_range,
                    exc,
                )

        if append:
            appended_range = result.get("updatedRange", range_name)
            text_output = (
                f"Successfully appended {updated_rows} rows after table '{range_name}' in spreadsheet "
                f"{spreadsheet_id} for {user_google_email}. Written to '{appended_range}': "
                f"{updated_cells} cells, {updated_rows} rows, {updated_columns} columns."
            )
        else:
            text_output = (
                f"Successfully updated range '{range_name}' in spreadsheet {spreadsheet_id} for {user_google_email}. "
                f"Updated: {updated_cells} cells, {updated_rows} rows, {updated_columns} columns."
            )
        text_output += detailed_errors_section
        logger.info(
            f"Successfully updated {updated_cells} cells for {user_google_email}."
        )

    return text_output


# Not in the API docs: the PR author's manual testing hit a 10-chip cap per batchUpdate.
MAX_DRIVE_CHIPS_PER_BATCH = 8


async def _write_smart_chips(
    service,
    user_google_email: str,
    spreadsheet_id: str,
    range_name: str,
    chips: Union[str, dict, List[Any]],
    chip_type: Optional[str] = None,
) -> str:
    """Writes smart chips into a range for modify_sheet_values.

    Args:
        service: Google Sheets API service client.
        user_google_email: The user's Google email address.
        spreadsheet_id: The ID of the spreadsheet.
        range_name: Target range or cell (e.g., "Sheet1!F3", "F3:F23").
        chips: Smart chip(s) specification (string, list, 2D list, dict).
        chip_type: Optional explicit chip type ("drive" or "person").

    Returns:
        Confirmation message of the operation.
    """
    logger.info(
        f"[modify_sheet_values] Writing smart chips. Email: '{user_google_email}', Spreadsheet: {spreadsheet_id}, Range: {range_name}"
    )

    metadata = await asyncio.to_thread(
        service.spreadsheets()
        .get(
            spreadsheetId=spreadsheet_id,
            fields="sheets(properties(sheetId,title))",
        )
        .execute
    )
    grid_range = _parse_a1_range(range_name, metadata.get("sheets", []))
    sheet_id = grid_range["sheetId"]
    end_row = grid_range.get("endRowIndex")
    end_col = grid_range.get("endColumnIndex")

    cell_updates = _normalize_chips_input(
        chips=chips,
        start_row=grid_range.get("startRowIndex"),
        end_row=end_row - 1 if end_row is not None else None,
        start_col=grid_range.get("startColumnIndex"),
        end_col=end_col - 1 if end_col is not None else None,
        default_chip_type=chip_type,
    )

    if not cell_updates:
        return f"No smart chips to insert for range '{range_name}' in spreadsheet {spreadsheet_id}."

    total_inserted = sum(len(c[2].get("chipRuns", [])) for c in cell_updates)

    batches = []
    current_batch = []
    current_chip_count = 0
    for update in cell_updates:
        cell_chips = len(update[2].get("chipRuns", []))
        if cell_chips > MAX_DRIVE_CHIPS_PER_BATCH:
            raise UserInputError(
                f"Number of chips in a single cell ({cell_chips}) exceeds the "
                f"per-batch limit ({MAX_DRIVE_CHIPS_PER_BATCH})."
            )
        if current_batch and (
            current_chip_count + cell_chips > MAX_DRIVE_CHIPS_PER_BATCH
        ):
            batches.append(current_batch)
            current_batch = [update]
            current_chip_count = cell_chips
        else:
            current_batch.append(update)
            current_chip_count += cell_chips
    if current_batch:
        batches.append(current_batch)

    written_cells = 0
    for batch in batches:
        requests = []
        for r_idx, c_idx, cell_data in batch:
            requests.append(
                {
                    "updateCells": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": r_idx,
                            "endRowIndex": r_idx + 1,
                            "startColumnIndex": c_idx,
                            "endColumnIndex": c_idx + 1,
                        },
                        "rows": [{"values": [cell_data]}],
                        "fields": "userEnteredValue,chipRuns",
                    }
                }
            )
        try:
            await asyncio.to_thread(
                service.spreadsheets()
                .batchUpdate(spreadsheetId=spreadsheet_id, body={"requests": requests})
                .execute
            )
        except HttpError as error:
            if not written_cells:
                raise
            # Earlier batches are already committed, so say which cells changed.
            raise ToolError(
                f"Wrote smart chips to {written_cells} of {len(cell_updates)} cells in "
                f"range '{range_name}' before the Sheets API rejected a batch: {error}"
            ) from error
        written_cells += len(batch)

    logger.info(
        f"[modify_sheet_values] Successfully inserted {total_inserted} smart chips for {user_google_email}."
    )
    return (
        f"Successfully inserted {total_inserted} smart chip(s) into range '{range_name}' "
        f"in spreadsheet {spreadsheet_id} for {user_google_email}."
    )


# Internal implementation function for testing
async def _format_sheet_range_impl(
    service,
    spreadsheet_id: str,
    range_name: str,
    background_color: Optional[str] = None,
    text_color: Optional[str] = None,
    number_format_type: Optional[str] = None,
    number_format_pattern: Optional[str] = None,
    wrap_strategy: Optional[str] = None,
    horizontal_alignment: Optional[str] = None,
    vertical_alignment: Optional[str] = None,
    bold: Optional[bool] = None,
    italic: Optional[bool] = None,
    font_size: Optional[int] = None,
    border_style: Optional[str] = None,
    border_color: Optional[str] = None,
    border_sides: Optional[List[str]] = None,
    merge: Optional[str] = None,
    unmerge: bool = False,
) -> dict:
    """Internal implementation for format_sheet_range.

    Applies formatting to a Google Sheets range including colors, number formats,
    text wrapping, alignment, text styling, borders and cell merging.

    Args:
        service: Google Sheets API service client.
        spreadsheet_id: The ID of the spreadsheet.
        range_name: A1-style range (optionally with sheet name).
        background_color: Hex background color (e.g., "#FFEECC").
        text_color: Hex text color (e.g., "#000000").
        number_format_type: Sheets number format type (e.g., "DATE").
        number_format_pattern: Optional custom pattern for the number format.
        wrap_strategy: Text wrap strategy (WRAP, CLIP, OVERFLOW_CELL).
        horizontal_alignment: Horizontal alignment (LEFT, CENTER, RIGHT).
        vertical_alignment: Vertical alignment (TOP, MIDDLE, BOTTOM).
        bold: Whether to apply bold formatting.
        italic: Whether to apply italic formatting.
        font_size: Font size in points.
        border_style: Border style (SOLID, SOLID_MEDIUM, SOLID_THICK, DASHED,
            DOTTED, DOUBLE, NONE) applied to border_sides.
        border_color: Hex border color; defaults to the Sheets default (black).
        border_sides: Sides to apply the border to; defaults to all.
        merge: Merge the range: "all", "columns" or "rows".
        unmerge: Unmerge any merged cells in the range.

    Returns:
        Dictionary with keys: range_name, spreadsheet_id, summary.
    """
    if (border_color or border_sides) and not border_style:
        raise UserInputError(
            "border_style is required when border_color or border_sides is given."
        )
    # Validate at least one formatting option is provided
    has_any_format = any(
        [
            background_color,
            text_color,
            number_format_type,
            wrap_strategy,
            horizontal_alignment,
            vertical_alignment,
            bold is not None,
            italic is not None,
            font_size is not None,
            border_style,
            merge,
            unmerge,
        ]
    )
    if not has_any_format:
        raise UserInputError(
            "Provide at least one formatting option (background_color, text_color, "
            "number_format_type, wrap_strategy, horizontal_alignment, vertical_alignment, "
            "bold, italic, font_size, border_style, merge, or unmerge)."
        )
    if merge and unmerge:
        raise UserInputError("Provide either merge or unmerge, not both.")

    # Parse colors
    bg_color_parsed = _parse_hex_color(background_color)
    text_color_parsed = _parse_hex_color(text_color)

    # Validate and normalize number format
    number_format = None
    if number_format_type:
        allowed_number_formats = {
            "NUMBER",
            "NUMBER_WITH_GROUPING",
            "CURRENCY",
            "PERCENT",
            "SCIENTIFIC",
            "DATE",
            "TIME",
            "DATE_TIME",
            "TEXT",
        }
        normalized_type = number_format_type.upper()
        if normalized_type not in allowed_number_formats:
            raise UserInputError(
                f"number_format_type must be one of {sorted(allowed_number_formats)}."
            )
        number_format = {"type": normalized_type}
        if number_format_pattern:
            number_format["pattern"] = number_format_pattern

    # Validate and normalize wrap_strategy
    wrap_strategy_normalized = None
    if wrap_strategy:
        allowed_wrap_strategies = {"WRAP", "CLIP", "OVERFLOW_CELL"}
        wrap_strategy_normalized = wrap_strategy.upper()
        if wrap_strategy_normalized not in allowed_wrap_strategies:
            raise UserInputError(
                f"wrap_strategy must be one of {sorted(allowed_wrap_strategies)}."
            )

    # Validate and normalize horizontal_alignment
    h_align_normalized = None
    if horizontal_alignment:
        allowed_h_alignments = {"LEFT", "CENTER", "RIGHT"}
        h_align_normalized = horizontal_alignment.upper()
        if h_align_normalized not in allowed_h_alignments:
            raise UserInputError(
                f"horizontal_alignment must be one of {sorted(allowed_h_alignments)}."
            )

    # Validate and normalize vertical_alignment
    v_align_normalized = None
    if vertical_alignment:
        allowed_v_alignments = {"TOP", "MIDDLE", "BOTTOM"}
        v_align_normalized = vertical_alignment.upper()
        if v_align_normalized not in allowed_v_alignments:
            raise UserInputError(
                f"vertical_alignment must be one of {sorted(allowed_v_alignments)}."
            )

    # Get sheet metadata for range parsing
    metadata = await asyncio.to_thread(
        service.spreadsheets()
        .get(
            spreadsheetId=spreadsheet_id,
            fields="sheets(properties(sheetId,title))",
        )
        .execute
    )
    sheets = metadata.get("sheets", [])
    grid_range = _parse_a1_range(range_name, sheets)

    # Build userEnteredFormat and fields list
    user_entered_format = {}
    fields = []

    # Background color
    if bg_color_parsed:
        user_entered_format["backgroundColor"] = bg_color_parsed
        fields.append("userEnteredFormat.backgroundColor")

    # Text format (color, bold, italic, fontSize)
    text_format = {}
    text_format_fields = []

    if text_color_parsed:
        text_format["foregroundColor"] = text_color_parsed
        text_format_fields.append("userEnteredFormat.textFormat.foregroundColor")

    if bold is not None:
        text_format["bold"] = bold
        text_format_fields.append("userEnteredFormat.textFormat.bold")

    if italic is not None:
        text_format["italic"] = italic
        text_format_fields.append("userEnteredFormat.textFormat.italic")

    if font_size is not None:
        text_format["fontSize"] = font_size
        text_format_fields.append("userEnteredFormat.textFormat.fontSize")

    if text_format:
        user_entered_format["textFormat"] = text_format
        fields.extend(text_format_fields)

    # Number format
    if number_format:
        user_entered_format["numberFormat"] = number_format
        fields.append("userEnteredFormat.numberFormat")

    # Wrap strategy
    if wrap_strategy_normalized:
        user_entered_format["wrapStrategy"] = wrap_strategy_normalized
        fields.append("userEnteredFormat.wrapStrategy")

    # Horizontal alignment
    if h_align_normalized:
        user_entered_format["horizontalAlignment"] = h_align_normalized
        fields.append("userEnteredFormat.horizontalAlignment")

    # Vertical alignment
    if v_align_normalized:
        user_entered_format["verticalAlignment"] = v_align_normalized
        fields.append("userEnteredFormat.verticalAlignment")

    # Build and execute request(s): cell format first, then borders, then merges
    requests = []
    if user_entered_format:
        requests.append(
            {
                "repeatCell": {
                    "range": grid_range,
                    "cell": {"userEnteredFormat": user_entered_format},
                    "fields": ",".join(fields),
                }
            }
        )
    borders_request = None
    if border_style:
        borders_request = _build_update_borders_request(
            grid_range, border_style, border_color, border_sides
        )
        requests.append(borders_request)
    if unmerge:
        requests.append({"unmergeCells": {"range": grid_range}})
    if merge:
        requests.append(_build_merge_cells_request(grid_range, merge))

    if not requests:
        raise UserInputError(
            "No formatting applied. Verify provided formatting options."
        )

    request_body = {"requests": requests}

    await asyncio.to_thread(
        service.spreadsheets()
        .batchUpdate(spreadsheetId=spreadsheet_id, body=request_body)
        .execute
    )

    # Build confirmation message
    applied_parts = []
    if bg_color_parsed:
        applied_parts.append(f"background {background_color}")
    if text_color_parsed:
        applied_parts.append(f"text color {text_color}")
    if number_format:
        nf_desc = number_format["type"]
        if number_format_pattern:
            nf_desc += f" (pattern: {number_format_pattern})"
        applied_parts.append(f"number format {nf_desc}")
    if wrap_strategy_normalized:
        applied_parts.append(f"wrap {wrap_strategy_normalized}")
    if h_align_normalized:
        applied_parts.append(f"horizontal align {h_align_normalized}")
    if v_align_normalized:
        applied_parts.append(f"vertical align {v_align_normalized}")
    if bold is not None:
        applied_parts.append("bold" if bold else "not bold")
    if italic is not None:
        applied_parts.append("italic" if italic else "not italic")
    if font_size is not None:
        applied_parts.append(f"font size {font_size}")
    if borders_request:
        sides = [k for k in borders_request["updateBorders"] if k != "range"]
        border_desc = f"{border_style.strip().upper()} borders"
        if border_color:
            border_desc += f" {border_color}"
        applied_parts.append(f"{border_desc} on {', '.join(sides)}")
    if unmerge:
        applied_parts.append("unmerged cells")
    if merge:
        applied_parts.append(f"merged {merge.strip().lower()}")

    summary = ", ".join(applied_parts)

    # Return structured data for the wrapper to format
    return {
        "range_name": range_name,
        "spreadsheet_id": spreadsheet_id,
        "summary": summary,
    }


@server.tool(
    title="Format Sheet Range",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
@handle_http_errors("format_sheet_range", service_type="sheets")
@require_google_service("sheets", "sheets_write")
async def format_sheet_range(
    service,
    user_google_email: str,
    spreadsheet_id: str,
    range_name: str,
    background_color: Optional[str] = None,
    text_color: Optional[str] = None,
    number_format_type: Optional[str] = None,
    number_format_pattern: Optional[str] = None,
    wrap_strategy: Optional[str] = None,
    horizontal_alignment: Optional[str] = None,
    vertical_alignment: Optional[str] = None,
    bold: Optional[bool] = None,
    italic: Optional[bool] = None,
    font_size: Optional[int] = None,
    border_style: Optional[str] = None,
    border_color: Optional[str] = None,
    border_sides: Optional[List[str]] = None,
    merge: Optional[str] = None,
    unmerge: bool = False,
) -> str:
    """
    Applies formatting to a range: colors, number formats, text wrapping,
    alignment, text styling, borders, and merging/unmerging cells.

    Colors accept hex strings (#RRGGBB). Number formats follow Sheets types
    (e.g., NUMBER, CURRENCY, DATE, PERCENT). If no sheet name is provided,
    the first sheet is used.

    Args:
        user_google_email (str): The user's Google email address. Required.
        spreadsheet_id (str): The ID of the spreadsheet. Required.
        range_name (str): A1-style range (optionally with sheet name). Required.
        background_color (Optional[str]): Hex background color (e.g., "#FFEECC").
        text_color (Optional[str]): Hex text color (e.g., "#000000").
        number_format_type (Optional[str]): Sheets number format type (e.g., "DATE").
        number_format_pattern (Optional[str]): Custom pattern for the number format.
        wrap_strategy (Optional[str]): Text wrap strategy - WRAP (wrap text within
            cell), CLIP (clip text at cell boundary), or OVERFLOW_CELL (allow text
            to overflow into adjacent empty cells).
        horizontal_alignment (Optional[str]): Horizontal text alignment - LEFT,
            CENTER, or RIGHT.
        vertical_alignment (Optional[str]): Vertical text alignment - TOP, MIDDLE,
            or BOTTOM.
        bold (Optional[bool]): Whether to apply bold formatting.
        italic (Optional[bool]): Whether to apply italic formatting.
        font_size (Optional[int]): Font size in points.
        border_style (Optional[str]): Border line style - SOLID, SOLID_MEDIUM,
            SOLID_THICK, DASHED, DOTTED, DOUBLE, or NONE (removes borders).
        border_color (Optional[str]): Hex border color (e.g., "#000000").
            Requires border_style.
        border_sides (Optional[List[str]]): Which sides get the border: any of
            top, bottom, left, right, innerHorizontal, innerVertical, or the
            groups "all" (default), "outer", "inner". Requires border_style.
        merge (Optional[str]): Merge the cells in the range - "all" (one cell),
            "columns" (merge each column vertically) or "rows" (merge each row
            horizontally). Values other than the top-left of each merge are lost.
        unmerge (bool): Unmerge every merged cell in the range. Defaults to False.

    Returns:
        str: Confirmation of the applied formatting.
    """
    logger.info(
        "[format_sheet_range] Invoked. Email: '%s', Spreadsheet: %s, Range: %s",
        user_google_email,
        spreadsheet_id,
        range_name,
    )

    result = await _format_sheet_range_impl(
        service=service,
        spreadsheet_id=spreadsheet_id,
        range_name=range_name,
        background_color=background_color,
        text_color=text_color,
        number_format_type=number_format_type,
        number_format_pattern=number_format_pattern,
        wrap_strategy=wrap_strategy,
        horizontal_alignment=horizontal_alignment,
        vertical_alignment=vertical_alignment,
        bold=bold,
        italic=italic,
        font_size=font_size,
        border_style=border_style,
        border_color=border_color,
        border_sides=border_sides,
        merge=merge,
        unmerge=unmerge,
    )

    # Build confirmation message with user email
    return (
        f"Applied formatting to range '{result['range_name']}' in spreadsheet "
        f"{result['spreadsheet_id']} for {user_google_email}: {result['summary']}."
    )


@server.tool(
    title="Manage Conditional Formatting",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
@handle_http_errors("manage_conditional_formatting", service_type="sheets")
@require_google_service("sheets", "sheets_write")
async def manage_conditional_formatting(
    service,
    user_google_email: str,
    spreadsheet_id: str,
    action: str,
    range_name: Optional[str] = None,
    condition_type: Optional[str] = None,
    condition_values: Optional[Union[str, List[Union[str, int, float]]]] = None,
    background_color: Optional[str] = None,
    text_color: Optional[str] = None,
    rule_index: Optional[int] = None,
    gradient_points: Optional[Union[str, List[dict]]] = None,
    sheet_name: Optional[str] = None,
) -> str:
    """
    Manages conditional formatting rules on a Google Sheet. Supports adding,
    updating, and deleting conditional formatting rules via a single tool.

    Args:
        user_google_email (str): The user's Google email address. Required.
        spreadsheet_id (str): The ID of the spreadsheet. Required.
        action (str): The operation to perform. Must be one of "add", "update",
            or "delete".
        range_name (Optional[str]): A1-style range (optionally with sheet name).
            Required for "add". Optional for "update" (preserves existing ranges
            if omitted). Not used for "delete".
        condition_type (Optional[str]): Sheets condition type (e.g., NUMBER_GREATER,
            TEXT_CONTAINS, DATE_BEFORE, CUSTOM_FORMULA). Required for "add".
            Optional for "update" (preserves existing type if omitted).
        condition_values (Optional[Union[str, List[Union[str, int, float]]]]): Values
            for the condition; accepts a list or a JSON string representing a list.
            Depends on condition_type. Used by "add" and "update".
        background_color (Optional[str]): Hex background color to apply when
            condition matches. Used by "add" and "update".
        text_color (Optional[str]): Hex text color to apply when condition matches.
            Used by "add" and "update".
        rule_index (Optional[int]): 0-based index of the rule. For "add", optionally
            specifies insertion position. Required for "update" and "delete".
        gradient_points (Optional[Union[str, List[dict]]]): List (or JSON list) of
            gradient points for a color scale. If provided, a gradient rule is created
            and boolean parameters are ignored. Used by "add" and "update".
        sheet_name (Optional[str]): Sheet name to locate the rule when range_name is
            omitted. Defaults to the first sheet. Used by "update" and "delete".

    Returns:
        str: Confirmation of the operation and the current rule state.
    """
    allowed_actions = {"add", "update", "delete"}
    action_normalized = action.strip().lower()
    if action_normalized not in allowed_actions:
        raise UserInputError(
            f"action must be one of {sorted(allowed_actions)}, got '{action}'."
        )

    logger.info(
        "[manage_conditional_formatting] Invoked. Action: '%s', Email: '%s', Spreadsheet: %s",
        action_normalized,
        user_google_email,
        spreadsheet_id,
    )

    if action_normalized == "add":
        if not range_name:
            raise UserInputError("range_name is required for action 'add'.")
        if not condition_type and not gradient_points:
            raise UserInputError(
                "condition_type (or gradient_points) is required for action 'add'."
            )

        if rule_index is not None and (
            not isinstance(rule_index, int) or rule_index < 0
        ):
            raise UserInputError(
                "rule_index must be a non-negative integer when provided."
            )

        gradient_points_list = _parse_gradient_points(gradient_points)
        condition_values_list = (
            None if gradient_points_list else _parse_condition_values(condition_values)
        )

        sheets, sheet_titles = await _fetch_sheets_with_rules(service, spreadsheet_id)
        grid_range = _parse_a1_range(range_name, sheets)

        target_sheet = None
        for sheet in sheets:
            if sheet.get("properties", {}).get("sheetId") == grid_range.get("sheetId"):
                target_sheet = sheet
                break
        if target_sheet is None:
            raise UserInputError(
                "Target sheet not found while adding conditional formatting."
            )

        current_rules = target_sheet.get("conditionalFormats", []) or []

        insert_at = rule_index if rule_index is not None else len(current_rules)
        if insert_at > len(current_rules):
            raise UserInputError(
                f"rule_index {insert_at} is out of range for sheet "
                f"'{target_sheet.get('properties', {}).get('title', 'Unknown')}' "
                f"(current count: {len(current_rules)})."
            )

        if gradient_points_list:
            new_rule = _build_gradient_rule([grid_range], gradient_points_list)
            rule_desc = "gradient"
            values_desc = ""
            applied_parts = [f"gradient points {len(gradient_points_list)}"]
        else:
            rule, cond_type_normalized = _build_boolean_rule(
                [grid_range],
                condition_type,
                condition_values_list,
                background_color,
                text_color,
            )
            new_rule = rule
            rule_desc = cond_type_normalized
            values_desc = ""
            if condition_values_list:
                values_desc = f" with values {condition_values_list}"
            applied_parts = []
            if background_color:
                applied_parts.append(f"background {background_color}")
            if text_color:
                applied_parts.append(f"text {text_color}")

        new_rules_state = copy.deepcopy(current_rules)
        new_rules_state.insert(insert_at, new_rule)

        add_rule_request = {"rule": new_rule}
        if rule_index is not None:
            add_rule_request["index"] = rule_index

        request_body = {"requests": [{"addConditionalFormatRule": add_rule_request}]}

        await asyncio.to_thread(
            service.spreadsheets()
            .batchUpdate(spreadsheetId=spreadsheet_id, body=request_body)
            .execute
        )

        format_desc = ", ".join(applied_parts) if applied_parts else "format applied"

        sheet_title = target_sheet.get("properties", {}).get("title", "Unknown")
        state_text = _format_conditional_rules_section(
            sheet_title, new_rules_state, sheet_titles, indent=""
        )

        return "\n".join(
            [
                f"Added conditional format on '{range_name}' in spreadsheet "
                f"{spreadsheet_id} for {user_google_email}: "
                f"{rule_desc}{values_desc}; format: {format_desc}.",
                state_text,
            ]
        )

    elif action_normalized == "update":
        if rule_index is None:
            raise UserInputError("rule_index is required for action 'update'.")
        if not isinstance(rule_index, int) or rule_index < 0:
            raise UserInputError("rule_index must be a non-negative integer.")

        gradient_points_list = _parse_gradient_points(gradient_points)
        condition_values_list = (
            None
            if gradient_points_list is not None
            else _parse_condition_values(condition_values)
        )

        sheets, sheet_titles = await _fetch_sheets_with_rules(service, spreadsheet_id)

        target_sheet = None
        grid_range = None
        if range_name:
            grid_range = _parse_a1_range(range_name, sheets)
            for sheet in sheets:
                if sheet.get("properties", {}).get("sheetId") == grid_range.get(
                    "sheetId"
                ):
                    target_sheet = sheet
                    break
        else:
            target_sheet = _select_sheet(sheets, sheet_name)

        if target_sheet is None:
            raise UserInputError(
                "Target sheet not found while updating conditional formatting."
            )

        sheet_props = target_sheet.get("properties", {})
        sheet_id = sheet_props.get("sheetId")
        sheet_title = sheet_props.get("title", f"Sheet {sheet_id}")

        rules = target_sheet.get("conditionalFormats", []) or []
        if rule_index >= len(rules):
            raise UserInputError(
                f"rule_index {rule_index} is out of range for sheet "
                f"'{sheet_title}' (current count: {len(rules)})."
            )

        existing_rule = rules[rule_index]
        ranges_to_use = existing_rule.get("ranges", [])
        if range_name:
            ranges_to_use = [grid_range]
        if not ranges_to_use:
            ranges_to_use = [{"sheetId": sheet_id}]

        new_rule = None
        rule_desc = ""
        values_desc = ""
        format_desc = ""

        if gradient_points_list is not None:
            new_rule = _build_gradient_rule(ranges_to_use, gradient_points_list)
            rule_desc = "gradient"
            format_desc = f"gradient points {len(gradient_points_list)}"
        elif "gradientRule" in existing_rule:
            if any(
                [
                    background_color,
                    text_color,
                    condition_type,
                    condition_values_list,
                ]
            ):
                raise UserInputError(
                    "Existing rule is a gradient rule. Provide gradient_points "
                    "to update it, or omit formatting/condition parameters to "
                    "keep it unchanged."
                )
            new_rule = {
                "ranges": ranges_to_use,
                "gradientRule": existing_rule.get("gradientRule", {}),
            }
            rule_desc = "gradient"
            format_desc = "gradient (unchanged)"
        else:
            existing_boolean = existing_rule.get("booleanRule", {})
            existing_condition = existing_boolean.get("condition", {})
            existing_format = copy.deepcopy(existing_boolean.get("format", {}))

            cond_type = (condition_type or existing_condition.get("type", "")).upper()
            if not cond_type:
                raise UserInputError("condition_type is required for boolean rules.")
            if cond_type not in CONDITION_TYPES:
                raise UserInputError(
                    f"condition_type must be one of {sorted(CONDITION_TYPES)}."
                )

            if condition_values_list is not None:
                cond_values = [
                    {"userEnteredValue": str(val)} for val in condition_values_list
                ]
            else:
                cond_values = existing_condition.get("values")

            new_format = copy.deepcopy(existing_format) if existing_format else {}
            if background_color is not None:
                bg_color_parsed = _parse_hex_color(background_color)
                if bg_color_parsed:
                    new_format["backgroundColor"] = bg_color_parsed
                elif "backgroundColor" in new_format:
                    del new_format["backgroundColor"]
            if text_color is not None:
                text_color_parsed = _parse_hex_color(text_color)
                text_format = copy.deepcopy(new_format.get("textFormat", {}))
                if text_color_parsed:
                    text_format["foregroundColor"] = text_color_parsed
                elif "foregroundColor" in text_format:
                    del text_format["foregroundColor"]
                if text_format:
                    new_format["textFormat"] = text_format
                elif "textFormat" in new_format:
                    del new_format["textFormat"]

            if not new_format:
                raise UserInputError(
                    "At least one format option must remain on the rule."
                )

            new_rule = {
                "ranges": ranges_to_use,
                "booleanRule": {
                    "condition": {"type": cond_type},
                    "format": new_format,
                },
            }
            if cond_values:
                new_rule["booleanRule"]["condition"]["values"] = cond_values

            rule_desc = cond_type
            if condition_values_list:
                values_desc = f" with values {condition_values_list}"
            format_parts = []
            if "backgroundColor" in new_format:
                format_parts.append("background updated")
            if "textFormat" in new_format and new_format["textFormat"].get(
                "foregroundColor"
            ):
                format_parts.append("text color updated")
            format_desc = (
                ", ".join(format_parts) if format_parts else "format preserved"
            )

        new_rules_state = copy.deepcopy(rules)
        new_rules_state[rule_index] = new_rule

        request_body = {
            "requests": [
                {
                    "updateConditionalFormatRule": {
                        "index": rule_index,
                        "sheetId": sheet_id,
                        "rule": new_rule,
                    }
                }
            ]
        }

        await asyncio.to_thread(
            service.spreadsheets()
            .batchUpdate(spreadsheetId=spreadsheet_id, body=request_body)
            .execute
        )

        state_text = _format_conditional_rules_section(
            sheet_title, new_rules_state, sheet_titles, indent=""
        )

        return "\n".join(
            [
                f"Updated conditional format at index {rule_index} on sheet "
                f"'{sheet_title}' in spreadsheet {spreadsheet_id} "
                f"for {user_google_email}: "
                f"{rule_desc}{values_desc}; format: {format_desc}.",
                state_text,
            ]
        )

    else:  # action_normalized == "delete"
        if rule_index is None:
            raise UserInputError("rule_index is required for action 'delete'.")
        if not isinstance(rule_index, int) or rule_index < 0:
            raise UserInputError("rule_index must be a non-negative integer.")

        sheets, sheet_titles = await _fetch_sheets_with_rules(service, spreadsheet_id)
        target_sheet = _select_sheet(sheets, sheet_name)

        sheet_props = target_sheet.get("properties", {})
        sheet_id = sheet_props.get("sheetId")
        target_sheet_name = sheet_props.get("title", f"Sheet {sheet_id}")
        rules = target_sheet.get("conditionalFormats", []) or []
        if rule_index >= len(rules):
            raise UserInputError(
                f"rule_index {rule_index} is out of range for sheet "
                f"'{target_sheet_name}' (current count: {len(rules)})."
            )

        new_rules_state = copy.deepcopy(rules)
        del new_rules_state[rule_index]

        request_body = {
            "requests": [
                {
                    "deleteConditionalFormatRule": {
                        "index": rule_index,
                        "sheetId": sheet_id,
                    }
                }
            ]
        }

        await asyncio.to_thread(
            service.spreadsheets()
            .batchUpdate(spreadsheetId=spreadsheet_id, body=request_body)
            .execute
        )

        state_text = _format_conditional_rules_section(
            target_sheet_name, new_rules_state, sheet_titles, indent=""
        )

        return "\n".join(
            [
                f"Deleted conditional format at index {rule_index} on sheet "
                f"'{target_sheet_name}' in spreadsheet {spreadsheet_id} "
                f"for {user_google_email}.",
                state_text,
            ]
        )


@server.tool(
    title="Create Spreadsheet",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
@handle_http_errors("create_spreadsheet", service_type="sheets")
@require_google_service("sheets", "sheets_write")
async def create_spreadsheet(
    service,
    user_google_email: str,
    title: str,
    sheet_names: Optional[StringList] = None,
    folder_id: str = "root",
) -> str:
    """
    Creates a new Google Spreadsheet.

    Args:
        user_google_email (str): The user's Google email address. Required.
        title (str): The title of the new spreadsheet. Required.
        sheet_names (Optional[List[str]]): List of sheet names to create. If not provided, creates one sheet with default name.
        folder_id (str): The ID of the parent folder. Defaults to 'root'. For shared
            drives, this must be a folder ID within the shared drive.

    Returns:
        str: Information about the newly created spreadsheet including ID, URL, and locale.
    """
    logger.info(
        f"[create_spreadsheet] Invoked. Email: '{user_google_email}', "
        f"title_len={len(title)}, folder_id='{folder_id}'"
    )

    spreadsheet_body = {"properties": {"title": title}}

    if sheet_names:
        spreadsheet_body["sheets"] = [
            {"properties": {"title": sheet_name}} for sheet_name in sheet_names
        ]

    spreadsheet = await asyncio.to_thread(
        service.spreadsheets()
        .create(
            body=spreadsheet_body,
            fields="spreadsheetId,spreadsheetUrl,properties(title,locale)",
        )
        .execute
    )

    properties = spreadsheet.get("properties", {})
    spreadsheet_id = spreadsheet.get("spreadsheetId")
    spreadsheet_url = spreadsheet.get("spreadsheetUrl")
    locale = properties.get("locale", "Unknown")

    placement_note = await move_new_file_to_folder(
        user_google_email, spreadsheet_id, folder_id, "create_spreadsheet"
    )

    text_output = (
        f"Successfully created spreadsheet '{title}' for {user_google_email}."
        f"{placement_note} "
        f"ID: {spreadsheet_id} | URL: {spreadsheet_url} | Locale: {locale}"
    )

    logger.info(
        f"Successfully created spreadsheet for {user_google_email}. ID: {spreadsheet_id}"
    )
    return text_output


@server.tool(
    title="Create Sheet",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
@handle_http_errors("create_sheet", service_type="sheets")
@require_google_service("sheets", "sheets_write")
async def create_sheet(
    service,
    user_google_email: str,
    spreadsheet_id: str,
    sheet_name: Optional[str] = None,
    source_sheet_name: Optional[str] = None,
    insert_sheet_index: Optional[int] = None,
) -> str:
    """Creates a new sheet or duplicates an existing sheet (user_google_email: str, spreadsheet_id: str, sheet_name: Optional[str] = None, source_sheet_name: Optional[str] = None, insert_sheet_index: Optional[int] = None)."""
    if insert_sheet_index is not None and (
        isinstance(insert_sheet_index, bool)
        or not isinstance(insert_sheet_index, int)
        or insert_sheet_index < 0
    ):
        raise UserInputError("insert_sheet_index must be a non-negative integer.")

    if source_sheet_name is not None:
        source_sheet_name = source_sheet_name.strip()
        if not source_sheet_name:
            raise UserInputError("source_sheet_name must be a non-empty string")

        logger.info(
            f"[create_sheet] Duplicate invoked. Email: '{user_google_email}', "
            f"Spreadsheet: {spreadsheet_id}, Source: {source_sheet_name}"
        )

        spreadsheet = await asyncio.to_thread(
            service.spreadsheets()
            .get(spreadsheetId=spreadsheet_id, fields="sheets.properties")
            .execute
        )

        sheets = spreadsheet.get("sheets", [])
        source_sheet = _select_sheet(sheets, source_sheet_name)
        source_sheet_id = source_sheet["properties"]["sheetId"]

        dup_request = {"sourceSheetId": source_sheet_id}
        if sheet_name is not None:
            dup_request["newSheetName"] = sheet_name
        if insert_sheet_index is not None:
            dup_request["insertSheetIndex"] = insert_sheet_index

        request_body = {"requests": [{"duplicateSheet": dup_request}]}

        response = await asyncio.to_thread(
            service.spreadsheets()
            .batchUpdate(spreadsheetId=spreadsheet_id, body=request_body)
            .execute
        )

        new_props = response["replies"][0]["duplicateSheet"]["properties"]
        new_id = new_props["sheetId"]
        new_title = new_props["title"]

        text_output = (
            f"Successfully duplicated '{source_sheet_name}' to '{new_title}' "
            f"(ID: {new_id}) in spreadsheet {spreadsheet_id} for {user_google_email}."
        )

        logger.info(
            f"Successfully duplicated sheet for {user_google_email}. "
            f"New sheet ID: {new_id}"
        )
        return text_output

    logger.info(
        f"[create_sheet] Invoked. Email: '{user_google_email}', Spreadsheet: {spreadsheet_id}, sheet_name_len={len(sheet_name) if sheet_name else 0}"
    )

    add_request: dict = {"properties": {}}
    if sheet_name is not None:
        add_request["properties"]["title"] = sheet_name
    if insert_sheet_index is not None:
        add_request["properties"]["index"] = insert_sheet_index

    request_body = {"requests": [{"addSheet": add_request}]}

    response = await asyncio.to_thread(
        service.spreadsheets()
        .batchUpdate(spreadsheetId=spreadsheet_id, body=request_body)
        .execute
    )

    sheet_props = response["replies"][0]["addSheet"]["properties"]
    sheet_id = sheet_props["sheetId"]
    created_sheet_name = sheet_props.get("title", sheet_name or "Untitled")

    text_output = f"Successfully created sheet '{created_sheet_name}' (ID: {sheet_id}) in spreadsheet {spreadsheet_id} for {user_google_email}."

    logger.info(
        f"Successfully created sheet for {user_google_email}. Sheet ID: {sheet_id}"
    )
    return text_output


def _to_extended_value(val) -> dict:
    """Convert a Python value to a Sheets API ExtendedValue dict."""
    if isinstance(val, bool):
        return {"boolValue": val}
    if isinstance(val, (int, float)):
        return {"numberValue": val}
    s = str(val)
    if s.startswith("="):
        return {"formulaValue": s}
    return {"stringValue": s}


@server.tool(
    title="List Sheet Tables",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@handle_http_errors("list_sheet_tables", is_read_only=True, service_type="sheets")
@require_google_service("sheets", "sheets_read")
async def list_sheet_tables(
    service,
    user_google_email: str,
    spreadsheet_id: str,
) -> str:
    """
    Lists all structured tables in a spreadsheet with their IDs, names, ranges,
    and column details. Use this to find table IDs for append_table_rows.

    Args:
        user_google_email (str): The user's Google email address. Required.
        spreadsheet_id (str): The ID of the spreadsheet. Required.

    Returns:
        str: Formatted list of tables with their IDs, names, ranges, and columns.
    """
    logger.info(
        f"[list_sheet_tables] Invoked. Email: '{user_google_email}', "
        f"Spreadsheet: {spreadsheet_id}"
    )

    spreadsheet = await asyncio.to_thread(
        service.spreadsheets()
        .get(
            spreadsheetId=spreadsheet_id,
            fields="sheets(properties(title,sheetId),tables)",
        )
        .execute
    )

    tables_found = []
    for sheet in spreadsheet.get("sheets", []):
        sheet_title = sheet.get("properties", {}).get("title", "Unknown")
        for table in sheet.get("tables", []):
            table_id = table.get("tableId")
            name = table.get("name", "Unnamed")
            range_info = table.get("range", {})

            start_row = range_info.get("startRowIndex", 0)
            end_row = range_info.get("endRowIndex", "?")
            start_col = range_info.get("startColumnIndex", 0)
            end_col = range_info.get("endColumnIndex", "?")

            columns = []
            for col in table.get("columnProperties", []):
                col_name = col.get("columnName", "")
                columns.append(col_name)

            tables_found.append(
                f"  Table ID: {table_id}\n"
                f"  Name: {name}\n"
                f"  Sheet: {sheet_title}\n"
                f"  Range: rows {start_row}-{end_row}, cols {start_col}-{end_col}\n"
                f"  Columns: {', '.join(columns) if columns else 'N/A'}"
            )

    if not tables_found:
        text_output = (
            f"No structured tables found in spreadsheet {spreadsheet_id} "
            f"for {user_google_email}."
        )
    else:
        text_output = (
            f"Found {len(tables_found)} table(s) in spreadsheet {spreadsheet_id} "
            f"for {user_google_email}:\n\n" + "\n\n".join(tables_found)
        )

    logger.info(
        f"[list_sheet_tables] Found {len(tables_found)} tables for {user_google_email}"
    )
    return text_output


@server.tool(
    title="Append Table Rows",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
@handle_http_errors("append_table_rows", service_type="sheets")
@require_google_service("sheets", "sheets_write")
async def append_table_rows(
    service,
    user_google_email: str,
    spreadsheet_id: str,
    table_id: str,
    values: Union[str, List[List]],
) -> str:
    """
    Appends rows to a structured table in a Google Sheet. The rows are added
    to the end of the table body, automatically extending the table range.

    Use list_sheet_tables first to find the table ID.

    Args:
        user_google_email (str): The user's Google email address. Required.
        spreadsheet_id (str): The ID of the spreadsheet. Required.
        table_id (str): The ID of the table to append to (get from list_sheet_tables). Required.
        values (Union[str, List[List]]): 2D array of values to append. Each inner
            list is one row. Can be a JSON string or Python list. Required.

    Returns:
        str: Confirmation message with the number of rows appended.
    """
    logger.info(
        f"[append_table_rows] Invoked. Email: '{user_google_email}', "
        f"Spreadsheet: {spreadsheet_id}, Table: {table_id}"
    )

    # Parse values if JSON string
    if isinstance(values, str):
        try:
            values = json.loads(values)
        except json.JSONDecodeError as e:
            raise UserInputError(f"Invalid JSON in values parameter: {e}")

    if not values or not isinstance(values, list):
        raise UserInputError("values must be a non-empty 2D list of cell values.")

    # Resolve the sheet ID for the table before building the request
    spreadsheet = await asyncio.to_thread(
        service.spreadsheets()
        .get(
            spreadsheetId=spreadsheet_id,
            fields="sheets(properties(sheetId),tables(tableId))",
        )
        .execute
    )

    sheet_id = None
    for sheet in spreadsheet.get("sheets", []):
        for table in sheet.get("tables", []):
            if table.get("tableId") == table_id:
                sheet_id = sheet["properties"]["sheetId"]
                break
        if sheet_id is not None:
            break

    if sheet_id is None:
        raise UserInputError(
            f"Table '{table_id}' not found in spreadsheet {spreadsheet_id}. "
            f"Use list_sheet_tables to find valid table IDs."
        )

    # Build cell data for appendCells
    rows = []
    for row_values in values:
        if not isinstance(row_values, list):
            raise UserInputError(
                "Each row in values must be a list. "
                'Expected format: [["val1", "val2"], ["val3", "val4"]]'
            )
        cells = []
        for val in row_values:
            cells.append({"userEnteredValue": _to_extended_value(val)})
        rows.append({"values": cells})

    request_body = {
        "requests": [
            {
                "appendCells": {
                    "sheetId": sheet_id,
                    "tableId": table_id,
                    "rows": rows,
                    "fields": "userEnteredValue",
                }
            }
        ]
    }

    await asyncio.to_thread(
        service.spreadsheets()
        .batchUpdate(spreadsheetId=spreadsheet_id, body=request_body)
        .execute
    )

    num_rows = len(values)
    text_output = (
        f"Successfully appended {num_rows} row(s) to table '{table_id}' "
        f"in spreadsheet {spreadsheet_id} for {user_google_email}."
    )

    logger.info(f"[append_table_rows] Appended {num_rows} rows for {user_google_email}")
    return text_output


def _build_column_visibility_requests(sheet_id, letters, hidden, label):
    """Build updateDimensionProperties requests to hide/unhide columns."""
    if not isinstance(letters, list):
        raise UserInputError(f"{label} must be a list of column letters.")
    reqs = []
    for col_letter in letters:
        col_idx = _column_to_index(str(col_letter).upper())
        if col_idx is None:
            raise UserInputError(f"Invalid column letter in {label}: '{col_letter}'.")
        reqs.append(
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "COLUMNS",
                        "startIndex": col_idx,
                        "endIndex": col_idx + 1,
                    },
                    "properties": {"hiddenByUser": hidden},
                    "fields": "hiddenByUser",
                }
            }
        )
    return reqs


def _build_row_visibility_requests(sheet_id, row_nums, hidden, label):
    """Build updateDimensionProperties requests to hide/unhide rows."""
    if not isinstance(row_nums, list):
        raise UserInputError(f"{label} must be a list of row numbers.")
    reqs = []
    for row_num in row_nums:
        try:
            row_num = int(row_num)
        except ValueError as exc:
            raise UserInputError(
                f"Row number must be an integer in {label}, got {row_num}."
            ) from exc
        if row_num < 1:
            raise UserInputError(f"Row number must be >= 1 in {label}, got {row_num}.")
        reqs.append(
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "ROWS",
                        "startIndex": row_num - 1,
                        "endIndex": row_num,
                    },
                    "properties": {"hiddenByUser": hidden},
                    "fields": "hiddenByUser",
                }
            }
        )
    return reqs


async def _resize_sheet_dimensions_impl(
    service,
    spreadsheet_id: str,
    sheet_name: Optional[str] = None,
    column_sizes: Optional[Union[str, dict]] = None,
    row_sizes: Optional[Union[str, dict]] = None,
    auto_resize_columns: Optional[Union[str, List[str]]] = None,
    auto_resize_rows: Optional[Union[str, List[int]]] = None,
    frozen_row_count: Optional[int] = None,
    frozen_column_count: Optional[int] = None,
    hide_columns: Optional[Union[str, List[str]]] = None,
    unhide_columns: Optional[Union[str, List[str]]] = None,
    hide_rows: Optional[Union[str, List[int]]] = None,
    unhide_rows: Optional[Union[str, List[int]]] = None,
    insert_rows: Optional[int] = None,
    insert_rows_at: Optional[int] = None,
    insert_columns: Optional[int] = None,
    insert_columns_at: Optional[str] = None,
    delete_rows: Optional[Union[str, List[int]]] = None,
    delete_row_range: Optional[str] = None,
    delete_columns: Optional[Union[str, List[str]]] = None,
) -> dict:
    """Internal implementation for resize_sheet_dimensions.

    Manages sheet-level dimension properties: resize columns/rows, auto-resize
    to fit content, freeze rows/columns, hide/unhide rows/columns, and
    insert/delete rows/columns.

    Args:
        service: Google Sheets API service client.
        spreadsheet_id: The ID of the spreadsheet.
        sheet_name: Sheet name to target. Defaults to the first sheet.
        column_sizes: Dict mapping column letters to pixel widths.
        row_sizes: Dict mapping 1-based row numbers to pixel heights.
        auto_resize_columns: List of column letters to auto-resize to fit content.
        auto_resize_rows: List of 1-based row numbers to auto-resize to fit content.
        frozen_row_count: Number of rows to freeze from the top (0 to unfreeze).
        frozen_column_count: Number of columns to freeze from the left (0 to unfreeze).
        hide_columns: List of column letters to hide.
        unhide_columns: List of column letters to unhide.
        hide_rows: List of 1-based row numbers to hide.
        unhide_rows: List of 1-based row numbers to unhide.
        insert_rows: Number of rows to insert.
        insert_rows_at: 1-based row number to insert before. Appends to end if omitted.
        insert_columns: Number of columns to insert.
        insert_columns_at: Column letter to insert before (e.g. "C"). Appends to end if omitted.
        delete_rows: List of 1-based row numbers to delete.
        delete_row_range: Contiguous range of rows to delete, as "start:end"
            (1-based, inclusive). Example: "5:10". More efficient than
            delete_rows for large contiguous ranges.
        delete_columns: List of column letters to delete.

    Returns:
        Dictionary with keys: spreadsheet_id, summary.
    """
    has_any = any(
        [
            column_sizes,
            row_sizes,
            auto_resize_columns,
            auto_resize_rows,
            frozen_row_count is not None,
            frozen_column_count is not None,
            hide_columns,
            unhide_columns,
            hide_rows,
            unhide_rows,
            insert_rows is not None,
            insert_columns is not None,
            delete_rows,
            delete_row_range,
            delete_columns,
        ]
    )
    if not has_any:
        raise UserInputError(
            "Provide at least one of: column_sizes, row_sizes, "
            "auto_resize_columns, auto_resize_rows, frozen_row_count, "
            "frozen_column_count, hide_columns, unhide_columns, "
            "hide_rows, unhide_rows, insert_rows, insert_columns, "
            "delete_rows, delete_row_range, or delete_columns."
        )

    # Parse JSON string parameters
    def _parse_json(value, name):
        if not isinstance(value, str):
            return value
        try:
            return json.loads(value)
        except json.JSONDecodeError as e:
            raise UserInputError(f"Invalid JSON for {name}: {e}")

    column_sizes = _parse_json(column_sizes, "column_sizes")
    row_sizes = _parse_json(row_sizes, "row_sizes")
    auto_resize_columns = _parse_json(auto_resize_columns, "auto_resize_columns")
    auto_resize_rows = _parse_json(auto_resize_rows, "auto_resize_rows")
    hide_columns = _parse_json(hide_columns, "hide_columns")
    unhide_columns = _parse_json(unhide_columns, "unhide_columns")
    hide_rows = _parse_json(hide_rows, "hide_rows")
    unhide_rows = _parse_json(unhide_rows, "unhide_rows")
    delete_rows = _parse_json(delete_rows, "delete_rows")
    delete_columns = _parse_json(delete_columns, "delete_columns")

    # Get sheet metadata to resolve sheet ID
    metadata = await asyncio.to_thread(
        service.spreadsheets()
        .get(
            spreadsheetId=spreadsheet_id,
            fields="sheets(properties(sheetId,title))",
        )
        .execute
    )
    sheets = metadata.get("sheets", [])
    if not sheets:
        raise UserInputError("No sheets found in spreadsheet.")

    # Find target sheet
    target_sheet = None
    if sheet_name:
        for sheet in sheets:
            if sheet.get("properties", {}).get("title") == sheet_name:
                target_sheet = sheet
                break
        if not target_sheet:
            raise UserInputError(f"Sheet '{sheet_name}' not found.")
    else:
        target_sheet = sheets[0]

    sheet_id = target_sheet["properties"]["sheetId"]

    requests = []
    applied_parts = []

    # Build column resize requests
    if column_sizes:
        if not isinstance(column_sizes, dict):
            raise UserInputError(
                "column_sizes must be a dict mapping column letters to pixel widths."
            )
        for col_letter, pixel_size in column_sizes.items():
            col_idx = _column_to_index(col_letter.upper())
            if col_idx is None:
                raise UserInputError(f"Invalid column letter: '{col_letter}'.")
            if not isinstance(pixel_size, (int, float)) or pixel_size <= 0:
                raise UserInputError(
                    f"Pixel size for column '{col_letter}' must be a positive number."
                )
            requests.append(
                {
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": "COLUMNS",
                            "startIndex": col_idx,
                            "endIndex": col_idx + 1,
                        },
                        "properties": {"pixelSize": int(pixel_size)},
                        "fields": "pixelSize",
                    }
                }
            )
        applied_parts.append(
            f"resized columns: {', '.join(f'{k}={v}px' for k, v in column_sizes.items())}"
        )

    # Build row resize requests
    if row_sizes:
        if not isinstance(row_sizes, dict):
            raise UserInputError(
                "row_sizes must be a dict mapping row numbers to pixel heights."
            )
        for row_num_str, pixel_size in row_sizes.items():
            try:
                row_num = int(row_num_str)
            except ValueError as exc:
                raise UserInputError(
                    f"Row number must be an integer >= 1, got {row_num_str}."
                ) from exc
            if row_num < 1:
                raise UserInputError(f"Row number must be >= 1, got {row_num}.")
            if not isinstance(pixel_size, (int, float)) or pixel_size <= 0:
                raise UserInputError(
                    f"Pixel size for row {row_num} must be a positive number."
                )
            requests.append(
                {
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": "ROWS",
                            "startIndex": row_num - 1,
                            "endIndex": row_num,
                        },
                        "properties": {"pixelSize": int(pixel_size)},
                        "fields": "pixelSize",
                    }
                }
            )
        applied_parts.append(
            f"resized rows: {', '.join(f'{k}={v}px' for k, v in row_sizes.items())}"
        )

    # Build auto-resize column requests
    if auto_resize_columns:
        if not isinstance(auto_resize_columns, list):
            raise UserInputError(
                "auto_resize_columns must be a list of column letters."
            )
        for col_letter in auto_resize_columns:
            col_idx = _column_to_index(str(col_letter).upper())
            if col_idx is None:
                raise UserInputError(f"Invalid column letter: '{col_letter}'.")
            requests.append(
                {
                    "autoResizeDimensions": {
                        "dimensions": {
                            "sheetId": sheet_id,
                            "dimension": "COLUMNS",
                            "startIndex": col_idx,
                            "endIndex": col_idx + 1,
                        }
                    }
                }
            )
        applied_parts.append(
            f"auto-resized columns: {', '.join(str(c) for c in auto_resize_columns)}"
        )

    # Build auto-resize row requests
    if auto_resize_rows:
        if not isinstance(auto_resize_rows, list):
            raise UserInputError("auto_resize_rows must be a list of row numbers.")
        for row_num in auto_resize_rows:
            try:
                parsed_row_num = int(row_num)
            except ValueError as exc:
                raise UserInputError(
                    f"Row number must be an integer >= 1, got {row_num}."
                ) from exc
            if parsed_row_num < 1:
                raise UserInputError(f"Row number must be >= 1, got {parsed_row_num}.")
            requests.append(
                {
                    "autoResizeDimensions": {
                        "dimensions": {
                            "sheetId": sheet_id,
                            "dimension": "ROWS",
                            "startIndex": parsed_row_num - 1,
                            "endIndex": parsed_row_num,
                        }
                    }
                }
            )
        applied_parts.append(
            f"auto-resized rows: {', '.join(str(r) for r in auto_resize_rows)}"
        )

    # Build freeze requests
    grid_properties = {}
    grid_fields = []
    if frozen_row_count is not None:
        if not isinstance(frozen_row_count, int) or frozen_row_count < 0:
            raise UserInputError("frozen_row_count must be a non-negative integer.")
        grid_properties["frozenRowCount"] = frozen_row_count
        grid_fields.append("gridProperties.frozenRowCount")
        applied_parts.append(
            f"froze {frozen_row_count} row(s)"
            if frozen_row_count > 0
            else "unfroze rows"
        )

    if frozen_column_count is not None:
        if not isinstance(frozen_column_count, int) or frozen_column_count < 0:
            raise UserInputError("frozen_column_count must be a non-negative integer.")
        grid_properties["frozenColumnCount"] = frozen_column_count
        grid_fields.append("gridProperties.frozenColumnCount")
        applied_parts.append(
            f"froze {frozen_column_count} column(s)"
            if frozen_column_count > 0
            else "unfroze columns"
        )

    if grid_properties:
        requests.append(
            {
                "updateSheetProperties": {
                    "properties": {
                        "sheetId": sheet_id,
                        "gridProperties": grid_properties,
                    },
                    "fields": ",".join(grid_fields),
                }
            }
        )

    # Build hide/unhide column requests
    if hide_columns:
        requests.extend(
            _build_column_visibility_requests(
                sheet_id, hide_columns, True, "hide_columns"
            )
        )
        applied_parts.append(f"hid columns: {', '.join(str(c) for c in hide_columns)}")

    if unhide_columns:
        requests.extend(
            _build_column_visibility_requests(
                sheet_id, unhide_columns, False, "unhide_columns"
            )
        )
        applied_parts.append(
            f"unhid columns: {', '.join(str(c) for c in unhide_columns)}"
        )

    # Build hide/unhide row requests
    if hide_rows:
        requests.extend(
            _build_row_visibility_requests(sheet_id, hide_rows, True, "hide_rows")
        )
        applied_parts.append(f"hid rows: {', '.join(str(r) for r in hide_rows)}")

    if unhide_rows:
        requests.extend(
            _build_row_visibility_requests(sheet_id, unhide_rows, False, "unhide_rows")
        )
        applied_parts.append(f"unhid rows: {', '.join(str(r) for r in unhide_rows)}")

    # Build insert row requests
    if insert_rows is not None:
        if not isinstance(insert_rows, int) or insert_rows < 1:
            raise UserInputError("insert_rows must be a positive integer.")
        if insert_rows_at is not None:
            if not isinstance(insert_rows_at, int) or insert_rows_at < 1:
                raise UserInputError(
                    "insert_rows_at must be a positive integer (1-based)."
                )
            start_idx = insert_rows_at - 1
        else:
            start_idx = None

        if start_idx is not None:
            requests.append(
                {
                    "insertDimension": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": "ROWS",
                            "startIndex": start_idx,
                            "endIndex": start_idx + insert_rows,
                        },
                        "inheritFromBefore": start_idx > 0,
                    }
                }
            )
            applied_parts.append(
                f"inserted {insert_rows} row(s) at row {insert_rows_at}"
            )
        else:
            requests.append(
                {
                    "appendDimension": {
                        "sheetId": sheet_id,
                        "dimension": "ROWS",
                        "length": insert_rows,
                    }
                }
            )
            applied_parts.append(f"appended {insert_rows} row(s)")

    # Build insert column requests
    if insert_columns is not None:
        if not isinstance(insert_columns, int) or insert_columns < 1:
            raise UserInputError("insert_columns must be a positive integer.")
        if insert_columns_at is not None:
            col_idx = _column_to_index(str(insert_columns_at).upper())
            if col_idx is None:
                raise UserInputError(
                    f"Invalid column letter for insert_columns_at: '{insert_columns_at}'."
                )
            requests.append(
                {
                    "insertDimension": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": "COLUMNS",
                            "startIndex": col_idx,
                            "endIndex": col_idx + insert_columns,
                        },
                        "inheritFromBefore": col_idx > 0,
                    }
                }
            )
            applied_parts.append(
                f"inserted {insert_columns} column(s) at column {insert_columns_at}"
            )
        else:
            requests.append(
                {
                    "appendDimension": {
                        "sheetId": sheet_id,
                        "dimension": "COLUMNS",
                        "length": insert_columns,
                    }
                }
            )
            applied_parts.append(f"appended {insert_columns} column(s)")

    # Reject mixing delete_rows and delete_row_range — their interleaved
    # deleteDimension requests shift indices unpredictably.
    if delete_rows and delete_row_range:
        raise UserInputError(
            "delete_rows and delete_row_range cannot be used together. "
            "Specify one or the other."
        )

    # Build delete row requests (process in reverse to keep indices stable)
    if delete_rows:
        if not isinstance(delete_rows, list):
            raise UserInputError("delete_rows must be a list of row numbers.")
        parsed_delete_rows = []
        for row_num in delete_rows:
            try:
                parsed_delete_rows.append(int(row_num))
            except ValueError as exc:
                raise UserInputError(
                    f"Row number must be an integer >= 1 in delete_rows, got {row_num}."
                ) from exc
        sorted_rows = sorted(parsed_delete_rows, reverse=True)
        for row_num in sorted_rows:
            if row_num < 1:
                raise UserInputError(
                    f"Row number must be >= 1 in delete_rows, got {row_num}."
                )
            requests.append(
                {
                    "deleteDimension": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": "ROWS",
                            "startIndex": row_num - 1,
                            "endIndex": row_num,
                        }
                    }
                }
            )
        applied_parts.append(f"deleted rows: {', '.join(str(r) for r in delete_rows)}")

    # Build delete row range request (contiguous range, single API call)
    if delete_row_range:
        if isinstance(delete_row_range, str) and ":" in delete_row_range:
            parts = delete_row_range.split(":", 1)
            try:
                range_start = int(parts[0])
                range_end = int(parts[1])
            except ValueError as exc:
                raise UserInputError(
                    f"Invalid delete_row_range format: '{delete_row_range}'. "
                    f"Expected 'start:end' with integer row numbers."
                ) from exc
        else:
            raise UserInputError(
                f"delete_row_range must be a 'start:end' string (e.g. '5:10'), "
                f"got: '{delete_row_range}'."
            )
        if range_start < 1 or range_end < range_start:
            raise UserInputError(
                f"Invalid row range: start={range_start}, end={range_end}. "
                f"Rows are 1-based and end must be >= start."
            )
        requests.append(
            {
                "deleteDimension": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "ROWS",
                        "startIndex": range_start - 1,
                        "endIndex": range_end,
                    }
                }
            }
        )
        num_range_deleted = range_end - range_start + 1
        applied_parts.append(
            f"deleted row range {range_start}-{range_end} ({num_range_deleted} row(s))"
        )

    # Build delete column requests (process in reverse to keep indices stable)
    if delete_columns:
        if not isinstance(delete_columns, list):
            raise UserInputError("delete_columns must be a list of column letters.")
        col_indices = []
        for col_letter in delete_columns:
            col_idx = _column_to_index(str(col_letter).upper())
            if col_idx is None:
                raise UserInputError(
                    f"Invalid column letter in delete_columns: '{col_letter}'."
                )
            col_indices.append((col_letter, col_idx))
        # Sort by index descending to keep indices stable during deletion
        col_indices.sort(key=lambda x: x[1], reverse=True)
        for _, col_idx in col_indices:
            requests.append(
                {
                    "deleteDimension": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": "COLUMNS",
                            "startIndex": col_idx,
                            "endIndex": col_idx + 1,
                        }
                    }
                }
            )
        applied_parts.append(
            f"deleted columns: {', '.join(str(c) for c in delete_columns)}"
        )

    # Execute batch update
    await asyncio.to_thread(
        service.spreadsheets()
        .batchUpdate(spreadsheetId=spreadsheet_id, body={"requests": requests})
        .execute
    )

    return {
        "spreadsheet_id": spreadsheet_id,
        "summary": "; ".join(applied_parts),
    }


@server.tool(
    title="Manage Sheet Tab",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
@handle_http_errors("manage_sheet_tab", service_type="sheets")
@require_google_service("sheets", "sheets_write")
async def manage_sheet_tab(
    service,
    user_google_email: str,
    spreadsheet_id: str,
    sheet_name: str,
    action: str,
    new_name: Optional[str] = None,
    new_index: Optional[int] = None,
    destination_spreadsheet_id: Optional[str] = None,
) -> str:
    """
    Manages the lifecycle of an existing sheet tab: rename, delete, hide, unhide,
    reorder, or copy it into another spreadsheet.

    Use create_sheet to add a tab, and resize_sheet_dimensions for row and column
    level changes. This tool operates on the tab itself.

    Args:
        user_google_email: User's Google email address
        spreadsheet_id: ID of the spreadsheet
        sheet_name: Title of the existing tab to act on
        action: One of "rename", "delete", "hide", "unhide", "reorder", "copy_to"
        new_name: New title, required for action="rename"
        new_index: New zero-based position, required for action="reorder"
        destination_spreadsheet_id: Spreadsheet to copy the tab into, required for
            action="copy_to". Pass the same spreadsheet_id to duplicate within the
            file. The copy is appended as the last tab, titled "Copy of <name>".

    Returns:
        str: Confirmation of the change.
    """
    valid_actions = ("rename", "delete", "hide", "unhide", "reorder", "copy_to")
    action_lower = action.strip().lower() if isinstance(action, str) else ""
    if action_lower not in valid_actions:
        raise UserInputError(
            f"Invalid action '{action}'. Must be one of: {', '.join(valid_actions)}."
        )

    if action_lower == "rename":
        if not new_name or not new_name.strip():
            raise UserInputError("new_name is required for action='rename'.")
        new_name = new_name.strip()
    if action_lower == "reorder":
        if (
            new_index is None
            or isinstance(new_index, bool)
            or not isinstance(new_index, int)
            or new_index < 0
        ):
            raise UserInputError(
                "new_index must be a non-negative integer for action='reorder'."
            )
    if action_lower == "copy_to":
        if not destination_spreadsheet_id or not destination_spreadsheet_id.strip():
            raise UserInputError(
                "destination_spreadsheet_id is required for action='copy_to'."
            )
        destination_spreadsheet_id = destination_spreadsheet_id.strip()

    logger.info(
        f"[manage_sheet_tab] Email: '{user_google_email}', Spreadsheet: {spreadsheet_id}, "
        f"Sheet: '{sheet_name}', Action: {action_lower}"
    )

    spreadsheet = await asyncio.to_thread(
        service.spreadsheets()
        .get(spreadsheetId=spreadsheet_id, fields="sheets.properties")
        .execute
    )
    sheets = spreadsheet.get("sheets", [])
    target_sheet = _select_sheet(sheets, sheet_name)
    sheet_id = target_sheet["properties"]["sheetId"]

    if action_lower == "copy_to":
        copied = await asyncio.to_thread(
            service.spreadsheets()
            .sheets()
            .copyTo(
                spreadsheetId=spreadsheet_id,
                sheetId=sheet_id,
                body={"destinationSpreadsheetId": destination_spreadsheet_id},
            )
            .execute
        )
        text_output = (
            f"Successfully copied sheet '{sheet_name}' (ID: {sheet_id}) from spreadsheet "
            f"{spreadsheet_id} to spreadsheet {destination_spreadsheet_id} as "
            f"'{copied.get('title', 'Copy of ' + sheet_name)}' (ID: {copied.get('sheetId')}, "
            f"index {copied.get('index')}) for {user_google_email}."
        )
        logger.info(text_output)
        return text_output

    if action_lower == "delete":
        if len(sheets) == 1:
            raise UserInputError(
                f"Cannot delete '{sheet_name}': a spreadsheet must keep at least one sheet."
            )
        request = {"deleteSheet": {"sheetId": sheet_id}}
        summary = f"deleted sheet '{sheet_name}'"
    else:
        properties: dict = {"sheetId": sheet_id}
        if action_lower == "rename":
            properties["title"] = new_name
            fields = "title"
            summary = f"renamed sheet '{sheet_name}' to '{new_name}'"
        elif action_lower in ("hide", "unhide"):
            properties["hidden"] = action_lower == "hide"
            fields = "hidden"
            summary = f"{action_lower} sheet '{sheet_name}'"
        else:
            if new_index >= len(sheets):
                raise UserInputError(
                    f"new_index must be less than the number of sheets ({len(sheets)})."
                )
            current_index = target_sheet["properties"].get(
                "index", sheets.index(target_sheet)
            )
            # Sheets interprets indices before removing the tab from its old position.
            properties["index"] = (
                new_index + 1 if new_index > current_index else new_index
            )
            fields = "index"
            summary = f"moved sheet '{sheet_name}' to index {new_index}"
        request = {
            "updateSheetProperties": {"properties": properties, "fields": fields}
        }

    await asyncio.to_thread(
        service.spreadsheets()
        .batchUpdate(spreadsheetId=spreadsheet_id, body={"requests": [request]})
        .execute
    )

    text_output = (
        f"Successfully {summary} (ID: {sheet_id}) in spreadsheet "
        f"{spreadsheet_id} for {user_google_email}."
    )
    logger.info(text_output)
    return text_output


@server.tool(
    title="Resize Sheet Dimensions",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
@handle_http_errors("resize_sheet_dimensions", service_type="sheets")
@require_google_service("sheets", "sheets_write")
async def resize_sheet_dimensions(
    service,
    user_google_email: str,
    spreadsheet_id: str,
    sheet_name: Optional[str] = None,
    column_sizes: Optional[Union[str, dict]] = None,
    row_sizes: Optional[Union[str, dict]] = None,
    auto_resize_columns: Optional[Union[str, List[str]]] = None,
    auto_resize_rows: Optional[Union[str, List[int]]] = None,
    frozen_row_count: Optional[int] = None,
    frozen_column_count: Optional[int] = None,
    hide_columns: Optional[Union[str, List[str]]] = None,
    unhide_columns: Optional[Union[str, List[str]]] = None,
    hide_rows: Optional[Union[str, List[int]]] = None,
    unhide_rows: Optional[Union[str, List[int]]] = None,
    insert_rows: Optional[int] = None,
    insert_rows_at: Optional[int] = None,
    insert_columns: Optional[int] = None,
    insert_columns_at: Optional[str] = None,
    delete_rows: Optional[Union[str, List[int]]] = None,
    delete_row_range: Optional[str] = None,
    delete_columns: Optional[Union[str, List[str]]] = None,
) -> str:
    """
    Manages sheet-level dimension properties: resize columns/rows, auto-resize
    to fit content, freeze rows/columns, hide/unhide rows/columns, and
    insert/delete rows/columns.

    Args:
        user_google_email (str): The user's Google email address. Required.
        spreadsheet_id (str): The ID of the spreadsheet. Required.
        sheet_name (Optional[str]): Sheet name to target. Defaults to the
            first sheet if not provided.
        column_sizes (Optional[Union[str, dict]]): Dict mapping column letters
            to pixel widths. Example: {"A": 200, "C": 300}. Can be a JSON
            string or Python dict.
        row_sizes (Optional[Union[str, dict]]): Dict mapping 1-based row
            numbers to pixel heights. Example: {"1": 40, "3": 60}. Can be
            a JSON string or Python dict.
        auto_resize_columns (Optional[Union[str, List[str]]]): List of column
            letters to auto-resize to fit content. Example: ["A", "B"].
        auto_resize_rows (Optional[Union[str, List[int]]]): List of 1-based
            row numbers to auto-resize to fit content. Example: [1, 2].
        frozen_row_count (Optional[int]): Number of rows to freeze from the
            top. Use 0 to unfreeze all rows.
        frozen_column_count (Optional[int]): Number of columns to freeze from
            the left. Use 0 to unfreeze all columns.
        hide_columns (Optional[Union[str, List[str]]]): List of column letters
            to hide. Example: ["C", "D"].
        unhide_columns (Optional[Union[str, List[str]]]): List of column
            letters to unhide. Example: ["C", "D"].
        hide_rows (Optional[Union[str, List[int]]]): List of 1-based row
            numbers to hide. Example: [3, 4].
        unhide_rows (Optional[Union[str, List[int]]]): List of 1-based row
            numbers to unhide. Example: [3, 4].
        insert_rows (Optional[int]): Number of rows to insert.
        insert_rows_at (Optional[int]): 1-based row number to insert before.
            Appends to the end of the sheet if omitted.
        insert_columns (Optional[int]): Number of columns to insert.
        insert_columns_at (Optional[str]): Column letter to insert before
            (e.g. "C"). Appends to the end if omitted.
        delete_rows (Optional[Union[str, List[int]]]): List of 1-based row
            numbers to delete. Example: [5, 6]. Best for non-contiguous rows.
        delete_row_range (Optional[str]): Contiguous range of rows to delete,
            as "start:end" (1-based, inclusive). Example: "5:10" deletes rows
            5 through 10. More efficient than delete_rows for large contiguous
            ranges.
        delete_columns (Optional[Union[str, List[str]]]): List of column
            letters to delete. Example: ["E", "F"].

    Returns:
        str: Confirmation of the applied dimension changes.
    """
    logger.info(
        "[resize_sheet_dimensions] Invoked. Email: '%s', Spreadsheet: %s",
        user_google_email,
        spreadsheet_id,
    )

    result = await _resize_sheet_dimensions_impl(
        service=service,
        spreadsheet_id=spreadsheet_id,
        sheet_name=sheet_name,
        column_sizes=column_sizes,
        row_sizes=row_sizes,
        auto_resize_columns=auto_resize_columns,
        auto_resize_rows=auto_resize_rows,
        frozen_row_count=frozen_row_count,
        frozen_column_count=frozen_column_count,
        hide_columns=hide_columns,
        unhide_columns=unhide_columns,
        hide_rows=hide_rows,
        unhide_rows=unhide_rows,
        insert_rows=insert_rows,
        insert_rows_at=insert_rows_at,
        insert_columns=insert_columns,
        insert_columns_at=insert_columns_at,
        delete_rows=delete_rows,
        delete_row_range=delete_row_range,
        delete_columns=delete_columns,
    )

    return (
        f"Applied dimension changes in spreadsheet {result['spreadsheet_id']} "
        f"for {user_google_email}: {result['summary']}."
    )


@server.tool(
    title="Move Sheet Rows",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
@handle_http_errors("move_sheet_rows", service_type="sheets")
@require_google_service("sheets", "sheets_write")
async def move_sheet_rows(
    service,
    user_google_email: str,
    spreadsheet_id: str,
    source_sheet: str,
    start_row: int,
    end_row: int,
    destination_sheet: str,
) -> str:
    """
    Moves rows from one sheet to another within the same spreadsheet. The move
    is performed in a single batchUpdate (copyPaste followed by
    deleteDimension). Note: batchUpdate executes requests sequentially but does
    not roll back on partial failure — if the copy succeeds but the delete
    fails, rows may be duplicated. Formulas, data types, and formatting are
    preserved (unlike a values.get/append round-trip).
    Row numbers are 1-based (matching the spreadsheet UI).

    Args:
        user_google_email (str): The user's Google email address. Required.
        spreadsheet_id (str): The ID of the spreadsheet. Required.
        source_sheet (str): Name of the sheet to move rows from. Required.
        start_row (int): First row to move (1-based, inclusive). Required.
        end_row (int): Last row to move (1-based, inclusive). Required.
        destination_sheet (str): Name of the sheet to move rows to. Required.

    Returns:
        str: Confirmation message with the number of rows moved.
    """
    logger.info(
        f"[move_sheet_rows] Invoked. Email: '{user_google_email}', "
        f"Spreadsheet: {spreadsheet_id}, "
        f"From: {source_sheet}!{start_row}-{end_row}, To: {destination_sheet}"
    )

    if start_row < 1 or end_row < start_row:
        raise UserInputError(
            f"Invalid row range: start_row={start_row}, end_row={end_row}. "
            f"Rows are 1-based and end_row must be >= start_row."
        )

    if source_sheet == destination_sheet:
        raise UserInputError("source_sheet and destination_sheet must be different.")

    spreadsheet = await asyncio.to_thread(
        service.spreadsheets()
        .get(
            spreadsheetId=spreadsheet_id,
            fields="sheets(properties(sheetId,title,gridProperties))",
        )
        .execute
    )
    sheets = spreadsheet.get("sheets", [])
    src = _select_sheet(sheets, source_sheet)
    dst = _select_sheet(sheets, destination_sheet)
    src_id = src["properties"]["sheetId"]
    dst_id = dst["properties"]["sheetId"]
    dst_grid_rows = dst["properties"].get("gridProperties", {}).get("rowCount", 0)

    # Validate that the source row block actually contains data.
    safe_source = source_sheet.replace("'", "''")
    src_range = f"'{safe_source}'!{start_row}:{end_row}"
    src_values = await asyncio.to_thread(
        service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=src_range)
        .execute
    )
    if not src_values.get("values"):
        raise UserInputError(
            f"Source range '{source_sheet}' rows {start_row}-{end_row} "
            f"contains no data. Nothing to move."
        )

    # Find the last row with actual data in the destination sheet.
    # gridProperties.rowCount is the allocated grid size (e.g. 1000 for a new
    # sheet), not the count of rows containing data.  Fetch all columns so the
    # append position reflects any non-empty cell, not just column A.
    safe_destination = destination_sheet.replace("'", "''")
    dst_values = await asyncio.to_thread(
        service.spreadsheets()
        .values()
        .get(
            spreadsheetId=spreadsheet_id,
            range=f"'{safe_destination}'",
            majorDimension="ROWS",
        )
        .execute
    )
    dst_data_rows = len(dst_values.get("values", []))

    num_rows = end_row - start_row + 1
    paste_start = dst_data_rows

    # If pasting beyond the current grid, expand the destination sheet first.
    requests = []
    if paste_start + num_rows > dst_grid_rows:
        requests.append(
            {
                "appendDimension": {
                    "sheetId": dst_id,
                    "dimension": "ROWS",
                    "length": (paste_start + num_rows) - dst_grid_rows,
                }
            }
        )

    requests.extend(
        [
            {
                "copyPaste": {
                    "source": {
                        "sheetId": src_id,
                        "startRowIndex": start_row - 1,
                        "endRowIndex": end_row,
                    },
                    "destination": {
                        "sheetId": dst_id,
                        "startRowIndex": paste_start,
                        "endRowIndex": paste_start + num_rows,
                    },
                    "pasteType": "PASTE_NORMAL",
                }
            },
            {
                "deleteDimension": {
                    "range": {
                        "sheetId": src_id,
                        "dimension": "ROWS",
                        "startIndex": start_row - 1,
                        "endIndex": end_row,
                    }
                }
            },
        ]
    )

    await asyncio.to_thread(
        service.spreadsheets()
        .batchUpdate(spreadsheetId=spreadsheet_id, body={"requests": requests})
        .execute
    )

    text_output = (
        f"Successfully moved {num_rows} row(s) from '{source_sheet}' "
        f"(rows {start_row}-{end_row}) to '{destination_sheet}' "
        f"in spreadsheet {spreadsheet_id} for {user_google_email}."
    )

    logger.info(f"[move_sheet_rows] Moved {num_rows} rows for {user_google_email}")
    return text_output


async def _manage_named_range_impl(
    service,
    user_google_email: str,
    spreadsheet_id: str,
    action: str,
    name: Optional[str] = None,
    range_name: Optional[str] = None,
    named_range_id: Optional[str] = None,
    new_name: Optional[str] = None,
    new_range: Optional[str] = None,
) -> str:
    """Internal implementation of manage_named_range."""
    valid_actions = ("list", "create", "update", "delete")
    action_lower = action.strip().lower() if isinstance(action, str) else ""
    if action_lower not in valid_actions:
        raise UserInputError(
            f"Invalid action '{action}'. Must be one of: {', '.join(valid_actions)}."
        )

    if action_lower == "list":
        spreadsheet = await asyncio.to_thread(
            service.spreadsheets()
            .get(
                spreadsheetId=spreadsheet_id,
                fields="sheets(properties(sheetId,title)),namedRanges(namedRangeId,name,range)",
            )
            .execute
        )
        sheet_titles = {
            sheet["properties"]["sheetId"]: sheet["properties"].get(
                "title", f"Sheet {sheet['properties']['sheetId']}"
            )
            for sheet in spreadsheet.get("sheets", [])
            if "properties" in sheet and "sheetId" in sheet["properties"]
        }
        named_ranges = spreadsheet.get("namedRanges", [])
        return _format_named_ranges_list(
            named_ranges=named_ranges,
            sheet_titles=sheet_titles,
            spreadsheet_id=spreadsheet_id,
            user_google_email=user_google_email,
        )

    if action_lower == "create":
        if not name or not name.strip():
            raise UserInputError("name is required for action='create'.")
        if not range_name or not range_name.strip():
            raise UserInputError("range_name is required for action='create'.")

        name_clean = name.strip()
        range_clean = range_name.strip()

        spreadsheet = await asyncio.to_thread(
            service.spreadsheets()
            .get(
                spreadsheetId=spreadsheet_id,
                fields="sheets(properties(sheetId,title))",
            )
            .execute
        )
        sheets = spreadsheet.get("sheets", [])
        grid_range = _parse_a1_range(range_clean, sheets)

        body = {
            "requests": [
                {
                    "addNamedRange": {
                        "namedRange": {
                            "name": name_clean,
                            "range": grid_range,
                        }
                    }
                }
            ]
        }
        response = await asyncio.to_thread(
            service.spreadsheets()
            .batchUpdate(spreadsheetId=spreadsheet_id, body=body)
            .execute
        )

        created_nr = (
            (response.get("replies") or [{}])[0]
            .get("addNamedRange", {})
            .get("namedRange", {})
        )
        created_id = created_nr.get("namedRangeId", "")
        id_info = f" (ID: {created_id})" if created_id else ""

        return (
            f"Successfully created named range '{name_clean}'{id_info} covering '{range_clean}' "
            f"in spreadsheet {spreadsheet_id} for {user_google_email}."
        )

    if action_lower == "update":
        if not (new_name and new_name.strip()) and not (
            new_range and new_range.strip()
        ):
            raise UserInputError(
                "At least one of 'new_name' or 'new_range' must be provided for action='update'."
            )
        if not (named_range_id and named_range_id.strip()) and not (
            name and name.strip()
        ):
            raise UserInputError(
                "Either 'named_range_id' or 'name' is required to identify the named range to update."
            )

        spreadsheet = await asyncio.to_thread(
            service.spreadsheets()
            .get(
                spreadsheetId=spreadsheet_id,
                fields="sheets(properties(sheetId,title)),namedRanges(namedRangeId,name,range)",
            )
            .execute
        )
        sheets = spreadsheet.get("sheets", [])
        named_ranges = spreadsheet.get("namedRanges", [])

        target_nr = _find_named_range(
            named_ranges, target_id=named_range_id, target_name=name
        )
        if not target_nr:
            identifier = (
                f"ID '{named_range_id}'" if named_range_id else f"name '{name}'"
            )
            raise UserInputError(
                f"Named range with {identifier} not found in spreadsheet {spreadsheet_id}."
            )

        resolved_id = target_nr.get("namedRangeId")
        existing_name = target_nr.get("name", "")

        update_payload: dict = {"namedRangeId": resolved_id}
        fields = []
        applied_desc = []

        if new_name and new_name.strip():
            new_name_clean = new_name.strip()
            if new_name_clean != existing_name:
                update_payload["name"] = new_name_clean
                fields.append("name")
                applied_desc.append(
                    f"renamed from '{existing_name}' to '{new_name_clean}'"
                )

        if new_range and new_range.strip():
            new_range_clean = new_range.strip()
            new_grid_range = _parse_a1_range(new_range_clean, sheets)
            update_payload["range"] = new_grid_range
            fields.append("range")
            applied_desc.append(f"range updated to '{new_range_clean}'")

        if not fields:
            raise UserInputError("No changes to apply to the named range.")

        body = {
            "requests": [
                {
                    "updateNamedRange": {
                        "namedRange": update_payload,
                        "fields": ",".join(fields),
                    }
                }
            ]
        }
        await asyncio.to_thread(
            service.spreadsheets()
            .batchUpdate(spreadsheetId=spreadsheet_id, body=body)
            .execute
        )

        return (
            f"Successfully updated named range (ID: {resolved_id}) in spreadsheet {spreadsheet_id} "
            f"for {user_google_email}: {', '.join(applied_desc)}."
        )

    if action_lower == "delete":
        if not (named_range_id and named_range_id.strip()) and not (
            name and name.strip()
        ):
            raise UserInputError(
                "Either 'named_range_id' or 'name' is required to identify the named range to delete."
            )

        resolved_id = named_range_id.strip() if named_range_id else None
        deleted_name = name.strip() if name else None

        if not resolved_id:
            spreadsheet = await asyncio.to_thread(
                service.spreadsheets()
                .get(
                    spreadsheetId=spreadsheet_id,
                    fields="namedRanges(namedRangeId,name)",
                )
                .execute
            )
            named_ranges = spreadsheet.get("namedRanges", [])
            target_nr = _find_named_range(named_ranges, target_name=name)
            if not target_nr:
                raise UserInputError(
                    f"Named range with name '{name}' not found in spreadsheet {spreadsheet_id}."
                )
            resolved_id = target_nr.get("namedRangeId")
            deleted_name = target_nr.get("name", name)

        body = {
            "requests": [
                {
                    "deleteNamedRange": {
                        "namedRangeId": resolved_id,
                    }
                }
            ]
        }
        await asyncio.to_thread(
            service.spreadsheets()
            .batchUpdate(spreadsheetId=spreadsheet_id, body=body)
            .execute
        )

        name_info = f"'{deleted_name}' " if deleted_name else ""
        return (
            f"Successfully deleted named range {name_info}(ID: {resolved_id}) "
            f"from spreadsheet {spreadsheet_id} for {user_google_email}."
        )

    raise UserInputError(f"Unhandled action: {action}")


@server.tool(
    title="Manage Named Range",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
@handle_http_errors("manage_named_range", service_type="sheets")
@require_google_service("sheets", "sheets_write")
async def manage_named_range(
    service,
    user_google_email: str,
    spreadsheet_id: str,
    action: str,
    name: Optional[str] = None,
    range_name: Optional[str] = None,
    named_range_id: Optional[str] = None,
    new_name: Optional[str] = None,
    new_range: Optional[str] = None,
) -> str:
    """
    Manages the lifecycle of named ranges in a Google Sheet: list, create, update, or delete.

    Args:
        user_google_email: The user's Google email address. Required.
        spreadsheet_id: The ID of the spreadsheet. Required.
        action: The operation to perform: "list", "create", "update", or "delete". Required.
        name: Name for the named range (required for "create"; optional identifier for "update"/"delete").
        range_name: Target cell or range in A1 notation (e.g., "Sheet1!A1:D10", "A1:B5") (required for "create").
        named_range_id: The ID of the named range (optional identifier for "update"/"delete").
        new_name: New name for the named range (used with action="update").
        new_range: New A1-style range for the named range (used with action="update").

    Returns:
        str: Confirmation message or formatted list of named ranges.
    """
    logger.info(
        "[manage_named_range] Invoked. Email: '%s', Spreadsheet: %s, Action: %s",
        user_google_email,
        spreadsheet_id,
        action,
    )
    return await _manage_named_range_impl(
        service=service,
        user_google_email=user_google_email,
        spreadsheet_id=spreadsheet_id,
        action=action,
        name=name,
        range_name=range_name,
        named_range_id=named_range_id,
        new_name=new_name,
        new_range=new_range,
    )


# Create comment management tools for sheets
_comment_tools = create_comment_tools(
    "spreadsheet", "spreadsheet_id", insert_cell_comment=_insert_cell_comment
)

# Extract and register the functions
list_spreadsheet_comments = _comment_tools["list_comments"]
manage_spreadsheet_comment = _comment_tools["manage_comment"]


@server.tool(
    title="Batch Update Sheet",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
@handle_http_errors("batch_update_sheet", service_type="sheets")
@require_google_service("sheets", "sheets_write")
async def batch_update_sheet(
    service,
    user_google_email: str,
    spreadsheet_id: str,
    requests: List[Dict[str, Any]],
) -> str:
    """
    Apply raw Sheets API batchUpdate requests to a spreadsheet.

    This is the escape hatch for anything the typed Sheets tools do not cover
    (banding, slicers, developer metadata, dimension groups, copy/paste, ...).
    Each request is passed through to spreadsheets.batchUpdate unchanged, so it
    must follow the Sheets API v4 Request schema exactly: a single key naming
    the request type, whose value is that request's body. Sheet IDs, not
    titles, identify sheets; get_spreadsheet_info lists them.

    Args:
        user_google_email (str): The user's Google email address. Required.
        spreadsheet_id (str): The ID of the spreadsheet to update. Required.
        requests (List[Dict[str, Any]]): Requests to apply in order, e.g.
            [{"addSheet": {"properties": {"title": "Q3"}}},
             {"deleteDimension": {"range": {"sheetId": 0, "dimension": "ROWS",
             "startIndex": 5, "endIndex": 10}}}]. Any of the 74 batchUpdate
            request types is accepted; unknown keys are rejected before the call.

    Returns:
        str: Per-request results, including IDs of anything created.
    """
    logger.info(
        f"[batch_update_sheet] Invoked. Email: '{user_google_email}', Spreadsheet: {spreadsheet_id}, Requests: {len(requests) if isinstance(requests, list) else '?'}"
    )

    validated = _validate_batch_update_requests(requests)

    result = await asyncio.to_thread(
        service.spreadsheets()
        .batchUpdate(spreadsheetId=spreadsheet_id, body={"requests": validated})
        .execute
    )
    replies = result.get("replies", []) or []

    lines = [
        f"Batch Update Completed for {user_google_email}:",
        f"- Spreadsheet ID: {spreadsheet_id}",
        f"- URL: https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit",
        f"- Requests Applied: {len(validated)}",
        f"- Replies Received: {len(replies)}",
        "",
        "Update Results:",
    ]
    lines.extend(_summarize_batch_update_replies(validated, replies))
    logger.info(f"Batch update completed successfully for {user_google_email}")
    return "\n".join(lines)


@server.tool(
    title="Manage Sheet Filter",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
@handle_http_errors("manage_sheet_filter", service_type="sheets")
@require_google_service("sheets", "sheets_write")
async def manage_sheet_filter(
    service,
    user_google_email: str,
    spreadsheet_id: str,
    action: str,
    sheet_name: Optional[str] = None,
    range_name: Optional[str] = None,
    title: Optional[str] = None,
    filter_view_id: Optional[int] = None,
    sort_specs: Optional[List[Dict[str, Any]]] = None,
    filter_specs: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """
    Manage a sheet's basic filter and its saved filter views.

    Args:
        user_google_email (str): The user's Google email address. Required.
        spreadsheet_id (str): The ID of the spreadsheet. Required.
        action (str): One of:
            - "set_basic": set the sheet's basic filter on range_name (replaces any
              existing basic filter) with optional sort_specs / filter_specs.
            - "clear_basic": remove the basic filter from sheet_name.
            - "list_views": list the basic filter and filter views of every sheet.
            - "create_view": create a named filter view (title, range_name, specs).
            - "update_view": update the filter view filter_view_id; only the
              supplied title / range_name / sort_specs / filter_specs change.
            - "delete_view": delete the filter view filter_view_id.
        sheet_name (Optional[str]): Sheet to act on; defaults to the first sheet.
            A sheet prefix inside range_name takes precedence.
        range_name (Optional[str]): A1 range the filter covers, e.g. "A1:F200".
            Required for set_basic and create_view. Omitted sheet uses sheet_name.
        title (Optional[str]): Filter view title (create_view requires it).
        filter_view_id (Optional[int]): Target view for update_view / delete_view.
        sort_specs (Optional[List[Dict]]): Sort order, applied in sequence, e.g.
            [{"column": "C", "order": "DESCENDING"}, {"column": "A"}]. Columns are
            sheet columns as letters or zero-based indexes; order defaults to
            ASCENDING.
        filter_specs (Optional[List[Dict]]): Per-column criteria, e.g.
            [{"column": "B", "hidden_values": ["Closed", "Lost"]},
             {"column": "D", "condition_type": "NUMBER_GREATER", "condition_values": [1000]}].
            condition_type accepts any Sheets BooleanCondition type (NUMBER_*,
            TEXT_*, DATE_*, BLANK, NOT_BLANK, CUSTOM_FORMULA, ...).

    Returns:
        str: Confirmation, or the filter listing for action="list_views".
    """
    valid_actions = (
        "set_basic",
        "clear_basic",
        "list_views",
        "create_view",
        "update_view",
        "delete_view",
    )
    action_lower = action.strip().lower() if isinstance(action, str) else ""
    if action_lower not in valid_actions:
        raise UserInputError(
            f"Invalid action '{action}'. Must be one of: {', '.join(valid_actions)}."
        )
    logger.info(
        f"[manage_sheet_filter] Email: '{user_google_email}', Spreadsheet: {spreadsheet_id}, Action: {action_lower}"
    )

    if action_lower == "list_views":
        sheets = await _fetch_sheets(
            service,
            spreadsheet_id,
            fields="sheets(properties(sheetId,title),basicFilter,filterViews)",
        )
        return (
            f"Filters in spreadsheet {spreadsheet_id} for {user_google_email}:\n"
            + _format_filter_views_list(sheets, _sheet_titles_by_id(sheets))
        )

    if action_lower in ("update_view", "delete_view"):
        if filter_view_id is None or isinstance(filter_view_id, bool):
            raise UserInputError(
                f"filter_view_id is required for action='{action_lower}'."
            )
    if action_lower in ("set_basic", "create_view") and not range_name:
        raise UserInputError(f"range_name is required for action='{action_lower}'.")
    if action_lower == "create_view" and not (title and title.strip()):
        raise UserInputError("title is required for action='create_view'.")

    if action_lower == "delete_view":
        request = {"deleteFilterView": {"filterId": filter_view_id}}
        summary = f"deleted filter view {filter_view_id}"
    else:
        sheets = await _fetch_sheets(service, spreadsheet_id)
        if action_lower == "clear_basic":
            sheet = _select_sheet(sheets, sheet_name)
            request = {"clearBasicFilter": {"sheetId": sheet["properties"]["sheetId"]}}
            summary = f"cleared the basic filter on sheet '{sheet['properties'].get('title')}'"
        else:
            if action_lower == "update_view" and not range_name:
                # Only the supplied fields change; fetch the view's range so column
                # bounds for sort/filter specs are validated against it.
                views_sheets = await _fetch_sheets(
                    service,
                    spreadsheet_id,
                    fields="sheets(properties(sheetId,title),filterViews(filterViewId,range))",
                )
                existing = next(
                    (
                        v
                        for sh in views_sheets
                        for v in (sh.get("filterViews") or [])
                        if v.get("filterViewId") == filter_view_id
                    ),
                    None,
                )
                if existing is None:
                    raise UserInputError(
                        f"No filter view with filter_view_id {filter_view_id} in this spreadsheet."
                    )
                grid_range = existing.get("range", {})
            else:
                grid_range = _resolve_scope_grid_range(range_name, sheet_name, sheets)

            body, fields = _build_filter_body(
                grid_range,
                sort_specs,
                filter_specs,
                title=title.strip() if title else None,
                filter_view_id=filter_view_id
                if action_lower == "update_view"
                else None,
            )
            if action_lower == "set_basic":
                body.pop("title", None)
                request = {"setBasicFilter": {"filter": body}}
                summary = "set the basic filter"
            elif action_lower == "create_view":
                request = {"addFilterView": {"filter": body}}
                summary = f"created filter view '{title.strip()}'"
            else:
                if not range_name:
                    body.pop("range", None)
                    fields.remove("range")
                if len(fields) == 0:
                    raise UserInputError(
                        "action='update_view' needs at least one of title, range_name, "
                        "sort_specs or filter_specs."
                    )
                request = {
                    "updateFilterView": {"filter": body, "fields": ",".join(fields)}
                }
                summary = f"updated filter view {filter_view_id}"

    result = await asyncio.to_thread(
        service.spreadsheets()
        .batchUpdate(spreadsheetId=spreadsheet_id, body={"requests": [request]})
        .execute
    )
    if action_lower == "create_view":
        replies = result.get("replies", []) or [{}]
        created = (replies[0].get("addFilterView") or {}).get("filter", {})
        summary += f" (ID: {created.get('filterViewId')})"

    text_output = f"Successfully {summary} in spreadsheet {spreadsheet_id} for {user_google_email}."
    logger.info(text_output)
    return text_output


@server.tool(
    title="Manage Sheet Protection",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
@handle_http_errors("manage_sheet_protection", service_type="sheets")
@require_google_service("sheets", "sheets_write")
async def manage_sheet_protection(
    service,
    user_google_email: str,
    spreadsheet_id: str,
    action: str,
    sheet_name: Optional[str] = None,
    range_name: Optional[str] = None,
    protected_range_id: Optional[int] = None,
    description: Optional[str] = None,
    warning_only: Optional[bool] = None,
    editors: Optional[List[str]] = None,
    editor_groups: Optional[List[str]] = None,
    domain_users_can_edit: Optional[bool] = None,
    unprotected_ranges: Optional[List[str]] = None,
) -> str:
    """
    List, add, update or delete protected ranges (and protected sheets).

    Args:
        user_google_email (str): The user's Google email address. Required.
        spreadsheet_id (str): The ID of the spreadsheet. Required.
        action (str): "list", "add", "update" or "delete".
        sheet_name (Optional[str]): Sheet to protect, or the sheet range_name
            refers to; defaults to the first sheet. For "add" without range_name
            the whole sheet is protected.
        range_name (Optional[str]): A1 range to protect, e.g. "A1:D20" or
            "Totals!A1:D20".
        protected_range_id (Optional[int]): Target for "update" / "delete"
            (see "list").
        description (Optional[str]): Label shown in the Sheets UI.
        warning_only (Optional[bool]): True shows a warning but lets anyone edit;
            False (default on add) restricts edits to the editors below.
        editors (Optional[List[str]]): Emails of users allowed to edit. The
            spreadsheet owner and the caller can always edit.
        editor_groups (Optional[List[str]]): Emails of groups allowed to edit.
        domain_users_can_edit (Optional[bool]): Allow anyone in the domain to edit.
        unprotected_ranges (Optional[List[str]]): A1 ranges inside the protected
            area that stay editable (only valid when protecting a whole sheet).

    Returns:
        str: Confirmation with the protected range ID, or the listing for "list".
    """
    valid_actions = ("list", "add", "update", "delete")
    action_lower = action.strip().lower() if isinstance(action, str) else ""
    if action_lower not in valid_actions:
        raise UserInputError(
            f"Invalid action '{action}'. Must be one of: {', '.join(valid_actions)}."
        )
    logger.info(
        f"[manage_sheet_protection] Email: '{user_google_email}', Spreadsheet: {spreadsheet_id}, Action: {action_lower}"
    )

    if action_lower == "list":
        sheets = await _fetch_sheets(
            service,
            spreadsheet_id,
            fields="sheets(properties(sheetId,title),protectedRanges)",
        )
        return (
            f"Protected ranges in spreadsheet {spreadsheet_id} for {user_google_email}:\n"
            + _format_protected_ranges_list(sheets, _sheet_titles_by_id(sheets))
        )

    if action_lower in ("update", "delete") and (
        protected_range_id is None or isinstance(protected_range_id, bool)
    ):
        raise UserInputError(
            f"protected_range_id is required for action='{action_lower}'."
        )

    if action_lower == "delete":
        request = {"deleteProtectedRange": {"protectedRangeId": protected_range_id}}
        summary = f"deleted protected range {protected_range_id}"
    else:
        sheets = await _fetch_sheets(service, spreadsheet_id)
        grid_range = None
        if action_lower == "add" or range_name or sheet_name:
            grid_range = _resolve_scope_grid_range(range_name, sheet_name, sheets)
        unprotected = None
        if unprotected_ranges is not None:
            unprotected = [
                _resolve_scope_grid_range(str(r), sheet_name, sheets)
                for r in _parse_json_list(unprotected_ranges, "unprotected_ranges")
            ]
        if action_lower == "add" and warning_only is None:
            warning_only = False
        body, fields = _build_protected_range(
            grid_range,
            description,
            warning_only,
            editors,
            editor_groups,
            domain_users_can_edit,
            unprotected,
            protected_range_id=protected_range_id if action_lower == "update" else None,
        )
        if action_lower == "add":
            request = {"addProtectedRange": {"protectedRange": body}}
            summary = "protected " + (
                f"range '{range_name}'"
                if range_name
                else f"sheet '{sheet_name or sheets[0]['properties'].get('title')}'"
            )
        else:
            if not fields:
                raise UserInputError(
                    "action='update' needs at least one of range_name, description, "
                    "warning_only, editors, editor_groups, domain_users_can_edit or "
                    "unprotected_ranges."
                )
            request = {
                "updateProtectedRange": {
                    "protectedRange": body,
                    "fields": ",".join(fields),
                }
            }
            summary = (
                f"updated protected range {protected_range_id} ({', '.join(fields)})"
            )

    result = await asyncio.to_thread(
        service.spreadsheets()
        .batchUpdate(spreadsheetId=spreadsheet_id, body={"requests": [request]})
        .execute
    )
    if action_lower == "add":
        replies = result.get("replies", []) or [{}]
        created = (replies[0].get("addProtectedRange") or {}).get("protectedRange", {})
        mode = "warning only" if warning_only else "restricted"
        summary += f" [{mode}] (ID: {created.get('protectedRangeId')})"

    text_output = f"Successfully {summary} in spreadsheet {spreadsheet_id} for {user_google_email}."
    logger.info(text_output)
    return text_output


@server.tool(
    title="Manage Sheet Validation",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@handle_http_errors("manage_sheet_validation", service_type="sheets")
@require_google_service("sheets", "sheets_write")
async def manage_sheet_validation(
    service,
    user_google_email: str,
    spreadsheet_id: str,
    range_name: str,
    action: str = "set",
    rule_type: Optional[str] = None,
    values: Optional[Union[str, List[Union[str, int, float]]]] = None,
    condition_type: Optional[str] = None,
    strict: bool = True,
    show_dropdown: bool = True,
    input_message: Optional[str] = None,
) -> str:
    """
    Set or clear data validation on a range (dropdowns, checkboxes, number/date/text rules).

    Args:
        user_google_email (str): The user's Google email address. Required.
        spreadsheet_id (str): The ID of the spreadsheet. Required.
        range_name (str): A1 range to validate, e.g. "Sheet1!C2:C100". Required.
        action (str): "set" (default) applies a rule; "clear" removes validation.
        rule_type (Optional[str]): Required for "set". One of:
            - "list": dropdown of `values` (e.g. ["Open", "Closed"]).
            - "range": dropdown from a range; values=["=Lists!A2:A20"].
            - "checkbox": tick box; optional values=[checked, unchecked].
            - "number": condition_type NUMBER_GREATER, NUMBER_LESS, NUMBER_EQ,
              NUMBER_BETWEEN, ... with the operand(s) in values.
            - "date": condition_type DATE_AFTER, DATE_BEFORE, DATE_BETWEEN,
              DATE_IS_VALID, ... values take dates ("2026-01-31") or TODAY,
              YESTERDAY, TOMORROW, PAST_WEEK, PAST_MONTH, PAST_YEAR.
            - "text": condition_type TEXT_CONTAINS, TEXT_EQ, TEXT_IS_EMAIL,
              TEXT_IS_URL, ... with the operand in values.
            - "custom_formula": values=["=A1>0"].
        values (Optional[List]): Rule operands as described above; a JSON string
            is accepted.
        condition_type (Optional[str]): Comparison for number/date/text rules.
        strict (bool): Reject invalid input (True, default) or only warn (False).
        show_dropdown (bool): For list/range rules, show the in-cell dropdown
            arrow. Defaults to True.
        input_message (Optional[str]): Help text shown when the cell is selected.

    Returns:
        str: Confirmation of the rule set or cleared.
    """
    action_lower = action.strip().lower() if isinstance(action, str) else ""
    if action_lower not in ("set", "clear"):
        raise UserInputError(f"Invalid action '{action}'. Must be 'set' or 'clear'.")
    if not range_name or not range_name.strip():
        raise UserInputError("range_name is required.")
    logger.info(
        f"[manage_sheet_validation] Email: '{user_google_email}', Spreadsheet: {spreadsheet_id}, Range: {range_name}, Action: {action_lower}"
    )

    request_body: dict = {}
    if action_lower == "set":
        request_body["rule"] = _build_data_validation_rule(
            rule_type, values, condition_type, strict, show_dropdown, input_message
        )

    sheets = await _fetch_sheets(service, spreadsheet_id)
    request_body["range"] = _parse_a1_range(range_name, sheets)

    await asyncio.to_thread(
        service.spreadsheets()
        .batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [{"setDataValidation": request_body}]},
        )
        .execute
    )

    if action_lower == "set":
        condition = request_body["rule"]["condition"]
        summary = (
            f"set {rule_type.strip().lower()} validation ({condition['type']}"
            + (
                f": {', '.join(v.get('userEnteredValue') or v.get('relativeDate') for v in condition['values'])}"
                if condition.get("values")
                else ""
            )
            + f", {'strict' if strict else 'warning only'})"
        )
    else:
        summary = "cleared data validation"
    text_output = (
        f"Successfully {summary} on range '{range_name}' in spreadsheet {spreadsheet_id} "
        f"for {user_google_email}."
    )
    logger.info(text_output)
    return text_output


@server.tool(
    title="Manage Sheet Chart",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
@handle_http_errors("manage_sheet_chart", service_type="sheets")
@require_google_service("sheets", "sheets_write")
async def manage_sheet_chart(
    service,
    user_google_email: str,
    spreadsheet_id: str,
    action: str,
    chart_id: Optional[int] = None,
    chart_type: Optional[str] = None,
    title: Optional[str] = None,
    subtitle: Optional[str] = None,
    domain_range: Optional[str] = None,
    series_ranges: Optional[List[str]] = None,
    anchor_cell: Optional[str] = None,
    width: Optional[int] = None,
    height: Optional[int] = None,
    legend_position: Optional[str] = None,
    header_count: Optional[int] = None,
) -> str:
    """
    List, add, update, move/resize or delete embedded charts.

    Args:
        user_google_email (str): The user's Google email address. Required.
        spreadsheet_id (str): The ID of the spreadsheet. Required.
        action (str): One of:
            - "list": every chart with its ID, title, type and placement.
            - "add": create a chart from domain_range + series_ranges, placed at
              anchor_cell (or on its own new sheet when anchor_cell is omitted).
            - "update": change title / subtitle / chart_type / legend_position of
              chart_id. chart_type can switch between the basic types only.
            - "move": reposition and/or resize chart_id (anchor_cell, width, height).
            - "delete": remove chart_id.
        chart_id (Optional[int]): Target chart for update / move / delete.
        chart_type (Optional[str]): BAR, COLUMN, LINE, AREA, SCATTER or PIE.
            Required for "add".
        title (Optional[str]): Chart title.
        subtitle (Optional[str]): Chart subtitle.
        domain_range (Optional[str]): A1 range of the categories / x-axis labels,
            e.g. "Data!A1:A13" (include the header row and set header_count=1).
            Required for "add".
        series_ranges (Optional[List[str]]): One A1 range per data series, e.g.
            ["Data!B1:B13", "Data!C1:C13"]. PIE takes exactly one. Required for "add".
        anchor_cell (Optional[str]): Top-left cell the chart floats over, e.g.
            "Data!E2". For "add" omitting it puts the chart on a new sheet.
        width (Optional[int]): Chart width in pixels (default 600 on add).
        height (Optional[int]): Chart height in pixels (default 371 on add).
        legend_position (Optional[str]): BOTTOM, TOP, LEFT, RIGHT, NONE, or
            LABELED (pie only).
        header_count (Optional[int]): Rows of the ranges that are headers (series
            names). Sheets guesses when omitted.

    Returns:
        str: Confirmation with the chart ID, or the chart listing for "list".
    """
    valid_actions = ("list", "add", "update", "move", "delete")
    action_lower = action.strip().lower() if isinstance(action, str) else ""
    if action_lower not in valid_actions:
        raise UserInputError(
            f"Invalid action '{action}'. Must be one of: {', '.join(valid_actions)}."
        )
    logger.info(
        f"[manage_sheet_chart] Email: '{user_google_email}', Spreadsheet: {spreadsheet_id}, Action: {action_lower}"
    )

    if action_lower == "list":
        sheets = await _fetch_sheets(
            service, spreadsheet_id, fields="sheets(properties(sheetId,title),charts)"
        )
        return (
            f"Charts in spreadsheet {spreadsheet_id} for {user_google_email}:\n"
            + _format_charts_list(sheets, _sheet_titles_by_id(sheets))
        )

    if action_lower != "add" and (chart_id is None or isinstance(chart_id, bool)):
        raise UserInputError(f"chart_id is required for action='{action_lower}'.")

    if action_lower == "delete":
        request = {"deleteEmbeddedObject": {"objectId": chart_id}}
        summary = f"deleted chart {chart_id}"
    elif action_lower == "add":
        if not chart_type:
            raise UserInputError("chart_type is required for action='add'.")
        if not domain_range:
            raise UserInputError("domain_range is required for action='add'.")
        if not series_ranges:
            raise UserInputError("series_ranges is required for action='add'.")
        sheets = await _fetch_sheets(service, spreadsheet_id)
        domain_grid = _parse_a1_range(domain_range, sheets)
        series_grids = [
            _parse_a1_range(str(r), sheets)
            for r in _parse_json_list(series_ranges, "series_ranges")
        ]
        spec = _build_chart_spec(
            chart_type,
            title,
            subtitle,
            domain_grid,
            series_grids,
            legend_position,
            header_count,
        )
        anchor = _grid_coordinate_from_a1(anchor_cell, sheets) if anchor_cell else None
        request = {
            "addChart": {
                "chart": {
                    "spec": spec,
                    "position": _build_chart_position(anchor, width, height),
                }
            }
        }
        summary = f"added {chart_type.strip().upper()} chart" + (
            f" '{title}'" if title else ""
        )
    elif action_lower == "update":
        sheets = await _fetch_sheets(
            service, spreadsheet_id, fields="sheets(properties(sheetId,title),charts)"
        )
        chart = _find_chart(sheets, chart_id)
        spec = _apply_chart_spec_updates(
            chart.get("spec", {}) or {}, title, subtitle, chart_type, legend_position
        )
        request = {"updateChartSpec": {"chartId": chart_id, "spec": spec}}
        summary = f"updated chart {chart_id}"
    else:
        anchor = None
        if anchor_cell:
            sheets = await _fetch_sheets(service, spreadsheet_id)
            anchor = _grid_coordinate_from_a1(anchor_cell, sheets)
        request = _build_embedded_object_position_update(
            chart_id, anchor, width, height
        )
        summary = (
            f"moved chart {chart_id}"
            + (f" to {anchor_cell}" if anchor_cell else "")
            + (f" ({width or '?'}x{height or '?'}px)" if width or height else "")
        )

    result = await asyncio.to_thread(
        service.spreadsheets()
        .batchUpdate(spreadsheetId=spreadsheet_id, body={"requests": [request]})
        .execute
    )
    if action_lower == "add":
        replies = result.get("replies", []) or [{}]
        created = (replies[0].get("addChart") or {}).get("chart", {})
        summary += f" (ID: {created.get('chartId')})"
        if not anchor_cell:
            summary += " on a new sheet"

    text_output = f"Successfully {summary} in spreadsheet {spreadsheet_id} for {user_google_email}."
    logger.info(text_output)
    return text_output


@server.tool(
    title="Sort Sheet Range",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@handle_http_errors("sort_sheet_range", service_type="sheets")
@require_google_service("sheets", "sheets_write")
async def sort_sheet_range(
    service,
    user_google_email: str,
    spreadsheet_id: str,
    range_name: str,
    sort_specs: List[Dict[str, Any]],
    has_header: bool = False,
) -> str:
    """
    Sort the rows of a range by one or more columns (sortRange).

    Args:
        user_google_email (str): The user's Google email address. Required.
        spreadsheet_id (str): The ID of the spreadsheet. Required.
        range_name (str): A1 range whose rows are sorted, e.g. "Sheet1!A1:F200".
            Rows move as whole rows within the range only; columns outside it
            are not reordered, so include every column of the table. Required.
        sort_specs (List[Dict]): Sort keys in priority order, e.g.
            [{"column": "C", "order": "DESCENDING"}, {"column": "A"}]. "column"
            is a sheet column as letters or a zero-based index and must lie
            inside range_name; "order" is ASCENDING (default) or DESCENDING.
        has_header (bool): True keeps the first row of the range in place and
            sorts the rows below it. Defaults to False.

    Returns:
        str: Confirmation of the sort.
    """
    if not range_name or not range_name.strip():
        raise UserInputError("range_name is required.")
    logger.info(
        f"[sort_sheet_range] Email: '{user_google_email}', Spreadsheet: {spreadsheet_id}, Range: {range_name}"
    )

    sheets = await _fetch_sheets(service, spreadsheet_id)
    grid_range = _parse_a1_range(range_name, sheets)
    if has_header:
        start_row = grid_range.get("startRowIndex", 0)
        end_row = grid_range.get("endRowIndex")
        if end_row is not None and end_row - start_row < 2:
            raise UserInputError(
                "has_header=True needs a range with at least two rows (header + data)."
            )
        grid_range["startRowIndex"] = start_row + 1
    specs = _parse_sort_specs(sort_specs, grid_range)

    await asyncio.to_thread(
        service.spreadsheets()
        .batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "requests": [{"sortRange": {"range": grid_range, "sortSpecs": specs}}]
            },
        )
        .execute
    )

    described = ", ".join(
        f"{_index_to_column(s['dimensionIndex'])} {s['sortOrder'].lower()}"
        for s in specs
    )
    text_output = (
        f"Successfully sorted range '{range_name}'"
        + (" (header row kept)" if has_header else "")
        + f" by {described} in spreadsheet {spreadsheet_id} for {user_google_email}."
    )
    logger.info(text_output)
    return text_output


@server.tool(
    title="Find Replace Sheet",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
@handle_http_errors("find_replace_sheet", service_type="sheets")
@require_google_service("sheets", "sheets_write")
async def find_replace_sheet(
    service,
    user_google_email: str,
    spreadsheet_id: str,
    find: str,
    replacement: str = "",
    sheet_name: Optional[str] = None,
    range_name: Optional[str] = None,
    all_sheets: bool = False,
    match_case: bool = False,
    match_entire_cell: bool = False,
    search_by_regex: bool = False,
    include_formulas: bool = False,
) -> str:
    """
    Find and replace text across a range, one sheet, or the whole spreadsheet.

    Args:
        user_google_email (str): The user's Google email address. Required.
        spreadsheet_id (str): The ID of the spreadsheet. Required.
        find (str): Text (or regex when search_by_regex=True) to find. Required.
        replacement (str): Replacement text; with search_by_regex, $1..$9 refer
            to capture groups. Defaults to "" (delete the match).
        sheet_name (Optional[str]): Limit to one sheet; defaults to the first
            sheet unless range_name or all_sheets is given.
        range_name (Optional[str]): Limit to an A1 range, e.g. "Sheet1!B2:B500".
            Takes precedence over sheet_name / all_sheets.
        all_sheets (bool): Search every sheet. Defaults to False.
        match_case (bool): Case-sensitive match. Defaults to False.
        match_entire_cell (bool): Only replace when the whole cell matches.
            Defaults to False.
        search_by_regex (bool): Treat `find` as a regular expression (RE2 syntax).
            Defaults to False.
        include_formulas (bool): Also search inside formulas, not just values.
            Defaults to False.

    Returns:
        str: Counts of occurrences, cells, rows and sheets changed.
    """
    logger.info(
        f"[find_replace_sheet] Email: '{user_google_email}', Spreadsheet: {spreadsheet_id}, Find: {find!r}"
    )

    if range_name:
        sheets = await _fetch_sheets(service, spreadsheet_id)
        scope = {"range": _resolve_scope_grid_range(range_name, sheet_name, sheets)}
        scope_label = f"range '{range_name}'"
    elif all_sheets:
        scope = {"allSheets": True}
        scope_label = "all sheets"
    else:
        sheets = await _fetch_sheets(service, spreadsheet_id)
        sheet = _select_sheet(sheets, sheet_name)
        scope = {"sheetId": sheet["properties"]["sheetId"]}
        scope_label = f"sheet '{sheet['properties'].get('title')}'"

    request = _build_find_replace_request(
        find,
        replacement,
        scope,
        match_case,
        match_entire_cell,
        search_by_regex,
        include_formulas,
    )
    result = await asyncio.to_thread(
        service.spreadsheets()
        .batchUpdate(spreadsheetId=spreadsheet_id, body={"requests": [request]})
        .execute
    )
    replies = result.get("replies", []) or [{}]
    reply = replies[0].get("findReplace", {}) if replies else {}

    text_output = (
        f"Find/replace of {find!r} with {replacement!r} in {scope_label} of spreadsheet "
        f"{spreadsheet_id} for {user_google_email}: {_format_find_replace_reply(reply)}."
    )
    logger.info(text_output)
    return text_output
