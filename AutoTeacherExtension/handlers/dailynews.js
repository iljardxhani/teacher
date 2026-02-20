// Textbook handler: daily_news
// Called by class.js via: window[textbookTypeName]() where textbookTypeName === "daily_news".
//
// Responsibilities:
// 1) Scrape readable text (no tags) for the current textbook/article.
// 2) (mode: "prepare") Cache the scraped text locally without sending anything.
// 3) (mode: "send") Send a *special* package to the local router. The router expands it into:
//    - rule prompt (no_return_expected)
//    - textbook content (no_return_expected)
//    - kickoff prompt (reply expected; forwarded to teacher)
//    sent to the AI tab in the correct order with a small buffer.

(function registerDailyNewsHandler() {
  const ROUTER_URL = "http://127.0.0.1:5000";
  const BOOK_TYPE = "daily_news";
  const AUTO_POLL_INTERVAL_MS = 500;
  const AUTO_MAX_ATTEMPTS = 120; // ~60s
  const AUTO_SEND_DELAY_MS = 1000;
  const DEFAULT_ONGOING_TIMEOUT_MS = 10000;
  const DEFAULT_SCRAPE_TIMEOUT_MS = 20000;
  const HARDCODED_ARTICLE_SCRAPE_DELAY_MS = 5000;
  const MIN_DAILYNEWS_TEXT_CHARS = 400;
  const MIN_DAILYNEWS_TEXT_WORDS = 60;
  const SENT_KEY_PREFIX = "at_dailynews_sent_v3";

  let dailyNewsSendInFlight = false;
  let autoWatcherStarted = false;
  let autoWatcherInFlight = false;

  function getPreparedStore() {
    // Stored in the content-script world, shared across this page's content scripts.
    try {
      if (globalThis.AT) {
        globalThis.AT.preparedLessons = globalThis.AT.preparedLessons || {};
        return globalThis.AT.preparedLessons;
      }
    } catch (_) {
      // ignore
    }
    globalThis.__AT_PREPARED_LESSONS__ = globalThis.__AT_PREPARED_LESSONS__ || {};
    return globalThis.__AT_PREPARED_LESSONS__;
  }

  function getPreparedLesson(bookType) {
    try {
      return getPreparedStore()?.[bookType] || null;
    } catch (_) {
      return null;
    }
  }

  function setPreparedLesson(bookType, prepared) {
    try {
      const store = getPreparedStore();
      store[bookType] = prepared;
      return true;
    } catch (_) {
      return false;
    }
  }

  function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
  }

  function makeId() {
    if (typeof generateRouterMessageId === "function") return generateRouterMessageId();
    return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }

  function cleanReadableText(raw = "") {
    return String(raw)
      .replace(/[ \t]+/g, " ")       // collapse spaces/tabs
      .split("\n")                   // line-based cleanup
      .map(line => line.trim())
      .filter(line => line.length > 0)
      .join("\n");
  }

  function getQueryParamFromUrlLike(rawUrl, key) {
    const safeKey = String(key || "").trim();
    if (!safeKey) return null;
    const src = String(rawUrl || "").trim();
    if (!src) return null;

    try {
      const url = new URL(src, window.location.href);
      const v = url.searchParams.get(safeKey);
      if (v != null && String(v).trim() !== "") return String(v).trim();
    } catch (_) {
      // ignore
    }

    try {
      const m = src.match(new RegExp(`[?&#]${safeKey}=([^&#]+)`, "i"));
      if (m?.[1]) return decodeURIComponent(m[1]).trim();
    } catch (_) {
      // ignore
    }

    return null;
  }

  function makeShortHash(raw = "") {
    const text = String(raw || "");
    if (!text) return null;
    let hash = 2166136261;
    for (let i = 0; i < text.length; i += 1) {
      hash ^= text.charCodeAt(i);
      hash = Math.imul(hash, 16777619);
    }
    return (hash >>> 0).toString(36);
  }

  function getFlowRunId(meta = {}, ctx = {}) {
    const byCtx = ctx?.flow_run_id || ctx?.run_id || null;
    if (byCtx) return String(byCtx);
    const byMeta = meta?.flow_run_id || meta?.run_id || meta?.runId || null;
    if (byMeta) return String(byMeta);
    try {
      const run = globalThis.AT?.getRun?.() || null;
      if (run?.id) return String(run.id);
    } catch (_) {
      // ignore
    }
    return "no-run";
  }

  function getDateKey(meta = {}) {
    const orderFlag = String(meta?.order_flag || meta?.orderFlag || "").trim();
    if (/^\d{8}$/.test(orderFlag)) return orderFlag;
    try {
      return new Date().toISOString().slice(0, 10);
    } catch (_) {
      return "unknown-date";
    }
  }

  function makeSentKey(bookType, meta = {}, ctx = {}, textbookText = "") {
    const runId = getFlowRunId(meta, ctx);
    const bt = String(bookType || BOOK_TYPE || "unknown_book");

    const connectId =
      String(
        ctx?.connect_id ||
        ctx?.connectId ||
        meta?.connect_id ||
        meta?.connectId ||
        getQueryParamFromUrlLike(meta?.iframe_src || meta?.iframeSrc || "", "connect_id") ||
        ""
      ).trim();

    const textbookId =
      String(
        ctx?.textbook_id ||
        ctx?.textbookId ||
        meta?.textbook_id ||
        meta?.textbookId ||
        getQueryParamFromUrlLike(meta?.iframe_src || meta?.iframeSrc || "", "textbook_id") ||
        ""
      ).trim();

    const chapterId =
      String(
        ctx?.chapter_id ||
        ctx?.chapterId ||
        meta?.chapter_id ||
        meta?.chapterId ||
        getQueryParamFromUrlLike(meta?.iframe_src || meta?.iframeSrc || "", "chapter_id") ||
        ""
      ).trim();

    const orderFlag = String(meta?.order_flag || meta?.orderFlag || "").trim();
    const textHash = makeShortHash(String(textbookText || "").slice(0, 2000));
    const dateKey = getDateKey(meta);

    const lessonParts = [];
    if (connectId) lessonParts.push(`connect:${connectId}`);
    if (textbookId) lessonParts.push(`textbook:${textbookId}`);
    if (chapterId) lessonParts.push(`chapter:${chapterId}`);
    if (orderFlag) lessonParts.push(`order:${orderFlag}`);
    if (textHash) lessonParts.push(`text:${textHash}`);
    if (lessonParts.length === 0) lessonParts.push(`date:${dateKey}`);

    const key = `${SENT_KEY_PREFIX}:run:${runId}:book:${bt}:${lessonParts.join(":")}`;
    try {
      console.log("[daily_news] makeSentKey", {
        key,
        run_id: runId,
        connect_id: connectId || null,
        textbook_id: textbookId || null,
        chapter_id: chapterId || null,
        order_flag: orderFlag || null,
        text_hash: textHash || null
      });
    } catch (_) {
      // ignore
    }
    return key;
  }

  function sameLessonMeta(currentMeta = {}, preparedMeta = {}) {
    const curConnect = String(
      currentMeta?.connect_id ||
      currentMeta?.connectId ||
      getQueryParamFromUrlLike(currentMeta?.iframe_src || currentMeta?.iframeSrc || "", "connect_id") ||
      ""
    ).trim();
    const oldConnect = String(
      preparedMeta?.connect_id ||
      preparedMeta?.connectId ||
      getQueryParamFromUrlLike(preparedMeta?.iframe_src || preparedMeta?.iframeSrc || "", "connect_id") ||
      ""
    ).trim();
    if (curConnect && oldConnect) return curConnect === oldConnect;

    const curTextbook = String(
      currentMeta?.textbook_id ||
      currentMeta?.textbookId ||
      getQueryParamFromUrlLike(currentMeta?.iframe_src || currentMeta?.iframeSrc || "", "textbook_id") ||
      ""
    ).trim();
    const oldTextbook = String(
      preparedMeta?.textbook_id ||
      preparedMeta?.textbookId ||
      getQueryParamFromUrlLike(preparedMeta?.iframe_src || preparedMeta?.iframeSrc || "", "textbook_id") ||
      ""
    ).trim();
    if (curTextbook && oldTextbook) return curTextbook === oldTextbook;

    const curChapter = String(
      currentMeta?.chapter_id ||
      currentMeta?.chapterId ||
      getQueryParamFromUrlLike(currentMeta?.iframe_src || currentMeta?.iframeSrc || "", "chapter_id") ||
      ""
    ).trim();
    const oldChapter = String(
      preparedMeta?.chapter_id ||
      preparedMeta?.chapterId ||
      getQueryParamFromUrlLike(preparedMeta?.iframe_src || preparedMeta?.iframeSrc || "", "chapter_id") ||
      ""
    ).trim();
    if (curChapter && oldChapter) return curChapter === oldChapter;

    const curOrder = String(currentMeta?.order_flag || currentMeta?.orderFlag || "").trim();
    const oldOrder = String(preparedMeta?.order_flag || preparedMeta?.orderFlag || "").trim();
    if (curOrder && oldOrder) return curOrder === oldOrder;

    return false;
  }

  function hasSentKey(sentKey, opts = {}) {
    const useLocal = opts?.useLocal !== false;
    if (!sentKey) return false;
    try {
      if (sessionStorage.getItem(sentKey)) return true;
    } catch (_) {
      // ignore
    }
    if (useLocal) {
      try {
        if (localStorage.getItem(sentKey)) return true;
      } catch (_) {
        // ignore
      }
    }
    return false;
  }

  function markSentKey(sentKey, payload = {}, opts = {}) {
    const useLocal = opts?.useLocal !== false;
    if (!sentKey) return false;
    const value = JSON.stringify({
      ts: Date.now(),
      payload
    });
    let ok = false;
    try {
      sessionStorage.setItem(sentKey, value);
      ok = true;
    } catch (_) {
      // ignore
    }
    if (useLocal) {
      try {
        localStorage.setItem(sentKey, value);
        ok = true;
      } catch (_) {
        // ignore
      }
    }
    return ok;
  }

  function isNativeCampClassPage() {
    try {
      const host = String(window.location.hostname || "").toLowerCase();
      if (!host.includes("nativecamp.net")) return false;
      if (typeof detectPageRole === "function") return detectPageRole() === "class";
      return true;
    } catch (_) {
      return false;
    }
  }

  function isLessonOngoingSignalPresent() {
    // Primary signal from class DOM state.
    if (document.querySelector("#chat_area_table.in-lesson, .chat_area_table.in-lesson")) {
      return { ok: true, signal: "chat_area_table.in-lesson" };
    }

    // Fallback text signal.
    const textTargets = [
      "#lesson_connection_state",
      "#lesson_connecting",
      "#textchat_area",
      "#chat_area_table"
    ];
    for (const sel of textTargets) {
      const text = String(document.querySelector(sel)?.innerText || "").toLowerCase();
      if (text.includes("ongoing lesson")) {
        return { ok: true, signal: `${sel}:ongoing_lesson_text` };
      }
    }

    return { ok: false, signal: null };
  }

  function inferBookTypeFromIframe(iframe) {
    if (!iframe) return "";
    const attrType = String(iframe.getAttribute("html-directory") || "").trim().toLowerCase();
    if (attrType) return attrType;

    const srcRaw = String(iframe.getAttribute("src") || iframe.src || "");
    if (!srcRaw) return "";
    try {
      const url = new URL(srcRaw, window.location.href);
      const qpType = String(
        url.searchParams.get("html_dir") ||
        url.searchParams.get("class_id") ||
        ""
      ).trim().toLowerCase();
      if (qpType) return qpType;
    } catch (_) {
      // ignore
    }
    try {
      const m = srcRaw.match(/[?&#](?:html_dir|class_id)=([^&#]+)/i);
      if (m?.[1]) return decodeURIComponent(m[1]).trim().toLowerCase();
    } catch (_) {
      // ignore
    }
    return "";
  }

  function getDailyNewsArticleState() {
    const iframe = document.querySelector("#textbook-iframe");
    if (!iframe) return { ok: false, reason: "iframe_not_found" };

    const htmlDirectoryAttr = iframe.getAttribute("html-directory") || null;
    const inferredBookType = inferBookTypeFromIframe(iframe) || null;
    if (inferredBookType !== BOOK_TYPE) {
      return {
        ok: false,
        reason: "book_type_not_ready",
        html_directory: htmlDirectoryAttr,
        inferred_book_type: inferredBookType
      };
    }

    let doc = null;
    try {
      if (typeof textbookFrameDocument !== "undefined" && textbookFrameDocument?.body) {
        doc = textbookFrameDocument;
      }
    } catch (_) {
      // ignore
    }
    if (!doc) {
      try {
        doc = iframe.contentDocument || iframe.contentWindow?.document || null;
      } catch (_) {
        doc = null;
      }
    }
    const docReady = Boolean(doc?.body);
    const article = docReady ? doc.querySelector("article") : null;
    const text = docReady ? cleanReadableText(article?.innerText || doc.body?.innerText || "") : "";

    return {
      ok: true,
      html_directory: htmlDirectoryAttr,
      inferred_book_type: inferredBookType,
      order_flag: iframe.getAttribute("order-flag") || null,
      iframe_src: iframe.getAttribute("src") || iframe.src || null,
      doc_ready: docReady,
      article_present: Boolean(article),
      text_len: text.length
    };
  }

  async function waitForOngoingLesson({ timeoutMs = DEFAULT_ONGOING_TIMEOUT_MS, intervalMs = AUTO_POLL_INTERVAL_MS } = {}) {
    const startedAt = Date.now();
    let attempts = 0;
    while (Date.now() - startedAt < timeoutMs) {
      attempts += 1;
      const ongoing = isLessonOngoingSignalPresent();
      if (ongoing.ok) {
        return { ok: true, attempts, signal: ongoing.signal };
      }
      await sleep(intervalMs);
    }
    return { ok: false, attempts, error: "ongoing_lesson_timeout" };
  }

  async function waitForTextbookDocument(timeoutMs = DEFAULT_SCRAPE_TIMEOUT_MS) {
    const startedAt = Date.now();
    while (Date.now() - startedAt < timeoutMs) {
      const resolved = resolveLiveTextbookDocument();
      if (resolved?.doc) return resolved.doc;

      await sleep(250);
    }
    return null;
  }

  function resolveLiveTextbookDocument() {
    const out = {
      iframe: null,
      doc: null,
      iframe_src: null,
      doc_href: null,
      reason: null
    };

    const iframe = document.querySelector("#textbook-iframe");
    out.iframe = iframe || null;
    if (!iframe) {
      out.reason = "iframe_not_found";
      return out;
    }

    out.iframe_src = iframe.getAttribute("src") || iframe.src || null;

    let doc = null;
    try {
      if (typeof textbookFrameDocument !== "undefined" && textbookFrameDocument?.body) {
        doc = textbookFrameDocument;
      }
    } catch (_) {
      // ignore
    }

    try {
      const directDoc = iframe.contentDocument || iframe.contentWindow?.document || null;
      if (directDoc?.body) doc = directDoc;
    } catch (_) {
      // ignore
    }

    if (!doc?.body) {
      out.reason = "doc_not_ready";
      return out;
    }

    let docHref = null;
    try {
      docHref = String(doc.location?.href || "").trim() || null;
    } catch (_) {
      docHref = null;
    }
    out.doc_href = docHref;

    const iframeSrc = String(out.iframe_src || "");
    const expectsTextbookDoc = /\/HtmlTextbook\//i.test(iframeSrc) || /[?&#](?:html_dir|class_id)=/i.test(iframeSrc);
    const isAboutBlankDoc = docHref === "about:blank";
    if (expectsTextbookDoc && isAboutBlankDoc) {
      out.reason = "doc_about_blank";
      return out;
    }
    if (expectsTextbookDoc && docHref && !/\/HtmlTextbook\//i.test(String(docHref))) {
      out.reason = "doc_not_textbook_page";
      return out;
    }
    if (expectsTextbookDoc && /\/teacher\/lesson-tutorial/i.test(String(docHref || ""))) {
      out.reason = "doc_not_textbook_page";
      return out;
    }

    out.doc = doc;
    out.reason = "ok";
    return out;
  }

  function countWords(text = "") {
    return String(text || "").trim().split(/\s+/).filter(Boolean).length;
  }

  function isTextLikelyDailyNewsLesson(text = "") {
    const safe = cleanReadableText(text);
    if (!safe) return false;
    if (safe.length < MIN_DAILYNEWS_TEXT_CHARS) return false;
    if (countWords(safe) < MIN_DAILYNEWS_TEXT_WORDS) return false;
    return true;
  }

  function getRichNodeText(node) {
    if (!node) return "";
    const visibleText = cleanReadableText(node.innerText || "");
    let fullText = "";
    try {
      const clone = node.cloneNode(true);
      clone.querySelectorAll("script,style,noscript,svg,canvas").forEach(el => el.remove());
      fullText = cleanReadableText(clone.textContent || "");
    } catch (_) {
      fullText = visibleText;
    }
    return fullText.length > visibleText.length ? fullText : visibleText;
  }

  function extractBestTextFromDoc(doc, opts = {}) {
    if (!doc?.body) return { text: "", selector: null, len: 0, words: 0, candidates: [] };

    const includeBodyFallback = opts?.includeBodyFallback === true;
    const selectors = [
      "article",
      "#article",
      ".article",
      "[id*='article']",
      "[class*='article']",
      "main article",
      "#content article",
      "main",
      "#content",
      ".content"
    ];
    if (includeBodyFallback) selectors.push("body");

    const candidates = [];
    for (const sel of selectors) {
      try {
        const nodes = sel === "body"
          ? [doc.body]
          : Array.from(doc.querySelectorAll(sel));
        if (!nodes || nodes.length === 0) continue;

        for (let i = 0; i < nodes.length; i += 1) {
          const node = nodes[i];
          const text = getRichNodeText(node);
          if (!text) continue;
          candidates.push({
            selector: `${sel}[${i}]`,
            text,
            len: text.length,
            words: countWords(text)
          });
        }
      } catch (_) {
        // ignore
      }
    }

    if (candidates.length === 0) {
      return { text: "", selector: null, len: 0, words: 0, candidates: [] };
    }

    // If body is available in the same textbook document, prefer it because Daily News
    // often splits lesson parts across multiple sections and "body" preserves full flow.
    const bodyCandidate = candidates.find(c => String(c.selector || "").startsWith("body["));
    if (bodyCandidate) {
      return {
        text: bodyCandidate.text,
        selector: bodyCandidate.selector,
        len: bodyCandidate.len,
        words: bodyCandidate.words,
        candidates
      };
    }

    candidates.sort((a, b) => b.len - a.len);
    const best = candidates[0];
    return {
      text: best.text,
      selector: best.selector,
      len: best.len,
      words: best.words,
      candidates
    };
  }

  function scrapeArticleDirectOnce() {
    const docs = [];
    try {
      if (typeof textbookFrameDocument !== "undefined" && textbookFrameDocument?.body) {
        docs.push(textbookFrameDocument);
      }
    } catch (_) {
      // ignore
    }
    try {
      const iframe = document.querySelector("#textbook-iframe");
      const directDoc = iframe?.contentDocument || iframe?.contentWindow?.document || null;
      if (directDoc?.body) docs.push(directDoc);
    } catch (_) {
      // ignore
    }

    // Preserve insertion order while deduping.
    const seen = new Set();
    const uniqueDocs = docs.filter(doc => {
      if (!doc) return false;
      if (seen.has(doc)) return false;
      seen.add(doc);
      return true;
    });

    for (const doc of uniqueDocs) {
      try {
        const extracted = extractBestTextFromDoc(doc, { includeBodyFallback: true });
        if (!extracted?.text) continue;
        if (!isTextLikelyDailyNewsLesson(extracted.text)) {
          try {
            console.log("[daily_news] hardcoded_5s_candidate_too_short", {
              selector: extracted.selector,
              text_len: extracted.len,
              words: extracted.words
            });
          } catch (_) {
            // ignore
          }
          continue;
        }
        let docHref = null;
        try {
          docHref = String(doc.location?.href || "").trim() || null;
        } catch (_) {
          docHref = null;
        }
        return {
          ok: true,
          text: extracted.text,
          selector: extracted.selector,
          text_len: extracted.len,
          words: extracted.words,
          doc_href: docHref
        };
      } catch (_) {
        // ignore
      }
    }

    return { ok: false, error: "article_not_readable_after_5s" };
  }

  async function scrapeDailyNewsText(timeoutMs = DEFAULT_SCRAPE_TIMEOUT_MS) {
    const safeTimeoutMs = Math.max(1000, Number(timeoutMs) || DEFAULT_SCRAPE_TIMEOUT_MS);
    try {
      console.log("[daily_news] scrape_start", {
        timeout_ms: safeTimeoutMs,
        hardcoded_delay_ms: HARDCODED_ARTICLE_SCRAPE_DELAY_MS,
        min_chars: MIN_DAILYNEWS_TEXT_CHARS,
        min_words: MIN_DAILYNEWS_TEXT_WORDS
      });
    } catch (_) {
      // ignore
    }

    // Hardcoded fallback requested: after 5 seconds, attempt a direct article scrape
    // regardless of regular readiness checks.
    const hardcodedDelayMs = Math.min(HARDCODED_ARTICLE_SCRAPE_DELAY_MS, safeTimeoutMs);
    if (hardcodedDelayMs > 0) await sleep(hardcodedDelayMs);
    const hardcoded = scrapeArticleDirectOnce();
    if (hardcoded.ok) {
      try {
        globalThis.AT?.uiLog?.(
          "daily_news_scrape_hardcoded_after_5s",
          "Daily News scraped via hardcoded 5s article fallback",
          {
            text_len: hardcoded.text.length,
            selector: hardcoded.selector || null,
            words: hardcoded.words || null,
            doc_href: hardcoded.doc_href || null
          }
        );
      } catch (_) {
        // ignore
      }
      try {
        console.log("[daily_news] hardcoded_5s_success", {
          text_len: hardcoded.text_len,
          words: hardcoded.words,
          selector: hardcoded.selector,
          doc_href: hardcoded.doc_href || null
        });
      } catch (_) {
        // ignore
      }
      return { ok: true, text: hardcoded.text, via: "hardcoded_5s_article" };
    }

    const doc = await waitForTextbookDocument(Math.min(safeTimeoutMs, 10000));
    if (!doc) {
      const unresolved = resolveLiveTextbookDocument();
      return {
        ok: false,
        error: "textbook_doc_not_ready",
        hardcoded_error: hardcoded.error || null,
        reason: unresolved?.reason || null,
        iframe_src: unresolved?.iframe_src || null,
        doc_href: unresolved?.doc_href || null
      };
    }

    const startedAt = Date.now();
    let lastState = null;
    let bestSeen = { len: 0, words: 0, selector: null, sample: null };
    let probeCount = 0;
    while (Date.now() - startedAt < safeTimeoutMs) {
      probeCount += 1;
      const live = resolveLiveTextbookDocument();
      const activeDoc = live?.doc || doc;
      const extracted = extractBestTextFromDoc(activeDoc, { includeBodyFallback: true });
      const text = extracted?.text || "";

      if (extracted?.len > bestSeen.len) {
        bestSeen = {
          len: extracted.len,
          words: extracted.words,
          selector: extracted.selector,
          sample: String(text || "").slice(0, 120)
        };
      }

      if (text && isTextLikelyDailyNewsLesson(text)) {
        try {
          console.log("[daily_news] scrape_success", {
            probe_count: probeCount,
            text_len: extracted.len,
            words: extracted.words,
            selector: extracted.selector,
            doc_href: live?.doc_href || null
          });
        } catch (_) {
          // ignore
        }
        return { ok: true, text };
      }

      if (text && !isTextLikelyDailyNewsLesson(text) && (probeCount === 1 || probeCount % 8 === 0)) {
        try {
          console.log("[daily_news] scrape_probe_short_text", {
            probe_count: probeCount,
            text_len: extracted.len,
            words: extracted.words,
            selector: extracted.selector,
            reason: live?.reason || null,
            doc_href: live?.doc_href || null,
            iframe_src: live?.iframe_src || null,
            sample: String(text || "").slice(0, 120)
          });
        } catch (_) {
          // ignore
        }
      }

      if (!text && (probeCount === 1 || probeCount % 8 === 0)) {
        try {
          console.log("[daily_news] scrape_probe_empty", {
            probe_count: probeCount,
            reason: live?.reason || null,
            doc_href: live?.doc_href || null,
            iframe_src: live?.iframe_src || null
          });
        } catch (_) {
          // ignore
        }
      }

      lastState = {
        reason: live?.reason || null,
        iframe_src: live?.iframe_src || null,
        doc_href: live?.doc_href || null,
        best_seen_len: bestSeen.len,
        best_seen_words: bestSeen.words,
        best_seen_selector: bestSeen.selector,
        best_seen_sample: bestSeen.sample
      };
      await sleep(250);
    }

    try {
      console.warn("[daily_news] scrape_timeout", {
        timeout_ms: safeTimeoutMs,
        probes: probeCount,
        best_seen: bestSeen,
        last_state: lastState
      });
    } catch (_) {
      // ignore
    }

    return {
      ok: false,
      error: "empty_textbook_text",
      ...lastState
    };
  }

  async function sendLessonPackage({ bookType, textbookText, meta = {} }) {
    const sender = (() => {
      // Avoid referencing `pageRole` here because it is declared with `let` in content.js
      // (loaded later) and can be in TDZ depending on how Chrome instantiates content scripts.
      try {
        if (typeof detectPageRole === "function") return detectPageRole();
      } catch (_) {
        // ignore
      }
      return "class";
    })();

    const payload = {
      id: makeId(),
      kind: "lesson_package",
      book_type: bookType,
      textbook_text: textbookText,
      meta,
      flags: {
        special: true
      }
    };

    try {
      globalThis.AT?.uiLog?.(
        "lesson_package_send_attempt",
        `Class -> Router: lesson_package (${bookType})`,
        { to: "ai", sender, payload_id: payload.id, book_type: bookType, text_len: textbookText?.length || 0 }
      );
    } catch (_) {
      // ignore
    }

    try {
      // Prefer proxying through the extension background service worker (more reliable than fetch
      // from some content-script contexts).
      if (typeof chrome !== "undefined" && chrome.runtime?.sendMessage) {
        const proxied = await new Promise(resolve => {
          try {
            chrome.runtime.sendMessage(
              { type: "router_send", from: sender, to: "ai", message: payload },
              response => {
                const err = chrome.runtime.lastError;
                if (err) {
                  const msg = err.message || String(err);
                  // This usually means the receiver didn't call sendResponse; the message may still
                  // have been handled. Treat it as success to avoid breaking the class flow.
                  if (msg.includes("The message port closed before a response was received")) {
                    return resolve({ ok: true, via: "background_proxy", ack: false });
                  }
                  return resolve({ ok: false, error: msg });
                }
                resolve(response || { ok: true });
              }
            );
          } catch (err) {
            resolve({ ok: false, error: String(err?.message || err) });
          }
        });

        if (!proxied?.ok) {
          console.warn("[daily_news] background proxy failed:", proxied);
          try {
            globalThis.AT?.uiLog?.(
              "lesson_package_send_failed",
              "Router send failed (background proxy)",
              { proxied, payload_id: payload.id, book_type: bookType },
              { level: "warn", ttlMs: 6500 }
            );
          } catch (_) {
            // ignore
          }
          return proxied;
        }

        try {
          globalThis.AT?.uiLog?.(
            "lesson_package_send_ok",
            "Router queued lesson package (background proxy)",
            { payload_id: payload.id, book_type: bookType }
          );
        } catch (_) {
          // ignore
        }

        // If we got a "port closed" error, we treat it as success but annotate that we didn't get
        // an ack back from the service worker.
        if (proxied?.ack === false) return { ok: true, via: "background_proxy", ack: false };
        return { ok: true, via: "background_proxy", ack: true };
      }

      const res = await fetch(`${ROUTER_URL}/send_message`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          from: sender,
          to: "ai",
          message: payload
        })
      });

      if (!res.ok) {
        let err = null;
        try {
          err = await res.json();
        } catch (_) {
          // ignore
        }
        console.warn("[daily_news] Failed to send lesson package:", res.status, err);
        try {
          globalThis.AT?.uiLog?.(
            "lesson_package_send_failed",
            `Router send failed (${res.status})`,
            { status: res.status, err, payload_id: payload.id, book_type: bookType },
            { level: "warn", ttlMs: 6500 }
          );
        } catch (_) {
          // ignore
        }
        return { ok: false, status: res.status, err };
      }

      let body = null;
      try {
        body = await res.json();
      } catch (_) {
        // ignore
      }
      try {
        globalThis.AT?.uiLog?.(
          "lesson_package_send_ok",
          "Router queued lesson package",
          { payload_id: payload.id, book_type: bookType, response: body }
        );
      } catch (_) {
        // ignore
      }
      return { ok: true };
    } catch (err) {
      console.warn("[daily_news] Error sending lesson package:", err);
      try {
        globalThis.AT?.uiLog?.(
          "lesson_package_send_error",
          "Router send error",
          { error: String(err?.message || err), payload_id: payload.id, book_type: bookType },
          { level: "error", ttlMs: 6500 }
        );
      } catch (_) {
        // ignore
      }
      return { ok: false, error: String(err?.message || err) };
    }
  }

  async function daily_news(ctx = {}) {
    console.log("[daily_news] handler fired", ctx);
    try {
      globalThis.AT?.uiLog?.("handler_daily_news_fired", "Handler: daily_news", { ctx });
    } catch (_) {
      // ignore
    }

    // Basic metadata for server-side routing / debugging.
    const iframe = document.querySelector("#textbook-iframe");
    const flowRun = (() => {
      try {
        return globalThis.AT?.getRun?.() || null;
      } catch (_) {
        return null;
      }
    })();
    const meta = {
      page_url: window.location.href,
      iframe_src: iframe?.getAttribute("src") || iframe?.src || null,
      html_directory: iframe?.getAttribute("html-directory") || null,
      order_flag: iframe?.getAttribute("order-flag") || null,
      connect_id:
        iframe?.getAttribute("connect-id") ||
        getQueryParamFromUrlLike(iframe?.getAttribute("src") || iframe?.src || null, "connect_id") ||
        null,
      chapter_id:
        iframe?.getAttribute("chapter-id") ||
        getQueryParamFromUrlLike(iframe?.getAttribute("src") || iframe?.src || null, "chapter_id") ||
        null,
      textbook_id:
        iframe?.getAttribute("textbook-id") ||
        getQueryParamFromUrlLike(iframe?.getAttribute("src") || iframe?.src || null, "textbook_id") ||
        null,
      flow_run_id: flowRun?.id || null
    };

    const mode = String(ctx?.mode || "send").toLowerCase();
    const scrapeTimeoutMs = Math.max(
      1000,
      Number(ctx?.scrape_timeout_ms ?? ctx?.scrapeTimeoutMs ?? DEFAULT_SCRAPE_TIMEOUT_MS) || DEFAULT_SCRAPE_TIMEOUT_MS
    );
    const prepared = getPreparedLesson(BOOK_TYPE);
    const canUsePrepared =
      ctx?.use_prepared !== false &&
      prepared &&
      typeof prepared?.textbookText === "string" &&
      prepared.textbookText.trim() !== "" &&
      sameLessonMeta(meta, prepared?.meta || {});

    if (mode === "prepare" || mode === "prep" || mode === "scrape") {
      const scraped = await scrapeDailyNewsText(scrapeTimeoutMs);
      if (!scraped.ok) {
        console.warn("[daily_news] scrape failed:", scraped);
        try {
          globalThis.AT?.uiLog?.(
            "daily_news_scrape_failed",
            "Daily News scrape failed",
            scraped,
            { level: "warn", ttlMs: 6500 }
          );
        } catch (_) {
          // ignore
        }
        return null;
      }

      console.log("[daily_news] scraped textbook text:\n", scraped.text);
      try {
        globalThis.AT?.uiLog?.("daily_news_scrape_ok", "Daily News scraped", { text_len: scraped.text.length });
      } catch (_) {
        // ignore
      }

      const preparedObj = {
        bookType: BOOK_TYPE,
        textbookText: scraped.text,
        meta,
        prepared_at_ts: Date.now()
      };
      setPreparedLesson(BOOK_TYPE, preparedObj);
      try {
        globalThis.AT?.uiLog?.(
          "lesson_prepared",
          "Lesson prepared (not sent)",
          { book_type: BOOK_TYPE, text_len: scraped.text.length, order_flag: meta.order_flag, flow_run_id: meta.flow_run_id }
        );
      } catch (_) {
        // ignore
      }
      return { ok: true, mode: "prepare", book_type: BOOK_TYPE, text_len: scraped.text.length };
    }

    if (dailyNewsSendInFlight) {
      try {
        globalThis.AT?.uiLog?.(
          "daily_news_send_in_flight",
          "Daily News send already in flight",
          { mode, ctx },
          { level: "warn", ttlMs: 3500 }
        );
      } catch (_) {
        // ignore
      }
      return { ok: false, error: "in_flight" };
    }
    dailyNewsSendInFlight = true;

    try {
      // Wait for actual lesson state (not just DOM loaded) before scraping/sending.
      const requireOngoing = ctx?.require_ongoing === true || ctx?.requireOngoing === true;
      if (requireOngoing) {
        const ongoingTimeoutMs = Math.max(
          1000,
          Number(ctx?.ongoing_timeout_ms ?? ctx?.ongoingTimeoutMs ?? DEFAULT_ONGOING_TIMEOUT_MS) || DEFAULT_ONGOING_TIMEOUT_MS
        );
        const ongoing = await waitForOngoingLesson({ timeoutMs: ongoingTimeoutMs });
        if (!ongoing.ok) {
          try {
            globalThis.AT?.uiLog?.(
              "daily_news_wait_ongoing_timeout",
              "Daily News: ongoing lesson signal not found (stopped)",
              { ongoing, ctx },
              { level: "warn", ttlMs: 6500 }
            );
          } catch (_) {
            // ignore
          }
          return { ok: false, error: "ongoing_lesson_timeout", ongoing };
        }
        try {
          globalThis.AT?.uiLog?.(
            "daily_news_wait_ongoing_ok",
            "Daily News: ongoing lesson confirmed",
            ongoing
          );
        } catch (_) {
          // ignore
        }
      }

      let textbookText = null;
      let preparedAtTs = null;
      let flowRunIdOverride = null;
      if (canUsePrepared) {
        textbookText = prepared.textbookText;
        preparedAtTs = prepared.prepared_at_ts || null;
        flowRunIdOverride = prepared?.meta?.flow_run_id || null;
        try {
          globalThis.AT?.uiLog?.(
            "lesson_send_using_cached",
            "Sending cached lesson",
            { book_type: BOOK_TYPE, order_flag: meta.order_flag, prepared_at_ts: preparedAtTs, flow_run_id: flowRunIdOverride }
          );
        } catch (_) {
          // ignore
        }
      } else {
        const scraped = await scrapeDailyNewsText(scrapeTimeoutMs);
        if (!scraped.ok) {
          console.warn("[daily_news] scrape failed:", scraped);
          try {
            globalThis.AT?.uiLog?.(
              "daily_news_scrape_failed",
              "Daily News scrape failed",
              scraped,
              { level: "warn", ttlMs: 6500 }
            );
          } catch (_) {
            // ignore
          }
          return null;
        }

        textbookText = scraped.text;
        console.log("[daily_news] scraped textbook text:\n", textbookText);
        try {
          globalThis.AT?.uiLog?.("daily_news_scrape_ok", "Daily News scraped", { text_len: textbookText.length });
        } catch (_) {
          // ignore
        }

        // Update cache so a second "send" doesn't need to scrape again.
        setPreparedLesson(BOOK_TYPE, {
          bookType: BOOK_TYPE,
          textbookText,
          meta,
          prepared_at_ts: Date.now()
        });
      }

      const metaToSend = {
        ...meta,
        // Keep a stable run id if we prepared first.
        flow_run_id: flowRunIdOverride || meta.flow_run_id || null,
        prepared_at_ts: preparedAtTs || undefined
      };

      const sentKey = makeSentKey(BOOK_TYPE, metaToSend, ctx, textbookText);
      const runIdForDedupe = getFlowRunId(metaToSend, ctx);
      const isNoRun = runIdForDedupe === "no-run";
      const isManualTrigger = ctx?.source === "popup_button" || ctx?.manual_trigger === true;
      const shouldForceSend = ctx?.force_send === true || ctx?.forceSend === true;
      const dedupeEnabled = !shouldForceSend && !isManualTrigger && ctx?.disable_dedupe !== true;
      const useLocalDedupe = !isNoRun;
      const duplicateSeen = dedupeEnabled ? hasSentKey(sentKey, { useLocal: useLocalDedupe }) : false;

      try {
        console.log("[daily_news] dedupe_check", {
          source: ctx?.source || null,
          dedupe_enabled: dedupeEnabled,
          force_send: shouldForceSend,
          is_manual_trigger: isManualTrigger,
          run_id: runIdForDedupe,
          is_no_run: isNoRun,
          use_local_dedupe: useLocalDedupe,
          duplicate_seen: duplicateSeen,
          sent_key: sentKey,
          connect_id: metaToSend.connect_id || null,
          chapter_id: metaToSend.chapter_id || null,
          order_flag: metaToSend.order_flag || null
        });
      } catch (_) {
        // ignore
      }

      if (duplicateSeen) {
        console.warn("[daily_news] duplicate send skipped", {
          sent_key: sentKey,
          source: ctx?.source || null,
          run_id: runIdForDedupe,
          connect_id: metaToSend.connect_id || null,
          chapter_id: metaToSend.chapter_id || null,
          order_flag: metaToSend.order_flag || null
        });
        try {
          globalThis.AT?.uiLog?.(
            "daily_news_send_skipped_duplicate",
            "Daily News already sent for this run/lesson",
            {
              sent_key: sentKey,
              flow_run_id: metaToSend.flow_run_id || null,
              connect_id: metaToSend.connect_id || null,
              chapter_id: metaToSend.chapter_id || null,
              order_flag: metaToSend.order_flag || null
            }
          );
        } catch (_) {
          // ignore
        }
        return { ok: true, skipped: true, reason: "already_sent", sent_key: sentKey };
      }

      if (!dedupeEnabled) {
        try {
          globalThis.AT?.uiLog?.(
            "daily_news_send_dedupe_bypassed",
            "Daily News duplicate check bypassed",
            {
              source: ctx?.source || null,
              force_send: shouldForceSend,
              manual_trigger: isManualTrigger,
              disable_dedupe: ctx?.disable_dedupe === true
            }
          );
        } catch (_) {
          // ignore
        }
      }

      const postDetectDelayMs = Math.max(
        0,
        Number(ctx?.post_detect_delay_ms ?? ctx?.postDetectDelayMs ?? 500) || 0
      );
      if (postDetectDelayMs > 0) {
        try {
          globalThis.AT?.uiLog?.(
            "daily_news_send_delay",
            `Daily News: waiting ${postDetectDelayMs}ms before send`,
            { post_detect_delay_ms: postDetectDelayMs }
          );
        } catch (_) {
          // ignore
        }
        await sleep(postDetectDelayMs);
      }

      const sent = await sendLessonPackage({
        bookType: BOOK_TYPE,
        textbookText,
        meta: metaToSend
      });

      if (sent.ok && sent?.ack !== false) {
        markSentKey(sentKey, {
          flow_run_id: metaToSend.flow_run_id || null,
          order_flag: metaToSend.order_flag || null,
          book_type: BOOK_TYPE
        }, { useLocal: useLocalDedupe });
        console.log("[daily_news] duplicate lock set", {
          sent_key: sentKey,
          use_local_dedupe: useLocalDedupe,
          run_id: runIdForDedupe
        });
        console.log("[daily_news] lesson package queued for AI (server will inject rule prompt + content).");
      } else if (sent.ok && sent?.ack === false) {
        try {
          globalThis.AT?.uiLog?.(
            "lesson_package_send_unconfirmed",
            "Router send unconfirmed; duplicate lock not set",
            { sent_key: sentKey, via: sent?.via || null },
            { level: "warn", ttlMs: 6500 }
          );
        } catch (_) {
          // ignore
        }
      }

      return sent;
    } finally {
      dailyNewsSendInFlight = false;
    }
  }

  async function runDailyNewsAutoWatcher() {
    if (autoWatcherInFlight) return { ok: false, error: "in_flight" };
    autoWatcherInFlight = true;
    try {
      let dailyNewsDetectedAtTs = null;

      try {
        globalThis.AT?.uiLog?.(
          "daily_news_auto_start",
          "Daily News auto watcher started",
          {
            poll_interval_ms: AUTO_POLL_INTERVAL_MS,
            max_attempts: AUTO_MAX_ATTEMPTS
          }
        );
      } catch (_) {
        // ignore
      }

      for (let attempt = 1; attempt <= AUTO_MAX_ATTEMPTS; attempt += 1) {
        const ongoing = isLessonOngoingSignalPresent();
        const articleState = getDailyNewsArticleState();
        if (!articleState.ok) {
          dailyNewsDetectedAtTs = null;
          if (attempt === 1 || attempt % 10 === 0) {
            try {
              globalThis.AT?.uiLog?.(
                "daily_news_auto_wait_lesson",
                "Daily News auto: waiting for daily_news lesson",
                {
                  attempt,
                  max_attempts: AUTO_MAX_ATTEMPTS,
                  ongoing_signal: ongoing.signal || null,
                  article_state: articleState
                }
              );
            } catch (_) {
              // ignore
            }
          }
          await sleep(AUTO_POLL_INTERVAL_MS);
          continue;
        }

        if (!dailyNewsDetectedAtTs) dailyNewsDetectedAtTs = Date.now();
        const sinceDetectedMs = Date.now() - dailyNewsDetectedAtTs;

        const ongoingReady = ongoing.ok || sinceDetectedMs >= 10000;
        if (!ongoingReady) {
          if (attempt === 1 || attempt % 10 === 0) {
            try {
              globalThis.AT?.uiLog?.(
                "daily_news_auto_wait_ongoing",
                "Daily News auto: waiting for ongoing lesson (fallback at 10s)",
                {
                  attempt,
                  max_attempts: AUTO_MAX_ATTEMPTS,
                  since_detected_ms: sinceDetectedMs
                }
              );
            } catch (_) {
              // ignore
            }
          }
          await sleep(AUTO_POLL_INTERVAL_MS);
          continue;
        }

        const lightlyReady = articleState.doc_ready === true || sinceDetectedMs >= 10000;
        if (!lightlyReady) {
          if (attempt === 1 || attempt % 10 === 0) {
            try {
              globalThis.AT?.uiLog?.(
                "daily_news_auto_wait_light_ready",
                "Daily News auto: waiting for light readiness",
                {
                  attempt,
                  max_attempts: AUTO_MAX_ATTEMPTS,
                  since_detected_ms: sinceDetectedMs,
                  article_state: articleState
                }
              );
            } catch (_) {
              // ignore
            }
          }
          await sleep(AUTO_POLL_INTERVAL_MS);
          continue;
        }

        const iframe = document.querySelector("#textbook-iframe");
        const autoMeta = {
          flow_run_id: getFlowRunId({}, {}),
          order_flag: iframe?.getAttribute("order-flag") || articleState.order_flag || null,
          connect_id:
            iframe?.getAttribute("connect-id") ||
            getQueryParamFromUrlLike(iframe?.getAttribute("src") || iframe?.src || null, "connect_id") ||
            null,
          chapter_id:
            iframe?.getAttribute("chapter-id") ||
            getQueryParamFromUrlLike(iframe?.getAttribute("src") || iframe?.src || null, "chapter_id") ||
            null
        };
        const sentKey = makeSentKey(BOOK_TYPE, autoMeta, {});
        const autoRunId = getFlowRunId(autoMeta, {});
        const autoUseLocalDedupe = autoRunId !== "no-run";
        const autoDuplicateSeen = hasSentKey(sentKey, { useLocal: autoUseLocalDedupe });
        try {
          console.log("[daily_news] auto_dedupe_check", {
            sent_key: sentKey,
            run_id: autoRunId,
            use_local_dedupe: autoUseLocalDedupe,
            duplicate_seen: autoDuplicateSeen,
            attempt
          });
        } catch (_) {
          // ignore
        }
        if (autoDuplicateSeen) {
          try {
            globalThis.AT?.uiLog?.(
              "daily_news_auto_already_sent",
              "Daily News auto: already sent for this run/lesson",
              { sent_key: sentKey, attempt, article_state: articleState }
            );
          } catch (_) {
            // ignore
          }
          return { ok: true, skipped: true, reason: "already_sent", sent_key: sentKey };
        }

        try {
          globalThis.AT?.uiLog?.(
            "daily_news_auto_trigger_send",
            "Daily News auto: triggering detect + scrape + send",
            {
              attempt,
              ongoing_signal: ongoing.signal || (ongoingReady ? "fallback_10s" : null),
              article_state: articleState,
              sent_key: sentKey
            }
          );
        } catch (_) {
          // ignore
        }

        // Shared core path (same used by popup button): detectTextbook() -> handler scrape/send.
        const runFlow = typeof runClassTextbookFlow === "function"
          ? runClassTextbookFlow
          : null;
        if (!runFlow) {
          try {
            globalThis.AT?.uiLog?.(
              "daily_news_auto_missing_shared_flow",
              "Daily News auto: runClassTextbookFlow() missing",
              {},
              { level: "error", ttlMs: 6500 }
            );
          } catch (_) {
            // ignore
          }
          return { ok: false, error: "runClassTextbookFlow_missing" };
        }

        const result = await runFlow({
          mode: "send",
          source: "dailynews_auto_watcher",
          intervalMs: AUTO_POLL_INTERVAL_MS,
          maxAttempts: 1,
          ctx: {
            auto_trigger: true,
            require_ongoing: false,
            post_detect_delay_ms: AUTO_SEND_DELAY_MS,
            scrape_timeout_ms: DEFAULT_SCRAPE_TIMEOUT_MS
          }
        });

        if (result?.ok) return result;

        // Ongoing is present but detect/send not done yet; keep retrying until timeout.
        try {
          globalThis.AT?.uiLog?.(
            "daily_news_auto_retry_after_flow",
            "Daily News auto: detect/send not ready, retrying",
            { attempt, result },
            { level: "warn", ttlMs: 3500 }
          );
        } catch (_) {
          // ignore
        }
        await sleep(AUTO_POLL_INTERVAL_MS);
      }

      try {
        globalThis.AT?.uiLog?.(
          "daily_news_auto_timeout",
          "Daily News auto: article never became ready (stopped)",
          { max_attempts: AUTO_MAX_ATTEMPTS },
          { level: "warn", ttlMs: 6500 }
        );
      } catch (_) {
        // ignore
      }
      return { ok: false, error: "timeout" };
    } finally {
      autoWatcherInFlight = false;
    }
  }

  function startDailyNewsAutoWatcher() {
    if (autoWatcherStarted) return false;
    if (!isNativeCampClassPage()) return false;
    autoWatcherStarted = true;
    runDailyNewsAutoWatcher().catch(err => {
      console.warn("[daily_news] auto watcher failed:", err);
      try {
        globalThis.AT?.uiLog?.(
          "daily_news_auto_error",
          "Daily News auto watcher error",
          { error: String(err?.message || err) },
          { level: "error", ttlMs: 6500 }
        );
      } catch (_) {
        // ignore
      }
    });
    return true;
  }

  function bootstrapDailyNewsAutoWatcher() {
    if (startDailyNewsAutoWatcher()) return;
    let tries = 0;
    const timer = setInterval(() => {
      tries += 1;
      if (startDailyNewsAutoWatcher()) {
        clearInterval(timer);
        return;
      }
      if (tries >= AUTO_MAX_ATTEMPTS) {
        clearInterval(timer);
      }
    }, AUTO_POLL_INTERVAL_MS);
  }

  // Register globally so detectTextbookType() can dispatch to it.
  window.daily_news = daily_news;
  try {
    globalThis.AT?.log?.("handler_registered", { book_type: BOOK_TYPE });
  } catch (_) {
    // ignore
  }

  // Automatic flow for Daily News: waits for lesson state + article readiness, then triggers
  // the exact same detect/scrape/send path used by the popup button.
  bootstrapDailyNewsAutoWatcher();
})();
