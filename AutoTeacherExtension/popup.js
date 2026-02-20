const statusEl = document.getElementById("status");
const versionEl = document.getElementById("extVersion");
const roleEl = document.getElementById("pageRole");
const classActionsEl = document.getElementById("classActions");
const btnSendMessageEl = document.getElementById("btnSendMessage");

let activeTabId = null;

function setStatus(text, color = "#666") {
  if (!statusEl) return;
  statusEl.textContent = text || "";
  statusEl.style.color = color;
}

function setRole(role) {
  if (!roleEl) return;
  roleEl.textContent = role || "unknown";
  if (classActionsEl) {
    if (role === "class") classActionsEl.classList.remove("hidden");
    else classActionsEl.classList.add("hidden");
  }
}

function detectRoleFromUrl(url = "") {
  const lower = url.toLowerCase();
  if (lower.includes("akool.com/apps/streaming-avatar/edit")) return "teacher";
  if (lower.includes("chatgpt.com") || lower.includes("chat.openai.com")) return "ai";
  if (lower.includes("speechtexter.com")) return "stt";
  if (lower.includes("/walkie/receiver")) return "class"; // TEMP_WALKIE_MODE
  if (lower.includes("/teacher/lesson-tutorial")) return "class";
  if (lower.includes("/teacher/home")) return "home";
  if (lower.includes("/teacher/login")) return "login";
  return "unknown";
}

function sendToActiveTab(message) {
  return new Promise(resolve => {
    if (!activeTabId) return resolve({ ok: false, error: "no_active_tab" });
    chrome.tabs.sendMessage(activeTabId, message, response => {
      const err = chrome.runtime.lastError;
      if (err) return resolve({ ok: false, error: err.message || String(err) });
      resolve(response || { ok: true });
    });
  });
}

document.addEventListener("DOMContentLoaded", () => {
  try {
    if (versionEl && chrome?.runtime?.getManifest) {
      versionEl.textContent = chrome.runtime.getManifest().version || "?";
    }
  } catch (_) {
    // ignore
  }

  chrome.tabs.query({ active: true, currentWindow: true }, tabs => {
    const tab = tabs?.[0];
    if (!tab?.id) {
      setStatus("No active tab found.", "#b00020");
      setRole("unknown");
      return;
    }
    activeTabId = tab.id;

    chrome.tabs.sendMessage(tab.id, { action: "get_page_role" }, response => {
      const err = chrome.runtime.lastError;
      if (!err && response?.role) {
        setRole(response.role);
        setStatus("");
        return;
      }

      // Fallback to URL detection if content script is not available.
      const fallbackRole = detectRoleFromUrl(tab.url || "");
      setRole(fallbackRole);
      if (err) {
        setStatus("Role via URL fallback.", "#666");
      } else {
        setStatus("");
      }
    });
  });

  if (btnSendMessageEl) {
    btnSendMessageEl.addEventListener("click", async () => {
      setStatus("Sending lesson package...");
      const res = await sendToActiveTab({
        action: "class_fire_detect_textbook_type",
        mode: "send"
      });
      if (!res?.ok) {
        setStatus(`Failed: ${res?.error || "unknown"}`, "#b00020");
        return;
      }
      setStatus("Send triggered.", "#0b6f3c");
    });
  }
});
