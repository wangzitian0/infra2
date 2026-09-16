// Replays a UTC day of traffic against cloudflare/infra-watchdog/worker.js with an
// in-memory KV and a fake clock, and prints the put() counts as JSON.
//
//   node watchdog_kv_budget_harness.mjs <worker.mjs> <vars.json>
//
// Used by libs/tests/test_cloudflare_watchdog_kv_budget.py; the worker file must be
// an ES module copy (.mjs) because worker.js has no package.json beside it.
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

const [, , workerPath, varsPath] = process.argv;
const worker = (await import(pathToFileURL(workerPath).href)).default;
const vars = JSON.parse(readFileSync(varsPath, "utf-8"));

const DAY_START = Date.UTC(2026, 8, 16, 0, 0, 0);
const DAY_MS = 86400 * 1000;
// The probe runner: a liveness ping, a probe round of PROBE_MS, the verdict, then a
// 60 s sleep (tools/infra_probe_runner.py, observed ~6 s rounds on 2026-09-16).
const PROBE_MS = 6000;
const SLEEP_MS = 60000;
const CRON_MS = 30 * 60 * 1000;

let clock = DAY_START;
Date.now = () => clock;
// The worker logs structured events with console.log; stdout carries only the result.
console.log = () => {};

class MemoryKV {
  constructor() {
    this.map = new Map();
    this.puts = {};
  }
  async get(key) {
    return this.map.has(key) ? this.map.get(key) : null;
  }
  async put(key, value) {
    this.puts[key] = (this.puts[key] || 0) + 1;
    this.map.set(key, value);
  }
  async delete(key) {
    this.map.delete(key);
  }
  total() {
    return Object.values(this.puts).reduce((sum, n) => sum + n, 0);
  }
}

function makeEnv(kv) {
  return {
    ...vars,
    WATCHDOG_STATE: kv,
    HEARTBEAT_TOKEN: "hb-token",
    WATCHDOG_RETRY_DELAY_MS: "0",
    ALERT_DELIVERY_MODE: "feishu_webhook",
    FEISHU_WEBHOOK_URL: "https://feishu.invalid/hook",
  };
}

const heartbeats = JSON.parse(vars.WATCHDOG_HEARTBEATS_JSON);

async function post(env, body) {
  const request = new Request("https://watchdog.invalid/heartbeat", {
    method: "POST",
    headers: { Authorization: "Bearer hb-token", "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const response = await worker.fetch(request, env);
  return { status: response.status, body: await response.json() };
}

function stored(kv, heartbeat) {
  const raw = kv.map.get(`heartbeat:${heartbeat.environment}:${heartbeat.name}`);
  return raw ? JSON.parse(raw) : null;
}

// One runner posting for a day. `verdict(i)` is the probe result of round i;
// `livenessMode` is "flagged" (current runner), "legacy" (unflagged fixed detail)
// or "none" (verdict posts only). Checkpoints sample what the 30-min cron would read.
async function runnerDay(heartbeat, { verdict, livenessMode = "flagged", verdicts = true, seed = null }) {
  const kv = new MemoryKV();
  const env = makeEnv(kv);
  if (seed) {
    kv.map.set(`heartbeat:${heartbeat.environment}:${heartbeat.name}`, JSON.stringify(seed));
  }
  clock = DAY_START;
  let nextCheckpoint = DAY_START + CRON_MS;
  const samples = [];
  const firstVerdict = [];
  for (let round = 0; clock < DAY_START + DAY_MS; round += 1) {
    if (livenessMode !== "none") {
      const ping = {
        env: heartbeat.environment,
        name: heartbeat.name,
        ok: true,
        detail: "probe loop iteration starting",
        timestamp: Math.floor(clock / 1000),
      };
      if (livenessMode === "flagged") {
        ping.liveness = true;
      }
      await post(env, ping);
    }
    clock += PROBE_MS;
    if (verdicts) {
      const ok = verdict(round);
      if (firstVerdict.length === 0) {
        firstVerdict.push({ ok, at: clock });
      }
      await post(env, {
        env: heartbeat.environment,
        name: heartbeat.name,
        ok,
        detail: ok ? "probe loop completed" : "probe loop failed",
        timestamp: Math.floor(clock / 1000),
      });
    }
    clock += SLEEP_MS;
    while (nextCheckpoint <= clock && nextCheckpoint < DAY_START + DAY_MS) {
      const record = stored(kv, heartbeat);
      samples.push({
        ok: record ? record.ok !== false : null,
        ageSeconds: record ? Math.floor((nextCheckpoint - record.receivedAt) / 1000) : null,
      });
      nextCheckpoint += CRON_MS;
    }
  }
  return { puts: kv.total(), samples, kv };
}

function summarize({ puts, samples }) {
  return {
    puts,
    storedOk: samples.map((sample) => sample.ok),
    maxAgeSeconds: Math.max(...samples.map((sample) => sample.ageSeconds ?? Infinity)),
  };
}

const results = {};
const heartbeat = heartbeats[0];

results.healthy = summarize(await runnerDay(heartbeat, { verdict: () => true }));
results.failing = summarize(await runnerDay(heartbeat, { verdict: () => false }));
results.failingLegacyRunner = summarize(
  await runnerDay(heartbeat, { verdict: () => false, livenessMode: "legacy" }),
);
results.flapping = summarize(await runnerDay(heartbeat, { verdict: (round) => round % 2 === 0 }));
results.flappingNoLiveness = summarize(
  await runnerDay(heartbeat, { verdict: (round) => round % 2 === 0, livenessMode: "none" }),
);
// Probe rounds hang: only liveness pings arrive; the stored failing verdict must survive.
results.livenessOnly = summarize(
  await runnerDay(heartbeat, {
    verdict: () => true,
    verdicts: false,
    seed: {
      environment: heartbeat.environment,
      name: heartbeat.name,
      ok: false,
      detail: "probe loop failed",
      timestamp: 0,
      receivedAt: DAY_START - 1000,
    },
  }),
);

// A failing verdict is stored; the first healthy verdict must replace it at once.
{
  const kv = new MemoryKV();
  const env = makeEnv(kv);
  clock = DAY_START;
  const base = { env: heartbeat.environment, name: heartbeat.name, timestamp: 1 };
  await post(env, { ...base, ok: false, detail: "probe loop failed" });
  clock += 66000;
  await post(env, { ...base, ok: true, detail: "probe loop iteration starting", liveness: true });
  const afterPing = stored(kv, heartbeat).ok;
  clock += 6000;
  const recovered = await post(env, { ...base, ok: true, detail: "probe loop completed" });
  results.recovery = {
    okAfterPing: afterPing,
    okAfterVerdict: stored(kv, heartbeat).ok,
    reason: recovered.body.reason,
    puts: kv.total(),
  };
}

// A name the cron never reads is refused and costs no put.
{
  const kv = new MemoryKV();
  const env = makeEnv(kv);
  clock = DAY_START;
  const response = await post(env, { env: heartbeat.environment, name: "rogue-runner", ok: true });
  results.unconfigured = { status: response.status, puts: kv.total() };
}

// 48 cron runs whose heartbeat verdicts alternate, so every run changes the alert
// state (the most alert-state puts a day can cost).
{
  const kv = new MemoryKV();
  const env = makeEnv(kv);
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response("ok", { status: 200 });
  try {
    for (let run = 0; run < 48; run += 1) {
      clock = DAY_START + run * CRON_MS;
      for (const configured of heartbeats) {
        kv.map.set(
          `heartbeat:${configured.environment}:${configured.name}`,
          JSON.stringify({ ok: run % 2 === 0, detail: "", receivedAt: clock - 1000 }),
        );
      }
      const pending = [];
      await worker.scheduled({ scheduledTime: clock }, env, { waitUntil: (p) => pending.push(p) });
      await Promise.all(pending);
    }
  } finally {
    globalThis.fetch = originalFetch;
  }
  results.cronDay = { puts: kv.total(), putsByKey: kv.puts };
}

results.heartbeatKeys = heartbeats.length;
process.stdout.write(`${JSON.stringify(results)}\n`);
