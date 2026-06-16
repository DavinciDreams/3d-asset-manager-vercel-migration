import io
import json
import os
import struct
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
from app import conversion
from app.models import Model3D, ModelVariant, User


def _ensure_user(username):
    user = User.get_by_username(username)
    if user:
        return user
    user = User(username=username, email=f"{username}@example.com")
    user.set_password("pw123456")
    return user.save()


def _minimal_glb(gltf):
    raw = json.dumps(gltf, separators=(",", ":")).encode("utf-8")
    json_chunk = raw + (b" " * ((4 - len(raw) % 4) % 4))
    length = 12 + 8 + len(json_chunk)
    return b"glTF" + struct.pack("<II", 2, length) + struct.pack("<II", len(json_chunk), 0x4E4F534A) + json_chunk


def _attach_thumbnail(app, model_id):
    with app.app_context():
        model = Model3D.get_by_id(model_id)
        file_id = app.config["FILE_STORE"].put(
            b"fake-thumbnail",
            filename=f"{model_id}.webp",
            content_type="image/webp",
            metadata={"model_id": model_id, "kind": "thumbnail"},
        )
        model.thumbnail_file_id = str(file_id)
        model.save()


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
    _attach_thumbnail(app, model_id)
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


def test_upload_derives_rig_and_animation_metadata_from_glb():
    app = create_app()
    client = app.test_client()
    headers = {"Authorization": "Bearer test-token"}
    glb = _minimal_glb({
        "asset": {"version": "2.0"},
        "nodes": [
            {"name": "Mesh", "mesh": 0, "skin": 0},
            {"name": "Hips"},
            {"name": "Spine"},
        ],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 1}, "indices": 2}]}],
        "skins": [{"joints": [1, 2]}],
        "accessors": [
            {"max": [1.75]},
            {"count": 90, "min": [-1, 0, -0.5], "max": [1, 2, 0.5]},
            {"count": 120},
        ],
        "animations": [{"name": "Idle", "samplers": [{"input": 0}]}],
    })

    upload = client.post("/api/upload", headers=headers, data={
        "is_public": "false",
        "file": (io.BytesIO(glb), "animated_avatar.glb"),
    }, content_type="multipart/form-data")
    assert upload.status_code == 201, upload.get_json()
    model = upload.get_json()["model"]
    assert model["asset_types"] == ["rigged", "animated"]
    assert model["runtime_metadata"]["animations"] == [{"name": "Idle", "duration": 1.75}]
    assert model["mesh_stats"] == {"vertices": 90, "triangles": 40, "primitives": 1}
    assert model["physical_metadata"]["height"] == 2
    assert model["physical_metadata"]["width"] == 2
    assert model["physical_metadata"]["depth"] == 1
    assert model["physical_metadata"]["center"] == [0, 1, 0]
    assert model["physical_metadata"]["suggested_scale"] == 0.5
    assert model["effective_file_size"] == model["file_size"]
    assert model["effective_mesh_stats"] == model["mesh_stats"]
    assert model["effective_physical_metadata"] == model["physical_metadata"]
    assert model["media_capture"]["needs_thumbnail"] is True
    assert model["media_capture"]["needs_preview"] is True
    assert model["detail_url"].endswith(f"/model/{model['id']}?capture=1")

    with app.app_context():
        file_id = app.config["FILE_STORE"].put(
            glb,
            filename="animated_avatar-game.glb",
            content_type="model/gltf-binary",
            metadata={"kind": "game", "source_model_id": model["id"]},
        )
        ModelVariant.upsert(
            model["id"], "game", str(file_id),
            file_format="glb", size=321,
            settings={
                "mesh_stats": {"vertices": 24, "triangles": 12, "primitives": 1},
                "physical": {"height": 1, "width": 1, "depth": 1, "radius": 0.866025, "suggested_scale": 1},
            },
        )

    fetched = client.get(f"/api/model/{model['id']}", headers=headers)
    assert fetched.status_code == 200, fetched.get_json()
    fetched_model = fetched.get_json()["model"]
    assert fetched_model["effective_file_size"] == 321
    assert fetched_model["game_optimized"]["mesh_stats"] == {"vertices": 24, "triangles": 12, "primitives": 1}
    assert fetched_model["effective_mesh_stats"] == {"vertices": 24, "triangles": 12, "primitives": 1}
    assert fetched_model["game_optimized"]["physical"]["height"] == 1
    assert fetched_model["effective_physical_metadata"]["suggested_scale"] == 1


def test_animated_models_endpoint_lists_only_loadable_animated_glbs():
    app = create_app()
    client = app.test_client()
    headers = {"Authorization": "Bearer test-token"}
    animated_glb = _minimal_glb({
        "asset": {"version": "2.0"},
        "nodes": [{"mesh": 0, "skin": 0}, {"name": "Hips"}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 1}, "indices": 2}]}],
        "skins": [{"joints": [1]}],
        "accessors": [
            {"max": [2.5]},
            {"count": 12, "min": [-0.5, 0, -0.25], "max": [0.5, 1.75, 0.25]},
            {"count": 18},
        ],
        "animations": [{"name": "Wave", "samplers": [{"input": 0}]}],
    })
    static_glb = _minimal_glb({
        "asset": {"version": "2.0"},
        "nodes": [{"mesh": 0}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0}}]}],
        "accessors": [{"count": 8, "min": [0, 0, 0], "max": [1, 1, 1]}],
    })
    private_animated_glb = _minimal_glb({
        "asset": {"version": "2.0"},
        "nodes": [{"mesh": 0, "skin": 0}, {"name": "Hips"}, {"name": "Arm"}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 1}, "indices": 2}]}],
        "skins": [{"joints": [1, 2]}],
        "accessors": [
            {"max": [1.0]},
            {"count": 20, "min": [-1, 0, -1], "max": [1, 2, 1]},
            {"count": 30},
        ],
        "animations": [{"name": "Dance", "samplers": [{"input": 0}]}],
    })

    public_upload = client.post("/api/upload", headers=headers, data={
        "is_public": "true",
        "file": (io.BytesIO(animated_glb), "public_animated.glb"),
    }, content_type="multipart/form-data")
    assert public_upload.status_code == 201, public_upload.get_json()
    public_id = public_upload.get_json()["model"]["id"]

    static_upload = client.post("/api/upload", headers=headers, data={
        "is_public": "true",
        "file": (io.BytesIO(static_glb), "static_tree.glb"),
    }, content_type="multipart/form-data")
    assert static_upload.status_code == 201, static_upload.get_json()
    static_id = static_upload.get_json()["model"]["id"]

    source_upload = client.post("/api/upload", headers=headers, data={
        "is_public": "true",
        "asset_types": "rigged, animated",
        "tags": "animation-source",
        "file": (io.BytesIO(b"FBX source animation bytes"), "mixamo_source.fbx"),
    }, content_type="multipart/form-data")
    assert source_upload.status_code == 201, source_upload.get_json()
    source_id = source_upload.get_json()["model"]["id"]

    private_upload = client.post("/api/upload", headers=headers, data={
        "is_public": "false",
        "file": (io.BytesIO(private_animated_glb), "private_animated.glb"),
    }, content_type="multipart/form-data")
    assert private_upload.status_code == 201, private_upload.get_json()
    private_id = private_upload.get_json()["model"]["id"]

    public_list = client.get("/api/animated-models")
    assert public_list.status_code == 200, public_list.get_json()
    public_body = public_list.get_json()
    ids = {model["id"] for model in public_body["models"]}
    assert public_id in ids
    assert static_id not in ids
    assert source_id not in ids
    assert private_id not in ids
    animated_model = next(model for model in public_body["models"] if model["id"] == public_id)
    assert animated_model["file_format"] == "glb"
    assert animated_model["asset_types"] == ["rigged", "animated"]
    assert animated_model["runtime_metadata"]["animations"] == [{"name": "Wave", "duration": 2.5}]
    assert animated_model["view_url"].endswith(f"/api/view/{public_id}?viewer=2")
    assert animated_model["download_url"].endswith(f"/api/download/{public_id}")
    assert public_body["filters"] == {"asset_types": ["rigged", "animated"], "formats": ["glb", "gltf"]}

    private_list = client.get("/api/animated-models?include_private=true", headers=headers)
    assert private_list.status_code == 200, private_list.get_json()
    private_ids = {model["id"] for model in private_list.get_json()["models"]}
    assert public_id in private_ids
    assert private_id in private_ids
    assert static_id not in private_ids
    assert source_id not in private_ids


def test_humanoid_glb_upload_auto_creates_vrm_variant(monkeypatch):
    app = create_app()
    client = app.test_client()
    headers = {"Authorization": "Bearer test-token"}
    monkeypatch.setenv("AUTO_GAME_OPTIMIZE", "0")

    created = {"called": False}

    def fake_convert(model, data, author=None):
        created["called"] = True
        variant, _old = ModelVariant.upsert(
            model.id,
            "vrm",
            model.gridfs_file_id,
            file_format="vrm",
            size=len(data),
            status="ready",
        )
        return variant, False

    import app.api as api
    monkeypatch.setattr(api, "_convert_glb_bytes_to_vrm", fake_convert)

    glb = _minimal_glb({
        "asset": {"version": "2.0"},
        "nodes": [{"name": name} for name in sorted(conversion.MIXAMO_BONES)],
    })
    upload = client.post("/api/upload", headers=headers, data={
        "is_public": "false",
        "file": (io.BytesIO(glb), "mixamo_avatar.glb"),
    }, content_type="multipart/form-data")
    assert upload.status_code == 201, upload.get_json()
    model_id = upload.get_json()["model"]["id"]

    assert created["called"] is True

    with app.app_context():
        variant = ModelVariant.get(model_id, "vrm")
    assert variant is not None
    assert variant.status == "ready"


def test_vrm_viewer_supports_compressed_vrm_assets():
    viewer = Path("app/templates/_vrm_viewer.html").read_text(encoding="utf-8")
    assert "KTX2Loader" in viewer
    assert "MeshoptDecoder" in viewer
    assert "setKTX2Loader" in viewer
    assert "setMeshoptDecoder" in viewer
    assert "getBoundingSphere" in viewer
    assert "fitFov" in viewer
    assert "VRM onLoad callback failed" in viewer
    assert "frame(padding)" in viewer


def test_vrm_detail_capture_flags_do_not_break_avatar_loader():
    detail = Path("app/templates/model_detail.html").read_text(encoding="utf-8")
    assert "window.TellusDetailMediaFlags" in detail
    assert "function mediaFlag(name)" in detail
    assert "typeof FORCE_REGEN !== 'undefined'" in detail
    assert "mediaFlag('captureEnabled')" in detail


def test_model_detail_escapes_metadata_in_viewer_script():
    app = create_app()
    client = app.test_client()
    with app.app_context():
        owner = _ensure_user("detail-owner")
        owner.set_password("pw123456")
        owner.save()
    client.post("/auth/login", data={"login_field": "detail-owner", "password": "pw123456"})

    upload = client.post("/api/upload", data={
        "name": "Fairy's \"Walk\" Demo",
        "description": "Line one\nLine two with 'quotes' and </script> text.",
        "is_public": "true",
        "file": (io.BytesIO(b"glTF" + b"\x0d" * 64), "fairy_walk.glb"),
    }, content_type="multipart/form-data")
    assert upload.status_code == 201, upload.get_json()
    model_id = upload.get_json()["model"]["id"]

    page = client.get(f"/model/{model_id}")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert 'name: "Fairy\\u0027s \\"Walk\\" Demo"' in html
    assert "description: \"Line one\\nLine two with \\u0027quotes\\u0027 and \\u003c/script\\u003e text.\"" in html
    assert "description: 'Line one" not in html
    assert 'id="edit-asset-category"' in html
    assert 'name="asset_category"' in html
    assert 'id="edit-asset-styles"' in html
    assert 'id="edit-asset-types"' in html


def test_admin_media_capture_queue_lists_only_renderable_missing_media():
    app = create_app()
    client = app.test_client()
    headers = {"Authorization": "Bearer test-token"}

    denied = client.get("/api/admin/media-capture/queue")
    assert denied.status_code == 401

    native = client.post("/api/upload", headers=headers, data={
        "is_public": "true",
        "file": (io.BytesIO(b"glTF" + b"\x01" * 64), "queue_native.glb"),
    }, content_type="multipart/form-data")
    assert native.status_code == 201, native.get_json()
    native_id = native.get_json()["model"]["id"]

    vrma = client.post("/api/upload", headers=headers, data={
        "is_public": "true",
        "file": (io.BytesIO(b"VRMA" + b"\x02" * 64), "idle_clip.vrma"),
    }, content_type="multipart/form-data")
    assert vrma.status_code == 201, vrma.get_json()
    vrma_id = vrma.get_json()["model"]["id"]

    fbx = client.post("/api/upload", headers=headers, data={
        "is_public": "true",
        "file": (io.BytesIO(b"Kaydara FBX Binary" + b"\x03" * 64), "prop_source.fbx"),
    }, content_type="multipart/form-data")
    assert fbx.status_code == 201, fbx.get_json()
    fbx_id = fbx.get_json()["model"]["id"]

    queue = client.get("/api/admin/media-capture/queue?limit=20", headers=headers)
    assert queue.status_code == 200, queue.get_json()
    ids = {item["id"] for item in queue.get_json()["models"]}
    assert native_id in ids
    assert vrma_id not in ids
    assert fbx_id not in ids

    native_item = next(item for item in queue.get_json()["models"] if item["id"] == native_id)
    assert native_item["needs_thumbnail"] is True
    assert native_item["needs_preview"] is True
    assert native_item["capture_ready"] is True
    assert native_item["capture_url"].endswith(f"/model/{native_id}?capture=1")

    with app.app_context():
        converted_fbx = Model3D.get_by_id(fbx_id)
        viewable_id = app.config["FILE_STORE"].put(
            b"glTF" + b"\x04" * 64,
            filename="prop_source_viewable.glb",
            content_type="model/gltf-binary",
            metadata={"derived_for": fbx_id, "kind": "viewable"},
        )
        vrma_clip_id = app.config["FILE_STORE"].put(
            b"vrma",
            filename="prop_source_clip.vrma",
            content_type="application/octet-stream",
            metadata={"derived_for": fbx_id, "kind": "vrma"},
        )
        converted_fbx.viewable_file_id = str(viewable_id)
        converted_fbx.viewable_format = "glb"
        converted_fbx.vrma_file_id = str(vrma_clip_id)
        converted_fbx.conversion_status = "done"
        converted_fbx.save()

    converted_queue = client.get("/api/admin/media-capture/queue?limit=20", headers=headers)
    assert converted_queue.status_code == 200, converted_queue.get_json()
    converted_ids = {item["id"] for item in converted_queue.get_json()["models"]}
    assert fbx_id not in converted_ids

    animation_queue = client.get(
        "/api/admin/media-capture/queue?kind=animations&include_not_ready=true&limit=20",
        headers=headers,
    )
    assert animation_queue.status_code == 200, animation_queue.get_json()
    fbx_item = next(item for item in animation_queue.get_json()["models"] if item["id"] == fbx_id)
    assert fbx_item["capture_mode"] == "animation"
    assert fbx_item["capture_url"].endswith(f"/animations?capture_clip={fbx_id}:vrma")


def test_fbx_source_with_vrm_and_vrma_lives_in_avatar_animation_apis():
    app = create_app()
    client = app.test_client()
    headers = {"Authorization": "Bearer test-token"}

    upload = client.post("/api/upload", headers=headers, data={
        "is_public": "true",
        "file": (io.BytesIO(b"Kaydara FBX Binary" + b"\x05" * 64), "avatar_source.fbx"),
    }, content_type="multipart/form-data")
    assert upload.status_code == 201, upload.get_json()
    model_id = upload.get_json()["model"]["id"]

    private_vrm_upload = client.post("/api/upload", headers=headers, data={
        "is_public": "false",
        "file": (io.BytesIO(b"glTF private vrm" + b"\x09" * 64), "private_avatar.vrm"),
    }, content_type="multipart/form-data")
    assert private_vrm_upload.status_code == 201, private_vrm_upload.get_json()
    private_vrm_id = private_vrm_upload.get_json()["model"]["id"]

    with app.app_context():
        source = Model3D.get_by_id(model_id)
        viewable_id = app.config["FILE_STORE"].put(
            b"glTF" + b"\x06" * 64,
            filename="avatar_source_viewable.glb",
            content_type="model/gltf-binary",
            metadata={"derived_for": model_id, "kind": "viewable"},
        )
        vrm_id = app.config["FILE_STORE"].put(
            b"glTF" + b"\x07" * 64,
            filename="avatar_source.vrm",
            content_type="model/gltf-binary",
            metadata={"derived_for": model_id, "kind": "vrm"},
        )
        vrma_id = app.config["FILE_STORE"].put(
            b"vrma",
            filename="avatar_source.vrma",
            content_type="application/octet-stream",
            metadata={"derived_for": model_id, "kind": "vrma"},
        )
        source.viewable_file_id = str(viewable_id)
        source.viewable_format = "glb"
        source.vrma_file_id = str(vrma_id)
        source.conversion_status = "done"
        source.save()
        ModelVariant.upsert(
            model_id, "vrm", str(vrm_id),
            file_format="vrm", size=68, status="ready",
        )

    browse = client.get("/api/models/browse?per_page=20")
    assert browse.status_code == 200, browse.get_json()
    assert model_id not in {item["id"] for item in browse.get_json()["models"]}

    public_models = client.get("/api/models")
    assert public_models.status_code == 200, public_models.get_json()
    assert model_id not in {item["id"] for item in public_models.get_json()["models"]}

    avatars = client.get("/api/vrm")
    assert avatars.status_code == 200, avatars.get_json()
    avatar = next(item for item in avatars.get_json()["avatars"] if item["model_id"] == model_id)
    assert avatar["id"] == model_id + ":vrm"
    assert avatar["view_url"].endswith(f"/api/model/{model_id}/vrm")
    assert private_vrm_id not in {item["model_id"] for item in avatars.get_json()["avatars"]}

    avatar_models = client.get("/api/vrm-models?include_private=true", headers=headers)
    assert avatar_models.status_code == 200, avatar_models.get_json()
    avatar_model_ids = {item["model_id"] for item in avatar_models.get_json()["avatars"]}
    assert model_id in avatar_model_ids
    assert private_vrm_id in avatar_model_ids

    animations = client.get("/api/vrma")
    assert animations.status_code == 200, animations.get_json()
    clip = next(item for item in animations.get_json()["animations"] if item["model_id"] == model_id)
    assert clip["id"] == model_id + ":vrma"
    assert clip["view_url"].endswith(f"/api/export/{model_id}?format=vrma")


def test_browse_asset_filters_include_vrm_and_animated_models():
    app = create_app()
    client = app.test_client()
    headers = {"Authorization": "Bearer test-token"}

    vrm_upload = client.post("/api/upload", headers=headers, data={
        "name": "Filterable Avatar",
        "is_public": "true",
        "file": (io.BytesIO(b"glTF avatar vrm filter" + b"\x13" * 64), "filter_avatar.vrm"),
    }, content_type="multipart/form-data")
    assert vrm_upload.status_code == 201, vrm_upload.get_json()
    vrm_id = vrm_upload.get_json()["model"]["id"]

    glb_upload = client.post("/api/upload", headers=headers, data={
        "name": "Converted Avatar Source",
        "is_public": "true",
        "asset_types": "character, avatar, vrm",
        "tags": "avatar, vrm",
        "file": (io.BytesIO(b"glTF avatar source filter" + b"\x14" * 64), "filter_avatar_source.glb"),
    }, content_type="multipart/form-data")
    assert glb_upload.status_code == 201, glb_upload.get_json()
    glb_id = glb_upload.get_json()["model"]["id"]
    with app.app_context():
        vrm_file_id = app.config["FILE_STORE"].put(
            b"vrm variant",
            filename="filter_avatar_source.vrm",
            content_type="model/gltf-binary",
            metadata={"derived_for": glb_id, "kind": "vrm"},
        )
        ModelVariant.upsert(glb_id, "vrm", str(vrm_file_id), file_format="vrm", size=11, status="ready")

    animated_upload = client.post("/api/upload", headers=headers, data={
        "name": "Animated Creature",
        "is_public": "true",
        "asset_types": "rigged, animated",
        "file": (io.BytesIO(_minimal_glb({
            "nodes": [{"name": "Armature"}],
            "animations": [{"name": "Hop", "channels": [], "samplers": []}],
        })), "animated_creature.glb"),
    }, content_type="multipart/form-data")
    assert animated_upload.status_code == 201, animated_upload.get_json()
    animated_id = animated_upload.get_json()["model"]["id"]

    vrm_browse = client.get("/api/models/browse?asset=vrm&per_page=50")
    assert vrm_browse.status_code == 200, vrm_browse.get_json()
    vrm_ids = {item["id"] for item in vrm_browse.get_json()["models"]}
    assert vrm_id in vrm_ids
    assert glb_id in vrm_ids
    assert animated_id not in vrm_ids

    animated_browse = client.get("/api/models/browse?asset=animated&per_page=50")
    assert animated_browse.status_code == 200, animated_browse.get_json()
    animated_ids = {item["id"] for item in animated_browse.get_json()["models"]}
    assert animated_id in animated_ids
    assert vrm_id not in animated_ids


def test_animations_page_renders_playable_clips_on_preview_avatar():
    app = create_app()
    client = app.test_client()
    headers = {"Authorization": "Bearer test-token"}

    avatar_upload = client.post("/api/upload", headers=headers, data={
        "is_public": "true",
        "file": (io.BytesIO(b"glTF avatar vrm" + b"\x08" * 64), "preview_avatar.vrm"),
    }, content_type="multipart/form-data")
    assert avatar_upload.status_code == 201, avatar_upload.get_json()

    clip_upload = client.post("/api/upload", headers=headers, data={
        "name": "Generated Dance Source",
        "is_public": "true",
        "file": (io.BytesIO(b"Kaydara FBX Binary" + b"\x0a" * 64), "dance_source.fbx"),
    }, content_type="multipart/form-data")
    assert clip_upload.status_code == 201, clip_upload.get_json()
    clip_id = clip_upload.get_json()["model"]["id"]

    with app.app_context():
        model = Model3D.get_by_id(clip_id)
        vrma_id = app.config["FILE_STORE"].put(
            b"vrma",
            filename="dance_source.vrma",
            content_type="application/octet-stream",
            metadata={"derived_for": clip_id, "kind": "vrma"},
        )
        model.vrma_file_id = str(vrma_id)
        model.runtime_metadata = {"animations": [{"name": "Friendly Wave"}]}
        model.save()

    page = client.get("/animations")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "Friendly Wave" in html
    assert "animation-vrm-preview" in html
    assert f'data-model-id="{clip_id}"' in html
    assert f'data-clip-id="{clip_id}:vrma"' in html
    assert "VRMA conversion needed" not in html


def test_vrm_avatar_rows_do_not_show_as_animation_clips():
    app = create_app()
    client = app.test_client()
    headers = {"Authorization": "Bearer test-token"}

    upload = client.post("/api/upload", headers=headers, data={
        "name": "Avatar With Motion",
        "is_public": "true",
        "asset_types": "animation, avatar-animation",
        "runtime_metadata": json.dumps({"animations": [{"name": "Idle"}]}),
        "file": (io.BytesIO(b"glTF avatar vrm" + b"\x0b" * 64), "avatar_with_motion.vrm"),
    }, content_type="multipart/form-data")
    assert upload.status_code == 201, upload.get_json()
    avatar_id = upload.get_json()["model"]["id"]

    with app.app_context():
        model = Model3D.get_by_id(avatar_id)
        vrma_id = app.config["FILE_STORE"].put(
            b"vrma",
            filename="avatar_idle.vrma",
            content_type="application/octet-stream",
            metadata={"derived_for": avatar_id, "kind": "vrma"},
        )
        model.vrma_file_id = str(vrma_id)
        model.save()

    animations_page = client.get("/animations")
    assert animations_page.status_code == 200
    html = animations_page.get_data(as_text=True)
    assert "Avatar With Motion" not in html
    assert f'data-model-id="{avatar_id}"' not in html

    avatars = client.get("/api/vrm-models")
    assert avatars.status_code == 200, avatars.get_json()
    assert avatar_id in {item["model_id"] for item in avatars.get_json()["avatars"]}


def test_vrm_avatars_stay_on_browse_and_avatar_endpoint():
    app = create_app()
    client = app.test_client()
    headers = {"Authorization": "Bearer test-token"}

    upload = client.post("/api/upload", headers=headers, data={
        "name": "Browseable Avatar",
        "is_public": "true",
        "asset_types": "avatar-animation",
        "runtime_metadata": json.dumps({"animations": [{"name": "Idle"}]}),
        "file": (io.BytesIO(b"glTF avatar vrm" + b"\x0c" * 64), "browseable_avatar.vrm"),
    }, content_type="multipart/form-data")
    assert upload.status_code == 201, upload.get_json()
    model = upload.get_json()["model"]
    avatar_id = model["id"]
    assert {"avatar", "vrm"}.issubset(set(model["tags"]))
    assert {"avatar", "vrm"}.issubset(set(model["asset_types"]))

    browse = client.get("/api/models/browse?type=avatar&per_page=20")
    assert browse.status_code == 200, browse.get_json()
    assert avatar_id in {item["id"] for item in browse.get_json()["models"]}

    public_models = client.get("/api/models?type=avatar")
    assert public_models.status_code == 200, public_models.get_json()
    assert avatar_id in {item["id"] for item in public_models.get_json()["models"]}

    avatars = client.get("/api/vrm-models")
    assert avatars.status_code == 200, avatars.get_json()
    assert avatar_id in {item["model_id"] for item in avatars.get_json()["avatars"]}

    animations_page = client.get("/animations")
    assert animations_page.status_code == 200
    assert "Browseable Avatar" not in animations_page.get_data(as_text=True)


def test_fbx_animation_source_without_vrma_is_animation_catalog_only():
    app = create_app()
    client = app.test_client()
    headers = {"Authorization": "Bearer test-token"}

    upload = client.post("/api/upload", headers=headers, data={
        "name": "Pharoah",
        "description": "Humanoid animation source.",
        "is_public": "true",
        "tags": "animation-library, animation-source, humanoid-animation",
        "asset_category": "animation",
        "asset_styles": "humanoid, vrm",
        "asset_types": "animation, humanoid, fbx",
        "runtime_metadata": json.dumps({
            "animations": [{"name": "Pharoah"}],
            "behaviors": ["avatar-animation"],
            "upload": {"source": "vrma-library-import"},
        }),
        "file": (io.BytesIO(b"Kaydara FBX Binary" + b"\x09" * 64), "Animations_pharoah.fbx"),
    }, content_type="multipart/form-data")
    assert upload.status_code == 201, upload.get_json()
    model_id = upload.get_json()["model"]["id"]
    assert upload.get_json()["model"]["has_vrma"] is False

    browse = client.get("/api/models/browse?per_page=20")
    assert browse.status_code == 200, browse.get_json()
    assert model_id not in {item["id"] for item in browse.get_json()["models"]}

    public_models = client.get("/api/models")
    assert public_models.status_code == 200, public_models.get_json()
    assert model_id not in {item["id"] for item in public_models.get_json()["models"]}

    media_queue = client.get("/api/admin/media-capture/queue?limit=20", headers=headers)
    assert media_queue.status_code == 200, media_queue.get_json()
    assert model_id not in {item["id"] for item in media_queue.get_json()["models"]}

    animations_api = client.get("/api/vrma")
    assert animations_api.status_code == 200, animations_api.get_json()
    assert model_id not in {item["model_id"] for item in animations_api.get_json()["animations"]}

    animations_page = client.get("/animations")
    assert animations_page.status_code == 200
    html = animations_page.get_data(as_text=True)
    assert "Pharoah" in html
    assert "VRMA conversion needed" in html


def test_upload_does_not_tag_static_unrigged_glb():
    app = create_app()
    client = app.test_client()
    headers = {"Authorization": "Bearer test-token"}
    glb = _minimal_glb({
        "asset": {"version": "2.0"},
        "nodes": [{"name": "Crate", "mesh": 0}],
        "meshes": [{}],
    })

    upload = client.post("/api/upload", headers=headers, data={
        "is_public": "false",
        "file": (io.BytesIO(glb), "static_crate.glb"),
    }, content_type="multipart/form-data")
    assert upload.status_code == 201, upload.get_json()
    model = upload.get_json()["model"]
    assert model["asset_types"] == []
    assert "animations" not in model["runtime_metadata"]


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
    assert model["asset_types"] == []

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

    _attach_thumbnail(app, model["id"])
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


def test_tellus_admin_upload_can_target_player_and_world_by_headers(monkeypatch):
    monkeypatch.setenv("TELLUS_ADMIN_API_TOKEN", "tellus-admin-token")
    monkeypatch.setenv("TELLUS_ADMIN_USERNAME", "tellusadmin")
    app = create_app()
    client = app.test_client()

    with app.app_context():
        player = _ensure_user("rsafier")
        admin = _ensure_user("tellusadmin")

    upload = client.post("/api/upload", headers={
        "Authorization": "Bearer tellus-admin-token",
        "X-Asset-Username": "rsafier",
        "X-Tellus-World-Id": "Crystal Arena",
    }, data={
        "name": "Crystal Arena Prop",
        "is_public": "false",
        "file": (io.BytesIO(b"glTF-crystal-arena" + b"\x00" * 64), "crystal_arena_prop.glb"),
    }, content_type="multipart/form-data")
    assert upload.status_code == 201, upload.get_json()
    model = upload.get_json()["model"]
    assert model["tags"] == ["tellus", "tellus-world-crystal-arena"]

    listed = client.get(
        "/api/models?include_private=true&search=tellus-world-crystal-arena",
        headers={"Authorization": "Bearer tellus-admin-token"},
    )
    assert listed.status_code == 200, listed.get_json()
    found = listed.get_json()["models"][0]
    assert found["id"] == model["id"]
    assert found["owner"]["id"] == player.id
    assert found["owner"]["id"] != admin.id


def test_tellus_world_upload_deletes_recent_pixal3d_direct_duplicate(monkeypatch):
    monkeypatch.setenv("TELLUS_ADMIN_API_TOKEN", "tellus-admin-token")
    monkeypatch.setenv("TELLUS_ADMIN_USERNAME", "tellusadmin")
    monkeypatch.setenv("BLOCK_LEGACY_PIXAL3D_UPLOADS", "0")
    app = create_app()
    client = app.test_client()

    with app.app_context():
        lisa = _ensure_user("lisa")
        _ensure_user("tellusadmin")

    direct_glb = _minimal_glb({
        "asset": {"version": "2.0"},
        "nodes": [{"name": "Mesh", "mesh": 0}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 1}, "indices": 2}]}],
        "accessors": [{}, {"count": 101}, {"count": 60}],
    })
    world_glb = _minimal_glb({
        "asset": {"version": "2.0"},
        "nodes": [{"name": "Mesh", "mesh": 0}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 1}, "indices": 2}]}],
        "accessors": [{}, {"count": 102}, {"count": 60}],
    })

    direct = client.post("/api/upload", headers={
        "Authorization": "Bearer test-token",
        "X-Asset-Username": "lisa",
    }, data={
        "name": "Pixal3D hyades-1234-direct",
        "description": "Generated by Pixal3D. resolution=1024",
        "is_public": "true",
        "tags": "pixal3d, generated, image-to-3d",
        "file": (io.BytesIO(direct_glb), "pixal3d-hyades-1234-direct.glb"),
    }, content_type="multipart/form-data")
    assert direct.status_code == 201, direct.get_json()
    direct_id = direct.get_json()["model"]["id"]

    world = client.post("/api/upload", headers={
        "Authorization": "Bearer tellus-admin-token",
        "X-Asset-Username": "lisa",
        "X-Tellus-World-Id": "Agent Test",
    }, data={
        "name": "a small fox den",
        "is_public": "true",
        "tags": "generated, agent",
        "file": (io.BytesIO(world_glb), "model.glb"),
    }, content_type="multipart/form-data")
    assert world.status_code == 201, world.get_json()
    world_model = world.get_json()["model"]
    assert {"tellus", "tellus-world-agent-test"}.issubset(set(world_model["tags"]))
    assert world_model["runtime_metadata"]["upload"]["world_id"] == "Agent Test"

    with app.app_context():
        assert Model3D.get_by_id(direct_id) is None
        assert Model3D.get_by_id(world_model["id"]) is not None
        user_models, _ = Model3D.get_user_models(lisa.id, page=1, per_page=10)
        assert [model.id for model in user_models] == [world_model["id"]]


def test_legacy_pixal3d_direct_uploads_are_blocked(monkeypatch):
    monkeypatch.setenv("TELLUS_ADMIN_API_TOKEN", "tellus-admin-token")
    app = create_app()
    client = app.test_client()

    with app.app_context():
        _ensure_user("lisa")

    glb = _minimal_glb({
        "asset": {"version": "2.0"},
        "nodes": [{"name": "Mesh", "mesh": 0}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 1}, "indices": 2}]}],
        "accessors": [{}, {"count": 101}, {"count": 60}],
    })

    direct = client.post("/api/upload", headers={
        "Authorization": "Bearer test-token",
        "X-Asset-Username": "lisa",
    }, data={
        "name": "Pixal3D hyades-1234-direct",
        "description": "Generated by Pixal3D. resolution=1024",
        "is_public": "true",
        "tags": "pixal3d, generated, image-to-3d",
        "file": (io.BytesIO(glb), "pixal3d-hyades-1234-direct.glb"),
    }, content_type="multipart/form-data")
    assert direct.status_code == 409, direct.get_json()
    assert "pixal3d direct uploads are disabled" in direct.get_json()["error"].lower()


def test_generation_id_dedupes_retry_uploads(monkeypatch):
    monkeypatch.setenv("TELLUS_ADMIN_API_TOKEN", "tellus-admin-token")
    monkeypatch.setenv("TELLUS_ADMIN_USERNAME", "tellusadmin")
    app = create_app()
    client = app.test_client()

    with app.app_context():
        _ensure_user("lisa")
        _ensure_user("tellusadmin")

    first_glb = _minimal_glb({
        "asset": {"version": "2.0"},
        "nodes": [{"name": "Mesh", "mesh": 0}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 1}, "indices": 2}]}],
        "accessors": [{}, {"count": 101}, {"count": 60}],
    })
    retry_glb = _minimal_glb({
        "asset": {"version": "2.0"},
        "nodes": [{"name": "Mesh", "mesh": 0}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 1}, "indices": 2}]}],
        "accessors": [{}, {"count": 130}, {"count": 90}],
    })

    first = client.post("/api/upload", headers={
        "Authorization": "Bearer tellus-admin-token",
        "X-Asset-Username": "lisa",
        "X-Tellus-World-Id": "Agent Test",
        "X-Generation-Id": "hyades-generation-123",
    }, data={
        "name": "a small white butterfly",
        "is_public": "true",
        "tags": "generated, agent",
        "file": (io.BytesIO(first_glb), "model.glb"),
    }, content_type="multipart/form-data")
    assert first.status_code == 201, first.get_json()
    first_model = first.get_json()["model"]
    assert first_model["runtime_metadata"]["upload"]["generation_id"] == "hyades-generation-123"

    retry = client.post("/api/upload", headers={
        "Authorization": "Bearer tellus-admin-token",
        "X-Asset-Username": "lisa",
        "X-Tellus-World-Id": "Agent Test",
        "X-Generation-Id": "hyades-generation-123",
    }, data={
        "name": "Pixal3D hyades-retry",
        "is_public": "true",
        "tags": "pixal3d, generated, image-to-3d",
        "file": (io.BytesIO(retry_glb), "pixal3d-hyades-retry.glb"),
    }, content_type="multipart/form-data")
    assert retry.status_code == 409, retry.get_json()
    assert "duplicate generation already exists" in retry.get_json()["error"].lower()


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
    assert "effective_file_size" in model_props
    assert "mesh_stats" in model_props
    assert "effective_mesh_stats" in model_props
    assert "runtime_metadata" in model_props
    assert "MeshStats" in spec["components"]["schemas"]
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
    _attach_thumbnail(app, model_id)

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


def test_autotag_requires_saved_thumbnail():
    app = create_app()
    client = app.test_client()
    headers = {"Authorization": "Bearer test-token"}
    glb = b"glTF-thumbnail-required" + b"\x00" * 64

    upload = client.post("/api/upload", headers=headers, data={
        "name": "Original No Thumb Title",
        "description": "Original no-thumbnail description.",
        "file": (io.BytesIO(glb), "needs_thumb.glb"),
    }, content_type="multipart/form-data")
    assert upload.status_code == 201, upload.get_json()
    model_id = upload.get_json()["model"]["id"]

    sync = client.post(
        f"/api/model/{model_id}/ai/autotag",
        headers=headers,
        json={"overwrite": True},
    )
    assert sync.status_code == 409, sync.get_json()
    assert sync.get_json()["error"] == "Thumbnail required"

    queued = client.post(
        f"/api/model/{model_id}/ai/autotag",
        headers=headers,
        json={"async": True},
    )
    assert queued.status_code == 409, queued.get_json()
    assert queued.get_json()["error"] == "Thumbnail required"
    with app.app_context():
        guarded_model = Model3D.get_by_id(model_id)
    assert guarded_model.name == "Original No Thumb Title"
    assert guarded_model.description == "Original no-thumbnail description."
    assert guarded_model.ai_status is None


def test_autotag_replaces_generic_no_thumbnail_copy(monkeypatch):
    app = create_app()
    client = app.test_client()
    headers = {"Authorization": "Bearer test-token"}
    glb = b"glTF-generic-copy" + b"\x00" * 64

    upload = client.post("/api/upload", headers=headers, data={
        "name": "Hyades",
        "description": (
            "As no preview thumbnail is available for visual analysis, specific details "
            "regarding its exact appearance, materials, and optimal use cases cannot be confirmed."
        ),
        "file": (io.BytesIO(glb), "generic_copy.glb"),
    }, content_type="multipart/form-data")
    assert upload.status_code == 201, upload.get_json()
    model_id = upload.get_json()["model"]["id"]
    _attach_thumbnail(app, model_id)

    def fake_enrich(stored_model, extra_context=None):
        return {
            "title": "Wooden Signpost",
            "description": "A stylized wooden signpost prop with a grassy base for fantasy scenes.",
            "summary": "Stylized wooden signpost prop.",
            "tags": ["signpost", "wooden", "fantasy"],
            "asset_category": "prop",
            "asset_styles": ["stylized", "fantasy"],
            "asset_types": ["decorative-prop"],
            "runtime_metadata": {},
            "categories": [],
            "quality_notes": [],
            "provider": "fake",
        }

    monkeypatch.setattr("app.ai_enrichment.enrich_model", fake_enrich)

    enrich = client.post(
        f"/api/model/{model_id}/ai/autotag",
        headers=headers,
        json={"overwrite": False, "include_title": True, "include_description": True},
    )
    assert enrich.status_code == 200, enrich.get_json()
    model = enrich.get_json()["model"]
    assert model["name"] == "Wooden Signpost"
    assert model["description"] == "A stylized wooden signpost prop with a grassy base for fantasy scenes."


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
    _attach_thumbnail(app, model_id)

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
    _attach_thumbnail(app, model_id)

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
    assert "light" not in model.runtime_metadata
    assert "light-emitter" not in model.asset_types


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
    assert enriched["runtime_metadata"] == {}


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
    assert "decorative-prop" in enriched["asset_types"]
    assert "static" not in enriched["asset_types"]


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
    assert enriched["runtime_metadata"] == {}


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
    assert "static" not in enriched["asset_types"]
    assert "light-emitter" not in enriched["asset_types"]
    assert {"high-poly", "low-poly"} != set(enriched["asset_types"]).intersection({"high-poly", "low-poly"})


def test_ai_enrichment_uses_signpost_vision_over_no_thumbnail_fallback(monkeypatch):
    from app import ai_enrichment

    class FakeModel:
        name = "Hyades"
        description = ""
        original_filename = "hyades.glb"
        file_format = "glb"
        file_size = 1710000
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
            "title": "Hyades",
            "asset_category": "flora",
            "asset_styles": ["stylized", "fantasy", "low-poly"],
            "asset_types": ["decorative-prop", "low-poly"],
            "runtime_metadata": {"behaviors": []},
            "tags": [],
            "description": (
                "A 3D model named Hyades. As no preview thumbnail is available for visual analysis, "
                "specific details regarding its exact appearance, materials, and optimal use cases cannot be confirmed."
            ),
            "summary": "A foundational component ready to be integrated and verified.",
            "categories": [],
            "quality_notes": [],
            "vision_mcp": True,
            "vision_mcp_analysis": (
                "The preview depicts a stylized, cartoon-like wooden signpost. "
                "It has a blank wooden signboard on a vertical post, rooted in a small circular patch of grass "
                "with rocks and foliage. It is a prop or environmental decoration and not a light emitter."
            ),
        }

    monkeypatch.setattr(ai_enrichment, "_ai_metadata", fake_ai_metadata)

    enriched = ai_enrichment.enrich_model(FakeModel())

    assert enriched["title"] == "Stylized Wooden Signpost"
    assert enriched["asset_category"] == "prop"
    assert enriched["description"].startswith("A stylized wooden signpost prop")
    assert {"signpost", "wooden-sign", "signboard"}.issubset(set(enriched["tags"]))
    assert {"stylized", "fantasy", "low-poly"}.issubset(set(enriched["asset_styles"]))
    assert {"decorative-prop", "low-poly"}.issubset(set(enriched["asset_types"]))
    assert "flora" not in enriched["asset_types"]


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
    assert "specialist in 3D assets" in system_text
    assert "writing marketing copy" in system_text
    assert "polished buyer-facing prose" in user_text
    assert "Keep the title under 80 characters" in user_text
    assert "Return up to 10 discoverability tags" in user_text
    assert schema["properties"]["tags"]["maxItems"] == 10
    assert "Fab listing title under 80 characters" in schema["properties"]["title"]["description"]
    assert "Buyer-facing Fab product description" in schema["properties"]["description"]["description"]
    assert enriched["title"] == "Classical Stone Fountain"
