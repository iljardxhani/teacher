const recipients = ["ai", "class"]; // teacher & stt self-poll via their tabs
const POLL_INTERVAL = 1000;

// role -> tabId
const tabRegistry = {};
const pollingLoops = {};
const pendingDirectMessages = {};
const pendingFlushTimers = {};
const FLUSH_INTERVAL = 1000;

if (typeof chrome === "undefined" || !chrome.runtime?.onMessage) {
  console.warn("background.js: chrome.runtime unavailable — background idle.");
} else {
  chrome.runtime.onMessage.addListener((msg, sender) => {
    if (msg.type === "register_tab") {
      const tabId = sender.tab?.id;
      if (!tabId) {
        console.warn("❌ register_tab without sender.tab", msg);
        return;
      }

      tabRegistry[msg.role] = tabId;
      console.log(`✅ Registered role '${msg.role}' -> tab ${tabId}`);
      console.log("📦 tabRegistry:", tabRegistry);
      flushPendingDirectMessages(msg.role);
      return;
    }

    if (msg.type === "relay_message") {
      console.log("📩 relay_message received:", {
        from: msg.from,
        to: msg.to,
        message: msg.message
      });
      deliverDirectMessage({
        from: msg.from,
        to: msg.to,
        message: msg.message
      });
    }
  });
}

function deliverDirectMessage(payload) {
  const tabId = tabRegistry[payload.to];
  if (tabId) {
    chrome.tabs.sendMessage(tabId, payload, () => {
      if (chrome.runtime.lastError) {
        console.warn(
          `⚠️ Failed to deliver direct message to '${payload.to}' (tab ${tabId}):`,
          chrome.runtime.lastError.message
        );
        requeueMessage(payload);
        return;
      }
      console.log(`📨 Direct routed message to '${payload.to}' in tab ${tabId}:`, payload);
    });
  } else {
    requeueMessage(payload);
  }
}

function flushPendingDirectMessages(role) {
  const queue = pendingDirectMessages[role];
  if (!queue || queue.length === 0) return;
  const tabId = tabRegistry[role];
  if (!tabId) return;

  queue.forEach(payload => {
    chrome.tabs.sendMessage(tabId, payload, () => {
      if (chrome.runtime.lastError) {
        console.warn(
          `⚠️ Failed to flush message to '${role}' (tab ${tabId}):`,
          chrome.runtime.lastError.message
        );
        requeueMessage(payload);
        return;
      }
      console.log(`📨 Flushed queued message to '${role}' in tab ${tabId}:`, payload);
    });
  });
  pendingDirectMessages[role] = [];
}

function requeueMessage(payload) {
  pendingDirectMessages[payload.to] = pendingDirectMessages[payload.to] || [];
  pendingDirectMessages[payload.to].push(payload);
  console.warn(`⏳ Queued message for '${payload.to}' — waiting for active content script`, payload);
  schedulePendingFlush(payload.to);
}

function schedulePendingFlush(role) {
  if (pendingFlushTimers[role]) return;
  pendingFlushTimers[role] = setTimeout(() => {
    pendingFlushTimers[role] = null;
    flushPendingDirectMessages(role);
    if (pendingDirectMessages[role] && pendingDirectMessages[role].length > 0) {
      schedulePendingFlush(role);
    }
  }, FLUSH_INTERVAL);
}

async function fetchMessages(recipient) {
  try {
    const res = await fetch(`http://127.0.0.1:5000/get_messages/${recipient}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    return data.messages || [];
  } catch (err) {
    console.error(`❌ Failed to fetch messages for ${recipient}:`, err);
    return [];
  }
}

async function pollRecipient(recipient) {
  if (pollingLoops[recipient]) return;
  pollingLoops[recipient] = true;

  async function loop() {
    const tabId = tabRegistry[recipient];

    if (!tabId) {
      // No tab registered yet; keep queue intact and retry later.
      setTimeout(loop, POLL_INTERVAL);
      return;
    }

    const messages = await fetchMessages(recipient);

    if (messages.length > 0) {
      messages.forEach(msg => {
        const payload = {
          from: msg.from,
          to: recipient,
          message: msg.message
        };

        chrome.tabs.sendMessage(tabId, payload, () => {
          if (chrome.runtime.lastError) {
            console.warn(
              `⚠️ Failed to deliver polled message to '${recipient}' (tab ${tabId}):`,
              chrome.runtime.lastError.message
            );
            requeueMessage(payload);
            return;
          }
          console.log(
            `🚀 Forwarded message to '${recipient}' in tab ${tabId}:`,
            msg
          );
        });
      });
    }

    setTimeout(loop, POLL_INTERVAL);
  }

  loop();
}

if (typeof chrome !== "undefined" && chrome.runtime?.onMessage) {
  recipients.forEach(r => pollRecipient(r));
  console.log("📡 Background message polling started for:", recipients);
}
