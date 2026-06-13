"""Background conversion pipeline for uploaded 3D models.

The pipeline stores derived files through the app's FILE_STORE abstraction, so
it works with either the local database fallback or MinIO/S3 in production.
"""
import json
import os
import shutil
import subprocess
import tempfile
import threading
from datetime import datetime, timedelta

from sqlalchemy import and_, or_, select, update

from app.db import models


NATIVE_VIEWABLE = {"glb", "gltf", "vrm"}
CONVERTIBLE_TO_GLB = {"fbx", "obj", "stl", "dae", "ply", "3ds"}
# Animation-only formats: no mesh to view, converted straight to a VRMA clip.
CONVERTIBLE_TO_VRMA = {"bvh"}

MIXAMO_BONES = {
    "mixamorig:Hips", "mixamorig:Spine", "mixamorig:Spine1", "mixamorig:Spine2",
    "mixamorig:Neck", "mixamorig:Head", "mixamorig:LeftShoulder", "mixamorig:LeftArm",
    "mixamorig:LeftForeArm", "mixamorig:LeftHand", "mixamorig:RightShoulder",
    "mixamorig:RightArm", "mixamorig:RightForeArm", "mixamorig:RightHand",
    "mixamorig:LeftUpLeg", "mixamorig:LeftLeg", "mixamorig:LeftFoot",
    "mixamorig:RightUpLeg", "mixamorig:RightLeg", "mixamorig:RightFoot",
}
HUMANOID_BONE_THRESHOLD = 6
STALE_PROCESSING_MINUTES = 10


def _cfg(app, key, default):
    return app.config.get(key, default)


def tool_paths(app):
    return {
        "fbx2gltf": _cfg(app, "FBX2GLTF_BIN", os.environ.get("FBX2GLTF_BIN", "/usr/local/bin/FBX2glTF")),
        "assimp": _cfg(app, "ASSIMP_BIN", os.environ.get("ASSIMP_BIN", "assimp")),
        "node": _cfg(app, "NODE_BIN", os.environ.get("NODE_BIN", "node")),
        "fbx2vrma_dir": _cfg(app, "FBX2VRMA_DIR", os.environ.get("FBX2VRMA_DIR", "/app/tools")),
    }


def _run(cmd, timeout):
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:500]
        raise RuntimeError(f"{cmd[0]} failed (exit {proc.returncode}): {detail}")
    return proc


def fbx2gltf_to_glb(fbx2gltf_bin, input_path, out_dir, timeout=120):
    out_base = os.path.join(out_dir, "viewable")
    _run([fbx2gltf_bin, "-i", input_path, "-o", out_base, "-b"], timeout)
    glb = out_base + ".glb"
    if os.path.exists(glb):
        return glb
    alt_dir = out_base + "_out"
    if os.path.isdir(alt_dir):
        for name in os.listdir(alt_dir):
            if name.lower().endswith(".glb"):
                return os.path.join(alt_dir, name)
    raise RuntimeError("FBX2glTF produced no .glb output")


def assimp_export(assimp_bin, input_path, output_path, timeout=120):
    _run([assimp_bin, "export", input_path, output_path], timeout)
    if not os.path.exists(output_path):
        raise RuntimeError("assimp produced no output")
    return output_path


def fbx_to_vrma(node_bin, converter_dir, fbx2gltf_bin, input_path, output_path, timeout=180):
    script = os.path.join(converter_dir, "fbx2vrma-converter.js")
    _run([
        node_bin, script,
        "-i", input_path,
        "-o", output_path,
        "--fbx2gltf", fbx2gltf_bin,
    ], timeout)
    if not os.path.exists(output_path):
        raise RuntimeError("fbx2vrma produced no output")
    return output_path


def bvh_to_vrma(node_bin, converter_dir, input_path, output_path, clip_name=None, timeout=120):
    """Convert a BVH mocap clip straight to a VRMA animation (pure JS, no FBX2glTF)."""
    script = os.path.join(converter_dir, "bvh2vrma-converter.js")
    cmd = [node_bin, script, "-i", input_path, "-o", output_path]
    if clip_name:
        cmd += ["--name", clip_name]
    _run(cmd, timeout)
    if not os.path.exists(output_path):
        raise RuntimeError("bvh2vrma produced no output")
    return output_path


def glb_to_vrm(node_bin, converter_dir, input_path, output_path,
               name=None, author=None, timeout=120):
    """Turn a rigged GLB (humanoid/mixamorig skeleton, e.g. from mesh2motion)
    into a VRM by injecting the VRMC_vrm humanoid extension. Pure JS; the mesh
    and binary buffer are preserved untouched."""
    script = os.path.join(converter_dir, "glb2vrm-converter.js")
    cmd = [node_bin, script, "-i", input_path, "-o", output_path]
    if name:
        cmd += ["--name", name]
    if author:
        cmd += ["--author", author]
    _run(cmd, timeout)
    if not os.path.exists(output_path):
        raise RuntimeError("glb2vrm produced no output")
    return output_path


def gltf_node_names(glb_or_gltf_path):
    try:
        with open(glb_or_gltf_path, "rb") as f:
            head = f.read(4)
            f.seek(0)
            if head == b"glTF":
                import struct
                data = f.read()
                if len(data) < 20:
                    return set()
                offset = 12
                json_bytes = None
                while offset + 8 <= len(data):
                    clen, ctype = struct.unpack("<II", data[offset:offset + 8])
                    body = data[offset + 8:offset + 8 + clen]
                    if ctype == 0x4E4F534A:
                        json_bytes = body
                        break
                    offset += 8 + clen
                if json_bytes is None:
                    return set()
                gltf = json.loads(json_bytes.decode("utf-8", errors="ignore"))
            else:
                gltf = json.loads(f.read().decode("utf-8", errors="ignore"))
        return {node.get("name") for node in gltf.get("nodes", []) if node.get("name")}
    except Exception as e:
        print(f"gltf_node_names warning: {e}")
        return set()


def looks_humanoid(node_names):
    return len(node_names & MIXAMO_BONES) >= HUMANOID_BONE_THRESHOLD


def patch_model(app, model_id, **fields):
    with app.config["DB_ENGINE"].begin() as conn:
        conn.execute(update(models).where(models.c.id == str(model_id)).values(**fields))


def process_model_doc(app, doc):
    fs = app.config["FILE_STORE"]
    paths = tool_paths(app)

    model_id = doc["id"]
    fmt = (doc.get("file_format") or "").lower()

    if fmt in NATIVE_VIEWABLE:
        patch_model(app, model_id, conversion_status="skipped", conversion_error=None)
        return "skipped"
    if fmt not in CONVERTIBLE_TO_GLB and fmt not in CONVERTIBLE_TO_VRMA:
        patch_model(app, model_id, conversion_status="skipped", conversion_error=None)
        return "skipped"

    src_id = doc.get("file_id")
    if not src_id:
        patch_model(app, model_id, conversion_status="failed", conversion_error="No source file.")
        return "failed"

    workdir = tempfile.mkdtemp(prefix="convert_")
    try:
        in_path = os.path.join(workdir, "input." + fmt)
        with open(in_path, "wb") as f:
            f.write(fs.get(src_id).read())

        # BVH is animation-only: no mesh to view, so produce a VRMA clip directly.
        if fmt in CONVERTIBLE_TO_VRMA:
            vrma_path = os.path.join(workdir, "clip.vrma")
            bvh_to_vrma(
                paths["node"], paths["fbx2vrma_dir"], in_path, vrma_path,
                clip_name=(doc.get("name") or None),
            )
            with open(vrma_path, "rb") as f:
                vrma_bytes = f.read()
            vrma_id = fs.put(
                vrma_bytes,
                filename=f"clip_{model_id}.vrma",
                content_type="application/octet-stream",
                metadata={"derived_for": str(model_id), "kind": "vrma"},
            )
            patch_model(
                app, model_id,
                vrma_file_id=str(vrma_id),
                conversion_status="done",
                conversion_error=None,
            )
            return "done"

        if fmt == "fbx":
            glb_path = fbx2gltf_to_glb(paths["fbx2gltf"], in_path, workdir)
        else:
            glb_path = os.path.join(workdir, "viewable.glb")
            assimp_export(paths["assimp"], in_path, glb_path)

        with open(glb_path, "rb") as f:
            glb_bytes = f.read()
        viewable_id = fs.put(
            glb_bytes,
            filename=f"viewable_{model_id}.glb",
            content_type="model/gltf-binary",
            metadata={"derived_for": str(model_id), "kind": "viewable"},
        )

        fields = {
            "viewable_file_id": str(viewable_id),
            "viewable_format": "glb",
            "conversion_error": None,
        }

        if fmt == "fbx" and looks_humanoid(gltf_node_names(glb_path)):
            try:
                vrma_path = os.path.join(workdir, "clip.vrma")
                fbx_to_vrma(paths["node"], paths["fbx2vrma_dir"], paths["fbx2gltf"], in_path, vrma_path)
                with open(vrma_path, "rb") as f:
                    vrma_bytes = f.read()
                fields["vrma_file_id"] = str(fs.put(
                    vrma_bytes,
                    filename=f"clip_{model_id}.vrma",
                    content_type="application/octet-stream",
                    metadata={"derived_for": str(model_id), "kind": "vrma"},
                ))
            except Exception as e:
                print(f"VRMA generation failed for {model_id} (non-fatal): {e}")

            # The viewable GLB from a humanoid (Mixamo-rigged) FBX already has a
            # mixamorig:* skeleton -- exactly what glb2vrm needs. Auto-produce a
            # VRM avatar variant so the model can play VRMA clips in the VRM
            # viewer. Non-fatal: a non-humanoid-enough GLB just won't get a VRM.
            try:
                vrm_path = os.path.join(workdir, "avatar.vrm")
                glb_to_vrm(
                    paths["node"], paths["fbx2vrma_dir"], glb_path, vrm_path,
                    name=(doc.get("name") or None),
                )
                with open(vrm_path, "rb") as f:
                    vrm_bytes = f.read()
                vrm_id = fs.put(
                    vrm_bytes,
                    filename=f"avatar_{model_id}.vrm",
                    content_type="model/gltf-binary",
                    metadata={"derived_for": str(model_id), "kind": "vrm"},
                )
                from app.models import ModelVariant, Model3D
                _, old_vrm_id = ModelVariant.upsert(
                    model_id, "vrm", str(vrm_id),
                    file_format="vrm", size=len(vrm_bytes), status="ready",
                )
                if old_vrm_id and old_vrm_id != str(vrm_id):
                    try:
                        fs.delete(old_vrm_id)
                    except Exception as e:
                        print(f"Old VRM blob {old_vrm_id} not deleted: {e}")

                # Auto-produce the rig-safe optimized avatar. Best-effort:
                # imported lazily to avoid an api<->conversion import cycle.
                try:
                    from app.api import _optimize_vrm_variant
                    model_obj = Model3D.get_by_id(model_id)
                    if model_obj:
                        _optimize_vrm_variant(model_obj)
                except Exception as e:
                    print(f"Auto VRM optimization skipped for {model_id}: {e}")
            except Exception as e:
                print(f"VRM generation failed for {model_id} (non-fatal): {e}")

        fields["conversion_status"] = "done"
        patch_model(app, model_id, **fields)
        return "done"

    except subprocess.TimeoutExpired:
        patch_model(app, model_id, conversion_status="failed", conversion_error="Conversion timed out.")
        return "failed"
    except Exception as e:
        msg = str(e)[:300]
        print(f"Conversion failed for {model_id}: {msg}")
        patch_model(app, model_id, conversion_status="failed", conversion_error=msg)
        return "failed"
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def claim_one(engine):
    now = datetime.utcnow()
    stale_before = now - timedelta(minutes=STALE_PROCESSING_MINUTES)
    claimable = or_(
        models.c.conversion_status == "pending",
        and_(models.c.conversion_status == "processing", models.c.conversion_claimed_at < stale_before),
    )
    with engine.begin() as conn:
        row = conn.execute(
            select(models).where(claimable).order_by(models.c.upload_date.asc()).limit(1)
        ).mappings().first()
        if not row:
            return None
        updated = conn.execute(
            update(models)
            .where(and_(models.c.id == row.id, claimable))
            .values(conversion_status="processing", conversion_claimed_at=now)
        )
        if updated.rowcount != 1:
            return None
        return dict(row)


def drain_once(app):
    processed = 0
    while True:
        doc = claim_one(app.config["DB_ENGINE"])
        if not doc:
            break
        with app.app_context():
            process_model_doc(app, doc)
        processed += 1
    return processed


class ConversionWorker:
    def __init__(self, app, poll_interval=2.0):
        self.app = app
        self.poll_interval = poll_interval
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="conversion-worker", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                drain_once(self.app)
            except Exception as e:
                print(f"Conversion worker loop error: {e}")
            self._stop.wait(self.poll_interval)


def enqueue(model, enabled=True):
    fmt = (model.file_format or "").lower()
    if fmt in NATIVE_VIEWABLE:
        status = "skipped"
    elif fmt in CONVERTIBLE_TO_GLB or fmt in CONVERTIBLE_TO_VRMA:
        status = "pending" if enabled else "skipped"
        if not enabled:
            model.conversion_error = "Conversion is disabled on this server."
    else:
        status = "skipped"
    model.conversion_status = status
    return status


def start_worker(app):
    if not app.config.get("ENABLE_CONVERSION", True):
        return None
    worker = ConversionWorker(app)
    worker.start()
    app.config["CONVERSION_WORKER"] = worker
    return worker
