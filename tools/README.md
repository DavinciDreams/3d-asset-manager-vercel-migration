# Vendored Conversion Tools

These files support `app/conversion.py`.

- `fbx2vrma-converter.js` converts humanoid FBX animation data through
  FBX2glTF and writes a VRMA-compatible glTF JSON payload.
- `package.json` declares the small Node runtime dependencies installed in the
  Docker image.

`FBX2glTF` and `assimp` are installed by the Dockerfile. Tool paths can be
overridden with `FBX2GLTF_BIN`, `ASSIMP_BIN`, `NODE_BIN`, and `FBX2VRMA_DIR`.
