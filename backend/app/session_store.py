from __future__ import annotations

import json
from datetime import datetime, timezone
from urllib.parse import quote
from uuid import UUID, uuid4

from botocore.exceptions import ClientError
from fastapi import HTTPException

from .config import Settings
from .s3_service import create_s3_client
from .upload_sessions import ManifestFile, UploadSession, UploadSessionStore, clean_relative_path


class SessionStore:
    def create(self, files: list[ManifestFile], destination_prefix: str) -> UploadSession:
        raise NotImplementedError

    def get(self, session_id: str) -> UploadSession:
        raise NotImplementedError

    def save_uploaded(self, session: UploadSession, path: str, key: str) -> None:
        raise NotImplementedError

    def finish(self, session_id: str) -> UploadSession:
        raise NotImplementedError


class MemorySessionStore(SessionStore):
    def __init__(self):
        self._store = UploadSessionStore()

    def create(self, files: list[ManifestFile], destination_prefix: str) -> UploadSession:
        return self._store.create(files, destination_prefix)

    def get(self, session_id: str) -> UploadSession:
        return self._store.get(session_id)

    def save_uploaded(self, session: UploadSession, path: str, key: str) -> None:
        session.uploaded[path] = key

    def finish(self, session_id: str) -> UploadSession:
        return self._store.finish(session_id)


class S3ManifestSessionStore(SessionStore):
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = create_s3_client(settings)

    def create(self, files: list[ManifestFile], destination_prefix: str) -> UploadSession:
        self._require_bucket()
        session = UploadSession(
            id=str(uuid4()),
            destination_prefix=clean_relative_path(destination_prefix, allow_empty=True),
            files={file.path: file for file in files},
        )
        self.client.put_object(
            Bucket=self.settings.s3_bucket,
            Key=self._manifest_key(session.id),
            Body=json.dumps(self._to_manifest(session), separators=(",", ":")).encode("utf-8"),
            ContentType="application/json",
        )
        return session

    def get(self, session_id: str) -> UploadSession:
        session = self._load_manifest(session_id)
        self._raise_if_expired(session)
        return session

    def save_uploaded(self, session: UploadSession, path: str, key: str) -> None:
        marker = {"path": path, "key": key, "uploaded_at": datetime.now(timezone.utc).isoformat()}
        self.client.put_object(
            Bucket=self.settings.s3_bucket,
            Key=self._uploaded_marker_key(session.id, path),
            Body=json.dumps(marker, separators=(",", ":")).encode("utf-8"),
            ContentType="application/json",
        )
        session.uploaded[path] = key

    def finish(self, session_id: str) -> UploadSession:
        session = self._load_manifest(session_id)
        self._raise_if_expired(session)
        session.uploaded = self._load_uploaded_markers(session_id)
        self._delete_session_objects(session_id)
        return session

    def _load_manifest(self, session_id: str) -> UploadSession:
        self._require_bucket()
        try:
            response = self.client.get_object(
                Bucket=self.settings.s3_bucket,
                Key=self._manifest_key(session_id),
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in {"NoSuchKey", "404"}:
                raise HTTPException(status_code=404, detail="Upload session not found or expired")
            raise

        payload = json.loads(response["Body"].read().decode("utf-8"))
        return self._from_manifest(payload)

    def _load_uploaded_markers(self, session_id: str) -> dict[str, str]:
        uploaded: dict[str, str] = {}
        prefix = self._uploaded_prefix(session_id)
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.settings.s3_bucket, Prefix=prefix):
            for item in page.get("Contents", []):
                response = self.client.get_object(Bucket=self.settings.s3_bucket, Key=item["Key"])
                marker = json.loads(response["Body"].read().decode("utf-8"))
                uploaded[marker["path"]] = marker["key"]
        return uploaded

    def _delete_session_objects(self, session_id: str) -> None:
        keys = [self._manifest_key(session_id)]
        prefix = self._session_prefix(session_id)
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.settings.s3_bucket, Prefix=prefix):
            keys.extend(item["Key"] for item in page.get("Contents", []))

        keys = sorted(set(keys))
        for index in range(0, len(keys), 1000):
            self.client.delete_objects(
                Bucket=self.settings.s3_bucket,
                Delete={"Objects": [{"Key": key} for key in keys[index : index + 1000]]},
            )

    def _to_manifest(self, session: UploadSession) -> dict:
        return {
            "session_id": session.id,
            "destination_prefix": session.destination_prefix,
            "files": {
                path: {
                    "path": file.path,
                    "size": file.size,
                    "content_type": file.content_type or "",
                }
                for path, file in session.files.items()
            },
            "created_at": session.created_at.isoformat(),
            "expires_at": session.expires_at.isoformat(),
        }

    def _from_manifest(self, payload: dict) -> UploadSession:
        files = {
            path: ManifestFile(
                path=value["path"],
                size=int(value["size"]),
                content_type=value.get("content_type") or None,
            )
            for path, value in payload.get("files", {}).items()
        }
        created_at = datetime.fromisoformat(payload["created_at"])
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        return UploadSession(
            id=payload["session_id"],
            destination_prefix=payload.get("destination_prefix", ""),
            files=files,
            uploaded={},
            created_at=created_at,
        )

    def _raise_if_expired(self, session: UploadSession) -> None:
        if session.expires_at < datetime.now(timezone.utc):
            self._delete_session_objects(session.id)
            raise HTTPException(status_code=404, detail="Upload session not found or expired")

    def _session_prefix(self, session_id: str) -> str:
        safe_session_id = self._safe_session_id(session_id)
        prefix = self.settings.session_manifest_prefix.strip("/")
        pieces = [self.settings.normalized_base_prefix.strip("/"), prefix, safe_session_id]
        return "/".join(piece for piece in pieces if piece)

    def _manifest_key(self, session_id: str) -> str:
        return f"{self._session_prefix(session_id)}/manifest.json"

    def _uploaded_prefix(self, session_id: str) -> str:
        return f"{self._session_prefix(session_id)}/uploaded/"

    def _uploaded_marker_key(self, session_id: str, path: str) -> str:
        return f"{self._uploaded_prefix(session_id)}{quote(path, safe='')}.json"

    def _require_bucket(self) -> None:
        if not self.settings.s3_bucket:
            raise HTTPException(status_code=500, detail="S3_BUCKET is not configured")

    def _safe_session_id(self, session_id: str) -> str:
        try:
            return str(UUID(session_id))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid upload session id") from exc


memory_store = MemorySessionStore()


def create_session_store(settings: Settings) -> SessionStore:
    store = settings.upload_session_store.strip().lower()
    if store == "memory" or (store == "auto" and not settings.s3_bucket):
        return memory_store
    if store in {"auto", "s3"}:
        return S3ManifestSessionStore(settings)
    raise HTTPException(status_code=500, detail="UPLOAD_SESSION_STORE must be auto, memory, or s3")
