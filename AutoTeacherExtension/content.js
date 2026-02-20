console.log("content.js loaded successfully");

function atLog(event, data = {}, level = "info") {
  try {
    globalThis.AT?.log?.(event, data, level);
  } catch (_) {
    // ignore
  }
}

function atUiLog(event, message, data = {}, opts = {}) {
  try {
    globalThis.AT?.uiLog?.(event, message, data, opts);
  } catch (_) {
    // ignore
  }
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

async function waitForSelector(selector, { timeoutMs = 20000, intervalMs = 250 } = {}) {
  const startedAt = Date.now();
  while (Date.now() - startedAt < timeoutMs) {
    try {
      const el = document.querySelector(selector);
      if (el) return el;
    } catch (_) {
      // ignore
    }
    await sleep(intervalMs);
  }
  return null;
}

let pageRole = "unknown";
let stat = null;
try {
  if (typeof detectPageRole === "function") {
    pageRole = detectPageRole();   // e.g. "ai", "class", "teacher", "stt"
  }
} catch (err) {
  console.warn("detectPageRole() failed, defaulting to 'unknown':", err);
}

atLog("content_loaded", { pageRole, url: window.location.href });

let trafficState = "busy";
let automationStarted = false;
const pendingRouterMessages = [];
// Prevent TDZ errors when flushRouterQueue() is triggered before the router section
// in this file finishes executing (e.g. auto-load starts very early on fast pages).
let routerFlushInProgress = false;

// Give page scripts (and the extension's other content scripts) a moment to settle.
// This also prevents early calls into router code before it's initialized.
const AUTOMATION_START_DELAY_MS = 2000;

// Auto-start class flow when the textbook is detected.
const CLASS_AUTOSTART_ENABLED = true;
// Temporary override requested: keep traffic always free on class pages.
const CLASS_FORCE_TRAFFIC_FREE = true;
// STT uses explicit lock/unlock control from stt.js + teacher signals.
const STT_FORCE_TRAFFIC_FREE = false;

const runtime = typeof chrome !== "undefined" ? chrome.runtime : null;

function isTempWalkieClassPage() {
  try {
    const roleNow = resolvePageRoleNow();
    if (roleNow !== "class") return false;
    return typeof isWalkieReceiverPage === "function" && isWalkieReceiverPage();
  } catch (_) {
    return false;
  }
}

// MV3 service workers are ephemeral; keep a lightweight long-lived connection open so the
// background worker stays awake while the automation tabs are open.
const KEEPALIVE_PING_INTERVAL_MS = 25000;
let keepAlivePort = null;
let keepAliveTimer = null;
let keepAliveReconnectBackoffMs = 500;
let keepAlivePaused = false;
let extensionContextInvalidated = false;

function isContextInvalidatedErrorMessage(msg) {
  return /extension context invalidated/i.test(String(msg || ""));
}

function markExtensionContextInvalidated(source = "unknown", errMsg = null) {
  if (extensionContextInvalidated) return;
  extensionContextInvalidated = true;
  keepAlivePort = null;
  stopKeepAliveTimer();
  atLog(
    "extension_context_invalidated",
    { source, err: errMsg ? String(errMsg) : null },
    "warn"
  );
}

function stopKeepAliveTimer() {
  if (keepAliveTimer) clearInterval(keepAliveTimer);
  keepAliveTimer = null;
}

function stopKeepAlivePort(reason = "unknown", { pause = true } = {}) {
  keepAlivePaused = pause === true;
  try {
    if (keepAlivePort) {
      try {
        keepAlivePort.disconnect();
      } catch (_) {
        // ignore
      }
    }
  } finally {
    keepAlivePort = null;
    stopKeepAliveTimer();
  }
  atLog("keepalive_stopped", { reason, paused: keepAlivePaused });
}

function startKeepAlivePort(opts = {}) {
  if (extensionContextInvalidated) return false;
  if (!runtime?.connect) return false;
  const force = opts?.force === true;
  if (force) stopKeepAlivePort("force_restart", { pause: false });
  if (keepAlivePort) return true;
  keepAlivePaused = false;

  try {
    keepAlivePort = runtime.connect({ name: "at_keepalive" });
  } catch (err) {
    const errMsg = String(err?.message || err);
    if (isContextInvalidatedErrorMessage(errMsg)) {
      markExtensionContextInvalidated("startKeepAlivePort.connect", errMsg);
      return false;
    }
    console.warn("keepAlive: connect failed:", err);
    atLog("keepalive_connect_failed", { err: errMsg }, "warn");
    keepAlivePort = null;
    return false;
  }

  keepAliveReconnectBackoffMs = 500;

  const ping = () => {
    if (!keepAlivePort) return;
    try {
      const roleNow = typeof resolvePageRoleNow === "function" ? resolvePageRoleNow() : pageRole;
      keepAlivePort.postMessage({ type: "keepalive_ping", role: roleNow, ts: Date.now() });
    } catch (_) {
      // ignore
    }
  };

  try {
    keepAlivePort.onDisconnect.addListener(() => {
      const errMsg = runtime?.lastError?.message || null;
      keepAlivePort = null;
      stopKeepAliveTimer();

      if (isContextInvalidatedErrorMessage(errMsg)) {
        markExtensionContextInvalidated("keepAlivePort.onDisconnect", errMsg);
        return;
      }

      // If we intentionally stopped for BFCache/freeze, don't immediately reconnect.
      if (keepAlivePaused) {
        atLog("keepalive_disconnected", { reason: "paused", err: errMsg });
        return;
      }

      // Content scripts get torn down on navigations; avoid noisy reconnect loops.
      const backoff = Math.min(30000, Math.max(500, keepAliveReconnectBackoffMs));
      keepAliveReconnectBackoffMs = Math.min(30000, keepAliveReconnectBackoffMs * 2);
      setTimeout(() => {
        try {
          startKeepAlivePort();
        } catch (_) {
          // ignore
        }
      }, backoff);
    });
  } catch (_) {
    // ignore
  }

  // Start immediately so the SW wakes right away.
  ping();
  keepAliveTimer = setInterval(ping, KEEPALIVE_PING_INTERVAL_MS);
  return true;
}

function safeRuntimeSendMessageNoAck(payload, label = "runtime message") {
  if (extensionContextInvalidated) return false;
  if (!runtime?.sendMessage) {
    console.warn(`chrome.runtime.sendMessage unavailable; ${label} skipped.`);
    atLog("runtime_send_unavailable", { label, payload }, "warn");
    return false;
  }
  try {
    // Fire-and-forget: do not attach a callback, so Chrome can't surface the
    // "message port closed before a response was received" warning.
    const p = runtime.sendMessage(payload);
    // Some Chrome versions return a Promise when no callback is provided; avoid unhandled rejections.
    if (p && typeof p.then === "function") {
      p.catch(err => {
        const msg = String(err?.message || err);
        if (isContextInvalidatedErrorMessage(msg)) {
          markExtensionContextInvalidated(`safeRuntimeSendMessageNoAck(${label})`, msg);
          return;
        }
      });
    }
    return true;
  } catch (err) {
    const msg = String(err?.message || err);
    if (isContextInvalidatedErrorMessage(msg)) {
      markExtensionContextInvalidated(`safeRuntimeSendMessageNoAck(${label})`, msg);
      return false;
    }
    console.warn(`chrome.runtime.sendMessage threw (${label}):`, err);
    atLog("runtime_send_threw", { label, err: msg, payload }, "warn");
    return false;
  }
}

function safeRuntimeSendMessage(payload, label = "runtime message") {
  if (extensionContextInvalidated) return false;
  if (!runtime?.sendMessage) {
    console.warn(`chrome.runtime.sendMessage unavailable; ${label} skipped.`);
    atLog("runtime_send_unavailable", { label, payload }, "warn");
    return false;
  }
  try {
    runtime.sendMessage(payload, () => {
      const err = runtime.lastError;
      if (!err) return;
      const msg = err.message || String(err);
      // Very common during fast navigations/reloads; not actionable for register_tab.
      if (
        msg.includes("The message port closed before a response was received") &&
        (label.startsWith("register_tab") || payload?.type === "register_tab")
      ) {
        return;
      }
      if (isContextInvalidatedErrorMessage(msg)) {
        markExtensionContextInvalidated(`safeRuntimeSendMessage(${label})`, msg);
        return;
      }
      console.warn(`chrome.runtime.sendMessage failed (${label}):`, msg);
      atLog("runtime_send_failed", { label, err: msg, payload }, "warn");
    });
    return true;
  } catch (err) {
    const msg = String(err?.message || err);
    if (isContextInvalidatedErrorMessage(msg)) {
      markExtensionContextInvalidated(`safeRuntimeSendMessage(${label})`, msg);
      return false;
    }
    console.warn(`chrome.runtime.sendMessage threw (${label}):`, err);
    atLog("runtime_send_threw", { label, err: msg, payload }, "warn");
    return false;
  }
}

function resolvePageRoleNow() {
  const prevRole = pageRole;
  let role = pageRole;
  try {
    if (typeof detectPageRole === "function") {
      role = detectPageRole();
    }
  } catch (err) {
    console.warn("detectPageRole() failed during resolve:", err);
    role = prevRole;
  }

  const nextRole = role || "unknown";

  // If role becomes known after early load, start the orchestration.
  if (!automationStarted && nextRole !== "unknown") {
    try {
      startAutomation("role-change");
    } catch (_) {
      // ignore
    }
  }

  if (nextRole !== prevRole) {
    pageRole = nextRole;
    safeRuntimeSendMessageNoAck({ type: "register_tab", role: pageRole }, "register_tab (role update)");
    atUiLog("role_changed", `Role: ${prevRole} -> ${pageRole}`, { prev: prevRole, next: pageRole, url: window.location.href });

    // If we enter the class role (common on /teacher/home after redirects), auto-start the class flow.
    if (pageRole === "class" && CLASS_AUTOSTART_ENABLED && typeof startClassAutomation === "function") {
      const walkieTemp = typeof isWalkieReceiverPage === "function" && isWalkieReceiverPage();
      if (walkieTemp) {
        // TEMP_WALKIE_MODE: never run textbook automation on local walkie receiver page.
        atUiLog("temp_walkie_role_change_skip", "Class autostart skipped (TEMP_WALKIE_MODE)", { prev: prevRole, next: pageRole, url: window.location.href });
      } else {
        try {
          startClassAutomation();
          atUiLog("class_autostart", "Class: auto-starting flow", { prev: prevRole, next: pageRole, url: window.location.href });
        } catch (err) {
          console.warn("startClassAutomation() failed during role change:", err);
        }
      }
    }
  }

  return nextRole;
}

let roleWatchdogTimer = null;
function startRoleWatchdog() {
  if (roleWatchdogTimer) return;
  try {
    const host = String(window.location.hostname || "").toLowerCase();
    if (!host.includes("nativecamp.net")) return;
  } catch (_) {
    return;
  }

  const startedAt = Date.now();
  roleWatchdogTimer = setInterval(() => {
    try {
      const roleNow = resolvePageRoleNow();
      if (roleNow === "class") {
        clearInterval(roleWatchdogTimer);
        roleWatchdogTimer = null;
        return;
      }
      if (Date.now() - startedAt > 60000) {
        clearInterval(roleWatchdogTimer);
        roleWatchdogTimer = null;
      }
    } catch (_) {
      // ignore
    }
  }, 500);
}

function safeRuntimeAddListener(handler, label = "runtime listener") {
  if (!runtime?.onMessage?.addListener) {
    console.warn(`chrome.runtime.onMessage unavailable; ${label} skipped.`);
    return false;
  }
  runtime.onMessage.addListener(handler);
  return true;
}

// If a tab is put into BFCache, Chrome will forcibly close extension ports and can log:
// "The page keeping the extension port is moved into back/forward cache..."
// Proactively stop/restart our keepalive port on lifecycle events to avoid noisy errors and
// to ensure the service worker reconnects cleanly after BFCache restore.
function initPageLifecycleKeepAlive() {
  try {
    if (typeof window !== "undefined" && window.addEventListener) {
      window.addEventListener("pagehide", e => {
        try {
          if (e?.persisted) stopKeepAlivePort("pagehide_bfcache");
        } catch (_) {
          // ignore
        }
      });

      window.addEventListener("pageshow", e => {
        try {
          if (!e?.persisted) return;
          startKeepAlivePort({ force: true });
          const roleNow = resolvePageRoleNow();
          safeRuntimeSendMessageNoAck({ type: "register_tab", role: roleNow }, "register_tab (pageshow)");
        } catch (_) {
          // ignore
        }
      });
    }
  } catch (_) {
    // ignore
  }

  try {
    if (typeof document !== "undefined" && document.addEventListener) {
      document.addEventListener("freeze", () => {
        try {
          stopKeepAlivePort("freeze");
        } catch (_) {
          // ignore
        }
      });

      document.addEventListener("resume", () => {
        try {
          startKeepAlivePort({ force: true });
          const roleNow = resolvePageRoleNow();
          safeRuntimeSendMessageNoAck({ type: "register_tab", role: roleNow }, "register_tab (resume)");
        } catch (_) {
          // ignore
        }
      });
    }
  } catch (_) {
    // ignore
  }
}

initPageLifecycleKeepAlive();

startKeepAlivePort();
startRoleWatchdog();

safeRuntimeSendMessageNoAck({
  type: "register_tab",
  role: pageRole
}, "register_tab");
atLog("register_tab_sent", { role: pageRole });

// Ensure UI badge renders after reloads even without router init.
if (typeof document !== "undefined") {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => {
      renderStatusBadge();
      renderCornerBoxes();
      if (pageRole !== "unknown") {
        startAutomation("auto-load");
      }
    });
  } else {
    renderStatusBadge();
    renderCornerBoxes();
    if (pageRole !== "unknown") {
      startAutomation("auto-load");
    }
  }
}

function renderStatusBadge(attempt = 0) {
  try {
    const doc = typeof document !== "undefined" ? document : null;
    if (!doc || !doc.getElementById) {
      if (attempt < 50) {
        setTimeout(() => renderStatusBadge(attempt + 1), 100);
      }
      return;
    }
    if (doc.getElementById("at-status-badge")) return;

    const root = doc.body || doc.documentElement;
    if (!root) {
      if (attempt < 50) {
        setTimeout(() => renderStatusBadge(attempt + 1), 100);
      }
      return;
    }

    const badge = document.createElement("div");
    badge.id = "at-status-badge";
    badge.style.position = "fixed";
    badge.style.top = "1.2%";
    badge.style.left = "1.2%";
    badge.style.zIndex = "2147483647";
    badge.style.display = "flex";
    badge.style.alignItems = "center";
    badge.style.justifyContent = "flex-start";
    badge.style.gap = "0.45em";
    badge.style.padding = "0.4em 0.65em";
    badge.style.width = "fit-content";
    badge.style.maxWidth = "22%";
    badge.style.minWidth = "6%";
    badge.style.background = "rgba(255,255,255,0.9)";
    badge.style.color = "#0d0d0d";
    badge.style.font = "600 clamp(11px, 0.95vw, 15px)/1.2 Arial, sans-serif";
    badge.style.borderRadius = "0.7em";
    badge.style.border = "1px solid rgba(0, 0, 0, 0.2)";
    badge.style.boxShadow = "0 4px 14px rgba(0,0,0,0.16)";
    badge.style.pointerEvents = "none";
    badge.style.userSelect = "none";

    const dot = document.createElement("span");
    dot.id = "at-status-dot";
    dot.style.width = "0.75em";
    dot.style.height = "0.75em";
    dot.style.borderRadius = "999px";
    dot.style.flex = "0 0 auto";

    const status = document.createElement("span");
    status.id = "at-status-text";
    status.textContent = trafficState === "free" ? "free" : "busy";
    status.style.opacity = "0.92";
    status.style.fontSize = "1em";
    status.style.textTransform = "uppercase";
    status.style.letterSpacing = "0.06em";

    badge.appendChild(dot);
    badge.appendChild(status);
    root.appendChild(badge);
    updateStatusBadgeVisuals();
  } catch (err) {
    console.warn("Status badge failed to render:", err);
  }
}

function renderCornerBoxes(attempt = 0) {
  try {
    const topId = "at-top-box";
    const bottomId = "at-bottom-box";
    const doc = typeof document !== "undefined" ? document : null;
    if (!doc || !doc.getElementById) {
      if (attempt < 50) {
        setTimeout(() => renderCornerBoxes(attempt + 1), 100);
      }
      return;
    }
    if (doc.getElementById(topId) && doc.getElementById(bottomId)) return;

    const root = doc.body || doc.documentElement;
    if (!root) {
      if (attempt < 50) {
        setTimeout(() => renderCornerBoxes(attempt + 1), 100);
      }
      return;
    }

    if (!doc.getElementById(topId)) {
      const topBox = document.createElement("div");
      topBox.id = topId;
      topBox.style.position = "fixed";
      topBox.style.top = "0.6%";
      topBox.style.right = "0.6%";
      topBox.style.width = "3.5%";
      topBox.style.height = "1.8%";
      topBox.style.minWidth = "30px";
      topBox.style.maxWidth = "60px";
      topBox.style.minHeight = "8px";
      topBox.style.background = "#fff";
      topBox.style.border = "1px solid rgba(0, 0, 0, 0.2)";
      topBox.style.borderRadius = "3px";
      topBox.style.zIndex = "2147483647";
      topBox.style.pointerEvents = "none";
      topBox.style.userSelect = "none";
      root.appendChild(topBox);
    }

    if (!doc.getElementById(bottomId)) {
      const bottomBox = document.createElement("div");
      bottomBox.id = bottomId;
      bottomBox.style.position = "fixed";
      bottomBox.style.bottom = "0.6%";
      bottomBox.style.right = "0.6%";
      bottomBox.style.width = "3.5%";
      bottomBox.style.height = "1.8%";
      bottomBox.style.minWidth = "30px";
      bottomBox.style.maxWidth = "60px";
      bottomBox.style.minHeight = "8px";
      bottomBox.style.background = "#fff";
      bottomBox.style.border = "1px solid rgba(0, 0, 0, 0.2)";
      bottomBox.style.borderRadius = "3px";
      bottomBox.style.zIndex = "2147483647";
      bottomBox.style.pointerEvents = "none";
      bottomBox.style.userSelect = "none";
      root.appendChild(bottomBox);
    }
  } catch (err) {
    console.warn("Corner boxes failed to render:", err);
  }
}

function updateStatusBadgeVisuals() {
  const doc = typeof document !== "undefined" ? document : null;
  if (!doc || !doc.getElementById) return;

  const badge = doc.getElementById("at-status-badge");
  const statusEl = doc.getElementById("at-status-text");
  const dotEl = doc.getElementById("at-status-dot");
  const isFree = trafficState === "free";

  if (statusEl) {
    statusEl.textContent = isFree ? "free" : "busy";
    statusEl.style.color = isFree ? "#146c2e" : "#b42318";
  }
  if (dotEl) {
    dotEl.style.background = isFree ? "#20c05c" : "#e5484d";
    dotEl.style.boxShadow = isFree
      ? "0 0 0 0.18em rgba(32,192,92,0.22)"
      : "0 0 0 0.18em rgba(229,72,77,0.22)";
  }
  if (badge) {
    badge.style.borderColor = isFree ? "rgba(20,108,46,0.36)" : "rgba(180,35,24,0.36)";
    badge.style.background = isFree ? "rgba(244,255,248,0.9)" : "rgba(255,245,245,0.9)";
  }
}

function startAutomation(reason = "unknown") {
  if (automationStarted) return;
  automationStarted = true;
  console.log(`Starting extension logic in ${AUTOMATION_START_DELAY_MS}ms (${reason})`);
  atUiLog(
    "automation_start",
    `Automation scheduled (+${AUTOMATION_START_DELAY_MS}ms) (${pageRole})`,
    { reason, pageRole, url: window.location.href, delay_ms: AUTOMATION_START_DELAY_MS }
  );
  renderStatusBadge();
  renderCornerBoxes();

  const run = () => {
    try {
      runPageAutomation();
    } catch (err) {
      console.warn("runPageAutomation() failed:", err);
      atLog("runPageAutomation_failed", { err: String(err?.message || err) }, "warn");
    }
  };

  if (AUTOMATION_START_DELAY_MS > 0) setTimeout(run, AUTOMATION_START_DELAY_MS);
  else run();
}

function setTrafficState(state) {
  const prev = trafficState;
  let requestedState = null;
  if (state === "b" || state === "busy") {
    requestedState = "busy";
  } else if (state === "f" || state === "free") {
    requestedState = "free";
  } else {
    console.warn("Unknown traffic state:", state);
    return;
  }

  let roleNow = pageRole;
  try {
    if (typeof detectPageRole === "function") roleNow = detectPageRole();
  } catch (_) {
    // ignore
  }

  const forceFree =
    (CLASS_FORCE_TRAFFIC_FREE && roleNow === "class") ||
    (STT_FORCE_TRAFFIC_FREE && roleNow === "stt");

  if (forceFree) {
    trafficState = "free";
    if (requestedState !== "free") {
      atLog("traffic_state_forced_free", {
        role: roleNow,
        requested: requestedState
      });
    }
  } else {
    trafficState = requestedState;
  }

  if (prev !== trafficState) {
    atLog("traffic_state", { prev, next: trafficState });
  }

  const statusEl = typeof document !== "undefined"
    ? document.getElementById("at-status-text")
    : null;
  if (statusEl) {
    statusEl.textContent = trafficState;
  }
  updateStatusBadgeVisuals();

  if (trafficState === "free") {
    flushRouterQueue();
  }
}
setTrafficState(trafficState)

// Wait until the local traffic flag is free before proceeding with a task.
function runWhenTrafficFree(task, label = "task", interval = 300) {
  const isFree = () => trafficState === "free";
  const retry = () => setTimeout(run, interval);

  function run() {
    if (isFree()) {
      task();
    } else {
      console.log(`⏳ ${label} postponed; traffic=${trafficState}`);
      retry();
    }
  }

  run();
}

// --- Listen for stat updates / init from background ---
safeRuntimeAddListener((msg, sender, sendResponse) => {
  if (msg?.action === "get_page_role") {
    const roleNow = resolvePageRoleNow();
    if (typeof sendResponse === "function") {
      sendResponse({ role: roleNow });
    }
    return;
  }

  if (msg?.action === "class_get_textbook_loaded") {
    const roleNow = resolvePageRoleNow();
    if (isTempWalkieClassPage()) {
      atUiLog(
        "temp_walkie_class_get_textbook_disabled",
        "Class textbook query disabled (TEMP_WALKIE_MODE)",
        { roleNow, url: window.location.href },
        { level: "warn", ttlMs: 6500 }
      );
      if (typeof sendResponse === "function") {
        sendResponse({ ok: false, error: "disabled_in_temp_walkie_mode", role: roleNow, temp_walkie_mode: true });
      }
      return;
    }
    if (roleNow !== "class") {
      if (typeof sendResponse === "function") {
        sendResponse({ ok: false, error: "not_class", role: roleNow });
      }
      return;
    }
    try {
      const iframe = document.querySelector("#textbook-iframe");
      const htmlDirectory = iframe?.getAttribute("html-directory") || null;
      const orderFlag = iframe?.getAttribute("order-flag") || null;
      const iframeSrc = iframe?.getAttribute("src") || iframe?.src || null;
      const loaded =
        (typeof textbookTypeName !== "undefined" && textbookTypeName)
          ? textbookTypeName
          : htmlDirectory;

      const info = {
        role: "class",
        url: window.location.href,
        loadedTextbook: loaded || null,
        htmlDirectory,
        orderFlag,
        iframeSrc
      };
      if (typeof sendResponse === "function") {
        sendResponse({ ok: true, info });
      }
    } catch (err) {
      if (typeof sendResponse === "function") {
        sendResponse({ ok: false, error: String(err?.message || err) });
      }
    }
    return true; // keep sendResponse alive
  }

  if (msg?.action === "class_fire_detect_textbook_type") {
    const roleNow = resolvePageRoleNow();
    if (isTempWalkieClassPage()) {
      atUiLog(
        "temp_walkie_detect_disabled",
        "Class detect/send disabled (TEMP_WALKIE_MODE)",
        { roleNow, url: window.location.href },
        { level: "warn", ttlMs: 6500 }
      );
      if (typeof sendResponse === "function") {
        sendResponse({ ok: false, error: "disabled_in_temp_walkie_mode", role: roleNow, temp_walkie_mode: true });
      }
      return;
    }
    if (roleNow !== "class") {
      atUiLog("class_fire_detect_not_class", "Not on class page", { roleNow }, { level: "warn", ttlMs: 6500 });
      if (typeof sendResponse === "function") {
        sendResponse({ ok: false, error: "not_class", role: roleNow });
      }
      return;
    }
    try {
      const rawMode = String(msg?.mode || "prepare").toLowerCase();
      const mode = rawMode === "send" ? "send" : "prepare";

      if (typeof runClassTextbookFlow !== "function") {
        atUiLog("runClassTextbookFlow_missing", "runClassTextbookFlow() missing", {}, { level: "error", ttlMs: 6500 });
        if (typeof sendResponse === "function") {
          sendResponse({ ok: false, error: "runClassTextbookFlow_missing" });
        }
        return;
      }

      console.log("🧩 popup -> class_fire_detect_textbook_type received");
      atUiLog(
        "class_fire_detect",
        mode === "send" ? "Class: scrape + send lesson package" : "Class: scrape textbook (prepare only)",
        { mode }
      );

      (async () => {
        try {
          const result = await runClassTextbookFlow({ mode, source: "popup_button" });
          if (typeof sendResponse === "function") sendResponse(result || { ok: true, mode });
        } catch (err) {
          console.warn("runClassTextbookFlow() failed:", err);
          if (typeof sendResponse === "function") sendResponse({ ok: false, error: String(err?.message || err) });
        }
      })();
      return true;
    } catch (err) {
      if (typeof sendResponse === "function") {
        sendResponse({ ok: false, error: String(err?.message || err) });
      }
    }
    return;
  }

  if (msg?.action === "class_start_flow") {
    (async () => {
      let run = null;
      try {
        run = globalThis.AT?.startRun?.("log") || null;
      } catch (_) {
        run = null;
      }
      const initialRole = resolvePageRoleNow();
      atUiLog("class_start_flow", "Class: Start Flow requested", { run, initialRole, url: window.location.href });

      // If role is not class yet (often on /teacher/home), wait briefly for the class DOM.
      if (initialRole !== "class") {
        atUiLog(
          "class_start_flow_wait_dom",
          "Waiting for class UI...",
          { run, initialRole },
          { ttlMs: 5000 }
        );
        await waitForSelector("#textbook-iframe", { timeoutMs: 20000, intervalMs: 250 });
      }

      const roleNow = resolvePageRoleNow();
      if (isTempWalkieClassPage()) {
        atUiLog(
          "temp_walkie_start_flow_disabled",
          "Class start flow disabled (TEMP_WALKIE_MODE)",
          { run, roleNow, url: window.location.href },
          { level: "warn", ttlMs: 6500 }
        );
        if (typeof sendResponse === "function") {
          sendResponse({ ok: false, error: "disabled_in_temp_walkie_mode", role: roleNow, temp_walkie_mode: true });
        }
        return;
      }
      if (roleNow !== "class") {
        atUiLog("class_start_flow_not_class", "Not on class page", { run, roleNow }, { level: "warn", ttlMs: 6500 });
        if (typeof sendResponse === "function") {
          sendResponse({ ok: false, error: "not_class", role: roleNow });
        }
        return;
      }

      console.log("🧩 popup -> class_start_flow received");

      try {
        if (typeof startClassAutomation !== "function") {
          atUiLog("class_start_flow_missing", "startClassAutomation() missing", {}, { level: "error", ttlMs: 6500 });
          if (typeof sendResponse === "function") {
            sendResponse({ ok: false, error: "startClassAutomation_missing" });
          }
          return;
        }

        // Backward-compatibility: manual "start flow" now starts the full flow (prep + send).
        startClassAutomation({ mode: "send" });
        atUiLog("class_start_flow_started", "Class flow started", { run });
        if (typeof sendResponse === "function") sendResponse({ ok: true });
      } catch (err) {
        console.warn("startClassAutomation() failed:", err);
        atUiLog(
          "class_start_flow_failed",
          "Class flow failed",
          { run, err: String(err?.message || err) },
          { level: "error", ttlMs: 6500 }
        );
        if (typeof sendResponse === "function") {
          sendResponse({ ok: false, error: String(err?.message || err) });
        }
      }
    })();
    return true;
  }

  if (msg?.from === "background" && msg?.type === "statUpdate" && msg?.stat) {
    stat = msg.stat;  // overwrite local stat with the latest
    console.log("🔥 Stat updated in content.js:", stat);
    atLog("stat_update", { stat });
    return;
  }

  const payload = msg?.message || msg;
  if (payload?.type === "init") {
    atLog("router_init", { payload });
    startAutomation("router-init");
  }
}, "stat/init listener");

function generateRouterMessageId() {
  try {
    if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
      return crypto.randomUUID();
    }
  } catch (_) {
    // ignore
  }
  return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

async function sendRouterMessage(message, receiver, id = null) {
  const sender = (() => {
    try {
      if (typeof resolvePageRoleNow === "function") return resolvePageRoleNow();
      if (typeof detectPageRole === "function") return detectPageRole();
    } catch (_) {
      // ignore
    }
    return pageRole || "unknown";
  })();
  if (!receiver) {
    console.error("sendRouterMessage: receiver is required");
    atLog("router_send_invalid", { reason: "missing_receiver", sender }, "warn");
    return false;
  }

  let text = null;
  let extra = null;
  if (typeof message === "string") {
    text = message;
  } else if (message && typeof message === "object" && typeof message.text === "string") {
    extra = message;
    text = message.text;
  }

  if (typeof text !== "string" || text.trim() === "") {
    console.error("sendRouterMessage: message text is required");
    atLog("router_send_invalid", { reason: "missing_text", sender, receiver }, "warn");
    return false;
  }
  // ===========================
  const msgId = id || generateRouterMessageId();
  const payload = extra
    ? { ...extra, id: msgId, sender, receiver, text }
    : { id: msgId, sender, receiver, text };

  console.log("📤 sendRouterMessage() dispatching payload:", payload);
  atLog("router_send_attempt", { id: msgId, sender, receiver, kind: payload?.kind || null, text_len: text.length });

  // Prefer proxying through the extension background service worker.
  // Content scripts are often blocked from accessing loopback URLs due to Private Network Access.
  if (runtime?.sendMessage) {
    try {
      const proxied = await new Promise(resolve => {
        try {
          runtime.sendMessage(
            { type: "router_send", from: sender, to: receiver, message: payload },
            response => {
              const e = runtime.lastError;
              if (e) {
                const msg = e.message || String(e);
                // Match textbook sender behavior: this error often means the proxy handled
                // the request but didn't keep the response port open long enough.
                if (msg.includes("The message port closed before a response was received")) {
                  return resolve({ ok: true, via: "background_proxy", ack: false });
                }
                return resolve({ ok: false, error: msg });
              }
              resolve(response || { ok: true });
            }
          );
        } catch (e) {
          resolve({ ok: false, error: String(e?.message || e) });
        }
      });

      if (proxied?.ok) {
        atLog("router_send_ok", {
          id: msgId,
          sender,
          receiver,
          kind: payload?.kind || null,
          via: proxied?.via || "background_proxy",
          ack: proxied?.ack === false ? false : true
        });
        return true;
      }

      atUiLog(
        "router_send_proxy_failed",
        "Router send failed (background proxy)",
        { id: msgId, sender, receiver, kind: payload?.kind || null, proxied },
        { level: "warn", ttlMs: 6500 }
      );
    } catch (_) {
      // ignore
    }
  }

  // Fallback: direct fetch only in extension pages (content scripts are typically blocked).
  const origin = typeof window !== "undefined" ? String(window.location?.origin || "") : "";
  if (!origin.startsWith("chrome-extension://")) return false;

  try {
    const response = await fetch("http://127.0.0.1:5000/send_message", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        from: sender,
        to: receiver,
        message: payload
      })
    });

    if (!response.ok) {
      const error = await response.json().catch(() => null);
      console.error("Failed to send message:", error);
      atUiLog(
        "router_send_failed",
        `Router send failed (${response.status})`,
        { id: msgId, sender, receiver, kind: payload?.kind || null, error, status: response.status },
        { level: "warn", ttlMs: 6500 }
      );
      return false;
    }

    const data = await response.json().catch(() => null);
    console.log("Message sent successfully:", data);
    atLog("router_send_ok", { id: msgId, sender, receiver, kind: payload?.kind || null, response: data });
    return true;
  } catch (err) {
    console.error("Error sending message:", err);
    atUiLog(
      "router_send_error",
      "Router send error",
      { id: msgId, sender, receiver, kind: payload?.kind || null, err: String(err?.message || err) },
      { level: "error", ttlMs: 6500 }
    );
    return false;
  }
}

function queueRouterMessage(message, receiver) {
  if (!receiver) {
    console.error("queueRouterMessage: receiver is required");
    atLog("router_queue_invalid", { receiver }, "warn");
    return;
  }

  const text =
    typeof message === "string"
      ? message
      : (message && typeof message === "object" ? message.text : null);

  if (typeof text !== "string" || text.trim() === "") {
    console.error("queueRouterMessage: invalid message", { receiver, message });
    atLog("router_queue_invalid", { receiver, text_len: typeof text === "string" ? text.length : null }, "warn");
    return;
  }

  pendingRouterMessages.push({ id: generateRouterMessageId(), message, receiver });
  atLog("router_queued", { receiver, pending: pendingRouterMessages.length, kind: typeof message === "object" ? message?.kind : null });
  flushRouterQueue();
}

async function flushRouterQueue() {
  if (routerFlushInProgress) return;
  if (trafficState !== "free") return;
  if (pendingRouterMessages.length > 0) {
    atLog("router_flush_start", { pending: pendingRouterMessages.length });
  }

  routerFlushInProgress = true;
  try {
    while (trafficState === "free" && pendingRouterMessages.length > 0) {
      const next = pendingRouterMessages.shift();
      const ok = await sendRouterMessage(next.message, next.receiver, next.id);
      if (!ok) {
        // Put it back at the front and try again later.
        pendingRouterMessages.unshift(next);
        atLog("router_flush_paused", { pending: pendingRouterMessages.length, failed_id: next.id }, "warn");
        break;
      }
      // Avoid hammering the router if many messages were queued up.
      await new Promise(resolve => setTimeout(resolve, 40));
    }
  } finally {
    routerFlushInProgress = false;
    // If traffic went busy mid-flush, we'll be called again on the next free transition.
    // If the router was temporarily unavailable, keep retrying while we're still free.
    if (trafficState === "free" && pendingRouterMessages.length > 0) {
      setTimeout(flushRouterQueue, 500);
    }
  }
}





function runPageAutomation(){
  // NativeCamp login flow: after successful login, the site often lands on /teacher/home.
  // Force navigation back to the tutorial URL (with retries) if a redirect target is armed.
  try {
    if (typeof maybePostLoginRedirect === "function" && maybePostLoginRedirect()) {
      atLog("post_login_redirect_handled", { url: window.location.href });
      return;
    }
  } catch (_) {
    // ignore
  }

  const roleNow = resolvePageRoleNow();
  atLog("runPageAutomation", { role: roleNow, url: window.location.href });
  if (roleNow === "login") {
    if (typeof performLogin === "function") {
      performLogin();
    } else {
      console.warn("performLogin() not available.");
    }
  } else if (roleNow === "home") {
    if (typeof setStandbyMode === "function") {
      setStandbyMode();
    } else {
      console.warn("setStandbyMode() not available.");
    }
  } else if (roleNow === "class") {
    if (isTempWalkieClassPage()) {
      // TEMP_WALKIE_MODE: skip NativeCamp textbook automation on local receiver page.
      atUiLog(
        "temp_walkie_class_mode",
        "Class automation bypassed (TEMP_WALKIE_MODE receiver page)",
        { url: window.location.href, roleNow }
      );
      setTrafficState("free");
    } else if (!CLASS_AUTOSTART_ENABLED) {
      console.log("Class automation disabled (TEMP). Use popup 'Start Flow'.");
      atLog("class_autostart_disabled", {});
    } else if (typeof startClassAutomation === "function") {
      startClassAutomation();
    } else {
      console.warn("startClassAutomation() not available.");
    }
  } else if (roleNow === "ai") {
    if (typeof startChatGPTFlow === "function") {
      startChatGPTFlow();
    } else {
      console.warn("startChatGPTFlow() not available.");
    }
  } else if (roleNow === "teacher"){
    if (typeof runTeacherAutomation === "function") {
      runTeacherAutomation();
    } else {
      console.warn("runTeacherAutomation() not available.");
    }
  } else if (roleNow === "stt"){
    // Keep STT router queue drain unlocked at all times for live transcript forwarding.
    setTrafficState("free");
    if (typeof startSTTFlow === "function") {
      startSTTFlow();
    } else {
      console.warn("startSTTFlow() not available.");
    }
  };
}
// runPageAutomation() is invoked after selenium signals init via the router.



safeRuntimeAddListener((msg, sender, sendResponse) => {
  if (msg?.action === "terminate") {
    console.log("🚨 Terminate command received from popup!");
    sendResponse({ status: "System terminated!" });
  }
}, "terminate listener");
