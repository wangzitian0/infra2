import { pathToFileURL } from "node:url";

const worker = (await import(pathToFileURL(process.argv[2]).href)).default;
const now = Date.UTC(2026, 8, 23, 12, 0, 0);
const url = "https://hc-ping.com/test-worker-check";
const events = [];
console.log = () => {};
globalThis.fetch = async (input) => {
  events.push(String(input));
  return new Response("ok", { status: 200 });
};

function env({ failPut = false, pingUrl = url } = {}) {
  const map = new Map([
    ["heartbeat:production:test-runner", JSON.stringify({ receivedAt: now, ok: true })],
  ]);
  return {
    WATCHDOG_ENVIRONMENTS: "production",
    WATCHDOG_TARGETS_JSON: JSON.stringify([{
      environment: "production", name: "test-route", service_id: "test/app",
      url: "https://target.invalid/health", statuses: [200], severity: "critical",
    }]),
    WATCHDOG_HEARTBEATS_JSON: JSON.stringify([{
      environment: "production", name: "test-runner", service_id: "test/app",
      maxAgeSeconds: 3600, severity: "critical",
    }]),
    WATCHDOG_DEADMAN_PING_URL: pingUrl,
    WATCHDOG_STATE: {
      async get(key) { return map.get(key) || null; },
      async put(key, value) {
        if (failPut) throw new Error("simulated KV failure");
        map.set(key, value);
      },
      async delete(key) { map.delete(key); },
    },
  };
}

async function scheduled(options) {
  const pending = [];
  try {
    await worker.scheduled({ scheduledTime: now }, env(options), {
      waitUntil(promise) { pending.push(promise); },
    });
    await Promise.all(pending);
    return { ok: true, calls: events.splice(0) };
  } catch (error) {
    return { ok: false, error: String(error.message), calls: events.splice(0) };
  }
}

const result = {
  success: await scheduled(),
  whitespaceUrl: await scheduled({ pingUrl: ` \n${url} \n` }),
  workerFailure: await scheduled({ failPut: true }),
  missingUrl: await scheduled({ pingUrl: "" }),
};
process.stdout.write(JSON.stringify(result));
