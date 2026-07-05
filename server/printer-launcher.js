#!/usr/bin/env node
"use strict";
/* Launcher for the bambu-printer-mcp npm package (printer status + camera).
 * Reads printer settings from a JSON config file, maps them to the env vars
 * the package expects, and runs it via npx so it executes natively on this
 * machine and benefits from the npm cache. */

const { spawn } = require("child_process");
const fs = require("fs");
const path = require("path");
const os = require("os");

const PKG = "bambu-printer-mcp@1.1.5";

function coworkProjectConfigs() {
  try {
    const root = path.join(os.homedir(), "Claude", "Projects");
    return fs.readdirSync(root).map((d) => path.join(root, d, "printer-config.json"));
  } catch {
    return [];
  }
}

const CANDIDATES = [
  process.env.BAMBU_CONFIG_PATH,
  path.join(os.homedir(), ".bambu-studio-connector", "printer-config.json"),
  ...coworkProjectConfigs(),
].filter(Boolean);

let cfg = null;
for (const p of CANDIDATES) {
  try {
    cfg = JSON.parse(fs.readFileSync(p, "utf8"));
    break;
  } catch {
    /* try next */
  }
}

const looksUnset = (v) => !v || /HERE|YOUR_/i.test(String(v));
if (!cfg || looksUnset(cfg.host) || looksUnset(cfg.access_code) || looksUnset(cfg.serial)) {
  console.error(
    "bambu-printer: printer not configured yet. Fill in host, access_code, and serial in printer-config.json. Looked in:\n" +
      CANDIDATES.join("\n")
  );
  process.exit(1);
}

const env = { ...process.env };
env.BAMBU_PRINTER_HOST = String(cfg.host);
env.PRINTER_HOST = String(cfg.host);
env.BAMBU_PRINTER_ACCESS_TOKEN = String(cfg.access_code);
env.BAMBU_TOKEN = String(cfg.access_code);
env.BAMBU_PRINTER_SERIAL = String(cfg.serial);
env.BAMBU_SERIAL = String(cfg.serial);
if (cfg.model) {
  env.BAMBU_PRINTER_MODEL = String(cfg.model);
  env.BAMBU_MODEL = String(cfg.model);
}
if (cfg.slicer_path) env.SLICER_PATH = String(cfg.slicer_path);
if (cfg.bed_type) env.BED_TYPE = String(cfg.bed_type);

const localNpx = path.join(path.dirname(process.execPath), "npx");
const cmd = fs.existsSync(localNpx) ? localNpx : "npx";
const child = spawn(cmd, ["-y", PKG], { env, stdio: "inherit" });
child.on("error", (e) => {
  console.error("bambu-printer: failed to launch npx: " + e.message);
  process.exit(1);
});
child.on("exit", (code) => process.exit(code === null ? 1 : code));
