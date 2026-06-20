"""Smoke-test the server-side GLB->PNG renderer inside the container.

Run this once in the deployed image to confirm the OSMesa software-GL stack
actually renders (the one thing that can't be verified off-Linux):

    docker compose exec app python scripts/render_smoke_test.py path/to/asset.glb

With no argument it renders a built-in trimesh box. Exits non-zero on failure
and writes the PNG to /tmp/render_smoke.png so you can eyeball it.
"""

import sys


def main(argv):
    from app import render as r

    if not r.render_available():
        print("FAIL: render stack unavailable (trimesh/pyrender/OSMesa not importable).")
        return 2

    if len(argv) > 1:
        with open(argv[1], "rb") as f:
            data = f.read()
        print(f"Rendering {argv[1]} ({len(data)} bytes)...")
    else:
        import trimesh
        import io
        scene = trimesh.Scene(trimesh.creation.box(extents=(1, 1, 1)))
        buf = io.BytesIO()
        scene.export(buf, file_type="glb")
        data = buf.getvalue()
        print(f"Rendering built-in box ({len(data)} bytes)...")

    try:
        png = r.render_glb_to_png(data, size=512)
    except Exception as e:
        print(f"FAIL: {type(e).__name__}: {e}")
        return 1

    out = "/tmp/render_smoke.png"
    with open(out, "wb") as f:
        f.write(png)
    print(f"OK: rendered {len(png)} PNG bytes -> {out} (magic {png[:8]!r})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
