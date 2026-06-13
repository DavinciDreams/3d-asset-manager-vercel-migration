// autorig-skinning.js — client-side humanoid auto-rigging for the 3D asset
// manager. Builds a mixamorig:* skeleton from a few placed markers, then skins
// the mesh using a nearest-bone (midpoint-to-child) + boundary-smooth +
// normalize pipeline ported (simplified) from DavinciDreams/mesh2motion-app
// (MIT). Pure Three.js, runs in the browser — the server never touches the mesh.
//
// The bone names must stay in sync with tools/vrm-bone-map.js so the produced
// GLB converts to a VRM (glb2vrm) and plays VRMA clips.

import * as THREE from 'three';

const MAX_INFLUENCES = 4;

// ---------------------------------------------------------------------------
// Skeleton construction
// ---------------------------------------------------------------------------

// `markers` is a map of world-space THREE.Vector3 keyed by:
//   chin, groin, wristL, wristR, elbowL, elbowR, kneeL, kneeR
// `bbox` is a THREE.Box3 of the whole model. Returns { bones, skeleton,
// hipsBone, boneWorld } where boneWorld is a Map<name, Vector3> of rest world
// positions and crotchY (= groin.y) is attached to the result.
export function buildSkeletonFromMarkers(markers, bbox) {
  const need = ['chin', 'groin', 'wristL', 'wristR', 'elbowL', 'elbowR', 'kneeL', 'kneeR'];
  for (const k of need) {
    if (!markers[k]) throw new Error(`autorig: missing marker "${k}"`);
  }

  const V = (x, y, z) => new THREE.Vector3(x, y, z);
  const lerp = (a, b, t) => a.clone().lerp(b, t);

  const chin = markers.chin.clone();
  const groin = markers.groin.clone();
  const wristL = markers.wristL.clone(), wristR = markers.wristR.clone();
  const elbowL = markers.elbowL.clone(), elbowR = markers.elbowR.clone();
  const kneeL = markers.kneeL.clone(), kneeR = markers.kneeR.clone();

  const crotchY = groin.y;
  const topY = bbox.max.y;
  const floorY = bbox.min.y;
  const centerX = (chin.x + groin.x) / 2;
  const midZ = (chin.z + groin.z) / 2;

  // Derived world positions for the full hierarchy.
  const world = {};
  world['Hips'] = groin.clone();

  // Spine chain up the centerline from hips to just below the chin (neck base).
  const neckBaseY = chin.y - (topY - chin.y) * 0.15; // a touch below the chin
  const spineBottom = V(centerX, groin.y, midZ);
  const spineTop = V(centerX, neckBaseY, midZ);
  world['Spine'] = lerp(spineBottom, spineTop, 0.30);
  world['Spine1'] = lerp(spineBottom, spineTop, 0.58);
  world['Spine2'] = lerp(spineBottom, spineTop, 0.82);
  world['Neck'] = V(centerX, neckBaseY, midZ);
  world['Head'] = chin.clone();
  world['HeadTop_End'] = V(chin.x, topY, chin.z);

  // Shoulder sockets: at upper-chest height, offset laterally toward each elbow.
  const shoulderY = lerp(world['Spine2'], world['Neck'], 0.5).y;
  const shoulderHalf = Math.abs(elbowL.x - elbowR.x) / 2 * 0.45; // narrower than elbows
  world['LeftShoulder'] = V(centerX + shoulderHalf * 0.4, shoulderY, midZ);
  world['LeftArm'] = V(centerX + shoulderHalf, shoulderY, midZ);
  world['LeftForeArm'] = elbowL.clone();
  world['LeftHand'] = wristL.clone();
  world['RightShoulder'] = V(centerX - shoulderHalf * 0.4, shoulderY, midZ);
  world['RightArm'] = V(centerX - shoulderHalf, shoulderY, midZ);
  world['RightForeArm'] = elbowR.clone();
  world['RightHand'] = wristR.clone();

  // Hand end caps (extend along the forearm direction).
  world['LeftHand_End'] = wristL.clone().add(wristL.clone().sub(elbowL).normalize().multiplyScalar(0.08 * (topY - floorY)));
  world['RightHand_End'] = wristR.clone().add(wristR.clone().sub(elbowR).normalize().multiplyScalar(0.08 * (topY - floorY)));

  // Hip sockets: laterally offset from Hips toward each knee's X.
  const hipHalf = Math.abs(kneeL.x - kneeR.x) / 2 * 0.85;
  world['LeftUpLeg'] = V(centerX + hipHalf, groin.y, midZ);
  world['LeftLeg'] = kneeL.clone();
  world['LeftFoot'] = V(kneeL.x, floorY + (topY - floorY) * 0.04, kneeL.z);
  const footFwd = 0.10 * (topY - floorY);
  world['LeftToeBase'] = V(kneeL.x, floorY + (topY - floorY) * 0.01, kneeL.z - footFwd);
  world['LeftToe_End'] = V(kneeL.x, floorY, kneeL.z - footFwd * 1.6);
  world['RightUpLeg'] = V(centerX - hipHalf, groin.y, midZ);
  world['RightLeg'] = kneeR.clone();
  world['RightFoot'] = V(kneeR.x, floorY + (topY - floorY) * 0.04, kneeR.z);
  world['RightToeBase'] = V(kneeR.x, floorY + (topY - floorY) * 0.01, kneeR.z - footFwd);
  world['RightToe_End'] = V(kneeR.x, floorY, kneeR.z - footFwd * 1.6);

  // Parent → children tree (names without the mixamorig: prefix here).
  const tree = {
    Hips: ['Spine', 'LeftUpLeg', 'RightUpLeg'],
    Spine: ['Spine1'], Spine1: ['Spine2'],
    Spine2: ['Neck', 'LeftShoulder', 'RightShoulder'],
    Neck: ['Head'], Head: ['HeadTop_End'],
    LeftShoulder: ['LeftArm'], LeftArm: ['LeftForeArm'], LeftForeArm: ['LeftHand'], LeftHand: ['LeftHand_End'],
    RightShoulder: ['RightArm'], RightArm: ['RightForeArm'], RightForeArm: ['RightHand'], RightHand: ['RightHand_End'],
    LeftUpLeg: ['LeftLeg'], LeftLeg: ['LeftFoot'], LeftFoot: ['LeftToeBase'], LeftToeBase: ['LeftToe_End'],
    RightUpLeg: ['RightLeg'], RightLeg: ['RightFoot'], RightFoot: ['RightToeBase'], RightToeBase: ['RightToe_End'],
  };

  // Build THREE.Bone objects in a deterministic order (depth-first from Hips).
  const bones = [];
  const boneByName = {};
  const order = [];
  (function dfs(name) {
    order.push(name);
    (tree[name] || []).forEach(dfs);
  })('Hips');

  for (const name of order) {
    const bone = new THREE.Bone();
    bone.name = 'mixamorig:' + name;
    boneByName[name] = bone;
    bones.push(bone);
  }

  // Parent the bones and set LOCAL positions (rest rotations are identity, so
  // local position = childWorld - parentWorld).
  for (const name of order) {
    const children = tree[name] || [];
    for (const childName of children) {
      boneByName[name].add(boneByName[childName]);
    }
  }
  for (const name of order) {
    const parentName = order.find((p) => (tree[p] || []).includes(name));
    const wp = world[name];
    if (parentName) {
      boneByName[name].position.copy(wp.clone().sub(world[parentName]));
    } else {
      boneByName[name].position.copy(wp); // Hips: world == local (root)
    }
  }

  boneByName['Hips'].updateMatrixWorld(true);
  const skeleton = new THREE.Skeleton(bones);

  const boneWorld = new Map();
  for (const name of order) {
    boneWorld.set('mixamorig:' + name, world[name].clone());
  }

  return { bones, skeleton, hipsBone: boneByName['Hips'], boneWorld, crotchY, order };
}

// ---------------------------------------------------------------------------
// Bone midpoints (mesh2motion midpoint_to_child)
// ---------------------------------------------------------------------------

// For each bone, the deform reference point is the midpoint between the bone
// and its first child. Leaf bones (no children, e.g. *_End) use their own pos.
// Bones whose names indicate they are pure end caps are excluded from weighting.
export function computeBoneMidpoints(bones) {
  const midpoints = [];
  for (const bone of bones) {
    const bw = bone.getWorldPosition(new THREE.Vector3());
    const child = bone.children.find((c) => c.isBone);
    if (child) {
      const cw = child.getWorldPosition(new THREE.Vector3());
      midpoints.push(bw.clone().lerp(cw, 0.5));
    } else {
      midpoints.push(bw);
    }
  }
  return midpoints;
}

// End-cap / non-deforming bones never receive vertices (keeps weights on real
// bones). Matches the spirit of mesh2motion's non-deforming-control-bone skip.
function isNonDeformingBone(bone) {
  return /_End$/.test(bone.name) || /HeadTop/.test(bone.name);
}

// ---------------------------------------------------------------------------
// Weight calculation (nearest midpoint, 1.0 to closest bone)
// ---------------------------------------------------------------------------

export function calculateMedianBoneWeights(geometry, bones, midpoints, crotchY, worldMatrix) {
  const pos = geometry.attributes.position;
  const count = pos.count;
  const skinIndices = new Uint16Array(count * 4);
  const skinWeights = new Float32Array(count * 4);

  const v = new THREE.Vector3();
  const hipsIdx = bones.findIndex((b) => /:Hips$/.test(b.name));

  for (let i = 0; i < count; i++) {
    v.fromBufferAttribute(pos, i);
    if (worldMatrix) v.applyMatrix4(worldMatrix);

    let best = Infinity;
    let bestBone = 0;
    for (let b = 0; b < bones.length; b++) {
      if (isNonDeformingBone(bones[b])) continue;
      // Hip rule: a vertex below the crotch belongs to a leg, never the hips,
      // even if the hips midpoint is nearest. Prevents legs grabbing the pelvis.
      if (b === hipsIdx && v.y < crotchY) continue;
      const d = midpoints[b].distanceToSquared(v);
      if (d < best) { best = d; bestBone = b; }
    }
    skinIndices[i * 4] = bestBone;
    skinWeights[i * 4] = 1.0;
  }
  return { skinIndices, skinWeights };
}

// ---------------------------------------------------------------------------
// Boundary smoothing (simplified standard pass from mesh2motion WeightSmoother)
// ---------------------------------------------------------------------------

function buildAdjacency(geometry) {
  const count = geometry.attributes.position.count;
  const adj = Array.from({ length: count }, () => new Set());
  const index = geometry.index;
  if (index) {
    const a = index.array;
    for (let t = 0; t < a.length; t += 3) {
      const i0 = a[t], i1 = a[t + 1], i2 = a[t + 2];
      adj[i0].add(i1); adj[i0].add(i2);
      adj[i1].add(i0); adj[i1].add(i2);
      adj[i2].add(i0); adj[i2].add(i1);
    }
  } else {
    // Non-indexed: triangles are consecutive triples.
    for (let t = 0; t + 2 < count; t += 3) {
      adj[t].add(t + 1); adj[t].add(t + 2);
      adj[t + 1].add(t); adj[t + 1].add(t + 2);
      adj[t + 2].add(t); adj[t + 2].add(t + 1);
    }
  }
  return adj;
}

// For each vertex sitting on a bone boundary (a neighbor bound to a different
// bone), blend in that neighbor bone by distance to the two bone midpoints.
// Single-ring 50/50-ish gradient — the "standard" path of the original.
export function smoothBoneWeightBoundaries(geometry, skinIndices, skinWeights, bones, midpoints, worldMatrix, crotchY) {
  const count = geometry.attributes.position.count;
  const adj = buildAdjacency(geometry);
  const pos = geometry.attributes.position;
  const v = new THREE.Vector3();
  const hipsIdx = bones.findIndex((b) => /:Hips$/.test(b.name));

  // Snapshot the pre-smoothing single-bone assignment.
  const baseBone = new Int32Array(count);
  for (let i = 0; i < count; i++) baseBone[i] = skinIndices[i * 4];

  for (let i = 0; i < count; i++) {
    const myBone = baseBone[i];
    // Collect distinct neighbor bones.
    const neighborBones = new Set();
    for (const j of adj[i]) {
      if (baseBone[j] !== myBone) neighborBones.add(baseBone[j]);
    }
    if (neighborBones.size === 0) continue;

    v.fromBufferAttribute(pos, i);
    if (worldMatrix) v.applyMatrix4(worldMatrix);

    // Honor the hip rule during smoothing too: a vertex below the crotch must
    // never blend back into Hips (otherwise the leg/pelvis seam re-grabs it).
    if (crotchY !== undefined && v.y < crotchY) neighborBones.delete(hipsIdx);
    if (neighborBones.size === 0) continue;

    // Build influence list: self bone + up to 3 nearest neighbor bones, weighted
    // by inverse distance to each bone midpoint.
    const candidates = [myBone, ...neighborBones];
    const weighted = candidates.map((b) => {
      const d = Math.sqrt(midpoints[b].distanceToSquared(v)) + 1e-6;
      return { bone: b, w: 1 / d };
    });
    weighted.sort((a, b) => b.w - a.w);
    const top = weighted.slice(0, MAX_INFLUENCES);
    const sum = top.reduce((s, c) => s + c.w, 0) || 1;
    for (let k = 0; k < MAX_INFLUENCES; k++) {
      if (k < top.length) {
        skinIndices[i * 4 + k] = top[k].bone;
        skinWeights[i * 4 + k] = top[k].w / sum;
      } else {
        skinIndices[i * 4 + k] = 0;
        skinWeights[i * 4 + k] = 0;
      }
    }
  }
}

// ---------------------------------------------------------------------------
// Normalize
// ---------------------------------------------------------------------------

export function normalizeWeights(skinWeights) {
  const count = skinWeights.length / 4;
  for (let i = 0; i < count; i++) {
    const o = i * 4;
    const sum = skinWeights[o] + skinWeights[o + 1] + skinWeights[o + 2] + skinWeights[o + 3];
    if (sum > 1e-8) {
      skinWeights[o] /= sum; skinWeights[o + 1] /= sum;
      skinWeights[o + 2] /= sum; skinWeights[o + 3] /= sum;
    } else {
      // Orphan: bind 100% to bone 0 as a fallback (never leave a vertex loose).
      skinWeights[o] = 1.0;
    }
  }
}

// ---------------------------------------------------------------------------
// Orchestration: skin one mesh
// ---------------------------------------------------------------------------

// Produces a bound THREE.SkinnedMesh from a plain Mesh. The skeleton's bones
// must already be in a scene graph with up-to-date world matrices. `crotchY`
// gates the hip rule. Tiny disconnected accessory meshes (< rigidThreshold
// verts) bind rigidly to their single nearest bone (skip smoothing).
export function skinMesh(mesh, skeleton, bones, midpoints, crotchY, rigidThreshold = 50) {
  const geometry = mesh.geometry.index ? mesh.geometry : mesh.geometry.toNonIndexed();
  const cloned = geometry.clone();
  mesh.updateMatrixWorld(true);
  const worldMatrix = mesh.matrixWorld.clone();

  const { skinIndices, skinWeights } = calculateMedianBoneWeights(
    cloned, bones, midpoints, crotchY, worldMatrix
  );

  if (cloned.attributes.position.count >= rigidThreshold) {
    smoothBoneWeightBoundaries(cloned, skinIndices, skinWeights, bones, midpoints, worldMatrix, crotchY);
  }
  normalizeWeights(skinWeights);

  cloned.setAttribute('skinIndex', new THREE.Uint16BufferAttribute(skinIndices, 4));
  cloned.setAttribute('skinWeight', new THREE.Float32BufferAttribute(skinWeights, 4));

  const skinned = new THREE.SkinnedMesh(cloned, mesh.material);
  // Bake the mesh's world transform into the geometry so the SkinnedMesh can sit
  // at the skeleton root (identity) without double-transforming.
  skinned.applyMatrix4(worldMatrix);
  skinned.matrixAutoUpdate = true;
  skinned.name = mesh.name || 'rigged_mesh';
  skinned.bind(skeleton);
  return skinned;
}

// Full pipeline helper: given the loaded model root + a markers map + bbox,
// returns { root, skeleton, skinnedMeshes } ready for GLTFExporter.
export function rigModel(modelRoot, markers, bbox) {
  const { bones, skeleton, hipsBone, crotchY } = buildSkeletonFromMarkers(markers, bbox);
  hipsBone.updateMatrixWorld(true);
  const midpoints = computeBoneMidpoints(bones);

  // Collect renderable meshes. Prefer plain (unrigged) meshes — the common
  // case. If the model is ALREADY skinned (re-rigging), fall back to its
  // SkinnedMeshes: we re-skin their geometry to the new skeleton (the old
  // skinIndex/skinWeight attributes are overwritten by skinMesh).
  const plain = [];
  const skinned = [];
  modelRoot.updateMatrixWorld(true);
  modelRoot.traverse((o) => {
    if (!o.isMesh) return;
    if (o.isSkinnedMesh) skinned.push(o); else plain.push(o);
  });
  const meshes = plain.length ? plain : skinned;
  if (meshes.length === 0) throw new Error('autorig: model has no meshes to skin');

  const root = new THREE.Group();
  root.name = 'rig_root';
  root.add(hipsBone);

  const skinnedMeshes = [];
  for (const mesh of meshes) {
    const skinned = skinMesh(mesh, skeleton, bones, midpoints, crotchY);
    root.add(skinned);
    skinnedMeshes.push(skinned);
  }
  root.updateMatrixWorld(true);
  return { root, skeleton, skinnedMeshes, bones };
}
