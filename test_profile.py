import io
import os

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
