#!/usr/bin/env node
"use strict";

// Mixamo bulk FBX downloader (admin/local tool).
//
// Adobe provides no public Mixamo API, so this drives a real logged-in browser
// session (Puppeteer) to obtain an access token, then calls Mixamo's internal
// REST endpoints to search and export animations as FBX. Inspired by 3dchat's
// scripts/download-mixamo.js.
//
//   !! Use responsibly. Automated downloading may conflict with Adobe/Mixamo's
//      Terms of Service. This is a convenience tool for clips you are entitled
//      to use; you are responsible for compliance and licensing. The vendored
//      animation-list.json + convert-raw-to-vrma.js path (manual download) is
//      the recommended, ToS-safe way to build the library.
//
// Puppeteer is an OPTIONAL dependency (large). Install it only when you need
// this tool:  cd tools && npm install puppeteer
//
// Usage:
//   node download-mixamo.js --out animations-raw \
//        [--email you@adobe.com --password ****] [--limit 50] [--headful] \
//        [--list animation-list.json]
//
//   Credentials may also come from MIXAMO_EMAIL / MIXAMO_PASSWORD env vars. With
//   --headful (default) you can simply log in by hand in the opened window.

const fs = require("fs-extra");
const path = require("path");
const { Command } = require("commander");

const MIXAMO_API = "https://www.mixamo.com/api/v1";
// Public Mixamo web client id (the value the mixamo.com SPA itself sends).
const MIXAMO_CLIENT_ID = "mixamo2";

function requirePuppeteer() {
  try {
    return require("puppeteer");
  } catch (e) {
    console.error(
      "Puppeteer is not installed. This tool needs it to authenticate.\n" +
      "  cd tools && npm install puppeteer\n" +
      "Or download FBX clips manually from mixamo.com into your raw directory and\n" +
      "use convert-raw-to-vrma.js instead (recommended)."
    );
    process.exit(1);
  }
}

const sleep = (ms) => new Promise((res) => setTimeout(res, ms));

async function getAccessToken(page, email, password, headful) {
  await page.goto("https://www.mixamo.com/", { waitUntil: "networkidle2" });

  // If creds are supplied, try to drive the Adobe login form; otherwise rely on
  // the user logging in by hand in the headful window.
  if (email && password) {
    try {
      await page.goto("https://account.adobe.com/", { waitUntil: "networkidle2" });
      await page.waitForSelector("#EmailPage-EmailField", { timeout: 15000 });
      await page.type("#EmailPage-EmailField", email, { delay: 20 });
      await page.click("[data-id='EmailPage-ContinueButton']");
      await page.waitForSelector("#PasswordPage-PasswordField", { timeout: 15000 });
      await page.type("#PasswordPage-PasswordField", password, { delay: 20 });
      await page.click("[data-id='PasswordPage-ContinueButton']");
      await page.waitForNavigation({ waitUntil: "networkidle2", timeout: 30000 });
    } catch (e) {
      console.warn("Automated Adobe login flow did not complete; finish logging in manually.");
    }
  }

  // Poll localStorage for the bearer token the Mixamo SPA stores after login.
  const deadline = Date.now() + (headful ? 5 * 60 * 1000 : 60 * 1000);
  while (Date.now() < deadline) {
    await page.goto("https://www.mixamo.com/", { waitUntil: "networkidle2" });
    const token = await page.evaluate(() => {
      try {
        return window.localStorage.getItem("access_token");
      } catch (e) {
        return null;
      }
    });
    if (token) return token;
    await sleep(3000);
  }
  throw new Error("Failed to get authentication token (not logged in?).");
}

function apiHeaders(token) {
  return {
    Authorization: `Bearer ${token}`,
    "X-Api-Key": MIXAMO_CLIENT_ID,
    Accept: "application/json",
    "Content-Type": "application/json",
  };
}

// page.evaluate-driven fetch so requests carry the page's session/cookies.
async function pageFetchJson(page, url, options) {
  return page.evaluate(
    async (u, o) => {
      const r = await fetch(u, o);
      if (!r.ok) throw new Error(`${r.status} ${r.statusText} for ${u}`);
      return r.json();
    },
    url,
    options
  );
}

async function searchAnimation(page, token, query) {
  const url =
    `${MIXAMO_API}/products?page=1&limit=12&order=&type=Motion%2CMotionPack` +
    `&query=${encodeURIComponent(query)}`;
  const data = await pageFetchJson(page, url, { headers: apiHeaders(token) });
  return (data.results || [])[0] || null;
}

async function exportFbx(page, token, product) {
  // Request an FBX export for the product, then poll the character monitor.
  const exportUrl = `${MIXAMO_API}/animations/export`;
  const body = JSON.stringify({
    gms_hash: [
      {
        "model-id": product.id,
        mirror: false,
        trim: [0, 100],
        inplace: false,
        "arm-space": 0,
        overdrive: 0,
        params: (product.details && product.details.default_frames) || "",
      },
    ],
    preferences: { format: "fbx7_2019", skin: "false", fps: "30", reducekf: "0" },
    character_id: product.id,
    type: "Motion",
    product_name: product.name,
  });
  await pageFetchJson(page, exportUrl, { method: "POST", headers: apiHeaders(token), body });

  const monitorUrl = `${MIXAMO_API}/characters/${product.id}/monitor`;
  const deadline = Date.now() + 120000;
  while (Date.now() < deadline) {
    const status = await pageFetchJson(page, monitorUrl, { headers: apiHeaders(token) });
    if (status.status === "completed" && status.job_result) return status.job_result;
    if (status.status === "failed") throw new Error(`Export failed for ${product.name}`);
    await sleep(2000);
  }
  throw new Error(`Export timed out for ${product.name}`);
}

async function downloadTo(page, url, dest) {
  const buf = await page.evaluate(async (u) => {
    const r = await fetch(u);
    const ab = await r.arrayBuffer();
    return Array.from(new Uint8Array(ab));
  }, url);
  await fs.writeFile(dest, Buffer.from(buf));
}

async function main() {
  const program = new Command()
    .option("--out <dir>", "Output directory for raw FBX", path.join(process.cwd(), "animations-raw"))
    .option("--list <path>", "animation-list.json to drive which clips to fetch", path.join(__dirname, "animation-list.json"))
    .option("--email <email>", "Adobe account email", process.env.MIXAMO_EMAIL)
    .option("--password <password>", "Adobe account password", process.env.MIXAMO_PASSWORD)
    .option("--limit <n>", "Max clips to download", (v) => parseInt(v, 10), 50)
    .option("--headful", "Show the browser window (lets you log in manually)", true)
    .option("--headless", "Run headless (requires --email/--password)")
    .parse();

  const opts = program.opts();
  const headful = opts.headless ? false : opts.headful;
  const outDir = path.resolve(opts.out);
  fs.ensureDirSync(outDir);

  const list = fs.existsSync(opts.list) ? (fs.readJsonSync(opts.list).animations || []) : [];
  if (list.length === 0) {
    console.error(`No animations in ${opts.list}; nothing to download.`);
    process.exit(1);
  }
  const queries = list.slice(0, opts.limit);

  const puppeteer = requirePuppeteer();
  const browser = await puppeteer.launch({
    headless: !headful,
    defaultViewport: null,
    args: ["--no-sandbox", "--disable-setuid-sandbox"],
  });

  try {
    const page = await browser.newPage();
    console.log("Authenticating with Mixamo...");
    const token = await getAccessToken(page, opts.email, opts.password, headful);
    console.log("Authenticated. Downloading clips...");

    let ok = 0;
    const failed = [];
    for (const anim of queries) {
      try {
        const product = await searchAnimation(page, token, anim.mixamoName);
        if (!product) {
          failed.push({ name: anim.mixamoName, error: "not found" });
          console.error(`not found: ${anim.mixamoName}`);
          continue;
        }
        const fbxUrl = await exportFbx(page, token, product);
        const dest = path.join(outDir, `${anim.mixamoName}.fbx`);
        await downloadTo(page, fbxUrl, dest);
        ok++;
        console.log(`ok: ${anim.mixamoName} -> ${path.basename(dest)}`);
        await sleep(2000); // be gentle with the API
      } catch (e) {
        failed.push({ name: anim.mixamoName, error: e.message });
        console.error(`FAIL: ${anim.mixamoName}: ${e.message}`);
      }
    }

    console.log(`\nDone: ${ok} downloaded, ${failed.length} failed.`);
    console.log(`Next: node convert-raw-to-vrma.js --in "${outDir}" --out ../app/static/animations`);
  } finally {
    await browser.close();
  }
}

main().catch((e) => {
  console.error(e.message || e);
  process.exit(1);
});
