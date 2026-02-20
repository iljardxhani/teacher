
//-------------------

function teacherLog(event, data = {}, level = "info") {
  try {
    globalThis.AT?.log?.(event, data, level);
  } catch (_) {
    // ignore
  }
}

function teacherUi(event, message, data = {}, opts = {}) {
  try {
    globalThis.AT?.uiLog?.(event, message, data, opts);
  } catch (_) {
    // ignore
  }
}

function waitForVideoReady(selector = "video.css-m0a7mp", buffer = 1000) {
  console.log("🔥 [DEBUG] waitForVideoReady start. selector:", selector);
  setTrafficState("busy");
  listenForTeacherMessages();

  let done = false;
  let pollId = null;
  let observer = null;

  const finish = reason => {
    if (done) return;
    done = true;
    if (observer) observer.disconnect();
    if (pollId) clearInterval(pollId);
    console.log(`🧪 [DEBUG] video_ready fired (${reason})`);
    document.dispatchEvent(new Event("video_ready"));
  };

  const isReady = video => {
    if (!video) return false;
    const readyState = video.readyState ?? 0;
    const hasData = readyState >= (HTMLMediaElement?.HAVE_FUTURE_DATA ?? 3);
    const hasDims = (video.videoWidth ?? 0) > 0 && (video.videoHeight ?? 0) > 0;
    const hasSrc = Boolean(video.currentSrc || video.src);
    return hasData && hasDims && hasSrc;
  };

  const watchVideo = video => {
    if (!video) return;

    if (isReady(video)) {
      return finish("already ready");
    }

    const onReady = () => finish("ready event");
    ["loadedmetadata", "canplay", "canplaythrough"].forEach(ev => {
      video.addEventListener(ev, onReady, { once: true });
    });

    pollId = setInterval(() => {
      if (isReady(video)) {
        finish("readyState poll");
      }
    }, buffer);
  };

  const initial = document.querySelector(selector);
  if (initial) {
    watchVideo(initial);
  } else {
    observer = new MutationObserver(() => {
      const video = document.querySelector(selector);
      if (video) {
        observer.disconnect();
        watchVideo(video);
      }
    });
    withBodyReady(body => observer.observe(body, { childList: true, subtree: true }));
    console.log("👀 [DEBUG] waiting for video element to appear...");
  }

  // Fallback: if nothing becomes ready in a reasonable time, continue flow.
  setTimeout(() => finish("fallback timeout"), buffer * 10);
}

function normalizeText(str = "") {
  return str.replace(/[’]/g, "'").replace(/\s+/g, " ").trim().toLowerCase();
}

function withBodyReady(cb, attempt = 0) {
  const body = typeof document !== "undefined" ? document.body : null;
  if (body) {
    cb(body);
    return;
  }
  if (attempt >= 50) {
    console.warn("Body not ready; skipping observer setup.");
    return;
  }
  setTimeout(() => withBodyReady(cb, attempt + 1), 100);
}

function findButtonByText(text) {
  const target = normalizeText(text);
  return Array.from(document.querySelectorAll("button, [role='button']")).find(btn => {
    const content = normalizeText(btn.textContent || "");
    const aria = normalizeText(btn.getAttribute("aria-label") || "");
    const title = normalizeText(btn.getAttribute("title") || "");
    return content.includes(target) || aria.includes(target) || title.includes(target);
  });
}

function runWhenTrafficReady(task, label = "teacher-task") {
  if (typeof runWhenTrafficFree === "function") {
    runWhenTrafficFree(task, label);
  } else {
    task();
  }
}

function initializeAvatarChat() {
  // Find the 'Repeat' button dynamically
  const repeatBtn = Array.from(document.querySelectorAll('.MuiTab-root'))
                         .find(btn => btn.innerText.trim() === "Repeat");

  if (!repeatBtn) {
    console.log("⚠️ Repeat button not found, retrying...");
    return setTimeout(initializeAvatarChat, 500); // retry THIS function
  }

  const opts = { bubbles: true, cancelable: true, view: window };

  // Smart click sequence
  repeatBtn.dispatchEvent(new MouseEvent("mouseover", opts));
  repeatBtn.dispatchEvent(new MouseEvent("mousedown", opts));
  repeatBtn.dispatchEvent(new MouseEvent("mouseup", opts));
  repeatBtn.dispatchEvent(new MouseEvent("click", opts));

  console.log("🎯 Smart click fired on 'Repeat' button!");

  // Now look for Let's Chat button
  const chatBtn = Array.from(
    document.querySelectorAll("button.MuiButton-containedPrimary.MuiButton-sizeSmall")
  ).find(btn => btn.textContent.trim().includes("Let's chat"));

  if (!chatBtn) {
    console.log("⚠️ Let's chat button not found, retrying...");
    return setTimeout(initializeAvatarChat, 500); // retry again until it exists
  }

  chatBtn.dispatchEvent(new MouseEvent("mouseover", opts));
  chatBtn.dispatchEvent(new MouseEvent("mousedown", opts));
  chatBtn.dispatchEvent(new MouseEvent("mouseup", opts));
  chatBtn.dispatchEvent(new MouseEvent("click", opts));
  console.log("🎯 Smart click fired on 'Let's chat' button!");

  // Now wait for chat frame
  function waitForChatFrame() {
    // Check if already present
    const existing = document.querySelector("div.MuiStack-root.css-1pu19h3");
    if (existing) {
      console.log("✅ Chat frame already present:", existing);
      return existing;
    }

    // Watch DOM changes
    const observer = new MutationObserver((mutations, obs) => {
      for (const m of mutations) {
        for (const node of m.addedNodes) {
          if (node.nodeType === 1 && node.querySelector("textarea.input-container")) {
            obs.disconnect();
            console.log("✅ Chat input ready:", node);
            // Fire global event so you can hook anything after
            document.dispatchEvent(new Event("chat_ready"));
        }
      }
      }
      
    });

    observer.observe(document.body, {
      childList: true,
      subtree: true
    });

    console.log("👀 Watching DOM for chat frame...");
    
  }

waitForChatFrame()
}

function hideAvatarChatOverlay() {
  const opts = { bubbles: true, cancelable: true, view: window };

  // --- 1️⃣ Hide messages on avatar ---
  const hideMsgBtn = document.querySelector("div.MuiStack-root.css-qcyw6z svg[data-name='down-small']");
  if (hideMsgBtn) {
    ["mouseover", "mousedown", "mouseup", "click"].forEach(ev =>
      hideMsgBtn.dispatchEvent(new MouseEvent(ev, opts))
    );
    console.log("🎯 Hidden messages overlay on avatar!");

    // --- 2️⃣ Fire global event ---
    document.dispatchEvent(new Event("teacher_input_ready"));
    console.log("🚀 Event 'teacher_input_ready' dispatched!");
  } else {
    console.log("⚠️ Hide messages button not found, will retry...");
    setTimeout(hideAvatarChatOverlay, 500); // keep retrying THIS function
    return;
  }
  setTrafficState("free");
}

let teacherListenerRegistered = false;
const teacherProcessedMessageIds = new Set();
const TEACHER_MAX_TRACKED = 200;
let teacherLastInboundMeta = {};

function resolveTeacherPage() {
  if (typeof detectPageRole === "function") {
    try {
      return detectPageRole();
    } catch (err) {
      console.warn("teacher.js: failed to resolve detectPageRole():", err);
    }
  }
  return "unknown";
}

async function notifySttTeacherTurnFinished(contextMeta = {}, status = "done") {
  const payload = {
    kind: "teacher_turn_finished",
    text: "teacher_turn_finished",
    meta: {
      source_role: "teacher",
      ts_ms: Date.now(),
      status: String(status || "done"),
      flow_run_id: contextMeta?.flow_run_id || null,
      ai_message_id: contextMeta?.ai_message_id || null,
      source_segment_id: contextMeta?.source_segment_id || null,
      source_kind: contextMeta?.source_kind || null,
    },
  };

  let sent = false;
  let via = "none";
  try {
    if (typeof sendRouterMessage === "function") {
      via = "sendRouterMessage";
      sent = await sendRouterMessage(payload, "stt");
    } else if (typeof queueRouterMessage === "function") {
      via = "queueRouterMessage";
      queueRouterMessage(payload, "stt");
      sent = true;
    }
  } catch (err) {
    teacherLog("teacher_turn_finished_signal_error", {
      via,
      status,
      err: String(err?.message || err),
      flow_run_id: contextMeta?.flow_run_id || null,
      source_segment_id: contextMeta?.source_segment_id || null,
    }, "warn");
    return false;
  }

  teacherLog(
    sent ? "teacher_turn_finished_signal_sent" : "teacher_turn_finished_signal_failed",
    {
      via,
      status,
      flow_run_id: contextMeta?.flow_run_id || null,
      source_segment_id: contextMeta?.source_segment_id || null,
      ai_message_id: contextMeta?.ai_message_id || null,
    },
    sent ? "info" : "warn"
  );
  return sent;
}

function teacherMessageKey(payload) {
  if (!payload) return null;
  if (payload.id) return payload.id;
  if (typeof payload === "string") return payload;
  if (payload.text) return payload.text;
  return JSON.stringify(payload);
}

function teacherIsDuplicate(payload) {
  const key = teacherMessageKey(payload);
  if (!key) return false;
  if (teacherProcessedMessageIds.has(key)) return true;
  teacherProcessedMessageIds.add(key);
  if (teacherProcessedMessageIds.size > TEACHER_MAX_TRACKED) {
    const iterator = teacherProcessedMessageIds.values();
    teacherProcessedMessageIds.delete(iterator.next().value);
  }
  return false;
}
function processTeacherPayload(label, payload) {
  if (!payload) return;
  if (teacherIsDuplicate(payload)) {
    console.log("↩️ teacher duplicate ignored:", { label, payload });
    teacherLog("teacher_duplicate_ignored", { label, payload_id: payload?.id || null });
    return;
  }

  // Normalize the payload to the inner message if wrapped.
  const normalized = payload.message || payload;

  console.log("📨 Received message for teacher:", normalized);
  // Safety: if the traffic flag is stuck in "busy" due to a missed init, unlock when not streaming.
  try {
    if (typeof setTrafficState === "function" && typeof isStreamingNow === "function" && !isStreamingNow()) {
      setTrafficState("free");
    }
  } catch (_) {
    // ignore
  }
  try {
    const runId = (normalized && typeof normalized === "object") ? normalized?.meta?.flow_run_id : null;
    if (runId && typeof globalThis.AT?.setRunId === "function") {
      globalThis.AT.setRunId(runId, "log");
    }
  } catch (_) {
    // ignore
  }
  teacherUi(
    "teacher_inbound",
    "Teacher: inbound message",
    { label, id: normalized?.id || null, text_len: typeof normalized === "string" ? normalized.length : (normalized?.text || "").length }
  );
  teacherLog("teacher_inbound", { label, normalized });

  const text =
    typeof normalized === "string"
      ? normalized
      : normalized?.text ?? JSON.stringify(normalized);
  teacherLastInboundMeta = {
    flow_run_id: normalized?.meta?.flow_run_id || null,
    ai_message_id: normalized?.id || null,
    source_segment_id:
      normalized?.meta?.source_segment_id ||
      normalized?.meta?.segment_id ||
      null,
    source_kind: normalized?.meta?.source_kind || normalized?.kind || null,
  };
  enqueueTeacherSend(text, teacherLastInboundMeta);

}

function listenForTeacherMessages() {
  if (!(typeof chrome !== "undefined" && chrome.runtime?.onMessage?.addListener)) {
    console.warn("chrome.runtime.onMessage unavailable; teacher listener skipped.");
    return;
  }

  if (teacherListenerRegistered) return;
  teacherListenerRegistered = true;

  chrome.runtime.onMessage.addListener(msg => {
    if (msg.to === "teacher" && msg.from === "ai") {
      runWhenTrafficReady(() =>
        processTeacherPayload("direct-message", msg.message),
        "teacher-onMessage"
      );
    }
  });
}

function teacherSleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function waitForTrafficFreePromise(label = "teacher-wait") {
  return new Promise(resolve => {
    if (typeof runWhenTrafficFree === "function") {
      runWhenTrafficFree(resolve, label);
    } else {
      resolve();
    }
  });
}

let teacherSendChain = Promise.resolve();
function enqueueTeacherSend(text, contextMeta = {}) {
  if (typeof text !== "string" || text.trim() === "") return;
  teacherLog("teacher_queue_send", {
    text_len: text.length,
    flow_run_id: contextMeta?.flow_run_id || null,
    source_segment_id: contextMeta?.source_segment_id || null,
  });
  teacherSendChain = teacherSendChain
    .catch(err => console.warn("teacherSendChain: previous error:", err))
    .then(async () => {
      await waitForTrafficFreePromise("teacher-queue");
      // Don't try to inject/send while the avatar is already streaming.
      await waitForCondition(() => !isStreamingNow(), {
        timeoutMs: 90000,
        intervalMs: 250,
        label: "wait-not-streaming"
      });
      teacherUi("teacher_send", "Teacher: sending to avatar", { text_len: text.length });
      await sendAvatarMessage(text, contextMeta);
    });
}

function isStreamingNow() {
  // Fallback to DOM in case the MutationObserver lags.
  const selector = "button[aria-label='stop'] svg[data-name='pause']";
  return Boolean(isAvatarStreaming || document.querySelector(selector));
}

async function waitForCondition(cond, { timeoutMs = 15000, intervalMs = 250, label = "condition" } = {}) {
  const startedAt = Date.now();
  while (Date.now() - startedAt < timeoutMs) {
    let ok = false;
    try {
      ok = Boolean(cond());
    } catch (err) {
      console.warn(`waitForCondition(${label}) threw:`, err);
    }
    if (ok) return true;
    await teacherSleep(intervalMs);
  }
  console.warn(`waitForCondition(${label}) timed out after ${timeoutMs}ms`);
  return false;
}

async function findTeacherInput(timeoutMs = 20000) {
  const startedAt = Date.now();
  while (Date.now() - startedAt < timeoutMs) {
    const textarea = document.querySelector("textarea.input-container");
    if (textarea) return { el: textarea, kind: "textarea" };
    const editable = document.querySelector("div#prompt-textarea[contenteditable='true']");
    if (editable) return { el: editable, kind: "contenteditable" };
    await teacherSleep(250);
  }
  return null;
}

async function findTeacherSendButton(timeoutMs = 20000) {
  const startedAt = Date.now();
  while (Date.now() - startedAt < timeoutMs) {
    const sendBtn = Array.from(document.querySelectorAll("button"))
      .find(btn => btn.querySelector("svg[data-name='send-msg']"));
    if (sendBtn) return sendBtn;
    await teacherSleep(250);
  }
  return null;
}

async function sendAvatarMessage(message, contextMeta = {}) {
  const textToInsert = String(message || "").trim();
  if (!textToInsert) return false;

  if (typeof setTrafficState === "function") {
    setTrafficState("busy");
  }

  let clickedSend = false;
  let turnStatus = "started";
  try {
    // Small buffer so the UI has time to settle before we query.
    await teacherSleep(300);

    const input = await findTeacherInput(20000);
    if (!input?.el) {
      turnStatus = "input_missing";
      console.warn("⚠️ Teacher input not found; dropping message.");
      teacherUi("teacher_input_missing", "Teacher: input not found", {}, { level: "warn", ttlMs: 6500 });
      teacherLog("teacher_input_missing", {}, "warn");
      return false;
    }

    try {
      input.el.focus?.();
    } catch (_) {
      // ignore
    }

    // Insert text in a way that works for both textarea and contenteditable.
    if (input.kind === "textarea") {
      input.el.value = textToInsert;
      input.el.dispatchEvent(new Event("input", { bubbles: true }));
    } else {
      input.el.textContent = textToInsert;
      if (typeof InputEvent === "function") {
        input.el.dispatchEvent(new InputEvent("input", { bubbles: true }));
      } else {
        input.el.dispatchEvent(new Event("input", { bubbles: true }));
      }
    }
    console.log("✍️ Inserted text:", textToInsert);

    const sendBtn = await findTeacherSendButton(20000);
    if (!sendBtn) {
      turnStatus = "send_button_missing";
      console.warn("⚠️ Send button not found; dropping message.");
      teacherUi("teacher_send_button_missing", "Teacher: send button not found", {}, { level: "warn", ttlMs: 6500 });
      teacherLog("teacher_send_button_missing", {}, "warn");
      return false;
    }

    const opts = { bubbles: true, cancelable: true, view: window };
    ["mouseover", "mousedown", "mouseup", "click"].forEach(ev => {
      sendBtn.dispatchEvent(new MouseEvent(ev, opts));
    });
    clickedSend = true;
    console.log("📨 Smart click fired on send button!");

    // Wait for a full streaming cycle. Don't assume it starts immediately.
    const started = await waitForCondition(() => isStreamingNow(), {
      timeoutMs: 12000,
      intervalMs: 250,
      label: "avatar-stream-start"
    });

    if (!started) {
      // Sometimes the UI doesn't show a stream indicator; avoid unlocking instantly.
      await teacherSleep(1500);
      turnStatus = "no_stream_indicator";
    } else {
      await waitForCondition(() => !isStreamingNow(), {
        timeoutMs: 90000,
        intervalMs: 250,
        label: "avatar-stream-end"
      });
      turnStatus = "stream_finished";
    }

    document.dispatchEvent(new Event("avatar_stream_finished"));
    if (turnStatus === "started") turnStatus = "completed";
    return true;
  } catch (err) {
    turnStatus = "error";
    console.warn("sendAvatarMessage failed:", err);
    teacherUi(
      "teacher_send_failed",
      "Teacher: send failed",
      { err: String(err?.message || err) },
      { level: "warn", ttlMs: 6500 }
    );
    teacherLog("teacher_send_failed", { err: String(err?.message || err) }, "warn");
    if (clickedSend) {
      document.dispatchEvent(new Event("avatar_stream_finished"));
    }
    return false;
  } finally {
    await notifySttTeacherTurnFinished(contextMeta || teacherLastInboundMeta || {}, turnStatus);
    if (typeof setTrafficState === "function") {
      setTrafficState("free");
    }
  }
}









function watchSessionDialog() {
  const targetSelector = "div[role='dialog'] .MuiTypography-body1";

  const observer = new MutationObserver(() => {
  const dialog = document.querySelector(targetSelector);
    if (dialog && dialog.textContent.includes("Do you still need me here?")) {
      console.log("🚨 ALERT page appeared!");

      // Find and click Stay active button
      const stayBtn = document.querySelector("div[role='dialog'] button.MuiButton-containedPrimary");
      if (stayBtn) {
        stayBtn.click();
        console.log("🔥 Clicked 'Stay active' to keep session alive!");
      } else {
        console.log("⚠️ Stay active button not found!");
      }
    }
    
  });

  withBodyReady(body => observer.observe(body, { childList: true, subtree: true }));
  console.log("👀 Watching for session break dialog...");
}

// --- Global variable ---
let isAvatarStreaming = false;

function watchAvatarStreaming() {
  const selector = "button[aria-label='stop'] svg[data-name='pause']";

  const observer = new MutationObserver(() => {
    const isStreaming = !!document.querySelector(selector);

    if (isStreaming && !isAvatarStreaming) {
      isAvatarStreaming = true;
      console.log("🎥 Model streaming started → isAvatarStreaming =", isAvatarStreaming);
    } else if (!isStreaming && isAvatarStreaming) {
      isAvatarStreaming = false;
      console.log("⏹️ Model streaming stopped → isAvatarStreaming =", isAvatarStreaming);
    }
  });

  withBodyReady(body => observer.observe(body, { childList: true, subtree: true }));
  console.log("👀 Watching DOM for model streaming state...");
}

function reloadWhenTimerLow(){
  const observer = new MutationObserver(() => {
    // Look for the timer element
    const timer = document.querySelector(
      ".MuiTypography-root.MuiTypography-caption-m.css-64kod5"
    );

    if (timer) {
      const timeText = timer.textContent.trim(); // e.g. "00:49"
      const [mins, secs] = timeText.split(":").map(Number);
      const totalSeconds = mins * 60 + secs;

      if (totalSeconds <= 30) {
        console.log(`⏰ Time low (${timeText}) → Reloading page...`);
        location.reload(); // Hard reload
      }
    }
  });

  withBodyReady(body => observer.observe(body, { childList: true, subtree: true }));
  console.log("👀 Watching countdown timer...");

}

function reloadWhenCriticalTime() {
  const observer = new MutationObserver(() => {
    const timer = document.querySelector(
      ".MuiTypography-root.MuiTypography-caption-m.css-64kod5"
    );

    if (timer) {
      const timeText = timer.textContent.trim(); // e.g. "00:49"
      const [mins, secs] = timeText.split(":").map(Number);
      const totalSeconds = mins * 60 + secs;

      if (totalSeconds <= 20) { // critical threshold
        console.log(`⚠️ Critical time (${timeText}) reached!`);

        const waitAndReload = () => {
          if (!isAvatarStreaming) {
            console.log("⏱ Not streaming → Reloading page now!");
            location.reload();
          } else {
            console.log("⏱ Streaming active, waiting...");
            setTimeout(waitAndReload, 500); // retry in 0.5s
          }
        };

        waitAndReload();
      }
    }
  });

  withBodyReady(body => observer.observe(body, { childList: true, subtree: true }));
  console.log("👀 Watching countdown timer for critical reload...");
}


function runTeacherAutomation(){
  waitForVideoReady();
  watchSessionDialog();
  watchAvatarStreaming();
  document.addEventListener("video_ready", initializeAvatarChat, { once: true });
  document.addEventListener("chat_ready", hideAvatarChatOverlay, { once: true });
  document.addEventListener("teacher_input_ready", listenForTeacherMessages, { once: true });
  document.addEventListener("avatar_stream_finished", reloadWhenTimerLow, { once: true });
}

// Ensure we always listen for backend messages even if other hooks fail.
// (Keep it idempotent; listenForTeacherMessages() is guarded.)
if (resolveTeacherPage() === "teacher") {
  listenForTeacherMessages();
}
