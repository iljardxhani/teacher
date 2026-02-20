// Minimal structured logging + ephemeral on-page status block.
// Works in content scripts and in the MV3 service worker (background) without DOM.
(function initATLogger(global) {
  if (global.AT && typeof global.AT.log === "function") return;

  const hasDocument =
    typeof global.document !== "undefined" &&
    typeof global.document.createElement === "function";

  const MAX_LOGS = 800;
  const logs = [];
  const sessionId = `${Date.now().toString(36)}-${Math.random().toString(16).slice(2)}`;

  // Remote logging (to local router) so runs are saved to disk.
  const ROUTER_LOG_URL = "http://127.0.0.1:5000/log_event";
  const REMOTE_MAX_QUEUE = 2000;
  const remoteQueue = [];
  let remoteFlushInProgress = false;
  let remoteBackoffMs = 0;
  let remoteTimer = null;

  const runState = {
    label: null,
    seq: 0,
    id: null,
    started_at_ts: null
  };

  function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
  }

  function shouldRemoteLog() {
    try {
      return global.AT?.remoteLoggingEnabled === true;
    } catch (_) {
      return false;
    }
  }

  function enqueueRemote(entry) {
    if (!entry) return;
    if (!shouldRemoteLog()) return;
    const canProxy = hasDocument && global.chrome?.runtime?.sendMessage;
    if (typeof fetch !== "function" && !canProxy) return;

    remoteQueue.push(entry);
    while (remoteQueue.length > REMOTE_MAX_QUEUE) remoteQueue.shift();
    scheduleRemoteFlush(0);
  }

  function scheduleRemoteFlush(delayMs) {
    if (remoteTimer) return;
    remoteTimer = setTimeout(() => {
      remoteTimer = null;
      flushRemote();
    }, Math.max(0, Number(delayMs) || 0));
  }

  async function flushRemote() {
    if (remoteFlushInProgress) return;
    if (!shouldRemoteLog()) return;
    if (remoteQueue.length === 0) return;
    remoteFlushInProgress = true;

    try {
      while (remoteQueue.length > 0 && shouldRemoteLog()) {
        const entry = remoteQueue[0];

        // Content scripts are subject to Private Network Access restrictions when fetching
        // loopback URLs from https origins. Prefer proxying via the extension background service
        // worker, which has host_permissions to reach the local router.
        let proxied = false;
        try {
          if (hasDocument && global.chrome?.runtime?.sendMessage) {
            proxied = await new Promise(resolve => {
              try {
                global.chrome.runtime.sendMessage(
                  { type: "at_log_entry", entry },
                  resp => {
                    const err = global.chrome?.runtime?.lastError;
                    if (err) {
                      const msg = err.message || String(err);
                      // This error is common when the receiver doesn't respond, but the message may
                      // still have been processed. Treat it as success to avoid noisy spam.
                      if (msg.includes("The message port closed before a response was received")) {
                        return resolve(true);
                      }
                      return resolve(false);
                    }
                    resolve(resp?.ok === true);
                  }
                );
              } catch (_) {
                resolve(false);
              }
            });
          }
        } catch (_) {
          proxied = false;
        }

        if (proxied) {
          remoteQueue.shift();
          remoteBackoffMs = 0;
          continue;
        }

        // Only attempt a direct fetch in non-DOM contexts (MV3 background service worker) or
        // extension pages. Web pages often cannot access loopback due to PNA/CORS.
        const origin = hasDocument ? String(global.location?.origin || "") : "";
        const canDirectFetch =
          typeof fetch === "function" &&
          (!hasDocument || origin.startsWith("chrome-extension://"));

        if (!canDirectFetch) {
          remoteBackoffMs = Math.min(15000, remoteBackoffMs ? remoteBackoffMs * 2 : 500);
          break;
        }

        try {
          const res = await fetch(ROUTER_LOG_URL, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              source: {
                session_id: sessionId,
                role: entry?.role || tryGetRole(),
                url: entry?.url || null
              },
              entry
            })
          });
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          remoteQueue.shift();
          remoteBackoffMs = 0;
        } catch (err) {
          remoteBackoffMs = Math.min(15000, remoteBackoffMs ? remoteBackoffMs * 2 : 500);
          try {
            console.warn("[AT] remote log send failed; backing off", remoteBackoffMs, err);
          } catch (_) {
            // ignore
          }
          break;
        }

        // Keep UI responsive if we have a lot of logs.
        await sleep(5);
      }
    } finally {
      remoteFlushInProgress = false;
      if (remoteQueue.length > 0 && shouldRemoteLog()) {
        scheduleRemoteFlush(remoteBackoffMs);
      }
    }
  }

  function safeJson(obj) {
    try {
      return JSON.parse(JSON.stringify(obj));
    } catch (_) {
      return { unserializable: true, type: typeof obj };
    }
  }

  function isoNow() {
    try {
      return new Date().toISOString();
    } catch (_) {
      return null;
    }
  }

  function tryGetRole() {
    try {
      if (typeof detectPageRole === "function") return detectPageRole();
    } catch (_) {
      // ignore
    }
    try {
      if (typeof pageRole !== "undefined" && pageRole) return pageRole;
    } catch (_) {
      // ignore
    }
    return "unknown";
  }

  function push(entry) {
    logs.push(entry);
    while (logs.length > MAX_LOGS) logs.shift();
  }

  // ------------------- Toast UI -------------------
  const toastState = {
    el: null,
    hideTimer: null,
    lines: [],
    maxLines: 6
  };

  function ensureToastEl() {
    if (!hasDocument) return null;
    if (toastState.el && toastState.el.isConnected) return toastState.el;

    const el = global.document.createElement("div");
    el.id = "at-flow-toast";
    el.style.position = "fixed";
    el.style.left = "16px";
    el.style.bottom = "16px";
    el.style.zIndex = "2147483647";
    el.style.maxWidth = "420px";
    el.style.padding = "10px 12px";
    el.style.borderRadius = "10px";
    el.style.border = "1px solid rgba(0,0,0,0.18)";
    el.style.background = "rgba(255,255,255,0.96)";
    el.style.color = "#111";
    el.style.boxShadow = "0 10px 28px rgba(0,0,0,0.18)";
    el.style.font = "12px/1.35 system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif";
    el.style.whiteSpace = "pre-wrap";
    el.style.pointerEvents = "none";
    el.style.userSelect = "none";
    el.style.opacity = "0";
    el.style.transform = "translateY(6px)";
    el.style.transition = "opacity 180ms ease, transform 180ms ease";

    const root = global.document.body || global.document.documentElement;
    if (!root) return null;
    root.appendChild(el);

    toastState.el = el;
    return el;
  }

  function formatTime(ts) {
    try {
      const d = new Date(ts);
      const hh = String(d.getHours()).padStart(2, "0");
      const mm = String(d.getMinutes()).padStart(2, "0");
      const ss = String(d.getSeconds()).padStart(2, "0");
      return `${hh}:${mm}:${ss}`;
    } catch (_) {
      return "";
    }
  }

  function toast(text, { ttlMs = 4500 } = {}) {
    if (!hasDocument) return false;
    if (!global.AT?.toastEnabled) return false;
    const el = ensureToastEl();
    if (!el) return false;

    const prefix = runState.id ? `[${runState.id}] ` : "";
    const line = `${formatTime(Date.now())} ${prefix}${String(text || "")}`.trim();
    toastState.lines.push(line);
    while (toastState.lines.length > toastState.maxLines) toastState.lines.shift();

    el.textContent = toastState.lines.join("\n");
    el.style.opacity = "1";
    el.style.transform = "translateY(0)";

    if (toastState.hideTimer) clearTimeout(toastState.hideTimer);
    toastState.hideTimer = setTimeout(() => {
      if (!toastState.el) return;
      toastState.el.style.opacity = "0";
      toastState.el.style.transform = "translateY(6px)";
    }, Math.max(800, Number(ttlMs) || 4500));

    return true;
  }

  // ------------------- Core logging -------------------
  function log(event, data = {}, level = "info") {
    const entry = {
      ts: Date.now(),
      iso: isoNow(),
      session_id: sessionId,
      run_id: runState.id,
      run_seq: runState.seq,
      run_label: runState.label,
      role: tryGetRole(),
      url: hasDocument ? String(global.location?.href || "") : null,
      level: String(level || "info"),
      event: String(event || "event"),
      data: safeJson(data)
    };

    push(entry);

    try {
      const fn = console?.[entry.level] || console.log;
      const parts = ["AT"];
      if (entry.role && entry.role !== "unknown") parts.push(entry.role);
      if (entry.run_id) parts.push(entry.run_id);
      const prefix = `[${parts.join(":")}]`;

      let dataStr = "";
      try {
        dataStr = JSON.stringify(entry.data);
      } catch (_) {
        dataStr = String(entry.data || "");
      }
      if (dataStr.length > 1400) dataStr = dataStr.slice(0, 1400) + "...";

      const line = dataStr && dataStr !== "{}"
        ? `${prefix} ${entry.event} ${dataStr}`
        : `${prefix} ${entry.event}`;

      fn.call(console, line);
    } catch (_) {
      // ignore
    }

    enqueueRemote(entry);
    return entry;
  }

  function uiLog(event, message, data = {}, opts = {}) {
    const level = opts?.level || "info";
    const ttlMs = opts?.ttlMs;
    const entry = log(event, data, level);
    if (message) toast(message, { ttlMs });
    return entry;
  }

  function getLogs() {
    return logs.slice();
  }

  function clearLogs() {
    logs.length = 0;
  }

  function setRunId(id, label = null) {
    runState.id = id ? String(id) : null;
    if (label != null) runState.label = String(label);
    if (runState.id) runState.started_at_ts = Date.now();
    return getRun();
  }

  function startRun(label = "log") {
    runState.label = String(label || "run");
    runState.seq += 1;
    runState.id = `${runState.label}${runState.seq}`;
    runState.started_at_ts = Date.now();
    // Log the run start as a normal event so it's included in dumps.
    log("run_start", { run: getRun() });
    toast(`Run started`, { ttlMs: 5000 });
    return getRun();
  }

  function getRun() {
    return {
      id: runState.id,
      seq: runState.seq,
      label: runState.label,
      started_at_ts: runState.started_at_ts
    };
  }

  function getReport(runId = null) {
    const rid = runId != null ? String(runId) : runState.id;
    const events = rid ? getLogs().filter(e => e.run_id === rid) : getLogs();
    return { session_id: sessionId, run_id: rid, run: getRun(), events };
  }

  function dumpReport(runId = null) {
    try {
      console.log("[AT_REPORT]", JSON.stringify(getReport(runId), null, 2));
    } catch (err) {
      console.log("[AT_REPORT_FAILED]", err);
    }
  }

  function dumpLogs() {
    try {
      console.log("[AT_DUMP]", JSON.stringify(getLogs(), null, 2));
    } catch (err) {
      console.log("[AT_DUMP_FAILED]", err);
    }
  }

  // Capture uncaught errors in the content-script realm.
  if (hasDocument && typeof global.addEventListener === "function") {
    global.addEventListener(
      "error",
      e => {
        const target = e?.target;
        const tag = String(target?.tagName || "").toLowerCase() || null;
        const src = target?.src || target?.href || null;
        const isErrorEvent = typeof ErrorEvent !== "undefined" && e instanceof ErrorEvent;
        const rawMsg = typeof e?.message === "string" ? e.message.trim() : "";
        const msg = rawMsg || "unknown error";
        const filename = e?.filename || null;
        const lineno = Number.isFinite(e?.lineno) ? e.lineno : null;
        const colno = Number.isFinite(e?.colno) ? e.colno : null;
        const stack = e?.error?.stack || null;
        const hasLocation = Boolean(filename) || lineno != null || colno != null;
        const hasStack = typeof stack === "string" && stack.trim().length > 0;
        const lowSignalMessage =
          rawMsg.length === 0 ||
          rawMsg.toLowerCase() === "unknown error" ||
          rawMsg.toLowerCase() === "script error.";
        const parseHost = value => {
          try {
            if (!value) return "";
            return String(new URL(String(value), global.location?.href).hostname || "").toLowerCase();
          } catch (_) {
            return "";
          }
        };
        const resourceHost = parseHost(src);
        const pageHost = String(global.location?.hostname || "").toLowerCase();
        const isCrossOriginResource = Boolean(resourceHost) && Boolean(pageHost) && resourceHost !== pageHost;
        const trackerHosts = new Set([
          "d.adroll.com",
          "bat.bing.com",
          "google-analytics.com",
          "www.google-analytics.com",
          "stats.g.doubleclick.net",
          "connect.facebook.net",
          "www.googletagmanager.com",
          "www.googleadservices.com"
        ]);
        const isTrackerResource = Array.from(trackerHosts).some(
          host => resourceHost === host || resourceHost.endsWith(`.${host}`)
        );
        const isNoisyResourceError =
          (tag === "img" && (isCrossOriginResource || isTrackerResource)) ||
          isTrackerResource;

        // Non-ErrorEvent resource errors (img/script/link load failures) often provide no stack/message.
        // Avoid surfacing them as noisy app failures.
        if (!isErrorEvent && lowSignalMessage && !hasStack && !hasLocation) {
          if (tag || src) {
            if (isNoisyResourceError) return;
            log("window_resource_error", { tag, src }, "warn");
          }
          return;
        }

        // Some opaque browser errors provide no actionable info at all (null filename/line/stack).
        // Downgrade them to warn-level telemetry to avoid hard-failure noise in the UI.
        if (lowSignalMessage && !hasStack && !hasLocation && !e?.error) {
          if (isNoisyResourceError) return;
          log("window_opaque_error", { msg, tag, src }, "warn");
          return;
        }

        uiLog(
          "window_error",
          `Error: ${msg}`,
          { msg, filename, lineno, colno, stack },
          { level: "error", ttlMs: 6500 }
        );
      },
      true
    );

    global.addEventListener("unhandledrejection", e => {
      const reason = e?.reason;
      uiLog(
        "unhandled_rejection",
        "Unhandled rejection",
        { reason: safeJson(reason), reason_str: String(reason || "") },
        { level: "error", ttlMs: 6500 }
      );
    });
  }

  global.AT = {
    sessionId,
    toastEnabled: true,
    remoteLoggingEnabled: true,
    log,
    uiLog,
    toast,
    flushRemoteLogs: flushRemote,
    getLogs,
    clearLogs,
    dumpLogs,
    startRun,
    setRunId,
    getRun,
    getReport,
    dumpReport
  };
})(typeof globalThis !== "undefined" ? globalThis : window);
