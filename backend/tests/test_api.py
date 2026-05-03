import importlib
import io
import zipfile


def load_test_client(monkeypatch):
    monkeypatch.setenv("S3_BUCKET", "")
    monkeypatch.setenv("UPLOAD_SESSION_STORE", "memory")

    config = importlib.import_module("backend.app.config")
    config.get_settings.cache_clear()

    main = importlib.import_module("backend.app.main")
    from fastapi.testclient import TestClient

    return TestClient(main.app)


def test_health_uses_memory_store_when_bucket_is_not_configured(monkeypatch):
    client = load_test_client(monkeypatch)

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["configured"] is False
    assert response.json()["session_store"] == "memory"


def test_manifest_skips_obsidian_and_finish_reports_missing(monkeypatch):
    client = load_test_client(monkeypatch)
    client.app.dependency_overrides = {}
    monkeypatch.setenv("S3_BUCKET", "test-bucket")
    monkeypatch.setenv("UPLOAD_SESSION_STORE", "memory")
    import backend.app.config as config

    config.get_settings.cache_clear()

    manifest_response = client.post(
        "/api/upload-manifest",
        json={
            "destination_prefix": "test",
            "ignore_obsidian": True,
            "files": [
                {"path": "notes/a.md", "size": 12, "content_type": "text/markdown"},
                {"path": "notes/.obsidian/workspace.json", "size": 3},
            ],
        },
    )

    assert manifest_response.status_code == 200
    manifest = manifest_response.json()
    assert manifest["total_files"] == 1
    assert manifest["accepted"][0]["path"] == "notes/a.md"
    assert manifest["skipped"][0]["reason"] == ".obsidian ignored"

    finish_response = client.post(
        "/api/finish-upload",
        json={"session_id": manifest["session_id"]},
    )

    assert finish_response.status_code == 200
    assert finish_response.json()["missing"] == ["notes/a.md"]


def test_list_requires_bucket(monkeypatch):
    client = load_test_client(monkeypatch)

    response = client.get("/api/list")

    assert response.status_code == 500
    assert response.json()["detail"] == "S3_BUCKET is not configured"


def test_manifest_requires_bucket_before_upload(monkeypatch):
    client = load_test_client(monkeypatch)

    response = client.post(
        "/api/upload-manifest",
        json={
            "destination_prefix": "test",
            "ignore_obsidian": True,
            "files": [{"path": "notes/a.md", "size": 12}],
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "S3_BUCKET is required before uploading files"


def test_list_reports_missing_aws_credentials(monkeypatch):
    client = load_test_client(monkeypatch)
    monkeypatch.setenv("S3_BUCKET", "test-bucket")
    monkeypatch.setenv("UPLOAD_SESSION_STORE", "memory")

    import backend.app.config as config

    config.get_settings.cache_clear()

    from botocore.exceptions import NoCredentialsError
    from backend.app import s3_service

    class FakePaginator:
        def paginate(self, **kwargs):
            raise NoCredentialsError()

    class FakeClient:
        def get_paginator(self, name):
            return FakePaginator()

    monkeypatch.setattr(s3_service, "create_s3_client", lambda settings: FakeClient())

    response = client.get("/api/list")

    assert response.status_code == 401
    assert "AWS credentials not found" in response.json()["detail"]


def test_upload_file_json_restores_file_content(monkeypatch):
    client = load_test_client(monkeypatch)
    monkeypatch.setenv("S3_BUCKET", "test-bucket")
    monkeypatch.setenv("UPLOAD_SESSION_STORE", "memory")
    import backend.app.config as config
    import backend.app.main as main

    config.get_settings.cache_clear()

    captured = {}

    class FakeS3:
        def make_key(self, relative_path, destination_prefix=""):
            pieces = [destination_prefix.strip("/"), relative_path.strip("/")]
            return "/".join(piece for piece in pieces if piece)

        def upload_fileobj(self, fileobj, key, content_type=None):
            captured["content"] = fileobj.read()
            captured["key"] = key
            captured["content_type"] = content_type

    client.app.dependency_overrides[main.get_s3] = lambda: FakeS3()
    try:
        manifest_response = client.post(
            "/api/upload-manifest",
            json={
                "destination_prefix": "notes",
                "ignore_obsidian": False,
                "files": [
                    {"path": "folder/a.txt", "size": 11, "content_type": "text/plain"},
                ],
            },
        )
        assert manifest_response.status_code == 200
        session_id = manifest_response.json()["session_id"]

        upload_response = client.post(
            "/api/post-raw-json",
            json={
                "session_id": session_id,
                "path": "folder/a.txt",
                "content_base64": "aGVsbG8gd29ybGQ=",
                "content_type": "text/plain",
            },
        )
        assert upload_response.status_code == 200
        assert captured["content"] == b"hello world"
        assert captured["key"] == "notes/folder/a.txt"
        assert captured["content_type"] == "text/plain"

        finish_response = client.post("/api/finish-upload", json={"session_id": session_id})
        assert finish_response.status_code == 200
        assert finish_response.json()["uploaded_count"] == 1
    finally:
        client.app.dependency_overrides = {}


def test_get_prefix_delete_preview_lists_files_under_folder(monkeypatch):
    client = load_test_client(monkeypatch)
    monkeypatch.setenv("S3_BUCKET", "test-bucket")
    monkeypatch.setenv("UPLOAD_SESSION_STORE", "memory")
    import backend.app.config as config
    import backend.app.main as main

    config.get_settings.cache_clear()

    class FakeS3:
        def list_recursive_keys(self, prefix=""):
            assert prefix == "notes"
            return ["notes/a.txt", "notes/sub/b.md"]

    client.app.dependency_overrides[main.get_s3] = lambda: FakeS3()
    try:
        response = client.get("/api/prefix", params={"prefix": "notes"})
        assert response.status_code == 200
        assert response.json() == {
            "prefix": "notes",
            "count": 2,
            "keys": ["notes/a.txt", "notes/sub/b.md"],
        }
    finally:
        client.app.dependency_overrides = {}


def test_delete_prefix_deletes_all_files_under_folder(monkeypatch):
    client = load_test_client(monkeypatch)
    monkeypatch.setenv("S3_BUCKET", "test-bucket")
    monkeypatch.setenv("UPLOAD_SESSION_STORE", "memory")
    import backend.app.config as config
    import backend.app.main as main

    config.get_settings.cache_clear()
    deleted = []

    class FakeS3:
        def list_recursive_keys(self, prefix=""):
            assert prefix == "notes"
            return ["notes/a.txt", "notes/sub/b.md"]

        def delete_objects(self, keys):
            deleted.extend(keys)

    client.app.dependency_overrides[main.get_s3] = lambda: FakeS3()
    try:
        response = client.delete("/api/prefix", params={"prefix": "notes"})
        assert response.status_code == 200
        assert response.json() == {
            "prefix": "notes",
            "deleted": True,
            "deleted_count": 2,
            "keys": ["notes/a.txt", "notes/sub/b.md"],
        }
        assert deleted == ["notes/a.txt", "notes/sub/b.md"]
    finally:
        client.app.dependency_overrides = {}


def test_delete_prefix_requires_existing_files(monkeypatch):
    client = load_test_client(monkeypatch)
    monkeypatch.setenv("S3_BUCKET", "test-bucket")
    monkeypatch.setenv("UPLOAD_SESSION_STORE", "memory")
    import backend.app.config as config
    import backend.app.main as main

    config.get_settings.cache_clear()

    class FakeS3:
        def list_recursive_keys(self, prefix=""):
            assert prefix == "empty"
            return []

        def delete_objects(self, keys):
            raise AssertionError("delete_objects should not be called for empty prefixes")

    client.app.dependency_overrides[main.get_s3] = lambda: FakeS3()
    try:
        response = client.delete("/api/prefix", params={"prefix": "empty"})
        assert response.status_code == 404
        assert response.json()["detail"] == "No files found in this prefix"
    finally:
        client.app.dependency_overrides = {}


def test_download_prefix_returns_zip(monkeypatch):
    client = load_test_client(monkeypatch)
    monkeypatch.setenv("S3_BUCKET", "test-bucket")
    monkeypatch.setenv("UPLOAD_SESSION_STORE", "memory")
    import backend.app.config as config
    import backend.app.main as main

    config.get_settings.cache_clear()

    file_data = {
        "notes/a.txt": b"hello",
        "notes/sub/b.md": b"# title\n",
    }

    class FakeS3:
        def list_recursive_keys(self, prefix=""):
            if prefix != "notes":
                return []
            return sorted(file_data.keys())

        def archive_name_for_key(self, key, prefix=""):
            return key[len(prefix.rstrip("/") + "/") :] if prefix else key

        def get_object_stream(self, key):
            content = file_data.get(key)
            if content is None:
                return None
            return {"Body": io.BytesIO(content)}

    client.app.dependency_overrides[main.get_s3] = lambda: FakeS3()
    try:
        response = client.get("/api/download-prefix", params={"prefix": "notes"})
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/zip"

        archive = zipfile.ZipFile(io.BytesIO(response.content))
        assert sorted(archive.namelist()) == ["a.txt", "sub/b.md"]
        assert archive.read("a.txt") == b"hello"
        assert archive.read("sub/b.md") == b"# title\n"
    finally:
        client.app.dependency_overrides = {}
