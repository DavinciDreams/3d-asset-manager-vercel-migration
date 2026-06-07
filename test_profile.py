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

    glb = b"glTF" + b"\x00" * 64
    for name in ["One", "Two", "Three"]:
        response = client.post("/api/upload", data={
            "name": name,
            "is_public": "true",
            "file": (io.BytesIO(glb), f"{name.lower()}.glb"),
        }, content_type="multipart/form-data")
        assert response.status_code == 201, response.get_json()

    profile = client.get("/profile")
    assert profile.status_code == 200
    html = profile.get_data(as_text=True)
    assert 'class="text-3xl font-bold text-blue-600">3</div>' in html
    assert "One" in html
    assert "Two" in html
    assert "Three" in html
