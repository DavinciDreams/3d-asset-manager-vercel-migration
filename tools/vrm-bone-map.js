"use strict";

// Shared VRM 1.0 humanoid bone vocabulary + skeleton name maps used by both
// the FBX and BVH VRMA converters. Keeping this in one place means a fix to the
// mapping (e.g. a new mocap source) helps both pipelines at once.

// The canonical Mixamo rig -> VRM humanoid bone names. Mixamo FBX exports use
// the "mixamorig:" prefix; some tools strip the colon ("mixamorigHips") or the
// whole prefix, so the matcher below normalizes before lookup.
const MIXAMO_TO_VRM = {
  hips: "hips",
  spine: "spine",
  spine1: "chest",
  spine2: "upperChest",
  neck: "neck",
  head: "head",
  leftshoulder: "leftShoulder",
  leftarm: "leftUpperArm",
  leftforearm: "leftLowerArm",
  lefthand: "leftHand",
  rightshoulder: "rightShoulder",
  rightarm: "rightUpperArm",
  rightforearm: "rightLowerArm",
  righthand: "rightHand",
  leftupleg: "leftUpperLeg",
  leftleg: "leftLowerLeg",
  leftfoot: "leftFoot",
  lefttoebase: "leftToes",
  rightupleg: "rightUpperLeg",
  rightleg: "rightLowerLeg",
  rightfoot: "rightFoot",
  righttoebase: "rightToes",
  // Common hand/finger bones (optional in VRM but harmless to map).
  lefthandthumb1: "leftThumbMetacarpal",
  lefthandthumb2: "leftThumbProximal",
  lefthandthumb3: "leftThumbDistal",
  lefthandindex1: "leftIndexProximal",
  lefthandindex2: "leftIndexIntermediate",
  lefthandindex3: "leftIndexDistal",
  lefthandmiddle1: "leftMiddleProximal",
  lefthandmiddle2: "leftMiddleIntermediate",
  lefthandmiddle3: "leftMiddleDistal",
  lefthandring1: "leftRingProximal",
  lefthandring2: "leftRingIntermediate",
  lefthandring3: "leftRingDistal",
  lefthandpinky1: "leftLittleProximal",
  lefthandpinky2: "leftLittleIntermediate",
  lefthandpinky3: "leftLittleDistal",
  righthandthumb1: "rightThumbMetacarpal",
  righthandthumb2: "rightThumbProximal",
  righthandthumb3: "rightThumbDistal",
  righthandindex1: "rightIndexProximal",
  righthandindex2: "rightIndexIntermediate",
  righthandindex3: "rightIndexDistal",
  righthandmiddle1: "rightMiddleProximal",
  righthandmiddle2: "rightMiddleIntermediate",
  righthandmiddle3: "rightMiddleDistal",
  righthandring1: "rightRingProximal",
  righthandring2: "rightRingIntermediate",
  righthandring3: "rightRingDistal",
  righthandpinky1: "rightLittleProximal",
  righthandpinky2: "rightLittleIntermediate",
  righthandpinky3: "rightLittleDistal",
};

// CMU / common BVH skeletons (e.g. the classic CMU mocap database, BioVision
// sample rigs) use different joint names. These alias maps normalize them onto
// the same Mixamo keys so a single matcher handles them all.
const ALIAS_TO_MIXAMO_KEY = {
  // Spine / head
  hip: "hips",
  pelvis: "hips",
  spine3: "spine2",
  chest: "spine1",
  chest2: "spine2",
  upperchest: "spine2",
  lowerback: "spine",
  abdomen: "spine",
  thorax: "spine2",
  neck1: "neck",
  // Left arm (CMU uses LeftCollar/LeftUpArm/LeftLowArm/LeftHand etc.)
  leftcollar: "leftshoulder",
  leftclavicle: "leftshoulder",
  leftuparm: "leftarm",
  leftuparm: "leftarm",
  leftlowarm: "leftforearm",
  lefthandindex: "lefthandindex1",
  // Right arm
  rightcollar: "rightshoulder",
  rightclavicle: "rightshoulder",
  rightuparm: "rightarm",
  rightupperarm: "rightarm",
  rightlowarm: "rightforearm",
  // Left leg (CMU uses LHipJoint/LeftUpLeg/LeftLeg/LeftFoot/LeftToeBase)
  lhipjoint: null, // CMU dummy joint with no VRM equivalent
  lefthip: "leftupleg",
  leftthigh: "leftupleg",
  leftshin: "leftleg",
  leftknee: "leftleg",
  leftankle: "leftfoot",
  lefttoe: "lefttoebase",
  // Right leg
  rhipjoint: null,
  righthip: "rightupleg",
  rightthigh: "rightupleg",
  rightshin: "rightleg",
  rightknee: "rightleg",
  rightankle: "rightfoot",
  righttoe: "righttoebase",
};

// Strip prefixes/colons/underscores and lowercase so "mixamorig:LeftArm",
// "mixamorigLeftArm", "LeftArm", and "Character1_LeftArm" all collapse to the
// same key.
function normalizeJointName(raw) {
  if (!raw) return "";
  let s = String(raw).toLowerCase();
  s = s.replace(/^mixamorig[:_]?/, "");
  s = s.replace(/^character\d*[:_]/, "");
  s = s.replace(/^bip\d*[:_ ]?/, ""); // 3ds Max Biped: "Bip001 L UpperArm"
  s = s.replace(/[:_\s]/g, "");
  return s;
}

// Resolve any joint name -> a VRM humanoid bone name (or null). `overrides` is
// an optional { jointName: vrmBoneName } map loaded from a sidecar JSON for
// custom skeletons; it takes priority and is matched on the raw name too.
function jointToVrmBone(rawName, overrides) {
  if (overrides) {
    if (Object.prototype.hasOwnProperty.call(overrides, rawName)) {
      return overrides[rawName] || null;
    }
  }
  const key = normalizeJointName(rawName);
  if (overrides && Object.prototype.hasOwnProperty.call(overrides, key)) {
    return overrides[key] || null;
  }
  if (Object.prototype.hasOwnProperty.call(MIXAMO_TO_VRM, key)) {
    return MIXAMO_TO_VRM[key];
  }
  if (Object.prototype.hasOwnProperty.call(ALIAS_TO_MIXAMO_KEY, key)) {
    const mixKey = ALIAS_TO_MIXAMO_KEY[key];
    return mixKey ? MIXAMO_TO_VRM[mixKey] || null : null;
  }
  return null;
}

// The minimum number of mapped humanoid bones for an animation to be considered
// a usable humanoid clip. Mirrors HUMANOID_BONE_THRESHOLD in conversion.py.
const HUMANOID_BONE_THRESHOLD = 6;

// The five bones VRM 1.0 requires every humanoid to define. A converted VRM is
// invalid without all of them, so glb2vrm refuses to emit if any are missing.
const VRM_REQUIRED_BONES = [
  "hips",
  "spine",
  "head",
  "leftUpperLeg",
  "rightUpperLeg",
  "leftLowerLeg",
  "rightLowerLeg",
  "leftFoot",
  "rightFoot",
  "leftUpperArm",
  "rightUpperArm",
  "leftLowerArm",
  "rightLowerArm",
  "leftHand",
  "rightHand",
];

module.exports = {
  MIXAMO_TO_VRM,
  ALIAS_TO_MIXAMO_KEY,
  normalizeJointName,
  jointToVrmBone,
  HUMANOID_BONE_THRESHOLD,
  VRM_REQUIRED_BONES,
};
