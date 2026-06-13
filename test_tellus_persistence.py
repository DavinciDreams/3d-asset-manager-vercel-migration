import os
import io

os.environ.setdefault("SQLITE_PATH", ":memory:")
os.environ.setdefault("TELLUS_PERSISTENCE_API_TOKEN", "test-token")
os.environ.setdefault("ASSET_MANAGER_API_TOKEN", "test-token")

from app import create_app
from app.models import Model3D, User


def _ensure_user(username):
    user = User.get_by_username(username)
    if user:
        return user
    user = User(username=username, email=f"{username}@example.com")
    user.set_password("pw123456")
    return user.save()


def test_service_token_lists_private_worlds():
    app = create_app()
    client = app.test_client()
    headers = {"Authorization": "Bearer test-token"}

    put = client.put("/api/tellus/worlds/private-demo/state", json={"name": "Private Demo"}, headers=headers)
    assert put.status_code == 200, put.get_json()
    assert put.get_json()["is_public"] is False

    get = client.get("/api/tellus/worlds/private-demo/state", headers=headers)
    assert get.status_code == 200, get.get_json()

    listed = client.get("/api/tellus/worlds", headers=headers)
    assert listed.status_code == 200, listed.get_json()
    world_ids = [world["worldId"] for world in listed.get_json()["worlds"]]
    assert "private-demo" in world_ids


def test_public_world_list_stays_public_only_without_token():
    app = create_app()
    client = app.test_client()
    headers = {"Authorization": "Bearer test-token"}

    client.put("/api/tellus/worlds/private-demo/state", json={"name": "Private Demo"}, headers=headers)
    listed = client.get("/api/tellus/worlds")
    assert listed.status_code == 200, listed.get_json()
    assert listed.get_json()["worlds"] == []


def test_service_token_can_persist_world_for_target_owner():
    app = create_app()
    client = app.test_client()

    with app.app_context():
        rsafier = _ensure_user("rsafier")

    headers = {
        "Authorization": "Bearer test-token",
        "X-Asset-Username": "rsafier",
    }
    put = client.put(
        "/api/tellus/worlds/rsafier-private/state",
        json={"name": "Rsafier Private", "is_public": False},
        headers=headers,
    )
    assert put.status_code == 200, put.get_json()
    assert put.get_json()["owner"]["id"] == rsafier.id

    listed = client.get("/api/tellus/worlds", headers=headers)
    assert listed.status_code == 200, listed.get_json()
    worlds = {world["worldId"]: world for world in listed.get_json()["worlds"]}
    assert worlds["rsafier-private"]["owner"]["id"] == rsafier.id


def test_world_save_tags_referenced_assets_with_tellus_world():
    app = create_app()
    client = app.test_client()
    headers = {"Authorization": "Bearer test-token"}

    upload = client.post("/api/upload", headers=headers, data={
        "is_public": "false",
        "file": (io.BytesIO(b"glTF-world-link" + b"\x00" * 64), "world_prop.glb"),
    }, content_type="multipart/form-data")
    assert upload.status_code == 201, upload.get_json()
    model_id = upload.get_json()["model"]["id"]
    assert "tellus-world-forest-hub" not in upload.get_json()["model"]["tags"]

    put = client.put(
        "/api/tellus/worlds/forest-hub/state",
        json={
            "name": "Forest Hub",
            "objects": [
                {"id": "placed-1", "assetId": model_id},
            ],
        },
        headers=headers,
    )
    assert put.status_code == 200, put.get_json()

    with app.app_context():
        model = Model3D.get_by_id(model_id)
    assert {"tellus", "tellus-world-forest-hub"}.issubset(set(model.tags))
