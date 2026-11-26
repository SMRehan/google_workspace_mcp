# Google Keep Reference

Tool-by-tool parameter reference for the Google Keep service.

### list_keep_notes
List Google Keep notes as a compact summary (name, title, kind, item counts,
attachment count, update time). Google Workspace accounts only.

| Parameter | Type | Required | Default | Notes |
|-----------|------|----------|---------|-------|
| user_google_email | string | yes |  | The user's Google email address. Required. |
| filter | string | no |  | AIP-160 filter string passed straight to the API. Fields: create_time, update_time, trash_time, trashed. Example: "trashed=false". When omitted the API applies… |
| page_size | integer | no | 20 | Notes per page (default 20, max 100). |
| page_token | string | no |  | Token from a previous page. |

### get_keep_note
Get one Google Keep note with its full text or checklist (nested items with
check marks), attachments and permissions; optionally download an
attachment. Google Workspace accounts only.

| Parameter | Type | Required | Default | Notes |
|-----------|------|----------|---------|-------|
| user_google_email | string | yes |  | The user's Google email address. Required. |
| note_id | string | yes |  | Note resource name ("notes/abc123") or bare id ("abc123"). |
| download_attachment | string | no |  | Attachment to download instead of rendering the note: its full name ("notes/abc/attachments/xyz") or bare id. Text-like types are returned inline; binaries… |
| mime_type | string | no |  | MIME type to download the attachment as. Must be one the attachment lists; may be omitted when it lists exactly one. |
| include_base64 | boolean | no | false | For binary attachments, include the content as standard base64 (default False). |

### manage_keep_note
Create or delete a Google Keep note. Google Workspace accounts only.

| Parameter | Type | Required | Default | Notes |
|-----------|------|----------|---------|-------|
| user_google_email | string | yes |  | The user's Google email address. Required. |
| action | string | yes |  | "create" or "delete". |
| note_id | string | no |  | Note to delete ("notes/abc123" or "abc123"). Required for "delete". Deletion is immediate and cannot be undone; the caller must own the note. |
| title | string | no |  | Title for "create". |
| text | string | no |  | Body for a text note ("create"). Mutually exclusive with list_items. |
| list_items | array | no |  | Body for a checklist note ("create"): a list of {"text": str, "checked": bool, "children": [items]}. Mutually exclusive with text. |

### manage_keep_note_permissions
List, grant or revoke collaborator access on a Google Keep note. Google
Workspace accounts only; the caller must own the note to grant or revoke.

| Parameter | Type | Required | Default | Notes |
|-----------|------|----------|---------|-------|
| user_google_email | string | yes |  | The user's Google email address. Required. |
| action | string | yes |  | "list", "grant" or "revoke". |
| note_id | string | yes |  | Note resource name ("notes/abc123") or bare id. |
| emails | array | no |  | Email addresses to grant WRITER access. Required for "grant". |
| permission_names | array | no |  | Permissions to revoke, as full names ("notes/abc/permissions/xyz") or bare ids. Required for "revoke". Use "list" to find them. |
