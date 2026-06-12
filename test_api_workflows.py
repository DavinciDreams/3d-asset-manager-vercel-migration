import io
import json
import os

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
    assert "glb" in body["model"]["tags"]

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
