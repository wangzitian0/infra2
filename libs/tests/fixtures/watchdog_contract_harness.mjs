// Posts heartbeats the real probe runner produced (tools/infra_probe_runner.py,
// contract v2) to the real Worker, then runs two crons with the runner's failing
// route unreachable from Cloudflare, and prints the pages each case produced.
//
//   node watchdog_contract_harness.mjs <worker.mjs> <vars.json> <beats.json>
//
// beats.json: {"<case>": <the runner's last verdict POST body>, ...}. Used by
// libs/tests/test_heartbeat_contract.py.
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

const [, , workerPath, varsPath, beatsPath] = process.argv;
const worker = (await import(pathToFileURL(workerPath).href)).default;
const vars = JSON.parse(readFileSync(varsPath, "utf-8"));
const beats = JSON.parse(readFileSync(beatsPath, "utf-8"));
const TARGETS = JSON.parse(vars.WATCHDOG_TARGETS_JSON);

let clock = 0;
Date.now = () => clock;
console.log = () => {};

const results = {};
for (const [label, beat] of Object.entries(beats)) {
  const map = new Map();
  const messages = [];
  const env = {
    ...vars,
    WATCHDOG_STATE: {
      async get(key) { return map.has(key) ? map.get(key) : null; },
      async put(key, value) { map.set(key, value); },
      async delete(key) { map.delete(key); },
    },
    HEARTBEAT_TOKEN: "hb-token",
    WATCHDOG_STATUS_TOKEN: "status-token",
    WATCHDOG_DEADMAN_PING_URL: "https://hc-ping.com/test-worker-check",
    ALERT_DELIVERY_MODE: "feishu_webhook",
    FEISHU_WEBHOOK_URL: "https://feishu.invalid/hook",
  };
  const failing = new Set(
    TARGETS.filter((t) => (beat.failing_route_under_test || []).includes(t.name)).map((t) => t.url),
  );
  const original = globalThis.fetch;
  globalThis.fetch = async (input, init = {}) => {
    const url = String(input);
    if (url.startsWith("https://feishu.invalid/")) {
      messages.push(JSON.parse(init.body).content.text);
      return new Response("ok", { status: 200 });
    }
    if (url.startsWith("https://hc-ping.com/")) return new Response("ok", { status: 200 });
    return new Response("", { status: failing.has(url) ? 503 : 200 });
  };
  try {
    const { failing_route_under_test: _unused, ...body } = beat;
    clock = body.timestamp * 1000;
    const response = await worker.fetch(
      new Request("https://watchdog.invalid/heartbeat", {
        method: "POST",
        headers: { Authorization: "Bearer hb-token", "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }),
      env,
    );
    const stored = JSON.parse(map.get(`heartbeat:${body.env}:${body.name}`) || "null");
    for (const offset of [60e3, 31 * 60e3]) {
      clock = body.timestamp * 1000 + offset;
      const pending = [];
      await worker.scheduled({ scheduledTime: clock }, env, { waitUntil: (p) => pending.push(p) });
      await Promise.all(pending);
    }
    results[label] = {
      status: response.status,
      storedRoutes: stored ? stored.failingPublicRoutes : null,
      storedDeliveryAt: stored ? stored.lastDeliveryOkAt : null,
      entrypointPages: messages.filter((m) => /级别：P0\n环境：production\n对象：\S+ · \S+-public-route\n现象：external entrypoint /.test(m)).length,
      loopPages: messages.filter((m) => /级别：P1\n环境：production\n对象：\S+ · \S+\n现象：probe loop reports unhealthy/.test(m)).length,
    };
  } finally {
    globalThis.fetch = original;
  }
}
process.stdout.write(`${JSON.stringify(results)}\n`);
