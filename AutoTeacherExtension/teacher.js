
//-------------------

function waitVideoFullyReady(selector = "video.css-m0a7mp", buffer = 1000) {
  console.log("🔥 [DEBUG] waitVideoFullyReady stub. selector:", selector);
  ch_traffic("b");
  bg_listener();
  setTimeout(() => {
    console.log("🧪 [DEBUG] Synthetic videoloaded fired");
    document.dispatchEvent(new Event("videoloaded"));
  }, buffer);
}
function prepareModel() {
  // Find the 'Repeat' button dynamically
  const repeatBtn = Array.from(document.querySelectorAll('.MuiTab-root'))
                         .find(btn => btn.innerText.trim() === "Repeat");

  if (!repeatBtn) {
    console.log("⚠️ Repeat button not found, retrying...");
    return setTimeout(prepareModel, 500); // retry THIS function
  }

  const opts = { bubbles: true, cancelable: true, view: window };

  // Smart click sequence
  repeatBtn.dispatchEvent(new MouseEvent("mouseover", opts));
  repeatBtn.dispatchEvent(new MouseEvent("mousedown", opts));
  repeatBtn.dispatchEvent(new MouseEvent("mouseup", opts));
  repeatBtn.dispatchEvent(new MouseEvent("click", opts));

  console.log("🎯 Smart click fired on 'Repeat' button!");

  // Now look for Let's Chat button
const chatBtn = Array.from(document.querySelectorAll('button.MuiButton-root'))
  .find(btn => btn.textContent.includes("Let's chat"));

  if (!chatBtn) {
    console.log("⚠️ Let's chat button not found, retrying...");
    return setTimeout(prepareModel, 500); // retry again until it exists
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
            document.dispatchEvent(new Event("model_semiprepered"));
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

function hide_chat() {
  const opts = { bubbles: true, cancelable: true, view: window };

  // --- 1️⃣ Hide messages on avatar ---
  const hideMsgBtn = document.querySelector("div.MuiStack-root.css-qcyw6z svg[data-name='down-small']");
  if (hideMsgBtn) {
    ["mouseover", "mousedown", "mouseup", "click"].forEach(ev =>
      hideMsgBtn.dispatchEvent(new MouseEvent(ev, opts))
    );
    console.log("🎯 Hidden messages overlay on avatar!");

    // --- 2️⃣ Fire global event ---
    document.dispatchEvent(new Event("readforinput"));
    console.log("🚀 Event 'readforinput' dispatched!");
  } else {
    console.log("⚠️ Hide messages button not found, will retry...");
    setTimeout(hide_chat, 500); // keep retrying THIS function
    return;
  }
  ch_traffic('f')
}

let bgListenerRegistered = false;
let teacherPollTimer = null;
const teacherProcessedMessageIds = new Set();
const TEACHER_MAX_TRACKED = 200;

function resolveTeacherPage() {
  if (typeof current_page !== "undefined") return current_page;
  if (typeof page === "function") {
    try {
      return page();
    } catch (err) {
      console.warn("teacher.js: failed to resolve page():", err);
    }
  }
  return "unknown";
}

function teacherMessageKey(payload) {
  if (!payload) return null;
  if (payload.id) return payload.id;
  if (payload.text && payload.timestamp) return `${payload.text}|${payload.timestamp}`;
  if (typeof payload === "string") return payload;
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
    return;
  }

  const text =
    typeof payload === "string"
      ? payload
      : payload?.text ?? JSON.stringify(payload);

  console.log("🤖➡️👩‍🏫 AI says:", {
    label,
    id: payload?.id,
    text,
    raw: payload
  });
  sendMessageToModel(text);
}

function bg_listener() {
  if (bgListenerRegistered) return;
  bgListenerRegistered = true;

  chrome.runtime.onMessage.addListener(msg => {
    console.log("📨 Incoming extension message:", msg);

    if (msg.to === "teacher" && msg.from === "ai") {
      processTeacherPayload("direct-message", msg.message);
    }
  });
}

async function pollTeacherRouteMessages() {
  const pageName = resolveTeacherPage();
  if (pageName !== "teacher") {
    teacherPollTimer = setTimeout(pollTeacherRouteMessages, 2000);
    return;
  }

  try {
    const res = await fetch("http://127.0.0.1:5000/get_messages/teacher");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const messages = data.messages || [];
    if (messages.length > 0) {
      messages.forEach(msg => {
        const payload = msg?.message ?? msg;
        processTeacherPayload("route-poll", payload);
      });
    }
  } catch (err) {
    console.error("teacher.js: route poll failed:", err);
  }

  teacherPollTimer = setTimeout(pollTeacherRouteMessages, 1000);
}


function sendMessageToModel(message) {
  setTimeout(() => {
    const textToInsert = message;
    const textarea = 
    document.querySelector("textarea.input-container") || 
    document.querySelector("div#prompt-textarea[contenteditable='true']");

    if (textarea) {
      // Smart type
      textarea.value = textToInsert;
      textarea.dispatchEvent(new Event("input", { bubbles: true }));
      console.log("✍️ Inserted text:", textToInsert);

      // Find send button (the one with send-msg icon)
      const sendBtn = Array.from(document.querySelectorAll("button"))
        .find(btn => btn.querySelector("svg[data-name='send-msg']"));

      if (sendBtn) {
        const opts = { bubbles: true, cancelable: true, view: window };
        sendBtn.dispatchEvent(new MouseEvent("mouseover", opts));
        sendBtn.dispatchEvent(new MouseEvent("mousedown", opts));
        sendBtn.dispatchEvent(new MouseEvent("mouseup", opts));
        sendBtn.dispatchEvent(new MouseEvent("click", opts));
        console.log("📨 Smart click fired on send button!");
        function finishted(){
          if (!modelStreaming) {
          document.dispatchEvent(new Event("finishStreaming"));
          console.log("Streaming finishted!")
            ch_traffic('f')
          } else {
            finishted()
          }
        }
        finishted()
        
      } else {
        console.log("⚠️ Send button not found!");
      }
    } else {
      console.log("⚠️ Textarea not found!");
    }
  }, 2000); // 1s buffer


}


function watchForAlertDialog() {
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

  observer.observe(document.body, { childList: true, subtree: true });
  console.log("👀 Watching for session break dialog...");
}

// --- Global variable ---
let modelStreaming = false;

function watchModelStreaming() {
  const selector = "button[aria-label='stop'] svg[data-name='pause']";

  const observer = new MutationObserver(() => {
    const isStreaming = !!document.querySelector(selector);

    if (isStreaming && !modelStreaming) {
      modelStreaming = true;
      console.log("🎥 Model streaming started → modelStreaming =", modelStreaming);
    } else if (!isStreaming && modelStreaming) {
      modelStreaming = false;
      console.log("⏹️ Model streaming stopped → modelStreaming =", modelStreaming);
    }
  });

  observer.observe(document.body, { childList: true, subtree: true });
  console.log("👀 Watching DOM for model streaming state...");
}

function reload(){
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

  observer.observe(document.body, { childList: true, subtree: true });
  console.log("👀 Watching countdown timer...");

}

function reloadWhenCritical() {
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
          if (!modelStreaming) {
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

  observer.observe(document.body, { childList: true, subtree: true });
  console.log("👀 Watching countdown timer for critical reload...");
}


function feedModel(){
  waitVideoFullyReady();
  watchForAlertDialog();
  watchModelStreaming();
  document.addEventListener("videoloaded", prepareModel, { once: true })
  document.addEventListener("model_semiprepered", hide_chat, { once: true })
  document.addEventListener("readforinput", bg_listener, { once: true })
  document.addEventListener("finishStreaming", reload, { once: true })
}

if (resolveTeacherPage() === "teacher") {
  bg_listener();
  pollTeacherRouteMessages();
}

// Ensure we always listen for backend messages even if other hooks fail.
if (typeof current_page !== "undefined" && current_page === "teacher") {
  bg_listener();
}
