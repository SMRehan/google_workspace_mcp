# REST passthrough Reference

Tool-by-tool parameter reference for the REST passthrough service.

### google_api_request
Call any REST method of a Google API this server is configured for.

| Parameter | Type | Required | Default | Notes |
|-----------|------|----------|---------|-------|
| user_google_email | string | yes |  | The user's Google email address. Required. |
| service | string | yes |  | Service name: gmail, drive, calendar, docs, sheets, chat, forms, slides, tasks, people (alias: contacts), customsearch (alias: search), script (alias:… |
| method | string | yes |  | Dotted method path, e.g. 'users.messages.list', 'spreadsheets.values.batchGet', 'files.export_media'. |
| params | object | no |  | Path and query parameters, keyed exactly as the API documents them (camelCase). |
| body | object | no |  | JSON request body for POST/PUT/PATCH methods. |
| version | string | no |  | API version override (default: the version the typed tools use, e.g. drive v3, sheets v4, calendar v3). |
| return_base64 | boolean | no | false | For '_media' methods, include the downloaded bytes as base64. Default False (size, MIME guess and SHA-256 only). |
| max_output_chars | integer | no |  | Cap on the returned text (default 60000). Longer output is truncated with a note. |
