#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");
const {
  VRM_REQUIRED_BONES,
  jointToVrmBone,
} = require("./vrm-bone-map");

const GLB_MAGIC = 0x46546c67;
const CHUNK_JSON = 0x4e4f534a;

const MIXAMO_REQUIRED = [
  "Hips",
  "Spine",
  "Spine1",
  "Spine2",
  "Neck",
  "Head",
  "LeftShoulder",
  "LeftArm",
  "LeftForeArm",
  "LeftHand",
  "RightShoulder",
  "RightArm",
  "RightForeArm",
  "RightHand",
  "LeftUpLeg",
  "LeftLeg",
  "LeftFoot",
  "LeftToeBase",
  "RightUpLeg",
  "RightLeg",
  "RightFoot",
  "RightToeBase",
];

const VRM_AXIS_PAIRS = [
  ["leftUpperLeg", "leftLowerLeg"],
  ["leftLowerLeg", "leftFoot"],
  ["leftFoot", "leftToes"],
  ["rightUpperLeg", "rightLowerLeg"],
  ["rightLowerLeg", "rightFoot"],
  ["rightFoot", "rightToes"],
  ["leftUpperArm", "leftLowerArm"],
  ["leftLowerArm", "leftHand"],
  ["rightUpperArm", "rightLowerArm"],
  ["rightLowerArm", "rightHand"],
  ["spine", "chest"],
  ["chest", "upperChest"],
  ["upperChest", "neck"],
  ["neck", "head"],
];

function usage() {
  console.error("Usage: node tools/inspect-humanoid-rig.js <model.vrm|model.glb|model.gltf|model.fbx>");
  process.exit(2);
}

function round(n) {
  return Number.isFinite(n) ? Number(n.toFixed(4)) : n;
}

function v3(a) {
  return [Number(a?.[0] || 0), Number(a?.[1] || 0), Number(a?.[2] || 0)];
}

function q4(a) {
  return [Number(a?.[0] || 0), Number(a?.[1] || 0), Number(a?.[2] || 0), Number(a?.[3] ?? 1)];
}

function s3(a) {
  return [Number(a?.[0] ?? 1), Number(a?.[1] ?? 1), Number(a?.[2] ?? 1)];
}

function matIdentity() {
  return [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1];
}

function matMul(a, b) {
  const out = new Array(16).fill(0);
  for (let col = 0; col < 4; col++) {
    for (let row = 0; row < 4; row++) {
      out[col * 4 + row] =
        a[0 * 4 + row] * b[col * 4 + 0] +
        a[1 * 4 + row] * b[col * 4 + 1] +
        a[2 * 4 + row] * b[col * 4 + 2] +
        a[3 * 4 + row] * b[col * 4 + 3];
    }
  }
  return out;
}

function matFromTrs(t, q, s) {
  const [x, y, z, w] = q;
  const [sx, sy, sz] = s;
  const x2 = x + x;
  const y2 = y + y;
  const z2 = z + z;
  const xx = x * x2;
  const xy = x * y2;
  const xz = x * z2;
  const yy = y * y2;
  const yz = y * z2;
  const zz = z * z2;
  const wx = w * x2;
  const wy = w * y2;
  const wz = w * z2;

  return [
    (1 - (yy + zz)) * sx,
    (xy + wz) * sx,
    (xz - wy) * sx,
    0,
    (xy - wz) * sy,
    (1 - (xx + zz)) * sy,
    (yz + wx) * sy,
    0,
    (xz + wy) * sz,
    (yz - wx) * sz,
    (1 - (xx + yy)) * sz,
    0,
    t[0],
    t[1],
    t[2],
    1,
  ];
}

function matTranslation(m) {
  return [m[12], m[13], m[14]];
}

function quatMul(a, b) {
  const [ax, ay, az, aw] = a;
  const [bx, by, bz, bw] = b;
  return [
    aw * bx + ax * bw + ay * bz - az * by,
    aw * by - ax * bz + ay * bw + az * bx,
    aw * bz + ax * by - ay * bx + az * bw,
    aw * bw - ax * bx - ay * by - az * bz,
  ];
}

function quatRotate(q, v) {
  const [x, y, z, w] = q;
  const uv = [
    y * v[2] - z * v[1],
    z * v[0] - x * v[2],
    x * v[1] - y * v[0],
  ];
  const uuv = [
    y * uv[2] - z * uv[1],
    z * uv[0] - x * uv[2],
    x * uv[1] - y * uv[0],
  ];
  return [
    v[0] + 2 * (w * uv[0] + uuv[0]),
    v[1] + 2 * (w * uv[1] + uuv[1]),
    v[2] + 2 * (w * uv[2] + uuv[2]),
  ];
}

function vecAdd(a, b) {
  return [a[0] + b[0], a[1] + b[1], a[2] + b[2]];
}

function vecSub(a, b) {
  return [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
}

function vecLen(v) {
  return Math.hypot(v[0], v[1], v[2]);
}

function vecNorm(v) {
  const len = vecLen(v) || 1;
  return [v[0] / len, v[1] / len, v[2] / len];
}

function formatVec(v) {
  return `[${round(v[0])}, ${round(v[1])}, ${round(v[2])}]`;
}

function matAxis(m, axis) {
  const offset = axis * 4;
  return vecNorm([m[offset], m[offset + 1], m[offset + 2]]);
}

function expectedAxisWarning(a, b, dir) {
  const checks = {
    "leftUpperArm->leftLowerArm": dir[0] < 0.5 ? "expected left upper arm mostly +X" : null,
    "leftLowerArm->leftHand": dir[0] < 0.5 ? "expected left forearm mostly +X" : null,
    "rightUpperArm->rightLowerArm": dir[0] > -0.5 ? "expected right upper arm mostly -X" : null,
    "rightLowerArm->rightHand": dir[0] > -0.5 ? "expected right forearm mostly -X" : null,
    "leftUpperLeg->leftLowerLeg": dir[1] > -0.7 ? "expected left upper leg mostly -Y" : null,
    "leftLowerLeg->leftFoot": dir[1] > -0.7 ? "expected left lower leg mostly -Y" : null,
    "rightUpperLeg->rightLowerLeg": dir[1] > -0.7 ? "expected right upper leg mostly -Y" : null,
    "rightLowerLeg->rightFoot": dir[1] > -0.7 ? "expected right lower leg mostly -Y" : null,
    "leftFoot->leftToes": dir[2] < 0.35 ? "expected left toes to point mostly +Z" : null,
    "rightFoot->rightToes": dir[2] < 0.35 ? "expected right toes to point mostly +Z" : null,
  };
  return checks[`${a}->${b}`] || null;
}

function readGltf(file) {
  const buf = fs.readFileSync(file);
  const ext = path.extname(file).toLowerCase();
  if (ext === ".gltf") {
    return JSON.parse(buf.toString("utf8"));
  }
  if (buf.length < 12 || buf.readUInt32LE(0) !== GLB_MAGIC) {
    throw new Error("Not a GLB/VRM file");
  }
  let offset = 12;
  while (offset + 8 <= buf.length) {
    const chunkLen = buf.readUInt32LE(offset);
    const chunkType = buf.readUInt32LE(offset + 4);
    const body = buf.slice(offset + 8, offset + 8 + chunkLen);
    if (chunkType === CHUNK_JSON) {
      return JSON.parse(body.toString("utf8"));
    }
    offset += 8 + chunkLen;
  }
  throw new Error("No JSON chunk found");
}

function collectParents(gltf) {
  const parents = new Map();
  for (const [idx, node] of (gltf.nodes || []).entries()) {
    for (const child of node.children || []) {
      parents.set(child, idx);
    }
  }
  return parents;
}

function computeWorldTransforms(gltf) {
  const parents = collectParents(gltf);
  const cache = new Map();
  function visit(idx) {
    if (cache.has(idx)) return cache.get(idx);
    const node = gltf.nodes[idx] || {};
    const localT = v3(node.translation);
    const localQ = q4(node.rotation);
    const localS = s3(node.scale);
    const localM = Array.isArray(node.matrix) && node.matrix.length === 16
      ? node.matrix.map(Number)
      : matFromTrs(localT, localQ, localS);
    const parentIdx = parents.get(idx);
    if (parentIdx === undefined) {
      const root = { t: matTranslation(localM), q: localQ, m: localM };
      cache.set(idx, root);
      return root;
    }
    const parent = visit(parentIdx);
    const worldM = matMul(parent.m || matIdentity(), localM);
    const t = matTranslation(worldM);
    const q = quatMul(parent.q, localQ);
    const result = { t, q, m: worldM };
    cache.set(idx, result);
    return result;
  }
  for (let i = 0; i < (gltf.nodes || []).length; i++) visit(i);
  return cache;
}

function vrmHumanBones(gltf) {
  const vrm1 = gltf.extensions?.VRMC_vrm;
  if (vrm1) {
    const out = {};
    for (const [bone, ref] of Object.entries(vrm1.humanoid?.humanBones || {})) {
      if (typeof ref.node === "number") out[bone] = ref.node;
    }
    return { version: `VRM 1.0 (${vrm1.specVersion || "unknown spec"})`, bones: out };
  }
  const vrm0 = gltf.extensions?.VRM;
  if (vrm0) {
    const out = {};
    for (const ref of vrm0.humanoid?.humanBones || []) {
      if (ref.bone && typeof ref.node === "number") out[ref.bone] = ref.node;
    }
    return { version: `VRM 0.x (${vrm0.meta?.version || "unknown meta"})`, bones: out };
  }
  return { version: null, bones: {} };
}

function mixamoBonesFromGltf(gltf) {
  const out = new Map();
  const jointNodes = new Set();
  for (const skin of gltf.skins || []) {
    for (const joint of skin.joints || []) jointNodes.add(joint);
  }
  const candidateNodes = jointNodes.size ? [...jointNodes] : (gltf.nodes || []).map((_, i) => i);
  for (const idx of candidateNodes) {
    const name = gltf.nodes?.[idx]?.name || "";
    const m = name.match(/(?:^|[:_])([A-Za-z0-9_]+)$/);
    const stripped = name.replace(/^mixamorig[:_]?/i, "");
    if (name.toLowerCase().includes("mixamorig") || jointToVrmBone(name)) {
      out.set(stripped, idx);
    } else if (m && jointToVrmBone(m[1])) {
      out.set(m[1], idx);
    }
  }
  return out;
}

function humanoidFromMixamoNames(gltf) {
  const out = {};
  const jointNodes = new Set();
  for (const skin of gltf.skins || []) {
    for (const joint of skin.joints || []) jointNodes.add(joint);
  }
  const candidateNodes = jointNodes.size ? [...jointNodes] : (gltf.nodes || []).map((_, i) => i);
  for (const idx of candidateNodes) {
    const name = gltf.nodes?.[idx]?.name || "";
    const vrmBone = jointToVrmBone(name);
    if (vrmBone && out[vrmBone] === undefined) out[vrmBone] = idx;
  }
  return out;
}

function detectForward(gltf, humanBones, world) {
  const head = humanBones.head;
  const leftEye = humanBones.leftEye;
  const rightEye = humanBones.rightEye;
  const leftToes = humanBones.leftToes;
  const rightToes = humanBones.rightToes;
  const leftFoot = humanBones.leftFoot;
  const rightFoot = humanBones.rightFoot;

  if (typeof leftEye === "number" && typeof rightEye === "number" && typeof head === "number") {
    const eyeMid = vecNorm(vecSub(
      vecAdd(world.get(leftEye).t, world.get(rightEye).t).map((n) => n / 2),
      world.get(head).t,
    ));
    return { source: "eyes-head", vector: eyeMid, label: eyeMid[2] >= 0 ? "+Z" : "-Z" };
  }
  if ([leftToes, rightToes, leftFoot, rightFoot].every((v) => typeof v === "number")) {
    const toeMid = vecAdd(world.get(leftToes).t, world.get(rightToes).t).map((n) => n / 2);
    const footMid = vecAdd(world.get(leftFoot).t, world.get(rightFoot).t).map((n) => n / 2);
    const footFwd = vecNorm(vecSub(toeMid, footMid));
    return { source: "toes-feet", vector: footFwd, label: footFwd[2] >= 0 ? "+Z" : "-Z" };
  }
  return { source: "unknown", vector: [0, 0, 0], label: "unknown" };
}

function inspectGltfLike(file) {
  const gltf = readGltf(file);
  const human = vrmHumanBones(gltf);
  const mixamo = mixamoBonesFromGltf(gltf);
  const mappedHuman = Object.keys(human.bones).length ? human.bones : humanoidFromMixamoNames(gltf);
  const world = computeWorldTransforms(gltf);
  const humanBoneNames = Object.keys(mappedHuman);
  const requiredMissing = VRM_REQUIRED_BONES.filter((name) => !(name in mappedHuman));
  const mixamoMissing = MIXAMO_REQUIRED.filter((name) => !mixamo.has(name));
  const forward = detectForward(gltf, mappedHuman, world);

  console.log(`file: ${file}`);
  console.log(`format: ${human.version ? human.version : "glTF/GLB"}`);
  console.log(`asset.generator: ${gltf.asset?.generator || "unknown"}`);
  console.log(`nodes: ${(gltf.nodes || []).length}`);
  console.log(`meshes: ${(gltf.meshes || []).length}`);
  console.log(`skins: ${(gltf.skins || []).length}`);
  console.log(`humanoid bones: ${humanBoneNames.length}${human.version ? "" : " (mapped from names)"}`);
  console.log(`mixamo-like bones: ${mixamo.size}`);
  console.log(`vrm required: ${requiredMissing.length ? `missing ${requiredMissing.join(", ")}` : "pass"}`);
  console.log(`mixamo core: ${mixamoMissing.length ? `missing ${mixamoMissing.join(", ")}` : "pass"}`);
  console.log(`forward: ${forward.label} (${forward.source}) ${formatVec(forward.vector)}`);
  console.log("");
  console.log("limb axes:");
  const warnings = [];
  for (const [a, b] of VRM_AXIS_PAIRS) {
    const ia = mappedHuman[a];
    const ib = mappedHuman[b];
    if (typeof ia !== "number" || typeof ib !== "number") continue;
    const delta = vecSub(world.get(ib).t, world.get(ia).t);
    const dir = vecNorm(delta);
    const warning = expectedAxisWarning(a, b, dir);
    if (warning) warnings.push(`${a} -> ${b}: ${warning}; got ${formatVec(dir)}`);
    console.log(`  ${a} -> ${b}: len=${round(vecLen(delta))} dir=${formatVec(dir)}`);
  }
  if (warnings.length) {
    console.log("");
    console.log("axis warnings:");
    for (const warning of warnings) console.log(`  - ${warning}`);
  }

  const rollBones = [
    "leftUpperArm",
    "leftLowerArm",
    "rightUpperArm",
    "rightLowerArm",
    "leftHand",
    "rightHand",
  ];
  console.log("");
  console.log("arm local axes in world:");
  for (const bone of rollBones) {
    const idx = mappedHuman[bone];
    if (typeof idx !== "number") continue;
    const tx = world.get(idx);
    if (!tx?.m) continue;
    console.log(`  ${bone}: x=${formatVec(matAxis(tx.m, 0))} y=${formatVec(matAxis(tx.m, 1))} z=${formatVec(matAxis(tx.m, 2))}`);
  }
}

function inspectFbxStringFallback(file) {
  const text = fs.readFileSync(file).toString("latin1");
  const isBinary = text.startsWith("Kaydara FBX Binary");
  const names = [...text.matchAll(/mixamorig:[A-Za-z0-9_]+/g)].map((m) => m[0]);
  const unique = [...new Set(names)];
  const stripped = new Set(unique.map((name) => name.replace(/^mixamorig:/, "").replace(/_skin$/, "")));
  const requiredMissing = MIXAMO_REQUIRED.filter((name) => !stripped.has(name));
  const hasAnimation = /AnimationStack|AnimationLayer|AnimationCurve/.test(text);
  const hasSkin = /Deformer|Cluster|Skin/i.test(text);
  const mappedVrm = new Set();
  for (const name of unique) {
    const vrm = jointToVrmBone(name.replace(/_skin$/, ""));
    if (vrm) mappedVrm.add(vrm);
  }
  const missingVrm = VRM_REQUIRED_BONES.filter((name) => !mappedVrm.has(name));

  console.log(`file: ${file}`);
  console.log(`format: FBX ${isBinary ? "binary" : "ascii/string fallback"}`);
  console.log("parser: string fallback; transforms/axes unavailable without FBX conversion");
  console.log(`mixamo-like bones: ${unique.length}`);
  console.log(`mixamo core: ${requiredMissing.length ? `missing ${requiredMissing.join(", ")}` : "pass"}`);
  console.log(`vrm required via name map: ${missingVrm.length ? `missing ${missingVrm.join(", ")}` : "pass"}`);
  console.log(`skin markers: ${hasSkin ? "present" : "unknown"}`);
  console.log(`animation markers: ${hasAnimation ? "present" : "unknown"}`);
  console.log("");
  console.log("mixamo bones:");
  for (const name of unique) {
    if (name.endsWith("_skin")) continue;
    console.log(`  ${name}`);
  }
}

function main() {
  const file = process.argv[2];
  if (!file) usage();
  const ext = path.extname(file).toLowerCase();
  if (!fs.existsSync(file)) {
    console.error(`File not found: ${file}`);
    process.exit(1);
  }
  if (ext === ".fbx") {
    inspectFbxStringFallback(file);
  } else if ([".vrm", ".glb", ".gltf"].includes(ext)) {
    inspectGltfLike(file);
  } else {
    console.error(`Unsupported extension: ${ext}`);
    process.exit(1);
  }
}

main();
