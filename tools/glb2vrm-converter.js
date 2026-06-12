#!/usr/bin/env node
"use strict";

// GLB -> VRM converter.
//
// Takes a rigged GLB (e.g. exported from mesh2motion with "Mixamo" bone naming,
// or any GLB whose skinned skeleton uses recognizable humanoid joint names) and
// turns it into a VRM 1.0 avatar by injecting the VRMC_vrm extension with a
// humanoid bone map -- WITHOUT touching the mesh, skin weights, or binary
// buffer. The result loads in this app's VRM viewer and can play the VRMA clips
// produced by fbx2vrma / bvh2vrma.
//
// It does NOT create a skeleton or skin a mesh (mesh2motion does that). It only
// adds the VRM humanoid metadata that GLB lacks.
//
// Usage:
//   node glb2vrm-converter.js -i rigged.glb -o avatar.vrm [--map overrides.json]
//                             [--name "My Avatar"] [--author "me"]

const fs = require("fs-extra");
const path = require("path");
const { Command } = require("commander");
const { jointToVrmBone, VRM_REQUIRED_BONES } = require("./vrm-bone-map");

const GLB_MAGIC = 0x46546c67; // "glTF"
const CHUNK_JSON = 0x4e4f534a; // "JSON"
const CHUNK_BIN = 0x004e4942; // "BIN\0"

// ---- GLB container read -----------------------------------------------------

function readGlb(buf) {
  if (buf.length < 12) throw new Error("Not a GLB: too short");
  const magic = buf.readUInt32LE(0);
  if (magic !== GLB_MAGIC) {
    // Maybe it's a .gltf (JSON) — support that too.
    try {
      const json = JSON.parse(buf.toString("utf8"));
      return { json, bin: null, isGltf: true };
    } catch (e) {
      throw new Error("Not a GLB (bad magic) and not parseable as .gltf JSON");
    }
  }
  const version = buf.readUInt32LE(4);
  if (version !== 2) throw new Error(`Unsupported GLB version ${version}`);

  let offset = 12;
  let json = null;
  let bin = null;
  while (offset + 8 <= buf.length) {
    const chunkLen = buf.readUInt32LE(offset);
    const chunkType = buf.readUInt32LE(offset + 4);
    const start = offset + 8;
    const end = start + chunkLen;
    const body = buf.slice(start, end);
    if (chunkType === CHUNK_JSON) {
      json = JSON.parse(body.toString("utf8"));
    } else if (chunkType === CHUNK_BIN) {
      bin = body;
    }
    offset = end;
  }
  if (!json) throw new Error("GLB has no JSON chunk");
  return { json, bin, isGltf: false };
}

// ---- GLB container write (JSON chunk replaced, BIN preserved) ----------------

function pad4(buf, padByte) {
  const rem = buf.length % 4;
  if (rem === 0) return buf;
  return Buffer.concat([buf, Buffer.alloc(4 - rem, padByte)]);
}

function writeGlb(json, bin) {
  // A GLB's embedded BIN is buffer 0 with NO uri; ensure buffers[0] exists and
  // its byteLength covers the (unpadded) BIN, else the file is invalid glTF and
  // tools like gltfpack reject it. (Real exporters already emit this; we only
  // guard the edge where it's missing.)
  if (bin && bin.length > 0) {
    json.buffers = json.buffers || [];
    if (!json.buffers[0]) json.buffers[0] = {};
    delete json.buffers[0].uri; // embedded buffer must not have a uri
    json.buffers[0].byteLength = bin.length;
  }
  const jsonBuf = pad4(Buffer.from(JSON.stringify(json), "utf8"), 0x20); // pad with spaces
  const chunks = [];
  // JSON chunk
  const jsonHeader = Buffer.alloc(8);
  jsonHeader.writeUInt32LE(jsonBuf.length, 0);
  jsonHeader.writeUInt32LE(CHUNK_JSON, 4);
  chunks.push(jsonHeader, jsonBuf);
  // BIN chunk (preserved verbatim, re-padded with zeros if needed)
  if (bin && bin.length > 0) {
    const binBuf = pad4(bin, 0x00);
    const binHeader = Buffer.alloc(8);
    binHeader.writeUInt32LE(binBuf.length, 0);
    binHeader.writeUInt32LE(CHUNK_BIN, 4);
    chunks.push(binHeader, binBuf);
  }
  const body = Buffer.concat(chunks);
  const header = Buffer.alloc(12);
  header.writeUInt32LE(GLB_MAGIC, 0);
  header.writeUInt32LE(2, 4);
  header.writeUInt32LE(12 + body.length, 8);
  return Buffer.concat([header, body]);
}

// ---- humanoid bone mapping --------------------------------------------------

// Find the set of node indices that are actually skeleton joints (referenced by
// any skin.joints). Restricting to real joints avoids mapping a mesh node that
// happens to be named "Head", etc.
function collectJointNodeIndices(gltf) {
  const joints = new Set();
  for (const skin of gltf.skins || []) {
    for (const j of skin.joints || []) joints.add(j);
  }
  return joints;
}

function buildHumanBones(gltf, overrides) {
  const nodes = gltf.nodes || [];
  const jointIndices = collectJointNodeIndices(gltf);
  // If the GLB has no skins (unrigged), fall back to all named nodes so we can
  // still report a useful error, but prefer real joints when present.
  const candidateIndices = jointIndices.size > 0
    ? [...jointIndices]
    : nodes.map((_, i) => i);

  const humanBones = {};
  const usedBone = new Set();
  const mappedJointNames = [];

  for (const idx of candidateIndices) {
    const node = nodes[idx];
    if (!node || !node.name) continue;
    const vrmBone = jointToVrmBone(node.name, overrides);
    if (!vrmBone || usedBone.has(vrmBone)) continue;
    humanBones[vrmBone] = { node: idx };
    usedBone.add(vrmBone);
    mappedJointNames.push(`${node.name} -> ${vrmBone}`);
  }
  return { humanBones, mappedJointNames, hadSkins: jointIndices.size > 0 };
}

function vrmMeta(name, author) {
  // Minimal valid VRM 1.0 meta. Licensing is left permissive-but-explicit; the
  // user can edit it. avatarPermission/commercialUssageName default to safe.
  return {
    name: name || "Untitled",
    version: "1.0",
    authors: [author || "Unknown"],
    licenseUrl: "https://vrm.dev/licenses/1.0/",
    avatarPermission: "onlyAuthor",
    commercialUsage: "personalNonProfit",
    creditNotation: "required",
    allowRedistribution: false,
    modification: "prohibited",
  };
}

function injectVrm(gltf, humanBones, name, author) {
  gltf.extensionsUsed = Array.from(new Set([...(gltf.extensionsUsed || []), "VRMC_vrm"]));
  gltf.extensions = gltf.extensions || {};
  gltf.extensions.VRMC_vrm = {
    specVersion: "1.0",
    meta: vrmMeta(name, author),
    humanoid: { humanBones },
    // firstPerson / lookAt / expressions are optional in VRM 1.0; omit them.
  };
  return gltf;
}

// ---- CLI --------------------------------------------------------------------

async function main() {
  const program = new Command()
    .requiredOption("-i, --input <path>", "Input rigged GLB (or GLTF)")
    .requiredOption("-o, --output <path>", "Output VRM file")
    .option("--map <path>", "JSON overrides: { jointName: vrmBoneName | null }")
    .option("--name <name>", "Avatar name (VRM meta)")
    .option("--author <author>", "Avatar author (VRM meta)")
    .option("--lenient", "Emit even if some required VRM bones are missing", false)
    .parse();

  const opts = program.opts();
  const inputBuf = await fs.readFile(opts.input);

  let overrides = null;
  if (opts.map) {
    const raw = await fs.readJson(opts.map);
    overrides = raw.overrides || raw;
  }

  const { json, bin } = readGlb(inputBuf);
  const { humanBones, mappedJointNames, hadSkins } = buildHumanBones(json, overrides);

  const mapped = Object.keys(humanBones);
  if (!hadSkins) {
    process.stderr.write(
      "warning: input GLB has no skinned mesh / skeleton. It must be rigged first " +
      "(e.g. in mesh2motion) before it can become a VRM.\n"
    );
  }

  const missingRequired = VRM_REQUIRED_BONES.filter((b) => !humanBones[b]);
  if (missingRequired.length > 0 && !opts.lenient) {
    throw new Error(
      `Cannot build a valid VRM: ${missingRequired.length} required humanoid bones are unmapped: ` +
      `${missingRequired.join(", ")}.\n` +
      `Mapped ${mapped.length}: ${mapped.join(", ") || "(none)"}.\n` +
      `Tip: export from mesh2motion with "Mixamo" bone naming, or pass --map overrides.json. ` +
      `Use --lenient to emit anyway (viewer may not accept it).`
    );
  }

  injectVrm(json, humanBones, opts.name || path.basename(opts.output, path.extname(opts.output)), opts.author);

  const out = bin === null ? Buffer.from(JSON.stringify(json), "utf8") : writeGlb(json, bin);
  await fs.writeFile(opts.output, out);

  process.stderr.write(
    `glb2vrm: mapped ${mapped.length} humanoid bones` +
    (missingRequired.length ? ` (LENIENT: missing ${missingRequired.length} required)` : "") +
    ` -> ${opts.output}\n`
  );
  if (process.env.GLB2VRM_VERBOSE) {
    process.stderr.write(mappedJointNames.join("\n") + "\n");
  }
}

if (require.main === module) {
  main().catch((error) => {
    console.error(error.message || error);
    process.exit(1);
  });
}

module.exports = { readGlb, writeGlb, buildHumanBones, injectVrm };
