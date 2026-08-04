#!/usr/bin/env node
"use strict";
/* Bambu Studio connector — stdio MCP server (no dependencies).
 * Runs on the user's Mac. Opens model files in Bambu Studio, finds model
 * files on disk, and reports whether Bambu Studio is installed/running. */

const { execFile } = require("child_process");
const fs = require("fs");
const path = require("path");
const os = require("os");
const https = require("https");
const dgram = require("dgram");

/* ---- Bambu cloud account integration ---- */

const STATE_DIR = path.join(os.homedir(), ".bambu-studio-connector");
const CLOUD_FILE = path.join(STATE_DIR, "cloud.json");

function coworkProjectConfigs() {
  /* Any Cowork project folder may hold a printer-config.json */
  try {
    const root = path.join(os.homedir(), "Claude", "Projects");
    return fs.readdirSync(root).map((d) => path.join(root, d, "printer-config.json"));
  } catch {
    return [];
  }
}

const CONFIG_CANDIDATES = [
  process.env.BAMBU_CONFIG_PATH,
  path.join(STATE_DIR, "printer-config.json"),
  ...coworkProjectConfigs(),
].filter(Boolean);

function apiBase(region) {
  return region === "china" ? "https://api.bambulab.cn" : "https://api.bambulab.com";
}

function httpJson(method, url, headers, body) {
  return new Promise((resolve, reject) => {
    const data = body ? JSON.stringify(body) : null;
    const req = https.request(
      url,
      {
        method,
        headers: {
          "Content-Type": "application/json",
          "User-Agent": "bambu-studio-connector/0.3.0",
          ...(data ? { "Content-Length": Buffer.byteLength(data) } : {}),
          ...headers,
        },
        timeout: 20000,
      },
      (res) => {
        let buf = "";
        res.on("data", (c) => (buf += c));
        res.on("end", () => {
          let parsed = null;
          try {
            parsed = JSON.parse(buf);
          } catch {
            /* non-JSON body */
          }
          resolve({ status: res.statusCode, body: parsed, raw: buf.slice(0, 500) });
        });
      }
    );
    req.on("timeout", () => req.destroy(new Error("request timed out")));
    req.on("error", reject);
    if (data) req.write(data);
    req.end();
  });
}

function loadCloud() {
  try {
    return JSON.parse(fs.readFileSync(CLOUD_FILE, "utf8"));
  } catch {
    return null;
  }
}

function saveCloud(obj) {
  fs.mkdirSync(STATE_DIR, { recursive: true });
  fs.writeFileSync(CLOUD_FILE, JSON.stringify(obj, null, 2), { mode: 0o600 });
}

function readPrinterConfig() {
  for (const p of CONFIG_CANDIDATES) {
    try {
      return JSON.parse(fs.readFileSync(p, "utf8"));
    } catch {
      /* try next */
    }
  }
  return {};
}

function findPrinterConfigPath() {
  for (const p of CONFIG_CANDIDATES) {
    if (fs.existsSync(p)) return p;
  }
  fs.mkdirSync(STATE_DIR, { recursive: true });
  return path.join(STATE_DIR, "printer-config.json");
}

function updatePrinterConfig(fields) {
  const p = findPrinterConfigPath();
  let cfg = {};
  try {
    cfg = JSON.parse(fs.readFileSync(p, "utf8"));
  } catch {
    /* start fresh */
  }
  Object.assign(cfg, fields);
  fs.writeFileSync(p, JSON.stringify(cfg, null, 2));
  return p;
}

function ssdpDiscover(serial, waitMs) {
  /* Bambu printers broadcast SSDP NOTIFY packets to UDP 2021 and 1990 with a
   * Location: header carrying the printer's IP and a USN containing the serial. */
  return new Promise((resolve) => {
    const found = {};
    const sockets = [];
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      for (const s of sockets) {
        try {
          s.close();
        } catch {}
      }
      const bySerial = serial && found[serial.toUpperCase()];
      const ips = Object.values(found);
      resolve(bySerial || (ips.length === 1 ? ips[0] : null));
    };
    for (const port of [2021, 1990]) {
      const sock = dgram.createSocket({ type: "udp4", reuseAddr: true });
      sockets.push(sock);
      sock.on("error", () => {
        try {
          sock.close();
        } catch {}
      });
      sock.on("message", (msg) => {
        const text = msg.toString("utf8");
        const loc = /Location:\s*([0-9.]+)/i.exec(text);
        if (!loc) return;
        const usn = /USN:\s*(\S+)/i.exec(text);
        found[usn ? usn[1].toUpperCase() : loc[1]] = loc[1];
        if (serial && usn && usn[1].toUpperCase() === serial.toUpperCase()) finish();
      });
      try {
        sock.bind(port);
      } catch {}
    }
    setTimeout(finish, waitMs || 8000);
  });
}

async function syncDevicesFromCloud(preferredSerial) {
  const cloud = loadCloud();
  if (!cloud || !cloud.accessToken) {
    return { ok: false, text: "Not signed in to a Bambu account yet. Use bambu_account_request_code, then bambu_account_login." };
  }
  if (cloud.expiresAt && Date.now() > cloud.expiresAt) {
    return { ok: false, text: "The saved Bambu account token has expired (they last ~3 months). Sign in again with bambu_account_request_code + bambu_account_login." };
  }
  const auth = { Authorization: "Bearer " + cloud.accessToken };
  const res = await httpJson("GET", apiBase(cloud.region) + "/v1/iot-service/api/user/bind", auth);
  if (res.status !== 200 || !res.body || !Array.isArray(res.body.devices)) {
    return { ok: false, text: `Could not list printers (HTTP ${res.status}): ${res.raw || "no body"}. The token may have been revoked — try signing in again.` };
  }
  const devices = res.body.devices;
  if (devices.length === 0) return { ok: false, text: "The Bambu account has no printers bound to it." };
  let dev = devices[0];
  if (preferredSerial) {
    dev = devices.find((d) => (d.dev_id || "").toUpperCase() === preferredSerial.toUpperCase()) || dev;
  }
  const serial = (dev.dev_id || "").trim();
  const accessCode = (dev.dev_access_code || "").trim();
  const ip = await ssdpDiscover(serial, 8000);
  const fields = {
    serial,
    access_code: accessCode,
    model: dev.dev_product_name || dev.dev_model_name || "unknown",
    printer_name: dev.name || "",
    online: !!dev.online,
  };
  if (ip) fields.host = ip;
  const cfgPath = updatePrinterConfig(fields);
  const others = devices.length > 1 ? ` (account has ${devices.length} printers; using "${dev.name || serial}" — pass serial to choose another)` : "";
  return {
    ok: true,
    text:
      `Synced from Bambu account${others}:\n` +
      `printer: ${dev.name || "?"} (${fields.model}), serial ${serial}, ${dev.online ? "online" : "offline"}\n` +
      `access code: updated\n` +
      (ip
        ? `IP address: ${ip} (discovered on the local network)\n`
        : `IP address: NOT found via network discovery — printer may be off or on another network. Ask the user for the IP (printer screen > Settings > WLAN) and write it into ${cfgPath} as "host".\n`) +
      `Config written to ${cfgPath}. If the printer connector was already running, the user must disable/re-enable the plugin to pick up changes.`,
  };
}

const MODEL_EXTS = new Set([".stl", ".3mf", ".obj", ".step", ".stp"]);
const SKIP_DIRS = new Set(["node_modules", "Library", ".Trash", ".git", "Applications", "Pictures", "Music", "Movies"]);
const APP_NAMES = ["BambuStudio", "Bambu Studio"];

const TOOLS = [
  {
    name: "open_in_bambu_studio",
    description:
      "Open one or more 3D model files (.stl, .3mf, .obj, .step) in Bambu Studio on this Mac, loading them onto the print bed. " +
      "Paths must be absolute Mac paths (starting with /Users/...), not sandbox paths. " +
      "Note: Bambu Studio may show a dialog (e.g. 'load these files as a single object?' or, for .3mf project files, 'open in a new window?') that the user confirms in the app.",
    inputSchema: {
      type: "object",
      properties: {
        file_paths: {
          type: "array",
          items: { type: "string" },
          description: "Absolute Mac paths to model files to load onto the print bed",
        },
      },
      required: ["file_paths"],
    },
  },
  {
    name: "find_model_files",
    description:
      "Search this Mac for 3D model files (.stl, .3mf, .obj, .step) whose path matches all given search terms (case-insensitive). " +
      "Searches the home folder (Downloads, Documents, Desktop, Claude projects, etc.) unless search_paths is given. Returns absolute Mac paths.",
    inputSchema: {
      type: "object",
      properties: {
        query: { type: "string", description: "Space-separated search terms, e.g. 'langstroth hive'" },
        search_paths: {
          type: "array",
          items: { type: "string" },
          description: "Optional absolute directories to search instead of the default home folders",
        },
        max_results: { type: "number", description: "Max results (default 25)" },
      },
      required: ["query"],
    },
  },
  {
    name: "bambu_studio_status",
    description: "Check whether Bambu Studio is installed on this Mac and whether it is currently running.",
    inputSchema: { type: "object", properties: {} },
  },
  {
    name: "bambu_account_request_code",
    description:
      "Step 1 of signing into the user's Bambu Lab account: sends a one-time verification code to their email. " +
      "Prefer this code flow over passwords so the user's password never enters the chat.",
    inputSchema: {
      type: "object",
      properties: {
        email: { type: "string", description: "Bambu Lab account email" },
        region: { type: "string", enum: ["global", "china"], description: "Account region (default global)" },
      },
      required: ["email"],
    },
  },
  {
    name: "bambu_account_login",
    description:
      "Step 2: log into the Bambu Lab account with the emailed verification code (or password as fallback). " +
      "On success the token is stored locally (~3 month validity), the printer's serial + access code are fetched from the account, " +
      "the printer's IP is discovered on the local network, and printer-config.json is filled in automatically.",
    inputSchema: {
      type: "object",
      properties: {
        email: { type: "string", description: "Bambu Lab account email" },
        code: { type: "string", description: "Verification code from the email (preferred)" },
        password: { type: "string", description: "Account password (fallback if code login unavailable)" },
        region: { type: "string", enum: ["global", "china"], description: "Account region (default global)" },
      },
      required: ["email"],
    },
  },
  {
    name: "bambu_account_sync",
    description:
      "Refresh printer settings from the signed-in Bambu Lab account: re-fetches the access code (e.g. after it changed), " +
      "updates printer-config.json, and re-discovers the printer's IP on the local network. Requires a prior bambu_account_login.",
    inputSchema: {
      type: "object",
      properties: {
        serial: { type: "string", description: "Prefer the printer with this serial if the account has several" },
      },
    },
  },
  {
    name: "bambu_cloud_printer_status",
    description:
      "Get printer online/task status via the Bambu cloud (works even when this Mac is not on the printer's network). " +
      "Requires a prior bambu_account_login. For detailed local status use the bambu-printer server's get_printer_status instead.",
    inputSchema: { type: "object", properties: {} },
  },
  {
    name: "send_push",
    description:
      "Send a push notification to the user's phone via ntfy (free, no account). Use to alert the user when they need to act: " +
      "clear the print bed, press Print in Bambu Studio for a scheduled print, or when a print finished or failed. " +
      "Requires 'ntfy_topic' set in printer-config.json and the ntfy app subscribed to that topic.",
    inputSchema: {
      type: "object",
      properties: {
        message: { type: "string", description: "Notification body text" },
        title: { type: "string", description: "Short title (optional)" },
        priority: {
          type: "string",
          enum: ["min", "low", "default", "high", "urgent"],
          description: "Notification priority (optional, default 'default'); use 'high' for action-needed alerts",
        },
      },
      required: ["message"],
    },
  },
];

function send(msg) {
  process.stdout.write(JSON.stringify(msg) + "\n");
}
function reply(id, result) {
  send({ jsonrpc: "2.0", id, result });
}
function replyError(id, code, message) {
  send({ jsonrpc: "2.0", id, error: { code, message } });
}
function toolText(id, text, isError) {
  reply(id, { content: [{ type: "text", text }], isError: !!isError });
}

function openInBambu(files, cb) {
  const attempt = (i) => {
    execFile("open", ["-a", APP_NAMES[i], ...files], (err) => {
      if (err && i + 1 < APP_NAMES.length) return attempt(i + 1);
      cb(err, APP_NAMES[i]);
    });
  };
  attempt(0);
}

function walk(dir, terms, results, maxResults, depth) {
  if (depth > 7 || results.length >= maxResults) return;
  let entries;
  try {
    entries = fs.readdirSync(dir, { withFileTypes: true });
  } catch {
    return;
  }
  for (const e of entries) {
    if (results.length >= maxResults) return;
    if (e.name.startsWith(".")) continue;
    const full = path.join(dir, e.name);
    if (e.isDirectory()) {
      if (!SKIP_DIRS.has(e.name)) walk(full, terms, results, maxResults, depth + 1);
    } else if (MODEL_EXTS.has(path.extname(e.name).toLowerCase())) {
      const hay = full.toLowerCase();
      if (terms.every((t) => hay.includes(t))) {
        let st;
        try {
          st = fs.statSync(full);
        } catch {
          continue;
        }
        results.push({
          path: full,
          size_kb: Math.round(st.size / 1024),
          modified: st.mtime.toISOString().slice(0, 10),
        });
      }
    }
  }
}

function handleToolCall(id, params) {
  const name = params && params.name;
  const args = (params && params.arguments) || {};

  if (name === "open_in_bambu_studio") {
    const files = args.file_paths;
    if (!Array.isArray(files) || files.length === 0) {
      return toolText(id, "file_paths must be a non-empty array of absolute Mac paths.", true);
    }
    const problems = [];
    for (const f of files) {
      if (typeof f !== "string" || !path.isAbsolute(f)) problems.push(`Not an absolute path: ${f}`);
      else if (!fs.existsSync(f)) problems.push(`File not found on this Mac: ${f}`);
      else if (!MODEL_EXTS.has(path.extname(f).toLowerCase())) problems.push(`Not a supported model type: ${f}`);
    }
    if (problems.length) return toolText(id, problems.join("\n"), true);
    return openInBambu(files, (err, app) => {
      if (err) {
        return toolText(
          id,
          `Could not open Bambu Studio (tried app names: ${APP_NAMES.join(", ")}). Is it installed in /Applications? Error: ${err.message}`,
          true
        );
      }
      toolText(
        id,
        `Sent ${files.length} file(s) to ${app}:\n${files.join("\n")}\n` +
          `Bambu Studio may show an import dialog the user needs to confirm (e.g. single vs. multiple objects, or new window for .3mf projects).`
      );
    });
  }

  if (name === "find_model_files") {
    const query = (args.query || "").trim();
    if (!query) return toolText(id, "query is required.", true);
    const terms = query.toLowerCase().split(/\s+/);
    const maxResults = Math.min(Math.max(args.max_results || 25, 1), 100);
    const home = os.homedir();
    let roots = Array.isArray(args.search_paths) && args.search_paths.length ? args.search_paths : [home];
    const results = [];
    for (const r of roots) walk(r, terms, results, maxResults, 0);
    if (!results.length) return toolText(id, `No model files matching "${query}" found under: ${roots.join(", ")}`);
    const lines = results.map((r) => `${r.path}  (${r.size_kb} KB, modified ${r.modified})`);
    return toolText(id, `Found ${results.length} model file(s):\n${lines.join("\n")}`);
  }

  if (name === "bambu_studio_status") {
    const home = os.homedir();
    const installPaths = [
      "/Applications/BambuStudio.app",
      "/Applications/Bambu Studio.app",
      path.join(home, "Applications/BambuStudio.app"),
      path.join(home, "Applications/Bambu Studio.app"),
    ];
    const installed = installPaths.find((p) => fs.existsSync(p));
    return execFile("pgrep", ["-if", "bambustudio"], (err, stdout) => {
      const running = !err && stdout.trim().length > 0;
      toolText(
        id,
        installed
          ? `Bambu Studio is installed at ${installed}. Currently ${running ? "running" : "not running"}.`
          : "Bambu Studio does not appear to be installed in /Applications or ~/Applications."
      );
    });
  }

  if (name === "bambu_account_request_code") {
    const email = (args.email || "").trim();
    if (!email) return toolText(id, "email is required.", true);
    httpJson("POST", apiBase(args.region) + "/v1/user-service/user/sendemail/code", {}, { email, type: "codeLogin" })
      .then((res) => {
        if (res.status === 200) {
          toolText(id, `Verification code sent to ${email}. Ask the user for the code, then call bambu_account_login with it.`);
        } else {
          toolText(id, `Bambu API returned HTTP ${res.status}: ${res.raw || "no body"}. Check the email address/region, or fall back to password login via bambu_account_login.`, true);
        }
      })
      .catch((e) => toolText(id, `Could not reach the Bambu API: ${e.message}`, true));
    return;
  }

  if (name === "bambu_account_login") {
    const email = (args.email || "").trim();
    if (!email) return toolText(id, "email is required.", true);
    if (!args.code && !args.password) return toolText(id, "Provide the emailed verification code (preferred) or a password.", true);
    const payload = { account: email };
    if (args.code) payload.code = String(args.code).trim();
    else payload.password = String(args.password);
    const region = args.region === "china" ? "china" : "global";
    httpJson("POST", apiBase(region) + "/v1/user-service/user/login", {}, payload)
      .then(async (res) => {
        const b = res.body || {};
        if (res.status !== 200 || !b.accessToken) {
          if (b.loginType === "verifyCode" || /verif/i.test(res.raw || "")) {
            return toolText(id, "This account requires verification-code login. Call bambu_account_request_code first, then retry with the code.", true);
          }
          if (b.tfaKey || /tfa|two.?factor/i.test(res.raw || "")) {
            return toolText(id, "This account has two-factor authentication enabled, which this connector doesn't support. Fill in printer-config.json manually instead (values are on the printer screen).", true);
          }
          return toolText(id, `Login failed (HTTP ${res.status}): ${res.raw || "no body"}`, true);
        }
        saveCloud({
          accessToken: b.accessToken,
          expiresAt: Date.now() + (b.expiresIn ? b.expiresIn * 1000 : 80 * 24 * 3600 * 1000),
          region,
        });
        const sync = await syncDevicesFromCloud(args.serial);
        toolText(id, `Signed in (token stored locally, valid ~3 months; the password/code was not stored).\n${sync.text}`, !sync.ok);
      })
      .catch((e) => toolText(id, `Could not reach the Bambu API: ${e.message}`, true));
    return;
  }

  if (name === "bambu_account_sync") {
    syncDevicesFromCloud(args.serial)
      .then((r) => toolText(id, r.text, !r.ok))
      .catch((e) => toolText(id, `Sync failed: ${e.message}`, true));
    return;
  }

  if (name === "bambu_cloud_printer_status") {
    const cloud = loadCloud();
    if (!cloud || !cloud.accessToken) return toolText(id, "Not signed in. Use bambu_account_request_code + bambu_account_login first.", true);
    httpJson("GET", apiBase(cloud.region) + "/v1/iot-service/api/user/print?force=true", { Authorization: "Bearer " + cloud.accessToken })
      .then((res) => {
        const devs = res.body && res.body.devices;
        if (res.status !== 200 || !Array.isArray(devs)) {
          return toolText(id, `Cloud status failed (HTTP ${res.status}): ${res.raw || "no body"}. Token may have expired — sign in again.`, true);
        }
        const lines = devs.map((d) => {
          const task = d.task_name ? `printing "${d.task_name}" (${d.progress != null ? d.progress + "%" : "progress unknown"})` : "no active cloud task";
          return `${d.dev_name || d.dev_id} (${d.dev_product_name || "?"}): ${d.dev_online ? "online" : "offline"}, ${task}`;
        });
        toolText(id, lines.join("\n") || "No printers on this account.");
      })
      .catch((e) => toolText(id, `Could not reach the Bambu API: ${e.message}`, true));
    return;
  }

  if (name === "send_push") {
    const message = (args.message || "").trim();
    if (!message) return toolText(id, "message is required.", true);
    const topic = (readPrinterConfig().ntfy_topic || "").trim();
    if (!topic) {
      return toolText(
        id,
        "No ntfy_topic set in printer-config.json. Add a private topic (e.g. a random string) there and subscribe to it in the ntfy app first.",
        true
      );
    }
    const headers = { "Content-Type": "text/plain; charset=utf-8" };
    if (args.title) headers["X-Title"] = String(args.title).replace(/[\r\n]/g, " ");
    if (args.priority) headers["X-Priority"] = String(args.priority);
    const body = Buffer.from(message, "utf8");
    headers["Content-Length"] = body.length;
    const req = https.request(
      "https://ntfy.sh/" + encodeURIComponent(topic),
      { method: "POST", headers, timeout: 15000 },
      (res) => {
        let buf = "";
        res.on("data", (c) => (buf += c));
        res.on("end", () => {
          if (res.statusCode >= 200 && res.statusCode < 300) {
            toolText(id, `Push sent to ntfy topic "${topic}".`);
          } else {
            toolText(id, `ntfy returned HTTP ${res.statusCode}: ${buf.slice(0, 300)}`, true);
          }
        });
      }
    );
    req.on("timeout", () => req.destroy(new Error("request timed out")));
    req.on("error", (e) => toolText(id, `Could not reach ntfy.sh: ${e.message}`, true));
    req.write(body);
    req.end();
    return;
  }

  return toolText(id, `Unknown tool: ${name}`, true);
}

function handleMessage(line) {
  let msg;
  try {
    msg = JSON.parse(line);
  } catch {
    return;
  }
  const { id, method, params } = msg;
  if (method === "initialize") {
    reply(id, {
      protocolVersion: (params && params.protocolVersion) || "2024-11-05",
      capabilities: { tools: {} },
      serverInfo: { name: "bambu-studio-connector", version: "0.1.0" },
    });
  } else if (method === "ping") {
    reply(id, {});
  } else if (method === "tools/list") {
    reply(id, { tools: TOOLS });
  } else if (method === "tools/call") {
    try {
      handleToolCall(id, params);
    } catch (e) {
      toolText(id, `Tool error: ${e.message}`, true);
    }
  } else if (typeof method === "string" && method.startsWith("notifications/")) {
    /* no response for notifications */
  } else if (id !== undefined) {
    replyError(id, -32601, `Method not found: ${method}`);
  }
}

let buffer = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => {
  buffer += chunk;
  let nl;
  while ((nl = buffer.indexOf("\n")) !== -1) {
    const line = buffer.slice(0, nl).trim();
    buffer = buffer.slice(nl + 1);
    if (line) handleMessage(line);
  }
});
process.stdin.on("end", () => process.exit(0));
