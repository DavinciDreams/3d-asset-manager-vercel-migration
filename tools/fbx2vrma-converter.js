#!/usr/bin/env node

const fs = require("fs-extra");
const path = require("path");
const { execFileSync } = require("child_process");
const { Command } = require("commander");

const MIXAMO_TO_VRM = {
  "mixamorig:Hips": "hips",
  "mixamorig:Spine": "spine",
  "mixamorig:Spine1": "chest",
  "mixamorig:Spine2": "upperChest",
  "mixamorig:Neck": "neck",
  "mixamorig:Head": "head",
  "mixamorig:LeftShoulder": "leftShoulder",
  "mixamorig:LeftArm": "leftUpperArm",
  "mixamorig:LeftForeArm": "leftLowerArm",
  "mixamorig:LeftHand": "leftHand",
  "mixamorig:RightShoulder": "rightShoulder",
  "mixamorig:RightArm": "rightUpperArm",
  "mixamorig:RightForeArm": "rightLowerArm",
  "mixamorig:RightHand": "rightHand",
  "mixamorig:LeftUpLeg": "leftUpperLeg",
  "mixamorig:LeftLeg": "leftLowerLeg",
  "mixamorig:LeftFoot": "leftFoot",
  "mixamorig:RightUpLeg": "rightUpperLeg",
  "mixamorig:RightLeg": "rightLowerLeg",
  "mixamorig:RightFoot": "rightFoot"
};

function convertFbxToGltf(inputPath, outputPath, fbx2gltfPath) {
  const outputDir = path.dirname(outputPath);
  const outputBase = path.join(outputDir, path.basename(outputPath, ".gltf"));
  execFileSync(fbx2gltfPath, ["-i", inputPath, "-o", outputBase, "--embed"], { stdio: "pipe" });

  const nested = path.join(outputDir, `${path.basename(outputBase)}_out`, `${path.basename(outputBase)}.gltf`);
  if (fs.existsSync(nested)) {
    fs.moveSync(nested, outputPath, { overwrite: true });
    fs.removeSync(path.dirname(nested));
  } else if (!fs.existsSync(outputPath)) {
    throw new Error("FBX2glTF produced no glTF output");
  }
}

function humanBones(gltf) {
  const bones = {};
  (gltf.nodes || []).forEach((node, index) => {
    if (node.name && MIXAMO_TO_VRM[node.name]) {
      bones[MIXAMO_TO_VRM[node.name]] = { node: index };
    }
  });
  return bones;
}

function animationDuration(gltf) {
  let duration = 0;
  for (const animation of gltf.animations || []) {
    for (const sampler of animation.samplers || []) {
      const accessor = gltf.accessors && gltf.accessors[sampler.input];
      if (accessor && accessor.max && accessor.max.length) {
        duration = Math.max(duration, Number(accessor.max[0]) || 0);
      }
    }
  }
  return duration;
}

async function main() {
  const program = new Command()
    .requiredOption("-i, --input <path>", "Input FBX file")
    .requiredOption("-o, --output <path>", "Output VRMA file")
    .option("--fbx2gltf <path>", "FBX2glTF binary", "/usr/local/bin/FBX2glTF")
    .parse();

  const options = program.opts();
  const tempGltf = path.join(path.dirname(options.output), "temp-vrma-source.gltf");
  convertFbxToGltf(options.input, tempGltf, options.fbx2gltf);
  const gltf = await fs.readJson(tempGltf);
  const duration = animationDuration(gltf);

  gltf.extensionsUsed = Array.from(new Set([...(gltf.extensionsUsed || []), "VRMC_vrm_animation"]));
  gltf.extensions = {
    ...(gltf.extensions || {}),
    VRMC_vrm_animation: {
      specVersion: "1.0",
      humanoid: { humanBones: humanBones(gltf) },
      meta: { duration }
    }
  };

  await fs.writeJson(options.output, gltf);
  await fs.remove(tempGltf);
}

main().catch((error) => {
  console.error(error.message || error);
  process.exit(1);
});
