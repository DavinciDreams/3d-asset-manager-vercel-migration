import io
import os

os.environ.setdefault("SQLITE_PATH", ":memory:")
os.environ.setdefault("ENABLE_CONVERSION", "0")
os.environ.setdefault("ASSET_MANAGER_API_TOKEN", "test-token")
os.environ.pop("OPENAI_API_KEY", None)

from app import create_app


def test_bearer_upload_enrich_approve_and_bundle():
    app = create_app()
    client = app.test_client()
    headers = {"Authorization": "Bearer test-token"}
    glb = b"glTF" + b"\x00" * 64

    unauth = client.post("/api/upload", data={
        "file": (io.BytesIO(glb), "crate.glb"),
    }, content_type="multipart/form-data")
    assert unauth.status_code == 401

    upload = client.post("/api/upload", headers=headers, data={
        "is_public": "false",
        "file": (io.BytesIO(glb), "warehouse_crate.glb"),
    }, content_type="multipart/form-data")
    assert upload.status_code == 201, upload.get_json()
    model_id = upload.get_json()["model"]["id"]

    enrich = client.post(
        f"/api/model/{model_id}/ai/autotag",
        headers=headers,
        json={"overwrite": False, "context": {"collection": "warehouse props"}},
    )
    assert enrich.status_code == 200, enrich.get_json()
    body = enrich.get_json()
    assert body["model"]["ai_status"] == "done"
    assert "glb" in body["model"]["tags"]

    approval = client.patch(
        f"/api/model/{model_id}/approval",
        headers=headers,
        json={
            "approve_game_ready": True,
            "approve_asset_store": True,
            "approval_notes": "Looks clean enough for the first store pass.",
        },
    )
    assert approval.status_code == 200, approval.get_json()
    assert approval.get_json()["model"]["approve_game_ready"] is True
    assert approval.get_json()["model"]["approve_asset_store"] is True

    bundle = client.post("/api/bundles", headers=headers, json={
        "name": "Warehouse Starter Bundle",
        "description": "A small bundle for smoke testing asset packaging.",
        "model_ids": [model_id],
        "tags": ["warehouse", "props"],
        "create_zip": True,
    })
    assert bundle.status_code == 201, bundle.get_json()
    bundle_body = bundle.get_json()["bundle"]
    assert bundle_body["has_file"] is True
    assert bundle_body["metadata"]["approve_game_ready"] is True
    assert bundle_body["metadata"]["approve_asset_store"] is True

    download = client.get(f"/api/bundles/{bundle_body['id']}/download", headers=headers)
    assert download.status_code == 200
    assert download.content_type == "application/zip"
    assert download.data.startswith(b"PK")


def test_openapi_documents_workflow_and_bearer_auth():
    app = create_app()
    spec = app.test_client().get("/api/openapi.json").get_json()
    assert "bearerAuth" in spec["components"]["securitySchemes"]
    assert "/model/{model_id}/ai/autotag" in spec["paths"]
    assert "/model/{model_id}/approval" in spec["paths"]
    assert "/bundles" in spec["paths"]
