// Infra2 out-of-band watchdog on Cloudflare Workers Cron (ops.observability.md §1.1, #904).
//
// The Worker judges only what the VPS cannot report about itself:
//   - the production probe-runner heartbeat is stale: P0 host-reachability
//     ("VPS 或它的出网中断", the VPS or its egress is down);
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
const MAX_FAILING_PUBLIC_ROUTES = 32;
const MAX_ROUTE_NAME_LENGTH = 64;
const STATE_SCHEMA = 2;
// The cron cadence (wrangler.toml `*/30 * * * *`). A failure first seen at
// `since` began after the previous run, which saw the entrypoint healthy.
const RUN_INTERVAL_MS = 30 * 60 * 1000;
// The probe-loop P1 fires after this many consecutive unhealthy runs and resolves
// after this many healthy ones, so a flapping loop does not page every flip.
const LOOP_FAILURE_RUNS = 2;
const LOOP_RECOVERY_RUNS = 2;
// Every stored or served free-text field is cut to this; GitHub reads /status
// through a 4096-byte window.
const DETAIL_MAX = 300;
// /status serves at most this much of any one text field (worst case tested < 3 KB).
const STATUS_TEXT_MAX = 120;
// #905: every pager message has one layout (ops.observability.md §3.1). These are
// libs/alerting.py's PAGER_FIELDS in the same order (the Worker has no log tail);
// libs/tests/test_cloudflare_watchdog.py holds the two together.
const PAGER_FIELDS = ["级别", "环境", "对象", "现象", "开始于", "影响", "下一步", "Runbook"];
const FIELD_SEPARATOR = "：";
// §3: critical = P0, error = P1, warning = P2; unknown counts as critical.
const LEVEL_BY_SEVERITY = { critical: "P0", error: "P1", warning: "P2", p0: "P0", p1: "P1", p2: "P2" };
const LEVEL_RANK = { P0: 0, P1: 1, P2: 2 };
const LEVEL_EMOJI = { P0: "🔴", P1: "🟠", P2: "🟡" };
// Feishu text limit shared with libs/alerting.py (MAX_MESSAGE_CHARS).
const MAX_MESSAGE_CHARS = 3500;
const TRUNCATION_SUFFIX = "\n...[truncated]";
const MAX_FULL_EVENTS = 5;
const DISPLAY_OFFSET_MS = 8 * 60 * 60 * 1000;
const REPO_BLOB = "https://github.com/wangzitian0/infra2/blob/main";

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
      await deliverAlert(env, formatAlertMessage(plan.events, nowMs), kind);
      state.alerts = plan.alerts;
    } catch (error) {
      // Nothing undelivered is marked sent: the next run retries it.
      deliveryError = trimText(errorText(error));
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
    problems.push(`配置预检失败:${errorText(error)}`);
  }
  if (problems.length === 0) {
    if (targets.length === 0) problems.push("生效的入口目标列表为空");
    if (heartbeats.length === 0) problems.push("生效的心跳目标列表为空");
  }
  if (!env.WATCHDOG_STATE) problems.push("缺少 WATCHDOG_STATE KV 绑定");
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
    summary: "Worker 无法评估它的检查",
    detail: problems.join("; "),
    recovery: "Worker 配置恢复有效",
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
  // Routes the VPS says it has paged and still sees failing (#903: the heartbeat
  // lists only pages it delivered), from a fresh production heartbeat whose loop is
  // healthy. The list is null (unknown) for a v1 runner; unknown, stale, missing or
  // an unhealthy loop never suppresses.
  const vpsFailingRoutes = new Map();
  pagedHeartbeats.forEach((heartbeat, index) => {
    const verdict = heartbeatVerdict(heartbeat, records[index], nowMs);
    const outage = trackOutageEdge(state, heartbeat, verdict, nowMs);
    observations.push(...heartbeatObservations(state, heartbeat, verdict, outage));
    const record = verdict.record;
    if (verdict.state === "fresh" && record.ok !== false && Array.isArray(record.failingPublicRoutes)) {
      for (const route of record.failingPublicRoutes) {
        vpsFailingRoutes.set(route, {
          source: `${heartbeat.environment}/${heartbeat.name}`,
          lastDeliveryOkAtMs: Number(record.lastDeliveryOkAt || 0) * 1000,
        });
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
      detail: `HTTP ${response.status};期望 ${statuses.join(",")};body=${await safeBody(response)}`,
    };
  } catch (error) {
    return { ok: false, detail: `请求失败:${errorText(error)}` };
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
    severity: pagerLevel(target.severity),
    summary: `从 Cloudflare 访问不到外部入口 ${target.url}`,
    detail: trimText(probe.detail),
    recovery: `${target.url} 已恢复可达(${probe.detail})`,
    url: target.url,
  };
  if (probe.ok) {
    delete state.entrypoints[key];
    return { ...base, status: "ok" };
  }
  const previous = state.entrypoints[key] || { failures: 0, since: nowMs };
  // Capped at the threshold, so a sustained outage writes nothing after it pages.
  const failures = Math.min(threshold, Number(previous.failures || 0) + 1);
  const since = Number(previous.since || nowMs);
  // Re-evaluated every run, so the entrypoint pages as soon as this stops holding.
  // The VPS must have delivered something after the last run that saw the route
  // healthy (since - one run); a delivery before that cannot be about this failure.
  // Stored in state, so it must not change from run to run while nothing else does.
  const vpsReport = vpsFailingRoutes.get(target.name);
  const suppressedReason =
    vpsReport && vpsReport.lastDeliveryOkAtMs >= since - RUN_INTERVAL_MS
      ? `${vpsReport.source} heartbeat: ${target.name} paged in-band and still failing`
      : "";
  const entry = { failures, since };
  if (suppressedReason) entry.suppressedReason = suppressedReason;
  state.entrypoints[key] = entry;
  if (failures < threshold) {
    return { ...base, status: "pending", since: entry.since, detail: `${probe.detail}(连续失败第 ${failures}/${threshold} 次)` };
  }
  return {
    ...base,
    status: "failing",
    since: entry.since,
    detail: `${probe.detail}(连续 ${failures} 次运行失败)`,
    suppressedReason,
  };
}

function heartbeatVerdict(heartbeat, raw, nowMs) {
  if (!raw) {
    return { state: "missing", detail: "心跳缺失", ageSeconds: null, record: null };
  }
  const record = parseHeartbeatRecord(raw);
  if (!record) {
    return { state: "invalid", detail: "心跳内容不是合法 JSON", ageSeconds: null, record: null };
  }
  const ageSeconds = Math.floor((nowMs - Number(record.receivedAt || 0)) / 1000);
  const maxAge = Number(heartbeat.maxAgeSeconds || 1800);
  if (ageSeconds < -300) {
    return { state: "future", detail: `心跳时间戳在未来:${ageSeconds}s`, ageSeconds, record };
  }
  if (ageSeconds > maxAge) {
    return { state: "stale", detail: `心跳过期:${ageSeconds}s 前(上限 ${maxAge}s)`, ageSeconds, record };
  }
  return { state: "fresh", detail: `心跳新鲜:${ageSeconds}s 前`, ageSeconds, record };
}

function heartbeatObservations(state, heartbeat, verdict, outage) {
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
    severity: pagerLevel(heartbeat.severity),
    status: fresh ? "ok" : "failing",
    since: outage ? outage.start : undefined,
    summary: "VPS 或它的出网中断",
    detail: verdict.detail,
    recovery: `心跳恢复新鲜(${verdict.ageSeconds}s 前)`,
  };
  const loopDetail = fresh ? trimText(verdict.record.detail) : "";
  const unhealthy = {
    ...common,
    identity: `${key}:heartbeat-unhealthy`,
    failureClass: "alert-pipeline",
    severity: "P1",
    status: loopStatus(state, key, `${key}:heartbeat-unhealthy`, verdict),
    summary: "探测循环报告不健康",
    detail: loopDetail || "无详情",
    recovery: `探测循环恢复健康(${loopDetail || "ok"})`,
  };
  return [stale, unhealthy];
}

// The probe loop's own health, debounced over cron runs. Only a v2 heartbeat
// speaks for the loop: a v1 runner sends ok=false whenever any probe fails, and a
// stale record says nothing about the loop now. Either is "unknown": no counter
// moves, nothing fires or resolves.
function loopStatus(state, key, identity, verdict) {
  if (verdict.state !== "fresh" || Number(verdict.record.schema) !== 2) {
    return "unknown";
  }
  const active = Boolean(state.alerts[identity]);
  const streak = state.loops[key] || { bad: 0, good: 0 };
  if (verdict.record.ok === false) {
    const bad = Math.min(LOOP_FAILURE_RUNS, Number(streak.bad || 0) + 1);
    state.loops[key] = { bad, good: 0 };
    return active || bad >= LOOP_FAILURE_RUNS ? "failing" : "pending";
  }
  if (!active) {
    delete state.loops[key]; // one unhealthy run that never paged is forgotten
    return "ok";
  }
  const good = Math.min(LOOP_RECOVERY_RUNS, Number(streak.good || 0) + 1);
  if (good >= LOOP_RECOVERY_RUNS) {
    delete state.loops[key];
    return "ok";
  }
  state.loops[key] = { bad: Number(streak.bad || 0), good };
  return "pending";
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
        service_id: obs.service_id,
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
      obs: obs || { ...active, recovery: "本 Worker 不再检查它" },
      since: Number(active.since || nowMs),
    });
  }
  return { alerts, events };
}

// One message per run: the firing (and re-sent) events in full, then what recovered.
// Fewer events are shown in full until the text fits MAX_MESSAGE_CHARS.
function formatAlertMessage(events, nowMs) {
  let text = "";
  for (let full = MAX_FULL_EVENTS; full >= 1; full -= 1) {
    text = pagerText(events, nowMs, full);
    if (text.length <= MAX_MESSAGE_CHARS) return text;
  }
  return `${text.slice(0, MAX_MESSAGE_CHARS - TRUNCATION_SUFFIX.length).trimEnd()}${TRUNCATION_SUFFIX}`;
}

function pagerText(events, nowMs, full) {
  // The most severe first, so the events shown in full are the ones that matter most.
  const firing = events
    .filter((e) => e.kind !== "resolved")
    .map((event, order) => ({ event, order, rank: LEVEL_RANK[pagerLevel(event.obs.severity)] }))
    .sort((a, b) => a.rank - b.rank || a.order - b.order)
    .map(({ event }) => event);
  const resolved = events.filter((e) => e.kind === "resolved");
  const lines = [];
  if (firing.length) {
    const level = highestLevel(firing.map((e) => e.obs.severity));
    lines.push(`${LEVEL_EMOJI[level]} [${level} 告警] Cloudflare 带外 watchdog · ${firing.length} 项`);
  } else {
    lines.push(`✅ [已恢复] Cloudflare 带外 watchdog · ${resolved.length} 项`);
  }
  lines.push("来源：Cloudflare Workers Cron → 飞书直发(不经 bridge)");
  lines.push(...eventBlocks(firing, full, (event) => firingValues(event, nowMs)));
  if (resolved.length) {
    if (firing.length) lines.push("", `✅ 已恢复 ${resolved.length} 项`);
    lines.push(...eventBlocks(resolved, full, (event) => resolvedValues(event, nowMs)));
  }
  return lines.join("\n");
}

// Each event is one block: its values under PAGER_FIELDS, in that order.
function eventBlocks(events, full, valuesOf) {
  const lines = [];
  events.slice(0, full).forEach((event, index) => {
    const note = event.kind === "renotify" ? " · 仍在告警" : "";
    lines.push("", `— ${index + 1}/${events.length}${note} —`);
    valuesOf(event).forEach((value, field) => {
      if (value) lines.push(`${PAGER_FIELDS[field]}${FIELD_SEPARATOR}${oneLine(value)}`);
    });
  });
  const rest = events.slice(full);
  if (rest.length) {
    lines.push("", `另有 ${rest.length} 项,只列摘要:`);
    for (const event of rest) {
      // 级别 · 环境 · 对象 · 现象 · 开始于
      const values = valuesOf(event).slice(0, 5).filter(Boolean);
      lines.push(`• ${values.map((value) => trimText(value, 160)).join(" · ")}`);
    }
  }
  return lines;
}

function firingValues(event, nowMs) {
  const { obs } = event;
  return [
    pagerLevel(obs.severity),
    obs.environment,
    objectName(obs),
    `${obs.summary} — ${obs.detail}`,
    `${formatTime(event.since)}(已持续 ${formatDuration(nowMs - event.since)})`,
    `[${obs.failureClass}] ${impactText(obs)}`,
    suggestedAction(obs),
    runbookUrl(obs),
  ];
}

// What recovered and how long it was down (级别 through 开始于).
function resolvedValues(event, nowMs) {
  const { obs } = event;
  return [
    pagerLevel(obs.severity),
    obs.environment,
    objectName(obs),
    obs.recovery,
    `${formatTime(event.since)} → ${formatTime(nowMs)}(共 ${formatDuration(nowMs - event.since)})`,
  ];
}

function objectName(obs) {
  return obs.service_id ? `${obs.service_id} · ${obs.name}` : obs.name;
}

function pagerLevel(severity) {
  return LEVEL_BY_SEVERITY[String(severity || "").trim().toLowerCase()] || "P0";
}

function highestLevel(severities) {
  return severities.map(pagerLevel).reduce((best, level) => (LEVEL_RANK[level] < LEVEL_RANK[best] ? level : best), "P2");
}

// The owner's zone (UTC+8, no daylight saving), as libs/alerting.py format_time.
function formatTime(ms) {
  const iso = new Date(ms + DISPLAY_OFFSET_MS).toISOString();
  return `${iso.slice(0, 10)} ${iso.slice(11, 16)}（UTC+8）`;
}

function formatDuration(ms) {
  const total = Math.max(0, Math.floor(ms / 1000));
  if (total < 60) return "不到 1 分钟";
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const parts = [];
  if (days) parts.push(`${days} 天`);
  if (hours) parts.push(`${hours} 小时`);
  if (minutes && !days) parts.push(`${minutes} 分钟`);
  return parts.join(" ");
}

function impactText(obs) {
  switch (obs.failureClass) {
    case "host-reachability":
      return obs.url
        ? `用户从公网打不开 ${obs.url}`
        : "VPS 或它的出网中断:带内探测与告警一起静默,所有产品可能不可用";
    case "alert-pipeline":
      return "带内探测循环或告警投递失效:VPS 上的新故障可能无人告知";
    default:
      return "Worker 评估不了任何检查:带外兜底暂时失明";
  }
}

// Commands, paths and identifiers stay verbatim, in backticks.
function suggestedAction(obs) {
  switch (obs.failureClass) {
    case "host-reachability":
      return obs.url
        ? `从外部网络执行 \`curl -I "${obs.url}"\`;若 VPS 心跳也过期,就是整机或它的出网中断`
        : "通过 SSH 登录 VPS,检查宿主机、它的网络与 `platform-alerting-probes` 容器";
    case "alert-pipeline":
      return "查看 `platform-alerting-probes` 的日志:探测循环或它的告警投递在失败";
    default:
      return "核对 `WATCHDOG_TARGETS_JSON` / `WATCHDOG_HEARTBEATS_JSON` 与 KV 绑定,再重新部署 Worker";
  }
}

// A specific anchor per failure class (#905).
function runbookUrl(obs) {
  switch (obs.failureClass) {
    case "host-reachability":
      return obs.url
        ? `${REPO_BLOB}/platform/12.alerting/README.md#public-route-probes`
        : `${REPO_BLOB}/docs/runbooks/infra022-p0.md#watchdog-silent`;
    case "alert-pipeline":
      return `${REPO_BLOB}/platform/12.alerting/README.md#infra-service-probes`;
    default:
      return `${REPO_BLOB}/cloudflare/infra-watchdog/README.md#optional-vars`;
  }
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
    if (!/^[a-zA-Z0-9_.-]+$/.test(text) || text.length > MAX_ROUTE_NAME_LENGTH) {
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
    detail: trimText(payload.detail),
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
        detail: trimText(record.detail, STATUS_TEXT_MAX),
        schema: Number(record.schema || 1),
        lastDeliveryOkAt: record.lastDeliveryOkAt ?? null,
        // A count and a cut-down list: the full list can be 32 names of 64 chars.
        failingPublicRouteCount: Array.isArray(record.failingPublicRoutes) ? record.failingPublicRoutes.length : null,
        failingPublicRoutes: Array.isArray(record.failingPublicRoutes)
          ? trimText(record.failingPublicRoutes.join(","), STATUS_TEXT_MAX)
          : null,
      };
    });
  } catch (error) {
    heartbeats = [{ error: trimText(errorText(error), STATUS_TEXT_MAX) }];
  }

  const alerts = Object.entries(state.alerts).map(([identity, alert]) => ({
    identity,
    severity: String(alert.severity || ""),
    since: Number(alert.since || 0),
  }));
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
      deliveryError: trimText(lastRun.deliveryError, STATUS_TEXT_MAX),
    },
    alertState: {
      active: alerts.length > 0,
      lastAlertAt: Object.values(state.alerts).reduce((latest, alert) => Math.max(latest, Number(alert.lastAlertAt || 0)), 0),
      alerts,
    },
    entrypoints: Object.entries(state.entrypoints).map(([key, entry]) => ({
      key,
      failures: Number(entry.failures || 0),
      since: Number(entry.since || 0),
      suppressedReason: trimText(entry.suppressedReason, STATUS_TEXT_MAX),
    })),
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
  state.loops = isPlainObject(state.loops) ? { ...state.loops } : {};
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

function trimText(value, max = DETAIL_MAX) {
  const text = oneLine(value);
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

function jsonResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
