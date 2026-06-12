import io
import os

os.environ.setdefault("SQLITE_PATH", ":memory:")

from app import create_app
from app.models import User


def _login(app, client):
    with app.app_context():
        user = User(username="profileuser", email="profile@example.com")
        user.set_password("pw123456")
        user.save()
    client.post("/auth/login", data={"login_field": "profileuser", "password": "pw123456"})


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
