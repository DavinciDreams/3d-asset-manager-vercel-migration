import io
import os
import json
import struct

os.environ.setdefault("SQLITE_PATH", ":memory:")
os.environ.setdefault("AI_AUTOTAG_WORKER", "0")
os.environ.setdefault("AI_AUTOTAG_KICK_ON_REQUEST", "0")

from app import create_app
from app.models import Model3D, User


def _login(app, client, username="profileuser", email="profile@example.com"):
    with app.app_context():
        user = User(username=username, email=email)
        user.set_password("pw123456")
        user.save()
    client.post("/auth/login", data={"login_field": username, "password": "pw123456"})


def test_profile_counts_uploaded_models():
    app = create_app()
    client = app.test_client()
    _login(app, client)

    model_ids = []
    for name in ["One", "Two", "Three"]:
        glb = b"glTF" + name.encode("utf-8") + b"\x00" * 64
        response = client.post("/api/upload", data={
            "name": name,
            "is_public": "true",
            "file": (io.BytesIO(glb), f"{name.lower()}.glb"),
        }, content_type="multipart/form-data")
        assert response.status_code == 201, response.get_json()
        model_ids.append(response.get_json()["model"]["id"])

    profile = client.get("/profile")
    assert profile.status_code == 200
    html = profile.get_data(as_text=True)
    assert 'class="text-3xl font-bold text-blue-600">3</div>' in html
    assert "One" in html
    assert "Two" in html
    assert "Three" in html

    detail = client.get(f"/model/{model_ids[0]}")
    assert detail.status_code == 200
    detail_html = detail.get_data(as_text=True)
    assert "Enrich Metadata" in detail_html
    assert "Catalog Metadata" in detail_html
    assert "/api/model/" in detail_html and "/ai/autotag" in detail_html


def test_asset_admin_can_manage_other_users_models(monkeypatch):
    monkeypatch.setenv("ASSET_MANAGER_ADMIN_USERNAMES", "storeadmin")
    app = create_app()
    client = app.test_client()

    with app.app_context():
        owner = User(username="assetowner", email="owner@example.com")
        owner.set_password("pw123456")
        owner.save()
        model = Model3D(
            name="Admin Owned Cleanup Target",
            description="Needs cleanup",
            original_filename="cleanup.glb",
            file_format="glb",
            file_size=72,
            gridfs_file_id="cleanup-file-id",
            user_id=owner.id,
            is_public=False,
        ).save()
        model_id = model.id

    _login(app, client, username="storeadmin", email="admin@example.com")

    detail = client.get(f"/model/{model_id}")
    assert detail.status_code == 200
    detail_html = detail.get_data(as_text=True)
    assert "Manage Model" in detail_html
    assert "Delete Model" in detail_html

    update = client.patch(f"/api/model/{model_id}", json={"name": "Cleaned Up Asset"})
    assert update.status_code == 200, update.get_json()
    assert update.get_json()["model"]["name"] == "Cleaned Up Asset"

    delete = client.delete(f"/api/model/{model_id}")
    assert delete.status_code == 200, delete.get_json()
    with app.app_context():
        assert Model3D.get_by_id(model_id) is None


def test_asset_admin_sees_delete_on_public_cards(monkeypatch):
    monkeypatch.setenv("ASSET_MANAGER_ADMIN_USERNAMES", "storeadmin")
    app = create_app()
    client = app.test_client()

    with app.app_context():
        owner = User(username="publicowner", email="public-owner@example.com")
        owner.set_password("pw123456")
        owner.save()
        model = Model3D(
            name="Public Cleanup Target",
            description="Should be removable from public pages",
            original_filename="public-cleanup.glb",
            file_format="glb",
            file_size=72,
            gridfs_file_id="public-cleanup-file-id",
            user_id=owner.id,
            is_public=True,
        ).save()
        model_id = model.id

    _login(app, client, username="storeadmin", email="admin@example.com")

    browse = client.get("/browse")
    assert browse.status_code == 200
    browse_html = browse.get_data(as_text=True)
    assert f'data-model-id="{model_id}"' in browse_html
    assert "data-admin-delete" in browse_html

    home = client.get("/")
    assert home.status_code == 200
    home_html = home.get_data(as_text=True)
    assert f'data-model-id="{model_id}"' in home_html
    assert "data-admin-delete" in home_html


def test_browse_prefers_cached_preview_video_over_live_viewer():
    app = create_app()
    client = app.test_client()

    with app.app_context():
        owner = User(username="previewowner", email="preview-owner@example.com")
        owner.set_password("pw123456")
        owner.save()
        model = Model3D(
            name="Preview Video Target",
            description="Uses cached video on browse",
            original_filename="preview.glb",
            file_format="glb",
            file_size=72,
            gridfs_file_id="preview-file-id",
            thumbnail_file_id="preview-thumb-id",
            preview_file_id="preview-video-id",
            user_id=owner.id,
            is_public=True,
        ).save()
        model_id = model.id

    browse = client.get("/browse")
    assert browse.status_code == 200
    html = browse.get_data(as_text=True)
    assert f'data-model-id="{model_id}"' in html
    assert f'data-preview-src="/api/model/{model_id}/preview"' in html
    assert f'data-view-src="/api/model/{model_id}' not in html


def test_detail_defaults_auto_capture_missing_media():
    app = create_app()
    client = app.test_client()
    _login(app, client)

    glb = b"glTF" + b"\x00" * 64
    response = client.post("/api/upload", data={
        "name": "Auto Capture Target",
        "is_public": "true",
        "file": (io.BytesIO(glb), "auto-capture.glb"),
    }, content_type="multipart/form-data")
    assert response.status_code == 201, response.get_json()
    model_id = response.get_json()["model"]["id"]

    detail = client.get(f"/model/{model_id}")
    assert detail.status_code == 200
    html = detail.get_data(as_text=True)
    assert "const AUTO_CAPTURE_THUMBNAIL = true;" in html
    assert "const AUTO_CAPTURE_PREVIEW = true;" in html
    assert "AUTO_CAPTURE_THUMBNAIL && !HAS_THUMBNAIL" in html
    assert "AUTO_CAPTURE_PREVIEW && !HAS_PREVIEW" in html


def test_api_me_reports_asset_admin_state(monkeypatch):
    monkeypatch.setenv("ASSET_MANAGER_ADMIN_EMAILS", "admin@example.com")
    app = create_app()
    client = app.test_client()

    anonymous = client.get("/api/me")
    assert anonymous.status_code == 200
    assert anonymous.get_json()["authenticated"] is False
    assert anonymous.get_json()["is_asset_admin"] is False

    _login(app, client, username="storeadmin2", email="admin@example.com")

    response = client.get("/api/me")
    assert response.status_code == 200
    body = response.get_json()
    assert body["authenticated"] is True
    assert body["username"] == "storeadmin2"
    assert body["email"] == "admin@example.com"
    assert body["is_asset_admin"] is True
    assert body["asset_admin_configured"] is True


def test_tellus_admin_username_grants_asset_admin(monkeypatch):
    monkeypatch.setenv("TELLUS_ADMIN_USERNAME", "lisa")
    app = create_app()
    client = app.test_client()

    _login(app, client, username="lisa", email="lisa@example.com")

    response = client.get("/api/me")
    assert response.status_code == 200
    body = response.get_json()
    assert body["is_asset_admin"] is True
    assert body["asset_admin_configured"] is True


def test_fbx_export_visible_for_optimized_glb():
    app = create_app()
    client = app.test_client()
    _login(app, client)

    gltf = {
        "asset": {"version": "2.0"},
        "extensionsUsed": ["EXT_meshopt_compression"],
        "extensionsRequired": ["EXT_meshopt_compression"],
        "buffers": [{"byteLength": 0}],
    }
    chunk = json.dumps(gltf).encode("utf-8")
    chunk += b" " * ((4 - (len(chunk) % 4)) % 4)
    glb = (
        struct.pack("<4sII", b"glTF", 2, 12 + 8 + len(chunk))
        + struct.pack("<II", len(chunk), 0x4E4F534A)
        + chunk
    )
    response = client.post("/api/upload", data={
        "name": "Optimized Export Target",
        "is_public": "true",
        "file": (io.BytesIO(glb), "optimized.glb"),
    }, content_type="multipart/form-data")
    assert response.status_code == 201, response.get_json()
    model_id = response.get_json()["model"]["id"]

    detail = client.get(f"/model/{model_id}")
    assert detail.status_code == 200
    html = detail.get_data(as_text=True)
    assert f"/api/export/{model_id}?format=fbx" in html
