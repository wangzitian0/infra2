import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";

const stateDir = process.env.XDG_CONFIG_HOME || "/home/node/.openclaw";
const jobsPath = path.join(stateDir, "cron", "jobs.json");
const runnerStatePath = path.join(stateDir, "cron", "tianclaw-runner-state.json");
const eventsPath = path.join(stateDir, "cron", "tianclaw-runner-events.jsonl");
const enabled = (process.env.TIANCLAW_CRON_RUNNER_ENABLED || "true").toLowerCase() === "true";
const intervalMs = positiveInt(process.env.TIANCLAW_CRON_RUNNER_INTERVAL_MS, 30_000);
const gatewayPort = process.env.OPENCLAW_GATEWAY_PORT || "18789";
const gatewayUrl = process.env.TIANCLAW_CRON_RUNNER_GATEWAY_URL || `ws://openclaw:${gatewayPort}`;
const cliTimeoutMs = positiveInt(process.env.TIANCLAW_CRON_RUNNER_CLI_TIMEOUT_MS, 45_000);
const defaultTz = process.env.TZ || "UTC";

if (!enabled) {
  console.log("tianclaw cron runner disabled");
  await sleep(2 ** 31 - 1);
}

console.log(`tianclaw cron runner started intervalMs=${intervalMs} gatewayUrl=${gatewayUrl}`);

await runLoopOnce();
setInterval(() => {
  runLoopOnce().catch((err) => {
    logEvent({ type: "runner_error", error: String(err) });
  });
}, intervalMs).unref?.();

await sleep(2 ** 31 - 1);

async function runLoopOnce() {
  const now = new Date();
  const jobsDoc = readJson(jobsPath, { jobs: [] });
  const runnerState = readJson(runnerStatePath, { version: 1, slots: {} });
  let stateChanged = false;

  for (const job of jobsDoc.jobs || []) {
    if (!job || job.enabled !== true || !job.schedule) continue;
    const due = currentDueSlot(job, now);
    if (!due) continue;
    if (runnerState.slots[job.id] === due.slotKey) continue;

    runnerState.slots[job.id] = due.slotKey;
    stateChanged = true;
    writeJsonAtomic(runnerStatePath, runnerState);

    const result = triggerJob(job.id);
    logEvent({
      type: result.ok ? "triggered" : "trigger_failed",
      jobId: job.id,
      name: job.name,
      slotKey: due.slotKey,
      schedule: job.schedule,
      status: result.status,
      error: result.error,
    });
  }

  if (stateChanged) writeJsonAtomic(runnerStatePath, runnerState);
}

function currentDueSlot(job, now) {
  if (job.schedule.kind === "cron") {
    const tz = job.schedule.tz || defaultTz;
    const parts = zonedParts(now, tz);
    if (!cronMatches(job.schedule.expr, parts)) return null;
    return { slotKey: `${tz}:${parts.year}-${pad2(parts.month)}-${pad2(parts.day)}T${pad2(parts.hour)}:${pad2(parts.minute)}` };
  }
  if (job.schedule.kind === "at") {
    const atMs = Date.parse(job.schedule.at || job.schedule.atMs);
    if (!Number.isFinite(atMs)) return null;
    const currentMinute = Math.floor(now.getTime() / 60_000);
    if (Math.floor(atMs / 60_000) !== currentMinute) return null;
    return { slotKey: `at:${atMs}` };
  }
  return null;
}

function cronMatches(expr, parts) {
  const fields = String(expr || "").trim().split(/\s+/);
  if (fields.length !== 5) return false;
  const [minute, hour, dom, month, dow] = fields;
  return matchField(minute, parts.minute, 0, 59)
    && matchField(hour, parts.hour, 0, 23)
    && matchField(dom, parts.day, 1, 31)
    && matchField(month, parts.month, 1, 12)
    && matchDow(dow, parts.dayOfWeek);
}

function matchDow(field, value) {
  if (String(field).trim() === "*") return true;
  return expandCronField(field, 0, 7).some((candidate) => (candidate === 7 ? 0 : candidate) === value);
}

function matchField(field, value, min, max) {
  if (String(field).trim() === "*") return true;
  return expandCronField(field, min, max).includes(value);
}

function expandCronField(field, min, max) {
  const values = new Set();
  for (const rawPart of String(field || "").split(",")) {
    const part = rawPart.trim();
    if (!part) continue;
    const [rangePart, stepPart] = part.split("/");
    const step = positiveInt(stepPart, 1);
    let start;
    let end;
    if (rangePart === "*") {
      start = min;
      end = max;
    } else if (rangePart.includes("-")) {
      const [a, b] = rangePart.split("-").map((x) => Number.parseInt(x, 10));
      start = a;
      end = b;
    } else {
      start = Number.parseInt(rangePart, 10);
      end = start;
    }
    if (!Number.isFinite(start) || !Number.isFinite(end)) continue;
    for (let value = Math.max(min, start); value <= Math.min(max, end); value += step) values.add(value);
  }
  return [...values];
}

function zonedParts(date, timeZone) {
  const formatter = new Intl.DateTimeFormat("en-CA", {
    timeZone,
    hourCycle: "h23",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
  const parts = Object.fromEntries(formatter.formatToParts(date).map((part) => [part.type, part.value]));
  const year = Number(parts.year);
  const month = Number(parts.month);
  const day = Number(parts.day);
  return {
    year,
    month,
    day,
    hour: Number(parts.hour),
    minute: Number(parts.minute),
    dayOfWeek: new Date(Date.UTC(year, month - 1, day)).getUTCDay(),
  };
}

function triggerJob(jobId) {
  const args = ["cron", "run", jobId, "--url", gatewayUrl, "--timeout", String(cliTimeoutMs)];
  if (process.env.OPENCLAW_GATEWAY_TOKEN) args.push("--token", process.env.OPENCLAW_GATEWAY_TOKEN);
  const result = spawnSync("openclaw", args, {
    encoding: "utf8",
    timeout: cliTimeoutMs + 5_000,
    env: process.env,
  });
  return {
    ok: result.status === 0,
    status: result.status,
    error: result.status === 0 ? undefined : redact((result.stderr || result.stdout || result.error?.message || "unknown error").trim()),
  };
}

function readJson(file, fallback) {
  try {
    return JSON.parse(fs.readFileSync(file, "utf8"));
  } catch {
    return fallback;
  }
}

function writeJsonAtomic(file, value) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const tmp = `${file}.tmp-${process.pid}`;
  fs.writeFileSync(tmp, `${JSON.stringify(value, null, 2)}\n`);
  fs.renameSync(tmp, file);
}

function logEvent(event) {
  fs.mkdirSync(path.dirname(eventsPath), { recursive: true });
  fs.appendFileSync(eventsPath, `${JSON.stringify({ ts: new Date().toISOString(), ...event })}\n`);
  if (event.type !== "runner_error") console.log(`${event.type} jobId=${event.jobId} slot=${event.slotKey}`);
}

function positiveInt(value, fallback) {
  const parsed = Number.parseInt(value, 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

function pad2(value) {
  return String(value).padStart(2, "0");
}

function redact(text) {
  return text.replace(/(token|secret|password|authorization|cookie)(=|:|\s+)[^\s,}]+/gi, "$1$2<redacted>");
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
