const DEFAULT_TARGETS = [
  ["production", "dokploy-public-route", "https://cloud.zitian.party", [200, 302], "critical"],
  ["production", "vault-public-route", "https://vault.zitian.party/v1/sys/health", [200, 429, 472, 473], "critical"],
  ["production", "minio-public-route", "https://minio.zitian.party/minio/health/live", [200], "warning"],
  ["production", "authentik-public-route", "https://sso.zitian.party/-/health/live/", [200, 204, 302], "critical"],
  ["production", "signoz-public-route", "https://signoz.zitian.party", [200, 302], "critical"],
  ["staging", "minio-public-route", "https://minio-staging.zitian.party/minio/health/live", [200], "warning"],
  ["staging", "authentik-public-route", "https://sso-staging.zitian.party/-/health/live/", [200, 204, 302], "warning"],
  ["staging", "signoz-public-route", "https://signoz-staging.zitian.party", [200, 302], "warning"],
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
    if (request.method === "POST" && url.pathname === "/heartbeat") {
      return recordHeartbeat(request, env);
    }
    return jsonResponse({ ok: false, error: "not found" }, 404);
  },
};

async function runWatchdog(env, nowMs = Date.now()) {
  const environments = enabledEnvironments(env);
  const targets = filterByEnvironment(parseJsonList(env.WATCHDOG_TARGETS_JSON, DEFAULT_TARGETS), environments);
  const heartbeats = filterByEnvironment(parseJsonList(env.WATCHDOG_HEARTBEATS_JSON, DEFAULT_HEARTBEATS), environments);
  const timeoutMs = Number(env.WATCHDOG_HTTP_TIMEOUT_MS || 8000);

  const routeResults = await Promise.all(targets.map((target) => checkHttpTarget(target, timeoutMs)));
  const heartbeatResults = await checkHeartbeats(env, heartbeats, nowMs);
  const configResults = checkEffectiveConfig(targets, heartbeats);
  const failures = routeResults.concat(heartbeatResults, configResults).filter((result) => !result.ok);
  const fingerprint = failures.length > 0 ? await failureFingerprint(failures) : "";

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
    throw new Error(`watchdog delivery failed: ${deliveryError}`);
  }
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
    results.push(failResult(configTarget, "effective public route target list is empty"));
  }
  if (heartbeats.length === 0) {
    results.push(failResult(configTarget, "effective heartbeat target list is empty"));
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
      return okResult(target, `HTTP ${response.status}`);
    }
    const body = await safeBody(response);
    return failResult(target, `HTTP ${response.status}; expected ${target.statuses.join(",")}; body=${body}`);
  } catch (error) {
    return failResult(target, `fetch failed: ${oneLine(error && error.message ? error.message : String(error))}`);
  } finally {
    clearTimeout(timeout);
  }
}

async function checkHeartbeats(env, heartbeats, nowMs) {
  if (!env.WATCHDOG_STATE) {
    return heartbeats.map((heartbeat) => failResult(heartbeat, "WATCHDOG_STATE KV binding is missing"));
  }

  const results = [];
  for (const heartbeat of heartbeats) {
    const raw = await env.WATCHDOG_STATE.get(heartbeatKey(heartbeat.environment, heartbeat.name));
    if (!raw) {
      results.push(failResult(heartbeat, "heartbeat missing"));
      continue;
    }
    let value;
    try {
      value = JSON.parse(raw);
    } catch (_error) {
      results.push(failResult(heartbeat, "heartbeat payload is invalid JSON"));
      continue;
    }
    const ageSeconds = Math.floor((nowMs - Number(value.receivedAt || 0)) / 1000);
    if (ageSeconds < -300) {
      results.push(failResult(heartbeat, `heartbeat timestamp is in the future: ${ageSeconds}s old`));
      continue;
    }
    if (value.ok === false) {
      results.push(failResult(heartbeat, `heartbeat reports unhealthy: ${oneLine(value.detail || "")}`));
      continue;
    }
    if (ageSeconds > Number(heartbeat.maxAgeSeconds || 1800)) {
      results.push(failResult(heartbeat, `heartbeat stale: ${ageSeconds}s old`));
      continue;
    }
    results.push(okResult(heartbeat, `heartbeat fresh: ${ageSeconds}s old`));
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

  const payload = await request.json();
  const environment = safeId(payload.env || payload.environment || "production");
  const name = safeId(payload.name || "infra-probe-runner");
  const key = heartbeatKey(environment, name);
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
      await sendFeishu(env, formatResolvedMessage());
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
    await sendFeishu(env, formatFailureMessage(failures));
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

function formatFailureMessage(failures) {
  const highestSeverity = failures.some((failure) => failure.severity === "critical") ? "P0" : "P1";
  const lines = [
    "[OUT-OF-BAND] Infra2 Cloudflare watchdog failed",
    `Severity: ${highestSeverity}`,
    "Route: Cloudflare Workers Cron -> Feishu direct",
    "Failures:",
  ];
  for (const failure of failures) {
    lines.push(`- ${failure.environment}/${failure.name}: ${failure.detail}`);
  }
  return lines.join("\n");
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

async function failureFingerprint(failures) {
  const encoded = new TextEncoder().encode(
    JSON.stringify(
      failures.map((failure) => ({
        environment: failure.environment,
        name: failure.name,
        detail: failure.detail,
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

function okResult(target, detail) {
  return {
    environment: target.environment,
    name: target.name,
    severity: target.severity || "warning",
    ok: true,
    detail,
  };
}

function failResult(target, detail) {
  return {
    environment: target.environment,
    name: target.name,
    severity: target.severity || "warning",
    ok: false,
    detail: oneLine(detail),
  };
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

function oneLine(value) {
  return String(value || "").replace(/\s+/g, " ").trim();
}

function jsonResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
