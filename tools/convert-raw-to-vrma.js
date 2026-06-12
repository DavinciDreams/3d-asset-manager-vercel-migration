#!/usr/bin/env node
"use strict";

// Batch-convert a folder of raw Mixamo clips (FBX and/or BVH) into VRMA files,
// building out the vendored animation library. Inspired by 3dchat's
// scripts/convert-raw-to-vrma.js, adapted to this repo's converters.
//
// Workflow:
//   1. Download FBX clips from mixamo.com (or via download-mixamo.js) into the
//      raw directory (default ./animations-raw).
//   2. node convert-raw-to-vrma.js --in animations-raw --out ../app/static/animations
//   3. Filenames are fuzzy-matched against animation-list.json so e.g.
//      "Hip Hop Dancing.fbx" -> hipHopDancing.vrma.
//
// FBX needs FBX2glTF (same binary the server uses); BVH is pure-JS. Failures on
// one clip are logged but don't abort the batch.

const fs = require("fs-extra");
const path = require("path");
const { execFileSync } = require("child_process");
const { Command } = require("commander");

const HERE = __dirname;

function loadAnimationList(listPath) {
  if (!fs.existsSync(listPath)) return [];
  try {
    const raw = fs.readJsonSync(listPath);
    return raw.animations || [];
  } catch (e) {
    console.warn(`Could not read ${listPath}: ${e.message}`);
    return [];
  }
}

// Normalize a name to a comparison key: lowercase alphanumerics only.
function key(s) {
  return String(s || "").toLowerCase().replace(/[^a-z0-9]/g, "");
}

// camelCase fallback for files not in the curated list.
function toCamelCase(stem) {
  const parts = stem.split(/[\s_\-]+/).filter(Boolean);
  if (parts.length === 0) return "animation";
  return parts
    .map((p, i) => (i === 0 ? p.toLowerCase() : p.charAt(0).toUpperCase() + p.slice(1).toLowerCase()))
    .join("");
}

// Resolve a raw filename stem -> output base name using the curated list.
function resolveName(stem, animations) {
  const k = key(stem);
  for (const anim of animations) {
    if (key(anim.mixamoName) === k || key(anim.name) === k) {
      return anim.name;
    }
  }
  // Also try a "starts with" match (Mixamo appends variant suffixes).
  for (const anim of animations) {
    const mk = key(anim.mixamoName);
    if (mk && (k.startsWith(mk) || mk.startsWith(k))) return anim.name;
  }
  return toCamelCase(stem);
}

function convertFbx(input, output, fbx2gltf) {
  execFileSync(
    process.execPath,
    [path.join(HERE, "fbx2vrma-converter.js"), "-i", input, "-o", output, "--fbx2gltf", fbx2gltf],
    { stdio: "pipe" }
  );
}

function convertBvh(input, output, name) {
  const args = [path.join(HERE, "bvh2vrma-converter.js"), "-i", input, "-o", output];
  if (name) args.push("--name", name);
  execFileSync(process.execPath, args, { stdio: "pipe" });
}

async function main() {
  const program = new Command()
    .option("--in <dir>", "Raw clips directory", path.join(process.cwd(), "animations-raw"))
    .option("--out <dir>", "Output VRMA directory", path.join(process.cwd(), "animations-vrma"))
    .option("--list <path>", "animation-list.json", path.join(HERE, "animation-list.json"))
    .option("--fbx2gltf <path>", "FBX2glTF binary", process.env.FBX2GLTF_BIN || "/usr/local/bin/FBX2glTF")
    .option("--overwrite", "Re-convert clips even if the .vrma already exists", false)
    .parse();

  const opts = program.opts();
  const inDir = path.resolve(opts.in);
  const outDir = path.resolve(opts.out);

  if (!fs.existsSync(inDir)) {
    fs.ensureDirSync(inDir);
    console.error(
      `Raw directory created: ${inDir}\n` +
      `Put Mixamo .fbx / .bvh clips here (download from mixamo.com or run download-mixamo.js), then re-run.`
    );
    process.exit(1);
  }
  fs.ensureDirSync(outDir);

  const animations = loadAnimationList(opts.list);
  const files = fs
    .readdirSync(inDir)
    .filter((f) => /\.(fbx|bvh)$/i.test(f));

  if (files.length === 0) {
    console.error(`No .fbx or .bvh files found in ${inDir}.`);
    process.exit(1);
  }

  let ok = 0;
  const failed = [];
  const manifest = [];

  for (const file of files) {
    const ext = path.extname(file).toLowerCase();
    const stem = path.basename(file, ext);
    const outName = resolveName(stem, animations);
    const input = path.join(inDir, file);
    const output = path.join(outDir, outName + ".vrma");

    if (fs.existsSync(output) && !opts.overwrite) {
      console.log(`skip (exists): ${outName}.vrma`);
      manifest.push({ name: outName, source: file });
      continue;
    }

    try {
      if (ext === ".fbx") {
        convertFbx(input, output, opts.fbx2gltf);
      } else {
        convertBvh(input, output, outName);
      }
      ok++;
      manifest.push({ name: outName, source: file });
      const meta = animations.find((a) => a.name === outName);
      console.log(`ok: ${file} -> ${outName}.vrma${meta ? "  [" + meta.category + "]" : ""}`);
    } catch (e) {
      const detail = (e.stderr && e.stderr.toString()) || e.message || String(e);
      failed.push({ file, error: detail.trim().split("\n").pop() });
      console.error(`FAIL: ${file}: ${detail.trim().split("\n").pop()}`);
    }
  }

  // Write a manifest so the app (or a future loader) can enumerate the library.
  const manifestPath = path.join(outDir, "manifest.json");
  fs.writeJsonSync(
    manifestPath,
    {
      animations: manifest.map((m) => {
        const meta = animations.find((a) => a.name === m.name) || {};
        return {
          name: m.name,
          file: m.name + ".vrma",
          category: meta.category || "other",
          description: meta.description || "",
          mixamoName: meta.mixamoName || m.name,
        };
      }),
    },
    { spaces: 2 }
  );

  console.log(`\nDone: ${ok} converted, ${failed.length} failed, ${files.length} total.`);
  console.log(`Manifest: ${manifestPath}`);
  if (failed.length) process.exitCode = 1;
}

main().catch((e) => {
  console.error(e.message || e);
  process.exit(1);
});
