from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import ROOT_DIR, Settings, get_settings
from .s3_service import S3Service
from .session_store import SessionStore, create_session_store
from .upload_sessions import (
    ManifestFile,
    clean_relative_path,
    is_obsidian_path,
)


FRONTEND_DIR = ROOT_DIR / "frontend"


class ManifestItem(BaseModel):
    path: str
    size: int = Field(ge=0)
    content_type: Optional[str] = None


class UploadManifestRequest(BaseModel):
    destination_prefix: str = ""
    ignore_obsidian: bool = True
    files: list[ManifestItem]


class FinishUploadRequest(BaseModel):
    session_id: str


def get_s3(settings: Annotated[Settings, Depends(get_settings)]) -> S3Service:
    return S3Service(settings)


def get_session_store(settings: Annotated[Settings, Depends(get_settings)]) -> SessionStore:
    return create_session_store(settings)


def verify_api_token(
    settings: Annotated[Settings, Depends(get_settings)],
    authorization: Annotated[Optional[str], Header()] = None,
):
    if not settings.app_api_token:
        return
    expected = f"Bearer {settings.app_api_token}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing API token")


app = FastAPI(title="aws-browser")

settings = get_settings()
if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


@app.get("/")
def index():
    if not (FRONTEND_DIR / "index.html").exists():
        return {"ok": True, "service": "aws-browser-api"}
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/config.js")
def frontend_config():
    config_path = FRONTEND_DIR / "config.js"
    if not config_path.exists():
        config_path = FRONTEND_DIR / "config.local.js"
    if not config_path.exists():
        return {"detail": "Frontend config is not bundled with this deployment"}
    return FileResponse(config_path, media_type="text/javascript")


@app.get("/api/health")
def health(settings: Annotated[Settings, Depends(get_settings)]):
    return {
        "ok": True,
        "configured": bool(settings.s3_bucket),
        "bucket": settings.s3_bucket,
        "base_prefix": settings.normalized_base_prefix,
        "max_file_bytes": settings.max_file_bytes,
        "session_store": settings.upload_session_store,
    }


@app.get("/api/list")
def list_objects(
    s3: Annotated[S3Service, Depends(get_s3)],
    _: Annotated[None, Depends(verify_api_token)],
    prefix: str = "",
):
    safe_prefix = clean_relative_path(prefix, allow_empty=True)
    entries = s3.list_prefix(safe_prefix)
    return {"prefix": safe_prefix, "entries": [entry.__dict__ for entry in entries]}


@app.post("/api/upload-manifest")
def upload_manifest(
    payload: UploadManifestRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    session_store: Annotated[SessionStore, Depends(get_session_store)],
    _: Annotated[None, Depends(verify_api_token)],
):
    if not settings.s3_bucket:
        raise HTTPException(status_code=400, detail="S3_BUCKET is required before uploading files")

    if len(payload.files) > settings.max_manifest_files:
        raise HTTPException(status_code=400, detail="Too many files in one upload")

    accepted: list[ManifestFile] = []
    skipped: list[dict[str, str]] = []

    for item in payload.files:
        path = clean_relative_path(item.path)
        if payload.ignore_obsidian and is_obsidian_path(path):
            skipped.append({"path": path, "reason": ".obsidian ignored"})
            continue
        if item.size > settings.max_file_bytes:
            skipped.append({"path": path, "reason": "file is over size limit"})
            continue
        accepted.append(
            ManifestFile(
                path=path,
                size=item.size,
                content_type=item.content_type,
            )
        )

    session = session_store.create(accepted, payload.destination_prefix)
    return {
        "session_id": session.id,
        "expires_at": session.expires_at.isoformat(),
        "accepted": [file.__dict__ for file in accepted],
        "skipped": skipped,
        "total_files": len(accepted),
        "total_bytes": sum(file.size for file in accepted),
    }


@app.post("/api/upload-file")
def upload_file(
    s3: Annotated[S3Service, Depends(get_s3)],
    session_store: Annotated[SessionStore, Depends(get_session_store)],
    _: Annotated[None, Depends(verify_api_token)],
    session_id: Annotated[str, Form()],
    path: Annotated[str, Form()],
    file: Annotated[UploadFile, File()],
):
    safe_path = clean_relative_path(path)
    session = session_store.get(session_id)
    manifest_file = session.files.get(safe_path)
    if not manifest_file:
        raise HTTPException(status_code=400, detail="File is not part of this upload session")

    content_type = file.content_type or manifest_file.content_type or "application/octet-stream"
    key = s3.make_key(safe_path, session.destination_prefix)
    s3.upload_fileobj(file.file, key, content_type)
    session_store.save_uploaded(session, safe_path, key)

    return {"path": safe_path, "key": key, "uploaded": True}


@app.post("/api/finish-upload")
def finish_upload(
    payload: FinishUploadRequest,
    session_store: Annotated[SessionStore, Depends(get_session_store)],
    _: Annotated[None, Depends(verify_api_token)],
):
    session = session_store.finish(payload.session_id)
    missing = sorted(set(session.files) - set(session.uploaded))
    return {
        "session_id": session.id,
        "uploaded_count": len(session.uploaded),
        "missing_count": len(missing),
        "missing": missing,
        "keys": session.uploaded,
    }


@app.get("/api/download")
def download_object(
    s3: Annotated[S3Service, Depends(get_s3)],
    _: Annotated[None, Depends(verify_api_token)],
    key: str,
):
    safe_key = clean_relative_path(key)
    obj = s3.get_object_stream(safe_key)
    if obj is None:
        raise HTTPException(status_code=404, detail="Object not found")

    file_name = Path(safe_key).name or "download"
    return StreamingResponse(
        stream_s3_body(obj["Body"]),
        media_type=obj.get("ContentType") or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{file_name}"'},
    )


@app.get("/api/read-text")
def read_text_object(
    s3: Annotated[S3Service, Depends(get_s3)],
    _: Annotated[None, Depends(verify_api_token)],
    key: str,
):
    safe_key = clean_relative_path(key)
    obj = s3.get_object_stream(safe_key)
    if obj is None:
        raise HTTPException(status_code=404, detail="Object not found")

    content_type = obj.get("ContentType") or ""
    body = obj["Body"].read(2 * 1024 * 1024 + 1)
    if len(body) > 2 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Text preview is limited to 2 MB")

    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=415, detail="Object is not UTF-8 text") from exc

    return {"key": safe_key, "content_type": content_type, "text": text}


@app.delete("/api/object")
def delete_object(
    s3: Annotated[S3Service, Depends(get_s3)],
    _: Annotated[None, Depends(verify_api_token)],
    key: str,
):
    safe_key = clean_relative_path(key)
    s3.delete_object(safe_key)
    return {"key": safe_key, "deleted": True}


def stream_s3_body(body):
    try:
        while True:
            chunk = body.read(1024 * 1024)
            if not chunk:
                break
            yield chunk
    finally:
        body.close()


if FRONTEND_DIR.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIR), name="assets")
