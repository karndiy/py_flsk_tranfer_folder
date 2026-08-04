# =============================
# app.py
# =============================
from flask import Flask, request, abort, jsonify, render_template, send_file
from pathlib import Path
import tempfile
import zipfile
import os
import time
import json
import threading
import shutil
from typing import IO
from urllib.parse import quote
import io


STREAM_CHUNK_SIZE = 8 * 1024 * 1024
ARCHIVE_SUFFIX = ".zip"
_raw_zip_level = os.environ.get("ZIP_COMPRESS_LEVEL")
try:
    _zip_level = int(_raw_zip_level) if _raw_zip_level is not None else 1
except (TypeError, ValueError):
    _zip_level = 1
ZIP_COMPRESSION_LEVEL = max(0, min(9, _zip_level))
ZIP_COMPRESSION = zipfile.ZIP_STORED if ZIP_COMPRESSION_LEVEL == 0 else zipfile.ZIP_DEFLATED
ZIP_WRITE_KWARGS = {"compression": ZIP_COMPRESSION}
if ZIP_COMPRESSION == zipfile.ZIP_DEFLATED:
    ZIP_WRITE_KWARGS["compresslevel"] = ZIP_COMPRESSION_LEVEL


app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-key")
# Disable Flask's default upload limit so large folders are accepted
app.config["MAX_CONTENT_LENGTH"] = None


BASE_DIR = Path(__file__).parent.resolve()
UPLOAD_ROOT = (BASE_DIR / "storage").resolve()
UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
UPLOAD_LOG = (BASE_DIR / "upload_log.jsonl").resolve()
_upload_log_lock = threading.Lock()

# Dedicate a local temp directory so large uploads are not spooled to a slow system temp drive
TEMP_UPLOAD_ROOT = (BASE_DIR / "tmp_uploads").resolve()
TEMP_UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
tempfile.tempdir = str(TEMP_UPLOAD_ROOT)
os.environ.setdefault("TMPDIR", str(TEMP_UPLOAD_ROOT))
os.environ.setdefault("TEMP", str(TEMP_UPLOAD_ROOT))
os.environ.setdefault("TMP", str(TEMP_UPLOAD_ROOT))


# ---------- Helpers ----------


def _ensure_within_upload_root(path: Path) -> Path:
    if path == UPLOAD_ROOT or UPLOAD_ROOT in path.parents:
        return path
    abort(400, description="Path escape blocked")


def _safe_path(relpath: str) -> Path:
    if not relpath:
        return UPLOAD_ROOT

    candidate = (UPLOAD_ROOT / relpath).resolve()
    return _ensure_within_upload_root(candidate)


def _relative_to_upload_root(path: Path) -> str:
    if path == UPLOAD_ROOT:
        return ""
    return path.relative_to(UPLOAD_ROOT).as_posix()


def _format_bytes(num: int | None) -> str:
    if num is None:
        return "-"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    size = float(num)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.2f} {unit}"
        size /= 1024


def _cleanup_temp_file(path: Path):
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _copy_stream(src: IO[bytes], dst: IO[bytes]) -> int:
    total = 0
    while True:
        chunk = src.read(STREAM_CHUNK_SIZE)
        if not chunk:
            break
        dst.write(chunk)
        total += len(chunk)
    return total


def _write_stream_to_path(stream: IO[bytes], out_path: Path) -> int:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("wb") as target_file:
        return _copy_stream(stream, target_file)


def _log_upload(event: dict):
    payload = {
        **event,
        "timestamp": time.time(),
    }
    payload.setdefault("target", "")
    with _upload_log_lock:
        with UPLOAD_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, separators=(",", ":")) + "\n")


def _zip_folder_to_temp(folder: Path) -> tuple[Path, int, int]:
    """Zip a folder into a temporary .zip file and return the temp path with stats."""
    if not folder.exists() or not folder.is_dir():
        abort(404, description="Folder not found")

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ARCHIVE_SUFFIX, dir=TEMP_UPLOAD_ROOT)
    tmp_path = Path(tmp.name)
    tmp.close()

    file_count = 0
    source_bytes = 0
    with zipfile.ZipFile(tmp_path, "w", allowZip64=True, **ZIP_WRITE_KWARGS) as zf:
        for root, _, files in os.walk(folder):
            for filename in files:
                full_path = Path(root) / filename
                try:
                    arcname = full_path.relative_to(folder)
                except ValueError:
                    continue
                zf.write(full_path, arcname)
                file_count += 1
                try:
                    source_bytes += full_path.stat().st_size
                except OSError:
                    pass

    return tmp_path, file_count, source_bytes


def _extract_zip_to_folder(zip_path: Path, dest_folder: Path) -> tuple[int, int, list[str]]:
    """Extract a zip archive safely into dest_folder and return (files, bytes, saved_paths)."""
    dest_folder.mkdir(parents=True, exist_ok=True)

    extracted = 0
    total_bytes = 0
    saved_paths: list[str] = []
    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            member_path = Path(info.filename)
            if member_path.is_absolute() or ".." in member_path.parts:
                abort(400, description="Archive contains unsafe paths")

            resolved = (dest_folder / member_path).resolve()
            _ensure_within_upload_root(resolved)

            if info.is_dir():
                resolved.mkdir(parents=True, exist_ok=True)
                continue

            resolved.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, "r") as src, resolved.open("wb") as target_file:
                total_bytes += _copy_stream(src, target_file)
            extracted += 1
            saved_paths.append(_relative_to_upload_root(resolved))

    return extracted, total_bytes, saved_paths


def _read_upload_log(limit: int = 200) -> list[dict]:
    if not UPLOAD_LOG.exists():
        return []

    with _upload_log_lock:
        lines = UPLOAD_LOG.read_text(encoding="utf-8").splitlines()

    entries: list[dict] = []
    for line in lines[-limit:]:
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    entries.sort(key=lambda item: item.get("timestamp", 0), reverse=True)
    return entries


def _list_directory(folder: Path) -> list[dict]:
    entries: list[dict] = []
    try:
        with os.scandir(folder) as it:
            for entry in it:
                entry_path = Path(entry.path)
                rel_path = _relative_to_upload_root(entry_path)
                is_dir = entry.is_dir()
                info = entry.stat()
                record = {
                    "name": entry.name,
                    "path": rel_path,
                    "is_dir": is_dir,
                    "size": None if is_dir else info.st_size,
                    "modified": info.st_mtime,
                }
                if is_dir:
                    record["download_url"] = f"/download_folder?path={quote(rel_path)}"
                else:
                    record["download_url"] = f"/download_file?path={quote(rel_path)}"
                entries.append(record)
    except FileNotFoundError:
        abort(404, description="Folder not found")

    entries.sort(key=lambda item: (not item["is_dir"], item["name"].lower()))
    return entries


@app.post("/delete")
def delete_items():
    """Delete files or folders under the upload root.

    Expects JSON body: { "paths": ["relative/path/one", "file.txt"] }
    Paths are validated and constrained to `storage/`.
    """
    try:
        payload = request.get_json(force=True)
    except Exception:
        abort(400, description="Invalid JSON payload")

    paths = payload.get("paths") if isinstance(payload, dict) else None
    if not paths or not isinstance(paths, list):
        abort(400, description="Missing or invalid 'paths' list")

    deleted = []
    errors = []
    for rel in paths:
        if not isinstance(rel, str):
            errors.append({"path": str(rel), "error": "invalid path"})
            continue

        # normalize and validate
        candidate = _safe_path(rel.strip())
        # don't allow deleting the upload root itself
        if candidate == UPLOAD_ROOT:
            errors.append({"path": _relative_to_upload_root(candidate), "error": "cannot delete root"})
            continue

        try:
            if candidate.is_dir():
                shutil.rmtree(candidate)
            else:
                candidate.unlink()
            deleted.append(_relative_to_upload_root(candidate))
        except FileNotFoundError:
            errors.append({"path": _relative_to_upload_root(candidate), "error": "not found"})
        except Exception as exc:
            errors.append({"path": _relative_to_upload_root(candidate), "error": str(exc)})

    _log_upload({
        "kind": "delete",
        "deleted_count": len(deleted),
        "deleted": deleted,
        "errors": errors,
    })

    return jsonify({
        "status": "ok",
        "deleted": deleted,
        "errors": errors,
    })



@app.post("/download")
def download_items():
    """Create and return a ZIP for one or more selected files/folders.

    Expects JSON body: { "paths": ["relative/path/one", "file.txt"] }
    """
    try:
        payload = request.get_json(force=True)
    except Exception:
        abort(400, description="Invalid JSON payload")

    paths = payload.get("paths") if isinstance(payload, dict) else None
    if not paths or not isinstance(paths, list):
        abort(400, description="Missing or invalid 'paths' list")

    # Normalize and validate candidates
    candidates: list[Path] = []
    for rel in paths:
        if not isinstance(rel, str):
            continue
        p = _safe_path(rel.strip())
        if not p.exists():
            continue
        candidates.append(p)

    if not candidates:
        abort(400, description="No valid paths to download")

    # Single file or single folder optimization: stream directly
    if len(candidates) == 1:
        single = candidates[0]
        if single.is_file():
            return send_file(
                single,
                as_attachment=True,
                download_name=single.name,
                max_age=0,
                conditional=True,
                last_modified=None,
            )
        # for a single folder, zip and return

    # Create a temp zip containing all selected items
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ARCHIVE_SUFFIX, dir=TEMP_UPLOAD_ROOT)
    tmp_path = Path(tmp.name)
    tmp.close()

    file_count = 0
    source_bytes = 0
    with zipfile.ZipFile(tmp_path, "w", allowZip64=True, **ZIP_WRITE_KWARGS) as zf:
        for cand in candidates:
            if cand.is_dir():
                for root, _, files in os.walk(cand):
                    for fname in files:
                        full = Path(root) / fname
                        try:
                            resolved = full.resolve()
                        except OSError:
                            continue
                        # ensure inside upload root
                        try:
                            _ensure_within_upload_root(resolved)
                        except Exception:
                            continue
                        try:
                            arcname = resolved.relative_to(UPLOAD_ROOT)
                        except Exception:
                            continue
                        zf.write(resolved, arcname.as_posix())
                        file_count += 1
                        try:
                            source_bytes += resolved.stat().st_size
                        except OSError:
                            pass
            else:
                try:
                    resolved = cand.resolve()
                except OSError:
                    continue
                try:
                    _ensure_within_upload_root(resolved)
                except Exception:
                    continue
                try:
                    arcname = resolved.relative_to(UPLOAD_ROOT)
                except Exception:
                    continue
                zf.write(resolved, arcname.as_posix())
                file_count += 1
                try:
                    source_bytes += resolved.stat().st_size
                except OSError:
                    pass

    response = send_file(
        tmp_path,
        mimetype="application/zip",
        as_attachment=True,
        download_name="selection.zip",
        max_age=0,
        conditional=False,
        last_modified=None,
    )
    response.headers["X-Archive-Files"] = str(file_count)
    response.headers["X-Archive-Source-Bytes"] = str(source_bytes)
    response.call_on_close(lambda path=tmp_path: _cleanup_temp_file(path))
    return response


# ---------- Routes ----------


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/upload_folder")
def upload_folder():
    """Directly upload an entire folder (via webkitdirectory) preserving structure."""
    target = request.form.get("target", "").strip()
    current = request.form.get("current", "").strip()
    skip_existing = request.form.get("skip_existing", "").lower() in {"1", "true", "yes", "on"}

    # Base destination is current directory
    dest_root = _safe_path(str(Path(current) / target)) if target else _safe_path(current)
    dest_root.mkdir(parents=True, exist_ok=True)

    files = request.files.getlist("files")
    paths = request.form.getlist("paths")
    if not files or not paths or len(files) != len(paths):
        abort(400, description="Missing files or paths")

    start_time = time.perf_counter()
    saved = 0
    skipped = 0
    total_bytes = 0
    saved_files: list[str] = []
    skipped_files: list[str] = []

    for upload, rel in zip(files, paths):
        rel_path = Path(rel).as_posix().lstrip("/")
        if ".." in Path(rel_path).parts:
            abort(400, description="Invalid relative path")

        out_path = (dest_root / rel_path).resolve()
        _ensure_within_upload_root(out_path)
        rel_to_root = _relative_to_upload_root(out_path)

        if skip_existing and out_path.exists():
            incoming_size = upload.content_length
            try:
                current_size = out_path.stat().st_size
            except OSError:
                current_size = None
            if incoming_size is not None and current_size == incoming_size:
                skipped += 1
                skipped_files.append(rel_to_root)
                if hasattr(upload, "close"):
                    upload.close()
                continue

        stream = upload.stream
        if hasattr(stream, "seek"):
            stream.seek(0)

        bytes_written = _write_stream_to_path(stream, out_path)
        total_bytes += bytes_written
        saved += 1
        saved_files.append(rel_to_root)

        if hasattr(upload, "close"):
            upload.close()

    duration = time.perf_counter() - start_time

    response_payload = {
        "status": "ok",
        "saved": saved,
        "skipped": skipped,
        "bytes": total_bytes,
        "duration": duration,
        "target": str(dest_root.relative_to(UPLOAD_ROOT)) if dest_root != UPLOAD_ROOT else "",
        "saved_files": saved_files,
        "skipped_files": skipped_files,
    }

    _log_upload({
        "kind": "folder_upload",
        **response_payload,
    })

    return jsonify(response_payload)


@app.post("/upload_archive")
def upload_archive():
    """Accept a single archive upload and extract it server-side."""
    target = request.form.get("target", "").strip()
    current = request.form.get("current", "").strip()
    archive = request.files.get("archive")
    if not archive:
        abort(400, description="Missing archive file")

    dest_root = _safe_path(str(Path(current) / target)) if target else _safe_path(current)
    dest_root.mkdir(parents=True, exist_ok=True)

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ARCHIVE_SUFFIX, dir=TEMP_UPLOAD_ROOT)
    tmp_path = Path(tmp.name)
    tmp.close()

    received_bytes = 0
    start_time = time.perf_counter()

    try:
        stream = archive.stream
        if hasattr(stream, "seek"):
            stream.seek(0)

        with tmp_path.open("wb") as tmp_file:
            received_bytes = _copy_stream(stream, tmp_file)

        extracted, extracted_bytes, saved_files = _extract_zip_to_folder(tmp_path, dest_root)
        duration = time.perf_counter() - start_time
    finally:
        if hasattr(archive, "close"):
            archive.close()
        _cleanup_temp_file(tmp_path)

    response_payload = {
        "status": "ok",
        "saved": extracted,
        "bytes": extracted_bytes,
        "received_bytes": received_bytes,
        "duration": duration,
        "target": str(dest_root.relative_to(UPLOAD_ROOT)) if dest_root != UPLOAD_ROOT else "",
        "saved_files": saved_files,
    }

    _log_upload({
        "kind": "archive_upload",
        **response_payload,
    })

    return jsonify(response_payload)


@app.get("/download_folder")
def download_folder():
    """Package a server folder into a zip and send it to the client."""
    rel = request.args.get("path", "").strip()
    folder = _safe_path(rel)
    if not folder.exists() or not folder.is_dir():
        abort(404, description="Folder not found")

    start_time = time.perf_counter()
    archive_path, file_count, source_bytes = _zip_folder_to_temp(folder)
    duration = time.perf_counter() - start_time

    download_name = f"{folder.name or 'storage'}.zip"

    response = send_file(
        archive_path,
        mimetype="application/zip",
        as_attachment=True,
        download_name=download_name,
        max_age=0,
        conditional=False,
        last_modified=None,
    )
    response.headers["X-Archive-Files"] = str(file_count)
    response.headers["X-Archive-Source-Bytes"] = str(source_bytes)
    response.headers["X-Archive-Generation-Time"] = f"{duration:.6f}"
    response.call_on_close(lambda path=archive_path: _cleanup_temp_file(path))
    return response


@app.get("/files")
def files_page():
    """Render an HTML file browser for uploaded content."""
    rel = request.args.get("path", "").strip()
    folder = _safe_path(rel)
    if not folder.exists() or not folder.is_dir():
        abort(404, description="Folder not found")

    entries = _list_directory(folder)
    for entry in entries:
        entry["size_human"] = _format_bytes(entry.get("size"))
        modified = entry.get("modified")
        entry["modified_label"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(modified)) if modified is not None else "-"
        entry["type_label"] = "Folder" if entry.get("is_dir") else "File"
    current_path = _relative_to_upload_root(folder)
    parent_path = "" if folder == UPLOAD_ROOT else _relative_to_upload_root(folder.parent)

    breadcrumbs = [{"name": "storage", "path": ""}]
    if current_path:
        parts = current_path.split('/')
        accum: list[str] = []
        for part in parts:
            accum.append(part)
            breadcrumbs.append({"name": part, "path": '/'.join(accum)})

    return render_template(
        "files.html",
        entries=entries,
        current_path=current_path,
        parent_path=parent_path,
        breadcrumbs=breadcrumbs,
    )


@app.get("/download_file")
def download_file():
    """Stream an individual file from storage."""
    rel = request.args.get("path", "").strip()
    file_path = _safe_path(rel)
    if not file_path.exists() or not file_path.is_file():
        abort(404, description="File not found")

    return send_file(
        file_path,
        as_attachment=True,
        download_name=file_path.name,
        max_age=0,
        conditional=True,
        last_modified=None,
    )


@app.get("/list_files")
def list_files():
    """List files and folders under storage for quick browsing."""
    rel = request.args.get("path", "").strip()
    folder = _safe_path(rel)
    if not folder.exists() or not folder.is_dir():
        abort(404, description="Folder not found")

    entries = _list_directory(folder)
    return jsonify({
        "status": "ok",
        "path": _relative_to_upload_root(folder),
        "entries": entries,
    })


@app.get("/upload_log")
def upload_log():
    """Expose recent upload events logged by the server."""
    limit = request.args.get("limit", "200").strip()
    try:
        limit_value = max(1, min(1000, int(limit)))
    except ValueError:
        limit_value = 200

    entries = _read_upload_log(limit_value)
    return jsonify({
        "status": "ok",
        "entries": entries,
    })


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 5002)))
