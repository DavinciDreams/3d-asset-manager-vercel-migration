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


def _minimal_glb(gltf, bin_chunk=None):
    raw = json.dumps(gltf, separators=(",", ":")).encode("utf-8")
    json_chunk = raw + (b" " * ((4 - len(raw) % 4) % 4))
    chunks = [(0x4E4F534A, json_chunk)]
    if bin_chunk is not None:
        padded_bin = bin_chunk + (b"\0" * ((4 - len(bin_chunk) % 4) % 4))
        chunks.append((0x004E4942, padded_bin))
    length = 12 + sum(8 + len(chunk) for _, chunk in chunks)
    out = bytearray(b"glTF" + struct.pack("<II", 2, length))
    for chunk_type, chunk in chunks:
        out.extend(struct.pack("<II", len(chunk), chunk_type))
        out.extend(chunk)
    return bytes(out)


def test_derived_conversion_files_are_reused_by_content_hash():
    app = create_app()
    with app.app_context():
        first = conversion.put_derived_file(
            app,
            b"same derived avatar bytes",
            filename="avatar_a.vrm",
            content_type="model/gltf-binary",
            model_id="source-a",
            kind="vrm",
        )
        second = conversion.put_derived_file(
            app,
            b"same derived avatar bytes",
            filename="avatar_b.vrm",
            content_type="model/gltf-binary",
            model_id="source-b",
            kind="vrm",
        )
        third = conversion.put_derived_file(
            app,
            b"same derived avatar bytes",
            filename="clip_a.vrma",
            content_type="application/octet-stream",
            model_id="source-a",
            kind="vrma",
        )

    assert second == first
    assert third != first


def test_admin_conversion_backfill_force_requeues_done_fbx():
    app = create_app()
    client = app.test_client()
    headers = {"Authorization": "Bearer test-token"}

    upload = client.post("/api/upload", headers=headers, data={
        "name": "Legacy FBX Avatar",
        "is_public": "true",
        "file": (io.BytesIO(b"Kaydara FBX Binary legacy" + b"\x20" * 64), "legacy_avatar.fbx"),
    }, content_type="multipart/form-data")
    assert upload.status_code == 201, upload.get_json()
    model_id = upload.get_json()["model"]["id"]
    with app.app_context():
        model = Model3D.get_by_id(model_id)
        viewable_id = app.config["FILE_STORE"].put(
            b"legacy glb with external textures",
            filename="legacy_avatar.glb",
            content_type="model/gltf-binary",
            metadata={"derived_for": model_id, "kind": "viewable"},
        )
        model.viewable_file_id = str(viewable_id)
        model.viewable_format = "glb"
        model.conversion_status = "done"
        model.save()

    start = client.post("/api/admin/conversion-backfill?force=true&sync=true&limit=20", headers=headers)
    assert start.status_code == 200, start.get_json()
    status = start.get_json()
    assert status.get("running") is False
    assert status["queued"] >= 1
    with app.app_context():
        model = Model3D.get_by_id(model_id)
    assert model.conversion_status == "pending"
    assert model.conversion_error is None


def test_asset_admin_dashboard_can_start_conversion_backfill(monkeypatch):
    monkeypatch.setenv("ASSET_MANAGER_ADMIN_USERNAMES", "dashboard-admin")
    app = create_app()
    client = app.test_client()
    with app.app_context():
        owner = _ensure_user("dashboard-admin")
        source = _minimal_glb({"asset": {"version": "2.0"}, "scene": 0})
        Model3D(
            name="Dashboard LOD Target",
            file_format="glb",
            file_size=len(source),
            user_id=owner.id,
            is_public=True,
            gridfs_file_id=app.config["FILE_STORE"].put(
                source,
                filename="dashboard-lod-target.glb",
                content_type="model/gltf-binary",
                metadata={},
            ),
            asset_types=[],
        ).save()

    login = client.post("/auth/login", data={
        "login_field": "dashboard-admin",
        "password": "pw123456",
    }, follow_redirects=True)
    assert login.status_code == 200
    html = login.get_data(as_text=True)
    assert "Run conversion backfill" in html
    assert "Run LOD backfill" in html
    assert "Repair pipeline" in html
    assert "Import FBX avatars" in html
    assert "Check media queue" in html
    assert "/api/admin/conversion-backfill?force=true" in html
    assert "/api/admin/lod-backfill?force=true" in html
    assert "/api/admin/lod-backfill/status" in html
    assert "/api/admin/fbx-avatar-import?tag=robot" in html
    assert "/api/admin/pipeline/status" in html

    start = client.post("/api/admin/conversion-backfill?force=true&sync=true&limit=1")
    assert start.status_code == 200, start.get_json()
    status = start.get_json()
    assert status.get("running") is False
    assert "queued" in status

    pipeline = client.get("/api/admin/pipeline/status")
    assert pipeline.status_code == 200, pipeline.get_json()
    assert "pipeline" in pipeline.get_json()
    assert "media_queue" in pipeline.get_json()
    assert "thumbnail_render" in pipeline.get_json()
    assert set(pipeline.get_json()["thumbnail_render"]) == {"enabled", "available"}

    import app.api as api
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: "gltfpack" if name == "gltfpack" else None)
    monkeypatch.setattr(api, "_run_lod_optimizer", lambda model, owner_id=None: {"success": True})
    lod_start = client.post("/api/admin/lod-backfill?sync=true&force=true&limit=1")
    assert lod_start.status_code == 200, lod_start.get_json()
    lod_status = lod_start.get_json()
    assert lod_status.get("running") is False
    assert lod_status.get("done") == 1
    assert "skipped" in lod_status

    lod_poll = client.get("/api/admin/lod-backfill/status")
    assert lod_poll.status_code == 200, lod_poll.get_json()
    assert lod_poll.get_json().get("done") == 1


def test_lod_backfill_refreshes_stale_defaults_version(monkeypatch):
    import app.api as api
    import shutil

    app = create_app()
    calls = []
    monkeypatch.setattr(shutil, "which", lambda name: "gltfpack" if name == "gltfpack" else None)
    monkeypatch.setattr(api, "_run_lod_optimizer", lambda model, owner_id=None: calls.append(model.id) or {"success": True})

    with app.app_context():
        owner = _ensure_user("stale-lod-owner")
        source = _minimal_glb({"asset": {"version": "2.0"}, "scene": 0})
        model = Model3D(
            name="Stale LOD Target",
            file_format="glb",
            file_size=len(source),
            user_id=owner.id,
            is_public=True,
            gridfs_file_id=app.config["FILE_STORE"].put(
                source,
                filename="stale-lod-target.glb",
                content_type="model/gltf-binary",
                metadata={},
            ),
            asset_types=[],
        ).save()
        for level in (0, 1, 2, 3):
            file_id = app.config["FILE_STORE"].put(
                _minimal_glb({"asset": {"version": "2.0"}, "scene": level}),
                filename=f"stale-lod-target-lod{level}.glb",
                content_type="model/gltf-binary",
                metadata={"kind": "lod", "level": level, "source_model_id": model.id},
            )
            ModelVariant.upsert(
                model.id,
                "lod",
                str(file_id),
                level=level,
                file_format="glb",
                size=64,
                settings={"defaults_version": "old-defaults"},
            )

        result = api._optimize_missing_lod_variants(limit=5)

    assert result["optimized"] == 1
    assert calls == [model.id]


def test_pipeline_reconciler_reports_impostor_generation(monkeypatch):
    import app.api as api

    app = create_app()
    monkeypatch.setattr(api, "_optimize_missing_game_variants", lambda limit=3: {"optimized": 0})
    monkeypatch.setattr(api, "_optimize_missing_lod_variants", lambda limit=2: {"optimized": 0})
    monkeypatch.setattr(api, "_generate_missing_impostor_variants", lambda limit=5: {"generated": 2, "failed": 0})
    monkeypatch.setattr(api, "_requeue_missing_conversions", lambda limit=25: 0)
    monkeypatch.setattr(api, "_queue_thumbnail_ready_enrichment", lambda limit=25: 0)
    monkeypatch.setattr(api, "_sweep_stuck_media_capture", lambda: {"reset": 0, "failed": 0})
    monkeypatch.setattr(api, "_render_missing_thumbnails", lambda limit=10: {"rendered": 0, "failed": 0, "skipped": 0})
    monkeypatch.setattr(api, "_media_capture_queue_snapshot", lambda **kwargs: {"count": 0, "models": []})

    result = api._reconcile_asset_pipeline_once(
        app,
        optimize_limit=1,
        lod_limit=1,
        impostor_limit=1,
        enrich_limit=1,
        conversion_limit=1,
    )

    assert result["success"] is True
    assert result["impostors"] == {"generated": 2, "failed": 0}


def test_asset_admin_can_import_fbx_avatar_batch(monkeypatch, tmp_path):
    monkeypatch.setenv("ASSET_MANAGER_ADMIN_USERNAMES", "fbx-admin")
    source = tmp_path / "fbx"
    source.mkdir()
    (source / "blueMeshy_AI_biped_Meshy_AI_Character_output.fbx").write_bytes(
        b"Kaydara FBX Binary avatar" + b"\x20" * 64
    )
    monkeypatch.setenv("FBX_AVATAR_IMPORT_SOURCE", str(source))
    app = create_app()
    client = app.test_client()
    with app.app_context():
        owner = _ensure_user("fbx-admin")

    login = client.post("/auth/login", data={
        "login_field": "fbx-admin",
        "password": "pw123456",
    }, follow_redirects=True)
    assert login.status_code == 200

    start = client.post("/api/admin/fbx-avatar-import?tag=robot&sync=true")
    assert start.status_code == 200, start.get_json()

    import time
    status = {}
    for _ in range(50):
        status = client.get("/api/admin/fbx-avatar-import/status").get_json()
        if not status.get("running"):
            break
        time.sleep(0.05)

    assert status.get("running") is False
    assert status["total"] == 1
    assert status["imported"] == 1
    with app.app_context():
        models, total = Model3D.get_user_models(owner.id)
    assert total == 1
    assert len(models) == 1
    model = models[0]
    assert model.name == "Blue Avatar"
    assert model.asset_category == "person"
    assert "avatar" in model.asset_types
    assert "robot" in model.tags
    assert model.file_format == "fbx"

    again = client.post("/api/admin/fbx-avatar-import?tag=robot&sync=true")
    assert again.status_code == 200, again.get_json()
    again_status = again.get_json()
    assert again_status["imported"] == 0
    assert again_status["skipped"] == 1


def test_uncategorized_gltf_upload_appears_in_browse():
    app = create_app()
    client = app.test_client()
    headers = {"Authorization": "Bearer test-token"}
    gltf = json.dumps({
        "asset": {"version": "2.0"},
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0}],
        "meshes": [{"primitives": []}],
    }).encode("utf-8")

    upload = client.post("/api/upload", headers=headers, data={
        "name": "Uncategorized GLTF Probe",
        "is_public": "true",
        "file": (io.BytesIO(gltf), "uncategorized_probe.gltf"),
    }, content_type="multipart/form-data")
    assert upload.status_code == 201, upload.get_json()
    model_id = upload.get_json()["model"]["id"]

    public_models = client.get("/api/models?per_page=10")
    assert public_models.status_code == 200, public_models.get_json()
    assert model_id in {item["id"] for item in public_models.get_json()["models"]}

    browse = client.get("/browse")
    assert browse.status_code == 200
    assert "Uncategorized GLTF Probe" in browse.get_data(as_text=True)


def test_browse_owner_cards_surface_category_dropdown_instead_of_tags():
    app = create_app()
    client = app.test_client()
    with app.app_context():
        _ensure_user("browse-owner")

    login = client.post("/auth/login", data={
        "login_field": "browse-owner",
        "password": "pw123456",
    }, follow_redirects=True)
    assert login.status_code == 200

    upload = client.post("/api/upload", data={
        "name": "Category Editable Browse Probe",
        "is_public": "true",
        "asset_category": "fauna",
        "tags": "legacy-tag",
        "file": (io.BytesIO(b"glTF" + b"\x21" * 64), "category_editable.glb"),
    }, content_type="multipart/form-data")
    assert upload.status_code == 201, upload.get_json()
    model_id = upload.get_json()["model"]["id"]

    browse = client.get("/browse")
    assert browse.status_code == 200
    html = browse.get_data(as_text=True)
    assert "Category Editable Browse Probe" in html
    assert f'data-category-select\n                                data-model-id="{model_id}"' in html
    assert f'data-saved-value="fauna"' in html
    assert '<option value="fauna" selected>Fauna</option>' in html
    assert "legacy-tag" not in html


def test_fbx_with_vrm_variant_stays_in_model_browse():
    app = create_app()
    client = app.test_client()
    headers = {"Authorization": "Bearer test-token"}

    upload = client.post("/api/upload", headers=headers, data={
        "name": "Converted Avatar Shell",
        "is_public": "true",
        "file": (io.BytesIO(b"Kaydara FBX Binary avatar shell" + b"\x20" * 64), "avatar_shell.fbx"),
    }, content_type="multipart/form-data")
    assert upload.status_code == 201, upload.get_json()
    model_id = upload.get_json()["model"]["id"]

    with app.app_context():
        model = Model3D.get_by_id(model_id)
        model.vrma_file_id = app.config["FILE_STORE"].put(
            b"vrma bytes",
            filename="clip.vrma",
            content_type="application/octet-stream",
            metadata={"kind": "vrma"},
        )
        model.tags = ["mythology"]
        model.asset_types = ["character"]
        model.asset_category = "material"
        model.save()
        vrm_id = app.config["FILE_STORE"].put(
            b"glTF" + b"\x00" * 32,
            filename="avatar.vrm",
            content_type="model/gltf-binary",
            metadata={"kind": "vrm"},
        )
        ModelVariant.upsert(model_id, "vrm", str(vrm_id), file_format="vrm", size=36, status="ready")

        browse_items, total = Model3D.get_public_models(exclude_formats=["vrma", "bvh"])
    assert total >= 1
    assert any(item.id == model_id for item in browse_items)


def test_dashboard_shows_game_optimized_size():
    app = create_app()
    client = app.test_client()
    with app.app_context():
        owner = _ensure_user("dashboard-size-owner")

    login = client.post("/auth/login", data={
        "login_field": "dashboard-size-owner",
        "password": "pw123456",
    }, follow_redirects=True)
    assert login.status_code == 200

    upload = client.post("/api/upload", data={
        "name": "Optimized Size Probe",
        "is_public": "true",
        "file": (io.BytesIO(b"glTF" + b"\x00" * 8192), "optimized_size_probe.glb"),
    }, content_type="multipart/form-data")
    assert upload.status_code == 201, upload.get_json()
    model_id = upload.get_json()["model"]["id"]

    with app.app_context():
        file_id = app.config["FILE_STORE"].put(
            b"optimized" * 256,
            filename="optimized_size_probe.game.glb",
            content_type="model/gltf-binary",
            metadata={"derived_for": model_id, "kind": "game"},
        )
        ModelVariant.upsert(model_id, "game", str(file_id), file_format="glb", size=2048, status="ready")

    dashboard = client.get("/dashboard")
    assert dashboard.status_code == 200
    html = dashboard.get_data(as_text=True)
    assert "Optimized Size Probe" in html
    assert "2.0 KB" in html
    assert "optimized" in html
    assert "Original: 8.0 KB" in html


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


def test_materials_category_is_not_persisted_or_listed_as_facet():
    app = create_app()
    client = app.test_client()
    headers = {"Authorization": "Bearer test-token"}

    upload = client.post("/api/upload", headers=headers, data={
        "is_public": "true",
        "asset_category": "materials",
        "file": (io.BytesIO(b"glTF" + b"\x00" * 64), "tileable_surface.glb"),
    }, content_type="multipart/form-data")
    assert upload.status_code == 201, upload.get_json()
    model_id = upload.get_json()["model"]["id"]
    assert upload.get_json()["model"]["asset_category"] is None

    with app.app_context():
        model = Model3D.get_by_id(model_id)
        model.asset_category = "material"
        model.save()
        facets = Model3D.get_public_facets()
    assert "material" not in facets["categories"]
    assert "materials" not in facets["categories"]


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
                "runtime_cost": {
                    "triangle_count": 12,
                    "vertex_count": 24,
                    "texture_count": 1,
                    "largest_texture_bytes": 4096,
                    "total_byte_size": 321,
                    "ktx2": True,
                    "ktx2_produced": True,
                    "approx_vram_bytes": 8192,
                    "preset": "balanced",
                    "defaults_version": "2026-06-17",
                },
                "preset": "balanced",
                "defaults_version": "2026-06-17",
                "texture_compression": "KTX2/Basis",
            },
        )

    fetched = client.get(f"/api/model/{model['id']}", headers=headers)
    assert fetched.status_code == 200, fetched.get_json()
    fetched_model = fetched.get_json()["model"]
    assert fetched_model["effective_file_size"] == 321
    assert fetched_model["game_optimized"]["mesh_stats"] == {"vertices": 24, "triangles": 12, "primitives": 1}
    assert fetched_model["game_optimized"]["runtime_cost"]["triangle_count"] == 12
    assert fetched_model["game_optimized"]["runtime_cost"]["ktx2_produced"] is True
    assert fetched_model["game_optimized"]["optimization"]["preset"] == "balanced"
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


def test_asset_admin_can_overwrite_rigged_variant(monkeypatch):
    monkeypatch.setenv("ASSET_MANAGER_ADMIN_USERNAMES", "rig-admin")
    app = create_app()
    client = app.test_client()
    with app.app_context():
        owner = _ensure_user("rig-owner")
        _ensure_user("rig-admin")
        model = Model3D(
            name="Admin Rig Target",
            file_format="glb",
            file_size=8,
            user_id=owner.id,
            is_public=False,
            gridfs_file_id=app.config["FILE_STORE"].put(
                _minimal_glb({"asset": {"version": "2.0"}}),
                filename="target.glb",
                content_type="model/gltf-binary",
                metadata={},
            ),
        ).save()

    login = client.post("/auth/login", data={
        "login_field": "rig-admin",
        "password": "pw123456",
    }, follow_redirects=True)
    assert login.status_code == 200

    rigged = _minimal_glb({"asset": {"version": "2.0"}, "nodes": []})
    response = client.post(f"/api/model/{model.id}/rig", data={
        "file": (io.BytesIO(rigged), "rerigged.glb"),
        "overwrite": "1",
    }, content_type="multipart/form-data")
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["success"] is True
    with app.app_context():
        variant = ModelVariant.get(model.id, "rigged")
    assert variant is not None
    assert variant.status == "ready"


def test_owner_can_attach_animated_roundtrip_to_original_model():
    app = create_app()
    client = app.test_client()
    with app.app_context():
        owner = _ensure_user("roundtrip-owner")
        model = Model3D(
            name="Roundtrip Target",
            file_format="glb",
            file_size=8,
            user_id=owner.id,
            is_public=False,
            gridfs_file_id=app.config["FILE_STORE"].put(
                _minimal_glb({"asset": {"version": "2.0"}}),
                filename="target.glb",
                content_type="model/gltf-binary",
                metadata={},
            ),
        ).save()

    login = client.post("/auth/login", data={
        "login_field": "roundtrip-owner",
        "password": "pw123456",
    }, follow_redirects=True)
    assert login.status_code == 200

    animated = _minimal_glb({
        "asset": {"version": "2.0"},
        "nodes": [{"name": "Armature"}],
        "skins": [{"joints": [0]}],
        "animations": [{"name": "Wave"}],
    })
    response = client.post(f"/api/model/{model.id}/animation-source", data={
        "file": (io.BytesIO(animated), "target_wave.glb"),
        "reoptimize": "0",
    }, content_type="multipart/form-data")
    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    assert body["success"] is True
    assert body["animations"] == [{"name": "Wave"}]
    assert "animated" in body["model"]["asset_types"]
    assert "rigged" in body["model"]["asset_types"]

    with app.app_context():
        saved = Model3D.get_by_id(model.id)
        variant = ModelVariant.get(model.id, "rigged")
    assert saved.runtime_metadata["animations"] == [{"name": "Wave"}]
    assert variant is not None
    assert variant.settings["source"] == "animation_roundtrip"
    assert variant.settings["original_filename"] == "target_wave.glb"

    detail = client.get(f"/model/{model.id}")
    assert detail.status_code == 200
    html = detail.get_data(as_text=True)
    assert 'id="variant-btn-rigged"' in html
    assert "switchVariant('rigged')" in html
    assert f"/api/model/${{modelId}}/rigged" in html
    assert "if (HAS_RIGGED_VARIANT) return 'rigged';" in html
    assert "{%" not in html
    assert "{{" not in html


def test_game_optimizer_prefers_uploaded_animated_roundtrip_source():
    import app.api as api

    app = create_app()
    with app.app_context():
        owner = _ensure_user("roundtrip-opt-owner")
        original = _minimal_glb({
            "asset": {"version": "2.0"},
            "extensionsUsed": ["EXT_meshopt_compression"],
        })
        animated = _minimal_glb({
            "asset": {"version": "2.0"},
            "extensionsUsed": ["EXT_meshopt_compression"],
            "nodes": [{"name": "Armature"}],
            "skins": [{"joints": [0]}],
            "animations": [{"name": "Run"}],
        })
        model = Model3D(
            name="Optimizer Roundtrip Target",
            file_format="glb",
            file_size=len(original),
            user_id=owner.id,
            is_public=False,
            gridfs_file_id=app.config["FILE_STORE"].put(
                original,
                filename="target.glb",
                content_type="model/gltf-binary",
                metadata={},
            ),
            asset_types=[],
        ).save()
        rigged_id = app.config["FILE_STORE"].put(
            animated,
            filename="target-run.glb",
            content_type="model/gltf-binary",
            metadata={"kind": "animation_source", "source_model_id": model.id},
        )
        ModelVariant.upsert(
            model.id, "rigged", str(rigged_id),
            file_format="glb", size=len(animated),
            settings={"source": "animation_roundtrip"},
            status="ready",
        )

        result = api._run_game_optimizer(model, owner.id, {})
        game = ModelVariant.get(model.id, "game")

    assert result["success"] is True
    assert result["source_variant_kind"] == "rigged"
    assert result["source_is_rigged"] is True
    assert result["original_size"] == len(animated)
    assert game.settings["source_variant_kind"] == "rigged"
    assert game.settings["source_is_rigged"] is True


def test_lod_optimizer_generates_levels_from_original_asset(monkeypatch):
    import app.api as api
    import shutil
    import subprocess

    app = create_app()
    calls = []

    monkeypatch.setattr(shutil, "which", lambda name: "gltfpack" if name == "gltfpack" else None)

    def fake_run(cmd, capture_output=True, text=True, timeout=300, check=False):
        calls.append(list(cmd))
        out_path = Path(cmd[cmd.index("-o") + 1])
        ratio = cmd[cmd.index("-si") + 1] if "-si" in cmd else "flat-repack"
        out_path.write_bytes(_minimal_glb({
            "asset": {"version": "2.0"},
            "extras": {"ratio": ratio},
            "materials": [{"pbrMetallicRoughness": {"baseColorFactor": [1, 1, 1, 1]}}],
        }))
        report_path = Path(cmd[cmd.index("-r") + 1])
        report_path.write_text(json.dumps({"ratio": ratio}), encoding="utf-8")

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()

    monkeypatch.setattr(subprocess, "run", fake_run)

    with app.app_context():
        owner = _ensure_user("lod-opt-owner")
        source = _minimal_glb({"asset": {"version": "2.0"}, "scene": 0})
        model = Model3D(
            name="LOD Optimizer Source",
            file_format="glb",
            file_size=len(source),
            user_id=owner.id,
            is_public=True,
            gridfs_file_id=app.config["FILE_STORE"].put(
                source,
                filename="lod-opt-source.glb",
                content_type="model/gltf-binary",
                metadata={},
            ),
            asset_types=[],
        ).save()

        result = api._run_lod_optimizer(model, owner.id)
        lod0 = ModelVariant.get(model.id, "lod", level=0)
        lod1 = ModelVariant.get(model.id, "lod", level=1)
        lod2 = ModelVariant.get(model.id, "lod", level=2)
        lod3 = ModelVariant.get(model.id, "lod", level=3)
        stored_lod2 = app.config["FILE_STORE"].get(lod2.file_id)

    assert result["success"] is True
    assert [level["level"] for level in result["levels"]] == [0, 1, 2, 3]
    simplify_calls = [cmd for cmd in calls if "-si" in cmd]
    repack_calls = [cmd for cmd in calls if "-si" not in cmd]
    assert [cmd[cmd.index("-si") + 1] for cmd in simplify_calls] == ["0.85", "0.18", "0.18", "0.015"]
    assert "-sa" in calls[1]
    assert "-sp" in calls[1]
    assert calls[1][calls[1].index("-se") + 1] == "0.03"
    assert calls[1][calls[1].index("-tl") + 1] == "512"
    assert "-sa" in calls[2]
    assert "-sp" in calls[2]
    assert calls[2][calls[2].index("-se") + 1] == "0.03"
    assert calls[2][calls[2].index("-tl") + 1] == "512"
    assert "-sa" in calls[3]
    assert "-sp" in calls[3]
    assert calls[3][calls[3].index("-se") + 1] == "0.08"
    assert "-tl" not in calls[3]
    assert all(cmd[cmd.index("-i") + 1].endswith("input.glb") for cmd in simplify_calls)
    assert len(repack_calls) == 1
    assert repack_calls[0][repack_calls[0].index("-i") + 1].endswith("lod3-flat-input.glb")
    assert lod0.settings["role"] == "near/game"
    assert lod1.settings["texture_limit"] == 512
    assert lod1.settings["simplify_ratio"] == 0.18
    assert lod1.settings["target_vertices"] == 20000
    assert lod1.settings["aggressive"] is True
    assert lod1.settings["permissive"] is True
    assert lod1.settings["role"] == "mid/fill"
    assert lod2.settings["texture_limit"] == 512
    assert lod2.settings["simplify_ratio"] == 0.18
    assert lod2.settings["target_vertices"] == 20000
    assert lod2.settings["aggressive"] is True
    assert lod2.settings["permissive"] is True
    assert lod2.settings["role"] == "far/large-fill"
    assert lod3.settings["texture_limit"] == 0
    assert lod3.settings["simplify_ratio"] == 0.015
    assert lod3.settings["target_vertices"] == 500
    assert lod3.settings["aggressive"] is True
    assert lod3.settings["permissive"] is True
    assert lod3.settings["flat_material"] is True
    assert lod3.settings["flat_material_mode"] == "texture_color_buckets"
    assert lod3.settings["flat_material_color"] == [0.30, 0.42, 0.20, 1.0]
    assert lod3.settings["flat_material_accent_color"] == [0xd9 / 255, 0x6a / 255, 0x28 / 255, 1.0]
    assert lod3.settings["flat_material_stage"] == "post_simplification"
    assert lod3.settings["role"] == "ultra-far/two-color-flat-proxy"
    assert stored_lod2.filename.endswith("-lod2.glb")


def test_lod_flat_material_color_can_be_configured(monkeypatch):
    import app.api as api

    monkeypatch.setenv("LOD3_FLAT_COLOR", "#336622cc")
    assert api._lod_flat_material_color() == [
        0x33 / 255,
        0x66 / 255,
        0x22 / 255,
        0xcc / 255,
    ]

    monkeypatch.setenv("LOD3_FLAT_COLOR", "80, 120, 40")
    assert api._lod_flat_material_color() == [80 / 255, 120 / 255, 40 / 255, 1.0]


def test_color_bucket_flatten_splits_textured_triangles():
    import app.api as api
    from PIL import Image

    image = Image.new("RGBA", (2, 1))
    image.putpixel((0, 0), (40, 180, 40, 255))
    image.putpixel((1, 0), (230, 90, 20, 255))
    image_io = io.BytesIO()
    image.save(image_io, format="PNG")
    image_bytes = image_io.getvalue()

    positions = struct.pack(
        "<18f",
        0, 0, 0, 1, 0, 0, 0, 1, 0,
        1, 0, 0, 2, 0, 0, 1, 1, 0,
    )
    uvs = struct.pack(
        "<12f",
        0.1, 0.5, 0.2, 0.5, 0.1, 0.5,
        0.8, 0.5, 0.9, 0.5, 0.8, 0.5,
    )
    indices = struct.pack("<6H", 0, 1, 2, 3, 4, 5)
    bin_chunk = positions + uvs + indices + image_bytes
    image_offset = len(positions) + len(uvs) + len(indices)
    gltf = {
        "asset": {"version": "2.0"},
        "buffers": [{"byteLength": len(bin_chunk)}],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": len(positions), "target": 34962},
            {"buffer": 0, "byteOffset": len(positions), "byteLength": len(uvs), "target": 34962},
            {"buffer": 0, "byteOffset": len(positions) + len(uvs), "byteLength": len(indices), "target": 34963},
            {"buffer": 0, "byteOffset": image_offset, "byteLength": len(image_bytes)},
        ],
        "accessors": [
            {"bufferView": 0, "componentType": 5126, "count": 6, "type": "VEC3"},
            {"bufferView": 1, "componentType": 5126, "count": 6, "type": "VEC2"},
            {"bufferView": 2, "componentType": 5123, "count": 6, "type": "SCALAR"},
        ],
        "images": [{"bufferView": 3, "mimeType": "image/png"}],
        "textures": [{"source": 0}],
        "materials": [{"pbrMetallicRoughness": {"baseColorTexture": {"index": 0}}}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0, "TEXCOORD_0": 1}, "indices": 2, "material": 0}]}],
    }

    out = api._flatten_lod_glb_materials(
        _minimal_glb(gltf, bin_chunk),
        color=[0.2, 0.6, 0.2, 1],
        accent_color=[0.9, 0.3, 0.1, 1],
        color_buckets=True,
    )
    out_gltf = api._gltf_json_from_bytes(out, "glb")
    assert len(out_gltf["materials"]) == 2
    assert len(out_gltf["meshes"][0]["primitives"]) == 2
    assert {primitive["material"] for primitive in out_gltf["meshes"][0]["primitives"]} == {0, 1}
    assert "images" not in out_gltf


def test_lod_optimizer_decodes_draco_sources_before_gltfpack(monkeypatch, tmp_path):
    import app.api as api
    import shutil
    import subprocess

    app = create_app()
    calls = []
    cli_path = tmp_path / "gltf-transform-cli.js"
    cli_path.write_text("", encoding="utf-8")

    def fake_which(name):
        if name == "gltfpack":
            return "gltfpack"
        if name == "node":
            return "node"
        return None

    def fake_run(cmd, capture_output=True, text=True, timeout=300, check=False):
        calls.append(list(cmd))
        if "copy" in cmd:
            out_path = Path(cmd[cmd.index("copy") + 2])
            out_path.write_bytes(_minimal_glb({"asset": {"version": "2.0"}, "scene": 0}))
        else:
            out_path = Path(cmd[cmd.index("-o") + 1])
            ratio = cmd[cmd.index("-si") + 1] if "-si" in cmd else "flat-repack"
            out_path.write_bytes(_minimal_glb({
                "asset": {"version": "2.0"},
                "extras": {"ratio": ratio},
                "materials": [{"pbrMetallicRoughness": {"baseColorFactor": [1, 1, 1, 1]}}],
            }))
            report_path = Path(cmd[cmd.index("-r") + 1])
            report_path.write_text(json.dumps({"ratio": ratio}), encoding="utf-8")

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()

    monkeypatch.setattr(shutil, "which", fake_which)
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(api, "_gltf_transform_cli_path", lambda: cli_path)

    with app.app_context():
        owner = _ensure_user("lod-draco-owner")
        source = _minimal_glb({
            "asset": {"version": "2.0"},
            "extensionsUsed": ["KHR_draco_mesh_compression"],
            "meshes": [{
                "primitives": [{
                    "attributes": {"POSITION": 0},
                    "extensions": {"KHR_draco_mesh_compression": {"bufferView": 0, "attributes": {"POSITION": 0}}},
                }],
            }],
            "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": 0}],
            "buffers": [{"byteLength": 0}],
        })
        model = Model3D(
            name="LOD Draco Source",
            file_format="glb",
            file_size=len(source),
            user_id=owner.id,
            is_public=True,
            gridfs_file_id=app.config["FILE_STORE"].put(
                source,
                filename="lod-draco-source.glb",
                content_type="model/gltf-binary",
                metadata={},
            ),
            asset_types=[],
        ).save()

        result = api._run_lod_optimizer(model, owner.id)
        lod0 = ModelVariant.get(model.id, "lod", level=0)

    assert result["success"] is True
    assert calls[0][2] == "copy"
    assert calls[0][3].endswith("input.glb")
    assert calls[0][4].endswith("input-dedraco.glb")
    gltfpack_inputs = [cmd[cmd.index("-i") + 1] for cmd in calls[1:] if "-si" in cmd]
    assert gltfpack_inputs == [calls[0][4], calls[0][4], calls[0][4], calls[0][4]]
    assert lod0.settings["source_prepare"]["draco_decompressed"] is True


def test_lod_optimizer_decodes_meshopt_external_fallback_before_gltfpack(monkeypatch, tmp_path):
    import app.api as api
    import shutil
    import subprocess

    app = create_app()
    calls = []
    decode_script = tmp_path / "decode-meshopt-glb.mjs"
    decode_script.write_text("", encoding="utf-8")

    def fake_which(name):
        if name == "gltfpack":
            return "gltfpack"
        if name == "node":
            return "node"
        return None

    def fake_run(cmd, capture_output=True, text=True, timeout=300, check=False):
        calls.append(list(cmd))
        if str(decode_script) in cmd:
            out_path = Path(cmd[-1])
            out_path.write_bytes(_minimal_glb({"asset": {"version": "2.0"}, "scene": 0}))
        else:
            out_path = Path(cmd[cmd.index("-o") + 1])
            ratio = cmd[cmd.index("-si") + 1] if "-si" in cmd else "flat-repack"
            out_path.write_bytes(_minimal_glb({
                "asset": {"version": "2.0"},
                "extras": {"ratio": ratio},
                "materials": [{"pbrMetallicRoughness": {"baseColorFactor": [1, 1, 1, 1]}}],
            }))
            report_path = Path(cmd[cmd.index("-r") + 1])
            report_path.write_text(json.dumps({"ratio": ratio}), encoding="utf-8")

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()

    monkeypatch.setattr(shutil, "which", fake_which)
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(api, "_meshopt_decode_script_path", lambda: decode_script)

    with app.app_context():
        owner = _ensure_user("lod-meshopt-owner")
        source = _minimal_glb({
            "asset": {"version": "2.0"},
            "extensionsUsed": ["EXT_meshopt_compression"],
            "extensionsRequired": ["EXT_meshopt_compression"],
            "meshes": [{
                "primitives": [{
                    "attributes": {"POSITION": 0},
                }],
            }],
            "accessors": [{
                "bufferView": 0,
                "componentType": 5126,
                "count": 3,
                "type": "VEC3",
            }],
            "bufferViews": [{
                "buffer": 1,
                "byteOffset": 0,
                "byteLength": 36,
                "extensions": {
                    "EXT_meshopt_compression": {
                        "buffer": 0,
                        "byteOffset": 0,
                        "byteLength": 16,
                        "byteStride": 12,
                        "mode": "ATTRIBUTES",
                        "count": 3,
                    },
                },
            }],
            "buffers": [
                {"byteLength": 16},
                {
                    "uri": "missing.optimized.fallback.bin",
                    "byteLength": 36,
                    "extensions": {"EXT_meshopt_compression": {"fallback": True}},
                },
            ],
        }, bin_chunk=b"\0" * 16)
        model = Model3D(
            name="LOD Meshopt Fallback Source",
            file_format="glb",
            file_size=len(source),
            user_id=owner.id,
            is_public=True,
            gridfs_file_id=app.config["FILE_STORE"].put(
                source,
                filename="lod-meshopt-fallback-source.glb",
                content_type="model/gltf-binary",
                metadata={},
            ),
            asset_types=[],
        ).save()

        result = api._run_lod_optimizer(model, owner.id)
        lod0 = ModelVariant.get(model.id, "lod", level=0)

    assert result["success"] is True
    assert calls[0][0] == "node"
    assert calls[0][1] == str(decode_script)
    assert calls[0][2].endswith("input-meshopt.glb")
    assert calls[0][3].endswith("input-demeshopt.glb")
    gltfpack_inputs = [cmd[cmd.index("-i") + 1] for cmd in calls[1:] if "-si" in cmd]
    assert gltfpack_inputs == [calls[0][3], calls[0][3], calls[0][3], calls[0][3]]
    assert lod0.settings["source_prepare"]["meshopt_decompressed"] is True
    assert lod0.settings["source_prepare"]["meshopt_decoder"] == "tools/decode-meshopt-glb.mjs"
    assert lod0.settings["source_prepare"]["draco_decompressed"] is False


def test_impostor_generator_prefers_octahedral_atlas(monkeypatch):
    import app.api as api
    from app import render as render_mod

    app = create_app()
    monkeypatch.setattr(render_mod, "render_available", lambda: True)
    monkeypatch.setattr(
        render_mod,
        "render_glb_to_octahedral_atlas",
        lambda *args, **kwargs: (
            b"atlas png bytes",
            {
                "atlas_width": 2048,
                "atlas_height": 2048,
                "grid_size_x": 31,
                "grid_size_y": 31,
                "cell_size": 66,
                "view_count": 961,
                "octahedron_type": "hemi",
            },
        ),
    )
    monkeypatch.setattr(
        api,
        "_encode_impostor_atlas_webp",
        lambda image_bytes: (b"RIFFatlasWEBP", 2048, 2048),
    )

    with app.app_context():
        owner = _ensure_user("impostor-owner")
        source = _minimal_glb({"asset": {"version": "2.0"}, "scene": 0})
        model = Model3D(
            name="Impostor Source",
            file_format="glb",
            file_size=len(source),
            user_id=owner.id,
            is_public=True,
            gridfs_file_id=app.config["FILE_STORE"].put(
                source,
                filename="impostor-source.glb",
                content_type="model/gltf-binary",
                metadata={},
            ),
            asset_types=[],
        ).save()

        result = api._run_impostor_generator(model, owner.id)
        variant = ModelVariant.get(model.id, "impostor")
        stored = app.config["FILE_STORE"].get(variant.file_id)

    assert result["success"] is True
    assert result["type"] == "octahedral_atlas"
    assert result["source"] == "octahedral_server_render"
    assert result["width"] == 2048
    assert result["view_count"] == 961
    assert variant.file_format == "webp"
    assert variant.size == len(b"RIFFatlasWEBP")
    assert variant.settings["role"] == "far/octahedral"
    assert variant.settings["grid_size_x"] == 31
    assert variant.settings["octahedron_type"] == "hemi"
    assert stored.content_type == "image/webp"
    assert stored.read() == b"RIFFatlasWEBP"


def test_impostor_generator_falls_back_to_thumbnail_billboard(monkeypatch):
    import app.api as api

    app = create_app()
    monkeypatch.setattr(api, "_octahedral_impostor_source_bytes", lambda model: (_ for _ in ()).throw(RuntimeError("no renderer")))
    monkeypatch.setattr(
        api,
        "_encode_impostor_webp",
        lambda image_bytes, size=512: (b"RIFFxxxxWEBP", 512, 512),
    )
    with app.app_context():
        owner = _ensure_user("impostor-fallback-owner")
        source = _minimal_glb({"asset": {"version": "2.0"}, "scene": 0})
        thumbnail_id = app.config["FILE_STORE"].put(
            b"thumbnail image bytes",
            filename="thumb.webp",
            content_type="image/webp",
            metadata={"kind": "thumbnail"},
        )
        model = Model3D(
            name="Impostor Fallback Source",
            file_format="glb",
            file_size=len(source),
            user_id=owner.id,
            is_public=True,
            gridfs_file_id=app.config["FILE_STORE"].put(
                source,
                filename="impostor-fallback-source.glb",
                content_type="model/gltf-binary",
                metadata={},
            ),
            thumbnail_file_id=str(thumbnail_id),
            asset_types=[],
        ).save()

        result = api._run_impostor_generator(model, owner.id)
        variant = ModelVariant.get(model.id, "impostor")

    assert result["success"] is True
    assert result["type"] == "billboard"
    assert result["source"] == "thumbnail"
    assert result["width"] == 512
    assert variant.settings["role"] == "far/billboard"


def test_game_optimizer_decodes_draco_sources_before_gltfpack(monkeypatch, tmp_path):
    import app.api as api
    import shutil
    import subprocess

    app = create_app()
    calls = []
    cli_path = tmp_path / "gltf-transform-cli.js"
    cli_path.write_text("", encoding="utf-8")

    def fake_which(name):
        if name == "gltfpack":
            return "gltfpack"
        if name == "node":
            return "node"
        return None

    def fake_run(cmd, capture_output=True, text=True, timeout=300, check=False):
        calls.append(list(cmd))
        if "copy" in cmd:
            out_path = Path(cmd[cmd.index("copy") + 2])
            out_path.write_bytes(_minimal_glb({"asset": {"version": "2.0"}, "scene": 0}))
        else:
            out_path = Path(cmd[cmd.index("-o") + 1])
            out_path.write_bytes(_minimal_glb({
                "asset": {"version": "2.0"},
                "extras": {"optimized": True},
            }))
            report_path = Path(cmd[cmd.index("-r") + 1])
            report_path.write_text(json.dumps({"optimized": True}), encoding="utf-8")

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()

    monkeypatch.setattr(shutil, "which", fake_which)
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(api, "_gltf_transform_cli_path", lambda: cli_path)

    with app.app_context():
        owner = _ensure_user("game-draco-owner")
        source = _minimal_glb({
            "asset": {"version": "2.0"},
            "extensionsUsed": ["KHR_draco_mesh_compression"],
            "meshes": [{
                "primitives": [{
                    "attributes": {"POSITION": 0},
                    "extensions": {"KHR_draco_mesh_compression": {"bufferView": 0, "attributes": {"POSITION": 0}}},
                }],
            }],
            "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": 0}],
            "buffers": [{"byteLength": 0}],
        })
        model = Model3D(
            name="Game Draco Source",
            file_format="glb",
            file_size=len(source),
            user_id=owner.id,
            is_public=True,
            gridfs_file_id=app.config["FILE_STORE"].put(
                source,
                filename="game-draco-source.glb",
                content_type="model/gltf-binary",
                metadata={},
            ),
            asset_types=[],
        ).save()

        result = api._run_game_optimizer(model, owner.id, {})
        game = ModelVariant.get(model.id, "game")

    assert result["success"] is True
    assert calls[0][2] == "copy"
    assert calls[0][3].endswith("input.glb")
    assert calls[0][4].endswith("input-dedraco.glb")
    assert calls[1][calls[1].index("-i") + 1] == calls[0][4]
    assert result["source_prepare"]["draco_decompressed"] is True
    assert game.settings["source_prepare"]["draco_decompressed"] is True


def test_replacing_vrm_drops_stale_optimized_variant(monkeypatch):
    app = create_app()
    client = app.test_client()
    with app.app_context():
        owner = _ensure_user("vrm-replace-owner")

    login = client.post("/auth/login", data={
        "login_field": "vrm-replace-owner",
        "password": "pw123456",
    }, follow_redirects=True)
    assert login.status_code == 200

    source_glb = _minimal_glb({"asset": {"version": "2.0"}, "nodes": []})
    upload = client.post("/api/upload", data={
        "name": "VRM Replace Probe",
        "is_public": "false",
        "file": (io.BytesIO(source_glb), "vrm_replace_probe.glb"),
    }, content_type="multipart/form-data")
    assert upload.status_code == 201, upload.get_json()
    model_id = upload.get_json()["model"]["id"]

    with app.app_context():
        old_opt = app.config["FILE_STORE"].put(
            b"old optimized vrm",
            filename="old-opt.vrm",
            content_type="model/gltf-binary",
            metadata={"kind": "vrm_optimized"},
        )
        ModelVariant.upsert(model_id, "vrm_optimized", str(old_opt), file_format="vrm", size=17, status="ready")

    import app.conversion as conversion_module
    import app.api as api

    def fake_glb_to_vrm(_node, _converter_dir, _input_path, output_path, name=None, author=None, timeout=120):
        Path(output_path).write_bytes(b"glTF" + b"new vrm bytes")
        return output_path

    def fail_optimize(_model, texture_limit=2048):
        raise RuntimeError("optimizer unavailable")

    monkeypatch.setattr(conversion_module, "glb_to_vrm", fake_glb_to_vrm)
    monkeypatch.setattr(api, "_optimize_vrm_variant", fail_optimize)

    response = client.post(f"/api/model/{model_id}/to-vrm")
    assert response.status_code == 200, response.get_json()
    with app.app_context():
        assert ModelVariant.get(model_id, "vrm") is not None
        assert ModelVariant.get(model_id, "vrm_optimized") is None


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


def test_model_detail_escapes_metadata_in_viewer_script(monkeypatch):
    monkeypatch.setenv("AUTO_GAME_OPTIMIZE", "0")
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
    assert 'id="rig-profile-flip"' in html
    assert f'href="/model/{model_id}?capture=1&amp;regen=1"' in html
    assert "Regenerate Media" in html
    assert f'href="/api/download/{model_id}"' in html
    assert "Download Source" in html
    assert 'href="https://app.mesh2motion.org/create.html"' in html
    assert "Attach Animated GLB" in html
    assert "Returned animated GLB/GLTF" in html


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

    animal = client.post("/api/upload", headers=headers, data={
        "name": "Animated Deer",
        "is_public": "true",
        "asset_types": "animal, quadruped",
        "runtime_metadata": json.dumps({"animations": [{"name": "Graze", "duration": 2.0}]}),
        "file": (io.BytesIO(b"glTF" + b"\x07" * 64), "animated_deer.glb"),
    }, content_type="multipart/form-data")
    assert animal.status_code == 201, animal.get_json()
    animal_id = animal.get_json()["model"]["id"]

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
    assert native_item["capture_status"] == "queued"
    assert native_item["capture_attempt_count"] == 0
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
    animation_ids = {item["id"] for item in animation_queue.get_json()["models"]}
    assert animal_id not in animation_ids
    fbx_item = next(item for item in animation_queue.get_json()["models"] if item["id"] == fbx_id)
    assert fbx_item["capture_mode"] == "animation"
    assert fbx_item["capture_url"].endswith(f"/animations?capture_clip={fbx_id}:vrma")

    with app.app_context():
        clip = Model3D.get_by_id(vrma_id)
        thumb_id = app.config["FILE_STORE"].put(
            b"webp",
            filename="idle_clip.webp",
            content_type="image/webp",
            metadata={"model_id": vrma_id, "kind": "thumbnail"},
        )
        preview_id = app.config["FILE_STORE"].put(
            b"webm",
            filename="idle_clip.webm",
            content_type="video/webm",
            metadata={"model_id": vrma_id, "kind": "preview"},
        )
        clip.thumbnail_file_id = str(thumb_id)
        clip.preview_file_id = str(preview_id)
        clip.ai_status = "done"
        clip.ai_metadata = {"provider": "heuristic", "vision_fallback": True}
        clip.save()

    enrichment_queue = client.get(
        "/api/admin/media-capture/queue?kind=animations&include_not_ready=true&limit=20",
        headers=headers,
    )
    assert enrichment_queue.status_code == 200, enrichment_queue.get_json()
    vrma_item = next(item for item in enrichment_queue.get_json()["models"] if item["id"] == vrma_id)
    assert vrma_item["capture_mode"] == "animation"
    assert vrma_item["needs_thumbnail"] is False
    assert vrma_item["needs_preview"] is False
    assert vrma_item["needs_enrichment"] is True


def test_admin_media_capture_status_and_heartbeat():
    app = create_app()
    client = app.test_client()
    headers = {"Authorization": "Bearer test-token"}

    upload = client.post("/api/upload", headers=headers, data={
        "is_public": "true",
        "file": (io.BytesIO(b"glTF" + b"\x0a" * 64), "needs_media.glb"),
    }, content_type="multipart/form-data")
    assert upload.status_code == 201, upload.get_json()
    model_id = upload.get_json()["model"]["id"]

    status = client.get(
        "/api/admin/media-capture/status?limit=20&kind=all&include_not_ready=true",
        headers=headers,
    )
    assert status.status_code == 200, status.get_json()
    data = status.get_json()
    assert data["count"] >= 1
    assert data["ready_count"] >= 1
    assert data["worker"]["last_seen"] is None
    assert data["worker"]["active"] is False
    assert model_id in {item["id"] for item in data["models"]}

    heartbeat = client.post(
        "/api/admin/media-capture/heartbeat",
        headers={**headers, "Content-Type": "application/json"},
        json={"status": "captured", "kind": "models", "count": 1, "captured": 1},
    )
    assert heartbeat.status_code == 200, heartbeat.get_json()
    assert heartbeat.get_json()["worker"]["active"] is True

    status_after = client.get("/api/admin/media-capture/status?limit=20", headers=headers)
    assert status_after.status_code == 200, status_after.get_json()
    worker = status_after.get_json()["worker"]
    assert worker["active"] is True
    assert worker["last_status"] == "captured"
    assert worker["last_count"] == 1
    assert worker["last_captured"] == 1

    report = client.post(
        "/api/admin/media-capture/report",
        headers={**headers, "Content-Type": "application/json"},
        json={"model_id": model_id, "status": "failed", "kind": "models", "error": "viewer timed out"},
    )
    assert report.status_code == 200, report.get_json()
    reported = report.get_json()["media_capture"]
    assert reported["status"] == "failed"
    assert reported["attempt_count"] == 1
    assert reported["last_error"] == "viewer timed out"

    status_failed = client.get("/api/admin/media-capture/status?limit=20", headers=headers)
    failed_item = next(item for item in status_failed.get_json()["models"] if item["id"] == model_id)
    assert failed_item["capture_status"] == "failed"
    assert failed_item["capture_last_error"] == "viewer timed out"


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
        source.tags = ["avatar", "vrm"]
        source.asset_types = ["avatar", "vrm"]
        source.save()
        ModelVariant.upsert(
            model_id, "vrm", str(vrm_id),
            file_format="vrm", size=68, status="ready",
        )

    browse = client.get("/api/models/browse?per_page=20")
    assert browse.status_code == 200, browse.get_json()
    assert model_id in {item["id"] for item in browse.get_json()["models"]}

    avatar_browse = client.get("/api/models/browse?asset=vrm&per_page=20")
    assert avatar_browse.status_code == 200, avatar_browse.get_json()
    assert model_id in {item["id"] for item in avatar_browse.get_json()["models"]}

    public_models = client.get("/api/models")
    assert public_models.status_code == 200, public_models.get_json()
    assert model_id in {item["id"] for item in public_models.get_json()["models"]}

    media_queue = client.get("/api/admin/media-capture/queue?kind=models&limit=20", headers=headers)
    assert media_queue.status_code == 200, media_queue.get_json()
    assert model_id in {item["id"] for item in media_queue.get_json()["models"]}

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


def test_animations_list_has_no_duplicate_model_cards():
    """A row that matches more than one animation list (e.g. a native .vrma that
    also carries a vrma_file_id) must surface as ONE card, not two."""
    app = create_app()
    client = app.test_client()
    headers = {"Authorization": "Bearer test-token"}

    upload = client.post("/api/upload", headers=headers, data={
        "is_public": "true",
        "file": (io.BytesIO(b"VRMA dup clip" + b"\x0c" * 64), "dup_clip.vrma"),
    }, content_type="multipart/form-data")
    assert upload.status_code == 201, upload.get_json()
    model_id = upload.get_json()["model"]["id"]

    with app.app_context():
        # Force the overlap: native .vrma (List 1) that ALSO carries a
        # vrma_file_id (List 2). Before the dedup fix this rendered twice.
        clip = Model3D.get_by_id(model_id)
        derived = app.config["FILE_STORE"].put(
            b"vrma", filename="dup_derived.vrma",
            content_type="application/octet-stream",
            metadata={"derived_for": model_id, "kind": "vrma"},
        )
        clip.vrma_file_id = str(derived)
        clip.save()

    vrma = client.get("/api/vrma")
    assert vrma.status_code == 200, vrma.get_json()
    model_ids = [it["model_id"] for it in vrma.get_json()["animations"]]
    assert model_ids.count(model_id) == 1, model_ids

    page = client.get("/animations")
    assert page.status_code == 200
    # The card template emits clip.detail_url 3x per card (thumb + 2 links). One
    # card -> 3 occurrences; a duplicate card would double it to 6.
    assert page.get_data(as_text=True).count(f'href="/model/{model_id}"') == 3


def test_animation_autotag_builds_intent_registry_without_thumbnail():
    app = create_app()
    client = app.test_client()
    headers = {"Authorization": "Bearer test-token"}

    upload = client.post("/api/upload", headers=headers, data={
        "name": "Hip Hop Dancing",
        "is_public": "true",
        "file": (io.BytesIO(b"VRMA dance clip" + b"\x0d" * 64), "hip_hop_dancing.vrma"),
    }, content_type="multipart/form-data")
    assert upload.status_code == 201, upload.get_json()
    model_id = upload.get_json()["model"]["id"]

    enrich = client.post(
        f"/api/model/{model_id}/ai/autotag",
        headers=headers,
        json={"overwrite": True, "context": {"source": "animations"}},
    )
    assert enrich.status_code == 200, enrich.get_json()
    model = enrich.get_json()["model"]
    assert model["ai_status"] == "done"
    assert model["asset_category"] == "animation"
    assert "avatar-animation" in model["asset_types"]
    enrichment = enrich.get_json()["enrichment"]
    assert enrichment["animation"]["intent"] == "dance"
    assert enrichment["animation"]["intents"] == ["dance"]
    assert enrichment["animation"]["actorKind"] == "avatar"
    assert enrichment["animation"]["skeletonProfile"] == "vrm-humanoid"
    assert enrichment["animation"]["category"] == "dance"
    assert enrichment["animation"]["loop"] is True
    assert enrichment["animation"]["rootMotion"] == "in-place"
    assert enrichment["animation"]["quality"]["issues"] == []

    vrma = client.get("/api/vrma")
    assert vrma.status_code == 200, vrma.get_json()
    clip = next(item for item in vrma.get_json()["animations"] if item["model_id"] == model_id)
    assert clip["intent"] == "dance"
    assert clip["actorKind"] == "avatar"
    assert clip["skeletonProfile"] == "vrm-humanoid"
    assert clip["category"] == "dance"
    assert clip["durationSeconds"] is None
    assert clip["animation"]["energy"] == "high"
    compact = next(item for item in vrma.get_json()["clips"] if item["id"] == model_id)
    assert compact["intent"] == "dance"
    assert compact["intents"] == ["dance"]
    assert compact["actorKind"] == "avatar"
    assert compact["rootMotion"] == "in-place"
    assert compact["quality"]["issues"] == []
    assert "dance" in compact["tags"]


def test_animation_enrichment_uses_thumbnail_frame_over_filename(monkeypatch):
    from app import ai_enrichment

    app = create_app()
    client = app.test_client()
    headers = {"Authorization": "Bearer test-token"}

    upload = client.post("/api/upload", headers=headers, data={
        "name": "Bowing",
        "is_public": "true",
        "file": (io.BytesIO(b"VRMA bowing clip" + b"\x0f" * 64), "mixamo_bowing.vrma"),
    }, content_type="multipart/form-data")
    assert upload.status_code == 201, upload.get_json()
    model_id = upload.get_json()["model"]["id"]
    _attach_thumbnail(app, model_id)

    captured = {}

    def fake_post_json(url, body, headers, provider=None, transport=None):
        captured["body"] = body
        return {
            "id": "animation-response",
            "choices": [{
                "message": {
                    "content": json.dumps({
                        "title": "Elbow Strike",
                        "description": "Humanoid elbowing gesture animation.",
                        "summary": "Elbowing gesture.",
                        "tags": ["elbow", "strike", "gesture"],
                        "asset_styles": [],
                        "asset_types": ["gesture"],
                        "categories": ["animation", "attack"],
                        "quality_notes": [],
                        "animation": {
                            "intent": "attack",
                            "intents": ["attack", "elbow"],
                            "actorKind": "avatar",
                            "skeletonProfile": "mixamo-humanoid",
                            "category": "action",
                            "bodyType": "humanoid",
                            "tags": ["elbow", "strike", "gesture"],
                            "loop": False,
                            "duration": None,
                            "durationSeconds": None,
                            "transitionIn": 0.15,
                            "transitionOut": 0.2,
                            "energy": "high",
                            "locomotion": False,
                            "rootMotion": "in-place",
                            "speedMetersPerSecond": None,
                            "direction": "none",
                            "gait": "idle",
                            "transition": {"from": ["idle"], "to": ["idle"]},
                            "aliases": ["elbow", "elbow strike", "attack"],
                            "quality": {"score": 0.9, "issues": []},
                            "searchText": "elbow strike attack avatar mixamo humanoid",
                            "requiresMount": False,
                        },
                    })
                }
            }],
        }

    monkeypatch.setenv("AI_AUTOTAG_PROVIDER", "openai")
    monkeypatch.setenv("AI_AUTOTAG_API_KEY", "openai-key")
    monkeypatch.setattr(ai_enrichment, "_post_json", fake_post_json)

    enrich = client.post(
        f"/api/model/{model_id}/ai/autotag",
        headers=headers,
        json={"overwrite": True, "context": {"source": "animations"}},
    )
    assert enrich.status_code == 200, enrich.get_json()
    enrichment = enrich.get_json()["enrichment"]
    assert enrichment["animation"]["intent"] == "attack"
    assert enrichment["animation"]["intents"] == ["attack", "elbow"]
    assert enrichment["animation"]["actorKind"] == "avatar"
    assert enrichment["animation"]["skeletonProfile"] == "mixamo-humanoid"
    assert enrichment["animation"]["aliases"][:2] == ["elbow", "elbow strike"]
    assert enrichment["vision_frame"] is True

    content = captured["body"]["messages"][1]["content"]
    assert isinstance(content, list)
    assert content[1]["type"] == "image_url"
    assert "visible pose/action as stronger evidence than the filename" in content[0]["text"]
    schema = captured["body"]["response_format"]["json_schema"]["schema"]
    required = schema["properties"]["animation"]["required"]
    assert {"actorKind", "skeletonProfile", "intents", "rootMotion", "quality", "searchText"}.issubset(required)


def test_animated_animal_glb_gets_embedded_animation_metadata_not_vrma_clip():
    app = create_app()
    client = app.test_client()
    headers = {"Authorization": "Bearer test-token"}

    upload = client.post("/api/upload", headers=headers, data={
        "name": "Forest Deer",
        "description": "Animated deer with graze and run loops.",
        "is_public": "true",
        "asset_category": "fauna",
        "asset_types": "animal, quadruped",
        "tags": "deer, animal, quadruped",
        "runtime_metadata": json.dumps({
            "animations": [
                {"name": "Graze", "duration": 2.0},
                {"name": "Run", "duration": 1.2},
            ],
        }),
        "file": (io.BytesIO(b"glTF deer animated" + b"\x11" * 64), "forest_deer.glb"),
    }, content_type="multipart/form-data")
    assert upload.status_code == 201, upload.get_json()
    model_id = upload.get_json()["model"]["id"]
    _attach_thumbnail(app, model_id)

    enrich = client.post(
        f"/api/model/{model_id}/ai/autotag",
        headers=headers,
        json={"overwrite": True, "context": {"source": "embedded-animations"}},
    )
    assert enrich.status_code == 200, enrich.get_json()
    model = enrich.get_json()["model"]
    assert model["asset_category"] == "fauna"
    assert "avatar-animation" not in model["asset_types"]

    metadata = enrich.get_json()["enrichment"]
    animated_model = metadata["animatedModel"]
    assert animated_model["assetId"] == model_id
    assert animated_model["actorKind"] == "animal"
    assert animated_model["skeletonProfile"] == "quadruped"
    assert animated_model["vehicleMode"] == "ground"
    assert animated_model["groundContact"] == "feet"
    assert animated_model["movement"]["idleIntent"] == "graze"
    assert animated_model["movement"]["runIntent"] == "run"
    clips = metadata["animationClips"]
    assert [clip["intent"] for clip in clips] == ["graze", "run"]
    assert clips[0]["actorKind"] == "animal"
    assert clips[0]["skeletonProfile"] == "quadruped"
    assert clips[1]["speedMetersPerSecond"] == 4.5

    vrma = client.get("/api/vrma")
    assert vrma.status_code == 200, vrma.get_json()
    assert model_id not in {item["model_id"] for item in vrma.get_json()["animations"]}


def test_render_processing_status_surfaces_and_blocks_tellus():
    """While a server-side thumbnail render is in flight, the model must report
    media_capture.status='processing' and ready_for_tellus=False so Tellus keeps
    polling and doesn't render it in-world yet. Once a thumbnail exists the
    status clears and ready_for_tellus depends only on thumbnail+game readiness."""
    app = create_app()
    client = app.test_client()
    headers = {"Authorization": "Bearer test-token"}

    upload = client.post("/api/upload", headers=headers, data={
        "is_public": "true",
        "file": (io.BytesIO(b"glTF" + b"\x0e" * 64), "render_status.glb"),
    }, content_type="multipart/form-data")
    assert upload.status_code == 201, upload.get_json()
    model_id = upload.get_json()["model"]["id"]

    import app.api as api
    with app.app_context():
        model = Model3D.get_by_id(model_id)
        api._set_media_capture_state(model, status="processing", kind="server_render")
        model.save()

    detail = client.get(f"/api/model/{model_id}")
    assert detail.status_code == 200, detail.get_json()
    body = detail.get_json()["model"]
    # The poller reads media_capture.status and processing_state.
    assert body["media_capture"]["status"] == "processing"
    assert body["processing_state"]["media_capture_status"] == "processing"
    # No thumbnail yet -> Tellus must not render it.
    assert body["processing_state"]["ready_for_tellus"] is False
    assert "thumbnail" in body["processing_state"]["blocked_by"]

    # Simulate the render completing: thumbnail stored flips status to captured.
    with app.app_context():
        model = Model3D.get_by_id(model_id)
        api._store_thumbnail_png(model, _one_px_png(), kind="thumbnail", source="server_render")

    detail2 = client.get(f"/api/model/{model_id}").get_json()["model"]
    assert detail2["media_capture"]["status"] == "captured"
    assert "thumbnail" not in detail2["processing_state"]["blocked_by"]


def _one_px_png():
    # Minimal valid 1x1 PNG so _store_thumbnail_png's WebP transcode has input.
    import base64
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
    )


def test_superseded_id_alias_resolves_to_replacement():
    """A reference to a superseded model id (e.g. a Tellus world that stored an
    id later replaced by a generationId re-upload) must still resolve to the
    replacement model, not 404."""
    app = create_app()
    client = app.test_client()
    headers = {"Authorization": "Bearer test-token"}

    up = client.post("/api/upload", headers=headers, data={
        "is_public": "true",
        "file": (io.BytesIO(b"glTF" + b"\x11" * 64), "alias_target.glb"),
    }, content_type="multipart/form-data")
    assert up.status_code == 201, up.get_json()
    new_id = up.get_json()["model"]["id"]

    old_id = "00000000-0000-0000-0000-deadbeef0001"
    with app.app_context():
        Model3D.record_alias(old_id, new_id, reason="test")
        assert Model3D.resolve_id(old_id) == new_id
        # get_by_id stays strict (no alias); get_by_id_or_alias follows it.
        assert Model3D.get_by_id(old_id) is None
        assert Model3D.get_by_id_or_alias(old_id).id == new_id

    # The consumer-facing endpoint resolves the old id to the live model.
    via_alias = client.get(f"/api/model/{old_id}")
    assert via_alias.status_code == 200, via_alias.get_json()
    assert via_alias.get_json()["model"]["id"] == new_id


def test_browse_page_renders_with_asset_filters():
    app = create_app()
    client = app.test_client()

    for path in ("/browse", "/browse?asset=vrm", "/browse?asset=animated"):
        response = client.get(path)
        assert response.status_code == 200
        html = response.get_data(as_text=True)
        assert "Browse 3D Models" in html
        assert 'name="asset"' in html


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


def test_home_and_browse_prefer_video_for_animated_model_previews(monkeypatch):
    monkeypatch.setenv("AUTO_GAME_OPTIMIZE", "0")
    app = create_app()
    client = app.test_client()
    headers = {"Authorization": "Bearer test-token"}

    animated_upload = client.post("/api/upload", headers=headers, data={
        "name": "Previewing Animated Creature",
        "is_public": "true",
        "asset_types": "rigged, animated",
        "file": (io.BytesIO(_minimal_glb({
            "asset": {"version": "2.0"},
            "nodes": [{"name": "Armature"}],
            "skins": [{"joints": [0]}],
            "animations": [{"name": "Hop", "channels": [], "samplers": []}],
        })), "previewing_animated_creature.glb"),
    }, content_type="multipart/form-data")
    assert animated_upload.status_code == 201, animated_upload.get_json()
    model_id = animated_upload.get_json()["model"]["id"]

    with app.app_context():
        model = Model3D.get_by_id(model_id)
        preview_id = app.config["FILE_STORE"].put(
            b"webm preview",
            filename="preview.webm",
            content_type="video/webm",
            metadata={"model_id": model_id, "kind": "preview"},
        )
        thumb_id = app.config["FILE_STORE"].put(
            b"webp thumbnail",
            filename="thumbnail.webp",
            content_type="image/webp",
            metadata={"model_id": model_id, "kind": "thumbnail"},
        )
        model.preview_file_id = str(preview_id)
        model.thumbnail_file_id = str(thumb_id)
        model.save()

    browse_api = client.get("/api/models/browse?asset=animated&per_page=20")
    assert browse_api.status_code == 200, browse_api.get_json()
    card = next(item for item in browse_api.get_json()["models"] if item["id"] == model_id)
    assert card["is_animated"] is True
    assert card["preview_url"].endswith(f"/api/model/{model_id}/preview")

    browse_page = client.get("/browse?asset=animated")
    assert browse_page.status_code == 200
    browse_html = browse_page.get_data(as_text=True)
    assert f'data-preview-src="/api/model/{model_id}/preview"' in browse_html
    assert f'data-img-src="/api/model/{model_id}/thumbnail"' not in browse_html

    home = client.get("/")
    assert home.status_code == 200
    home_html = home.get_data(as_text=True)
    assert f'data-preview-src="/api/model/{model_id}/preview"' in home_html
    assert f'data-model-id="{model_id}"\n                   data-viewable="0"' in home_html


def test_browse_uses_live_preview_fallback_for_uncaptured_glb(monkeypatch):
    monkeypatch.setenv("AUTO_GAME_OPTIMIZE", "0")
    app = create_app()
    client = app.test_client()
    headers = {"Authorization": "Bearer test-token"}

    upload = client.post("/api/upload", headers=headers, data={
        "name": "Uncaptured Browse Preview",
        "is_public": "true",
        "file": (io.BytesIO(_minimal_glb({"asset": {"version": "2.0"}})), "uncaptured.glb"),
    }, content_type="multipart/form-data")
    assert upload.status_code == 201, upload.get_json()
    model_id = upload.get_json()["model"]["id"]

    browse_api = client.get("/api/models/browse?per_page=20")
    assert browse_api.status_code == 200, browse_api.get_json()
    card = next(item for item in browse_api.get_json()["models"] if item["id"] == model_id)
    assert card["has_thumbnail"] is False
    assert card["viewable"] is True
    assert card["view_url"].endswith(f"/api/view/{model_id}?viewer=2")

    browse_page = client.get("/browse")
    assert browse_page.status_code == 200
    html = browse_page.get_data(as_text=True)
    assert f'data-model-id="{model_id}"' in html
    assert 'data-viewable="1"' in html
    assert f'data-view-src="/api/view/{model_id}?viewer=2"' in html
    assert "observeLiveIn(grid)" in html


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


def test_animation_clip_detail_uses_preview_avatar():
    app = create_app()
    client = app.test_client()
    headers = {"Authorization": "Bearer test-token"}

    avatar_upload = client.post("/api/upload", headers=headers, data={
        "name": "Detail Preview Avatar",
        "is_public": "true",
        "file": (io.BytesIO(b"glTF avatar vrm" + b"\x0d" * 64), "detail_preview_avatar.vrm"),
    }, content_type="multipart/form-data")
    assert avatar_upload.status_code == 201, avatar_upload.get_json()
    avatar_id = avatar_upload.get_json()["model"]["id"]

    clip_upload = client.post("/api/upload", headers=headers, data={
        "name": "Acknowledging",
        "is_public": "true",
        "asset_category": "animation",
        "asset_types": "animation, avatar-animation",
        "runtime_metadata": json.dumps({
            "animations": [{"name": "Acknowledging"}],
            "upload": {"source": "vrma-library-import"},
        }),
        "file": (io.BytesIO(b"Kaydara FBX Binary" + b"\x0e" * 64), "Animations_acknowledging.fbx"),
    }, content_type="multipart/form-data")
    assert clip_upload.status_code == 201, clip_upload.get_json()
    clip_id = clip_upload.get_json()["model"]["id"]

    with app.app_context():
        model = Model3D.get_by_id(clip_id)
        vrma_id = app.config["FILE_STORE"].put(
            b"vrma",
            filename="acknowledging.vrma",
            content_type="application/octet-stream",
            metadata={"derived_for": clip_id, "kind": "vrma"},
        )
        model.vrma_file_id = str(vrma_id)
        model.conversion_status = "done"
        model.save()

    page = client.get(f"/model/{clip_id}")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert f'id="animation-detail-preview-{clip_id}"' in html
    assert f'data-avatar-src="/api/view/{avatar_id}"' in html
    assert f'data-vrma-src="/api/export/{clip_id}?format=vrma"' in html
    assert f"loadDetailModel('{clip_id}')" not in html


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
    with app.app_context():
        vrm_file_id = app.config["FILE_STORE"].put(
            b"bad legacy avatar variant",
            filename="pharoah_accidental.vrm",
            content_type="model/gltf-binary",
            metadata={"derived_for": model_id, "kind": "vrm"},
        )
        ModelVariant.upsert(model_id, "vrm", str(vrm_file_id), file_format="vrm", size=26, status="ready")

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

    avatars_api = client.get("/api/vrm")
    assert avatars_api.status_code == 200, avatars_api.get_json()
    assert model_id not in {item["model_id"] for item in avatars_api.get_json()["avatars"]}

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
    monkeypatch.setenv("AUTO_GAME_OPTIMIZE", "0")
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


def test_tellus_asset_lod_routes_serve_variants_under_original_asset_id():
    app = create_app()
    client = app.test_client()
    with app.app_context():
        owner = _ensure_user("tellus-lod-owner")
        source_id = app.config["FILE_STORE"].put(
            _minimal_glb({"asset": {"version": "2.0"}}),
            filename="lod_source.glb",
            content_type="model/gltf-binary",
            metadata={},
        )
        model = Model3D(
            name="LOD Source",
            file_format="glb",
            file_size=64,
            user_id=owner.id,
            is_public=True,
            gridfs_file_id=str(source_id),
        ).save()
        game_bytes = _minimal_glb({"asset": {"version": "2.0"}, "scene": 0})
        lod1_bytes = _minimal_glb({"asset": {"version": "2.0"}, "scene": 1})
        impostor_bytes = b"RIFFxxxxWEBP"
        game_id = app.config["FILE_STORE"].put(
            game_bytes,
            filename="lod_source-game.glb",
            content_type="model/gltf-binary",
            metadata={"kind": "game", "source_model_id": model.id},
        )
        lod1_id = app.config["FILE_STORE"].put(
            lod1_bytes,
            filename="lod_source-lod1.glb",
            content_type="model/gltf-binary",
            metadata={"kind": "lod", "level": 1, "source_model_id": model.id},
        )
        impostor_id = app.config["FILE_STORE"].put(
            impostor_bytes,
            filename="lod_source-impostor.webp",
            content_type="image/webp",
            metadata={"kind": "impostor", "source_model_id": model.id},
        )
        ModelVariant.upsert(model.id, "game", str(game_id), file_format="glb", size=len(game_bytes))
        ModelVariant.upsert(
            model.id,
            "lod",
            str(lod1_id),
            level=1,
            file_format="glb",
            size=len(lod1_bytes),
            settings={"mesh_stats": {"vertices": 1234, "triangles": 2345}},
        )
        ModelVariant.upsert(
            model.id,
            "impostor",
            str(impostor_id),
            file_format="webp",
            size=len(impostor_bytes),
            settings={
                "type": "octahedral_atlas",
                "width": 2048,
                "height": 2048,
                "atlas_width": 2048,
                "atlas_height": 2048,
                "grid_size_x": 31,
                "grid_size_y": 31,
                "cell_size": 66,
                "view_count": 961,
                "octahedron_type": "hemi",
                "source": "octahedral_server_render",
                "role": "far/octahedral",
            },
        )

    detail = client.get(f"/api/model/{model.id}")
    assert detail.status_code == 200, detail.get_json()
    detail_model = detail.get_json()["model"]
    assert detail_model["asset_lod_urls"]["game_optimized"].endswith(f"/api/assets/model/{model.id}/game-optimized")
    assert detail_model["asset_lod_urls"]["lod0"].endswith(f"/api/assets/model/{model.id}/lod/0")
    assert detail_model["asset_lod_urls"]["lod1"].endswith(f"/api/assets/model/{model.id}/lod/1")
    assert detail_model["asset_lod_urls"]["lod2"].endswith(f"/api/assets/model/{model.id}/lod/2")
    assert detail_model["asset_lod_urls"]["lod3"].endswith(f"/api/assets/model/{model.id}/lod/3")
    assert detail_model["asset_lod_urls"]["impostor"].endswith(f"/api/assets/model/{model.id}/impostor")
    assert detail_model["has_lod_variants"] is True
    assert detail_model["lod_ready"] is False
    assert detail_model["lod_status"] == "partial"
    assert detail_model["lod_available_levels"] == [1]
    assert detail_model["lod_missing_levels"] == [0, 2, 3]
    assert detail_model["lod_preview_fallback_url"].endswith(f"/api/view/{model.id}?viewer=2")
    assert detail_model["lod_variants"][0]["vertices"] == 1234
    assert detail_model["lod_variants"][0]["triangles"] == 2345
    assert detail_model["lod_variants"][0]["recommended_use"] == "large_fill"
    assert detail_model["lod_summary"]["cheapest_vertices"] == 1234
    assert detail_model["lod_summary"]["recommended_use"] == "large_fill"
    assert detail_model["has_impostor"] is True
    assert detail_model["lod_variants"][0]["level"] == 1
    assert detail_model["lod_variants"][0]["url"].endswith(f"/api/assets/model/{model.id}/lod/1")
    assert detail_model["lod_variants"][0]["download_url"].endswith(f"/api/assets/model/{model.id}/lod/1?download=1")
    assert detail_model["impostor"]["file_format"] == "webp"
    assert detail_model["impostor"]["type"] == "octahedral_atlas"
    assert detail_model["impostor"]["width"] == 2048
    assert detail_model["impostor"]["height"] == 2048
    assert detail_model["impostor"]["grid_size_x"] == 31
    assert detail_model["impostor"]["grid_size_y"] == 31
    assert detail_model["impostor"]["view_count"] == 961
    assert detail_model["impostor"]["octahedron_type"] == "hemi"
    assert detail_model["impostor"]["source"] == "octahedral_server_render"
    assert detail_model["impostor"]["role"] == "far/octahedral"
    assert detail_model["impostor"]["url"].endswith(f"/api/assets/model/{model.id}/impostor")

    page = client.get(f"/model/{model.id}")
    assert page.status_code == 200
    page_html = page.get_data(as_text=True)
    assert "VARIANT_CACHE_KEYS" in page_html
    assert "variantUrlWithCacheKey" in page_html
    assert "lod1:" in page_html

    game = client.get(f"/api/assets/model/{model.id}/game-optimized")
    assert game.status_code == 200
    assert game.headers["Content-Type"] == "model/gltf-binary"
    assert game.headers["ETag"].startswith('"game-')
    assert game.data == game_bytes

    lod0 = client.get(f"/api/assets/model/{model.id}/lod/0")
    assert lod0.status_code == 200
    assert lod0.data == game_bytes

    lod1 = client.get(f"/api/assets/model/{model.id}/lod/1")
    assert lod1.status_code == 200
    assert lod1.headers["ETag"].startswith('"lod-1-')
    assert lod1.headers["Cache-Control"] == "public, max-age=0, must-revalidate"
    assert lod1.data == lod1_bytes

    lod2 = client.get(f"/api/assets/model/{model.id}/lod/2")
    assert lod2.status_code == 404

    impostor = client.get(f"/api/assets/model/{model.id}/impostor")
    assert impostor.status_code == 200
    assert impostor.headers["Content-Type"] == "image/webp"
    assert impostor.data == impostor_bytes


def test_tellus_asset_game_route_falls_back_to_lod0_variant():
    app = create_app()
    client = app.test_client()
    with app.app_context():
        owner = _ensure_user("tellus-lod0-game-owner")
        source_id = app.config["FILE_STORE"].put(
            _minimal_glb({"asset": {"version": "2.0"}}),
            filename="lod0_only_source.glb",
            content_type="model/gltf-binary",
            metadata={},
        )
        model = Model3D(
            name="LOD0 Only Source",
            file_format="glb",
            file_size=64,
            user_id=owner.id,
            is_public=True,
            gridfs_file_id=str(source_id),
        ).save()
        lod0_bytes = _minimal_glb({"asset": {"version": "2.0"}, "scene": 0})
        lod0_id = app.config["FILE_STORE"].put(
            lod0_bytes,
            filename="lod0_only_source-lod0.glb",
            content_type="model/gltf-binary",
            metadata={"kind": "lod", "level": 0, "source_model_id": model.id},
        )
        ModelVariant.upsert(model.id, "lod", str(lod0_id), level=0, file_format="glb", size=len(lod0_bytes))

    tellus_game = client.get(f"/api/assets/model/{model.id}/game-optimized")
    assert tellus_game.status_code == 200
    assert tellus_game.headers["Content-Type"] == "model/gltf-binary"
    assert tellus_game.headers["ETag"].startswith('"lod-0-')
    assert tellus_game.data == lod0_bytes

    strict_game = client.get(f"/api/model/{model.id}/game-optimized")
    assert strict_game.status_code == 404


def test_model_detail_renders_lod_variant_tabs():
    app = create_app()
    client = app.test_client()
    with app.app_context():
        owner = _ensure_user("detail-lod-owner")
        source_id = app.config["FILE_STORE"].put(
            _minimal_glb({"asset": {"version": "2.0"}}),
            filename="detail_lod_source.glb",
            content_type="model/gltf-binary",
            metadata={},
        )
        model = Model3D(
            name="Detail LOD Source",
            file_format="glb",
            file_size=64,
            user_id=owner.id,
            is_public=True,
            gridfs_file_id=str(source_id),
        ).save()
        lod1_bytes = _minimal_glb({"asset": {"version": "2.0"}, "scene": 1})
        lod1_id = app.config["FILE_STORE"].put(
            lod1_bytes,
            filename="detail_lod_source-lod1.glb",
            content_type="model/gltf-binary",
            metadata={"kind": "lod", "level": 1, "source_model_id": model.id},
        )
        ModelVariant.upsert(
            model.id,
            "lod",
            str(lod1_id),
            level=1,
            file_format="glb",
            size=len(lod1_bytes),
            settings={"role": "mid", "mesh_stats": {"vertices": 1234, "triangles": 2345}},
        )

    detail = client.get(f"/model/{model.id}")
    assert detail.status_code == 200
    html = detail.get_data(as_text=True)
    assert 'id="variant-btn-lod1"' in html
    assert "switchVariant('lod1')" in html
    assert f"/api/assets/model/{model.id}/lod/1" in html
    assert "LOD1 GLB" in html
    assert "LOD partial: missing 0, 2" in html
    assert "1,234v" in html

    browse = client.get("/api/models/browse?per_page=20")
    assert browse.status_code == 200, browse.get_json()
    card = next(item for item in browse.get_json()["models"] if item["id"] == model.id)
    assert card["lod_summary"]["cheapest_vertices"] == 1234
    assert card["lod_summary"]["recommended_use"] == "large_fill"


def test_game_optimization_job_generates_lods_for_one_model(monkeypatch):
    import app.api as api

    app = create_app()
    client = app.test_client()
    generated = {}

    def fake_game_optimizer(model, owner_id, settings):
        generated["game_model_id"] = model.id
        generated["game_owner_id"] = owner_id
        return {
            "success": True,
            "source_model_id": model.id,
            "original_size": 1000,
            "optimized_size": 500,
            "savings_ratio": 0.5,
        }

    def fake_lod_optimizer(model, owner_id=None, levels=None):
        generated["lod_model_id"] = model.id
        generated["lod_owner_id"] = owner_id
        generated["lod3_color"] = levels[3]["flat_material_color"]
        generated["lod3_accent_color"] = levels[3]["flat_material_accent_color"]
        lod0_bytes = _minimal_glb({"asset": {"version": "2.0"}, "scene": 0})
        lod0_id = app.config["FILE_STORE"].put(
            lod0_bytes,
            filename="single-lod-source-lod0.glb",
            content_type="model/gltf-binary",
            metadata={"kind": "lod", "level": 0, "source_model_id": model.id},
        )
        ModelVariant.upsert(
            model.id,
            "lod",
            str(lod0_id),
            level=0,
            file_format="glb",
            size=len(lod0_bytes),
            settings={"mesh_stats": {"vertices": 900, "triangles": 1500}},
        )
        return {"success": True, "source_model_id": model.id}

    def fake_impostor_generator(model, owner_id=None):
        generated["impostor_model_id"] = model.id
        generated["impostor_owner_id"] = owner_id
        return {
            "success": True,
            "source_model_id": model.id,
            "source": "thumbnail",
            "size": 123,
            "width": 512,
            "height": 512,
        }

    monkeypatch.setattr(api, "_run_game_optimizer", fake_game_optimizer)
    monkeypatch.setattr(api, "_run_lod_optimizer", fake_lod_optimizer)
    monkeypatch.setattr(api, "_run_impostor_generator", fake_impostor_generator)
    monkeypatch.setattr(
        api,
        "_start_game_optimization_thread",
        lambda app_obj, job_id: generated.update({"job_id": job_id}),
    )

    with app.app_context():
        owner = _ensure_user("single-lod-owner")
        source_id = app.config["FILE_STORE"].put(
            _minimal_glb({"asset": {"version": "2.0"}}),
            filename="single_lod_source.glb",
            content_type="model/gltf-binary",
            metadata={},
        )
        model = Model3D(
            name="Single LOD Source",
            file_format="glb",
            file_size=64,
            user_id=owner.id,
            is_public=False,
            gridfs_file_id=str(source_id),
        ).save()

    client.post("/auth/login", data={"login_field": "single-lod-owner", "password": "pw123456"})
    detail = client.get(f"/model/{model.id}")
    assert detail.status_code == 200
    html = detail.get_data(as_text=True)
    assert "Create Game + LODs" in html
    assert "Rebuild LODs" in html
    assert 'id="lod3-flat-color"' in html
    assert 'id="lod3-flat-accent-color"' in html
    assert "Generate LODs" not in html

    response = client.post(
        f"/api/model/{model.id}/optimize-game",
        json={"lod3_flat_color": "#663300", "lod3_flat_accent_color": "#d96a28"},
    )
    assert response.status_code == 202, response.get_json()
    body = response.get_json()
    assert body["success"] is True
    job_id = body["job"]["id"]
    assert generated["job_id"] == job_id
    api._process_game_optimization_job(app, job_id)
    status = client.get(body["status_url"])
    assert status.status_code == 200, status.get_json()
    job = status.get_json()["job"]
    assert job["status"] == "done"
    assert job["lod_available_levels"] == [0]
    assert job["lod_variants"][0]["vertices"] == 900
    assert job["lod_summary"]["recommended_use"] == "large_fill"
    assert job["impostor_result"]["success"] is True
    assert job["impostor_result"]["width"] == 512
    assert generated == {
        "game_model_id": model.id,
        "game_owner_id": owner.id,
        "lod_model_id": model.id,
        "lod_owner_id": owner.id,
        "lod3_color": [0x66 / 255, 0x33 / 255, 0, 1.0],
        "lod3_accent_color": [0xd9 / 255, 0x6a / 255, 0x28 / 255, 1.0],
        "impostor_model_id": model.id,
        "impostor_owner_id": owner.id,
        "job_id": job_id,
    }


def test_owner_can_rebuild_single_model_lods(monkeypatch):
    import app.api as api

    app = create_app()
    client = app.test_client()
    generated = {}

    def fake_lod_optimizer(model, owner_id=None, levels=None):
        generated["model_id"] = model.id
        generated["owner_id"] = owner_id
        generated["lod3_color"] = levels[3]["flat_material_color"]
        generated["lod3_accent_color"] = levels[3]["flat_material_accent_color"]
        lod2_id = app.config["FILE_STORE"].put(
            b"single rebuilt lod2",
            filename="single-lod-source-lod2.glb",
            content_type="model/gltf-binary",
            metadata={"kind": "lod", "level": 2, "source_model_id": model.id},
        )
        ModelVariant.upsert(
            model.id,
            "lod",
            str(lod2_id),
            level=2,
            file_format="glb",
            size=1234,
            settings={
                "defaults_version": api.LOD_OPTIMIZE_DEFAULTS_VERSION,
                "mesh_stats": {"vertices": 3210, "triangles": 5000},
            },
        )
        return {"success": True, "source_model_id": model.id}

    monkeypatch.setattr(api, "_run_lod_optimizer", fake_lod_optimizer)

    with app.app_context():
        owner = _ensure_user("single-lod-rebuild-owner")
        source_id = app.config["FILE_STORE"].put(
            _minimal_glb({"asset": {"version": "2.0"}}),
            filename="single_lod_rebuild_source.glb",
            content_type="model/gltf-binary",
            metadata={},
        )
        model = Model3D(
            name="Single LOD Rebuild Source",
            file_format="glb",
            file_size=64,
            user_id=owner.id,
            is_public=False,
            gridfs_file_id=str(source_id),
        ).save()

    client.post("/auth/login", data={"login_field": "single-lod-rebuild-owner", "password": "pw123456"})
    response = client.post(
        f"/api/model/{model.id}/lod/rebuild",
        json={"lod3_flat_color": "#663300", "lod3_flat_accent_color": "#d96a28"},
    )
    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    assert body["success"] is True
    assert body["defaults_version"] == api.LOD_OPTIMIZE_DEFAULTS_VERSION
    assert body["lod_variants"][0]["level"] == 2
    assert body["lod_variants"][0]["vertices"] == 3210
    assert generated == {
        "model_id": model.id,
        "owner_id": owner.id,
        "lod3_color": [0x66 / 255, 0x33 / 255, 0, 1.0],
        "lod3_accent_color": [0xd9 / 255, 0x6a / 255, 0x28 / 255, 1.0],
    }


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
    assert "/optimization/defaults" in spec["paths"]
    assert "/admin/lod-backfill" in spec["paths"]
    assert "/admin/lod-backfill/status" in spec["paths"]
    lod_backfill_params = {
        param["name"] for param in spec["paths"]["/admin/lod-backfill"]["post"].get("parameters", [])
    }
    assert "force" in lod_backfill_params
    reconcile_params = {
        param["name"] for param in spec["paths"]["/admin/pipeline/reconcile"]["post"].get("parameters", [])
    }
    assert "impostor_limit" in reconcile_params
    assert "/model/{model_id}/ai/autotag" in spec["paths"]
    assert "/model/{model_id}/approval" in spec["paths"]
    assert "/bundles" in spec["paths"]
    optimize_doc = spec["paths"]["/model/{model_id}/optimize-game"]["post"]
    assert "LOD" in optimize_doc["summary"]
    assert "impostor" in optimize_doc["summary"].lower()
    assert "LOD0" in optimize_doc["description"]
    assert "impostor" in optimize_doc["description"].lower()
    assert optimize_doc["responses"]["202"]["content"]["application/json"]["schema"]["$ref"] == "#/components/schemas/OptimizationJobResponse"
    optimize_props = optimize_doc["requestBody"]["content"]["application/json"]["schema"]["properties"]
    assert optimize_props["preset"]["default"] == "balanced"
    assert optimize_props["simplify_ratio"]["default"] == 0.85
    assert optimize_props["lod3_flat_color"]["default"] == "#4d6b33"
    assert optimize_props["lod3_flat_accent_color"]["default"] == "#d96a28"
    lod_rebuild_doc = spec["paths"]["/model/{model_id}/lod/rebuild"]["post"]
    assert "LOD" in lod_rebuild_doc["summary"]
    lod_rebuild_request = lod_rebuild_doc["requestBody"]["content"]["application/json"]["schema"]["properties"]
    assert lod_rebuild_request["lod3_flat_color"]["default"] == "#4d6b33"
    assert lod_rebuild_request["lod3_flat_accent_color"]["default"] == "#d96a28"
    lod_rebuild_props = lod_rebuild_doc["responses"]["200"]["content"]["application/json"]["schema"]["properties"]
    assert "defaults_version" in lod_rebuild_props
    assert "lod_variants" in lod_rebuild_props
    status_doc = spec["paths"]["/model/{model_id}/optimize-game/{job_id}"]["get"]
    status_job = status_doc["responses"]["200"]["content"]["application/json"]["schema"]["properties"]["job"]
    assert status_job["$ref"] == "#/components/schemas/OptimizationJob"
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
    assert "view_url" in model_props
    assert "lod_preview_fallback_url" in model_props
    assert "asset_lod_urls" in model_props
    assert "lod_variants" in model_props
    assert "has_lod_variants" in model_props
    assert "lod_ready" in model_props
    assert "lod_status" in model_props
    assert "lod_available_levels" in model_props
    assert "lod_missing_levels" in model_props
    assert "lod_summary" in model_props
    assert "has_impostor" in model_props
    assert "impostor" in model_props
    assert "MeshStats" in spec["components"]["schemas"]
    assert "RuntimeMetadata" in spec["components"]["schemas"]
    assert "RuntimeCost" in spec["components"]["schemas"]
    assert "AssetLodUrls" in spec["components"]["schemas"]
    assert "LodVariant" in spec["components"]["schemas"]
    assert "LodSummary" in spec["components"]["schemas"]
    assert "OptimizationJob" in spec["components"]["schemas"]
    assert "OptimizationJobResponse" in spec["components"]["schemas"]
    assert "LodBackfillStatus" in spec["components"]["schemas"]
    assert "ImpostorVariant" in spec["components"]["schemas"]
    job_props = spec["components"]["schemas"]["OptimizationJob"]["properties"]
    assert "lod_result" in job_props
    assert "lod_variants" in job_props
    assert "lod_ready" in job_props
    assert "lod_summary" in job_props
    assert "impostor_result" in job_props
    assert "has_impostor" in job_props
    assert "impostor" in job_props
    lod_props = spec["components"]["schemas"]["LodVariant"]["properties"]
    assert "runtime_cost" in lod_props
    assert "mesh_stats" in lod_props
    assert "vertices" in lod_props
    assert "triangles" in lod_props
    assert "recommended_use" in lod_props
    impostor_props = spec["components"]["schemas"]["ImpostorVariant"]["properties"]
    assert "type" in impostor_props
    assert "width" in impostor_props
    assert "height" in impostor_props
    assert "grid_size_x" in impostor_props
    assert "grid_size_y" in impostor_props
    assert "view_count" in impostor_props
    assert "octahedron_type" in impostor_props
    assert "source" in impostor_props
    assert "role" in impostor_props
    assert "/assets/model/{model_id}/game-optimized" in spec["paths"]
    assert "/assets/model/{model_id}/lod/{level}" in spec["paths"]
    assert "/assets/model/{model_id}/impostor" in spec["paths"]
    assert "model/gltf-binary" in spec["paths"]["/assets/model/{model_id}/lod/{level}"]["get"]["responses"]["200"]["content"]
    assert "image/webp" in spec["paths"]["/assets/model/{model_id}/impostor"]["get"]["responses"]["200"]["content"]


def test_game_optimization_defaults_are_public_tellus_contract():
    app = create_app()
    response = app.test_client().get("/api/optimization/defaults")
    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    assert body["success"] is True
    assert body["default_preset"] == "balanced"
    assert body["defaults"]["texture_limit"] == 1024
    assert body["defaults"]["simplify_ratio"] == 0.85
    assert body["presets"]["quality"]["texture_limit"] == 2048
    assert body["supported"]["texture_compression"].startswith("KTX2/Basis")


def test_ready_for_tellus_filter_requires_thumbnail_and_game_variant(monkeypatch):
    monkeypatch.setenv("AUTO_GAME_OPTIMIZE", "0")
    app = create_app()
    client = app.test_client()
    headers = {"Authorization": "Bearer test-token"}
    glb = _minimal_glb({"asset": {"version": "2.0"}, "nodes": [], "meshes": []})

    upload = client.post("/api/upload", headers=headers, data={
        "name": "Queued Tellus Asset",
        "is_public": "true",
        "file": (io.BytesIO(glb), "queued_tellus_asset.glb"),
    }, content_type="multipart/form-data")
    assert upload.status_code == 201, upload.get_json()
    model_id = upload.get_json()["model"]["id"]
    uploaded_model = upload.get_json()["model"]
    assert uploaded_model["processing_state"]["ready_for_tellus"] is False
    assert uploaded_model["world_ready"] is False
    assert uploaded_model["storefront_ready"] is False
    assert "thumbnail" in uploaded_model["processing_state"]["blocked_by"]
    assert uploaded_model["media_capture"]["status"] == "queued"

    ready = client.get("/api/models?ready_for_tellus=true&per_page=20")
    assert ready.status_code == 200, ready.get_json()
    assert model_id not in {item["id"] for item in ready.get_json()["models"]}

    with app.app_context():
        _attach_thumbnail(app, model_id)
        file_id = app.config["FILE_STORE"].put(
            glb,
            filename="queued_tellus_asset-game.glb",
            content_type="model/gltf-binary",
            metadata={"kind": "game", "source_model_id": model_id},
        )
        ModelVariant.upsert(model_id, "game", str(file_id), file_format="glb", size=len(glb), status="ready")

    ready = client.get("/api/models?ready_for_tellus=true&per_page=20")
    assert ready.status_code == 200, ready.get_json()
    models = ready.get_json()["models"]
    item = next(model for model in models if model["id"] == model_id)
    assert item["processing_state"]["ready_for_tellus"] is True
    assert item["ready_for_tellus"] is True
    assert item["world_ready"] is True


def test_gltf_runtime_cost_metadata_tracks_textures_meshopt_and_vram():
    import app.api as api

    glb = _minimal_glb({
        "asset": {"version": "2.0"},
        "extensionsUsed": ["EXT_meshopt_compression", "KHR_texture_basisu"],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 1}]}],
        "accessors": [
            {"bufferView": 0, "count": 24},
            {"bufferView": 1, "count": 36},
        ],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": 288},
            {"buffer": 0, "byteOffset": 288, "byteLength": 72},
            {"buffer": 0, "byteOffset": 360, "byteLength": 4096},
        ],
        "images": [{"bufferView": 2, "mimeType": "image/ktx2"}],
        "textures": [{"extensions": {"KHR_texture_basisu": {"source": 0}}}],
    })

    runtime = api._file_derived_metadata(glb, "glb")[1]
    stats = api._gltf_runtime_cost_metadata(glb, "glb", runtime, len(glb))
    assert stats["triangle_count"] == 12
    assert stats["vertex_count"] == 24
    assert stats["texture_count"] == 1
    assert stats["largest_texture_bytes"] == 4096
    assert stats["geometry_buffer_bytes"] == 360
    assert stats["texture_vram_bytes"] == 4096
    assert stats["approx_vram_bytes"] == 4456
    assert stats["total_byte_size"] == len(glb)
    assert stats["ktx2"] is True
    assert stats["meshopt"] is True


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
    assert enriched["asset_category"] == "environment"
    assert enriched["description"].startswith("A stylized wooden signpost prop")
    assert {"signpost", "wooden-sign", "signboard"}.issubset(set(enriched["tags"]))
    assert {"stylized", "fantasy", "low-poly"}.issubset(set(enriched["asset_styles"]))
    assert {"decorative-prop", "low-poly"}.issubset(set(enriched["asset_types"]))
    assert "flora" not in enriched["asset_types"]


def test_ai_enrichment_enforces_subject_category_taxonomy(monkeypatch):
    from app import ai_enrichment

    class FakeModel:
        name = "category_probe.glb"
        description = ""
        original_filename = "category_probe.glb"
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

    cases = [
        (
            "Stylized Mouse Character",
            "person",
            "A cute stylized mouse animal character with large ears, whiskers, paws, and fur.",
            "fauna",
        ),
        (
            "Stone Bridge",
            "building",
            "A small arched stone bridge for an outdoor garden path scene.",
            "environment",
        ),
        (
            "Fantasy Lantern",
            "material",
            "A metal and glass hanging lantern prop for outdoor fantasy village scenes.",
            "environment",
        ),
        (
            "Silk Robe Avatar",
            "material",
            "A humanoid female avatar wearing a blue silk robe with fabric folds and ornate trim.",
            "person",
        ),
        (
            "Seamless Moss Texture",
            "flora",
            "A seamless tileable moss texture material with albedo, roughness, and normal map detail.",
            "flora",
        ),
    ]

    for title, category, description, expected in cases:
        def fake_ai_metadata(model, extra_context=None, *, title=title, category=category, description=description):
            return {
                "title": title,
                "asset_category": category,
                "asset_styles": [],
                "asset_types": ["decorative-prop"],
                "runtime_metadata": {},
                "tags": title.lower().split(),
                "description": description,
                "summary": description,
                "categories": [],
                "quality_notes": [],
            }

        monkeypatch.setattr(ai_enrichment, "_ai_metadata", fake_ai_metadata)
        enriched = ai_enrichment.enrich_model(FakeModel())
        assert enriched["asset_category"] == expected, title


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
    assert "Animals and creatures are always fauna" in system_text
    assert "Do not use material as a category" in user_text
    assert schema["properties"]["tags"]["maxItems"] == 10
    assert "Fab listing title under 80 characters" in schema["properties"]["title"]["description"]
    assert "Buyer-facing Fab product description" in schema["properties"]["description"]["description"]
    assert "furniture for furniture" in schema["properties"]["asset_category"]["description"]
    assert "do not use material as a category" in schema["properties"]["asset_category"]["description"]
    assert enriched["title"] == "Classical Stone Fountain"
