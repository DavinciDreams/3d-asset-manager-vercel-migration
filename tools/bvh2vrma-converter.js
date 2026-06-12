#!/usr/bin/env node
"use strict";

// BVH -> VRMA converter.
//
// Reads a BioVision Hierarchy (.bvh) mocap file, maps its joints onto VRM 1.0
// humanoid bones (auto-detecting common skeletons: Mixamo-named, CMU, Rokoko,
// 3ds Max Biped, etc., with an optional JSON override for custom rigs), and
// writes a self-contained glTF carrying a VRMC_vrm_animation extension -- i.e.
// a .vrma clip that the @pixiv/three-vrm-animation loader can apply to any VRM.
//
// Pure JS: no FBX2glTF, no assimp, no Blender. The output buffer is base64
// data-URI embedded so the result is a single portable file.
//
// Usage:
//   node bvh2vrma-converter.js -i clip.bvh -o clip.vrma [--map overrides.json] [--name "Wave"]

const fs = require("fs-extra");
const path = require("path");
const { Command } = require("commander");
const {
  jointToVrmBone,
  HUMANOID_BONE_THRESHOLD,
} = require("./vrm-bone-map");

// ---------------------------------------------------------------------------
// BVH parsing
// ---------------------------------------------------------------------------

// A joint node parsed from the HIERARCHY section.
// { name, offset:[x,y,z], channels:[...], children:[...], parent, isEndSite }

function parseBvh(text) {
  const tokens = text
    .replace(/\r\n/g, "\n")
    .replace(/\r/g, "\n")
    .split(/\s+/)
    .filter((t) => t.length > 0);

  let pos = 0;
  const peek = () => tokens[pos];
  const next = () => tokens[pos++];
  const expect = (tok) => {
    const t = next();
    if (t !== tok) throw new Error(`BVH parse: expected "${tok}", got "${t}" at token ${pos}`);
  };

  const joints = []; // flat list in declaration order (channel order matters)
  let root = null;

  function parseJoint(parent, isRoot) {
    // current token is the joint name (ROOT/JOINT already consumed)
    const name = next();
    expect("{");
    const node = {
      name,
      offset: [0, 0, 0],
      channels: [],
      children: [],
      parent,
      isEndSite: false,
    };
    joints.push(node);
    if (parent) parent.children.push(node);

    while (true) {
      const tok = next();
      if (tok === "OFFSET") {
        node.offset = [parseFloat(next()), parseFloat(next()), parseFloat(next())];
      } else if (tok === "CHANNELS") {
        const count = parseInt(next(), 10);
        for (let i = 0; i < count; i++) node.channels.push(next());
      } else if (tok === "JOINT") {
        parseJoint(node, false);
      } else if (tok === "End") {
        // "End Site" — a leaf offset with no channels; skip its block.
        expect("Site");
        expect("{");
        const end = { name: name + "_End", offset: [0, 0, 0], channels: [], children: [], parent: node, isEndSite: true };
        while (true) {
          const t2 = next();
          if (t2 === "OFFSET") {
            end.offset = [parseFloat(next()), parseFloat(next()), parseFloat(next())];
          } else if (t2 === "}") {
            break;
          }
        }
        joints.push(end);
        node.children.push(end);
      } else if (tok === "}") {
        break;
      } else {
        throw new Error(`BVH parse: unexpected token "${tok}" in joint "${name}"`);
      }
    }
    return node;
  }

  expect("HIERARCHY");
  expect("ROOT");
  root = parseJoint(null, true);

  // MOTION section
  expect("MOTION");
  expect("Frames:");
  const frameCount = parseInt(next(), 10);
  expect("Frame");
  expect("Time:");
  const frameTime = parseFloat(next());

  // Each joint with channels contributes its channel count per frame, in
  // declaration order.
  const channelJoints = joints.filter((j) => j.channels.length > 0);
  const valuesPerFrame = channelJoints.reduce((sum, j) => sum + j.channels.length, 0);

  const frames = [];
  for (let f = 0; f < frameCount; f++) {
    const row = new Array(valuesPerFrame);
    for (let i = 0; i < valuesPerFrame; i++) {
      const v = next();
      if (v === undefined) {
        throw new Error(`BVH parse: ran out of motion data at frame ${f}, value ${i}`);
      }
      row[i] = parseFloat(v);
    }
    frames.push(row);
  }

  return { root, joints, channelJoints, frameCount, frameTime, frames };
}

// ---------------------------------------------------------------------------
// Euler -> quaternion (respecting per-joint channel rotation order)
// ---------------------------------------------------------------------------

const DEG2RAD = Math.PI / 180;

function axisQuat(axis, deg) {
  const half = deg * DEG2RAD * 0.5;
  const s = Math.sin(half);
  const c = Math.cos(half);
  switch (axis) {
    case "X": return [s, 0, 0, c];
    case "Y": return [0, s, 0, c];
    case "Z": return [0, 0, s, c];
    default: return [0, 0, 0, 1];
  }
}

function quatMul(a, b) {
  // Hamilton product a * b (xyzw)
  const [ax, ay, az, aw] = a;
  const [bx, by, bz, bw] = b;
  return [
    aw * bx + ax * bw + ay * bz - az * by,
    aw * by - ax * bz + ay * bw + az * bx,
    aw * bz + ax * by - ay * bx + az * bw,
    aw * bw - ax * bx - ay * by - az * bz,
  ];
}

function normalizeQuat(q) {
  const len = Math.hypot(q[0], q[1], q[2], q[3]) || 1;
  return [q[0] / len, q[1] / len, q[2] / len, q[3] / len];
}

// Apply rotation channels in their declared order. BVH composes rotations as
// the product of per-axis rotations in channel order (intrinsic).
function eulerChannelsToQuat(rotChannels, rotValues) {
  let q = [0, 0, 0, 1];
  for (let i = 0; i < rotChannels.length; i++) {
    const axis = rotChannels[i][0]; // "Xrotation" -> "X"
    q = quatMul(q, axisQuat(axis, rotValues[i]));
  }
  return normalizeQuat(q);
}

// ---------------------------------------------------------------------------
// Build per-joint rotation tracks
// ---------------------------------------------------------------------------

function buildTracks(parsed) {
  const { channelJoints, frameCount, frameTime, frames } = parsed;

  // Precompute, for each channel joint, the column offset of its rotation
  // channels within a frame row and the rotation axis order.
  let col = 0;
  const layout = channelJoints.map((j) => {
    const start = col;
    col += j.channels.length;
    const rotChannels = [];
    const rotCols = [];
    j.channels.forEach((ch, idx) => {
      if (/rotation$/i.test(ch)) {
        rotChannels.push(ch);
        rotCols.push(start + idx);
      }
    });
    return { joint: j, rotChannels, rotCols };
  });

  // times accessor (shared by all samplers)
  const times = new Float32Array(frameCount);
  for (let f = 0; f < frameCount; f++) times[f] = f * frameTime;

  // For each joint that has rotation channels, produce a quaternion track.
  const tracks = [];
  for (const entry of layout) {
    if (entry.rotChannels.length === 0) continue;
    const quats = new Float32Array(frameCount * 4);
    for (let f = 0; f < frameCount; f++) {
      const row = frames[f];
      const rotValues = entry.rotCols.map((c) => row[c]);
      const q = eulerChannelsToQuat(entry.rotChannels, rotValues);
      quats[f * 4 + 0] = q[0];
      quats[f * 4 + 1] = q[1];
      quats[f * 4 + 2] = q[2];
      quats[f * 4 + 3] = q[3];
    }
    tracks.push({ jointName: entry.joint.name, quats });
  }

  return { times, tracks, duration: frameCount > 0 ? (frameCount - 1) * frameTime : 0 };
}

// ---------------------------------------------------------------------------
// glTF / VRMA assembly
// ---------------------------------------------------------------------------

function alignTo4(n) {
  return (n + 3) & ~3;
}

function buildVrma(parsed, overrides, clipName) {
  const { times, tracks, duration } = buildTracks(parsed);

  // Map joints -> VRM humanoid bones. We only keep tracks whose joint resolves
  // to a humanoid bone (VRMA is a humanoid clip; non-humanoid joints are
  // dropped, matching the FBX converter's behaviour).
  const humanBones = {};
  const keptTracks = [];

  // We build a minimal node graph: one glTF node per kept track, named after
  // the VRM bone. The humanoid map references these node indices.
  let nodeIndex = 0;
  const nodes = [];

  for (const track of tracks) {
    const vrmBone = jointToVrmBone(track.jointName, overrides);
    if (!vrmBone || humanBones[vrmBone]) continue; // skip unmapped / duplicate
    const idx = nodeIndex++;
    nodes.push({ name: vrmBone });
    humanBones[vrmBone] = { node: idx };
    keptTracks.push({ ...track, node: idx });
  }

  const mappedCount = Object.keys(humanBones).length;
  if (mappedCount < HUMANOID_BONE_THRESHOLD) {
    throw new Error(
      `BVH does not look humanoid: only ${mappedCount} VRM bones mapped (need >= ${HUMANOID_BONE_THRESHOLD}). ` +
      `Pass --map overrides.json to map this skeleton.`
    );
  }

  // ---- Binary buffer: times accessor first, then one rotation accessor per track.
  const chunks = [];
  let byteOffset = 0;
  const bufferViews = [];
  const accessors = [];

  function pushAccessor(typedArray, type, componentCount, extraMinMax) {
    const buf = Buffer.from(typedArray.buffer, typedArray.byteOffset, typedArray.byteLength);
    const padded = alignTo4(buf.length);
    const view = {
      buffer: 0,
      byteOffset,
      byteLength: buf.length,
    };
    bufferViews.push(view);
    const bvIndex = bufferViews.length - 1;
    chunks.push(buf);
    if (padded > buf.length) chunks.push(Buffer.alloc(padded - buf.length));
    byteOffset += padded;

    const accessor = {
      bufferView: bvIndex,
      componentType: 5126, // FLOAT
      count: typedArray.length / componentCount,
      type,
    };
    if (extraMinMax) {
      accessor.min = extraMinMax.min;
      accessor.max = extraMinMax.max;
    }
    accessors.push(accessor);
    return accessors.length - 1;
  }

  // times accessor needs min/max for spec compliance
  let tMin = times.length ? times[0] : 0;
  let tMax = times.length ? times[times.length - 1] : 0;
  const timeAccessor = pushAccessor(times, "SCALAR", 1, { min: [tMin], max: [tMax] });

  const animSamplers = [];
  const animChannels = [];
  for (const track of keptTracks) {
    const rotAccessor = pushAccessor(track.quats, "VEC4", 4);
    const samplerIndex = animSamplers.length;
    animSamplers.push({ input: timeAccessor, output: rotAccessor, interpolation: "LINEAR" });
    animChannels.push({
      sampler: samplerIndex,
      target: { node: track.node, path: "rotation" },
    });
  }

  const bin = Buffer.concat(chunks);
  const dataUri = "data:application/octet-stream;base64," + bin.toString("base64");

  const gltf = {
    asset: { version: "2.0", generator: "bvh2vrma-converter" },
    extensionsUsed: ["VRMC_vrm_animation"],
    buffers: [{ byteLength: bin.length, uri: dataUri }],
    bufferViews,
    accessors,
    nodes,
    animations: [
      {
        name: clipName || "clip",
        samplers: animSamplers,
        channels: animChannels,
      },
    ],
    extensions: {
      VRMC_vrm_animation: {
        specVersion: "1.0",
        humanoid: { humanBones },
        meta: { duration },
      },
    },
  };

  return { gltf, mappedCount, duration, frameCount: parsed.frameCount };
}

// ---------------------------------------------------------------------------
// CLI
// ---------------------------------------------------------------------------

async function main() {
  const program = new Command()
    .requiredOption("-i, --input <path>", "Input BVH file")
    .requiredOption("-o, --output <path>", "Output VRMA file")
    .option("--map <path>", "JSON overrides: { jointName: vrmBoneName | null }")
    .option("--name <name>", "Animation clip name")
    .parse();

  const options = program.opts();

  const text = await fs.readFile(options.input, "utf8");
  let overrides = null;
  if (options.map) {
    const raw = await fs.readJson(options.map);
    // Accept either { overrides: {...} } or a flat map.
    overrides = raw.overrides || raw;
  }

  const parsed = parseBvh(text);
  const clipName = options.name || path.basename(options.output, path.extname(options.output));
  const { gltf, mappedCount, duration, frameCount } = buildVrma(parsed, overrides, clipName);

  await fs.writeJson(options.output, gltf);
  process.stderr.write(
    `bvh2vrma: ${mappedCount} bones, ${frameCount} frames, ${duration.toFixed(2)}s -> ${options.output}\n`
  );
}

if (require.main === module) {
  main().catch((error) => {
    console.error(error.message || error);
    process.exit(1);
  });
}

module.exports = { parseBvh, buildTracks, buildVrma, eulerChannelsToQuat, quatMul };
