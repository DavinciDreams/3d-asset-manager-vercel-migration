"""Server-side GLB/GLTF -> PNG thumbnail rendering.

Renders a 3D asset to a still image offscreen using trimesh + pyrender on the
OSMesa software-OpenGL backend (no GPU, no X server). This replaces the fragile
headless-browser capture path: the browser capture only fires for a logged-in
owner (an IS_OWNER gate the token-only capture worker can never satisfy), so it
never worked unattended. Rendering here needs no auth, no WebGL-in-Chromium, and
no MediaRecorder.

Everything is import-on-demand and best-effort: if the GL stack is missing the
caller gets a clear RenderUnavailable rather than an import-time crash, so the
app still boots without the optional render deps.
"""

import io
import os

# Force the software GL backend before PyOpenGL is imported anywhere. Redundant
# with the Dockerfile ENV, but makes local/dev runs work too.
os.environ.setdefault('PYOPENGL_PLATFORM', 'osmesa')


class RenderError(Exception):
    """Rendering failed for a specific asset (bad geometry, empty scene, ...)."""


class RenderUnavailable(RenderError):
    """The render stack (trimesh/pyrender/OSMesa) isn't importable in this env."""


def render_available():
    """Cheap check that the render dependencies import. Cached after first call."""
    global _AVAILABLE
    try:
        return _AVAILABLE
    except NameError:
        pass
    try:
        import trimesh  # noqa: F401
        import pyrender  # noqa: F401
        import numpy  # noqa: F401
        _AVAILABLE = True
    except Exception as e:  # ImportError or a GL backend load failure
        print(f"Server-side render unavailable: {e}")
        _AVAILABLE = False
    return _AVAILABLE


def _load_scene(glb_bytes, file_type):
    import numpy as np
    import trimesh

    loaded = trimesh.load(
        io.BytesIO(glb_bytes), file_type=file_type, force='scene', process=False
    )
    if isinstance(loaded, trimesh.Scene):
        scene = loaded
    else:
        scene = trimesh.Scene(loaded)
    if not scene.geometry:
        raise RenderError('Asset contains no renderable geometry.')
    # Drop degenerate/empty meshes so framing isn't thrown off by stray points.
    if all(getattr(g, 'is_empty', False) for g in scene.geometry.values()):
        raise RenderError('Asset geometry is empty.')
    return scene


def render_glb_to_png(glb_bytes, file_type='glb', size=1024, *, decompress=None):
    """Render GLB/GLTF bytes to PNG bytes (RGB on white).

    `decompress` is an optional callable(bytes) -> bytes used to strip
    EXT_meshopt_compression before loading (trimesh can't read meshopt). Pass
    the app's `_decompress_meshopt_glb`-style helper; if omitted, meshopt assets
    will simply fail to load and raise RenderError, which the caller can log.
    """
    if not render_available():
        raise RenderUnavailable('trimesh/pyrender/OSMesa not installed.')

    import numpy as np
    import pyrender
    import trimesh

    if not glb_bytes or glb_bytes[:4] != b'glTF':
        raise RenderError('Not a binary GLB.')

    if decompress is not None:
        try:
            glb_bytes = decompress(glb_bytes) or glb_bytes
        except Exception as e:
            print(f"meshopt decompress before render failed (using original): {e}")

    scene_tm = _load_scene(glb_bytes, file_type)

    # Center + scale the scene into a unit-ish box so a fixed camera frames it.
    bounds = scene_tm.bounds
    if bounds is None:
        raise RenderError('Could not compute asset bounds.')
    center = bounds.mean(axis=0)
    extent = float(np.linalg.norm(bounds[1] - bounds[0])) or 1.0

    scene = pyrender.Scene(bg_color=[1.0, 1.0, 1.0, 1.0], ambient_light=[0.35, 0.35, 0.35])

    for geom in scene_tm.geometry.values():
        try:
            mesh = pyrender.Mesh.from_trimesh(geom, smooth=False)
            scene.add(mesh)
        except Exception as e:
            # Skip a single bad mesh rather than fail the whole render.
            print(f"Skipping mesh during render: {e}")

    if not list(scene.mesh_nodes):
        raise RenderError('No meshes could be added to the render scene.')

    # A 3/4 front turntable angle, pulled back to frame the whole extent.
    cam = pyrender.PerspectiveCamera(yfov=np.pi / 4.0, aspectRatio=1.0)
    dist = extent * 1.1 + 1e-3
    # Look from front-right-above toward the asset center.
    eye = center + np.array([0.6, 0.5, 1.0]) * dist
    cam_pose = _look_at(eye, center, up=np.array([0.0, 1.0, 0.0]))
    scene.add(cam, pose=cam_pose)

    # Key + fill directional light from the camera direction so nothing is black.
    light = pyrender.DirectionalLight(color=[1.0, 1.0, 1.0], intensity=3.0)
    scene.add(light, pose=cam_pose)

    renderer = None
    try:
        renderer = pyrender.OffscreenRenderer(viewport_width=size, viewport_height=size)
        color, _ = renderer.render(scene)
    except Exception as e:
        raise RenderError(f'Offscreen render failed: {e}')
    finally:
        if renderer is not None:
            try:
                renderer.delete()
            except Exception:
                pass

    from PIL import Image
    img = Image.fromarray(color[:, :, :3], mode='RGB')
    out = io.BytesIO()
    img.save(out, format='PNG')
    return out.getvalue()


def _look_at(eye, target, up):
    """Build a camera-to-world pose matrix (pyrender convention: -Z forward)."""
    import numpy as np

    eye = np.asarray(eye, dtype=float)
    target = np.asarray(target, dtype=float)
    forward = eye - target
    fn = np.linalg.norm(forward)
    forward = forward / fn if fn else np.array([0.0, 0.0, 1.0])
    right = np.cross(up, forward)
    rn = np.linalg.norm(right)
    right = right / rn if rn else np.array([1.0, 0.0, 0.0])
    true_up = np.cross(forward, right)

    pose = np.eye(4)
    pose[:3, 0] = right
    pose[:3, 1] = true_up
    pose[:3, 2] = forward
    pose[:3, 3] = eye
    return pose
