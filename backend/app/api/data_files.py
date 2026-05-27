"""Data-file uploads → queryable DuckDB connections.

Phase 13. A user uploads a CSV / Parquet / JSON file via multipart POST;
the file lands on disk under ``DATA_FILES_DIR/<workspace_id>/`` and the
backend creates a fresh ``WorkspaceConnection`` with
``dialect="duckdb"`` pointing at it. The DuckDB engine surfaces the
file as a single read-only ``VIEW`` (driven by the ``attached_files``
field on ``connection_meta``), so the rest of the agent — planner,
validator, executor, chart designer — treats it like any other table.

Why a separate route and not the existing ``POST /workspaces/{id}/connections``?
  * Multipart vs JSON — `connections` is pure JSON; mixing it with file
    upload would force every other dialect through multipart too.
  * Auto-derived ``connection_meta`` — the caller never has to spell
    out ``{path: ":memory:", attached_files: [{...}]}``; we derive that
    from the uploaded filename.
  * Sanitisation lives in one place — filename normalization, size
    cap, and view-name slugification are all here.

Security:
  * Filenames are slugified and prefixed with the connection UUID so
    user-supplied path-traversal (`../../etc/passwd`) can't escape the
    workspace dir.
  * Max upload is :data:`settings.DATA_FILE_MAX_BYTES` (50 MB by
    default) — enforced by streaming the body in chunks and aborting
    when the running total exceeds the cap.
  * Only extensions in :data:`_ALLOWED_EXTENSIONS` are accepted; the
    DuckDB engine uses the matching reader function (``read_csv_auto``,
    ``read_parquet``, ``read_json_auto``) at view-creation time.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    UploadFile,
    status,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.workspaces import (
    ConnectionOut,
    _enqueue_profile_job,
    _get_owned_workspace,
)
from app.config import settings
from app.db.models import ProfileJob, User, WorkspaceConnection
from app.db.session import get_db

log = logging.getLogger(__name__)

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


# Keep this list in sync with `_DATA_FILE_LOADERS` in engines/duckdb.py.
# Same set of suffixes; we just need the matching subset here for the
# upload-side check (the engine accepts a stricter superset for the
# rare case of an attachment with no extension).
_ALLOWED_EXTENSIONS = {
    ".csv", ".tsv",
    ".parquet", ".pq",
    ".json", ".ndjson", ".jsonl",
}


def _slug(s: str) -> str:
    """Lowercase, replace runs of non-alphanumerics with ``_``, trim."""
    s = re.sub(r"[^a-zA-Z0-9]+", "_", s).strip("_").lower()
    if not s:
        s = "data"
    # SQL identifiers can be very long, but keeping table names <= 63
    # chars matches Postgres' default identifier limit and avoids weird
    # truncation in mixed deploys.
    return s[:63]


def _split_ext(filename: str) -> tuple[str, str]:
    """Return ``(stem, ext)`` where ``ext`` is lowercased and starts with
    a dot. Multi-suffix files like ``.json.gz`` keep only the last
    suffix — we don't auto-decompress."""
    base = os.path.basename(filename or "")
    name, ext = os.path.splitext(base)
    return name, ext.lower()


@router.post(
    "/{workspace_id}/data-files",
    response_model=ConnectionOut,
    status_code=status.HTTP_201_CREATED,
)
async def upload_data_file(
    workspace_id: UUID,
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ConnectionOut:
    """Save the upload + create a DuckDB connection wrapping it.

    The connection's name and the auto-generated view's name both
    derive from the file stem (sales_2024.csv → name=sales_2024,
    view=sales_2024). Collisions on the (workspace_id, name) unique
    constraint are surfaced as 409; the caller can rename the file
    before retrying.
    """
    await _get_owned_workspace(session, workspace_id, current_user)

    stem, ext = _split_ext(file.filename or "")
    if ext not in _ALLOWED_EXTENSIONS:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=(
                f"Unsupported file type {ext or '(no extension)'!r}. "
                f"Supported: {sorted(_ALLOWED_EXTENSIONS)}"
            ),
        )

    view_name = _slug(stem)
    # Reserve the connection ID up front so the on-disk filename can
    # carry it as a prefix (collision-free across workspaces).
    new_conn_id = uuid4()

    # Destination: <DATA_FILES_DIR>/<workspace_id>/<conn_id>_<slug><ext>
    workspace_dir = Path(settings.DATA_FILES_DIR) / str(workspace_id)
    workspace_dir.mkdir(parents=True, exist_ok=True)
    safe_basename = f"{new_conn_id}_{view_name}{ext}"
    dest = workspace_dir / safe_basename
    if dest.resolve().parent != workspace_dir.resolve():
        # Belt + suspenders against an unforeseen path-traversal path
        # through slug() — should be impossible given the regex.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="invalid destination path",
        )

    # Stream the body to disk with a hard size cap. We tally bytes as
    # we go so a large upload doesn't fill the volume before we notice.
    max_bytes = settings.DATA_FILE_MAX_BYTES
    written = 0
    try:
        with dest.open("wb") as fh:
            while True:
                chunk = await file.read(64 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    fh.close()
                    dest.unlink(missing_ok=True)
                    raise HTTPException(
                        status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=(
                            f"File exceeds {max_bytes // (1024 * 1024)}MB "
                            "upload limit."
                        ),
                    )
                fh.write(chunk)
    except HTTPException:
        raise
    except Exception:
        # Clean up the half-written file. Root-cause investigation
        # (disk full, permission denied, …) happens via the log entry.
        log.exception("data-file upload failed (workspace=%s)", workspace_id)
        if dest.exists():
            dest.unlink(missing_ok=True)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="upload failed; check server logs for details",
        )

    if written == 0:
        dest.unlink(missing_ok=True)
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail="uploaded file is empty"
        )

    abs_path = str(dest.resolve())
    conn = WorkspaceConnection(
        id=new_conn_id,
        workspace_id=workspace_id,
        name=view_name,
        dialect="duckdb",
        connection_meta={
            "path": ":memory:",
            "attached_files": [
                {"path": abs_path, "view_name": view_name},
            ],
            # Metadata for the UI / future cleanup paths.
            "source": "data_file_upload",
            "original_filename": file.filename or safe_basename,
            "size_bytes": written,
        },
        status="pending",
    )
    session.add(conn)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        dest.unlink(missing_ok=True)
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=(
                "A connection with this name already exists in this "
                "workspace. Rename the file and try again."
            ),
        ) from exc

    job = ProfileJob(connection_id=conn.id, state="queued")
    session.add(job)
    await session.commit()
    await session.refresh(conn)
    await session.refresh(job)

    _enqueue_profile_job(str(conn.id), str(job.id))

    return ConnectionOut(
        id=str(conn.id),
        workspace_id=str(conn.workspace_id),
        name=conn.name,
        dialect=conn.dialect,
        status=conn.status,
        profile_job_id=str(job.id),
    )
