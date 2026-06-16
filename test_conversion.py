import io
import json
import io
import os
import struct
import base64

os.environ.setdefault("SQLITE_PATH", ":memory:")
os.environ.setdefault("ENABLE_CONVERSION", "0")

from app import create_app
from app import conversion
from app.models import Model3D, User


def make_glb_with_nodes(names):
    gltf = {"asset": {"version": "2.0"}, "nodes": [{"name": name} for name in names]}
    json_bytes = json.dumps(gltf).encode("utf-8")
    json_bytes += b" " * ((4 - len(json_bytes) % 4) % 4)
    total_len = 12 + 8 + len(json_bytes)
    return (
        struct.pack("<III", 0x46546C67, 2, total_len)
        + struct.pack("<II", len(json_bytes), 0x4E4F534A)
        + json_bytes
    )


def glb_json(glb_bytes):
    offset = 12
    while offset + 8 <= len(glb_bytes):
        length, chunk_type = struct.unpack("<II", glb_bytes[offset:offset + 8])
        body = glb_bytes[offset + 8:offset + 8 + length]
        if chunk_type == 0x4E4F534A:
            return json.loads(body.decode("utf-8").rstrip(" \t\r\n\0"))
        offset += 8 + length
    raise AssertionError("No JSON chunk")


def test_pack_embedded_gltf_to_glb_embeds_images_and_buffers(tmp_path):
    binary = b"\x00\x01\x02\x03"
    image = b"fake-jpeg"
    gltf = {
        "asset": {"version": "2.0"},
        "buffers": [{
            "uri": "data:application/octet-stream;base64," + base64.b64encode(binary).decode("ascii"),
            "byteLength": len(binary),
        }],
        "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": len(binary)}],
        "images": [{
            "uri": "data:image/jpeg;base64," + base64.b64encode(image).decode("ascii"),
        }],
        "textures": [{"source": 0}],
        "nodes": [{"mesh": 0, "name": "Mesh"}],
        "meshes": [{"primitives": []}],
    }
    source = tmp_path / "embedded.gltf"
    output = tmp_path / "embedded.glb"
    source.write_text(json.dumps(gltf), encoding="utf-8")

    conversion.pack_embedded_gltf_to_glb(str(source), str(output))

    packed = output.read_bytes()
    assert b"Image_0.jpg" not in packed
    doc = glb_json(packed)
    assert doc["buffers"] == [{"byteLength": 13}]
    assert "uri" not in doc["images"][0]
    assert doc["images"][0]["mimeType"] == "image/jpeg"
    assert doc["images"][0]["bufferView"] == 1
    assert doc["bufferViews"][1]["byteOffset"] == 4
    assert doc["bufferViews"][1]["byteLength"] == len(image)


def _login(app, client, username="convtester"):
    with app.app_context():
        user = User.get_by_username(username)
        if not user:
            user = User(username=username, email=f"{username}@example.com")
            user.set_password("pw123456")
            user.save()
    client.post("/auth/login", data={"login_field": username, "password": "pw123456"})


def _app():
    app = create_app()
    app.config["ENABLE_CONVERSION"] = True
    worker = app.config.get("CONVERSION_WORKER")
    if worker:
        worker.stop()
    return app


def test_enqueue_status():
    assert conversion.enqueue(Model3D(file_format="glb")) == "skipped"
    assert conversion.enqueue(Model3D(file_format="vrm")) == "skipped"
    assert conversion.enqueue(Model3D(file_format="fbx")) == "pending"
    disabled = Model3D(file_format="obj")
    assert conversion.enqueue(disabled, enabled=False) == "skipped"
    assert "disabled" in disabled.conversion_error.lower()


def test_worker_converts_fbx(monkeypatch):
    app = _app()
    client = app.test_client()
    _login(app, client)
    glb_bytes = make_glb_with_nodes(sorted(conversion.MIXAMO_BONES))

    def fake_fbx2gltf(bin_, input_path, out_dir, timeout=120):
        out = os.path.join(out_dir, "viewable.glb")
        with open(out, "wb") as f:
            f.write(glb_bytes)
        return out

    def fake_fbx2vrma(node, cdir, fbxbin, input_path, output_path, timeout=180):
        with open(output_path, "wb") as f:
            f.write(b'{"vrma": true}')
        return output_path

    monkeypatch.setattr(conversion, "fbx2gltf_to_glb", fake_fbx2gltf)
    monkeypatch.setattr(conversion, "fbx_to_vrma", fake_fbx2vrma)

    response = client.post("/api/upload", data={
        "name": "Hero", "is_public": "true",
        "file": (io.BytesIO(b"fake-fbx"), "hero.fbx"),
    }, content_type="multipart/form-data")
    assert response.status_code == 201, response.get_json()
    model_id = response.get_json()["model"]["id"]
    assert response.get_json()["model"]["conversion_status"] == "pending"

    assert conversion.drain_once(app) == 1
    with app.app_context():
        model = Model3D.get_by_id(model_id)
    assert model.conversion_status == "done"
    assert model.viewable_file_id
    assert model.vrma_file_id

    status = client.get(f"/api/model/{model_id}/status").get_json()
    assert status["status"] == "done"
    assert status["has_viewable"] is True
    assert status["has_vrma"] is True

    view = client.get(f"/api/view/{model_id}")
    assert view.status_code == 200
    assert view.content_type == "model/gltf-binary"
    assert view.data == glb_bytes

    original = client.get(f"/api/export/{model_id}?format=fbx")
    assert original.status_code == 200
    assert original.data == b"fake-fbx"
    assert original.headers["Content-Disposition"].endswith('filename="hero.fbx"')


def test_worker_failure_marks_failed(monkeypatch):
    app = _app()
    client = app.test_client()
    _login(app, client, "convfail")

    def boom(*args, **kwargs):
        raise RuntimeError("FBX2glTF exploded")

    monkeypatch.setattr(conversion, "fbx2gltf_to_glb", boom)
    response = client.post("/api/upload", data={
        "name": "Bad", "is_public": "true",
        "file": (io.BytesIO(b"x"), "bad.fbx"),
    }, content_type="multipart/form-data")
    model_id = response.get_json()["model"]["id"]
    conversion.drain_once(app)
    with app.app_context():
        model = Model3D.get_by_id(model_id)
    assert model.conversion_status == "failed"
    assert "exploded" in model.conversion_error


def test_worker_converts_animation_only_fbx_to_vrma(monkeypatch):
    app = _app()
    client = app.test_client()
    _login(app, client, "convclip")

    def no_preview_glb(*args, **kwargs):
        raise RuntimeError("no mesh in FBX")

    def fake_fbx2vrma(node, cdir, fbxbin, input_path, output_path, timeout=180):
        with open(output_path, "wb") as f:
            f.write(b'{"vrma": true}')
        return output_path

    monkeypatch.setattr(conversion, "fbx2gltf_to_glb", no_preview_glb)
    monkeypatch.setattr(conversion, "fbx_to_vrma", fake_fbx2vrma)

    response = client.post("/api/upload", data={
        "name": "Dance Clip", "is_public": "true",
        "file": (io.BytesIO(b"animation-only-fbx"), "dance.fbx"),
    }, content_type="multipart/form-data")
    assert response.status_code == 201, response.get_json()
    model_id = response.get_json()["model"]["id"]

    assert conversion.drain_once(app) == 1
    with app.app_context():
        model = Model3D.get_by_id(model_id)
    assert model.conversion_status == "done"
    assert not model.viewable_file_id
    assert model.vrma_file_id

    status = client.get(f"/api/model/{model_id}/status").get_json()
    assert status["status"] == "done"
    assert status["has_viewable"] is False
    assert status["has_vrma"] is True


def test_obj_uses_assimp(monkeypatch):
    app = _app()
    client = app.test_client()
    _login(app, client, "convobj")
    glb_bytes = make_glb_with_nodes(["Mesh"])
    calls = {"assimp": 0}

    def fake_assimp(bin_, input_path, output_path, timeout=120):
        calls["assimp"] += 1
        with open(output_path, "wb") as f:
            f.write(glb_bytes)
        return output_path

    monkeypatch.setattr(conversion, "assimp_export", fake_assimp)
    response = client.post("/api/upload", data={
        "name": "Crate", "is_public": "true",
        "file": (io.BytesIO(b"o"), "crate.obj"),
    }, content_type="multipart/form-data")
    model_id = response.get_json()["model"]["id"]
    conversion.drain_once(app)
    with app.app_context():
        model = Model3D.get_by_id(model_id)
    assert model.conversion_status == "done"
    assert calls["assimp"] == 1
    assert not model.vrma_file_id
