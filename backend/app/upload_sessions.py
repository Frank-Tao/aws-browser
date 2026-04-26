from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Optional
from uuid import uuid4

from fastapi import HTTPException


SESSION_TTL = timedelta(hours=6)


@dataclass
class ManifestFile:
    path: str
    size: int
    content_type: Optional[str] = None


@dataclass
class UploadSession:
    id: str
    destination_prefix: str
    files: dict[str, ManifestFile]
    uploaded: dict[str, str] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def expires_at(self) -> datetime:
        return self.created_at + SESSION_TTL


class UploadSessionStore:
    def __init__(self):
        self._sessions: dict[str, UploadSession] = {}

    def create(self, files: list[ManifestFile], destination_prefix: str) -> UploadSession:
        self.prune()
        session = UploadSession(
            id=str(uuid4()),
            destination_prefix=clean_relative_path(destination_prefix, allow_empty=True),
            files={file.path: file for file in files},
        )
        self._sessions[session.id] = session
        return session

    def get(self, session_id: str) -> UploadSession:
        self.prune()
        session = self._sessions.get(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Upload session not found or expired")
        return session

    def finish(self, session_id: str) -> UploadSession:
        session = self.get(session_id)
        self._sessions.pop(session_id, None)
        return session

    def prune(self) -> None:
        now = datetime.now(timezone.utc)
        expired = [sid for sid, session in self._sessions.items() if session.expires_at < now]
        for sid in expired:
            self._sessions.pop(sid, None)


def clean_relative_path(path: str, allow_empty: bool = False) -> str:
    value = (path or "").replace("\\", "/").strip()
    if not value:
        if allow_empty:
            return ""
        raise HTTPException(status_code=400, detail="Path is required")

    normalized = PurePosixPath(value)
    if normalized.is_absolute():
        raise HTTPException(status_code=400, detail="Absolute paths are not allowed")
    if ".." in normalized.parts:
        raise HTTPException(status_code=400, detail="Parent path traversal is not allowed")

    return str(normalized).strip("/")


def is_obsidian_path(path: str) -> bool:
    return ".obsidian" in PurePosixPath(path.replace("\\", "/")).parts
