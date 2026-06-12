import io
import json
import os

os.environ.setdefault("SQLITE_PATH", ":memory:")
os.environ.setdefault("ENABLE_CONVERSION", "0")
os.environ.setdefault("ASSET_MANAGER_API_TOKEN", "test-token")
os.environ["AI_AUTOTAG_ON_UPLOAD"] = "0"
os.environ.pop("OPENAI_API_KEY", None)
os.environ.pop("AI_AUTOTAG_API_KEY", None)
os.environ.pop("AI_API_KEY", None)
os.environ.pop("HYADES_API_KEY", None)
os.environ.pop("HYADES_VISION_API_KEY", None)
os.environ.pop("ZAI_API_KEY", None)

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
        "asset_category": "prop",
        "asset_styles": "fantasy, stylized",
        "asset_types": "rigged, animated",
        "file": (io.BytesIO(glb), "warehouse_crate.glb"),
    }, content_type="multipart/form-data")
    assert upload.status_code == 201, upload.get_json()
    model_id = upload.get_json()["model"]["id"]
    assert upload.get_json()["model"]["asset_category"] == "prop"
    assert upload.get_json()["model"]["asset_styles"] == ["fantasy", "stylized"]
    assert upload.get_json()["model"]["asset_types"] == ["rigged", "animated"]
    assert len(upload.get_json()["model"]["content_hash"]) == 64

    duplicate = client.post("/api/upload", headers=headers, data={
        "is_public": "false",
        "file": (io.BytesIO(glb), "warehouse_crate_copy.glb"),
    }, content_type="multipart/form-data")
    assert duplicate.status_code == 409, duplicate.get_json()
    assert "duplicate model" in duplicate.get_json()["error"].lower()

    enrich = client.post(
        f"/api/model/{model_id}/ai/autotag",
        headers=headers,
        json={"overwrite": False, "context": {"collection": "warehouse props"}},
    )
    assert enrich.status_code == 200, enrich.get_json()
    body = enrich.get_json()
    assert body["model"]["ai_status"] == "done"
    assert body["model"]["ai_title"] == "Warehouse Crate"
    assert body["model"]["asset_category"] == "prop"
    assert body["model"]["asset_styles"] == ["fantasy", "stylized"]
    assert body["model"]["asset_types"] == ["rigged", "animated"]
    assert "glb" in body["model"]["tags"]

    update = client.put(
        f"/api/model/{model_id}",
        headers=headers,
        json={
            "name": body["model"]["name"],
            "asset_category": "building",
            "asset_styles": "fantasy, medieval",
            "asset_types": "modular, game-ready",
        },
    )
    assert update.status_code == 200, update.get_json()
    updated_model = update.get_json()["model"]
    assert updated_model["asset_category"] == "building"
    assert updated_model["asset_styles"] == ["fantasy", "medieval"]
    assert updated_model["asset_types"] == ["modular", "game-ready"]

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
    props = spec["paths"]["/model/{model_id}/ai/autotag"]["post"]["requestBody"]["content"]["application/json"]["schema"]["properties"]
    assert "include_title" in props
    model_props = spec["components"]["schemas"]["ModelSummary"]["properties"]
    assert "asset_category" in model_props
    assert "asset_styles" in model_props
    assert "asset_types" in model_props
    assert "content_hash" in model_props


def test_hyades_a2a_enrichment_uses_holo_vision(monkeypatch):
    from app import ai_enrichment

    captured = {}
    output = {
        "title": "Moonlit Shrine",
        "asset_category": "building",
        "asset_styles": ["fantasy"],
        "asset_types": ["game-ready"],
        "tags": ["shrine", "fantasy", "stone"],
        "description": "A fantasy shrine asset with a moonlit stone structure.",
        "summary": "Fantasy shrine asset.",
        "categories": ["environment"],
        "quality_notes": [],
    }

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({
                "jsonrpc": "2.0",
                "id": "response-1",
                "result": {
                    "task": {
                        "artifacts": [
                            {"parts": [{"text": json.dumps(output)}]},
                        ],
                    },
                },
            }).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeResponse()

    class FakeModel:
        name = "moon_shrine"
        description = ""
        original_filename = "moon_shrine.glb"
        file_format = "glb"
        file_size = 1234
        tags = []
        asset_category = None
        asset_styles = []
        asset_types = []
        approve_game_ready = False
        approve_asset_store = False
        conversion_status = None
        thumbnail_file_id = "thumb"

        def _read_stored_file(self, file_id):
            assert file_id == "thumb"
            return b"webp-thumbnail"

    monkeypatch.setenv("AI_AUTOTAG_PROVIDER", "hyades")
    monkeypatch.setenv("AI_AUTOTAG_API_KEY", "hyades-key")
    monkeypatch.delenv("AI_AUTOTAG_BASE_URL", raising=False)
    monkeypatch.delenv("AI_AUTOTAG_TRANSPORT", raising=False)
    monkeypatch.delenv("AI_AUTOTAG_MODEL", raising=False)
    monkeypatch.setattr(ai_enrichment.urllib.request, "urlopen", fake_urlopen)

    enriched = ai_enrichment.enrich_model(FakeModel())

    assert captured["url"] == "https://hyades.gnostr.cloud/a2a"
    assert captured["headers"]["Authorization"] == "Bearer hyades-key"
    assert captured["body"]["method"] == "message/send"
    assert captured["body"]["params"]["metadata"]["model"] == "holo"
    parts = captured["body"]["params"]["message"]["parts"]
    assert any(part.get("text") for part in parts)
    assert any(part.get("raw") for part in parts)
    assert enriched["provider"] == "hyades"
    assert enriched["transport"] == "a2a"
    assert enriched["asset_category"] == "building"
