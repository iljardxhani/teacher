let isChatGPTStreaming = false;
let aiSendChain = Promise.resolve();

const aiSeenMessageIds = new Set();
const AI_MAX_TRACKED = 200;

function aiLog(event, data = {}, level = "info") {
  try {
    globalThis.AT?.log?.(event, data, level);
  } catch (_) {
    // ignore
  }
}

function aiUi(event, message, data = {}, opts = {}) {
  try {
    globalThis.AT?.uiLog?.(event, message, data, opts);
  } catch (_) {
    // ignore
  }
}

function aiSleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function trackAiMessageId(id) {
  if (!id) return false;
  if (aiSeenMessageIds.has(id)) return true;
  aiSeenMessageIds.add(id);
  if (aiSeenMessageIds.size > AI_MAX_TRACKED) {
    const iterator = aiSeenMessageIds.values();
    aiSeenMessageIds.delete(iterator.next().value);
  }
  return false;
}

function isNoReturnExpected(meta = {}) {
  if (meta?.no_return_expected === true) return true;
  if (meta?.flags?.no_return_expected === true) return true;
  return false;
}

function getDelayAfterMs(meta = {}) {
  const v = meta?.delayAfterMs ?? meta?.delay_after_ms;
  const n = Number(v);
  return Number.isFinite(n) && n > 0 ? n : 0;
}

function isVisible(el) {
  if (!el || typeof el !== "object") return false;
  try {
    const style = window.getComputedStyle(el);
    if (!style) return false;
    if (style.display === "none" || style.visibility === "hidden") return false;
    const rect = el.getBoundingClientRect?.();
    if (!rect) return true;
    return rect.width > 0 && rect.height > 0;
  } catch (_) {
    return false;
  }
}

function findComposerInput() {
  // ChatGPT DOM changes often; try a few common targets.
  const primary = [
    "textarea#prompt-textarea",
    "textarea[data-testid='prompt-textarea']",
    "textarea[placeholder*='Message']",
    "textarea[placeholder*='Send a message']",
    "footer textarea",
    "div#prompt-textarea[contenteditable='true']",
    "div[contenteditable='true']#prompt-textarea",
    "footer div[contenteditable='true']",
    "div[contenteditable='true'] p",
    "div[contenteditable='true']"
  ];

  for (const sel of primary) {
    const el = document.querySelector(sel);
    if (el && isVisible(el)) return el;
  }

  // Fallback: any visible textarea (should be the composer in most cases).
  try {
    const candidates = Array.from(document.querySelectorAll("textarea"));
    const visible = candidates.find(isVisible);
    if (visible) return visible;
  } catch (_) {
    // ignore
  }

  return null;
}

function setComposerText(inputEl, text) {
  if (!inputEl) return false;
  try {
    inputEl.focus?.();
  } catch (_) {
    // ignore
  }

  const tag = String(inputEl.tagName || "").toLowerCase();
  const value = String(text ?? "");

  try {
    if (tag === "textarea" || tag === "input") {
      inputEl.value = value;
      inputEl.dispatchEvent(new Event("input", { bubbles: true }));
      inputEl.dispatchEvent(new Event("change", { bubbles: true }));
      return true;
    }
  } catch (_) {
    // ignore
  }

  // contenteditable or other: set its textContent.
  inputEl.textContent = value;

  try {
    if (typeof InputEvent === "function") {
      inputEl.dispatchEvent(new InputEvent("input", { bubbles: true, data: value, inputType: "insertText" }));
    } else {
      inputEl.dispatchEvent(new Event("input", { bubbles: true }));
    }
  } catch (_) {
    // ignore
  }
  return true;
}

function findStopButton() {
  // During generation, ChatGPT often replaces the send button with a stop button.
  return (
    document.querySelector("button[data-testid='stop-button']") ||
    document.querySelector("button[aria-label='Stop generating']") ||
    document.querySelector("button[aria-label='Stop']") ||
    null
  );
}

function findComposerForm(inputEl) {
  try {
    return inputEl?.closest?.("form") || inputEl?.closest?.("[role='form']") || null;
  } catch (_) {
    return null;
  }
}

function findSendButton(inputEl = null) {
  const selectors = [
    "#composer-submit-button",
    "button[data-testid='send-button']",
    "button[aria-label='Send prompt']",
    "button[aria-label='Send message']",
    "button[aria-label='Send']",
    "button[type='submit']"
  ];

  const form = inputEl ? findComposerForm(inputEl) : null;
  if (form) {
    for (const sel of selectors) {
      const el = form.querySelector(sel);
      if (el && isVisible(el)) return el;
    }
  }

  for (const sel of selectors) {
    const el = document.querySelector(sel);
    if (el && isVisible(el)) return el;
  }

  return null;
}

function sendViaEnter(inputEl) {
  if (!inputEl) return false;
  const init = {
    bubbles: true,
    cancelable: true,
    key: "Enter",
    code: "Enter",
    keyCode: 13,
    which: 13,
    shiftKey: false,
    altKey: false,
    ctrlKey: false,
    metaKey: false
  };
  try {
    inputEl.dispatchEvent(new KeyboardEvent("keydown", init));
    inputEl.dispatchEvent(new KeyboardEvent("keypress", init));
    inputEl.dispatchEvent(new KeyboardEvent("keyup", init));
    return true;
  } catch (_) {
    return false;
  }
}

function getComposerText(inputEl) {
  if (!inputEl) return "";
  const tag = String(inputEl.tagName || "").toLowerCase();
  if (tag === "textarea" || tag === "input") return String(inputEl.value || "");
  return String(inputEl.textContent || "");
}

function dumpFullTextToConsole(label, text, meta = {}) {
  const value = String(text ?? "");
  console.log(`[ai] ${label}`, {
    id: meta?.id || null,
    kind: meta?.kind || null,
    book_type: meta?.book_type || null,
    package_id: meta?.package_id || null,
    text_len: value.length
  });
  console.log(`[ai] ${label} START >>>`);
  console.log(value);
  console.log(`[ai] ${label} END <<<`);
}

async function waitUntilNotGenerating({ timeoutMs = 120000 } = {}) {
  const startedAt = Date.now();
  while (Date.now() - startedAt < timeoutMs) {
    const stop = findStopButton();
    if (!stop) return true;
    await aiSleep(400);
  }
  return false;
}

async function submitComposer(inputEl) {
  const sendBtn = findSendButton(inputEl);
  if (sendBtn && sendBtn.disabled !== true) {
    try {
      sendBtn.click();
      return { ok: true, via: "button" };
    } catch (_) {
      // ignore
    }
    try {
      sendBtn.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
      return { ok: true, via: "button_event" };
    } catch (_) {
      // ignore
    }
  }

  const form = findComposerForm(inputEl);
  if (form) {
    try {
      if (typeof form.requestSubmit === "function") {
        form.requestSubmit(sendBtn || undefined);
        return { ok: true, via: "requestSubmit" };
      }
    } catch (_) {
      // ignore
    }
    try {
      const ev = new Event("submit", { bubbles: true, cancelable: true });
      form.dispatchEvent(ev);
      return { ok: true, via: "submit_event" };
    } catch (_) {
      // ignore
    }
  }

  if (sendViaEnter(inputEl)) return { ok: true, via: "enter" };
  return { ok: false, via: "none" };
}

async function waitForUserSend({ userCountBefore, inputEl, timeoutMs = 8000 } = {}) {
  const startedAt = Date.now();
  while (Date.now() - startedAt < timeoutMs) {
    try {
      const userCountNow = document.querySelectorAll('article[data-turn="user"]').length;
      if (Number.isFinite(userCountBefore) && userCountNow > userCountBefore) return true;
    } catch (_) {
      // ignore
    }
    try {
      if (inputEl && getComposerText(inputEl).trim() === "") return true;
    } catch (_) {
      // ignore
    }
    await aiSleep(200);
  }
  return false;
}

async function waitForElement(getter, { timeoutMs = 20000, intervalMs = 250, label = "element" } = {}) {
  const startedAt = Date.now();
  while (Date.now() - startedAt < timeoutMs) {
    let el = null;
    try {
      el = getter();
    } catch (_) {
      el = null;
    }
    if (el) return el;
    await aiSleep(intervalMs);
  }
  console.warn(`[ai] waitForElement timed out: ${label}`);
  return null;
}

function extractAssistantText(articleEl) {
  if (!articleEl) return "";
  const parts = Array.from(articleEl.querySelectorAll("p, li"))
    .map(el => (el?.innerText || "").trim())
    .filter(t => t.length > 0);
  const text = parts.join("\n");
  return text || (articleEl.innerText || "").trim();
}

async function waitForAssistantReply(assistantIndex, { timeoutMs = 180000 } = {}) {
  const startedAt = Date.now();
  while (Date.now() - startedAt < timeoutMs) {
    const messages = document.querySelectorAll('article[data-turn="assistant"]');
    const message = messages[assistantIndex];
    if (!message) {
      await aiSleep(500);
      continue;
    }
    const copyButton = message.querySelector('[aria-label="Copy"]');
    if (!copyButton) {
      await aiSleep(500);
      continue;
    }
    const text = extractAssistantText(message);
    if (!text) {
      await aiSleep(250);
      continue;
    }
    return text;
  }
  console.warn("[ai] waitForAssistantReply timed out");
  return null;
}

async function sendChatGPTMessage(text, meta = {}) {
  const prompt = String(text || "").trim();
  if (!prompt) return null;

  aiUi(
    "ai_prompt_send",
    `AI: sending ${meta?.kind || "prompt"}`,
    {
      id: meta?.id,
      kind: meta?.kind,
      book_type: meta?.book_type,
      package_id: meta?.package_id,
      no_return_expected: isNoReturnExpected(meta),
      text_len: prompt.length
    }
  );
  aiLog("ai_prompt_send", {
    id: meta?.id,
    kind: meta?.kind,
    book_type: meta?.book_type,
    package_id: meta?.package_id,
    flags: meta?.flags,
    no_return_expected: isNoReturnExpected(meta),
    text_len: prompt.length
  });
  dumpFullTextToConsole("outbound_prompt_before_send", prompt, meta);

  const input = await waitForElement(findComposerInput, { label: "composer input" });
  if (!input) {
    aiUi("ai_missing_composer", "AI: composer input not found", {}, { level: "warn", ttlMs: 6500 });
    aiLog("ai_missing_composer", {}, "warn");
    return null;
  }

  // Avoid sending while ChatGPT is currently generating (send button may be replaced by stop button).
  const ready = await waitUntilNotGenerating({ timeoutMs: 120000 });
  if (!ready) {
    aiUi("ai_busy_generating", "AI: ChatGPT still generating; send skipped", {}, { level: "warn", ttlMs: 6500 });
    aiLog("ai_busy_generating", {}, "warn");
    return null;
  }

  // Make sure the per-page traffic flag is locked while we stream.
  if (typeof setTrafficState === "function") setTrafficState("busy");

  // Pair this send with the next assistant response.
  const assistantIndex = document.querySelectorAll('article[data-turn="assistant"]').length;
  const userCountBefore = document.querySelectorAll('article[data-turn="user"]').length;

  setComposerText(input, prompt);
  const composerValueAfterSet = getComposerText(input);
  console.log("[ai] composer_text_after_set", {
    kind: meta?.kind || null,
    expected_len: prompt.length,
    actual_len: composerValueAfterSet.length,
    matches_prompt: composerValueAfterSet === prompt
  });
  if (composerValueAfterSet !== prompt) {
    dumpFullTextToConsole("composer_text_after_set_mismatch", composerValueAfterSet, meta);
  }

  isChatGPTStreaming = true;
  // Give the UI a tick to enable the send button after input events.
  await aiSleep(50);
  const sent = await submitComposer(input);
  aiLog("ai_submit", { via: sent?.via, ok: sent?.ok, kind: meta?.kind });

  const sendObserved = await waitForUserSend({ userCountBefore, inputEl: input, timeoutMs: 10000 });
  if (!sendObserved) {
    isChatGPTStreaming = false;
    if (typeof setTrafficState === "function") setTrafficState("free");
    aiUi("ai_send_not_observed", "AI: send not observed (composer unchanged)", { via: sent?.via }, { level: "warn", ttlMs: 6500 });
    aiLog("ai_send_not_observed", { via: sent?.via }, "warn");
    return null;
  }

  const replyText = await waitForAssistantReply(assistantIndex);
  isChatGPTStreaming = false;

  if (typeof setTrafficState === "function") setTrafficState("free");

  if (!replyText) {
    aiUi("ai_reply_timeout", "AI: reply timed out", { kind: meta?.kind }, { level: "warn", ttlMs: 6500 });
    aiLog("ai_reply_timeout", { kind: meta?.kind }, "warn");
    return null;
  }

  if (isNoReturnExpected(meta)) {
    console.log("[ai] reply discarded (no_return_expected)", {
      kind: meta?.kind,
      book_type: meta?.book_type,
      package_id: meta?.package_id
    });
    aiUi(
      "ai_reply_discarded",
      `AI: discarded reply (${meta?.kind || "prompt"})`,
      { id: meta?.id, kind: meta?.kind, book_type: meta?.book_type, package_id: meta?.package_id }
    );
    aiLog("ai_reply_discarded", {
      id: meta?.id,
      kind: meta?.kind,
      book_type: meta?.book_type,
      package_id: meta?.package_id,
      reply_len: replyText.length
    });
    return replyText;
  }

  console.log("AI said:", replyText);
  console.log("🛰️ Dispatching text to route/background -> teacher");
  aiUi(
    "ai_reply_forward",
    "AI: forwarding reply to teacher",
    { id: meta?.id, kind: meta?.kind, reply_len: replyText.length }
  );
  aiLog("ai_reply_forward", { id: meta?.id, kind: meta?.kind, reply_len: replyText.length });
  if (typeof queueRouterMessage === "function") {
    const teacherPayload = {
      kind: "ai_reply",
      text: replyText,
      meta: {
        flow_run_id: meta?.flow_run_id || null,
        book_type: meta?.book_type || null,
        package_id: meta?.package_id || null,
        source_kind: meta?.kind || null
      }
    };
    queueRouterMessage(teacherPayload, "teacher");
  } else if (typeof sendRouterMessage === "function") {
    sendRouterMessage(
      {
        kind: "ai_reply",
        text: replyText,
        meta: {
          flow_run_id: meta?.flow_run_id || null,
          book_type: meta?.book_type || null,
          package_id: meta?.package_id || null,
          source_kind: meta?.kind || null
        }
      },
      "teacher"
    );
  } else {
    console.warn("[ai] sendRouterMessage unavailable; reply not forwarded.");
    aiUi("ai_reply_forward_failed", "AI: forward failed (sendRouterMessage missing)", {}, { level: "warn", ttlMs: 6500 });
    aiLog("ai_reply_forward_failed", { reason: "sendRouterMessage_missing" }, "warn");
  }

  return replyText;
}

function normalizeInboundAiPayload(msg) {
  // background forwards router payloads as: { from, message, to: "ai" }
  const payload = msg?.message ?? msg;
  if (!payload) return null;

  const id = typeof payload === "object" ? payload.id : null;
  if (trackAiMessageId(id)) {
    console.log("[ai] duplicate message ignored:", id);
    return null;
  }

  const text =
    typeof payload === "string"
      ? payload
      : (payload.text ?? payload.message ?? null);

  if (typeof text !== "string" || text.trim() === "") return null;

  const meta = {
    id,
    kind: payload.kind,
    book_type: payload.book_type || payload.bookType,
    package_id: payload.package_id || payload.packageId,
    delayAfterMs: payload.delay_after_ms ?? payload.delayAfterMs,
    flags: payload.flags || {},
    flow_run_id: payload?.meta?.flow_run_id || null
  };

  return { text, meta };
}

function enqueueAiPrompt(text, meta = {}) {
  aiSendChain = aiSendChain
    .catch(err => console.warn("[ai] previous send error:", err))
    .then(async () => {
      // Respect the shared traffic lock if available.
      if (typeof runWhenTrafficFree === "function") {
        await new Promise(resolve => runWhenTrafficFree(resolve, "ai-queue"));
      }

      await sendChatGPTMessage(text, meta);

      const delayMs = getDelayAfterMs(meta);
      if (delayMs) await aiSleep(delayMs);
    });
}

// Receive messages from the router/background and send them to ChatGPT.
if (typeof chrome !== "undefined" && chrome.runtime?.onMessage?.addListener) {
  chrome.runtime.onMessage.addListener(msg => {
    if (msg?.to !== "ai") return;
    const normalized = normalizeInboundAiPayload(msg);
    if (!normalized) return;
    // Safety: if the page never ran startChatGPTFlow(), the traffic flag can remain "busy" forever.
    // If we're not actively streaming, unlock so the queue can run.
    try {
      if (!isChatGPTStreaming && typeof setTrafficState === "function") setTrafficState("free");
    } catch (_) {
      // ignore
    }
    try {
      const runId = normalized?.meta?.flow_run_id;
      if (runId && typeof globalThis.AT?.setRunId === "function") {
        globalThis.AT.setRunId(runId, "log");
      }
    } catch (_) {
      // ignore
    }
    console.log("[ai] inbound router prompt:", {
      kind: normalized.meta?.kind,
      book_type: normalized.meta?.book_type,
      flags: normalized.meta?.flags
    });
    aiUi(
      "ai_inbound",
      `AI: inbound ${normalized.meta?.kind || "prompt"}`,
      { id: normalized.meta?.id, kind: normalized.meta?.kind, book_type: normalized.meta?.book_type, package_id: normalized.meta?.package_id }
    );
    aiLog("ai_inbound", { meta: normalized.meta });
    enqueueAiPrompt(normalized.text, normalized.meta);
  });
}

// --- START ---
function startChatGPTFlow() {
  if (typeof setTrafficState === "function") setTrafficState("free");
  // Do not auto-send any message on load; we only react to router prompts.
}
