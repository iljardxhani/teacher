function resolveCurrentPage() {
  if (typeof current_page !== "undefined") return current_page;
  if (typeof page === "function") {
    try {
      return page();
    } catch (err) {
      console.warn("stt.js: failed to call page():", err);
    }
  }
  return "unknown";
}

console.log("stt.js loaded; current_page =", resolveCurrentPage());

let sttListenerRegistered = false;
let sttPollTimer = null;

function logSttPayload(label, msg) {
  const payload = msg?.message ?? msg;
  const text =
    typeof payload === "string"
      ? payload
      : payload?.text ?? JSON.stringify(payload);

  console.log("🗣️ STT console log:", {
    label,
    from: msg?.from ?? payload?.origin ?? "unknown",
    text,
    raw: payload
  });
}

function initSttListener() {
  if (sttListenerRegistered) return;
  if (typeof chrome === "undefined" || !chrome.runtime?.onMessage) {
    console.warn("stt.js: chrome runtime not available yet, retrying...");
    return setTimeout(initSttListener, 500);
  }
  const pageName = resolveCurrentPage();
  if (pageName !== "stt") {
    console.log("stt.js: not on stt page (current_page =", pageName, ") — retrying...");
    return setTimeout(initSttListener, 1000);
  }

  chrome.runtime.onMessage.addListener(msg => {
    console.log("🎧 stt.js received message:", msg);
    if (msg.to === "stt" && msg.from) {
      logSttPayload("direct-message", msg);
    }
  });

  sttListenerRegistered = true;
  console.log("stt.js: listener registered.");
}

initSttListener();

async function pollRouteForMessages() {
  const pageName = resolveCurrentPage();
  if (pageName !== "stt") {
    sttPollTimer = setTimeout(pollRouteForMessages, 2000);
    return;
  }

  try {
    const res = await fetch("http://127.0.0.1:5000/get_messages/stt");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const messages = data.messages || [];

    if (messages.length > 0) {
      messages.forEach(msg => {
        logSttPayload("route-poll", msg);
      });
    }
  } catch (err) {
    console.error("stt.js: route poll failed:", err);
  }

  sttPollTimer = setTimeout(pollRouteForMessages, 1000);
}

pollRouteForMessages();
