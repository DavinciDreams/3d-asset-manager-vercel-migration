import io
import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("SQLITE_PATH", ":memory:")
os.environ.setdefault("ENABLE_CONVERSION", "0")
os.environ.setdefault("ASSET_MANAGER_API_TOKEN", "test-token")
os.environ["AI_AUTOTAG_ON_UPLOAD"] = "0"
os.environ["AI_AUTOTAG_WORKER"] = "0"
os.environ["AI_AUTOTAG_KICK_ON_REQUEST"] = "0"
os.environ.pop("OPENAI_API_KEY", None)
os.environ.pop("AI_AUTOTAG_API_KEY", None)
os.environ.pop("AI_API_KEY", None)
os.environ.pop("HYADES_API_KEY", None)
os.environ.pop("HYADES_VISION_API_KEY", None)
os.environ.pop("ZAI_API_KEY", None)
os.environ.pop("Z_AI_API_KEY", None)

from app import create_app
from app.models import Model3D, ModelVariant, User


def _ensure_user(username):
    user = User.get_by_username(username)
    if user:
        return user
    user = User(username=username, email=f"{username}@example.com")
    user.set_password("pw123456")
    return user.save()


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
        "runtime_metadata": json.dumps({
            "behaviors": ["placeable"],
            "light": {"enabled": False, "type": "none"},
        }),
        "file": (io.BytesIO(glb), "warehouse_crate.glb"),
    }, content_type="multipart/form-data")
    assert upload.status_code == 201, upload.get_json()
    model_id = upload.get_json()["model"]["id"]
    assert upload.get_json()["model"]["asset_category"] == "prop"
    assert upload.get_json()["model"]["asset_styles"] == ["fantasy", "stylized"]
    assert upload.get_json()["model"]["asset_types"] == ["rigged", "animated"]
    assert upload.get_json()["model"]["runtime_metadata"]["behaviors"] == ["placeable"]
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
    assert "glb" not in body["model"]["tags"]

    update = client.put(
        f"/api/model/{model_id}",
        headers=headers,
        json={
            "name": body["model"]["name"],
            "asset_category": "building",
            "asset_styles": "fantasy, medieval",
            "asset_types": "modular, game-ready",
            "runtime_metadata": {
                "behaviors": ["light-emitter"],
                "light": {
                    "enabled": True,
                    "type": "point",
                    "color": "#ffb35a",
                    "intensity": 1.5,
                    "range": 8,
                    "cast_shadow": True,
                    "attach_to": "",
                    "offset": [0, 0.6, 0],
                },
            },
        },
    )
    assert update.status_code == 200, update.get_json()
    updated_model = update.get_json()["model"]
    assert updated_model["asset_category"] == "building"
    assert updated_model["asset_styles"] == ["fantasy", "medieval"]
    assert updated_model["asset_types"] == ["modular", "game-ready"]
    assert updated_model["runtime_metadata"]["light"]["enabled"] is True
    assert "light-emitter" in updated_model["runtime_metadata"]["behaviors"]

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


def test_service_token_can_target_owners_search_private_metadata_and_dedupe_across_titles():
    app = create_app()
    client = app.test_client()

    with app.app_context():
        rsafier = _ensure_user("rsafier")
        lisa = _ensure_user("lisa")

    glb = b"glTF-service-targets" + b"\x00" * 64
    headers = {
        "Authorization": "Bearer test-token",
        "X-Asset-Username": "rsafier",
    }
    upload = client.post("/api/upload", headers=headers, data={
        "name": "Blue Thing From Source",
        "is_public": "false",
        "tags": "instant mesh, pixel 3d, tellus",
        "asset_category": "character",
        "asset_types": "generated, static-mesh",
        "runtime_metadata": json.dumps({"behaviors": ["placeable"]}),
        "file": (io.BytesIO(glb), "source_generation.glb"),
    }, content_type="multipart/form-data")
    assert upload.status_code == 201, upload.get_json()
    model = upload.get_json()["model"]

    all_private = client.get(
        "/api/models?include_private=true&search=instant",
        headers={"Authorization": "Bearer test-token"},
    )
    assert all_private.status_code == 200, all_private.get_json()
    assert [m["id"] for m in all_private.get_json()["models"]] == [model["id"]]
    assert all_private.get_json()["models"][0]["owner"]["id"] == rsafier.id

    rsafier_private = client.get(
        "/api/models?user_only=true",
        headers=headers,
    )
    assert rsafier_private.status_code == 200, rsafier_private.get_json()
    assert [m["id"] for m in rsafier_private.get_json()["models"]] == [model["id"]]

    lisa_duplicate = client.post("/api/upload", headers={
        "Authorization": "Bearer test-token",
        "X-Asset-Username": "lisa",
    }, data={
        "name": "Different Pixel 3D Title",
        "is_public": "false",
        "file": (io.BytesIO(glb), "renamed_generation.glb"),
    }, content_type="multipart/form-data")
    assert lisa_duplicate.status_code == 409, lisa_duplicate.get_json()
    assert "duplicate model" in lisa_duplicate.get_json()["error"].lower()

    lisa_private = client.get(
        "/api/models?user_only=true",
        headers={
            "Authorization": "Bearer test-token",
            "X-Asset-Username": "lisa",
        },
    )
    assert lisa_private.status_code == 200, lisa_private.get_json()
    assert lisa_private.get_json()["models"] == []
    assert lisa.id


def test_tellus_admin_token_defaults_owner_and_generation_search_metadata(monkeypatch):
    monkeypatch.setenv("TELLUS_ADMIN_API_TOKEN", "tellus-admin-token")
    monkeypatch.setenv("TELLUS_ADMIN_USERNAME", "tellusadmin")
    app = create_app()
    client = app.test_client()

    with app.app_context():
        admin = _ensure_user("tellusadmin")

    upload = client.post("/api/upload", headers={
        "Authorization": "Bearer tellus-admin-token",
    }, data={
        "name": "Instant Mesh Castle Result",
        "is_public": "false",
        "worldId": "Forest Hub",
        "file": (io.BytesIO(b"glTF-instant-mesh" + b"\x00" * 64), "instant_mesh_castle.glb"),
    }, content_type="multipart/form-data")
    assert upload.status_code == 201, upload.get_json()
    model = upload.get_json()["model"]
    assert model["tags"] == ["tellus", "tellus-world-forest-hub"]
    assert model["asset_types"] == []
    assert model["has_thumbnail"] is False
    assert model["thumbnail_url"] is None
    assert model["has_game_optimized"] is False

    with app.app_context():
        stored = Model3D.get_by_id(model["id"])
        file_id = app.config["FILE_STORE"].put(
            b"glTF-optimized" + b"\x00" * 64,
            filename="instant_mesh_castle-optimized.glb",
            content_type="model/gltf-binary",
            metadata={"kind": "game", "source_model_id": stored.id},
        )
        ModelVariant.upsert(
            stored.id, "game", str(file_id),
            file_format="glb", size=82, settings={"compression_mode": "meshopt"},
        )

    search = client.get(
        "/api/models?include_private=true&search=tellus-world-forest-hub",
        headers={"Authorization": "Bearer tellus-admin-token"},
    )
    assert search.status_code == 200, search.get_json()
    models = search.get_json()["models"]
    assert [m["id"] for m in models] == [model["id"]]
    assert models[0]["owner"]["id"] == admin.id
    assert models[0]["has_thumbnail"] is False
    assert models[0]["thumbnail_url"] is None
    assert models[0]["has_game_optimized"] is True
    assert models[0]["game_optimized"]["url"].endswith(f"/api/model/{model['id']}/game-optimized")

    user_only = client.get(
        "/api/user/models",
        headers={"Authorization": "Bearer tellus-admin-token"},
    )
    assert user_only.status_code == 200, user_only.get_json()
    listed = user_only.get_json()["models"]
    assert [m["id"] for m in listed] == [model["id"]]
    assert listed[0]["has_game_optimized"] is True
    assert listed[0]["has_thumbnail"] is False

    def fake_enrich(stored_model, extra_context=None):
        return {
            "title": "Castle Tower",
            "description": "A stone tower generated for a game world.",
            "summary": "Stone tower.",
            "tags": ["castle", "stone", "tower"],
            "asset_category": "architecture",
            "asset_styles": ["fantasy"],
            "asset_types": ["prop"],
            "runtime_metadata": {"collidable": True},
            "categories": [],
            "quality_notes": [],
            "provider": "test",
        }

    monkeypatch.setattr("app.ai_enrichment.enrich_model", fake_enrich)
    enrich = client.post(
        f"/api/model/{model['id']}/ai/autotag",
        headers={"Authorization": "Bearer tellus-admin-token"},
        json={"overwrite": True, "include_title": True, "include_description": True},
    )
    assert enrich.status_code == 200, enrich.get_json()
    enriched_model = enrich.get_json()["model"]
    assert {"tellus", "tellus-world-forest-hub", "castle", "stone", "tower"}.issubset(
        set(enriched_model["tags"])
    )
    assert enriched_model["asset_types"] == ["prop"]


def test_openapi_documents_workflow_and_bearer_auth():
    app = create_app()
    spec = app.test_client().get("/api/openapi.json").get_json()
    assert "bearerAuth" in spec["components"]["securitySchemes"]
    assert "get" in spec["paths"]["/model/{model_id}"]
    assert "/model/{model_id}/ai/autotag" in spec["paths"]
    assert "/model/{model_id}/approval" in spec["paths"]
    assert "/bundles" in spec["paths"]
    props = spec["paths"]["/model/{model_id}/ai/autotag"]["post"]["requestBody"]["content"]["application/json"]["schema"]["properties"]
    assert "include_title" in props
    assert "async" in props
    model_props = spec["components"]["schemas"]["ModelSummary"]["properties"]
    assert "asset_category" in model_props
    assert "asset_styles" in model_props
    assert "asset_types" in model_props
    assert "ai_error" in model_props
    assert "content_hash" in model_props
    assert "runtime_metadata" in model_props
    assert "RuntimeMetadata" in spec["components"]["schemas"]


def test_async_enrichment_queues_and_model_status_endpoint(monkeypatch):
    app = create_app()
    client = app.test_client()
    headers = {"Authorization": "Bearer test-token"}
    glb = b"glTF" + b"\x01" * 64
    upload = client.post("/api/upload", headers=headers, data={
        "file": (io.BytesIO(glb), "async_crate.glb"),
    }, content_type="multipart/form-data")
    assert upload.status_code == 201, upload.get_json()
    model_id = upload.get_json()["model"]["id"]

    queued = client.post(
        f"/api/model/{model_id}/ai/autotag",
        headers=headers,
        json={"async": True, "overwrite": True, "context": {"source": "test"}},
    )
    assert queued.status_code == 202, queued.get_json()
    assert queued.get_json()["status"] == "queued"
    assert queued.get_json()["model"]["ai_status"] == "pending"

    status = client.get(f"/api/model/{model_id}", headers=headers)
    assert status.status_code == 200, status.get_json()
    assert status.get_json()["model"]["ai_status"] == "pending"
    from app.models import Model3D
    with app.app_context():
        queued_model = Model3D.get_by_id(model_id)
    assert queued_model.ai_metadata["_job"]["data"]["context"]["source"] == "test"


def test_async_enrichment_kicks_queue_when_enabled(monkeypatch):
    from app import api as api_module

    app = create_app()
    client = app.test_client()
    headers = {"Authorization": "Bearer test-token"}
    glb = b"glTF" + b"\x03" * 64
    upload = client.post("/api/upload", headers=headers, data={
        "file": (io.BytesIO(glb), "kick_crate.glb"),
    }, content_type="multipart/form-data")
    assert upload.status_code == 201, upload.get_json()
    model_id = upload.get_json()["model"]["id"]

    kicked = {}

    def fake_kick(kicked_app):
        kicked["app_name"] = kicked_app.name

    monkeypatch.setenv("AI_AUTOTAG_KICK_ON_REQUEST", "1")
    monkeypatch.setattr(api_module, "_kick_ai_enrichment_worker", fake_kick)

    queued = client.post(
        f"/api/model/{model_id}/ai/autotag",
        headers=headers,
        json={"async": True, "overwrite": True, "context": {"source": "test_kick"}},
    )
    assert queued.status_code == 202, queued.get_json()
    assert kicked["app_name"] == app.name


def test_ai_enrichment_worker_drains_pending_job(monkeypatch):
    from app import ai_enrichment
    from app import api as api_module
    from app.models import Model3D

    app = create_app()
    client = app.test_client()
    headers = {"Authorization": "Bearer test-token"}
    glb = b"glTF" + b"\x02" * 64
    upload = client.post("/api/upload", headers=headers, data={
        "file": (io.BytesIO(glb), "worker_lantern.glb"),
    }, content_type="multipart/form-data")
    assert upload.status_code == 201, upload.get_json()
    model_id = upload.get_json()["model"]["id"]

    def fake_enrich_model(model, extra_context=None):
        assert extra_context == {"source": "test_worker"}
        return {
            "title": "Worker Lantern",
            "asset_category": "prop",
            "asset_styles": ["fantasy"],
            "asset_types": ["game-ready", "light-emitter"],
            "runtime_metadata": {
                "behaviors": ["light-emitter"],
                "light": {
                    "enabled": True,
                    "type": "point",
                    "color": "#ffb35a",
                    "intensity": 1.5,
                    "range": 8,
                    "cast_shadow": True,
                    "attach_to": "",
                    "offset": [0, 0.6, 0],
                },
            },
            "tags": ["lantern", "fantasy", "prop"],
            "description": "A fantasy lantern prop.",
            "summary": "Fantasy lantern.",
            "categories": ["props"],
            "quality_notes": [],
            "provider": "fake",
        }

    monkeypatch.setattr(ai_enrichment, "enrich_model", fake_enrich_model)

    queued = client.post(
        f"/api/model/{model_id}/ai/autotag",
        headers=headers,
        json={"async": True, "overwrite": True, "context": {"source": "test_worker"}},
    )
    assert queued.status_code == 202, queued.get_json()
    with app.app_context():
        assert api_module._drain_ai_enrichment_once(app) == 1
        model = Model3D.get_by_id(model_id)
    assert model.ai_status == "done"
    assert model.name == "Worker Lantern"
    assert model.runtime_metadata["light"]["enabled"] is True


def test_detail_page_shows_ai_vision_failure_message():
    from app.models import Model3D

    app = create_app()
    client = app.test_client()
    headers = {"Authorization": "Bearer test-token"}
    glb = b"glTF" + b"\x04" * 64
    upload = client.post("/api/upload", headers=headers, data={
        "is_public": "true",
        "file": (io.BytesIO(glb), "visionless_crate.glb"),
    }, content_type="multipart/form-data")
    assert upload.status_code == 201, upload.get_json()
    model_id = upload.get_json()["model"]["id"]

    with app.app_context():
        model = Model3D.get_by_id(model_id)
        model.ai_metadata = {
            "vision_mcp_attempted": True,
            "vision_mcp": False,
            "vision_mcp_error": "No thumbnail image is available for MCP analysis.",
        }
        model.save()

    detail = client.get(f"/model/{model_id}")
    assert detail.status_code == 200
    html = detail.get_data(as_text=True)
    assert "AI vision did not use image" in html
    assert "No thumbnail image is available for MCP analysis." in html


def test_detail_page_attempts_thumbnail_capture_before_ai_enrichment():
    html = Path("app/templates/model_detail.html").read_text(encoding="utf-8")
    assert "ensureThumbnailForAiVision" in html
    assert "Capturing a thumbnail for AI vision" in html
    assert "await ensureThumbnailForAiVision(status);" in html


def test_a2a_no_output_error_includes_task_state():
    from app import ai_enrichment

    payload = {
        "jsonrpc": "2.0",
        "id": "probe",
        "result": {
            "kind": "task",
            "id": "task-1",
            "status": {"state": "working"},
        },
    }

    assert ai_enrichment._extract_a2a_output(payload) == ""
    assert ai_enrichment._a2a_task_state(payload) == "working"
    assert "working" in ai_enrichment._summarize_provider_payload(payload)


def test_ai_output_parser_extracts_json_from_wrapped_text():
    from app import ai_enrichment

    parsed = ai_enrichment._parse_enrichment_json(
        "Here is the catalog metadata:\n"
        '{"title": "Wrapped Lantern", "tags": ["lantern"]}\n'
        "Hope that helps.",
        provider="hyades",
        transport="a2a",
    )

    assert parsed["title"] == "Wrapped Lantern"


def test_ai_output_parser_reports_non_json_text():
    from app import ai_enrichment

    with pytest.raises(RuntimeError) as exc:
        ai_enrichment._parse_enrichment_json(
            "I can describe this lantern, but I cannot emit JSON.",
            provider="hyades",
            transport="a2a",
        )

    message = str(exc.value)
    assert "AI enrichment returned non-JSON output from hyades/a2a" in message
    assert "I can describe this lantern" in message


def test_ai_output_parser_reports_provider_error_text():
    from app import ai_enrichment

    with pytest.raises(RuntimeError) as exc:
        ai_enrichment._parse_enrichment_json(
            "Retry failed after 4 tries. "
            "(No route to host (192.168.1.187:8008))",
            provider="hyades",
            transport="a2a",
        )

    message = str(exc.value)
    assert "AI enrichment provider returned error output for hyades/a2a" in message
    assert "No route to host" in message


def test_zai_api_key_alias(monkeypatch):
    from app import ai_enrichment

    monkeypatch.delenv("AI_AUTOTAG_API_KEY", raising=False)
    monkeypatch.delenv("ZAI_API_KEY", raising=False)
    monkeypatch.setenv("Z_AI_API_KEY", "zai-alias-key")

    assert ai_enrichment._api_key() == "zai-alias-key"


def test_zai_mcp_analysis_is_added_to_metadata_prompt(monkeypatch):
    from app import ai_enrichment

    class FakeModel:
        name = "lantern"
        description = ""
        original_filename = "lantern.glb"
        file_format = "glb"
        file_size = 123
        tags = []
        asset_category = None
        asset_styles = []
        asset_types = []
        runtime_metadata = {}
        approve_game_ready = False
        approve_asset_store = False
        conversion_status = None
        thumbnail_file_id = "thumb"

    captured = {}

    def fake_post_json(url, body, headers, provider=None, transport=None):
        captured["url"] = url
        captured["body"] = body
        return {
            "id": "zai-test",
            "choices": [{
                "message": {
                    "content": json.dumps({
                        "title": "Lantern Prop",
                        "asset_category": "prop",
                        "asset_styles": ["fantasy"],
                        "asset_types": ["light-emitter"],
                        "runtime_metadata": {
                            "behaviors": ["light-emitter"],
                            "light": {
                                "enabled": True,
                                "type": "point",
                                "color": "#ffb35a",
                                "intensity": 1.5,
                                "range": 8,
                                "cast_shadow": True,
                                "attach_to": "",
                                "offset": [0, 0.6, 0],
                            },
                        },
                        "tags": ["lantern", "fantasy", "prop"],
                        "description": "A fantasy lantern prop.",
                        "summary": "Fantasy lantern.",
                        "categories": ["props"],
                        "quality_notes": [],
                    })
                }
            }],
        }

    monkeypatch.setenv("AI_AUTOTAG_PROVIDER", "zai")
    monkeypatch.setenv("AI_AUTOTAG_API_KEY", "zai-key")
    monkeypatch.setenv("AI_AUTOTAG_BASE_URL", "https://api.z.ai/api/coding/paas/v4")
    monkeypatch.setenv("AI_AUTOTAG_MODEL", "glm-5.1")
    monkeypatch.setenv("AI_AUTOTAG_TRANSPORT", "zai-mcp")
    monkeypatch.setattr(
        ai_enrichment,
        "_zai_mcp_visual_context_result",
        lambda model, provider, api_key: {
            "enabled": True,
            "analysis": "Visible fantasy lantern with warm glow.",
            "error": None,
        },
    )
    monkeypatch.setattr(ai_enrichment, "_post_json", fake_post_json)

    enriched = ai_enrichment._ai_metadata(FakeModel())

    user_content = captured["body"]["messages"][1]["content"]
    assert captured["url"] == "https://api.z.ai/api/coding/paas/v4/chat/completions"
    assert "vision_mcp_analysis" in user_content
    assert "Visible fantasy lantern with warm glow." in user_content
    assert enriched["vision_mcp"] is True
    assert enriched["vision_mcp_attempted"] is True
    assert enriched["vision_mcp_analysis"] == "Visible fantasy lantern with warm glow."
    assert enriched["vision_mcp_error"] is None


def test_zai_openai_transport_does_not_send_image_parts(monkeypatch):
    from app import ai_enrichment

    class FakeModel:
        name = "lantern"
        description = ""
        original_filename = "lantern.glb"
        file_format = "glb"
        file_size = 123
        tags = []
        asset_category = None
        asset_styles = []
        asset_types = []
        runtime_metadata = {}
        approve_game_ready = False
        approve_asset_store = False
        conversion_status = None
        thumbnail_file_id = "thumb"

        def _read_stored_file(self, file_id):
            return b"webp-thumbnail"

    captured = {}

    def fake_post_json(url, body, headers, provider=None, transport=None):
        captured["body"] = body
        return {
            "id": "zai-test",
            "choices": [{
                "message": {
                    "content": json.dumps({
                        "title": "Lantern Prop",
                        "asset_category": "prop",
                        "asset_styles": ["fantasy"],
                        "asset_types": ["light-emitter"],
                        "runtime_metadata": {
                            "behaviors": ["light-emitter"],
                            "light": {
                                "enabled": True,
                                "type": "point",
                                "color": "#ffb35a",
                                "intensity": 1.5,
                                "range": 8,
                                "cast_shadow": True,
                                "attach_to": "",
                                "offset": [0, 0.6, 0],
                            },
                        },
                        "tags": ["lantern", "fantasy", "prop"],
                        "description": "A fantasy lantern prop.",
                        "summary": "Fantasy lantern.",
                        "categories": ["props"],
                        "quality_notes": [],
                    })
                }
            }],
        }

    monkeypatch.setenv("AI_AUTOTAG_PROVIDER", "zai")
    monkeypatch.setenv("AI_AUTOTAG_API_KEY", "zai-key")
    monkeypatch.setenv("AI_AUTOTAG_BASE_URL", "https://api.z.ai/api/coding/paas/v4")
    monkeypatch.setenv("AI_AUTOTAG_MODEL", "glm-5.1")
    monkeypatch.setenv("AI_AUTOTAG_TRANSPORT", "openai")
    monkeypatch.setattr(
        ai_enrichment,
        "_zai_mcp_visual_context_result",
        lambda model, provider, api_key: {
            "enabled": True,
            "analysis": "Visible fantasy lantern with warm glow.",
            "error": None,
        },
    )
    monkeypatch.setattr(ai_enrichment, "_post_json", fake_post_json)

    enriched = ai_enrichment._ai_metadata(FakeModel())

    user_content = captured["body"]["messages"][1]["content"]
    assert isinstance(user_content, str)
    assert "image_url" not in user_content
    assert "Visible fantasy lantern with warm glow." in user_content
    assert enriched["vision_mcp"] is True


def test_zai_mcp_discovers_image_capable_tool(monkeypatch):
    from app import ai_enrichment

    tools = [
        {
            "name": "read_file",
            "description": "Read a local file.",
            "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}},
        },
        {
            "name": "analyze_image",
            "description": "Analyze and describe a visual image.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                    "instructions": {"type": "string"},
                },
            },
        },
    ]

    tool = ai_enrichment._choose_mcp_tool(tools)

    assert tool["name"] == "analyze_image"
    assert ai_enrichment._choose_mcp_argument(
        tool,
        "AI_AUTOTAG_MCP_IMAGE_ARG",
        ("image_path", "file_path", "path", "image", "image_file", "file"),
        "image_path",
    ) == "file_path"
    assert ai_enrichment._choose_mcp_argument(
        tool,
        "AI_AUTOTAG_MCP_PROMPT_ARG",
        ("prompt", "query", "question", "instructions", "text"),
        "prompt",
    ) == "instructions"


def test_zai_mcp_reports_available_tools_when_no_image_tool():
    from app import ai_enrichment

    tools = [
        {"name": "list_models", "description": "List coding models."},
        {"name": "chat", "description": "Send a text message."},
    ]

    assert ai_enrichment._choose_mcp_tool(tools) is None
    names = ", ".join(str(item.get("name")) for item in tools if item.get("name"))
    assert names == "list_models, chat"


def test_zai_mcp_converts_thumbnail_to_png(monkeypatch):
    from app import ai_enrichment
    from PIL import Image

    source = io.BytesIO()
    Image.new("RGBA", (2, 2), (255, 0, 0, 255)).save(source, format="WEBP")

    monkeypatch.delenv("AI_AUTOTAG_MCP_IMAGE_SUFFIX", raising=False)
    converted, suffix = ai_enrichment._mcp_image_file_bytes(source.getvalue())

    assert suffix == ".png"
    with Image.open(io.BytesIO(converted)) as image:
        assert image.format == "PNG"
        assert image.size == (2, 2)


def test_zai_mcp_records_missing_thumbnail(monkeypatch):
    from app import ai_enrichment

    class FakeModel:
        name = "pixal3d asset"
        description = ""
        original_filename = "pixal3d.glb"
        file_format = "glb"
        file_size = 123
        tags = []
        asset_category = None
        asset_styles = []
        asset_types = []
        runtime_metadata = {}
        approve_game_ready = False
        approve_asset_store = False
        conversion_status = None
        thumbnail_file_id = None

    captured = {}

    def fake_post_json(url, body, headers, provider=None, transport=None):
        captured["body"] = body
        return {
            "id": "zai-test",
            "choices": [{
                "message": {
                    "content": json.dumps({
                        "title": "Pixal3D Asset",
                        "asset_category": "other",
                        "asset_styles": [],
                        "asset_types": [],
                        "runtime_metadata": {
                            "behaviors": [],
                            "light": {
                                "enabled": False,
                                "type": "none",
                                "color": "#ffffff",
                                "intensity": 0,
                                "range": 0,
                                "cast_shadow": False,
                                "attach_to": "",
                                "offset": [0, 0, 0],
                            },
                        },
                        "tags": ["pixal3d", "glb", "3d-model"],
                        "description": "A Pixal3D GLB asset.",
                        "summary": "Pixal3D GLB asset.",
                        "categories": [],
                        "quality_notes": [],
                    })
                }
            }],
        }

    monkeypatch.setenv("AI_AUTOTAG_PROVIDER", "zai")
    monkeypatch.setenv("AI_AUTOTAG_API_KEY", "zai-key")
    monkeypatch.setenv("AI_AUTOTAG_BASE_URL", "https://api.z.ai/api/coding/paas/v4")
    monkeypatch.setenv("AI_AUTOTAG_MODEL", "glm-5.1")
    monkeypatch.setenv("AI_AUTOTAG_TRANSPORT", "zai-mcp")
    monkeypatch.setattr(ai_enrichment, "_post_json", fake_post_json)

    enriched = ai_enrichment._ai_metadata(FakeModel())

    user_content = captured["body"]["messages"][1]["content"]
    assert "vision_mcp_status" in user_content
    assert enriched["vision_mcp"] is False
    assert enriched["vision_mcp_attempted"] is True
    assert "No thumbnail image is available" in enriched["vision_mcp_error"]


def test_hyades_a2a_empty_message_polls_task(monkeypatch):
    from app import ai_enrichment

    calls = []
    output = {
        "title": "Task Lantern",
        "asset_category": "prop",
        "asset_styles": ["fantasy"],
        "asset_types": ["game-ready", "light-emitter"],
        "runtime_metadata": {
            "behaviors": ["light-emitter"],
            "light": {
                "enabled": True,
                "type": "point",
                "color": "#ffb35a",
                "intensity": 1.5,
                "range": 8,
                "cast_shadow": True,
                "attach_to": "",
                "offset": [0, 0.6, 0],
            },
        },
        "tags": ["lantern", "fantasy", "prop"],
        "description": "A fantasy lantern prop.",
        "summary": "Fantasy lantern.",
        "categories": ["props"],
        "quality_notes": [],
    }

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(self.payload).encode("utf-8")

    def fake_urlopen(request, timeout):
        body = json.loads(request.data.decode("utf-8"))
        calls.append(body)
        if body["method"] == "message/send":
            return FakeResponse({
                "jsonrpc": "2.0",
                "id": "message-response",
                "result": {
                    "kind": "message",
                    "messageId": "empty-message",
                    "parts": [{"kind": "text", "text": ""}],
                    "role": "agent",
                    "taskId": "task-123",
                },
            })
        assert body["method"] == "tasks/get"
        assert body["params"]["id"] == "task-123"
        return FakeResponse({
            "jsonrpc": "2.0",
            "id": "task-response",
            "result": {
                "kind": "task",
                "id": "task-123",
                "status": {"state": "completed"},
                "artifacts": [{"parts": [{"kind": "text", "text": json.dumps(output)}]}],
            },
        })

    class FakeModel:
        name = "task_lantern"
        description = ""
        original_filename = "task_lantern.glb"
        file_format = "glb"
        file_size = 1234
        tags = []
        asset_category = None
        asset_styles = []
        asset_types = []
        runtime_metadata = {}
        approve_game_ready = False
        approve_asset_store = False
        conversion_status = None
        thumbnail_file_id = None

    monkeypatch.setenv("AI_AUTOTAG_PROVIDER", "hyades")
    monkeypatch.setenv("AI_AUTOTAG_API_KEY", "hyades-key")
    monkeypatch.setenv("AI_AUTOTAG_MODEL", "holo")
    monkeypatch.setenv("HYADES_A2A_POLL_ATTEMPTS", "2")
    monkeypatch.setenv("HYADES_A2A_POLL_INTERVAL", "0")
    monkeypatch.setattr(ai_enrichment.urllib.request, "urlopen", fake_urlopen)

    enriched = ai_enrichment.enrich_model(FakeModel())

    assert [call["method"] for call in calls] == ["message/send", "tasks/get"]
    assert enriched["title"] == "Task Lantern"
    assert enriched["provider"] == "hyades"
    assert enriched["transport"] == "a2a"


def test_hyades_a2a_enrichment_uses_holo_vision(monkeypatch):
    from app import ai_enrichment

    captured = {}
    output = {
        "title": "Moonlit Shrine",
        "asset_category": "building",
        "asset_styles": ["fantasy"],
        "asset_types": ["game-ready"],
        "runtime_metadata": {
            "behaviors": ["light-emitter"],
            "light": {
                "enabled": True,
                "type": "point",
                "color": "#ffb35a",
                "intensity": 1.5,
                "range": 8,
                "cast_shadow": True,
                "attach_to": "",
                "offset": [0, 0.6, 0],
            },
        },
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
        runtime_metadata = {}
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
    assert captured["headers"]["Accept"] == "application/json"
    assert captured["headers"]["User-agent"].startswith("3d-asset-manager/")
    assert captured["body"]["method"] == "message/send"
    assert captured["body"]["params"]["metadata"]["model"] == "holo"
    parts = captured["body"]["params"]["message"]["parts"]
    text_part = next(part for part in parts if part.get("kind") == "text")
    file_part = next(part for part in parts if part.get("kind") == "file")
    assert text_part["text"]
    assert file_part["file"]["bytes"]
    assert file_part["file"]["mimeType"] == "image/webp"
    assert enriched["provider"] == "hyades"
    assert enriched["transport"] == "a2a"
    assert enriched["asset_category"] == "building"
    assert enriched["runtime_metadata"]["light"]["enabled"] is True


def test_hyades_a2a_timeout_retries_text_only(monkeypatch):
    from app import ai_enrichment

    calls = []
    output = {
        "title": "Moonlit Shrine",
        "asset_category": "building",
        "asset_styles": ["fantasy"],
        "asset_types": ["game-ready"],
        "runtime_metadata": {"behaviors": [], "light": {"enabled": False, "type": "none", "color": "#ffffff", "intensity": 0, "range": 0, "cast_shadow": False, "attach_to": "", "offset": [0, 0, 0]}},
        "tags": ["shrine", "fantasy", "stone"],
        "description": "A fantasy shrine asset.",
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
                "result": {"task": {"artifacts": [{"parts": [{"text": json.dumps(output)}]}]}},
            }).encode("utf-8")

    def fake_urlopen(request, timeout):
        calls.append(json.loads(request.data.decode("utf-8")))
        if len(calls) == 1:
            raise TimeoutError("The read operation timed out")
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
        runtime_metadata = {}
        approve_game_ready = False
        approve_asset_store = False
        conversion_status = None
        thumbnail_file_id = "thumb"

        def _read_stored_file(self, file_id):
            assert file_id == "thumb"
            return b"webp-thumbnail"

    monkeypatch.setenv("AI_AUTOTAG_PROVIDER", "hyades")
    monkeypatch.setenv("AI_AUTOTAG_API_KEY", "hyades-key")
    monkeypatch.setenv("AI_AUTOTAG_RETRY_TEXT_ONLY", "1")
    monkeypatch.delenv("AI_AUTOTAG_BASE_URL", raising=False)
    monkeypatch.delenv("AI_AUTOTAG_TRANSPORT", raising=False)
    monkeypatch.delenv("AI_AUTOTAG_MODEL", raising=False)
    monkeypatch.setattr(ai_enrichment.urllib.request, "urlopen", fake_urlopen)

    enriched = ai_enrichment.enrich_model(FakeModel())

    assert len(calls) == 2
    assert any(part.get("kind") == "file" for part in calls[0]["params"]["message"]["parts"])
    assert not any(part.get("kind") == "file" for part in calls[1]["params"]["message"]["parts"])
    assert enriched["provider"] == "hyades"
    assert enriched["transport"] == "a2a"
    assert enriched["vision_fallback"] is True


def test_hyades_a2a_base_url_overrides_generic_openai_transport(monkeypatch):
    from app import ai_enrichment

    monkeypatch.setenv("AI_AUTOTAG_PROVIDER", "hyades")
    monkeypatch.setenv("AI_AUTOTAG_BASE_URL", "https://hyades.gnostr.cloud/a2a")
    monkeypatch.setenv("AI_AUTOTAG_TRANSPORT", "openai")

    assert ai_enrichment._transport("hyades") == "a2a"
    assert ai_enrichment._base_url("hyades") == "https://hyades.gnostr.cloud/a2a"


def test_hyades_holo_model_forces_a2a_over_generic_openai_env(monkeypatch):
    from app import ai_enrichment

    monkeypatch.setenv("AI_AUTOTAG_PROVIDER", "hyades")
    monkeypatch.setenv("AI_AUTOTAG_BASE_URL", "https://hyades.gnostr.cloud/v1")
    monkeypatch.setenv("AI_AUTOTAG_TRANSPORT", "openai")
    monkeypatch.setenv("AI_AUTOTAG_MODEL", "holo")

    assert ai_enrichment._transport("hyades") == "a2a"
    assert ai_enrichment._base_url("hyades") == "https://hyades.gnostr.cloud/a2a"


def test_openai_no_output_error_includes_payload_shape(monkeypatch):
    from app import ai_enrichment

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({
                "id": "chatcmpl-empty",
                "choices": [{"message": {"role": "assistant"}, "finish_reason": "stop"}],
            }).encode("utf-8")

    def fake_urlopen(request, timeout):
        return FakeResponse()

    class FakeModel:
        name = "empty_response"
        description = ""
        original_filename = "empty_response.glb"
        file_format = "glb"
        file_size = 1234
        tags = []
        asset_category = None
        asset_styles = []
        asset_types = []
        runtime_metadata = {}
        approve_game_ready = False
        approve_asset_store = False
        conversion_status = None
        thumbnail_file_id = None

    monkeypatch.setenv("AI_AUTOTAG_PROVIDER", "openai")
    monkeypatch.setenv("AI_AUTOTAG_API_KEY", "openai-key")
    monkeypatch.setenv("AI_AUTOTAG_MODEL", "gpt-test")
    monkeypatch.setattr(ai_enrichment.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(RuntimeError) as exc:
        ai_enrichment.enrich_model(FakeModel())

    message = str(exc.value)
    assert "AI enrichment returned no output text from openai/openai" in message
    assert "first choice message keys: role" in message
    assert "chatcmpl-empty" in message


def test_openai_vision_cloudflare_error_retries_text_only(monkeypatch):
    from app import ai_enrichment

    calls = []
    output = {
        "title": "Stone Lantern",
        "asset_category": "prop",
        "asset_styles": ["fantasy"],
        "asset_types": ["game-ready"],
        "runtime_metadata": {"behaviors": ["light-emitter"], "light": {"enabled": True, "type": "point", "color": "#ffb35a", "intensity": 1.5, "range": 8, "cast_shadow": True, "attach_to": "", "offset": [0, 0.6, 0]}},
        "tags": ["lantern", "stone", "prop"],
        "description": "A stone lantern prop for a fantasy scene.",
        "summary": "Fantasy stone lantern.",
        "categories": ["props"],
        "quality_notes": [],
    }

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({
                "id": "chatcmpl-1",
                "choices": [{"message": {"content": json.dumps(output)}}],
            }).encode("utf-8")

    def fake_urlopen(request, timeout):
        calls.append(json.loads(request.data.decode("utf-8")))
        if len(calls) == 1:
            raise ai_enrichment.urllib.error.HTTPError(
                request.full_url,
                520,
                "Origin Error",
                {},
                io.BytesIO(b"<html>The origin web server returned an invalid or incomplete response</html>"),
            )
        return FakeResponse()

    class FakeModel:
        name = "stone_lantern"
        description = ""
        original_filename = "stone_lantern.glb"
        file_format = "glb"
        file_size = 1234
        tags = []
        asset_category = None
        asset_styles = []
        asset_types = []
        runtime_metadata = {}
        approve_game_ready = False
        approve_asset_store = False
        conversion_status = None
        thumbnail_file_id = "thumb"

        def _read_stored_file(self, file_id):
            assert file_id == "thumb"
            return b"webp-thumbnail"

    monkeypatch.setenv("AI_AUTOTAG_PROVIDER", "openai")
    monkeypatch.setenv("AI_AUTOTAG_API_KEY", "openai-key")
    monkeypatch.setenv("AI_AUTOTAG_USE_VISION", "1")
    monkeypatch.setenv("AI_AUTOTAG_RETRY_TEXT_ONLY", "1")
    monkeypatch.setattr(ai_enrichment.urllib.request, "urlopen", fake_urlopen)

    enriched = ai_enrichment.enrich_model(FakeModel())

    assert len(calls) == 2
    assert isinstance(calls[0]["messages"][1]["content"], list)
    assert isinstance(calls[1]["messages"][1]["content"], str)
    assert enriched["vision_fallback"] is True
    assert enriched["title"] == "Stone Lantern"


def test_ai_enrichment_infers_facets_and_title_from_floral_vision(monkeypatch):
    from app import ai_enrichment

    output = {
        "title": "Pixal3D AI-Generated 3D Model",
        "asset_category": None,
        "asset_styles": [],
        "asset_types": [],
        "runtime_metadata": {
            "behaviors": [],
            "light": {
                "enabled": False,
                "type": "none",
                "color": "#ffffff",
                "intensity": 0,
                "range": 0,
                "cast_shadow": False,
                "attach_to": "",
                "offset": [0, 0, 0],
            },
        },
        "tags": ["flowers", "bouquet", "watercolor", "painterly", "stylized", "static", "glb"],
        "description": (
            "A stylized floral arrangement featuring red, pink, and purple blooms with green leaves and stems. "
            "Rendered in a painterly watercolor aesthetic as a static decorative prop."
        ),
        "summary": "A static watercolor floral arrangement.",
        "categories": [],
        "quality_notes": [],
    }

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({
                "id": "chatcmpl-floral",
                "choices": [{"message": {"content": json.dumps(output)}}],
            }).encode("utf-8")

    class FakeModel:
        name = "pixal3d_1781271874905.glb"
        description = ""
        original_filename = "pixal3d_1781271874905.glb"
        file_format = "glb"
        file_size = 1234
        tags = []
        asset_category = None
        asset_styles = []
        asset_types = []
        runtime_metadata = {}
        approve_game_ready = False
        approve_asset_store = False
        conversion_status = None
        thumbnail_file_id = None

    monkeypatch.setenv("AI_AUTOTAG_PROVIDER", "openai")
    monkeypatch.setenv("AI_AUTOTAG_API_KEY", "openai-key")
    monkeypatch.setenv("AI_AUTOTAG_USE_VISION", "0")
    monkeypatch.setattr(ai_enrichment.urllib.request, "urlopen", lambda request, timeout: FakeResponse())

    enriched = ai_enrichment.enrich_model(FakeModel())

    assert enriched["title"] == "Watercolor Floral Arrangement"
    assert enriched["asset_category"] == "flora"
    assert {"watercolor", "painterly", "stylized"}.issubset(set(enriched["asset_styles"]))
    assert {"static", "decorative-prop"}.issubset(set(enriched["asset_types"]))


def test_ai_enrichment_corrects_house_category_and_title(monkeypatch):
    from app import ai_enrichment

    output = {
        "title": "Pixal3D 1781271874905-1698b233e6dc8",
        "asset_category": "fauna",
        "asset_styles": ["painterly", "stylized", "fantasy"],
        "asset_types": ["static", "light-emitter", "decorative-prop"],
        "runtime_metadata": {
            "behaviors": ["light-emitter"],
            "light": {
                "enabled": True,
                "type": "point",
                "color": "#ffb35a",
                "intensity": 1.5,
                "range": 8,
                "cast_shadow": True,
                "attach_to": "",
                "offset": [0, 0.6, 0],
            },
        },
        "tags": [
            "cottage", "tower", "house", "fantasy", "storybook", "medieval",
            "timber-framed", "half-timbered", "tudor", "cupola", "balcony",
            "environment-prop", "fairy-tale", "stylized", "building", "architecture",
            "pixal3d", "generated", "image-to-3d", "ai-generated", "glb", "3d-model",
        ],
        "description": "A stylized fantasy cottage house with a tower, timber-framed facade, cupola, and balcony.",
        "summary": "Fantasy storybook cottage house.",
        "categories": [],
        "quality_notes": [],
    }

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({
                "id": "chatcmpl-house",
                "choices": [{"message": {"content": json.dumps(output)}}],
            }).encode("utf-8")

    class FakeModel:
        name = "pixal3d_1781271874905.glb"
        description = ""
        original_filename = "pixal3d_1781271874905.glb"
        file_format = "glb"
        file_size = 1234
        tags = []
        asset_category = None
        asset_styles = []
        asset_types = []
        runtime_metadata = {}
        approve_game_ready = False
        approve_asset_store = False
        conversion_status = None
        thumbnail_file_id = None

    monkeypatch.setenv("AI_AUTOTAG_PROVIDER", "openai")
    monkeypatch.setenv("AI_AUTOTAG_API_KEY", "openai-key")
    monkeypatch.setenv("AI_AUTOTAG_USE_VISION", "0")
    monkeypatch.setattr(ai_enrichment.urllib.request, "urlopen", lambda request, timeout: FakeResponse())

    enriched = ai_enrichment.enrich_model(FakeModel())

    assert enriched["asset_category"] == "building"
    assert enriched["title"] == "Cottage"
    assert "fauna" not in enriched["asset_category"]
    assert not {"pixal3d", "generated", "image-to-3d", "ai-generated", "glb", "3d-model"}.intersection(enriched["tags"])


def test_ai_enrichment_recovers_fountain_metadata_from_vision(monkeypatch):
    from app import ai_enrichment

    class FakeModel:
        name = "pixal3d_1781271874905.glb"
        description = ""
        original_filename = "pixal3d_1781271874905.glb"
        file_format = "glb"
        file_size = 2760000
        tags = []
        asset_category = None
        asset_styles = []
        asset_types = []
        runtime_metadata = {}
        approve_game_ready = False
        approve_asset_store = False
        conversion_status = None
        thumbnail_file_id = "thumb-1"

    def fake_ai_metadata(model, extra_context=None):
        return {
            "title": "Unknown AI-Generated 3D Model",
            "asset_category": "environment",
            "asset_styles": [],
            "asset_types": ["static", "light-emitter", "decorative-prop"],
            "runtime_metadata": {
                "behaviors": ["light-emitter"],
                "light": {
                    "enabled": True,
                    "type": "point",
                    "color": "#ffb35a",
                    "intensity": 1.5,
                    "range": 8,
                    "cast_shadow": True,
                    "attach_to": "",
                    "offset": [0, 0.6, 0],
                },
            },
            "tags": ["pixal3d", "generated", "image-to-3d", "ai-generated", "glb", "3d-model"],
            "description": (
                "An AI-generated 3D model produced via Pixal3D's image-to-3D pipeline. "
                "No thumbnail is available, so the specific visual subject matter cannot be determined."
            ),
            "summary": "Unknown AI-generated model.",
            "categories": [],
            "quality_notes": [],
            "vision_mcp": True,
            "vision_mcp_analysis": (
                "This 3D asset preview displays a two-tiered, classical-style fountain. "
                "The model appears to be made of a stone or marble-like material with an aged, weathered finish. "
                "The fountain does not display any characteristics of a light emitter. "
                "There are no glowing elements, emissive textures, or indications that it is designed to cast light."
            ),
        }

    monkeypatch.setattr(ai_enrichment, "_ai_metadata", fake_ai_metadata)

    enriched = ai_enrichment.enrich_model(FakeModel())

    assert enriched["title"] == "Classical Stone Fountain"
    assert enriched["description"].startswith("A classical two-tiered fountain")
    assert enriched["asset_category"] == "environment"
    assert {"fountain", "water-feature", "classical", "stone", "weathered"}.issubset(set(enriched["tags"]))
    assert not {"pixal3d", "generated", "image-to-3d", "ai-generated", "glb", "3d-model"}.intersection(enriched["tags"])
    assert "light-emitter" not in enriched["asset_types"]
    assert enriched["runtime_metadata"]["light"]["enabled"] is False


def test_ai_enrichment_removes_contradictory_facets(monkeypatch):
    from app import ai_enrichment

    class FakeModel:
        name = "storybook_cottage.glb"
        description = ""
        original_filename = "storybook_cottage.glb"
        file_format = "glb"
        file_size = 2760000
        tags = []
        asset_category = None
        asset_styles = []
        asset_types = []
        runtime_metadata = {}
        approve_game_ready = False
        approve_asset_store = False
        conversion_status = None
        thumbnail_file_id = "thumb-1"

    def fake_ai_metadata(model, extra_context=None):
        return {
            "title": "Storybook Cottage",
            "asset_category": "building",
            "asset_styles": ["stylized", "cartoon", "painted", "painterly", "fantasy", "realistic"],
            "asset_types": ["static-mesh", "high-poly", "building", "static", "light-emitter", "low-poly"],
            "runtime_metadata": {
                "behaviors": ["light-emitter"],
                "light": {
                    "enabled": True,
                    "type": "point",
                    "color": "#ffb35a",
                    "intensity": 1.5,
                    "range": 8,
                    "cast_shadow": True,
                    "attach_to": "",
                    "offset": [0, 0.6, 0],
                },
            },
            "tags": ["cottage", "storybook", "building"],
            "description": "A stylized cartoon fantasy cottage with a painterly hand-painted look and high detail.",
            "summary": "Stylized fantasy cottage.",
            "categories": [],
            "quality_notes": [],
            "vision_mcp": True,
            "vision_mcp_analysis": (
                "The preview shows a stylized cartoon fantasy cottage with a painted, painterly look. "
                "It does not function as a light emitter and has no glowing elements."
            ),
        }

    monkeypatch.setattr(ai_enrichment, "_ai_metadata", fake_ai_metadata)

    enriched = ai_enrichment.enrich_model(FakeModel())

    assert enriched["asset_category"] == "building"
    assert "realistic" not in enriched["asset_styles"]
    assert "painted" not in enriched["asset_styles"]
    assert {"stylized", "cartoon", "painterly", "fantasy"}.issubset(set(enriched["asset_styles"]))
    assert "building" not in enriched["asset_types"]
    assert "static-mesh" not in enriched["asset_types"]
    assert "static" in enriched["asset_types"]
    assert "light-emitter" not in enriched["asset_types"]
    assert {"high-poly", "low-poly"} != set(enriched["asset_types"]).intersection({"high-poly", "low-poly"})


def test_ai_enrichment_prompt_uses_fab_listing_copy(monkeypatch):
    from app import ai_enrichment

    class FakeModel:
        name = "classical_fountain.glb"
        description = ""
        original_filename = "classical_fountain.glb"
        file_format = "glb"
        file_size = 2760000
        tags = []
        asset_category = None
        asset_styles = []
        asset_types = []
        runtime_metadata = {}
        approve_game_ready = False
        approve_asset_store = False
        conversion_status = None
        thumbnail_file_id = None

    captured = {}

    def fake_post_json(url, body, headers, provider=None, transport=None):
        captured["body"] = body
        return {
            "id": "chatcmpl-fab-copy",
            "choices": [{
                "message": {
                    "content": json.dumps({
                        "title": "Classical Stone Fountain",
                        "asset_category": "environment",
                        "asset_styles": ["classical", "realistic"],
                        "asset_types": ["static", "decorative-prop"],
                        "runtime_metadata": {
                            "behaviors": [],
                            "light": {
                                "enabled": False,
                                "type": "none",
                                "color": "#ffffff",
                                "intensity": 0,
                                "range": 0,
                                "cast_shadow": False,
                                "attach_to": "",
                                "offset": [0, 0, 0],
                            },
                        },
                        "tags": ["fountain", "stone", "classical"],
                        "description": (
                            "Add a classical stone fountain to gardens, courtyards, and architectural scenes. "
                            "Its tiered silhouette and weathered material make it useful as a decorative focal point."
                        ),
                        "summary": "Classical stone fountain for architectural and garden scenes.",
                        "categories": [],
                        "quality_notes": [],
                    })
                }
            }],
        }

    monkeypatch.setenv("AI_AUTOTAG_PROVIDER", "openai")
    monkeypatch.setenv("AI_AUTOTAG_API_KEY", "openai-key")
    monkeypatch.setenv("AI_AUTOTAG_USE_VISION", "0")
    monkeypatch.setattr(ai_enrichment, "_post_json", fake_post_json)

    enriched = ai_enrichment._ai_metadata(FakeModel())

    system_text = captured["body"]["messages"][0]["content"]
    user_text = captured["body"]["messages"][1]["content"]
    schema = captured["body"]["response_format"]["json_schema"]["schema"]
    assert "Fab marketplace product descriptions" in system_text
    assert "buyer-facing copy" in user_text
    assert "Keep the title under 80 characters" in user_text
    assert "Return up to 25 discoverability tags" in user_text
    assert schema["properties"]["tags"]["maxItems"] == 25
    assert "Fab listing title under 80 characters" in schema["properties"]["title"]["description"]
    assert "Buyer-facing Fab product description" in schema["properties"]["description"]["description"]
    assert enriched["title"] == "Classical Stone Fountain"
