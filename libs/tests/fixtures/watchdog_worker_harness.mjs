// Drives cloudflare/infra-watchdog/worker.js under node against an in-memory KV,
// a fake clock and a fake network, and prints what each scenario observed as JSON.
//
//   node watchdog_worker_harness.mjs <worker.mjs> <vars.json>
//
// Used by libs/tests/test_cloudflare_watchdog.py. Every number it reports is
// counted at the boundary the Cloudflare limits apply to: a subrequest is one
// fetch() or one KV operation made during one scheduled() invocation.
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

const [, , workerPath, varsPath] = process.argv;
const worker = (await import(pathToFileURL(workerPath).href)).default;
const vars = JSON.parse(readFileSync(varsPath, "utf-8"));

const START = Date.UTC(2026, 8, 24, 0, 0, 0);
const MIN = 60 * 1000;
const CRON_MS = 30 * MIN;
const HOUR = 60 * MIN;
const FEISHU = "https://feishu.invalid";
const DEADMAN = "https://hc-ping.com/test-worker-check";
const PROD_HB = "heartbeat:production:platform-alerting-probes";
const STAGING_HB = "heartbeat:staging:platform-alerting-probes-staging";
const TARGETS = JSON.parse(vars.WATCHDOG_TARGETS_JSON);

let clock = START;
Date.now = () => clock;
console.log = () => {};

// Timers: the worker's abort timers are cleared before they fire; a timer whose
// callback runs during a cron invocation is an in-run sleep.
const realSetTimeout = globalThis.setTimeout;
const realClearTimeout = globalThis.clearTimeout;
let firedTimers = [];
globalThis.setTimeout = (callback, ms, ...args) =>
  realSetTimeout(() => {
    firedTimers.push(ms);
    callback(...args);
  }, 0);
globalThis.clearTimeout = (handle) => realClearTimeout(handle);

class World {
  constructor({ overrides = {}, withoutConfigVars = false } = {}) {
    this.map = new Map();
    this.ops = { get: 0, put: 0, delete: 0 };
    this.putsByKey = {};
    this.failPut = false;
    this.routes = {}; // url -> status (default 200)
    this.feishuFails = false;
    this.feishuSendFails = false;
    this.resendFails = false;
    this.fetches = [];
    this.messages = [];
    const base = { ...vars };
    if (withoutConfigVars) {
      delete base.WATCHDOG_TARGETS_JSON;
      delete base.WATCHDOG_HEARTBEATS_JSON;
    }
    const world = this;
    this.env = {
      ...base,
      ALERT_DELIVERY_MODE: "feishu_app",
      FEISHU_APP_ID: "app",
      FEISHU_APP_SECRET: "secret",
      FEISHU_CHAT_ID: "chat",
      FEISHU_API_BASE: FEISHU,
      ALERT_EMAIL_TO: "",
      RESEND_API_KEY: "",
      HEARTBEAT_TOKEN: "hb-token",
      WATCHDOG_STATUS_TOKEN: "status-token",
      WATCHDOG_DEADMAN_PING_URL: DEADMAN,
      ...overrides,
      WATCHDOG_STATE: {
        async get(key) {
          world.ops.get += 1;
          return world.map.has(key) ? world.map.get(key) : null;
        },
        async put(key, value) {
          world.ops.put += 1;
          if (world.failPut) throw new Error("KV put() limit exceeded for the day.");
          world.putsByKey[key] = (world.putsByKey[key] || 0) + 1;
          world.map.set(key, value);
        },
        async delete(key) {
          world.ops.delete += 1;
          world.map.delete(key);
        },
      },
    };
  }

  heartbeat(key, record) {
    this.map.set(key, JSON.stringify(record));
  }

  async fetchStub(input, init = {}) {
    const url = String(input);
    this.fetches.push(url);
    if (url.startsWith(DEADMAN)) return new Response("ok", { status: 200 });
    if (url.startsWith(`${FEISHU}/open-apis/auth/`)) {
      if (this.feishuFails) return new Response(JSON.stringify({ code: 99991663 }), { status: 500 });
      return new Response(JSON.stringify({ code: 0, tenant_access_token: "t" }), { status: 200 });
    }
    if (url.startsWith(`${FEISHU}/open-apis/im/`)) {
      if (this.feishuSendFails) return new Response(JSON.stringify({ code: 230002 }), { status: 400 });
      this.messages.push(JSON.parse(JSON.parse(init.body).content).text);
      return new Response(JSON.stringify({ code: 0 }), { status: 200 });
    }
    if (url.startsWith("https://api.resend.com/")) {
      if (this.resendFails) return new Response("no", { status: 500 });
      this.messages.push(`EMAIL ${JSON.parse(init.body).text}`);
      return new Response("{}", { status: 200 });
    }
    const status = this.routes[url] ?? 200;
    if (status === "network") throw new Error("connection refused");
    return new Response(`status ${status}`, { status });
  }

  // One cron invocation, with everything it cost.
  async cron(at) {
    clock = at;
    const before = { ...this.ops, fetches: this.fetches.length, messages: this.messages.length };
    const putsBefore = { ...this.putsByKey };
    firedTimers = [];
    const original = globalThis.fetch;
    globalThis.fetch = (input, init) => this.fetchStub(input, init);
    let error = null;
    try {
      const pending = [];
      await worker.scheduled({ scheduledTime: at }, this.env, { waitUntil: (p) => pending.push(p) });
      await Promise.all(pending);
    } catch (caught) {
      error = String(caught && caught.message ? caught.message : caught);
    } finally {
      globalThis.fetch = original;
    }
    const fetched = this.fetches.slice(before.fetches);
    const kvGets = this.ops.get - before.get;
    const kvPuts = this.ops.put - before.put;
    const kvDeletes = this.ops.delete - before.delete;
    return {
      error,
      fetched,
      kvGets,
      kvPuts,
      kvDeletes,
      subrequests: fetched.length + kvGets + kvPuts + kvDeletes,
      putKeys: Object.keys(this.putsByKey).filter((k) => this.putsByKey[k] !== (putsBefore[k] || 0)),
      messages: this.messages.slice(before.messages),
      firedTimers: [...firedTimers],
    };
  }

  async request(method, path, { body, token } = {}) {
    const headers = { "Content-Type": "application/json" };
    if (token) headers.Authorization = `Bearer ${token}`;
    const response = await worker.fetch(
      new Request(`https://watchdog.invalid${path}`, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
      }),
      this.env,
    );
    let json = null;
    try {
      json = await response.json();
    } catch (_error) {
      json = null;
    }
    return { status: response.status, body: json };
  }

  status(at) {
    clock = at;
    return this.request("GET", "/status", { token: "status-token" });
  }

  outages(at) {
    clock = at;
    return this.request("GET", "/outages", { token: "status-token" });
  }

  post(at, body) {
    clock = at;
    return this.request("POST", "/heartbeat", { body, token: "hb-token" });
  }

  stored(key) {
    const raw = this.map.get(key);
    return raw ? JSON.parse(raw) : null;
  }
}

// A heartbeat record as the Worker stores it for a v2 runner that posted at `at`
// and last delivered an alert at that moment.
function record(at, fields = {}) {
  return {
    ok: true,
    detail: "probe loop completed",
    receivedAt: at,
    schema: 2,
    failingPublicRoutes: [],
    lastDeliveryOkAt: Math.floor(at / 1000),
    ...fields,
  };
}

function freshHeartbeats(world, at, fields = {}) {
  world.heartbeat(PROD_HB, record(at - MIN, fields));
  world.heartbeat(STAGING_HB, record(at - MIN));
}

const url = (name) => TARGETS.find((t) => t.name === name).url;
const results = {};

// A healthy run: what it touches and what it costs.
{
  const world = new World();
  freshHeartbeats(world, START);
  results.quiet = await world.cron(START);
}

// The effective config is the built-in default: no drift between the two (#901 M2).
{
  const configured = new World();
  freshHeartbeats(configured, START);
  const defaults = new World({ withoutConfigVars: true });
  freshHeartbeats(defaults, START);
  const a = await configured.cron(START);
  const b = await defaults.cron(START);
  const status = await defaults.status(START + MIN);
  results.defaults = {
    configuredFetched: a.fetched,
    defaultFetched: b.fetched,
    defaultHeartbeats: status.body.heartbeats.map((h) => `${h.environment}:${h.name}`),
    configuredHeartbeats: JSON.parse(vars.WATCHDOG_HEARTBEATS_JSON).map((h) => `${h.environment}:${h.name}`),
  };
}

// One entrypoint down: debounce, dedupe, 6 h renotify, RESOLVED naming it.
{
  const world = new World();
  const down = url("dokploy-public-route");
  world.routes[down] = 502;
  const runs = [];
  for (const at of [START, START + CRON_MS, START + 2 * CRON_MS, START + 6 * HOUR + CRON_MS]) {
    freshHeartbeats(world, at);
    runs.push(await world.cron(at));
  }
  delete world.routes[down];
  freshHeartbeats(world, START + 7 * HOUR);
  runs.push(await world.cron(START + 7 * HOUR));
  results.entrypointDown = runs.map((run) => ({ messages: run.messages, putKeys: run.putKeys, error: run.error }));
}

// A network error counts the same as a wrong status.
{
  const world = new World();
  world.routes[url("truealpha-web-public-route")] = "network";
  freshHeartbeats(world, START);
  await world.cron(START);
  freshHeartbeats(world, START + CRON_MS);
  results.networkError = (await world.cron(START + CRON_MS)).messages;
}

// The VPS already reports the route paged and failing: suppress, and say why.
// Suppression is re-evaluated every run: once the heartbeat stops listing the
// route, the Worker pages it at once.
{
  const world = new World();
  const down = url("finance-report-web-public-route");
  world.routes[down] = 503;
  const runs = [];
  for (const at of [START, START + CRON_MS, START + 2 * CRON_MS]) {
    freshHeartbeats(world, at, { failingPublicRoutes: ["finance-report-web-public-route"] });
    runs.push(await world.cron(at));
  }
  const status = await world.status(START + 2 * CRON_MS + MIN);
  freshHeartbeats(world, START + 3 * CRON_MS, { failingPublicRoutes: [] });
  const released = await world.cron(START + 3 * CRON_MS);
  results.suppressed = {
    messages: runs.flatMap((run) => run.messages),
    stateWrites: runs.filter((run) => run.putKeys.includes("watchdog:state")).length,
    lastRunSuppressed: status.body.lastRun.suppressed,
    entrypoints: status.body.entrypoints,
    released: released.messages,
  };
}

// When the Worker must not believe the list (#911 review). Each case: the
// entrypoint fails two runs while the fresh v2 heartbeat says what it says.
const FR = "finance-report-web-public-route";
async function twoFailingRuns(fields) {
  const world = new World();
  world.routes[url(FR)] = 503;
  const messages = [];
  for (const at of [START, START + CRON_MS]) {
    freshHeartbeats(world, at, typeof fields === "function" ? fields(at) : fields);
    messages.push(...(await world.cron(at)).messages);
  }
  return messages;
}
results.notSuppressed = {
  // the loop itself is unhealthy: its list proves nothing was delivered
  loopUnhealthy: await twoFailingRuns({ ok: false, detail: "bridge delivery failed: HTTP 502", failingPublicRoutes: [FR] }),
  // nothing delivered since before the last run that saw the route healthy
  deliveryPredatesFailure: await twoFailingRuns({ failingPublicRoutes: [FR], lastDeliveryOkAt: Math.floor((START - CRON_MS - MIN) / 1000) }),
  // nothing ever delivered
  neverDelivered: await twoFailingRuns({ failingPublicRoutes: [FR], lastDeliveryOkAt: 0 }),
  // maintenance: the runner pages nothing, so it lists nothing
  maintenance: await twoFailingRuns({ detail: "probe loop completed; alerts suppressed during maintenance", failingPublicRoutes: [] }),
  // the runner paged another route, never this one
  neverPaged: await twoFailingRuns({ failingPublicRoutes: ["vault-public-route"] }),
};
// Suppressed while the VPS pages it, then the VPS dies: its last record still
// lists the route (and a recent-enough delivery), but a stale heartbeat speaks for
// nothing, so the entrypoint pages once the record goes stale.
{
  const world = new World();
  world.routes[url(FR)] = 503;
  const runs = [];
  for (let run = 0; run < 6; run += 1) {
    const at = START + run * CRON_MS;
    if (run < 3) freshHeartbeats(world, at, { failingPublicRoutes: [FR] });
    runs.push({ at, messages: (await world.cron(at)).messages });
  }
  results.suppressedThenVpsDies = runs.map((run) => ({
    minutes: (run.at - START) / MIN,
    entrypointFiring: run.messages.some((m) => m.includes(`FIRING P0 host-reachability production/${FR}`)),
    vpsDown: run.messages.some((m) => m.includes("VPS or its egress is down")),
  }));
}

// A delivery after the last healthy run (since - one run) is recent enough.
results.deliveryAfterLastHealthyRun = await twoFailingRuns({
  failingPublicRoutes: [FR],
  lastDeliveryOkAt: Math.floor((START - CRON_MS + MIN) / 1000),
});

// Unknown is not "the VPS has it": a v1 heartbeat or a stale one never suppresses.
for (const [label, fields, age] of [
  ["v1Heartbeat", { schema: 1, failingPublicRoutes: null }, MIN],
  ["staleHeartbeat", { failingPublicRoutes: ["finance-report-web-public-route"] }, 2 * HOUR],
]) {
  const world = new World();
  world.routes[url("finance-report-web-public-route")] = 503;
  const messages = [];
  for (const at of [START, START + CRON_MS]) {
    world.heartbeat(PROD_HB, record(at - age, fields));
    world.heartbeat(STAGING_HB, record(at - MIN));
    messages.push(...(await world.cron(at)).messages);
  }
  results[label] = messages;
}

// Production heartbeat goes stale, then comes back: one P0, one RESOLVED, one
// state write at each edge, and the outage interval on /outages.
{
  const world = new World();
  const lastContact = START + 5 * MIN;
  world.heartbeat(PROD_HB, record(lastContact));
  world.heartbeat(STAGING_HB, record(lastContact));
  const runs = [];
  // stale from START + 5 min + 5400 s onwards
  for (let at = START + 2 * HOUR; at <= START + 5 * HOUR; at += CRON_MS) {
    runs.push(await world.cron(at));
  }
  const recoveredAt = START + 5 * HOUR + 10 * MIN;
  world.heartbeat(PROD_HB, record(recoveredAt));
  const recovery = await world.cron(START + 5 * HOUR + CRON_MS);
  runs.push(recovery);
  const outages = await world.outages(START + 6 * HOUR);
  results.vpsDown = {
    messages: runs.flatMap((run) => run.messages),
    statePuts: runs.filter((run) => run.putKeys.includes("watchdog:state")).length,
    runs: runs.length,
    outages: outages.body.outages,
    lastContact,
    recoveredAt,
  };
}

// Fresh v2 heartbeat, probe loop unhealthy: P1 after 2 consecutive unhealthy runs,
// RESOLVED after 2 healthy ones, naming the runner's detail.
{
  const world = new World();
  const steps = [
    { ok: false, detail: "alert bridge delivery failing for 12 min" },
    { ok: false, detail: "alert bridge delivery failing for 42 min" },
    { ok: false, detail: "alert bridge delivery failing for 72 min" },
    { ok: true },
    { ok: true },
  ];
  const runs = [];
  for (const [index, fields] of steps.entries()) {
    const at = START + index * CRON_MS;
    freshHeartbeats(world, at, fields);
    runs.push(await world.cron(at));
  }
  results.loopUnhealthy = {
    messages: runs.map((run) => run.messages),
    putKeys: runs.map((run) => run.putKeys),
  };
}

// A loop that flips every run never pages (#911 review: 48 messages a day before).
{
  const world = new World();
  const messages = [];
  for (let run = 0; run < 12; run += 1) {
    const at = START + run * CRON_MS;
    freshHeartbeats(world, at, { ok: run % 2 === 1, detail: run % 2 ? "probe loop completed" : "group raised" });
    messages.push(...(await world.cron(at)).messages);
  }
  results.loopFlapping = messages;
}

// A v1 runner's ok=false means "some probe failed", not a broken loop: unknown.
{
  const world = new World();
  const messages = [];
  for (let run = 0; run < 3; run += 1) {
    const at = START + run * CRON_MS;
    freshHeartbeats(world, at, { ok: false, schema: 1, failingPublicRoutes: null, lastDeliveryOkAt: null, detail: "probe loop failed" });
    messages.push(...(await world.cron(at)).messages);
  }
  results.v1LoopFalse = messages;
}

// Staging is recorded, never paged.
{
  const world = new World();
  world.heartbeat(PROD_HB, record(START - MIN));
  world.heartbeat(STAGING_HB, record(START - 3 * HOUR, { ok: false, detail: "staging loop broken" }));
  const run = await world.cron(START);
  const status = await world.status(START + MIN);
  results.staging = {
    messages: run.messages,
    fetched: run.fetched,
    heartbeats: status.body.heartbeats,
  };
}

// Worst case per run: VPS down and all three entrypoints down, alert delivered.
{
  const world = new World();
  for (const target of TARGETS) world.routes[target.url] = 522;
  world.heartbeat(PROD_HB, record(START - 3 * HOUR));
  const runs = [];
  for (const at of [START, START + CRON_MS, START + 2 * CRON_MS]) runs.push(await world.cron(at));
  results.allDown = runs.map((run) => ({
    subrequests: run.subrequests,
    fetched: run.fetched.length,
    kvGets: run.kvGets,
    kvPuts: run.kvPuts,
    messages: run.messages,
    firedTimers: run.firedTimers,
  }));
}

// Two identities active; one recovers: RESOLVED names only that one.
{
  const world = new World();
  world.routes[url("dokploy-public-route")] = 502;
  freshHeartbeats(world, START, { ok: false, detail: "probe loop failed: iteration failed" });
  await world.cron(START);
  freshHeartbeats(world, START + CRON_MS, { ok: false, detail: "probe loop failed: iteration failed" });
  const fired = await world.cron(START + CRON_MS);
  freshHeartbeats(world, START + 2 * CRON_MS, { ok: true });
  const firstHealthy = await world.cron(START + 2 * CRON_MS);
  freshHeartbeats(world, START + 3 * CRON_MS, { ok: true });
  const partial = await world.cron(START + 3 * CRON_MS);
  const status = await world.status(START + 3 * CRON_MS + MIN);
  results.partialRecovery = {
    fired: fired.messages,
    firstHealthy: firstHealthy.messages,
    resolved: partial.messages,
    stillActive: status.body.alertState.alerts.map((a) => a.identity),
  };
}

// Delivery fails: nothing is marked sent, the run fails, the dead-man gets /fail,
// the next run delivers it.
{
  const world = new World();
  world.routes[url("dokploy-public-route")] = 502;
  freshHeartbeats(world, START);
  await world.cron(START);
  world.feishuFails = true;
  freshHeartbeats(world, START + CRON_MS);
  const failed = await world.cron(START + CRON_MS);
  const statusAfterFailure = await world.status(START + CRON_MS + MIN);
  world.feishuFails = false;
  freshHeartbeats(world, START + 2 * CRON_MS);
  const retried = await world.cron(START + 2 * CRON_MS);
  results.deliveryFailure = {
    error: failed.error,
    deadman: failed.fetched.filter((u) => u.startsWith(DEADMAN)),
    messagesOnFailure: failed.messages,
    statusOk: statusAfterFailure.body.ok,
    lastRunOk: statusAfterFailure.body.lastRun.ok,
    deliveryError: statusAfterFailure.body.lastRun.deliveryError,
    retried: retried.messages,
  };
}

// Feishu down, email configured: the page escalates to email and the run is ok.
{
  const world = new World({ overrides: { ALERT_EMAIL_TO: "oncall-test-inbox", RESEND_API_KEY: "re_test" } });
  world.routes[url("dokploy-public-route")] = 502;
  world.feishuFails = true;
  freshHeartbeats(world, START);
  await world.cron(START);
  freshHeartbeats(world, START + CRON_MS);
  const run = await world.cron(START + CRON_MS);
  // The dearest path: the token succeeds, the send fails, email carries the page.
  const dear = new World({ overrides: { ALERT_EMAIL_TO: "oncall-test-inbox", RESEND_API_KEY: "re_test" } });
  dear.routes[url("dokploy-public-route")] = 502;
  dear.feishuSendFails = true;
  freshHeartbeats(dear, START);
  await dear.cron(START);
  freshHeartbeats(dear, START + CRON_MS);
  const dearest = await dear.cron(START + CRON_MS);
  results.emailEscalation = {
    error: run.error,
    messages: run.messages,
    subrequests: run.subrequests,
    sendFailedError: dearest.error,
    sendFailedMessages: dearest.messages.length,
    sendFailedSubrequests: dearest.subrequests,
  };
}

// /status reports the Worker's own health, not the targets'.
{
  const world = new World();
  world.routes[url("dokploy-public-route")] = 502;
  freshHeartbeats(world, START);
  await world.cron(START);
  freshHeartbeats(world, START + CRON_MS);
  await world.cron(START + CRON_MS);
  const status = await world.status(START + CRON_MS + MIN);
  const stale = await world.status(START + CRON_MS + 3 * HOUR);
  const noToken = await world.request("GET", "/status");
  const outagesNoToken = await world.request("GET", "/outages");
  results.status = {
    ok: status.body.ok,
    lastRun: status.body.lastRun,
    alertActive: status.body.alertState.active,
    staleOk: stale.body.ok,
    noTokenStatus: noToken.status,
    outagesNoTokenStatus: outagesNoToken.status,
  };
}

// Broken config: its own page; alerts it could not evaluate are not "resolved".
{
  const world = new World();
  world.routes[url("dokploy-public-route")] = 502;
  freshHeartbeats(world, START);
  await world.cron(START);
  freshHeartbeats(world, START + CRON_MS);
  await world.cron(START + CRON_MS);
  world.env.WATCHDOG_TARGETS_JSON = "{not json";
  const broken = await world.cron(START + 2 * CRON_MS);
  const status = await world.status(START + 2 * CRON_MS + MIN);
  results.configBroken = {
    messages: broken.messages,
    lastRunOk: status.body.lastRun.ok,
    stillActive: status.body.alertState.alerts.map((a) => a.identity),
  };
}

// Heartbeat contract v2 at the POST boundary.
{
  const world = new World();
  const base = { env: "production", name: "platform-alerting-probes", timestamp: 1 };
  const first = await world.post(START, {
    ...base,
    ok: true,
    detail: "probe loop completed",
    schema: 2,
    last_delivery_ok_at: 1790000000,
    failing_public_routes: ["vault-public-route", "dokploy-public-route", "vault-public-route"],
  });
  const storedFirst = world.stored(PROD_HB);
  // The same verdict a minute later is throttled; a changed route set is a status change.
  const same = await world.post(START + MIN, {
    ...base,
    ok: true,
    schema: 2,
    last_delivery_ok_at: 1790000060,
    failing_public_routes: ["dokploy-public-route", "vault-public-route"],
  });
  const changed = await world.post(START + 2 * MIN, {
    ...base,
    ok: true,
    schema: 2,
    last_delivery_ok_at: 1790000120,
    failing_public_routes: ["vault-public-route"],
  });
  const storedChanged = world.stored(PROD_HB);
  // A liveness ping never rewrites the verdict, whatever it carries.
  const ping = await world.post(START + 3 * HOUR, {
    ...base,
    ok: true,
    liveness: true,
    detail: "probe loop iteration starting",
    schema: 2,
    failing_public_routes: [],
  });
  const storedAfterPing = world.stored(PROD_HB);
  // A v1 runner: the new fields are unknown, whatever else it sends.
  const legacy = new World();
  await legacy.post(START, { ...base, ok: false, detail: "probe loop failed", failing_public_routes: ["x-public-route"] });
  // A malformed route list is unknown, not "nothing failing".
  const malformed = new World();
  await malformed.post(START, { ...base, ok: true, schema: 2, failing_public_routes: ["ok-route", "bad route!"] });
  // A KV put failure degrades visibly instead of an unhandled 500.
  const quota = new World();
  quota.failPut = true;
  const degraded = await quota.post(START, { ...base, ok: true, schema: 2, failing_public_routes: [] });
  results.heartbeatV2 = {
    first: first.body,
    storedFirst,
    same: same.body,
    changed: changed.body,
    storedChanged,
    ping: ping.body,
    storedAfterPing,
    legacyStored: legacy.stored(PROD_HB),
    malformedStored: malformed.stored(PROD_HB),
    degraded: { status: degraded.status, body: degraded.body },
  };
}

// /status in the worst incident stays far inside GitHub's 4096-byte read: every
// identity active, every entrypoint suppressed with a reason, an open outage, a
// delivery error, and heartbeat records a pre-#904 Worker stored untrimmed.
{
  const world = new World();
  const long = "x".repeat(5000);
  const routes = Array.from({ length: 32 }, (_, i) => `r${String(i).padStart(2, "0")}-${"a".repeat(60)}`);
  const alerts = {};
  const identities = [
    "global:cloudflare-watchdog:config-preflight",
    "production:platform-alerting-probes:heartbeat-stale",
    "production:platform-alerting-probes:heartbeat-unhealthy",
    ...TARGETS.map((t) => `production:${t.name}:entrypoint`),
  ];
  for (const identity of identities) {
    alerts[identity] = { environment: "production", name: identity.split(":")[1], failureClass: "host-reachability", severity: "P0", since: START, lastAlertAt: START };
  }
  const entrypoints = {};
  for (const t of TARGETS) {
    entrypoints[`production:${t.name}`] = { failures: 2, since: START, suppressedReason: long };
  }
  world.map.set("watchdog:state", JSON.stringify({
    schema: 2,
    alerts,
    entrypoints,
    loops: { "production:platform-alerting-probes": { bad: 2, good: 1 } },
    outages: [{ environment: "production", name: "platform-alerting-probes", start: START, detectedAt: START, end: null, reason: "stale" }],
    lastRun: { ranAt: START, ok: false, routeTargetCount: 3, heartbeatTargetCount: 2, failureCount: 7, activeAlertCount: 6, suppressed: TARGETS.map((t) => t.name), deliveryError: long },
  }));
  for (const key of [PROD_HB, STAGING_HB]) {
    world.heartbeat(key, { ok: false, detail: long, receivedAt: START, schema: 2, failingPublicRoutes: routes, lastDeliveryOkAt: 1 });
  }
  const status = await world.status(START + MIN);
  // and a runner cannot store an oversized detail through POST
  const posted = new World();
  await posted.post(START, { env: "production", name: "platform-alerting-probes", ok: false, schema: 2, detail: long, failing_public_routes: routes });
  const tooLong = new World();
  await tooLong.post(START, { env: "production", name: "platform-alerting-probes", ok: true, schema: 2, failing_public_routes: [`r-${"a".repeat(80)}`] });
  results.statusSize = {
    status: status.status,
    bytes: Buffer.byteLength(JSON.stringify(status.body)),
    storedDetailLength: posted.stored(PROD_HB).detail.length,
    storedRoutes: posted.stored(PROD_HB).failingPublicRoutes.length,
    overlongRouteList: tooLong.stored(PROD_HB).failingPublicRoutes,
  };
}

// Endpoints that no longer exist.
{
  const world = new World();
  results.removedEndpoints = {
    ledger: (await world.request("GET", "/ledger", { token: "status-token" })).status,
    health: (await world.request("GET", "/health")).body,
  };
}

process.stdout.write(`${JSON.stringify(results)}\n`);
