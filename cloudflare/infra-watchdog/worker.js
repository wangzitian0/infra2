// Infra2 out-of-band watchdog on Cloudflare Workers Cron (ops.observability.md §1.1, #904).
//
// The Worker judges only what the VPS cannot report about itself:
//   - the production probe-runner heartbeat is stale: P0 host-reachability
//     ("VPS or its egress is down");
//   - the production heartbeat says the probe loop is unhealthy: P1 alert-pipeline;
//   - one external entrypoint per product fails 2 consecutive runs: P0
//     host-reachability, unless the fresh production heartbeat lists that route
//     among its own failing in-band probes (then the VPS has paged it already).
// Staging heartbeats are recorded for /status and never paged. The route
// inventory, retries, availability counting and reports all live on the VPS.
//
// Per cron run: one GET per entrypoint (no retry, no sleep), one KV read per
// production heartbeat, one KV read of the state document, one KV write (the
// state document when something transitioned, otherwise the last-run record),
// the Healthchecks.io ping, and on an alert run one Feishu delivery.

const DEFAULT_TARGETS = [
  ["production", "dokploy-public-route", "bootstrap/dokploy", "https://cloud.zitian.party", [200, 302]],
  ["production", "finance-report-web-public-route", "finance_report/app", "https://report.zitian.party/", [200, 302, 307, 308]],
  ["production", "truealpha-web-public-route", "truealpha/app", "https://truealpha.club/", [200, 302, 307, 308]],
].map(([environment, name, service_id, url, statuses]) => ({
  environment,
  name,
  service_id,
  url,
  statuses,
  severity: "critical",
}));

const DEFAULT_HEARTBEATS = [
  {
    environment: "production",
    name: "platform-alerting-probes",
    service_id: "platform/alerting",
    maxAgeSeconds: 5400,
    severity: "critical",
  },
  {
    environment: "staging",
    name: "platform-alerting-probes-staging",
    service_id: "platform/alerting",
    maxAgeSeconds: 5400,
    severity: "warning",
  },
];

// Only this environment pages. Everything else is recorded for /status.
const PAGED_ENVIRONMENT = "production";
const STATE_KEY = "watchdog:state";
const LAST_RUN_KEY = "watchdog:last-run";
const DEFAULT_RENOTIFY_SECONDS = 21600;
const DEFAULT_ENTRYPOINT_FAILURE_RUNS = 2;
const OUTAGE_EDGES_KEPT = 50;
const MAX_FAILING_PUBLIC_ROUTES = 64;
const STATE_SCHEMA = 2;

export default {
  async scheduled(controller, env, ctx) {
    ctx.waitUntil(runScheduledWatchdog(env, controller.scheduledTime || Date.now()));
  },

  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === "/health") {
      return jsonResponse({ ok: true });
    }
    if (request.method === "GET" && url.pathname === "/status") {
      return statusResponse(request, env);
    }
    if (request.method === "GET" && url.pathname === "/outages") {
      return outagesResponse(request, env);
    }
    if (request.method === "POST" && url.pathname === "/heartbeat") {
      return recordHeartbeat(request, env);
    }
    return jsonResponse({ ok: false, error: "not found" }, 404);
  },
};

async function runScheduledWatchdog(env, nowMs) {
  let runError = null;
  try {
    await runWatchdog(env, nowMs);
  } catch (error) {
    runError = error;
  }
  try {
    await pingSchedulerDeadman(env, runError === null);
  } catch (error) {
    logEvent({
      event: "watchdog.deadman.failure",
      timestamp: nowMs,
      status: "fail",
      error: errorText(error),
    });
    if (runError === null) runError = error;
  }
  if (runError !== null) throw runError;
}

async function pingSchedulerDeadman(env, ok) {
  const url = String(env.WATCHDOG_DEADMAN_PING_URL || "").trim();
  if (!/^https:\/\/hc-ping\.com\/[A-Za-z0-9_-]+$/.test(url)) {
    throw new Error("WATCHDOG_DEADMAN_PING_URL is missing or invalid");
  }
  let response;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 8000);
  try {
    response = await fetch(`${url}${ok ? "" : "/fail"}`, {
      method: "GET",
      redirect: "error",
      signal: controller.signal,
    });
  } catch (_error) {
    throw new Error("external scheduler heartbeat request failed");
  } finally {
    clearTimeout(timeout);
  }
  if (!response.ok) throw new Error("external scheduler heartbeat returned an error");
}

// ---------------------------------------------------------------------------
// Cron run
// ---------------------------------------------------------------------------

async function runWatchdog(env, nowMs = Date.now()) {
  const config = loadConfig(env);
  const state = normalizeState(await loadState(env, STATE_KEY));
  const before = JSON.stringify(withoutRunRecord(state));

  const configOk = config.problems.length === 0;
  const observations = [configObservation(config.problems)];
  if (configOk) {
    observations.push(...(await observe(env, config, state, nowMs)));
  }

  const renotifyMs = budgetVar(env.WATCHDOG_RENOTIFY_SECONDS, DEFAULT_RENOTIFY_SECONDS, { min: 600 }) * 1000;
  // With a broken config nothing else was evaluated, so nothing else may resolve.
  const plan = planAlerts(state.alerts, observations, nowMs, renotifyMs, { resolveUnobserved: configOk });
  let deliveryError = "";
  if (plan.events.length > 0) {
    try {
      const kind = plan.events.every((event) => event.kind === "resolved") ? "recovered" : "failure";
      await deliverAlert(env, formatAlertMessage(plan.events), kind);
      state.alerts = plan.alerts;
    } catch (error) {
      // Nothing undelivered is marked sent: the next run retries it.
      deliveryError = errorText(error);
    }
  }

  const failing = observations.filter((o) => o.status === "failing");
  const suppressed = observations.filter((o) => o.suppressedReason);
  const runRecord = {
    ranAt: nowMs,
    ok: configOk && !deliveryError,
    routeTargetCount: config.targets.length,
    heartbeatTargetCount: config.heartbeats.length,
    failureCount: failing.length,
    activeAlertCount: Object.keys(state.alerts).length,
    suppressed: suppressed.map((o) => o.name),
    deliveryError,
  };
  // One write per run. A transition rewrites the state document (it carries the
  // run record too); a quiet run refreshes only the small last-run record that
  // GitHub reads for the Worker's own freshness.
  const changed = JSON.stringify(withoutRunRecord(state)) !== before;
  if (env.WATCHDOG_STATE) {
    if (changed) {
      state.lastRun = runRecord;
      await env.WATCHDOG_STATE.put(STATE_KEY, JSON.stringify(state));
    } else {
      await env.WATCHDOG_STATE.put(LAST_RUN_KEY, JSON.stringify(runRecord));
    }
  }
  logEvent({
    event: "watchdog.run",
    timestamp: nowMs,
    status: runRecord.ok && failing.length === 0 ? "ok" : "fail",
    routeTargetCount: runRecord.routeTargetCount,
    heartbeatTargetCount: runRecord.heartbeatTargetCount,
    failing: failing.map((o) => o.identity),
    suppressed: runRecord.suppressed,
    fired: plan.events.filter((e) => e.kind === "firing").map((e) => e.identity),
    renotified: plan.events.filter((e) => e.kind === "renotify").map((e) => e.identity),
    resolved: plan.events.filter((e) => e.kind === "resolved").map((e) => e.identity),
    activeAlertCount: runRecord.activeAlertCount,
    kvWrite: env.WATCHDOG_STATE ? (changed ? STATE_KEY : LAST_RUN_KEY) : "",
    deliveryError,
  });
  if (deliveryError) {
    throw new Error(`watchdog delivery failed: ${deliveryError}`);
  }
  return { observations, events: plan.events, state };
}

function loadConfig(env) {
  const problems = [];
  let targets = [];
  let heartbeats = [];
  try {
    const environments = enabledEnvironments(env);
    targets = filterByEnvironment(parseJsonList(env.WATCHDOG_TARGETS_JSON, DEFAULT_TARGETS), environments);
    heartbeats = filterByEnvironment(parseJsonList(env.WATCHDOG_HEARTBEATS_JSON, DEFAULT_HEARTBEATS), environments);
  } catch (error) {
    problems.push(`config-preflight failed: ${errorText(error)}`);
  }
  if (problems.length === 0) {
    if (targets.length === 0) problems.push("effective entrypoint target list is empty");
    if (heartbeats.length === 0) problems.push("effective heartbeat target list is empty");
  }
  if (!env.WATCHDOG_STATE) problems.push("WATCHDOG_STATE KV binding is missing");
  return { targets, heartbeats, problems };
}

function configObservation(problems) {
  return {
    identity: "global:cloudflare-watchdog:config-preflight",
    environment: "global",
    name: "cloudflare-watchdog-config-preflight",
    service_id: "infra/cloudflare-watchdog",
    failureClass: "watchdog-config",
    severity: "P1",
    status: problems.length > 0 ? "failing" : "ok",
    summary: "the Worker cannot evaluate its checks",
    detail: problems.join("; "),
    recovery: "Worker config valid again",
  };
}

async function observe(env, config, state, nowMs) {
  const timeoutMs = budgetVar(env.WATCHDOG_HTTP_TIMEOUT_MS, 8000, { min: 1000, max: 20000 });
  const threshold = Math.floor(
    budgetVar(env.WATCHDOG_ENTRYPOINT_FAILURE_RUNS, DEFAULT_ENTRYPOINT_FAILURE_RUNS, { min: 1, max: 6 }),
  );
  const pagedTargets = config.targets.filter((t) => t.environment === PAGED_ENVIRONMENT);
  const pagedHeartbeats = config.heartbeats.filter((h) => h.environment === PAGED_ENVIRONMENT);

  const [probes, records] = await Promise.all([
    Promise.all(pagedTargets.map((target) => checkEntrypoint(target, timeoutMs))),
    Promise.all(pagedHeartbeats.map((hb) => env.WATCHDOG_STATE.get(heartbeatKey(hb.environment, hb.name)))),
  ]);

  const observations = [];
  // Routes the VPS itself reports failing, from any fresh v2 production heartbeat.
  // Unknown (v1, stale, missing) never suppresses anything.
  const vpsFailingRoutes = new Map();
  pagedHeartbeats.forEach((heartbeat, index) => {
    const verdict = heartbeatVerdict(heartbeat, records[index], nowMs);
    const outage = trackOutageEdge(state, heartbeat, verdict, nowMs);
    observations.push(...heartbeatObservations(heartbeat, verdict, outage));
    if (verdict.state === "fresh" && Array.isArray(verdict.record.failingPublicRoutes)) {
      for (const route of verdict.record.failingPublicRoutes) {
        vpsFailingRoutes.set(route, `${heartbeat.environment}/${heartbeat.name}`);
      }
    }
  });
  pagedTargets.forEach((target, index) => {
    observations.push(entrypointObservation(state, target, probes[index], threshold, vpsFailingRoutes, nowMs));
  });
  return observations;
}

async function checkEntrypoint(target, timeoutMs) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  const statuses = Array.isArray(target.statuses) ? target.statuses : [200];
  try {
    const response = await fetch(target.url, {
      method: "GET",
      redirect: "manual",
      signal: controller.signal,
      headers: {
        Accept: "text/html,application/json,text/plain,*/*",
        "User-Agent": "Mozilla/5.0 (compatible; infra2-cloudflare-watchdog/2.0; +https://zitian.party)",
      },
    });
    if (statuses.includes(response.status)) {
      return { ok: true, detail: `HTTP ${response.status}` };
    }
    return {
      ok: false,
      detail: `HTTP ${response.status}; expected ${statuses.join(",")}; body=${await safeBody(response)}`,
    };
  } catch (error) {
    return { ok: false, detail: `fetch failed: ${errorText(error)}` };
  } finally {
    clearTimeout(timeout);
  }
}

function entrypointObservation(state, target, probe, threshold, vpsFailingRoutes, nowMs) {
  const key = `${target.environment}:${target.name}`;
  const base = {
    identity: `${key}:entrypoint`,
    environment: target.environment,
    name: target.name,
    service_id: target.service_id || "infra/unregistered",
    failureClass: "host-reachability",
    severity: "P0",
    summary: `external entrypoint ${target.url} unreachable from Cloudflare`,
    detail: probe.detail,
    recovery: `${target.url} reachable again (${probe.detail})`,
    url: target.url,
  };
  if (probe.ok) {
    delete state.entrypoints[key];
    return { ...base, status: "ok" };
  }
  const previous = state.entrypoints[key] || { failures: 0, since: nowMs };
  // Capped at the threshold, so a sustained outage writes nothing after it pages.
  const failures = Math.min(threshold, Number(previous.failures || 0) + 1);
  // Stored in state, so it must not change from run to run while nothing else does.
  const vpsReport = vpsFailingRoutes.get(target.name);
  const suppressedReason = vpsReport
    ? `the fresh ${vpsReport} heartbeat lists ${target.name} among its failing in-band probes; the VPS pages it`
    : "";
  const entry = { failures, since: Number(previous.since || nowMs) };
  if (suppressedReason) entry.suppressedReason = suppressedReason;
  state.entrypoints[key] = entry;
  if (failures < threshold) {
    return { ...base, status: "pending", since: entry.since, detail: `${probe.detail} (failing run ${failures}/${threshold})` };
  }
  return {
    ...base,
    status: "failing",
    since: entry.since,
    detail: `${probe.detail} (${failures} consecutive runs)`,
    suppressedReason,
  };
}

function heartbeatVerdict(heartbeat, raw, nowMs) {
  if (!raw) {
    return { state: "missing", detail: "heartbeat missing", ageSeconds: null, record: null };
  }
  const record = parseHeartbeatRecord(raw);
  if (!record) {
    return { state: "invalid", detail: "heartbeat payload is invalid JSON", ageSeconds: null, record: null };
  }
  const ageSeconds = Math.floor((nowMs - Number(record.receivedAt || 0)) / 1000);
  const maxAge = Number(heartbeat.maxAgeSeconds || 1800);
  if (ageSeconds < -300) {
    return { state: "future", detail: `heartbeat timestamp is in the future: ${ageSeconds}s old`, ageSeconds, record };
  }
  if (ageSeconds > maxAge) {
    return { state: "stale", detail: `heartbeat stale: ${ageSeconds}s old (max ${maxAge}s)`, ageSeconds, record };
  }
  return { state: "fresh", detail: `heartbeat fresh: ${ageSeconds}s old`, ageSeconds, record };
}

function heartbeatObservations(heartbeat, verdict, outage) {
  const key = `${heartbeat.environment}:${heartbeat.name}`;
  const common = {
    environment: heartbeat.environment,
    name: heartbeat.name,
    service_id: heartbeat.service_id || "infra/unregistered",
  };
  const fresh = verdict.state === "fresh";
  const stale = {
    ...common,
    identity: `${key}:heartbeat-stale`,
    failureClass: "host-reachability",
    severity: "P0",
    status: fresh ? "ok" : "failing",
    since: outage ? outage.start : undefined,
    summary: "VPS or its egress is down",
    detail: verdict.detail,
    recovery: `heartbeat fresh again (${verdict.ageSeconds}s old)`,
  };
  const loopDetail = fresh ? oneLine(verdict.record.detail || "") : "";
  const unhealthy = {
    ...common,
    identity: `${key}:heartbeat-unhealthy`,
    failureClass: "alert-pipeline",
    severity: "P1",
    // A stale record says nothing about the loop now: neither fire nor resolve.
    status: !fresh ? "unknown" : verdict.record.ok === false ? "failing" : "ok",
    summary: "probe loop reports unhealthy",
    detail: loopDetail || "no detail",
    recovery: `probe loop healthy again (${loopDetail || "ok"})`,
  };
  return [stale, unhealthy];
}

// Availability's out-of-band half: the VPS cannot record its own outage, so the
// Worker keeps the down/up edges of the production heartbeat, one write at each
// edge. The outage starts at the last recorded contact (conservative: it may
// include up to one heartbeat write interval of uptime) and ends at the first
// fresh heartbeat after it. Returns the open outage, if any.
function trackOutageEdge(state, heartbeat, verdict, nowMs) {
  const open = state.outages.find(
    (edge) => edge.environment === heartbeat.environment && edge.name === heartbeat.name && edge.end === null,
  );
  const lastSeen = verdict.record ? Number(verdict.record.receivedAt || 0) : 0;
  if (verdict.state === "fresh") {
    if (open) {
      open.end = lastSeen;
      open.recoveredAt = nowMs;
    }
    return null;
  }
  if (open) {
    return open;
  }
  const edge = {
    environment: heartbeat.environment,
    name: heartbeat.name,
    start: lastSeen > 0 && lastSeen <= nowMs ? lastSeen : nowMs,
    detectedAt: nowMs,
    end: null,
    reason: verdict.state,
  };
  state.outages.push(edge);
  state.outages = state.outages.slice(-OUTAGE_EDGES_KEPT);
  return edge;
}

// Alert state per failure identity. Returns the delivery events and the alert map
// to commit once they are delivered.
function planAlerts(currentAlerts, observations, nowMs, renotifyMs, { resolveUnobserved }) {
  const alerts = { ...currentAlerts };
  const events = [];
  const observed = new Map(observations.map((o) => [o.identity, o]));
  for (const obs of observations) {
    if (obs.status !== "failing" || obs.suppressedReason) continue;
    const active = alerts[obs.identity];
    if (!active) {
      const since = Number(obs.since || nowMs);
      alerts[obs.identity] = {
        environment: obs.environment,
        name: obs.name,
        failureClass: obs.failureClass,
        severity: obs.severity,
        since,
        lastAlertAt: nowMs,
      };
      events.push({ kind: "firing", identity: obs.identity, obs, since });
    } else if (nowMs - Number(active.lastAlertAt || 0) >= renotifyMs) {
      alerts[obs.identity] = { ...active, lastAlertAt: nowMs };
      events.push({ kind: "renotify", identity: obs.identity, obs, since: Number(active.since || nowMs) });
    }
  }
  for (const [identity, active] of Object.entries(currentAlerts)) {
    const obs = observed.get(identity);
    if (obs && obs.status !== "ok") continue; // still failing, suppressed, pending or unknown
    if (!obs && !resolveUnobserved) continue;
    delete alerts[identity];
    events.push({
      kind: "resolved",
      identity,
      obs: obs || { ...active, recovery: "no longer checked by this Worker" },
      since: Number(active.since || nowMs),
    });
  }
  return { alerts, events };
}

function formatAlertMessage(events) {
  const firing = events.filter((e) => e.kind !== "resolved");
  const resolved = events.filter((e) => e.kind === "resolved");
  const severity = firing.some((e) => e.obs.severity === "P0") ? "P0" : firing.length ? "P1" : "";
  const headline = firing.length
    ? `[OUT-OF-BAND] ${severity} Infra2 Cloudflare watchdog failed`
    : "[RESOLVED] Infra2 Cloudflare watchdog recovered";
  const lines = [headline, "Route: Cloudflare Workers Cron -> Feishu direct"];
  for (const event of firing) {
    const { obs } = event;
    const label = event.kind === "renotify" ? "STILL FIRING" : "FIRING";
    lines.push(
      `${label} ${obs.severity} ${obs.failureClass} ${obs.environment}/${obs.name}: ${obs.summary} — ${obs.detail}`,
    );
    lines.push(`  Since: ${new Date(event.since).toISOString()}`);
    lines.push(`  Action: ${suggestedAction(obs)}`);
    lines.push(`  Runbook: ${runbookUrl(obs.failureClass)}`);
  }
  for (const event of resolved) {
    const { obs } = event;
    lines.push(
      `RESOLVED ${obs.environment}/${obs.name} (${obs.failureClass}): ${obs.recovery}; failing since ${new Date(event.since).toISOString()}`,
    );
  }
  return lines.join("\n");
}

function suggestedAction(obs) {
  switch (obs.failureClass) {
    case "host-reachability":
      return obs.url
        ? `curl -I "${obs.url}" from an external network; if the VPS heartbeat is also stale, the host or its egress is down`
        : "reach the VPS over SSH; check the host, its network and the platform-alerting-probes container";
    case "alert-pipeline":
      return "read platform-alerting-probes logs: the probe loop or its alert delivery is failing";
    default:
      return "validate WATCHDOG_TARGETS_JSON / WATCHDOG_HEARTBEATS_JSON and the KV binding, then redeploy the Worker";
  }
}

function runbookUrl(failureClass) {
  const anchor = failureClass === "alert-pipeline" ? "#infra-service-probes" : "#out-of-band-watchdog";
  return `https://github.com/wangzitian0/infra2/blob/main/platform/12.alerting/README.md${anchor}`;
}

// ---------------------------------------------------------------------------
// Heartbeat POST
// ---------------------------------------------------------------------------

// Heartbeat KV budget. The free tier allows 1000 put()/day for the whole account,
// and a spent quota silently freezes every heartbeat into a false "stale" page
// (#616; again 2026-09-15/16: 1198 and 1157 puts/day). Each configured heartbeat
// key costs at most
//   ceil(86400 / minWriteInterval)   refresh writes
// + statusChangeWritesPerDay         early writes for a changed verdict
// per UTC day, however often the runner posts and however its verdict flaps.
const DEFAULT_HEARTBEAT_MIN_WRITE_INTERVAL_SECONDS = 600;
const DEFAULT_HEARTBEAT_STATUS_CHANGE_WRITES_PER_DAY = 24;
// Hard runtime limits, whatever the Worker env says.
const MIN_HEARTBEAT_WRITE_INTERVAL_SECONDS = 600;
const MAX_HEARTBEAT_STATUS_CHANGE_WRITES_PER_DAY = 24;
// A liveness ping may refresh the record only once verdict posts have left it
// alone this long (in units of minWriteInterval), so a verdict always gets the
// write window first and a ping can never hide it.
const LIVENESS_REFRESH_INTERVALS = 2;
// The probe runner's liveness-first ping (#369) before every probe round. Runners
// built before the `liveness` flag send it with this fixed detail and ok=true.
const LEGACY_LIVENESS_DETAIL = "probe loop iteration starting";

function isLivenessPing(payload) {
  return payload.liveness === true || String(payload.detail || "") === LEGACY_LIVENESS_DETAIL;
}

// A variable that is unset, empty or non-numeric falls back to its default, and
// one outside [min, max] is clamped: NaN makes every comparison false, and a tiny
// interval or a huge budget writes (nearly) every post.
function budgetVar(raw, fallback, { min, max = Infinity }) {
  if (raw === undefined || raw === null || String(raw).trim() === "") {
    return fallback;
  }
  const value = Number(raw);
  if (!Number.isFinite(value)) {
    return fallback;
  }
  return Math.min(max, Math.max(min, value));
}

function heartbeatWritePolicy(env) {
  const minWriteIntervalMs =
    budgetVar(env.WATCHDOG_HEARTBEAT_MIN_WRITE_INTERVAL_SECONDS, DEFAULT_HEARTBEAT_MIN_WRITE_INTERVAL_SECONDS, {
      min: MIN_HEARTBEAT_WRITE_INTERVAL_SECONDS,
    }) * 1000;
  return {
    minWriteIntervalMs,
    livenessRefreshMs: minWriteIntervalMs * LIVENESS_REFRESH_INTERVALS,
    statusChangeWritesPerDay: Math.floor(
      budgetVar(env.WATCHDOG_HEARTBEAT_STATUS_CHANGE_WRITES_PER_DAY, DEFAULT_HEARTBEAT_STATUS_CHANGE_WRITES_PER_DAY, {
        min: 0,
        max: MAX_HEARTBEAT_STATUS_CHANGE_WRITES_PER_DAY,
      }),
    ),
  };
}

function parseHeartbeatRecord(raw) {
  if (!raw) {
    return null;
  }
  try {
    const record = JSON.parse(raw);
    return record && typeof record === "object" && !Array.isArray(record) ? record : null;
  } catch (_error) {
    return null;
  }
}

// null = unknown (a v1 runner, or a malformed field); never suppresses anything.
function failingRoutesKey(routes) {
  return Array.isArray(routes) ? JSON.stringify(routes) : "unknown";
}

// A verdict is `ok` (the probe loop is healthy) plus the set of in-band public
// routes the loop sees failing; a change in either is a status change.
function verdictChanged(existing, verdict) {
  return (
    (existing.ok !== false) !== verdict.ok ||
    failingRoutesKey(existing.failingPublicRoutes) !== failingRoutesKey(verdict.failingPublicRoutes)
  );
}

// Decides whether one heartbeat POST is worth a KV put(), and what to store.
// `incoming` is the verdict or liveness ping as received; `existing` the stored
// record or null. Only a verdict can change the stored verdict: the runner's
// liveness ping (ok=true) used to alternate with a failing verdict (ok=false) on
// every loop, and every alternation was written immediately.
function heartbeatWrite(existing, incoming, nowMs, policy) {
  const day = utcDateKey(nowMs);
  const stored = existing && existing.statusChangeBudget;
  let spent = 0;
  if (stored && stored.day === day) {
    const used = Number(stored.used);
    // A counter that is not a non-negative number (a corrupted or hand-edited
    // record) counts as spent: a NaN would make `spent >= budget` always false.
    spent = Number.isFinite(used) && used >= 0 ? Math.floor(used) : policy.statusChangeWritesPerDay;
  }
  const budget = (used) => ({ day, used });
  const { liveness, ...verdict } = incoming;
  if (!existing) {
    return { write: true, reason: "first", value: { ...verdict, statusChangeBudget: budget(spent) } };
  }
  const ageMs = nowMs - Number(existing.receivedAt || 0);
  if (ageMs < 0) {
    // A record stamped in the future cannot age out; replace it.
    return { write: true, reason: "future-record", value: { ...verdict, statusChangeBudget: budget(spent) } };
  }
  if (liveness) {
    if (ageMs < policy.livenessRefreshMs) {
      return { write: false, reason: "liveness-throttled" };
    }
    return {
      write: true,
      reason: "liveness-refresh",
      value: { ...existing, receivedAt: incoming.receivedAt, refreshedBy: "liveness", statusChangeBudget: budget(spent) },
    };
  }
  if (ageMs >= policy.minWriteIntervalMs) {
    return { write: true, reason: "refresh", value: { ...verdict, statusChangeBudget: budget(spent) } };
  }
  if (!verdictChanged(existing, verdict)) {
    return { write: false, reason: "throttled" };
  }
  if (spent >= policy.statusChangeWritesPerDay) {
    // Flapping past the daily budget: the next refresh window carries the verdict.
    return { write: false, reason: "status-change-budget-spent" };
  }
  return {
    write: true,
    reason: "status-change",
    value: { ...verdict, statusChangeBudget: budget(spent + 1) },
  };
}

function configuredHeartbeatKeys(env) {
  const heartbeats = filterByEnvironment(
    parseJsonList(env.WATCHDOG_HEARTBEATS_JSON, DEFAULT_HEARTBEATS),
    enabledEnvironments(env),
  );
  return new Set(heartbeats.map((heartbeat) => heartbeatKey(heartbeat.environment, heartbeat.name)));
}

function failingPublicRoutes(raw) {
  if (!Array.isArray(raw)) {
    return null;
  }
  const names = new Set();
  for (const item of raw) {
    const text = String(item || "").trim();
    if (!/^[a-zA-Z0-9_.-]+$/.test(text)) {
      return null; // a malformed list is unknown, not "nothing failing"
    }
    names.add(text);
  }
  if (names.size > MAX_FAILING_PUBLIC_ROUTES) {
    return null;
  }
  return [...names].sort();
}

function deliveryTimestamp(raw) {
  const value = Number(raw);
  return Number.isFinite(value) && value >= 0 ? Math.floor(value) : null;
}

async function recordHeartbeat(request, env) {
  const expectedToken = String(env.HEARTBEAT_TOKEN || "");
  if (!expectedToken) {
    return jsonResponse({ ok: false, error: "HEARTBEAT_TOKEN is not configured" }, 500);
  }
  const actualToken = request.headers.get("Authorization") || "";
  if (actualToken !== `Bearer ${expectedToken}`) {
    return jsonResponse({ ok: false, error: "unauthorized" }, 401);
  }
  if (!env.WATCHDOG_STATE) {
    return jsonResponse({ ok: false, error: "WATCHDOG_STATE KV binding is missing" }, 500);
  }

  let payload;
  try {
    payload = await request.json();
  } catch (_error) {
    payload = {};
  }
  // request.json() also accepts valid JSON that is not an object; normalize so
  // the field access below cannot throw.
  if (payload === null || typeof payload !== "object" || Array.isArray(payload)) {
    payload = {};
  }
  let environment;
  let name;
  let key;
  let configuredKeys;
  try {
    environment = safeId(payload.env || payload.environment || "production");
    if (environment === "prod") {
      environment = "production";
    }
    name = safeId(payload.name || "infra-probe-runner");
    key = heartbeatKey(environment, name);
    configuredKeys = configuredHeartbeatKeys(env);
  } catch (error) {
    // A malformed env/name (or heartbeat config) must not bubble up as an
    // unhandled 500/CF-1101; degrade visibly with a queryable event.
    logEvent({ event: "watchdog.heartbeat.error", timestamp: Date.now(), status: "fail", error: errorText(error) });
    return jsonResponse({ ok: true, persisted: false, degraded: true });
  }
  // Only keys the Worker reads are stored: an unconfigured name would spend the
  // shared put() budget on a record nobody checks.
  if (!configuredKeys.has(key)) {
    logEvent({ event: "watchdog.heartbeat.ignored", timestamp: Date.now(), status: "fail", key });
    return jsonResponse({ ok: false, key, persisted: false, error: "not a configured heartbeat" }, 404);
  }
  const now = Date.now();
  // Contract v2 (#903/#904): `ok` is the probe loop's health; the loop's failing
  // in-band public routes and its last successful alert delivery ride along. A v1
  // payload (no `schema`) leaves both unknown.
  const v2 = Number(payload.schema) === 2;
  const incoming = {
    environment,
    name,
    ok: payload.ok !== false,
    detail: String(payload.detail || ""),
    timestamp: Number(payload.timestamp || 0),
    receivedAt: now,
    liveness: isLivenessPing(payload),
    schema: v2 ? 2 : 1,
    lastDeliveryOkAt: v2 ? deliveryTimestamp(payload.last_delivery_ok_at) : null,
    failingPublicRoutes: v2 ? failingPublicRoutes(payload.failing_public_routes) : null,
  };

  // Read-then-maybe-write (reads are cheap, puts are budgeted): see heartbeatWrite.
  // A KV failure (e.g. the daily quota is exhausted) degrades to HTTP 200 with a
  // queryable event instead of an unhandled 500/1101.
  let decision;
  try {
    const existingRaw = await env.WATCHDOG_STATE.get(key);
    decision = heartbeatWrite(parseHeartbeatRecord(existingRaw), incoming, now, heartbeatWritePolicy(env));
    if (decision.write) {
      await env.WATCHDOG_STATE.put(key, JSON.stringify(decision.value));
    }
  } catch (error) {
    logEvent({ event: "watchdog.heartbeat.error", timestamp: now, status: "fail", key, error: errorText(error) });
    return jsonResponse({ ok: true, key, persisted: false, degraded: true });
  }
  return jsonResponse({ ok: true, key, persisted: decision.write, reason: decision.reason });
}

// ---------------------------------------------------------------------------
// Read endpoints
// ---------------------------------------------------------------------------

function authorizeRead(request, env) {
  const expectedToken = String(env.WATCHDOG_STATUS_TOKEN || "");
  if (!expectedToken) {
    return jsonResponse({ ok: false, error: "WATCHDOG_STATUS_TOKEN is not configured" }, 500);
  }
  if ((request.headers.get("Authorization") || "") !== `Bearer ${expectedToken}`) {
    return jsonResponse({ ok: false, error: "unauthorized" }, 401);
  }
  if (!env.WATCHDOG_STATE) {
    return jsonResponse({ ok: false, error: "WATCHDOG_STATE KV binding is missing" }, 500);
  }
  return null;
}

async function statusResponse(request, env) {
  const refused = authorizeRead(request, env);
  if (refused) return refused;
  const nowMs = Date.now();
  const maxAgeSeconds = Number(env.WATCHDOG_STATUS_MAX_AGE_SECONDS || 7200);
  const [lastRunRecord, rawState] = await Promise.all([loadState(env, LAST_RUN_KEY), loadState(env, STATE_KEY)]);
  const state = normalizeState(rawState);
  // A quiet run writes the last-run record, a transition the state document: the
  // newer of the two is the last run.
  const stateRun = state.lastRun || {};
  const lastRun = Number(stateRun.ranAt || 0) > Number(lastRunRecord.ranAt || 0) ? stateRun : lastRunRecord;
  const ranAt = Number(lastRun.ranAt || 0);
  const ageSeconds = ranAt > 0 ? Math.floor((nowMs - ranAt) / 1000) : null;
  const stale = ageSeconds === null || ageSeconds > maxAgeSeconds || ageSeconds < -300;

  let heartbeats = [];
  try {
    const configured = filterByEnvironment(
      parseJsonList(env.WATCHDOG_HEARTBEATS_JSON, DEFAULT_HEARTBEATS),
      enabledEnvironments(env),
    );
    const raws = await Promise.all(configured.map((hb) => env.WATCHDOG_STATE.get(heartbeatKey(hb.environment, hb.name))));
    heartbeats = configured.map((heartbeat, index) => {
      const verdict = heartbeatVerdict(heartbeat, raws[index], nowMs);
      const record = verdict.record || {};
      return {
        environment: heartbeat.environment,
        name: heartbeat.name,
        paged: heartbeat.environment === PAGED_ENVIRONMENT,
        state: verdict.state,
        ageSeconds: verdict.ageSeconds,
        loopOk: verdict.record ? record.ok !== false : null,
        detail: oneLine(record.detail || ""),
        schema: Number(record.schema || 1),
        lastDeliveryOkAt: record.lastDeliveryOkAt ?? null,
        failingPublicRoutes: Array.isArray(record.failingPublicRoutes) ? record.failingPublicRoutes : null,
      };
    });
  } catch (error) {
    heartbeats = [{ error: errorText(error) }];
  }

  const alerts = Object.entries(state.alerts).map(([identity, alert]) => ({ identity, ...alert }));
  return jsonResponse({
    ok: !stale && lastRun.ok !== false,
    lastRun: {
      ageSeconds,
      ok: lastRun.ok !== false,
      routeTargetCount: Number(lastRun.routeTargetCount || 0),
      heartbeatTargetCount: Number(lastRun.heartbeatTargetCount || 0),
      failureCount: Number(lastRun.failureCount || 0),
      activeAlertCount: Number(lastRun.activeAlertCount || 0),
      suppressed: Array.isArray(lastRun.suppressed) ? lastRun.suppressed : [],
      deliveryError: oneLine(lastRun.deliveryError || ""),
    },
    alertState: {
      active: alerts.length > 0,
      lastAlertAt: alerts.reduce((latest, alert) => Math.max(latest, Number(alert.lastAlertAt || 0)), 0),
      alerts,
    },
    entrypoints: Object.entries(state.entrypoints).map(([key, entry]) => ({ key, ...entry })),
    heartbeats,
    openOutages: state.outages.filter((edge) => edge.end === null),
  });
}

async function outagesResponse(request, env) {
  const refused = authorizeRead(request, env);
  if (refused) return refused;
  const state = normalizeState(await loadState(env, STATE_KEY));
  return jsonResponse({
    ok: true,
    generatedAt: Date.now(),
    kept: OUTAGE_EDGES_KEPT,
    outages: state.outages,
  });
}

// ---------------------------------------------------------------------------
// Delivery
// ---------------------------------------------------------------------------

async function sendFeishu(env, text) {
  const mode = String(env.ALERT_DELIVERY_MODE || "feishu_webhook").trim();
  if (mode === "feishu_app") {
    return sendFeishuApp(env, text);
  }
  if (mode !== "feishu_webhook") {
    throw new Error(`Unsupported ALERT_DELIVERY_MODE: ${mode}`);
  }
  return sendFeishuWebhook(env, text);
}

async function sendFeishuWebhook(env, text) {
  const webhookUrl = String(env.FEISHU_WEBHOOK_URL || "");
  if (!webhookUrl) {
    throw new Error("FEISHU_WEBHOOK_URL is required");
  }
  const response = await fetch(webhookUrl, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ msg_type: "text", content: { text } }),
  });
  if (!response.ok) {
    throw new Error(`Feishu delivery failed: HTTP ${response.status}`);
  }
}

async function sendFeishuApp(env, text) {
  const appId = String(env.FEISHU_APP_ID || "");
  const appSecret = String(env.FEISHU_APP_SECRET || "");
  const chatId = String(env.FEISHU_CHAT_ID || "");
  const apiBase = String(env.FEISHU_API_BASE || "https://open.feishu.cn").replace(/\/+$/, "");
  if (!appId || !appSecret || !chatId) {
    throw new Error("FEISHU_APP_ID, FEISHU_APP_SECRET, and FEISHU_CHAT_ID are required");
  }

  const tokenResponse = await fetch(`${apiBase}/open-apis/auth/v3/tenant_access_token/internal`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ app_id: appId, app_secret: appSecret }),
  });
  const tokenBody = await tokenResponse.json();
  if (!tokenResponse.ok || tokenBody.code !== 0 || !tokenBody.tenant_access_token) {
    throw new Error(`Feishu tenant token failed: HTTP ${tokenResponse.status}; code=${tokenBody.code}`);
  }

  const messageResponse = await fetch(`${apiBase}/open-apis/im/v1/messages?receive_id_type=chat_id`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${tokenBody.tenant_access_token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      receive_id: chatId,
      msg_type: "text",
      content: JSON.stringify({ text }),
    }),
  });
  const messageBody = await messageResponse.json();
  if (!messageResponse.ok || messageBody.code !== 0) {
    throw new Error(`Feishu app delivery failed: HTTP ${messageResponse.status}; code=${messageBody.code}`);
  }
}

async function deliverAlert(env, text, kind) {
  // Feishu is the primary channel; email is an independent secondary channel so
  // a Feishu outage cannot silently swallow an alert. Email is sent only when
  // Feishu delivery fails (escalation), to avoid duplicate noise on every alert.
  try {
    await sendFeishu(env, text);
  } catch (feishuError) {
    const ts = Date.now();
    const fe = errorText(feishuError);
    logEvent({ event: "watchdog.delivery.failure", timestamp: ts, kind, channel: "feishu", status: "fail", error: fe });
    const emailConfigured =
      String(env.ALERT_EMAIL_TO || "").trim() !== "" && String(env.RESEND_API_KEY || "").trim() !== "";
    if (!emailConfigured) {
      logEvent({ event: "watchdog.delivery.escalation_unavailable", timestamp: ts, kind, channel: "email" });
      throw feishuError;
    }
    try {
      await sendEmail(env, `[infra2 watchdog] ${kind}`, `${text}\n\n(primary Feishu delivery failed: ${fe})`);
      logEvent({ event: "watchdog.delivery.escalated", timestamp: ts, kind, channel: "email", status: "ok" });
    } catch (emailError) {
      const ee = errorText(emailError);
      logEvent({ event: "watchdog.delivery.failure", timestamp: ts, kind, channel: "email", status: "fail", error: ee });
      throw new Error(`all alert channels failed: feishu=${fe}; email=${ee}`);
    }
  }
}

async function sendEmail(env, subject, text) {
  const to = String(env.ALERT_EMAIL_TO || "").trim();
  const apiKey = String(env.RESEND_API_KEY || "").trim();
  if (!to || !apiKey) {
    throw new Error("email channel not configured (ALERT_EMAIL_TO / RESEND_API_KEY)");
  }
  const from = String(env.ALERT_EMAIL_FROM || "watchdog@zitian.party").trim();
  const response = await fetch("https://api.resend.com/emails", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${apiKey}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ from, to: [to], subject, text }),
  });
  if (!response.ok) {
    throw new Error(`Resend delivery failed: HTTP ${response.status}: ${oneLine(await safeBody(response))}`);
  }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function normalizeState(raw) {
  const state = raw && typeof raw === "object" && !Array.isArray(raw) ? { ...raw } : {};
  state.schema = STATE_SCHEMA;
  state.alerts = isPlainObject(state.alerts) ? { ...state.alerts } : {};
  state.entrypoints = isPlainObject(state.entrypoints) ? { ...state.entrypoints } : {};
  state.outages = Array.isArray(state.outages)
    ? state.outages.filter(isPlainObject).map((edge) => ({ ...edge, end: edge.end ?? null }))
    : [];
  return state;
}

function withoutRunRecord(state) {
  const { lastRun, ...rest } = state;
  return rest;
}

function isPlainObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

async function loadState(env, key) {
  if (!env.WATCHDOG_STATE) {
    return {};
  }
  const raw = await env.WATCHDOG_STATE.get(key);
  if (!raw) {
    return {};
  }
  try {
    const parsed = JSON.parse(raw);
    return isPlainObject(parsed) ? parsed : {};
  } catch (_error) {
    return {};
  }
}

function utcDateKey(ms) {
  return new Date(ms).toISOString().slice(0, 10);
}

function parseJsonList(raw, fallback) {
  if (!raw) {
    return fallback;
  }
  const parsed = JSON.parse(raw);
  if (!Array.isArray(parsed)) {
    throw new Error("watchdog JSON config must be a list");
  }
  return parsed;
}

function enabledEnvironments(env) {
  return new Set(
    String(env.WATCHDOG_ENVIRONMENTS || "production,staging")
      .split(",")
      .map((value) => value.trim())
      .filter(Boolean),
  );
}

function filterByEnvironment(items, environments) {
  return items.filter((item) => environments.has(item.environment));
}

function logEvent(payload) {
  console.log(JSON.stringify(payload));
}

async function safeBody(response) {
  try {
    return oneLine((await response.text()).slice(0, 240));
  } catch (_error) {
    return "";
  }
}

function heartbeatKey(environment, name) {
  return `heartbeat:${environment}:${name}`;
}

function safeId(value) {
  const text = String(value || "").trim();
  if (!/^[a-zA-Z0-9_.-]+$/.test(text)) {
    throw new Error(`unsafe id: ${text}`);
  }
  return text;
}

function errorText(error) {
  return oneLine(error && error.message ? error.message : String(error));
}

function oneLine(value) {
  return String(value || "").replace(/\s+/g, " ").trim();
}

function jsonResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
