from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Optional

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError, NoCredentialsError, PartialCredentialsError, ProfileNotFound
from fastapi import HTTPException

from .config import Settings


@dataclass(frozen=True)
class S3Entry:
    key: str
    name: str
    type: str
    size: Optional[int] = None
    last_modified: Optional[str] = None


def create_s3_client(settings: Settings):
    session_kwargs = {"region_name": settings.aws_region}
    if settings.aws_profile:
        session_kwargs["profile_name"] = settings.aws_profile
    return boto3.Session(**session_kwargs).client("s3", config=Config(signature_version="s3v4"))


class S3Service:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._client = None

    @property
    def client(self):
        self._require_bucket()
        if self._client is None:
            try:
                self._client = create_s3_client(self.settings)
            except ProfileNotFound as exc:
                raise HTTPException(
                    status_code=401,
                    detail=f"AWS profile not found: {self.settings.aws_profile}",
                ) from exc
        return self._client

    def make_key(self, relative_path: str, destination_prefix: str = "") -> str:
        pieces = [
            self.settings.normalized_base_prefix.strip("/"),
            destination_prefix.strip("/"),
            relative_path.strip("/"),
        ]
        return "/".join(piece for piece in pieces if piece)

    def list_prefix(self, prefix: str = "") -> list[S3Entry]:
        self._require_bucket()
        normalized_prefix = self._normalized_prefix(prefix)

        paginator = self.client.get_paginator("list_objects_v2")
        entries: list[S3Entry] = []

        try:
            for page in paginator.paginate(
                Bucket=self.settings.s3_bucket,
                Prefix=normalized_prefix,
                Delimiter="/",
            ):
                for item in page.get("CommonPrefixes", []):
                    key = item["Prefix"]
                    if self._is_control_key(key):
                        continue
                    entries.append(
                        S3Entry(
                            key=key,
                            name=_display_name(key),
                            type="folder",
                        )
                    )

                for item in page.get("Contents", []):
                    key = item["Key"]
                    if key == normalized_prefix or self._is_control_key(key):
                        continue
                    entries.append(
                        S3Entry(
                            key=key,
                            name=_display_name(key),
                            type="file",
                            size=item.get("Size"),
                            last_modified=item.get("LastModified").isoformat()
                            if item.get("LastModified")
                            else None,
                        )
                    )
        except (NoCredentialsError, PartialCredentialsError) as exc:
            raise missing_credentials_error() from exc
        except ClientError as exc:
            raise translate_client_error(exc) from exc

        return sorted(entries, key=lambda entry: (entry.type != "folder", entry.name.lower()))

    def list_recursive_keys(self, prefix: str = "") -> list[str]:
        self._require_bucket()
        normalized_prefix = self._normalized_prefix(prefix)
        paginator = self.client.get_paginator("list_objects_v2")
        keys: list[str] = []

        try:
            for page in paginator.paginate(
                Bucket=self.settings.s3_bucket,
                Prefix=normalized_prefix,
            ):
                for item in page.get("Contents", []):
                    key = item["Key"]
                    if key == normalized_prefix or self._is_control_key(key):
                        continue
                    keys.append(key)
        except (NoCredentialsError, PartialCredentialsError) as exc:
            raise missing_credentials_error() from exc
        except ClientError as exc:
            raise translate_client_error(exc) from exc

        return sorted(keys)

    def archive_name_for_key(self, key: str, prefix: str = "") -> str:
        normalized_prefix = self._normalized_prefix(prefix)
        if normalized_prefix and key.startswith(normalized_prefix):
            return key[len(normalized_prefix) :]

        base_prefix = self.settings.normalized_base_prefix
        if base_prefix and key.startswith(base_prefix):
            return key[len(base_prefix) :]
        return Path(key).name

    def upload_fileobj(self, fileobj: BinaryIO, key: str, content_type: Optional[str] = None) -> None:
        self._require_bucket()
        extra_args = {}
        if content_type:
            extra_args["ContentType"] = content_type

        try:
            self.client.upload_fileobj(
                fileobj,
                self.settings.s3_bucket,
                key,
                ExtraArgs=extra_args or None,
            )
        except (NoCredentialsError, PartialCredentialsError) as exc:
            raise missing_credentials_error() from exc
        except ClientError as exc:
            raise translate_client_error(exc) from exc

    def get_object_stream(self, key: str):
        self._require_bucket()
        try:
            return self.client.get_object(Bucket=self.settings.s3_bucket, Key=key)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in {"NoSuchKey", "404"}:
                return None
            raise translate_client_error(exc) from exc

    def delete_object(self, key: str) -> None:
        self._require_bucket()
        try:
            self.client.delete_object(Bucket=self.settings.s3_bucket, Key=key)
        except (NoCredentialsError, PartialCredentialsError) as exc:
            raise missing_credentials_error() from exc
        except ClientError as exc:
            raise translate_client_error(exc) from exc

    def delete_objects(self, keys: list[str]) -> None:
        self._require_bucket()
        if not keys:
            return

        try:
            for index in range(0, len(keys), 1000):
                batch = keys[index : index + 1000]
                self.client.delete_objects(
                    Bucket=self.settings.s3_bucket,
                    Delete={"Objects": [{"Key": key} for key in batch], "Quiet": True},
                )
        except (NoCredentialsError, PartialCredentialsError) as exc:
            raise missing_credentials_error() from exc
        except ClientError as exc:
            raise translate_client_error(exc) from exc

    def _require_bucket(self) -> None:
        if not self.settings.s3_bucket:
            raise HTTPException(status_code=500, detail="S3_BUCKET is not configured")

    def _normalized_prefix(self, prefix: str = "") -> str:
        normalized_prefix = self.make_key(prefix) if prefix else self.settings.normalized_base_prefix
        if normalized_prefix and not normalized_prefix.endswith("/"):
            normalized_prefix += "/"
        return normalized_prefix

    def _is_control_key(self, key: str) -> bool:
        session_prefix = self.settings.session_manifest_prefix.strip("/")
        if not session_prefix:
            return False
        control_root = session_prefix.split("/", 1)[0]
        pieces = [self.settings.normalized_base_prefix.strip("/"), control_root]
        prefix = "/".join(piece for piece in pieces if piece)
        return key == prefix or key.startswith(f"{prefix}/")


def _display_name(key: str) -> str:
    return key.rstrip("/").split("/")[-1]


def missing_credentials_error() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail=(
            "AWS credentials not found. Set AWS_PROFILE in backend/.env or export "
            "AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY before starting uvicorn."
        ),
    )


def translate_client_error(exc: ClientError) -> HTTPException:
    code = exc.response.get("Error", {}).get("Code", "Unknown")
    message = exc.response.get("Error", {}).get("Message", "AWS S3 request failed")
    status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 500)
    if code in {"AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch"}:
        status = 403
    elif code in {"NoSuchBucket"}:
        status = 404
    return HTTPException(status_code=status, detail=f"S3 {code}: {message}")
