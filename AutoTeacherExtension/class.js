async function acceptAllCheckboxes() {
    let checkboxes = document.querySelectorAll(".request_detail input[type='checkbox']");
    if (checkboxes.length === 0) {
      console.log("No checkboxes found!");
      try {
        globalThis.AT?.uiLog?.("class_accept_none", "Class: no request checkboxes found", {});
      } catch (_) {
        // ignore
      }
      return;
    }

    for (let cb of checkboxes) {
      if (!cb.checked) {
        await delay(500);
        cb.click();
        console.log("Checked one box ✅");
        try {
          globalThis.AT?.uiLog?.("class_accept_checkbox_click", "Class: checked request box", {});
        } catch (_) {
          // ignore
        }
        await delay(200); // wait 0.5 seconds between clicks
      }
    }

    console.log("All boxes checked ✅");

    await delay(500)
    let modal = document.querySelector("#dialog_lesson_length_cfm");

    if (modal) {
      // Traverse down to the OK button
      let ok_btn = modal.querySelector(".btn_wrap a.close_modal.btn_orange");
      if (ok_btn) {
        ok_btn.click(); // safely click the button
        console.log("Modal OK button clicked ✅");
        try {
          globalThis.AT?.uiLog?.("class_accept_modal_ok", "Class: lesson modal OK clicked", {});
        } catch (_) {
          // ignore
        }
      } else {
        console.log("OK button not found inside modal!");
      }
    } else {
      console.log("Modal not found!");
    }
}

let textbookFrameDocument;

function waitForTextbookIframe(selector = "body") {
    const iframe = document.querySelector("#textbook-iframe");
    if (!iframe) {
        // Retry if iframe not yet in DOM
        setTimeout(() => waitForTextbookIframe(selector), 500);
        return;
    } else{
      console.log("Iframe found")
    }

    // Consider it "ready" once the iframe document exists and a basic selector is present.
    // Textbook handlers are responsible for waiting until the actual text is non-empty.
    let effectiveSelector = selector;
    function checkIframeDoc() {
        let doc = null;
        try {
          doc = iframe.contentDocument || iframe.contentWindow?.document;
        } catch (err) {
          console.warn("⚠️ Failed to access iframe document:", err);
          try {
            globalThis.AT?.uiLog?.(
              "class_iframe_access_error",
              "Class: cannot access iframe document",
              { err: String(err?.message || err) },
              { level: "warn", ttlMs: 6500 }
            );
          } catch (_) {
            // ignore
          }
          setTimeout(checkIframeDoc, 500);
          return;
        }
        if (!doc) {
            setTimeout(checkIframeDoc, 500);
            return;
        }
        const target = doc.querySelector(effectiveSelector);
        // Consider it ready when the element exists. Textbook handlers will retry until the
        // content is readable (prevents hanging forever on transient empty text).
        if (target) {
            textbookFrameDocument = doc;
            // ✅ First log everything we need internally
            console.log("✅ Iframe content fully ready ✅");
            try {
              globalThis.AT?.uiLog?.("class_iframe_ready", "Class: iframe ready", {
                selector: effectiveSelector,
                url: window.location.href
              });
            } catch (_) {
              // ignore
            }

            // ✅ Then fire the custom event
            document.dispatchEvent(new Event("iframeloaded"));
        } else {
            // Retry until element appears
            setTimeout(checkIframeDoc, 500);
        }
    }
    // If iframe already fired load, start checking immediately
    if (iframe.contentDocument && iframe.contentDocument.readyState === "complete") {
        checkIframeDoc();
    } else {
        iframe.addEventListener("load", checkIframeDoc);
    }
}

let textbookTypeName;
async function detectTextbookType(ctx = {}){
  let iframe = document.querySelector('#textbook-iframe');
  if (!iframe) {
    console.warn("⚠️ detectTextbookType: iframe not found");
    return { ok: false, error: "iframe_not_found" };
  }
  const htmlDirectory = iframe.getAttribute("html-directory")
  if (!htmlDirectory) {
    console.warn("⚠️ detectTextbookType: html-directory missing");
    return { ok: false, error: "textbook_type_missing" };
  }

  textbookTypeName = htmlDirectory
  console.log("🔥 Textbook type: " + textbookTypeName)
  try {
    globalThis.AT?.uiLog?.("textbook_detected", `Textbook: ${textbookTypeName}`, {
      textbookTypeName,
      htmlDirectory,
      iframe_src: iframe?.getAttribute("src") || iframe?.src || null
    });
  } catch (_) {
    // ignore
  }
  //Execute the corresponding function to operate the Textbook
  const handler = window[textbookTypeName];
  if (typeof handler === "function") {
    const extra = (ctx && typeof ctx === "object") ? ctx : {};
    const handlerResult = await Promise.resolve(
      handler({ ...extra, book_type: textbookTypeName, bookType: textbookTypeName })
    );
    return {
      ok: true,
      bookType: textbookTypeName,
      handler: textbookTypeName,
      handler_result: handlerResult
    };
  } else {
    console.warn("⚠️ No handler registered for textbook type:", textbookTypeName);
    return { ok: false, error: "handler_missing", bookType: textbookTypeName };
  }
}

async function runClassTextbookFlow(opts = {}) {
  const mode = String(opts?.mode || "send").toLowerCase() === "send" ? "send" : "prepare";
  const intervalMs = Math.max(100, Number(opts?.intervalMs) || 500);
  const maxAttempts = Math.max(1, Number(opts?.maxAttempts) || 120);
  const source = String(opts?.source || "class_flow");
  const detectCtx = (opts?.ctx && typeof opts.ctx === "object") ? opts.ctx : {};

  if (runClassTextbookFlow._inFlight) {
    try {
      globalThis.AT?.uiLog?.(
        "class_flow_in_flight",
        "Class: flow already running",
        { source, mode },
        { level: "warn", ttlMs: 3500 }
      );
    } catch (_) {
      // ignore
    }
    return { ok: false, error: "in_flight" };
  }
  runClassTextbookFlow._inFlight = true;

  try {
    for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
      const iframe = document.querySelector("#textbook-iframe");
      if (!iframe) {
        if (attempt === 1 || attempt % 10 === 0) {
          try {
            globalThis.AT?.uiLog?.(
              "class_flow_wait_iframe",
              "Class: waiting for textbook iframe",
              { source, mode, attempt, maxAttempts }
            );
          } catch (_) {
            // ignore
          }
        }
        await delay(intervalMs);
        continue;
      }

      // Kick iframe tracking so textbookFrameDocument gets captured ASAP.
      try {
        if (typeof waitForTextbookIframe === "function") waitForTextbookIframe("body");
      } catch (_) {
        // ignore
      }

      const htmlDirectory = iframe.getAttribute("html-directory") || "";
      if (!htmlDirectory) {
        if (attempt === 1 || attempt % 10 === 0) {
          try {
            globalThis.AT?.uiLog?.(
              "class_flow_wait_textbook_type",
              "Class: waiting for textbook type",
              { source, mode, attempt, maxAttempts }
            );
          } catch (_) {
            // ignore
          }
        }
        await delay(intervalMs);
        continue;
      }

      const result = await detectTextbookType({ ...detectCtx, mode, source });
      if (result?.ok) {
        const handlerResult = result?.handler_result;
        const handlerSendOk = mode !== "send"
          ? true
          : (
            (handlerResult && typeof handlerResult === "object" && (handlerResult.ok === true || handlerResult.skipped === true))
            || handlerResult === true
          );

        if (!handlerSendOk) {
          if (attempt === 1 || attempt % 10 === 0) {
            try {
              globalThis.AT?.uiLog?.(
                "class_flow_handler_retry",
                "Class: handler did not send yet, retrying",
                { source, mode, attempt, maxAttempts, book_type: result.bookType || htmlDirectory, handler_result: handlerResult }
              );
            } catch (_) {
              // ignore
            }
          }
          await delay(intervalMs);
          continue;
        }

        try {
          globalThis.AT?.uiLog?.(
            "class_flow_detect_ok",
            `Class: detectTextbook ok (${result.bookType || htmlDirectory})`,
            {
              source,
              mode,
              attempt,
              maxAttempts,
              book_type: result.bookType || htmlDirectory,
              handler_result: handlerResult
            }
          );
        } catch (_) {
          // ignore
        }
        return {
          ok: true,
          source,
          mode,
          book_type: result.bookType || htmlDirectory,
          attempts: attempt
        };
      }

      if (attempt === 1 || attempt % 10 === 0) {
        try {
          globalThis.AT?.uiLog?.(
            "class_flow_detect_retry",
            "Class: detectTextbook retry",
            { source, mode, attempt, maxAttempts, result }
          );
        } catch (_) {
          // ignore
        }
      }
      await delay(intervalMs);
    }

    try {
      globalThis.AT?.uiLog?.(
        "class_flow_detect_timeout",
        "Class: detectTextbook timed out",
        { source, mode, maxAttempts },
        { level: "warn", ttlMs: 6500 }
      );
    } catch (_) {
      // ignore
    }
    return { ok: false, error: "detect_timeout", source, mode, attempts: maxAttempts };
  } finally {
    runClassTextbookFlow._inFlight = false;
  }
}
runClassTextbookFlow._inFlight = false;

// Shared helper used by popup/manual path and automatic watcher path.
window.runClassTextbookFlow = runClassTextbookFlow;


// Textbook-specific handlers live under `AutoTeacherExtension/handlers/`.
// They should send a `lesson_package` to the local router; the server injects the
// rule prompt from `book_rules/` and then forwards to the AI tab.

function startClassAutomation(opts = {}){
  const mode = String(opts?.mode || "send").toLowerCase();
  const fireDetect = () => {
    if (typeof runClassTextbookFlow === "function" && runClassTextbookFlow._inFlight) {
      try {
        globalThis.AT?.uiLog?.(
          "class_flow_skip_duplicate_trigger",
          "Class: flow already running, skip duplicate trigger",
          { source: "startClassAutomation", mode }
        );
      } catch (_) {
        // ignore
      }
      return;
    }
    runClassTextbookFlow({ mode, source: "startClassAutomation" }).catch(err => {
      console.warn("runClassTextbookFlow() failed:", err);
    });
  };

  if (startClassAutomation._started) {
    console.log("startClassAutomation() already started; re-triggering detectTextbookType().");
    try {
      globalThis.AT?.uiLog?.("class_flow_restart", "Class: re-triggering flow", {});
    } catch (_) {
      // ignore
    }
    try {
      if (typeof detectTextbookType === "function") {
        // If iframe doc is ready, fire immediately. Otherwise wait and then fire.
        if (typeof textbookFrameDocument !== "undefined" && textbookFrameDocument?.body) {
          setTimeout(fireDetect, 500);
        } else {
          document.addEventListener("iframeloaded", () => {
            setTimeout(fireDetect, 500);
          }, { once: true });
          waitForTextbookIframe("body");
        }
      }
    } catch (err) {
      console.warn("startClassAutomation restart failed:", err);
    }
    return;
  }
  startClassAutomation._started = true;

  // TEMPORARY: Delay class automation start so NativeCamp can fully render the class UI/iframe.
  // Remove once we have a reliable "class page ready" signal.
  setTimeout(() => {
    try {
      globalThis.AT?.uiLog?.("class_flow_start", "Class: starting flow", { url: window.location.href, mode });
    } catch (_) {
      // ignore
    }
    try {
      acceptAllCheckboxes();
    } catch (err) {
      console.warn("acceptAllCheckboxes() failed:", err);
    }

    // Listen before kicking the iframe polling to avoid missing a fast iframeloaded dispatch.
    document.addEventListener("iframeloaded", () => {
      console.log("Iframe is ready globally!");
      setTimeout(fireDetect, 500);
    }, { once: true });

    try {
      waitForTextbookIframe("body");
    } catch (err) {
      console.warn("waitForTextbookIframe() failed:", err);
    }
  }, 2000);
}
