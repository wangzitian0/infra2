const DEFAULT_TARGETS = [
  ["production", "dokploy-public-route", "https://cloud.zitian.party", [200, 302], "critical"],
  ["production", "vault-public-route", "https://vault.zitian.party/v1/sys/health", [200, 429, 472, 473], "critical"],
  ["production", "minio-public-route", "https://minio.zitian.party/minio/health/live", [200], "warning"],
  ["production", "authentik-public-route", "https://sso.zitian.party/-/health/live/", [200, 204, 302], "critical"],
  ["production", "signoz-public-route", "https://signoz.zitian.party", [200, 302], "critical"],
  ["staging", "minio-public-route", "https://minio-staging.zitian.party/minio/health/live", [200], "warning"],
  ["staging", "authentik-public-route", "https://sso-staging.zitian.party/-/health/live/", [200, 204, 302], "warning"],
  ["production", "finance-report-web-public-route", "https://report.zitian.party/", [200, 302, 307, 308], "critical"],
  ["production", "finance-report-api-public-route", "https://report.zitian.party/api/health", [200], "critical"],
  ["staging", "finance-report-web-public-route", "https://report-staging.zitian.party/", [200, 302, 307, 308], "warning"],
  ["staging", "finance-report-api-public-route", "https://report-staging.zitian.party/api/health", [200], "warning"],
].map(([environment, name, url, statuses, severity]) => ({
  environment,
  name,
  url,
  statuses,
  severity,
}));

const DEFAULT_HEARTBEATS = [
  {
    environment: "production",
    name: "platform-alerting-probes",
    maxAgeSeconds: 5400,
    severity: "critical",
  },
  {
    environment: "staging",
    name: "platform-alerting-probes-staging",
    maxAgeSeconds: 5400,
    severity: "warning",
  },
];

export default {
  async scheduled(controller, env, ctx) {
    ctx.waitUntil(runWatchdog(env, controller.scheduledTime || Date.now()));
  },

  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === "/health") {
      return jsonResponse({ ok: true });
    }
    if (request.method === "GET" && url.pathname === "/status") {
      return statusResponse(request, env);
    }
    if (request.method === "GET" && url.pathname === "/ledger") {
      return ledgerResponse(request, env);
    }
    if (request.method === "POST" && url.pathname === "/heartbeat") {
      return recordHeartbeat(request, env);
    }
    return jsonResponse({ ok: false, error: "not found" }, 404);
  },
};

async function runWatchdog(env, nowMs = Date.now()) {
  const environments = enabledEnvironments(env);
  let targets;
  let heartbeats;
  try {
   targets = filterByEnvironment(
     parseJsonList(env.WATCHDOG_TARGETS_JSON, DEFAULT_TARGETS),
     environments,
   );
   heartbeats = filterByEnvironment(
     parseJsonList(env.WATCHDOG_HEARTBEATS_JSON, DEFAULT_HEARTBEATS),
     environments,
   );
  } catch (error) {
   const configFailure = failResult(
     {
       environment: "global",
       name: "cloudflare-watchdog-config-preflight",
       severity: "critical",
     },
     `config-preflight failed: ${oneLine(
       error && error.message ? error.message : String(error),
     )}`,
     "config-preflight",
   );
   const failures = [configFailure];
   const fingerprint = await failureFingerprint(failures);
   let deliveryError = "";
   try {
     await notifyIfNeeded(env, failures, nowMs);
   } catch (deliveryExc) {
     deliveryError = oneLine(
       deliveryExc && deliveryExc.message ? deliveryExc.message : String(deliveryExc),
     );
   }
   await saveLastRun(env, {
     ranAt: nowMs,
     ok: false,
     routeTargetCount: 0,
     heartbeatTargetCount: 0,
     failureCount: 1,
     failureFingerprint: fingerprint,
     deliveryError,
   });
   if (deliveryError) {
     logWatchdogResult({
       event: "watchdog.delivery.failure",
       timestamp: nowMs,
       status: "fail",
       failureCount: failures.length,
       error: deliveryError,
     });
     throw new Error(`watchdog delivery failed: ${deliveryError}`);
   }
   return {
     failures,
     routeResults: [],
     heartbeatResults: [],
     configResults: failures,
   };
  }
  const timeoutMs = Number(env.WATCHDOG_HTTP_TIMEOUT_MS || 8000);
  const maxAttempts = Math.max(1, Number(env.WATCHDOG_RETRY_MAX_ATTEMPTS || 2));
  const retryDelayMs = Math.max(0, Number(env.WATCHDOG_RETRY_DELAY_MS || 60000));

  const routeResults = await Promise.all(
    targets.map((target) => checkHttpTargetWithRetry(target, timeoutMs, maxAttempts, retryDelayMs)),
  );
  const heartbeatResults = await checkHeartbeats(env, heartbeats, nowMs);
  const configResults = checkEffectiveConfig(targets, heartbeats);
  const allResults = routeResults.concat(heartbeatResults, configResults);
  const failures = allResults.filter((result) => !result.ok);
  const fingerprint = failures.length > 0 ? await failureFingerprint(failures) : "";

  for (const result of allResults) {
    logWatchdogResult({
      event: "watchdog.check",
      timestamp: nowMs,
      environment: result.environment,
      name: result.name,
      status: result.ok ? "ok" : "fail",
      severity: result.severity,
      failure_domain: result.failure_domain || "",
      attempt_count: Number(result.attempt_count || 1),
      detail: result.detail,
    });
  }

  await recordLedger(env, allResults, nowMs);

  let deliveryError = "";
  try {
    await notifyIfNeeded(env, failures, nowMs);
  } catch (error) {
    deliveryError = oneLine(error && error.message ? error.message : String(error));
  }
  await saveLastRun(env, {
    ranAt: nowMs,
    ok: failures.length === 0 && !deliveryError,
    routeTargetCount: targets.length,
    heartbeatTargetCount: heartbeats.length,
    failureCount: failures.length,
    failureFingerprint: fingerprint,
    deliveryError,
  });
  if (deliveryError) {
    logWatchdogResult({
      event: "watchdog.delivery.failure",
      timestamp: nowMs,
      status: "fail",
      failureCount: failures.length,
      error: deliveryError,
    });
    throw new Error(`watchdog delivery failed: ${deliveryError}`);
  }
  logWatchdogResult({
    event: "watchdog.run",
    timestamp: nowMs,
    status: failures.length === 0 ? "ok" : "fail",
    routeTargetCount: targets.length,
    heartbeatTargetCount: heartbeats.length,
    failureCount: failures.length,
    failureFingerprint: fingerprint,
    deliveryError,
  });
  return { failures, routeResults, heartbeatResults, configResults };
}

function checkEffectiveConfig(targets, heartbeats) {
  const configTarget = {
    environment: "global",
    name: "cloudflare-watchdog-effective-config",
    severity: "critical",
  };
  const results = [];
  if (targets.length === 0) {
    results.push(
      failResult(configTarget, "effective public route target list is empty", "config-preflight"),
    );
  }
  if (heartbeats.length === 0) {
    results.push(
      failResult(configTarget, "effective heartbeat target list is empty", "config-preflight"),
    );
  }
  return results;
}

async function checkHttpTarget(target, timeoutMs) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(target.url, {
      method: "GET",
      redirect: "manual",
      signal: controller.signal,
      headers: {
        Accept: "text/html,application/json,text/plain,*/*",
        "User-Agent": "Mozilla/5.0 (compatible; infra2-cloudflare-watchdog/1.0; +https://zitian.party)",
      },
    });
    if (target.statuses.includes(response.status)) {
      return okResult(target, `HTTP ${response.status}`, _failure_domain_for_http_target(target));
    }

    const body = await safeBody(response);
    return failResult(
      target,
      `HTTP ${response.status}; expected ${target.statuses.join(",")}; body=${body}`,
      _failure_domain_for_http_target(target),
    );
  } catch (error) {
    return failResult(
      target,
      `fetch failed: ${oneLine(error && error.message ? error.message : String(error))}`,
      _failure_domain_for_http_target(target),
    );
  } finally {
    clearTimeout(timeout);
  }
}

async function checkHttpTargetWithRetry(target, timeoutMs, maxAttempts, retryDelayMs) {
  for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
    const result = await checkHttpTarget(target, timeoutMs);
    result.attempt_count = attempt;
    if (result.ok || attempt >= maxAttempts) {
      return result;
    }
    if (retryDelayMs > 0) {
      await sleep(retryDelayMs);
    }
  }
  return failResult(target, "retry loop exhausted", _failure_domain_for_http_target(target));
}

async function checkHeartbeats(env, heartbeats, nowMs) {
  if (!env.WATCHDOG_STATE) {
    return heartbeats.map((heartbeat) =>
      failResult(
        heartbeat,
        "WATCHDOG_STATE KV binding is missing",
        _failure_domain_for_heartbeat(heartbeat),
      ),
    );
  }

  const results = [];
  for (const heartbeat of heartbeats) {
    const raw = await env.WATCHDOG_STATE.get(heartbeatKey(heartbeat.environment, heartbeat.name));
    if (!raw) {
      results.push(
        failResult(heartbeat, "heartbeat missing", _failure_domain_for_heartbeat(heartbeat)),
      );
      continue;
    }
    let value;
    try {
      value = JSON.parse(raw);
    } catch (_error) {
      results.push(
        failResult(
          heartbeat,
          "heartbeat payload is invalid JSON",
          _failure_domain_for_heartbeat(heartbeat),
        ),
      );
      continue;
    }
    const ageSeconds = Math.floor((nowMs - Number(value.receivedAt || 0)) / 1000);
    if (ageSeconds < -300) {
      results.push(
        failResult(
          heartbeat,
          `heartbeat timestamp is in the future: ${ageSeconds}s old`,
          _failure_domain_for_heartbeat(heartbeat),
        ),
      );
      continue;
    }
    if (value.ok === false) {
      results.push(
        failResult(
          heartbeat,
          `heartbeat reports unhealthy: ${oneLine(value.detail || "")}`,
          _failure_domain_for_heartbeat(heartbeat),
        ),
      );
      continue;
    }
    if (ageSeconds > Number(heartbeat.maxAgeSeconds || 1800)) {
      results.push(
        failResult(
          heartbeat,
          `heartbeat stale: ${ageSeconds}s old`,
          _failure_domain_for_heartbeat(heartbeat),
        ),
      );
      continue;
    }
    results.push(
      okResult(
        heartbeat,
        `heartbeat fresh: ${ageSeconds}s old`,
        _failure_domain_for_heartbeat(heartbeat),
      ),
    );
  }
  return results;
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
  // request.json() also accepts valid JSON that is not an object (null, a
  // string, a number, an array); normalize so the field access below cannot
  // throw on `payload.env` etc.
  if (payload === null || typeof payload !== "object" || Array.isArray(payload)) {
    payload = {};
  }
  let environment;
  let name;
  let key;
  try {
    environment = safeId(payload.env || payload.environment || "production");
    name = safeId(payload.name || "infra-probe-runner");
    key = heartbeatKey(environment, name);
  } catch (error) {
    // A malformed env/name must not bubble up as an unhandled 500/CF-1101;
    // degrade visibly with the same queryable event used for storage failures.
    logWatchdogResult({
      event: "watchdog.heartbeat.error",
      timestamp: Date.now(),
      status: "fail",
      error: oneLine(error && error.message ? error.message : String(error)),
    });
    return jsonResponse({ ok: true, persisted: false, degraded: true });
  }
  const now = Date.now();
  const value = {
    environment,
    name,
    ok: payload.ok !== false,
    detail: String(payload.detail || ""),
    timestamp: Number(payload.timestamp || 0),
    receivedAt: now,
  };

  // Throttle KV writes to stay within the Cloudflare KV daily put() limit.
  // The probe runner posts heartbeats far more often than the staleness window
  // requires; writing on every post exhausts the daily quota and then every
  // put() throws, which silently breaks heartbeat tracking and triggers false
  // "stale heartbeat" alerts. Reads are cheap, so read-then-maybe-write:
  // always persist a status change immediately, otherwise write at most once
  // per minWriteInterval (well under the staleness threshold).
  const minWriteIntervalMs = Number(env.WATCHDOG_HEARTBEAT_MIN_WRITE_INTERVAL_SECONDS || 600) * 1000;
  let shouldWrite = true;
  // Fail-safe: a watcher must distinguish its own storage failure from a target
  // failure. If KV get/put throws (e.g. the daily quota is exhausted), log a
  // queryable structured event and degrade gracefully (HTTP 200) instead of
  // throwing an unhandled exception (HTTP 500 / CF 1101) that is invisible
  // unless someone is live-tailing the worker.
  try {
    const existingRaw = await env.WATCHDOG_STATE.get(key);
    if (existingRaw) {
      try {
        const existing = JSON.parse(existingRaw);
        const ageMs = now - Number(existing.receivedAt || 0);
        const statusUnchanged = (existing.ok !== false) === value.ok;
        if (statusUnchanged && ageMs >= 0 && ageMs < minWriteIntervalMs) {
          shouldWrite = false;
        }
      } catch (_error) {
        shouldWrite = true;
      }
    }
    if (shouldWrite) {
      await env.WATCHDOG_STATE.put(key, JSON.stringify(value));
    }
  } catch (error) {
    logWatchdogResult({
      event: "watchdog.heartbeat.error",
      timestamp: now,
      status: "fail",
      key,
      error: oneLine(error && error.message ? error.message : String(error)),
    });
    return jsonResponse({ ok: true, key, persisted: false, degraded: true });
  }
  return jsonResponse({ ok: true, key, persisted: shouldWrite });
}

async function statusResponse(request, env) {
  const expectedToken = String(env.WATCHDOG_STATUS_TOKEN || "");
  if (!expectedToken) {
    return jsonResponse({ ok: false, error: "WATCHDOG_STATUS_TOKEN is not configured" }, 500);
  }
  const actualToken = request.headers.get("Authorization") || "";
  if (actualToken !== `Bearer ${expectedToken}`) {
    return jsonResponse({ ok: false, error: "unauthorized" }, 401);
  }
  if (!env.WATCHDOG_STATE) {
    return jsonResponse({ ok: false, error: "WATCHDOG_STATE KV binding is missing" }, 500);
  }
  const nowMs = Date.now();
  const maxAgeSeconds = Number(env.WATCHDOG_STATUS_MAX_AGE_SECONDS || 7200);
  const lastRun = await loadState(env, "watchdog:last-run");
  const alertState = await loadState(env, "alert-state:cloudflare-watchdog");
  const ranAt = Number(lastRun.ranAt || 0);
  const ageSeconds = ranAt > 0 ? Math.floor((nowMs - ranAt) / 1000) : null;
  const stale = ageSeconds === null || ageSeconds > maxAgeSeconds || ageSeconds < -300;
  return jsonResponse({
    ok: !stale && lastRun.ok !== false,
    lastRun: {
      ageSeconds,
      ok: lastRun.ok !== false,
      routeTargetCount: Number(lastRun.routeTargetCount || 0),
      heartbeatTargetCount: Number(lastRun.heartbeatTargetCount || 0),
      failureCount: Number(lastRun.failureCount || 0),
      deliveryError: oneLine(lastRun.deliveryError || ""),
    },
    alertState: {
      active: Boolean(alertState.active),
      lastAlertAt: Number(alertState.lastAlertAt || 0),
    },
  });
}

async function notifyIfNeeded(env, failures, nowMs) {
  const stateKey = "alert-state:cloudflare-watchdog";
  const state = await loadState(env, stateKey);
  const renotifyMs = Number(env.WATCHDOG_RENOTIFY_SECONDS || 7200) * 1000;

  if (failures.length === 0) {
    if (state.active) {
      await deliverAlert(env, formatResolvedMessage(), "recovered");
      await saveState(env, stateKey, { active: false, fingerprint: "", lastAlertAt: nowMs });
    }
    return;
  }

  const fingerprint = await failureFingerprint(failures);
  const shouldSend =
    !state.active ||
    state.fingerprint !== fingerprint ||
    nowMs - Number(state.lastAlertAt || 0) >= renotifyMs;

  if (shouldSend) {
    await deliverAlert(env, formatFailureMessage(failures), "failure");
    await saveState(env, stateKey, { active: true, fingerprint, lastAlertAt: nowMs });
  }
}

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
    const fe = oneLine(feishuError && feishuError.message ? feishuError.message : String(feishuError));
    logWatchdogResult({
      event: "watchdog.delivery.failure",
      timestamp: ts,
      kind,
      channel: "feishu",
      status: "fail",
      error: fe,
    });
    const emailConfigured =
      String(env.ALERT_EMAIL_TO || "").trim() !== "" && String(env.RESEND_API_KEY || "").trim() !== "";
    if (!emailConfigured) {
      // No secondary channel configured: the Feishu failure stands as the
      // delivery failure (escalation unavailable, recorded for querying).
      logWatchdogResult({
        event: "watchdog.delivery.escalation_unavailable",
        timestamp: ts,
        kind,
        channel: "email",
      });
      throw feishuError;
    }
    try {
      await sendEmail(env, `[infra2 watchdog] ${kind}`, `${text}\n\n(primary Feishu delivery failed: ${fe})`);
      logWatchdogResult({
        event: "watchdog.delivery.escalated",
        timestamp: ts,
        kind,
        channel: "email",
        status: "ok",
      });
    } catch (emailError) {
      const ee = oneLine(emailError && emailError.message ? emailError.message : String(emailError));
      logWatchdogResult({
        event: "watchdog.delivery.failure",
        timestamp: ts,
        kind,
        channel: "email",
        status: "fail",
        error: ee,
      });
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

function formatFailureMessage(failures) {
  const highestSeverity = failures.some((failure) => failure.severity === "critical") ? "P0" : "P1";
  const lines = [
    "[OUT-OF-BAND] Infra2 Cloudflare watchdog failed",
    `Severity: ${highestSeverity}`,
    "Route: Cloudflare Workers Cron -> Feishu direct",
    "Failures:",
  ];
  for (const failure of failures) {
    const failureDomain = failure.failure_domain || "unknown";
    lines.push(`- ${failure.environment}/${failure.name} [${failureDomain}]: ${failure.detail}`);
    lines.push(`  Action: ${suggestedActionForFailure(failure)}`);
    lines.push(`  Runbook: ${runbookUrlForDomain(failureDomain)}`);
  }
  return lines.join("\n");
}

function suggestedActionForFailure(failure) {
  const failureDomain = failure.failure_domain || "";
  const name = failure.name || "";
  const url = failure.url || "";
  switch (failureDomain) {
    case "public-route":
      return `curl -I "${url || "https://cloud.zitian.party"}" from an external network to verify edge routing`;
    case "heartbeat":
      return "check platform-alerting probe runner logs and heartbeat publish environment variables";
    case "config-preflight":
      return "validate WATCHDOG_TARGETS_JSON / WATCHDOG_HEARTBEATS_JSON format and redeploy worker";
    default:
      return "inspect watchdog /status and last-run payload, then verify target service health";
  }
}

function runbookUrlForDomain(failureDomain) {
  const anchorByDomain = {
    "public-route": "#out-of-band-watchdog",
    heartbeat: "#infra-service-probes",
    "config-preflight": "#out-of-band-watchdog",
  };
  const anchor = anchorByDomain[failureDomain] || "#out-of-band-watchdog";
  return `https://github.com/wangzitian0/infra2/blob/main/platform/12.alerting/README.md${anchor}`;
}

function formatResolvedMessage() {
  return [
    "[RESOLVED] Infra2 Cloudflare watchdog recovered",
    "Route: Cloudflare Workers Cron -> Feishu direct",
    "All configured public route and heartbeat checks are healthy.",
  ].join("\n");
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
    return JSON.parse(raw);
  } catch (_error) {
    return {};
  }
}

async function saveState(env, key, state) {
  if (!env.WATCHDOG_STATE) {
    return;
  }
  await env.WATCHDOG_STATE.put(key, JSON.stringify(state));
}

async function saveLastRun(env, state) {
  await saveState(env, "watchdog:last-run", state);
}

const LEDGER_RETENTION_DAYS = 21;
const R2_LEDGER_PREFIX = "watchdog-ledger/";
// How many recent finalized days reconcileArchives() re-checks each run. Big
// enough to self-heal a multi-day R2 outage at the 30-min cron cadence, small
// enough that the per-run head() cost stays negligible.
const ARCHIVE_BACKFILL_DAYS = 3;

function utcDateKey(ms) {
  return new Date(ms).toISOString().slice(0, 10);
}

function ledgerKey(date) {
  return `ledger:${date}`;
}

// Records this run's per-signal success/failure into a single rolling daily
// rollup key. One read + one write per cron run keeps the Cloudflare KV free
// tier (1000 put/day) safe; writing one key per signal per run would blow the
// quota and silently break every put(). Positive proof (success counts) is the
// point: it makes uptime% queryable, not just failures.
async function recordLedger(env, results, nowMs) {
  if (!env.WATCHDOG_STATE) {
    return;
  }
  const date = utcDateKey(nowMs);
  const key = ledgerKey(date);
  const existing = await loadState(env, key);
  const isNewDay = !existing.date;
  const signals = existing.signals && typeof existing.signals === "object" ? existing.signals : {};
  for (const result of results) {
    const id = `${result.environment}:${result.name}`;
    const entry = signals[id] || { ok: 0, fail: 0, severity: result.severity || "", lastDomain: "" };
    if (result.ok) {
      entry.ok += 1;
    } else {
      entry.fail += 1;
      entry.lastDomain = result.failure_domain || entry.lastDomain || "";
    }
    entry.severity = result.severity || entry.severity || "";
    signals[id] = entry;
  }
  await saveState(env, key, {
    date,
    updatedAt: nowMs,
    runs: Number(existing.runs || 0) + 1,
    signals,
  });
  // Cold-archive finalized days off-host to R2 every run, idempotently, rather
  // than as a one-shot at the day rollover. A single hiccup on that one run --
  // an R2 blip, the .date migration boundary, a thrown put -- used to lose a
  // whole day's archive silently and forever. Reconciling every run instead
  // retries until it sticks, backfills any gap, and surfaces a write failure as
  // a queryable event instead of swallowing it.
  await reconcileArchives(env, nowMs);
  // On the first run of a new day, prune the day that aged out of the KV hot
  // window. Once per day is enough and keeps the per-key delete budget tiny.
  if (isNewDay && env.WATCHDOG_STATE.delete) {
    const pruneDate = utcDateKey(nowMs - LEDGER_RETENTION_DAYS * 86400000);
    await env.WATCHDOG_STATE.delete(ledgerKey(pruneDate));
  }
}

// Idempotently ensure every finalized (past) day still in the KV hot window has
// its off-host R2 cold archive. Bounded and cheap: a head() existence check per
// recent day, a put only when the object is actually missing. Fully guarded so
// a missing binding is a no-op and a transient R2 failure is logged-and-retried
// next run rather than crashing the watchdog or vanishing silently.
async function reconcileArchives(env, nowMs) {
  if (!env.LEDGER_BUCKET) {
    return;
  }
  const scanDays = Math.min(LEDGER_RETENTION_DAYS, ARCHIVE_BACKFILL_DAYS);
  for (let dayOffset = 1; dayOffset <= scanDays; dayOffset += 1) {
    const date = utcDateKey(nowMs - dayOffset * 86400000);
    const record = await loadState(env, ledgerKey(date));
    if (!record.date) {
      continue;
    }
    const objectKey = `${R2_LEDGER_PREFIX}${date}.json`;
    try {
      if (await env.LEDGER_BUCKET.head(objectKey)) {
        continue;
      }
      await env.LEDGER_BUCKET.put(objectKey, JSON.stringify(record), {
        httpMetadata: { contentType: "application/json" },
      });
      logWatchdogResult({
        event: "watchdog.ledger.archive",
        timestamp: nowMs,
        status: "ok",
        date,
      });
    } catch (error) {
      logWatchdogResult({
        event: "watchdog.ledger.archive",
        timestamp: nowMs,
        status: "fail",
        date,
        error: oneLine(error && error.message ? error.message : String(error)),
      });
    }
  }
}

async function ledgerResponse(request, env) {
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
  const nowMs = Date.now();
  const days = [];
  for (let offset = 0; offset < LEDGER_RETENTION_DAYS; offset += 1) {
    const date = utcDateKey(nowMs - offset * 86400000);
    const record = await loadState(env, ledgerKey(date));
    if (record.date) {
      days.push(record);
    }
  }
  return jsonResponse({
    ok: true,
    as_of: utcDateKey(nowMs),
    generatedAt: nowMs,
    window_days: LEDGER_RETENTION_DAYS,
    ledger: days,
  });
}

async function failureFingerprint(failures) {
  const encoded = new TextEncoder().encode(
    JSON.stringify(
      failures.map((failure) => ({
        environment: failure.environment,
        name: failure.name,
        failureDomain: failure.failure_domain || "",
      })),
    ),
  );
  const digest = await crypto.subtle.digest("SHA-256", encoded);
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
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

function okResult(target, detail, failureDomain = "") {
  return {
    environment: target.environment,
    name: target.name,
    url: target.url || "",
    severity: target.severity || "warning",
    ok: true,
    detail,
    failure_domain: failureDomain,
    attempt_count: 1,
  };
}

function failResult(target, detail, failureDomain = "") {
  return {
    environment: target.environment,
    name: target.name,
    url: target.url || "",
    severity: target.severity || "warning",
    ok: false,
    detail: oneLine(detail),
    failure_domain: failureDomain,
    attempt_count: 1,
  };
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function logWatchdogResult(payload) {
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

function _failure_domain_for_http_target(target) {
  if (target.name === "infra2-public-entrypoint") {
    return "host-reachability";
  }
  if (target.name === "cloudflare-worker-health") {
    return "worker-health";
  }
  return "public-route";
}

function _failure_domain_for_heartbeat(heartbeat) {
  return "heartbeat";
}

function safeId(value) {
  const text = String(value || "").trim();
  if (!/^[a-zA-Z0-9_.-]+$/.test(text)) {
    throw new Error(`unsafe id: ${text}`);
  }
  return text;
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
