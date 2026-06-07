import os

os.environ.setdefault("SQLITE_PATH", ":memory:")
os.environ.setdefault("TELLUS_PERSISTENCE_API_TOKEN", "test-token")

from app import create_app


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
