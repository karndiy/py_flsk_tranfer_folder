# Flask Folder Transfer

Simple Flask app to upload entire folders from your browser, extract uploaded ZIP archives, browse stored files, and download any folder as a ZIP. Great for quick local or LAN transfers without setting up full file servers.

## Features

- Upload whole folders via browser (`webkitdirectory`) preserving structure
- Upload a `.zip` archive and extract server‑side
- Browse files and folders under `storage/` in a clean UI
- Download any folder as a ZIP (`/download_folder`)
- Stream individual file downloads (`/download_file`)
- JSON APIs to list files and inspect recent uploads
- Large-file friendly: streams uploads, temp files go to `tmp_uploads/`

## Quick Start

Prereqs: Python 3.10+.

1) Install deps

```
pip install -r requirements.txt
```

2) Run the app

```
python app.py
```

3) Open in your browser

```
http://localhost:5002/
```

Pick a folder to upload. Browse files at `http://localhost:5002/files`.

## Configuration

Set via environment variables (optional):

- `PORT` — server port (default `5002`)
- `SECRET_KEY` — Flask session key (default `dev-key`)
- `ZIP_COMPRESS_LEVEL` — 0–9 (0=store only, 1=fast default, 9=max compression)

Folders created on first run:

- `storage/` — root of uploaded content
- `tmp_uploads/` — temp area for large uploads and ZIPs
- `upload_log.jsonl` — newline‑delimited JSON log of uploads

## Endpoints

- `GET /` — Upload UI for folders (uses `webkitdirectory`)
- `POST /upload_folder` — Upload multiple files with relative paths preserved
  - Form fields: `files` (multiple), `paths` (matching relative paths), `current`, `target`, `skip_existing` (optional: `true|1`)
  - Intended for the browser UI; the server validates paths and prevents escapes
- `POST /upload_archive` — Upload a single `.zip` and extract into `storage/current/target`
  - Form fields: `archive` (file), `current`, `target`
- `GET /files?path=` — File browser UI for `storage/`
- `GET /download_folder?path=` — Streams a ZIP of the folder
  - Response headers include: `X-Archive-Files`, `X-Archive-Source-Bytes`, `X-Archive-Generation-Time`
- `GET /download_file?path=` — Streams a single file
- `GET /list_files?path=` — JSON listing of a directory
- `GET /upload_log?limit=` — JSON of recent upload events

`path` values are always relative to `storage/`.

## Examples

- Upload a ZIP and extract to `storage/projects/demo`:

```
curl -F "archive=@myfolder.zip" -F current=projects -F target=demo http://localhost:5002/upload_archive
```

- Download a folder as ZIP (relative path under `storage/`):

```
curl -L "http://localhost:5002/download_folder?path=projects/demo" -o demo.zip
```

- List files under `storage/` root:

```
curl http://localhost:5002/list_files
```

## Notes & Limits

- No authentication. Intended for local or trusted networks only.
- Path traversal is blocked; all operations are constrained to `storage/`.
- Browser folder upload relies on `webkitdirectory` (supported by most modern browsers).
- Default ZIP compression level is `1` (fast). Set `ZIP_COMPRESS_LEVEL` for trade‑offs.
- Flask upload size limit is disabled to allow large folders; ensure sufficient disk space.

## Project Layout

- `app.py` — Flask app and routes
- `templates/index.html` — Folder upload UI
- `templates/files.html` — File browser UI
- `storage/` — Uploaded files live here
- `tmp_uploads/` — Temp files/archives
- `upload_log.jsonl` — Upload logs (NDJSON)

---

Troubleshooting tips:

- If downloads show 0 bytes or stall, check antivirus or proxy filters.
- For very large zips, consider lowering `ZIP_COMPRESS_LEVEL` to `0` for speed.
- On Windows, long paths may need `git config core.longpaths true` or OS long‑path support.

