import importlib


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
