// Provide a safe helper for querying tabs from content scripts without mutating
// `chrome` APIs (newer Chrome builds can make that object non-extensible).
(function initTabsQueryShim() {
  if (typeof globalThis === "undefined") return;
  if (typeof globalThis.atTabsQuery === "function") return;
  globalThis.atTabsQuery = (queryInfo, callback) => {
    if (!(typeof chrome !== "undefined" && chrome.runtime?.sendMessage)) {
      if (typeof callback === "function") callback([]);
      return;
    }
    try {
      chrome.runtime.sendMessage({ type: "tabs.query", queryInfo }, response => {
        const err = chrome.runtime.lastError;
        if (err) {
          console.warn("tabs.query shim failed:", err.message || err);
          if (typeof callback === "function") callback([]);
          return;
        }
        if (typeof callback === "function") callback(response?.tabs || []);
      });
    } catch (err) {
      console.warn("tabs.query shim threw:", err);
      if (typeof callback === "function") callback([]);
    }
  };
})();

function updateStatus() {
  console.log("Update status...");
}

// ===================== NativeCamp: Post-login Redirect =====================
const AT_TUTORIAL_URL = "https://nativecamp.net/teacher/lesson-tutorial";
const AT_POST_LOGIN_REDIRECT_KEY = "at_post_login_redirect";
const AT_POST_LOGIN_REDIRECT_ATTEMPTS_KEY = "at_post_login_redirect_attempts";
const AT_POST_LOGIN_REDIRECT_STARTED_TS_KEY = "at_post_login_redirect_started_ts";
const AT_POST_LOGIN_REDIRECT_MAX_AGE_MS = 2 * 60 * 1000; // 2 minutes

function atIsNativeCamp() {
  try {
    return String(window.location.hostname || "").toLowerCase().includes("nativecamp.net");
  } catch (_) {
    return false;
  }
}

function atIsHomePath() {
  try {
    return String(window.location.pathname || "").includes("/teacher/home");
  } catch (_) {
    return false;
  }
}

function atIsTutorialPath() {
  try {
    return String(window.location.pathname || "") === "/teacher/lesson-tutorial";
  } catch (_) {
    return false;
  }
}

function atSafeSendMessage(payload) {
  try {
    const p = chrome?.runtime?.sendMessage?.(payload);
    if (p && typeof p.then === "function") p.catch(() => {});
  } catch (_) {
    // ignore
  }
}

function atArmPostLoginRedirect(targetUrl) {
  const target = String(targetUrl || "").trim();
  if (!target) return false;
  const startedTs = Date.now();
  try {
    localStorage.setItem(AT_POST_LOGIN_REDIRECT_KEY, target);
    localStorage.setItem(AT_POST_LOGIN_REDIRECT_ATTEMPTS_KEY, "0");
    localStorage.setItem(AT_POST_LOGIN_REDIRECT_STARTED_TS_KEY, String(startedTs));
  } catch (_) {
    // ignore
  }
  try {
    chrome?.storage?.local?.set?.({
      [AT_POST_LOGIN_REDIRECT_KEY]: target,
      [AT_POST_LOGIN_REDIRECT_ATTEMPTS_KEY]: 0,
      [AT_POST_LOGIN_REDIRECT_STARTED_TS_KEY]: startedTs
    });
  } catch (_) {
    // ignore
  }
  return true;
}

function atClearPostLoginRedirect() {
  try {
    localStorage.removeItem(AT_POST_LOGIN_REDIRECT_KEY);
    localStorage.removeItem(AT_POST_LOGIN_REDIRECT_ATTEMPTS_KEY);
    localStorage.removeItem(AT_POST_LOGIN_REDIRECT_STARTED_TS_KEY);
  } catch (_) {
    // ignore
  }
  try {
    chrome?.storage?.local?.remove?.([
      AT_POST_LOGIN_REDIRECT_KEY,
      AT_POST_LOGIN_REDIRECT_ATTEMPTS_KEY,
      AT_POST_LOGIN_REDIRECT_STARTED_TS_KEY
    ]);
  } catch (_) {
    // ignore
  }
}

function atReadPostLoginRedirectSync() {
  try {
    const target = localStorage.getItem(AT_POST_LOGIN_REDIRECT_KEY);
    const attempts = Number(localStorage.getItem(AT_POST_LOGIN_REDIRECT_ATTEMPTS_KEY) || "0");
    const startedTs = Number(localStorage.getItem(AT_POST_LOGIN_REDIRECT_STARTED_TS_KEY) || "0");
    return { target, attempts, startedTs };
  } catch (_) {
    return { target: null, attempts: 0, startedTs: 0 };
  }
}

function maybePostLoginRedirect() {
  if (!atIsNativeCamp()) return false;
  if (!atIsHomePath()) return false;

  // If we already landed, clear any stale flags.
  if (atIsTutorialPath()) {
    atClearPostLoginRedirect();
    return false;
  }

  // Prevent multiple concurrent loops.
  if (globalThis.__AT_POST_LOGIN_REDIRECT_LOOP_ACTIVE__) return true;

  const { target, attempts, startedTs } = atReadPostLoginRedirectSync();
  if (target && typeof target === "string") {
    const ageMs = startedTs > 0 ? (Date.now() - startedTs) : 0;
    if (ageMs > AT_POST_LOGIN_REDIRECT_MAX_AGE_MS) {
      atClearPostLoginRedirect();
      return false;
    }

    globalThis.__AT_POST_LOGIN_REDIRECT_LOOP_ACTIVE__ = true;

    const maxAttempts = 8;
    const doAttempt = () => {
      try {
        // Stop if we reached the target.
        if (atIsTutorialPath() || String(window.location.href || "") === target) {
          atClearPostLoginRedirect();
          globalThis.__AT_POST_LOGIN_REDIRECT_LOOP_ACTIVE__ = false;
          return;
        }

        const st = atReadPostLoginRedirectSync();
        let n = Number(st.attempts);
        if (!Number.isFinite(n) || n < 0) n = 0;
        if (n >= maxAttempts) {
          console.warn("[AT] post-login redirect gave up (max attempts).", { target, attempts: n, maxAttempts });
          atClearPostLoginRedirect();
          globalThis.__AT_POST_LOGIN_REDIRECT_LOOP_ACTIVE__ = false;
          return;
        }

        n += 1;
        try {
          localStorage.setItem(AT_POST_LOGIN_REDIRECT_ATTEMPTS_KEY, String(n));
        } catch (_) {
          // ignore
        }
        try {
          chrome?.storage?.local?.set?.({ [AT_POST_LOGIN_REDIRECT_ATTEMPTS_KEY]: n });
        } catch (_) {
          // ignore
        }

        console.log(`[AT] post-login redirect attempt ${n}/${maxAttempts} (in 1s) ->`, target);

        // 1s buffer before navigation.
        setTimeout(() => {
          try {
            if (atIsTutorialPath() || String(window.location.href || "") === target) return;
            // Attempt 1: navigate inside the tab.
            window.location.assign(target);
          } catch (_) {
            // ignore
          }
          try {
            // Attempt 2 (fallback): ask background to tabs.update this tab URL.
            atSafeSendMessage({ type: "tabs.update", url: target });
          } catch (_) {
            // ignore
          }
        }, 1000);

        // Verify and retry if we're still on home.
        setTimeout(() => {
          try {
            const ok = atIsTutorialPath() || String(window.location.href || "") === target;
            if (ok) return;
          } catch (_) {
            // ignore
          }
          // Still not there: schedule next attempt.
          doAttempt();
        }, 5000);
      } catch (_) {
        globalThis.__AT_POST_LOGIN_REDIRECT_LOOP_ACTIVE__ = false;
      }
    };

    doAttempt();
    return true;
  }

  // Fallback: localStorage may be cleared by the site; try extension storage once.
  if (!globalThis.__AT_POST_LOGIN_REDIRECT_STORAGE_LOOKUP__ && chrome?.storage?.local?.get) {
    globalThis.__AT_POST_LOGIN_REDIRECT_STORAGE_LOOKUP__ = true;
    try {
      chrome.storage.local.get(
        [AT_POST_LOGIN_REDIRECT_KEY, AT_POST_LOGIN_REDIRECT_ATTEMPTS_KEY, AT_POST_LOGIN_REDIRECT_STARTED_TS_KEY],
        items => {
          globalThis.__AT_POST_LOGIN_REDIRECT_STORAGE_LOOKUP__ = false;
          const t = items?.[AT_POST_LOGIN_REDIRECT_KEY];
          if (!t) return;
          try {
            localStorage.setItem(AT_POST_LOGIN_REDIRECT_KEY, String(t));
            localStorage.setItem(AT_POST_LOGIN_REDIRECT_ATTEMPTS_KEY, String(items?.[AT_POST_LOGIN_REDIRECT_ATTEMPTS_KEY] ?? 0));
            localStorage.setItem(AT_POST_LOGIN_REDIRECT_STARTED_TS_KEY, String(items?.[AT_POST_LOGIN_REDIRECT_STARTED_TS_KEY] ?? Date.now()));
          } catch (_) {
            // ignore
          }
          // Re-run now that localStorage is repopulated.
          try {
            maybePostLoginRedirect();
          } catch (_) {
            // ignore
          }
        }
      );
    } catch (_) {
      globalThis.__AT_POST_LOGIN_REDIRECT_STORAGE_LOOKUP__ = false;
    }
  }

  return false;
}

function performLogin() {
  let login_username = document.querySelector("#TeacherUsername");
  let login_password = document.querySelector("#TeacherPassword");
  let login_btn = document.querySelector(".btn_green");

  setTimeout(() => {
    if (login_username && login_password && login_btn) {
      atArmPostLoginRedirect(AT_TUTORIAL_URL);

      login_username.value = "xhani.iljard@gmail.com";
      login_password.value = "voiledofficialN@01";
      login_btn.click();
      console.log("clicked login");
    } else {
      console.log("Login elements not found!");
    }
  }, 5500);

}

const AT_CAMERA_PRIME_FLAG_KEY = "at_camera_prime_ok_v1";
const AT_CAMERA_PRIME_LAST_ATTEMPT_KEY = "at_camera_prime_attempt_ts_v1";
const AT_CAMERA_PRIME_RETRY_MS = 30 * 1000;

function atCameraPrimeLog(event, data = {}, level = "info") {
  try {
    globalThis.AT?.log?.(event, data, level);
  } catch (_) {
    // ignore
  }
}

function atShouldPrimeCameraOnCurrentPage() {
  try {
    const host = String(window.location.hostname || "").toLowerCase();
    const path = String(window.location.pathname || "").toLowerCase();
    const isNativeCamp = host.includes("nativecamp.net");
    const isAkool = host.includes("akool.com");
    if (
      isNativeCamp &&
      (
        path.includes("/teacher/home") ||
        path.includes("/teacher/lesson-tutorial") ||
        path.includes("/teacher/chat")
      )
    ) {
      return true;
    }
    if (isAkool && path.includes("/apps/streaming-avatar/edit")) {
      return true;
    }
    return (
      false
    );
  } catch (_) {
    return false;
  }
}

function atReadSessionStorageSafe(key) {
  try {
    return sessionStorage.getItem(key);
  } catch (_) {
    return null;
  }
}

function atWriteSessionStorageSafe(key, value) {
  try {
    sessionStorage.setItem(key, String(value));
  } catch (_) {
    // ignore
  }
}

function atReadExtensionSession(keys = []) {
  return new Promise(resolve => {
    try {
      if (!chrome?.storage?.session?.get) {
        resolve({});
        return;
      }
      chrome.storage.session.get(keys, items => {
        const err = chrome?.runtime?.lastError;
        if (err) {
          resolve({});
          return;
        }
        resolve(items || {});
      });
    } catch (_) {
      resolve({});
    }
  });
}

function atWriteExtensionSession(items = {}) {
  return new Promise(resolve => {
    try {
      if (!chrome?.storage?.session?.set) {
        resolve(false);
        return;
      }
      chrome.storage.session.set(items, () => {
        const err = chrome?.runtime?.lastError;
        resolve(!err);
      });
    } catch (_) {
      resolve(false);
    }
  });
}

async function atHasCameraPrimeFlag() {
  if (globalThis.__AT_CAMERA_PRIMED__ === true) return true;

  const localFlag = atReadSessionStorageSafe(AT_CAMERA_PRIME_FLAG_KEY);
  if (localFlag === "1") {
    globalThis.__AT_CAMERA_PRIMED__ = true;
    return true;
  }

  const storeItems = await atReadExtensionSession([AT_CAMERA_PRIME_FLAG_KEY]);
  if (storeItems?.[AT_CAMERA_PRIME_FLAG_KEY]) {
    atWriteSessionStorageSafe(AT_CAMERA_PRIME_FLAG_KEY, "1");
    globalThis.__AT_CAMERA_PRIMED__ = true;
    return true;
  }

  return false;
}

async function atMarkCameraPrimed() {
  globalThis.__AT_CAMERA_PRIMED__ = true;
  atWriteSessionStorageSafe(AT_CAMERA_PRIME_FLAG_KEY, "1");
  await atWriteExtensionSession({ [AT_CAMERA_PRIME_FLAG_KEY]: true });
}

async function atGetCameraPrimeLastAttemptTs() {
  const localTs = Number(atReadSessionStorageSafe(AT_CAMERA_PRIME_LAST_ATTEMPT_KEY) || "0");
  if (Number.isFinite(localTs) && localTs > 0) return localTs;

  const storeItems = await atReadExtensionSession([AT_CAMERA_PRIME_LAST_ATTEMPT_KEY]);
  const storeTs = Number(storeItems?.[AT_CAMERA_PRIME_LAST_ATTEMPT_KEY] || "0");
  if (Number.isFinite(storeTs) && storeTs > 0) {
    atWriteSessionStorageSafe(AT_CAMERA_PRIME_LAST_ATTEMPT_KEY, String(storeTs));
    return storeTs;
  }

  return 0;
}

async function atSetCameraPrimeLastAttemptTs(ts) {
  if (!Number.isFinite(ts) || ts <= 0) return;
  atWriteSessionStorageSafe(AT_CAMERA_PRIME_LAST_ATTEMPT_KEY, String(ts));
  await atWriteExtensionSession({ [AT_CAMERA_PRIME_LAST_ATTEMPT_KEY]: ts });
}

async function atPrimeCameraPermissionOnce(reason = "unknown", opts = {}) {
  const force = opts?.force === true;
  if (!atShouldPrimeCameraOnCurrentPage()) return { ok: false, skipped: "not_target_page" };
  if (!(navigator?.mediaDevices && typeof navigator.mediaDevices.getUserMedia === "function")) {
    atCameraPrimeLog("camera_prime_skipped", { reason, skipped: "getusermedia_unsupported" }, "warn");
    return { ok: false, skipped: "getusermedia_unsupported" };
  }

  if (await atHasCameraPrimeFlag()) {
    atCameraPrimeLog("camera_prime_skipped", { reason, skipped: "already_primed" });
    return { ok: true, skipped: "already_primed" };
  }

  const now = Date.now();
  if (!force) {
    const lastAttemptTs = await atGetCameraPrimeLastAttemptTs();
    if (lastAttemptTs > 0 && (now - lastAttemptTs) < AT_CAMERA_PRIME_RETRY_MS) {
      atCameraPrimeLog("camera_prime_skipped", { reason, skipped: "retry_cooldown", force });
      return { ok: false, skipped: "retry_cooldown" };
    }
  }
  await atSetCameraPrimeLastAttemptTs(now);

  try {
    const stream = await navigator.mediaDevices.getUserMedia({ video: true, audio: false });
    const tracks = typeof stream?.getTracks === "function" ? stream.getTracks() : [];
    for (const track of tracks) {
      try {
        track.stop();
      } catch (_) {
        // ignore
      }
    }
    await atMarkCameraPrimed();
    console.log(`[AT] camera prime success (${reason})`);
    atCameraPrimeLog("camera_prime_success", { reason, force });
    return { ok: true };
  } catch (err) {
    const name = String(err?.name || err?.message || "Error");
    console.warn(`[AT] camera prime failed (${reason})`, name);
    atCameraPrimeLog("camera_prime_failed", { reason, force, error: name }, "warn");
    return { ok: false, error: name };
  }
}

function atDisarmCameraPrimeGestureFallback() {
  const cleanup = globalThis.__AT_CAMERA_PRIME_GESTURE_CLEANUP__;
  if (typeof cleanup === "function") {
    try {
      cleanup();
    } catch (_) {
      // ignore
    }
  }
  globalThis.__AT_CAMERA_PRIME_GESTURE_CLEANUP__ = null;
}

function atArmCameraPrimeOnUserGesture() {
  if (typeof globalThis.__AT_CAMERA_PRIME_GESTURE_CLEANUP__ === "function") return;

  const events = ["pointerdown", "keydown", "touchstart", "click"];
  const handler = async () => {
    atDisarmCameraPrimeGestureFallback();
    try {
      const result = await atPrimeCameraPermissionOnce("user_gesture", { force: true });
      if (result?.ok || result?.skipped === "already_primed") return;
    } catch (_) {
      // ignore
    }
    // Keep a gesture-based retry path alive if first click attempt fails.
    atArmCameraPrimeOnUserGesture();
  };
  const cleanup = () => {
    for (const eventName of events) {
      try {
        window.removeEventListener(eventName, handler, true);
      } catch (_) {
        // ignore
      }
    }
  };

  for (const eventName of events) {
    try {
      window.addEventListener(eventName, handler, { capture: true, once: true });
    } catch (_) {
      // ignore
    }
  }
  globalThis.__AT_CAMERA_PRIME_GESTURE_CLEANUP__ = cleanup;
}

function atInitCameraPrimeForNativeCamp() {
  if (!atShouldPrimeCameraOnCurrentPage()) return;
  if (globalThis.__AT_CAMERA_PRIME_INIT_DONE__) return;
  globalThis.__AT_CAMERA_PRIME_INIT_DONE__ = true;
  atCameraPrimeLog("camera_prime_init", { url: String(window.location.href || "") });

  atArmCameraPrimeOnUserGesture();

  setTimeout(() => {
    atPrimeCameraPermissionOnce("startup")
      .then(result => {
        if (result?.ok) {
          atDisarmCameraPrimeGestureFallback();
        }
      })
      .catch(() => {});
  }, 300);
}


function setStandbyMode() {
  atInitCameraPrimeForNativeCamp();
  // If we just logged in, force navigation back to the tutorial/class page.
  // This prevents the flow from getting "stuck" on /teacher/home after successful login.
  if (maybePostLoginRedirect()) return;

  dismissFaceRecognitionModal();
  dismissTeacherMediaErrorModal();
  let status_area = document.querySelector(".area-status");
  let status_dropdown = document.querySelector("#status_select");
  let standby_btn = document.querySelector("#status_online a");

  if (status_area && status_area.innerText === "NOT STANDBY") {
    status_dropdown.style.display = "block";
    setTimeout(() => {
      standby_btn.click()
      console.log("clicked standby")
    }, 1000)
  } else {
    console.log("29: Status area not found or already standby!");
  }
}

// Run as early as possible on /teacher/home so we don't depend on content.js automation timing.
try {
  setTimeout(() => {
    try { maybePostLoginRedirect(); } catch (_) {}
    try { atInitCameraPrimeForNativeCamp(); } catch (_) {}
  }, 250);
} catch (_) {
  // ignore
}

function dismissFaceRecognitionModal() {
  const modalId = "new_face_recognition_orange";
  const removeModal = () => {
    const modal = document.getElementById(modalId);
    if (!modal) return;
    const closeBtn = modal.querySelector(".btn-close, .btn_close");
    if (closeBtn && typeof closeBtn.click === "function") {
      closeBtn.click();
    }
    modal.remove();
    console.log("🧹 Face recognition modal dismissed.");
  };

  const schedule = () => setTimeout(removeModal, 2000);

  if (document.readyState === "complete") {
    schedule();
  } else {
    window.addEventListener("load", schedule, { once: true });
  }

  const observer = new MutationObserver(() => {
    const modal = document.getElementById(modalId);
    if (modal) {
      schedule();
      observer.disconnect();
    }
  });

  if (document.body) {
    observer.observe(document.body, { childList: true, subtree: true });
  }
}

function dismissTeacherMediaErrorModal() {
  const modalId = "teacher_face_recog_no_camera";
  const removeModal = () => {
    const modal = document.getElementById(modalId);
    if (!modal) return;
    const closeBtn = modal.querySelector(".btn_close");
    if (closeBtn && typeof closeBtn.click === "function") {
      closeBtn.click();
    }
    modal.remove();
    console.log("🧹 Media error modal dismissed.");
  };

  const schedule = () => setTimeout(removeModal, 2000);

  if (document.readyState === "complete") {
    schedule();
  } else {
    window.addEventListener("load", schedule, { once: true });
  }

  const observer = new MutationObserver(() => {
    const modal = document.getElementById(modalId);
    if (modal) {
      schedule();
      observer.disconnect();
    }
  });

  if (document.body) {
    observer.observe(document.body, { childList: true, subtree: true });
  }
}

function delay(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}


// ================ Route =====================
function isWalkieReceiverPage(url = window.location.href) {
  try {
    const u = new URL(String(url || window.location.href || ""));
    const host = String(u.hostname || "").toLowerCase();
    const path = String(u.pathname || "").toLowerCase();
    if (!(host === "127.0.0.1" || host === "localhost")) return false;
    return path.startsWith("/walkie/receiver");
  } catch (_) {
    return false;
  }
}

function detectPageRole() {
  const url = String(window.location.href || "");
  const lower = url.toLowerCase();

  const hostname = String(window.location.hostname || "").toLowerCase();
  const isNativeCamp = hostname.includes("nativecamp.net");

  const looksLikeClassDom = () => {
    if (!isNativeCamp) return false;
    try {
      return Boolean(
        document.querySelector("#textbook-iframe") ||
        document.querySelector("iframe.textbook-iframe") ||
        document.querySelector(".request_detail")
      );
    } catch (_) {
      return false;
    }
  };

  if (lower.includes("chatgpt.com") || lower.includes("chat.openai.com")) return "ai";
  if (lower.includes("akool.com/apps/streaming-avatar/edit")) return "teacher";
  if (lower.includes("speechtexter.com")) return "stt";
  if (isWalkieReceiverPage(lower)) return "class"; // TEMP_WALKIE_MODE

  if (lower.includes("/teacher/lesson-tutorial")) return "class";
  if (lower.includes("/teacher/login")) return "login";

  // NativeCamp sometimes redirects the class UI under /teacher/home.
  if (lower.includes("/teacher/home")) {
    if (looksLikeClassDom()) return "class";
    return "home";
  }

  if (looksLikeClassDom()) return "class";

  return "unknown";
}


// =========== 
