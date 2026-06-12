# Vendored Conversion Tools

These files support `app/conversion.py` and the Mixamo animation library.

## Server-side converters (run automatically on upload)

- **`fbx2vrma-converter.js`** — converts a humanoid FBX animation through
  FBX2glTF and writes a VRMA-compatible glTF (`VRMC_vrm_animation`).
- **`bvh2vrma-converter.js`** — pure-JS BVH (BioVision mocap) → VRMA. No
  FBX2glTF/assimp/Blender needed. Parses the HIERARCHY/MOTION sections, maps
  joints onto VRM humanoid bones, and emits a self-contained `.vrma` (base64
  data-URI buffer). Auto-detects common skeletons (Mixamo-named, CMU, Rokoko,
  3ds Max Biped, `Character1_` prefixes); pass `--map overrides.json`
  (`{ "JointName": "vrmBoneName" | null }`) for custom rigs.
- **`vrm-bone-map.js`** — shared joint→VRM-bone vocabulary used by both
  converters. Fix a mapping here and both pipelines benefit.

`FBX2glTF` and `assimp` are installed by the Dockerfile. Tool paths can be
overridden with `FBX2GLTF_BIN`, `ASSIMP_BIN`, `NODE_BIN`, and `FBX2VRMA_DIR`.

### How uploads flow (see `app/conversion.py`)

| Uploaded format        | Result                                                        |
|------------------------|--------------------------------------------------------------|
| glb / gltf / vrm       | native viewable, conversion skipped                          |
| fbx / obj / stl / dae / ply / 3ds | converted to a viewable GLB; **humanoid FBX** also gets a VRMA (`vrma_file_id`) |
| **bvh**                | converted **directly to a VRMA** clip (animation-only, no mesh) |

Any model with `vrma_file_id` set automatically appears in the VRMA library
served by `GET /api/vrma` (via `list_generated_vrma_for_user`) and can be
applied to any VRM avatar in the viewer — no extra wiring needed.

## Mixamo animation library tooling (local / batch)

- **`animation-list.json`** — curated list of Mixamo clips (name, `mixamoName`,
  category, description). Drives both downloading and output-file naming.
- **`convert-raw-to-vrma.js`** — batch-convert a folder of raw `.fbx`/`.bvh`
  clips into `.vrma`, fuzzy-matching filenames against `animation-list.json`,
  and write a `manifest.json` describing the library.
- **`download-mixamo.js`** — *optional, admin-only* Puppeteer CLI that logs into
  Adobe and bulk-downloads Mixamo FBX. Puppeteer is **not** a package dependency
  (kept out of the server image); install on demand. Automated downloading may
  conflict with Mixamo's ToS — prefer manual download + `convert-raw-to-vrma.js`.

### Build the library (recommended, ToS-safe)

```bash
cd tools
# 1. Manually download the FBX clips named in animation-list.json from
#    mixamo.com into ./animations-raw  (or use download-mixamo.js, see below)
# 2. Batch-convert them to VRMA:
node convert-raw-to-vrma.js --in animations-raw --out ../app/static/animations
#    -> writes hipHopDancing.vrma, wave.vrma, ... + manifest.json
```

### Optional: automated download

```bash
cd tools
npm install puppeteer            # heavy; only for this tool
node download-mixamo.js --out animations-raw --headful   # log in by hand
#    or: MIXAMO_EMAIL=... MIXAMO_PASSWORD=... node download-mixamo.js --headless
node convert-raw-to-vrma.js --in animations-raw --out ../app/static/animations
```
