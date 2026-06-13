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

// `markers` is a map of world-space THREE.Vector3 keyed by the 2D rigger:
//   groin, chest, neck, chin,
//   shoulderL/R, elbowL/R, wristL/R, hipL/R, kneeL/R, ankleL/R, toeL/R.
// Older sparse marker sets are still accepted and missing joints are inferred.
// `bbox` is a THREE.Box3 of the whole model. Returns { bones, skeleton,
// hipsBone, boneWorld } where boneWorld is a Map<name, Vector3> of rest world
// positions and crotchY (= groin.y) is attached to the result.
// `facing` is +1 if the character faces +Z, -1 if it faces -Z (the side the rig
// camera sat on). It only affects which way the toes point; left/right is taken
// from each marker's own side, so the rig is correct regardless of facing.
export function buildSkeletonFromMarkers(markers, bbox, facing = -1, directions = {}) {
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
  const marker = (key, fallback) => markers[key] ? markers[key].clone() : fallback.clone();

  const crotchY = groin.y;
  const topY = bbox.max.y;
  const floorY = bbox.min.y;
  const height = topY - floorY;
  const centerX = (chin.x + groin.x) / 2;
  const midZ = (chin.z + groin.z) / 2;
  const kneePoleSign = directions.knees === -1 ? -1 : 1;
  const elbowPoleSign = directions.elbows === 1 ? 1 : -1;
  const forward = new THREE.Vector3(0, 0, facing || -1);
  const kneePole = forward.clone().multiplyScalar(kneePoleSign);
  const elbowPole = forward.clone().multiplyScalar(elbowPoleSign);

  // Derived world positions for the full hierarchy.
  const world = {};
  world['Hips'] = groin.clone();

  // Spine chain up the centerline from hips to just below the chin (neck base).
  const neckBaseY = chin.y - (topY - chin.y) * 0.15; // a touch below the chin
  const spineBottom = V(centerX, groin.y, midZ);
  const spineTop = V(centerX, neckBaseY, midZ);
  const chest = marker('chest', lerp(spineBottom, spineTop, 0.70));
  const neck = marker('neck', V(centerX, neckBaseY, midZ));
  const head = chin.clone();
  const headFromNeck = head.clone().sub(neck);
  const minHeadRise = height * 0.045;
  const maxBackset = height * 0.025;
  // User neck/chin clicks are easy to place too close together in a 2D flow.
  // Keep the actual head joint mostly above the neck with only a small forward
  // component so VRMA head/neck motion does not fold through the face.
  if (headFromNeck.length() < height * 0.055 || headFromNeck.y < minHeadRise || headFromNeck.z * facing < -maxBackset) {
    head.x = chin.x;
    head.y = Math.max(chin.y, neck.y + minHeadRise);
    head.z = neck.z + Math.max(height * 0.012, Math.min(Math.abs(headFromNeck.z), height * 0.035)) * facing;
  }
  world['Spine'] = lerp(spineBottom, spineTop, 0.30);
  world['Spine1'] = lerp(spineBottom, chest, 0.68);
  world['Spine2'] = chest.clone();
  world['Neck'] = neck.clone();
  world['Head'] = head.clone();
  world['HeadTop_End'] = V(head.x, topY, head.z);

  // Shoulder sockets: at upper-chest height, offset laterally toward each elbow.
  // IMPORTANT: derive the lateral direction from the MARKER's own side
  // (sign of elbow.x - centerX), NOT a fixed +X==left assumption. This keeps
  // the skeleton's left/right tied to the markers the user labeled L/R,
  // regardless of which way the model faces (±Z) -- so a back-facing or
  // flipped model still rigs correctly.
  const shoulderY = lerp(world['Spine2'], world['Neck'], 0.5).y;
  const shoulderHalf = Math.abs(elbowL.x - elbowR.x) / 2 * 0.45; // narrower than elbows
  const leftArmSign = Math.sign(elbowL.x - centerX) || 1;        // +1 or -1
  const rightArmSign = Math.sign(elbowR.x - centerX) || -1;
  world['LeftShoulder'] = V(centerX + leftArmSign * shoulderHalf * 0.4, shoulderY, midZ);
  world['LeftArm'] = marker('shoulderL', V(centerX + leftArmSign * shoulderHalf, shoulderY, midZ));
  world['LeftForeArm'] = elbowL.clone();
  world['LeftHand'] = wristL.clone();
  world['RightShoulder'] = V(centerX + rightArmSign * shoulderHalf * 0.4, shoulderY, midZ);
  world['RightArm'] = marker('shoulderR', V(centerX + rightArmSign * shoulderHalf, shoulderY, midZ));
  world['RightForeArm'] = elbowR.clone();
  world['RightHand'] = wristR.clone();

  // Hand end caps (extend along the forearm direction).
  world['LeftHand_End'] = wristL.clone().add(wristL.clone().sub(elbowL).normalize().multiplyScalar(0.08 * (topY - floorY)));
  world['RightHand_End'] = wristR.clone().add(wristR.clone().sub(elbowR).normalize().multiplyScalar(0.08 * (topY - floorY)));

  // Hip sockets: laterally offset from Hips, each toward its own knee's side
  // (marker-relative sign, facing-agnostic like the arms above).
  const hipHalf = Math.abs(kneeL.x - kneeR.x) / 2 * 0.85;
  const leftLegSign = Math.sign(kneeL.x - centerX) || 1;
  const rightLegSign = Math.sign(kneeR.x - centerX) || -1;
  // Toes point "forward" = the direction the character faces. `facing` is the
  // side the rig camera sat on (-1 => faces -Z, +1 => faces +Z), so forward Z
  // = facing. footFwd is applied toward +Z*facing.
  const footFwd = 0.10 * height * facing;
  world['LeftUpLeg'] = marker('hipL', V(centerX + leftLegSign * hipHalf, groin.y, midZ));
  world['LeftLeg'] = kneeL.clone();
  world['LeftFoot'] = marker('ankleL', V(kneeL.x, floorY + height * 0.04, kneeL.z));
  world['LeftToeBase'] = marker('toeL', V(world['LeftFoot'].x, floorY + height * 0.01, world['LeftFoot'].z + footFwd));
  world['LeftToe_End'] = world['LeftToeBase'].clone().add(forward.clone().multiplyScalar(Math.max(Math.abs(footFwd) * 0.6, height * 0.04)));
  world['RightUpLeg'] = marker('hipR', V(centerX + rightLegSign * hipHalf, groin.y, midZ));
  world['RightLeg'] = kneeR.clone();
  world['RightFoot'] = marker('ankleR', V(kneeR.x, floorY + height * 0.04, kneeR.z));
  world['RightToeBase'] = marker('toeR', V(world['RightFoot'].x, floorY + height * 0.01, world['RightFoot'].z + footFwd));
  world['RightToe_End'] = world['RightToeBase'].clone().add(forward.clone().multiplyScalar(Math.max(Math.abs(footFwd) * 0.6, height * 0.04)));

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

  // Parent the bones, then assign LOCAL transforms from desired world-space
  // joint positions. Keep core humanoid rest rotations identity: VRM/VRMA
  // retargeting expects a T-pose-ish humanoid frame where arms use X as the
  // lateral axis, not a DCC-style bone frame where local Y points down each
  // limb. Rolling the arms toward their child positions makes VRMA elbow bends
  // look flat even when the joint positions are correct.
  for (const name of order) {
    const children = tree[name] || [];
    for (const childName of children) {
      boneByName[name].add(boneByName[childName]);
    }
  }

  function safePole(dir, primary) {
    const projected = dir.clone().sub(primary.clone().multiplyScalar(dir.dot(primary)));
    if (projected.lengthSq() > 1e-8) return projected.normalize();
    const fallback = Math.abs(primary.y) < 0.85 ? new THREE.Vector3(0, 1, 0) : new THREE.Vector3(1, 0, 0);
    return fallback.sub(primary.clone().multiplyScalar(fallback.dot(primary))).normalize();
  }

  function poleFor(name) {
    if (/Leg|UpLeg/.test(name)) return kneePole;
    if (/Arm|ForeArm|Hand/.test(name)) return elbowPole;
    if (/Foot|Toe/.test(name)) return new THREE.Vector3(0, 1, 0);
    return forward;
  }

  function jointQuaternion(name) {
    if (/^(Hips|Spine|Spine1|Spine2|Neck|Head|LeftShoulder|LeftArm|LeftForeArm|LeftHand|RightShoulder|RightArm|RightForeArm|RightHand|LeftUpLeg|LeftLeg|LeftFoot|LeftToeBase|RightUpLeg|RightLeg|RightFoot|RightToeBase)$/.test(name)) {
      return new THREE.Quaternion();
    }
    const children = tree[name] || [];
    const firstChild = children.find((childName) => world[childName]);
    let primary;
    if (firstChild) primary = world[firstChild].clone().sub(world[name]);
    else {
      const parentName = order.find((p) => (tree[p] || []).includes(name));
      primary = parentName ? world[name].clone().sub(world[parentName]) : new THREE.Vector3(0, 1, 0);
    }
    if (primary.lengthSq() < 1e-8) primary.set(0, 1, 0);
    const yAxis = primary.normalize();
    const zAxis = safePole(poleFor(name), yAxis);
    const xAxis = new THREE.Vector3().crossVectors(yAxis, zAxis).normalize();
    zAxis.crossVectors(xAxis, yAxis).normalize();
    const m = new THREE.Matrix4().makeBasis(xAxis, yAxis, zAxis);
    return new THREE.Quaternion().setFromRotationMatrix(m);
  }

  const desiredWorld = {};
  const unitScale = new THREE.Vector3(1, 1, 1);
  for (const name of order) {
    const m = new THREE.Matrix4();
    m.compose(world[name], jointQuaternion(name), unitScale);
    desiredWorld[name] = m;
  }

  for (const name of order) {
    const parentName = order.find((p) => (tree[p] || []).includes(name));
    let local = desiredWorld[name].clone();
    if (parentName) {
      local = desiredWorld[parentName].clone().invert().multiply(local);
    }
    local.decompose(boneByName[name].position, boneByName[name].quaternion, boneByName[name].scale);
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
  const name = bone.name.toLowerCase();
  return /_end$/.test(name) ||
    /headtop/.test(name) ||
    name.startsWith('ik') ||
    name.includes('poletarget') ||
    name.startsWith('pole') ||
    /^ff[blr]{0,2}$/.test(name);
}

function boneCategory(bone) {
  const name = bone.name.toLowerCase();
  const extremity = [
    'hand', 'foot', 'toe', 'ball',
    'thumb', 'index', 'middle', 'ring', 'pinky', 'finger',
    'eye', 'tongue', 'wing', 'feather',
  ];
  if (extremity.some((kw) => name.includes(kw))) return 'extremity';

  const limb = [
    'arm', 'upperarm', 'lowerarm', 'forearm', 'elbow', 'wrist',
    'shoulder', 'clavicle', 'ankle', 'fin',
    'thigh', 'calf', 'shin', 'knee', 'leg', 'upleg', 'lowleg',
  ];
  if (limb.some((kw) => name.includes(kw))) return 'limb';

  const torso = [
    'spine', 'chest', 'hips', 'pelvis', 'neck', 'torso', 'abdomen', 'body',
    'tail', 'head', 'mouth', 'stomach', 'chin', 'teeth',
  ];
  if (torso.some((kw) => name.includes(kw))) return 'torso';

  return 'other';
}

function isTorsoBoundary(bones, a, b) {
  const ca = boneCategory(bones[a]);
  const cb = boneCategory(bones[b]);
  return (ca === 'torso' || cb === 'torso') && ca !== 'extremity' && cb !== 'extremity';
}

function isLimbBoundary(bones, a, b) {
  return boneCategory(bones[a]) === 'limb' || boneCategory(bones[b]) === 'limb';
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
function buildPositionMap(geometry) {
  const count = geometry.attributes.position.count;
  const pos = geometry.attributes.position;
  const map = new Map();
  for (let i = 0; i < count; i++) {
    const key = `${pos.getX(i).toFixed(6)},${pos.getY(i).toFixed(6)},${pos.getZ(i).toFixed(6)}`;
    if (!map.has(key)) map.set(key, []);
    map.get(key).push(i);
  }
  return map;
}

function sharedVertices(geometry, vertex, positionMap) {
  const pos = geometry.attributes.position;
  const key = `${pos.getX(vertex).toFixed(6)},${pos.getY(vertex).toFixed(6)},${pos.getZ(vertex).toFixed(6)}`;
  return positionMap.get(key) || [vertex];
}

function setBlend(geometry, skinIndices, skinWeights, positionMap, vertex, primary, secondary, secondaryWeight) {
  const primaryWeight = 1.0 - secondaryWeight;
  for (const idx of sharedVertices(geometry, vertex, positionMap)) {
    const o = idx * 4;
    skinIndices[o] = primary;
    skinIndices[o + 1] = secondary;
    skinIndices[o + 2] = 0;
    skinIndices[o + 3] = 0;
    skinWeights[o] = primaryWeight;
    skinWeights[o + 1] = secondaryWeight;
    skinWeights[o + 2] = 0;
    skinWeights[o + 3] = 0;
  }
}

function isParentOf(bones, parentIndex, childIndex) {
  const parent = bones[parentIndex];
  let child = bones[childIndex];
  while (child && child.parent && child.parent.isBone) {
    if (child.parent === parent) return true;
    child = child.parent;
  }
  return false;
}

function findBoundaryPairs(geometry, skinIndices, skinWeights, bones, adjacency, crotchY, hipsIdx) {
  const count = geometry.attributes.position.count;
  const pos = geometry.attributes.position;
  const pairs = [];
  const visited = new Set();
  const v = new THREE.Vector3();

  for (let i = 0; i < count; i++) {
    const oi = i * 4;
    const boneA = skinIndices[oi];
    if (skinWeights[oi] !== 1.0) continue;

    for (const j of adjacency[i]) {
      const oj = j * 4;
      const boneB = skinIndices[oj];
      if (boneA === boneB || skinWeights[oj] !== 1.0) continue;

      v.fromBufferAttribute(pos, i);
      const aDisallowedHip = crotchY !== undefined && boneB === hipsIdx && v.y < crotchY;
      v.fromBufferAttribute(pos, j);
      const bDisallowedHip = crotchY !== undefined && boneA === hipsIdx && v.y < crotchY;
      if (aDisallowedHip || bDisallowedHip) continue;

      const key = i < j ? `${i},${j}` : `${j},${i}`;
      if (visited.has(key)) continue;
      visited.add(key);

      let smoothingType = 'standard';
      if (isTorsoBoundary(bones, boneA, boneB)) smoothingType = 'torso';
      else if (isLimbBoundary(bones, boneA, boneB)) smoothingType = 'limb';

      pairs.push({ vertexA: i, vertexB: j, boneA, boneB, smoothingType });
    }
  }
  return pairs;
}

export function smoothBoneWeightBoundaries(geometry, skinIndices, skinWeights, bones, midpoints, worldMatrix, crotchY) {
  const adjacency = buildAdjacency(geometry);
  const positionMap = buildPositionMap(geometry);
  const hipsIdx = bones.findIndex((b) => /:Hips$/.test(b.name));
  const pairs = findBoundaryPairs(geometry, skinIndices, skinWeights, bones, adjacency, crotchY, hipsIdx);

  for (const pair of pairs.filter((p) => p.smoothingType === 'torso')) {
    setBlend(geometry, skinIndices, skinWeights, positionMap, pair.vertexA, pair.boneA, pair.boneB, 0.5);
    setBlend(geometry, skinIndices, skinWeights, positionMap, pair.vertexB, pair.boneB, pair.boneA, 0.5);
  }

  const processed = new Set();
  for (const pair of pairs.filter((p) => p.smoothingType === 'torso')) {
    processed.add(pair.vertexA);
    processed.add(pair.vertexB);
  }
  let currentRing = new Set(processed);
  for (const secondaryWeight of [0.25, 0.10]) {
    const nextRing = new Set();
    for (const vertex of currentRing) {
      const primary = skinIndices[vertex * 4];
      const secondary = skinIndices[vertex * 4 + 1];
      if (skinWeights[vertex * 4 + 1] <= 0 || secondary === primary) continue;
      for (const neighbor of adjacency[vertex]) {
        if (processed.has(neighbor)) continue;
        if (skinIndices[neighbor * 4] !== primary || skinWeights[neighbor * 4] !== 1.0) continue;
        setBlend(geometry, skinIndices, skinWeights, positionMap, neighbor, primary, secondary, secondaryWeight);
        processed.add(neighbor);
        nextRing.add(neighbor);
      }
    }
    currentRing = nextRing;
  }

  for (const pair of pairs.filter((p) => p.smoothingType === 'limb')) {
    const aParent = isParentOf(bones, pair.boneA, pair.boneB);
    const bParent = isParentOf(bones, pair.boneB, pair.boneA);
    if (aParent) {
      setBlend(geometry, skinIndices, skinWeights, positionMap, pair.vertexB, pair.boneB, pair.boneA, 0.5);
    } else if (bParent) {
      setBlend(geometry, skinIndices, skinWeights, positionMap, pair.vertexA, pair.boneA, pair.boneB, 0.5);
    } else {
      setBlend(geometry, skinIndices, skinWeights, positionMap, pair.vertexA, pair.boneA, pair.boneB, 0.5);
      setBlend(geometry, skinIndices, skinWeights, positionMap, pair.vertexB, pair.boneB, pair.boneA, 0.5);
    }
  }

  for (const pair of pairs.filter((p) => p.smoothingType === 'standard')) {
    setBlend(geometry, skinIndices, skinWeights, positionMap, pair.vertexA, pair.boneA, pair.boneB, 0.5);
    setBlend(geometry, skinIndices, skinWeights, positionMap, pair.vertexB, pair.boneB, pair.boneA, 0.5);
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
export function rigModel(modelRoot, markers, bbox, facing = -1, options = {}) {
  const { bones, skeleton, hipsBone, crotchY } = buildSkeletonFromMarkers(
    markers,
    bbox,
    facing,
    options.directions || {}
  );
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
