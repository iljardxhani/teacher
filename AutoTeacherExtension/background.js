// Structured logger (shared with content scripts).
try {
  importScripts("logger.js");
} catch (err) {
  console.warn("logger.js import failed:", err);
}

function bgLog(event, data = {}, level = "info") {
  try {
    if (globalThis.AT?.log) return globalThis.AT.log(event, data, level);
  } catch (_) {
    // ignore
  }
  try {
    const fn = console?.[level] || console.log;
    fn.call(console, "[AT-bg]", { event, level, data });
  } catch (_) {
    // ignore
  }
}

// ================== CONFIG ==================
const receivers = ["ai", "teacher", "class", "stt"]; // align with page() roles
// role -> tabId registry
const tabRegistry = {};
// Keepalive ports from content scripts. These keep the MV3 service worker awake.
const keepAlivePortsByTabId = new Map(); // tabId -> port

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

async function fetchWithTimeout(url, options = {}, timeoutMs = 4000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), Math.max(1, Number(timeoutMs) || 1));
  try {
    return await fetch(url, { ...options, signal: controller.signal });
  } finally {
    clearTimeout(timer);
  }
}

function isIgnorableSendError(errMsg = "") {
  // Common when the receiver doesn't call sendResponse.
  return errMsg.includes("The message port closed before a response was received");
}

function isRecoverableSendError(errMsg = "") {
  // Indicates the content script isn't injected yet (tab is loading/reloading).
  return errMsg.includes("Could not establish connection. Receiving end does not exist");
}

function isTabGoneError(errMsg = "") {
  return errMsg.includes("No tab with id");
}

async function requeueToRouter(raw, receiver, reason = "unknown") {
  try {
    // raw is expected to be: { from, message: { sender, receiver, text, ... } }
    const from = raw?.from || raw?.message?.sender || "unknown";
    const message = raw?.message ?? raw;
    const body = {
      from,
      to: receiver,
      message
    };

    console.warn(`Requeueing router message to '${receiver}' (${reason})`, body);
    bgLog(
      "router_requeue",
      {
        receiver,
        reason,
        flow_run_id: body?.message?.meta?.flow_run_id || body?.message?.meta?.flowRunId || null,
        body
      },
      "warn"
    );
    const res = await fetchWithTimeout("http://127.0.0.1:5000/send_message", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }, 4000);
    if (!res.ok) {
      console.warn("Failed to requeue message:", receiver, res.status);
    }
  } catch (err) {
    console.warn("Failed to requeue message:", err);
  }
}

function sendToTab(tabId, forwarded) {
  return new Promise(resolve => {
    try {
      chrome.tabs.sendMessage(tabId, forwarded, () => {
        const err = chrome.runtime.lastError;
        if (!err) return resolve({ ok: true });

        const errMsg = err.message || String(err);
        if (isIgnorableSendError(errMsg)) return resolve({ ok: true, ignorable: true });

        resolve({ ok: false, errMsg });
      });
    } catch (err) {
      resolve({ ok: false, errMsg: err?.message || String(err) });
    }
  });
}

if (typeof chrome !== "undefined" && chrome.runtime?.onConnect) {
  chrome.runtime.onConnect.addListener(port => {
    if (!port || port.name !== "at_keepalive") return;

    const tabId = port.sender?.tab?.id || null;
    if (tabId != null) keepAlivePortsByTabId.set(tabId, port);

    port.onDisconnect.addListener(() => {
      if (tabId != null) keepAlivePortsByTabId.delete(tabId);
    });

    port.onMessage.addListener(msg => {
      if (!msg || msg.type !== "keepalive_ping") return;
      const role = msg.role;
      if (tabId != null && role && receivers.includes(role)) {
        // Keep tabRegistry fresh even if register_tab messages were dropped during reloads.
        tabRegistry[role] = tabId;
      }
      // No logging here; keepalive pings are frequent.
    });
  });
}

if (typeof chrome !== "undefined" && chrome.runtime?.onMessage) {
  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (msg?.type === "register_tab") {
      (async () => {
        const tabId = sender.tab?.id;
        if (!tabId) {
          console.warn("register_tab without sender.tab", msg);
          bgLog("register_tab_failed", { reason: "missing_sender_tab", msg }, "warn");
          // Always answer so content scripts don't log "message port closed" errors.
          sendResponse?.({ ok: false, error: "missing_sender_tab" });
          return;
        }

        tabRegistry[msg.role] = tabId;
        console.log(`registered '${msg.role}' -> tab ${tabId}`, tabRegistry);
        bgLog("register_tab", { role: msg.role, tabId, tabRegistry: { ...tabRegistry } });

        // Flush any queued backend messages now that this tab is available.
        try {
          await pollRouterReceiver(msg.role, { reason: "register_tab" });
        } catch (err) {
          bgLog("poll_after_register_failed", { role: msg.role, err: String(err?.message || err) }, "warn");
        }

        // Content scripts sometimes send this as fire-and-forget, but they still attach a callback.
        // Reply to prevent "The message port closed before a response was received."
        sendResponse?.({ ok: true });
      })();
      return true;
    }

    if (msg?.type === "router_send") {
      // Proxy POST to the local router from extension scripts that can't fetch reliably.
      (async () => {
        const body = {
          from: msg?.from,
          to: msg?.to,
          message: msg?.message
        };
        try {
          const res = await fetchWithTimeout("http://127.0.0.1:5000/send_message", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body)
          }, 4000);
          const text = await res.text().catch(() => "");
          if (!res.ok) {
            bgLog("router_send_proxy_failed", { status: res.status, body, reply: text }, "warn");
            sendResponse?.({ ok: false, status: res.status, reply: text });
            return;
          }
          bgLog("router_send_proxy_ok", { to: body.to, message_id: body?.message?.id, kind: body?.message?.kind });
          // Immediately drain the target queue so prompts show up without relying on the SW staying alive.
          try {
            await pollRouterReceiver(body.to, { reason: "router_send" });
          } catch (err) {
            bgLog("poll_after_send_failed", { to: body.to, err: String(err?.message || err) }, "warn");
          }
          sendResponse?.({ ok: true });
        } catch (err) {
          bgLog("router_send_proxy_error", { body, err: String(err?.message || err) }, "warn");
          sendResponse?.({ ok: false, error: String(err?.message || err) });
        }
      })();
      return true; // keep sendResponse alive
    }

    if (msg?.type === "at_log_entry") {
      (async () => {
        const entry = msg?.entry;
        if (!entry || typeof entry !== "object") {
          sendResponse?.({ ok: false, error: "missing_entry" });
          return;
        }
        try {
          const res = await fetchWithTimeout("http://127.0.0.1:5000/log_event", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              source: {
                via: "background",
                sender_tab_id: sender?.tab?.id || null
              },
              entry
            })
          }, 4000);
          if (!res.ok) {
            bgLog("remote_log_proxy_failed", { status: res.status }, "warn");
            sendResponse?.({ ok: false, status: res.status });
            return;
          }
          sendResponse?.({ ok: true });
        } catch (err) {
          bgLog("remote_log_proxy_error", { err: String(err?.message || err) }, "warn");
          sendResponse?.({ ok: false, error: String(err?.message || err) });
        }
      })();
      return true;
    }

    if (msg?.type === "tabs.query") {
      chrome.tabs.query(msg.queryInfo || {}, tabs => {
        sendResponse({ tabs });
      });
      return true; // keep sendResponse alive
    }

    if (msg?.type === "tabs.update") {
      const tabId = sender?.tab?.id;
      const url = msg?.url;
      if (!tabId || !url) {
        sendResponse?.({ ok: false, error: "missing_tab_or_url" });
        return;
      }
      try {
        chrome.tabs.update(tabId, { url: String(url) }, () => {
          const err = chrome.runtime.lastError;
          if (err) {
            sendResponse?.({ ok: false, error: err.message || String(err) });
            return;
          }
          sendResponse?.({ ok: true });
        });
      } catch (err) {
        sendResponse?.({ ok: false, error: String(err?.message || err) });
      }
      return true;
    }
  });
} else {
  console.warn("chrome.runtime unavailable — background idle.");
}


// ===================== Pool messages from Route ======================= //
// Simple polling loop: for each receiver, ask the local router for messages.
// Any messages returned are logged so you can see what the backend has queued.
const POLL_INTERVAL_MS = 1000;

async function pollRouterReceiver(receiver, { reason = null } = {}) {
  if (!receiver || !receivers.includes(receiver)) return;
  const tabId = tabRegistry[receiver];
  // Do not drain the backend queue unless we have a known target tab.
  // Otherwise messages get cleared and lost while the tab is loading/reloading.
  if (!tabId) return;

  try {
    const res = await fetchWithTimeout(`http://127.0.0.1:5000/get_messages/${receiver}`, {}, 4000);
    if (!res.ok) {
      console.warn(`router reply not OK for ${receiver}:`, res.status);
      return;
    }

    const data = await res.json();
    const messages = data?.messages || [];
    if (messages.length === 0) return; // skip noisy logs
    bgLog("router_messages", { receiver, count: messages.length, reason });

    // Each item is { from, message: { sender, receiver, text } }.
    for (const raw of messages) {
      const payload = raw.message || raw; // normalize to inner payload
      console.log("📬 router payload:", payload);

      // Forward the full raw object (and annotate with receiver) so downstream keeps context.
      const forwarded = { ...raw, to: receiver };

      const result = await sendToTab(tabId, forwarded);
      if (!result.ok) {
        console.warn(`Failed to send to ${receiver} (tab ${tabId}):`, result.errMsg);
        bgLog(
          "forward_failed",
          {
            receiver,
            tabId,
            errMsg: result.errMsg,
            payload_id: payload?.id,
            kind: payload?.kind,
            flow_run_id: payload?.meta?.flow_run_id || payload?.meta?.flowRunId || null,
            payload
          },
          "warn"
        );

        if (isTabGoneError(result.errMsg || "")) {
          delete tabRegistry[receiver];
        }
        if (isRecoverableSendError(result.errMsg || "")) {
          // Tab is likely navigating; wait for the next register_tab.
          delete tabRegistry[receiver];
        }

        if (isRecoverableSendError(result.errMsg || "") || isTabGoneError(result.errMsg || "")) {
          await requeueToRouter(raw, receiver, result.errMsg);
          // Small delay to avoid hot-looping on a reloading tab.
          await sleep(200);
        }
      } else {
        bgLog("forward_ok", {
          receiver,
          tabId,
          payload_id: payload?.id,
          kind: payload?.kind,
          flow_run_id: payload?.meta?.flow_run_id || payload?.meta?.flowRunId || null
        });
      }
    }
  } catch (err) {
    console.error(`Failed to poll router for ${receiver}:`, err);
    bgLog("router_poll_failed", { receiver, err: String(err?.message || err) }, "error");
  }
}

async function pollRouterOnce() {
  for (const receiver of receivers) {
    await pollRouterReceiver(receiver, { reason: "pollRouterOnce" });
  }
}

// NOTE: MV3 service workers are not guaranteed to stay alive for timers/loops.
// We primarily poll on-demand (after router_send and register_tab). Keep this as a best-effort fallback.
(async function startPolling() {
  while (true) {
    await pollRouterOnce();
    await new Promise(resolve => setTimeout(resolve, POLL_INTERVAL_MS));
  }
})();
