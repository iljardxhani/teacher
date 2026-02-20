(() => {
  const STT_TICK_MS = 220;
  const STT_EDITOR_STABLE_MS = 1800;
  const STT_IDLE_SEND_MS = 4200;
  const STT_IDLE_SEND_INCOMPLETE_MS = 45000;
  const STT_MAX_HOLD_MS = 60000;
  const STT_MERGE_WINDOW_MS = 20000;
  const STT_SEND_RETRY_MS = 2500;
  const STT_MIC_CHECK_MS = 1500;
  const STT_MIC_REARM_INTERVAL_MS = 7000;
  const STT_MIC_INACTIVE_GRACE_MS = 3200;
  const STT_BUSY_DROP_LOG_INTERVAL_MS = 3000;

  const MAX_TRACKED_FINAL_KEYS = 400;
  const CONNECTOR_WORDS = new Set([
    "and",
    "but",
    "because",
    "so",
    "then",
    "if",
    "when",
    "while",
    "or",
    "to",
    "for",
    "with",
    "that",
    "which",
    "who",
    "whose",
    "where",
    "what",
    "why",
    "how"
  ]);
  const FILLER_WORDS = new Set([
    "uh",
    "um",
    "hmm",
    "mmm",
    "ah",
    "eh",
    "oh"
  ]);

  const sttState = {
    running: false,
    intervalId: null,
    seenFinalKeys: new Set(),
    seenFinalOrder: [],
    maxFinalIndexSeen: 0,
    pending: null,
    lastFinalSeenTs: 0,
    lastActivityTs: 0,
    lastTickTs: 0,
    micPrimed: false,
    micLastKickTs: 0,
    micInactiveSinceTs: 0,
    micLastCheckTs: 0,
    editorLastRaw: "",
    editorLastChangeTs: 0,
    editorCommitted: "",
    lastSentTextKey: "",
    lastSentTs: 0,
    waitingTeacherDone: false,
    waitingSegmentId: null,
    waitingSinceTs: 0,
    busyDroppedCount: 0,
    busyLastDropLogTs: 0,
  };

  function sttLog(event, data = {}, level = "info") {
    try {
      globalThis.AT?.log?.(event, data, level);
    } catch (_) {
      // ignore
    }
  }

  function sttUi(event, message, data = {}, opts = {}) {
    try {
      globalThis.AT?.uiLog?.(event, message, data, opts);
    } catch (_) {
      // ignore
    }
  }

  function getFlowRunId() {
    try {
      return globalThis.AT?.getRun?.()?.id || null;
    } catch (_) {
      return null;
    }
  }

  function nowMs() {
    return Date.now();
  }

  function syncTrafficState() {
    try {
      if (typeof setTrafficState === "function") {
        setTrafficState(sttState.waitingTeacherDone ? "busy" : "free");
        return true;
      }
    } catch (_) {
      // ignore
    }
    return false;
  }

  function lockWaitingForTeacherDone(segmentId) {
    const now = nowMs();
    sttState.waitingTeacherDone = true;
    sttState.waitingSegmentId = segmentId || null;
    sttState.waitingSinceTs = now;
    sttState.busyDroppedCount = 0;
    sttState.busyLastDropLogTs = 0;
    syncTrafficState();
    sttLog("stt_wait_teacher_done", {
      segment_id: sttState.waitingSegmentId,
      flow_run_id: getFlowRunId(),
      ts_ms: now,
    });
    sttUi(
      "stt_wait_teacher_done",
      "STT: waiting for teacher to finish",
      { segment_id: sttState.waitingSegmentId, flow_run_id: getFlowRunId() }
    );
  }

  function unlockFromTeacherDoneSignal(payload = null, fromRole = null) {
    if (!sttState.waitingTeacherDone) {
      sttLog("stt_teacher_done_ignored", {
        reason: "not_waiting",
        from: fromRole || null,
        kind: payload?.kind || null,
      }, "warn");
      return;
    }

    const now = nowMs();
    const waitMs = sttState.waitingSinceTs > 0 ? (now - sttState.waitingSinceTs) : null;
    const segmentId = sttState.waitingSegmentId;
    const droppedCount = sttState.busyDroppedCount;

    sttState.waitingTeacherDone = false;
    sttState.waitingSegmentId = null;
    sttState.waitingSinceTs = 0;
    sttState.busyDroppedCount = 0;
    sttState.busyLastDropLogTs = 0;
    sttState.pending = null;

    clearSpeechTexterBuffer();
    syncTrafficState();

    sttLog("stt_teacher_done", {
      segment_id: segmentId,
      flow_run_id: getFlowRunId(),
      from: fromRole || null,
      wait_ms: waitMs,
      dropped_while_busy: droppedCount,
      signal_status: payload?.meta?.status || null,
    });
    sttUi(
      "stt_teacher_done",
      "STT: teacher finished, listening for next student turn",
      { segment_id: segmentId, wait_ms: waitMs, dropped_while_busy: droppedCount }
    );
  }

  function normalizeText(text) {
    return String(text || "")
      .replace(/\u00a0/g, " ")
      .replace(/\s+/g, " ")
      .trim();
  }

  function textWords(text) {
    const t = normalizeText(text).toLowerCase();
    if (!t) return [];
    return t.split(/\s+/).filter(Boolean);
  }

  function makeSegmentId() {
    return `seg-${Date.now()}-${Math.random().toString(16).slice(2, 10)}`;
  }

  function rememberBoundedKey(stateSet, stateOrder, key, maxSize) {
    if (!key) return false;
    if (stateSet.has(key)) return true;
    stateSet.add(key);
    stateOrder.push(key);
    while (stateOrder.length > maxSize) {
      const old = stateOrder.shift();
      if (old) stateSet.delete(old);
    }
    return false;
  }

  function shouldDropNoise(text) {
    const t = normalizeText(text).toLowerCase();
    if (!t) return true;
    if (t.length < 2) return true;
    if (/^[.,!?;:\-_/*~\s]+$/.test(t)) return true;

    const alnum = t.replace(/[^a-z0-9]+/g, "");
    if (!alnum) return true;
    if (alnum.length >= 5 && new Set(alnum.split("")).size === 1) return true;

    const words = textWords(t);
    if (words.length > 0 && words.length <= 3 && words.every((w) => FILLER_WORDS.has(w))) {
      return true;
    }
    return false;
  }

  function isLikelyIncompleteThought(text) {
    const t = normalizeText(text);
    if (!t) return true;
    if (/[.?!]$/.test(t)) return false;
    const words = textWords(t);
    if (words.length <= 3) return true;
    const last = words[words.length - 1] || "";
    if (CONNECTOR_WORDS.has(last)) return true;
    return false;
  }

  function computeFlushDelayMs(lastText) {
    if (isLikelyIncompleteThought(lastText)) return STT_IDLE_SEND_INCOMPLETE_MS;
    return STT_IDLE_SEND_MS;
  }

  function clickElement(el) {
    if (!el) return false;
    try {
      if (typeof el.click === "function") {
        el.click();
        return true;
      }
    } catch (_) {
      // ignore
    }
    try {
      el.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true }));
      el.dispatchEvent(new MouseEvent("mouseup", { bubbles: true, cancelable: true }));
      el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
      return true;
    } catch (_) {
      return false;
    }
  }

  function micButton() {
    try {
      return document.getElementById("mic");
    } catch (_) {
      return null;
    }
  }

  function micLooksActive(btn) {
    if (!btn) return false;
    try {
      // SpeechTexter sets menu-item-active while mic is on.
      if (btn.classList?.contains("menu-item-active")) return true;
      if (btn.classList?.contains("active") || btn.classList?.contains("is-active")) return true;
      if (btn.classList?.contains("b-active")) return true;
      const pressed = String(btn.getAttribute("aria-pressed") || "").toLowerCase();
      if (pressed === "true") return true;
      const speechStatusText = normalizeText(document.getElementById("speech-status")?.innerText || "").toLowerCase();
      if (speechStatusText.includes("stop listening")) return true;
    } catch (_) {
      // ignore
    }
    return false;
  }

  function ensureMicListening(now = nowMs()) {
    const btn = micButton();
    if (!btn) return;

    const activeNow = micLooksActive(btn);
    if (activeNow) {
      sttState.micPrimed = true;
      sttState.micInactiveSinceTs = 0;
      return;
    }

    // One startup click to begin listening when STT flow starts.
    if (!sttState.micPrimed) {
      if (clickElement(btn)) {
        sttState.micPrimed = true;
        sttState.micLastKickTs = now;
        sttState.micInactiveSinceTs = 0;
        sttLog("stt_mic_start", { reason: "prime_click" });
      }
      return;
    }

    if (!sttState.micInactiveSinceTs) {
      sttState.micInactiveSinceTs = now;
      return;
    }
    const inactiveMs = now - sttState.micInactiveSinceTs;
    if (inactiveMs < STT_MIC_INACTIVE_GRACE_MS) return;
    if ((now - sttState.micLastKickTs) < STT_MIC_REARM_INTERVAL_MS) return;

    if (clickElement(btn)) {
      const firstKick = !sttState.micPrimed;
      sttState.micPrimed = true;
      sttState.micLastKickTs = now;
      sttState.micInactiveSinceTs = now;
      if (firstKick) {
        sttLog("stt_mic_start", { reason: "prime" });
      } else {
        sttLog("stt_mic_rearm", { reason: "stable_inactive", inactive_ms: inactiveMs }, "warn");
      }
    }
  }

  function readDataTestText() {
    try {
      const el = document.getElementById("data-test-text");
      if (!el) return "";
      const inner = String(el.innerText || "").trim();
      if (inner) return inner;

      const content = String(el.textContent || "").trim();
      if (content) return content;

      // Hidden nodes can flatten line breaks in textContent. Fall back to HTML.
      return String(el.innerHTML || "")
        .replace(/<br\s*\/?>/gi, "\n")
        .replace(/<\/p>/gi, "\n")
        .replace(/<[^>]+>/g, "")
        .replace(/&gt;/gi, ">")
        .replace(/&lt;/gi, "<")
        .replace(/&amp;/gi, "&");
    } catch (_) {
      return "";
    }
  }

  function readEditorText() {
    try {
      const el = document.getElementById("textEditor");
      if (!el) return "";
      return normalizeText(el.innerText || el.textContent || "");
    } catch (_) {
      return "";
    }
  }

  function computeDeltaText(nextBuffer, committedBuffer) {
    const next = normalizeText(nextBuffer);
    const prev = normalizeText(committedBuffer);
    if (!prev) return next;
    if (next.startsWith(prev)) return normalizeText(next.slice(prev.length));
    return next;
  }

  function parseFinalResults() {
    const raw = readDataTestText();
    if (!raw) return [];

    const normalizedRaw = String(raw || "").replace(/\r/g, "\n");
    const lines = normalizedRaw
      .split(/\r?\n+/)
      .map((line) => normalizeText(line))
      .filter(Boolean);

    const finalsByKey = new Map();
    const upsertFinal = (finalIndex, text, confidence, rawLine) => {
      const cleanText = normalizeText(text);
      const idx = Number(finalIndex);
      if (!idx || !Number.isFinite(idx)) return;
      const key = `${idx}|${cleanText.toLowerCase()}`;
      if (finalsByKey.has(key)) return;
      finalsByKey.set(key, {
        finalIndex: idx,
        text: cleanText,
        confidence: Number.isFinite(confidence) ? confidence : null,
        raw: normalizeText(rawLine || ""),
      });
    };

    for (const line of lines) {
      if (!line.toLowerCase().includes("final result #")) continue;

      const idxMatch = line.match(/final result\s*#\s*(\d+)/i);
      const txtMatch = line.match(/>\s*(.*?)\s*</);
      const confMatch = line.match(/\[\s*([0-9]+(?:\.[0-9]+)?)\s*%\s*\]/);

      upsertFinal(
        idxMatch ? Number(idxMatch[1]) : null,
        txtMatch ? txtMatch[1] : "",
        confMatch ? Number(confMatch[1]) : null,
        line
      );
    }

    // Fallback for flattened hidden-text blobs where line breaks are lost.
    const finalRegex = /(?:\[\s*([0-9]+(?:\.[0-9]+)?)\s*%\s*\]\s*)?\[\s*final result\s*#\s*(\d+)\s*\]\s*>\s*([^<]*)\s*</gi;
    let m;
    while ((m = finalRegex.exec(normalizedRaw)) !== null) {
      upsertFinal(
        Number(m[2]),
        m[3] || "",
        m[1] ? Number(m[1]) : null,
        m[0]
      );
    }

    const finals = Array.from(finalsByKey.values());
    finals.sort((a, b) => (a.finalIndex - b.finalIndex) || String(a.text).localeCompare(String(b.text)));
    return finals;
  }

  function sendStudentResponse(text, segmentId, meta = {}) {
    const clean = normalizeText(text);
    const flowRunId = getFlowRunId();
    const payload = {
      id: segmentId,
      kind: "student_response",
      text: clean,
      meta: {
        flow_run_id: flowRunId,
        segment_id: segmentId,
        source_role: "stt",
        source_page: "speechtexter",
        injected: false,
        finalized: true,
        ts_ms: nowMs(),
        ...meta,
      },
    };

    let sent = false;
    let routeMethod = "none";
    const retryDirect = (reason, err = null) => {
      if (typeof sendRouterMessage === "function") {
        try {
          sendRouterMessage(payload, "ai")
            .then((retryOk) => {
              sttLog(
                retryOk ? "stt_send_retry_ok" : "stt_send_retry_failed",
                {
                  segment_id: segmentId,
                  flow_run_id: flowRunId,
                  reason,
                  error: err ? String(err?.message || err) : null,
                },
                retryOk ? "info" : "warn"
              );
            })
            .catch((retryErr) => {
              sttLog("stt_send_retry_failed", {
                segment_id: segmentId,
                flow_run_id: flowRunId,
                reason,
                error: String(retryErr?.message || retryErr),
              }, "warn");
            });
          return;
        } catch (retryThrow) {
          sttLog("stt_send_retry_failed", {
            segment_id: segmentId,
            flow_run_id: flowRunId,
            reason,
            error: String(retryThrow?.message || retryThrow),
          }, "warn");
          return;
        }
      }
      if (typeof queueRouterMessage === "function") {
        queueRouterMessage(payload, "ai");
        sttLog("stt_send_retry_queued", {
          segment_id: segmentId,
          flow_run_id: flowRunId,
          reason,
          fallback: "queueRouterMessage",
          error: err ? String(err?.message || err) : null,
        }, "warn");
      }
    };

    // Match textbook flow first: proxy through background -> local router.
    if (typeof sendRouterMessage === "function") {
      routeMethod = "sendRouterMessage";
      const maybePromise = sendRouterMessage(payload, "ai");
      sent = true;

      if (maybePromise && typeof maybePromise.then === "function") {
        maybePromise
          .then((ok) => {
            if (ok) {
              sttLog("stt_send_confirmed", {
                segment_id: segmentId,
                flow_run_id: flowRunId,
                route_method: routeMethod,
              });
              return;
            }
            retryDirect("sendRouterMessage_false");
          })
          .catch((err) => {
            retryDirect("sendRouterMessage_error", err);
          });
      }
    } else if (typeof queueRouterMessage === "function") {
      routeMethod = "queueRouterMessage";
      queueRouterMessage(payload, "ai");
      sent = true;
    }

    sttLog("stt_send_attempt", {
      segment_id: segmentId,
      flow_run_id: flowRunId,
      text_len: clean.length,
      sent,
      route_method: routeMethod,
      chunk_count: Number(meta?.chunk_count || 0) || null,
    }, sent ? "info" : "warn");

    if (!sent) {
      console.warn("[stt] sendRouterMessage/queueRouterMessage unavailable; dropped segment", {
        segmentId,
        text: clean,
      });
    }
    return sent;
  }

  function clearSpeechTexterBuffer() {
    let method = "none";
    let ok = false;

    const newNoteBtn = document.getElementById("b-note-new");
    if (newNoteBtn && clickElement(newNoteBtn)) {
      method = "b-note-new";
      ok = true;
    } else {
      const editor = document.getElementById("textEditor");
      const debugData = document.getElementById("data-test-text");

      try {
        if (editor) {
          editor.innerHTML = "<p></p>";
          method = "textEditor.innerHTML";
          ok = true;
        }
      } catch (_) {
        // ignore
      }

      try {
        if (debugData) {
          debugData.textContent = "";
          if (!ok) method = "data-test-text";
          ok = true;
        }
      } catch (_) {
        // ignore
      }
    }

    sttLog("stt_buffer_cleared", { ok, method });
    if (ok) {
      sttState.editorCommitted = "";
      sttState.editorLastRaw = "";
      sttState.editorLastChangeTs = nowMs();
    }
    return ok;
  }

  function discardWhileTeacherBusy(now = nowMs()) {
    let hasOverlap = false;
    const finals = parseFinalResults();
    if (finals.length > 0) {
      hasOverlap = true;
      for (const final of finals) {
        const txt = normalizeText(final.text);
        const finalKey = `${final.finalIndex}|${txt.toLowerCase()}`;
        rememberBoundedKey(sttState.seenFinalKeys, sttState.seenFinalOrder, finalKey, MAX_TRACKED_FINAL_KEYS);
        sttState.maxFinalIndexSeen = Math.max(sttState.maxFinalIndexSeen, Number(final.finalIndex) || 0);
      }
      sttState.lastFinalSeenTs = now;
      sttState.lastActivityTs = now;
    }

    const editorText = readEditorText();
    if (editorText) {
      hasOverlap = true;
      sttState.lastActivityTs = now;
    }

    if (sttState.pending) {
      hasOverlap = true;
      sttState.pending = null;
    }

    if (!hasOverlap) return;

    clearSpeechTexterBuffer();
    sttState.busyDroppedCount += 1;
    if (!sttState.busyLastDropLogTs || (now - sttState.busyLastDropLogTs) >= STT_BUSY_DROP_LOG_INTERVAL_MS) {
      sttState.busyLastDropLogTs = now;
      sttLog("stt_overlap_dropped_while_teacher_busy", {
        waiting_segment_id: sttState.waitingSegmentId,
        dropped_count: sttState.busyDroppedCount,
        flow_run_id: getFlowRunId(),
      }, "warn");
      sttUi(
        "stt_overlap_dropped_while_teacher_busy",
        "STT: dropped overlapping student speech while teacher is speaking",
        { waiting_segment_id: sttState.waitingSegmentId, dropped_count: sttState.busyDroppedCount },
        { level: "warn", ttlMs: 2800 }
      );
    }
  }

  function initPending(now, sourceTag) {
    sttState.pending = {
      segmentId: makeSegmentId(),
      parts: [],
      startedTs: now,
      lastPartTs: now,
      flushAfterMs: STT_IDLE_SEND_MS,
      sources: new Set(),
      lastHoldLogTs: 0,
    };
    sttLog("stt_segment_held", {
      segment_id: sttState.pending.segmentId,
      reason: "new_turn",
      source: sourceTag || null,
    });
  }

  function pendingText(pending) {
    if (!pending || !Array.isArray(pending.parts)) return "";
    return normalizeText(pending.parts.map((p) => normalizeText(p.text)).join(" "));
  }

  function flushPending(reason) {
    const pending = sttState.pending;
    if (!pending) return false;

    const text = pendingText(pending);
    const segmentId = pending.segmentId;
    const now = nowMs();
    const holdMs = now - pending.startedTs;
    const idleMs = now - pending.lastPartTs;

    if (!text) {
      sttState.pending = null;
      sttLog("stt_segment_dropped", {
        segment_id: segmentId,
        reason: "empty_pending",
      }, "warn");
      return false;
    }

    if (shouldDropNoise(text)) {
      sttState.pending = null;
      sttLog("stt_segment_dropped_noise", {
        segment_id: segmentId,
        reason,
        hold_ms: holdMs,
        idle_ms: idleMs,
        text_len: text.length,
        preview: text.slice(0, 160),
      }, "warn");
      sttUi(
        "stt_segment_dropped_noise",
        "STT: dropped noisy segment",
        { segment_id: segmentId, text_len: text.length },
        { level: "warn", ttlMs: 3000 }
      );
      return false;
    }

    const confidenceValues = pending.parts
      .map((p) => (Number.isFinite(p.confidence) ? Number(p.confidence) : null))
      .filter((v) => v !== null);
    const avgConfidence = confidenceValues.length
      ? Math.round((confidenceValues.reduce((a, b) => a + b, 0) / confidenceValues.length) * 100) / 100
      : null;

    sttUi(
      "stt_segment_finalized",
      "STT: finalized segment",
      {
        segment_id: segmentId,
        text_len: text.length,
        flow_run_id: getFlowRunId(),
        hold_ms: holdMs,
        idle_ms: idleMs,
        reason,
      }
    );

    const sent = sendStudentResponse(text, segmentId, {
      chunk_count: pending.parts.length,
      avg_confidence: avgConfidence,
      finalize_reason: reason,
      hold_ms: holdMs,
      idle_ms: idleMs,
    });
    if (!sent) {
      // Keep the pending segment and retry instead of clearing text and losing a turn.
      pending.lastPartTs = now;
      pending.flushAfterMs = Math.min(pending.flushAfterMs || STT_SEND_RETRY_MS, STT_SEND_RETRY_MS);
      sttLog("stt_segment_send_deferred", {
        segment_id: segmentId,
        reason,
        hold_ms: holdMs,
        idle_ms: idleMs,
        text_len: text.length,
      }, "warn");
      sttUi(
        "stt_segment_send_deferred",
        "STT: router unavailable, retrying segment send",
        { segment_id: segmentId, text_len: text.length },
        { level: "warn", ttlMs: 3000 }
      );
      return false;
    }

    sttState.lastSentTextKey = text.toLowerCase();
    sttState.lastSentTs = now;

    sttLog("stt_segment_sent", {
      segment_id: segmentId,
      reason,
      hold_ms: holdMs,
      idle_ms: idleMs,
      chunk_count: pending.parts.length,
      avg_confidence: avgConfidence,
      text_len: text.length,
      text_preview: text.slice(0, 180),
    });

    lockWaitingForTeacherDone(segmentId);
    clearSpeechTexterBuffer();
    sttState.pending = null;
    return true;
  }

  function pushCandidate(text, meta = {}) {
    const clean = normalizeText(text);
    const now = nowMs();
    if (!clean) return false;

    const textKey = clean.toLowerCase();
    const lastPartText = normalizeText(sttState.pending?.parts?.[sttState.pending.parts.length - 1]?.text || "").toLowerCase();
    if (lastPartText && lastPartText === textKey) {
      sttLog("stt_candidate_duplicate", {
        reason: "same_as_last_part",
        source: meta.source || null,
        final_index: meta.finalIndex || null,
        text_len: clean.length,
      }, "warn");
      return false;
    }
    if (sttState.lastSentTextKey && sttState.lastSentTextKey === textKey && (now - sttState.lastSentTs) < 4000) {
      sttLog("stt_candidate_duplicate", {
        reason: "same_as_recent_sent",
        source: meta.source || null,
        final_index: meta.finalIndex || null,
        text_len: clean.length,
      }, "warn");
      return false;
    }

    if (sttState.pending) {
      const gapMs = now - sttState.pending.lastPartTs;
      if (gapMs > STT_MERGE_WINDOW_MS) {
        flushPending("merge_window_elapsed_before_new");
      }
    }

    if (!sttState.pending) {
      initPending(now, meta.source || "unknown");
    } else {
      sttLog("stt_segment_merged", {
        segment_id: sttState.pending.segmentId,
        source: meta.source || null,
        final_index: meta.finalIndex || null,
        gap_ms: now - sttState.pending.lastPartTs,
      });
    }

    const pending = sttState.pending;
    pending.parts.push({
      text: clean,
      confidence: Number.isFinite(meta.confidence) ? Number(meta.confidence) : null,
      finalIndex: Number.isFinite(meta.finalIndex) ? Number(meta.finalIndex) : null,
      source: meta.source || null,
      ts: now,
    });
    pending.lastPartTs = now;
    pending.sources.add(meta.source || "unknown");
    pending.flushAfterMs = computeFlushDelayMs(clean);

    sttState.lastActivityTs = now;
    sttLog("stt_candidate_held", {
      segment_id: pending.segmentId,
      source: meta.source || null,
      final_index: meta.finalIndex || null,
      confidence: Number.isFinite(meta.confidence) ? Number(meta.confidence) : null,
      flush_after_ms: pending.flushAfterMs,
      parts_count: pending.parts.length,
      text_len: clean.length,
      text_preview: clean.slice(0, 140),
    });
    return true;
  }

  function consumeFinalResults() {
    const finals = parseFinalResults();
    if (finals.length === 0) return 0;

    const currentMaxIndex = finals.reduce((acc, item) => Math.max(acc, Number(item.finalIndex) || 0), 0);
    if (sttState.maxFinalIndexSeen > 0 && currentMaxIndex > 0 && currentMaxIndex < sttState.maxFinalIndexSeen) {
      // SpeechTexter final counter reset (usually after clear/new note).
      const previousMax = sttState.maxFinalIndexSeen;
      sttState.seenFinalKeys.clear();
      sttState.seenFinalOrder.length = 0;
      sttState.maxFinalIndexSeen = 0;
      sttLog("stt_final_counter_reset", { previous_max: previousMax, current_max: currentMaxIndex });
    }

    let pushed = 0;
    for (const final of finals) {
      const txt = normalizeText(final.text);
      const finalKey = `${final.finalIndex}|${txt.toLowerCase()}`;
      const known = rememberBoundedKey(sttState.seenFinalKeys, sttState.seenFinalOrder, finalKey, MAX_TRACKED_FINAL_KEYS);
      if (known) continue;

      sttState.maxFinalIndexSeen = Math.max(sttState.maxFinalIndexSeen, final.finalIndex);
      sttState.lastFinalSeenTs = nowMs();
      sttState.lastActivityTs = sttState.lastFinalSeenTs;

      if (!txt) {
        sttLog("stt_segment_dropped", {
          reason: "empty_final_result",
          final_index: final.finalIndex,
          confidence: final.confidence,
        }, "warn");
        continue;
      }

      if (shouldDropNoise(txt)) {
        sttLog("stt_segment_dropped_noise", {
          reason: "noise_final_result",
          final_index: final.finalIndex,
          confidence: final.confidence,
          text_len: txt.length,
          text_preview: txt.slice(0, 120),
        }, "warn");
        continue;
      }

      if (pushCandidate(txt, {
        source: "data-test-final",
        finalIndex: final.finalIndex,
        confidence: final.confidence,
      })) {
        pushed += 1;
      }
    }
    return pushed;
  }

  function consumeEditorFallback() {
    const now = nowMs();
    const current = readEditorText();

    if (current !== sttState.editorLastRaw) {
      sttState.editorLastRaw = current;
      sttState.editorLastChangeTs = now;
      sttState.lastActivityTs = now;
      return 0;
    }

    if (!current) return 0;
    if (now - sttState.editorLastChangeTs < STT_EDITOR_STABLE_MS) return 0;
    if (sttState.lastFinalSeenTs > 0 && now - sttState.lastFinalSeenTs < 2000) return 0;

    const delta = computeDeltaText(current, sttState.editorCommitted);
    if (!delta) return 0;

    sttState.editorCommitted = current;
    const pushed = pushCandidate(delta, { source: "textEditor-fallback" }) ? 1 : 0;
    if (pushed) {
      sttLog("stt_editor_fallback_used", {
        text_len: normalizeText(delta).length,
      }, "warn");
    }
    return pushed;
  }

  function maybeFlushPending() {
    const pending = sttState.pending;
    if (!pending) return;

    const now = nowMs();
    const idleMs = now - pending.lastPartTs;
    const holdMs = now - pending.startedTs;
    if (holdMs >= STT_MAX_HOLD_MS) {
      flushPending("max_hold_timeout");
      return;
    }
    if (idleMs >= pending.flushAfterMs) {
      flushPending("idle_timeout");
      return;
    }

    if (now - pending.lastHoldLogTs > 5000) {
      pending.lastHoldLogTs = now;
      sttLog("stt_segment_held", {
        segment_id: pending.segmentId,
        reason: "waiting_for_more_context",
        idle_ms: idleMs,
        hold_ms: holdMs,
        flush_after_ms: pending.flushAfterMs,
        parts_count: pending.parts.length,
      });
    }
  }

  function handleTick() {
    if (!sttState.running) return;
    const now = nowMs();
    sttState.lastTickTs = now;

    syncTrafficState();
    if (!sttState.micLastCheckTs || (now - sttState.micLastCheckTs) >= STT_MIC_CHECK_MS) {
      sttState.micLastCheckTs = now;
      ensureMicListening(now);
    }
    if (sttState.waitingTeacherDone) {
      discardWhileTeacherBusy(now);
      return;
    }
    const newFinals = consumeFinalResults();
    if (newFinals === 0) {
      consumeEditorFallback();
    }
    maybeFlushPending();
  }

  function startSTTFlow() {
    if (sttState.running) return;
    sttState.running = true;
    sttState.lastActivityTs = nowMs();
    sttState.editorLastRaw = readEditorText();
    sttState.editorCommitted = sttState.editorLastRaw;
    sttState.editorLastChangeTs = nowMs();
    sttState.micLastCheckTs = 0;
    sttState.waitingTeacherDone = false;
    sttState.waitingSegmentId = null;
    sttState.waitingSinceTs = 0;
    sttState.busyDroppedCount = 0;
    sttState.busyLastDropLogTs = 0;

    syncTrafficState();
    ensureMicListening(nowMs());
    sttState.intervalId = setInterval(handleTick, STT_TICK_MS);

    sttLog("stt_flow_started", {
      tick_ms: STT_TICK_MS,
      idle_send_ms: STT_IDLE_SEND_MS,
      idle_send_incomplete_ms: STT_IDLE_SEND_INCOMPLETE_MS,
      max_hold_ms: STT_MAX_HOLD_MS,
      merge_window_ms: STT_MERGE_WINDOW_MS,
      mic_check_ms: STT_MIC_CHECK_MS,
      mic_rearm_interval_ms: STT_MIC_REARM_INTERVAL_MS,
      mic_inactive_grace_ms: STT_MIC_INACTIVE_GRACE_MS,
      overlap_policy: "drop_while_teacher_busy",
      source_priority: "data-test-final_then_textEditor_fallback",
    });
    sttUi("stt_flow_started", "STT: listening with adaptive endpointing", {});
  }

  function stopSTTFlow() {
    if (!sttState.running) return;
    sttState.running = false;
    if (sttState.intervalId) clearInterval(sttState.intervalId);
    sttState.intervalId = null;

    // Try to flush any pending text before stopping.
    if (sttState.pending) {
      flushPending("flow_stopped_flush");
    }
    sttState.waitingTeacherDone = false;
    sttState.waitingSegmentId = null;
    sttState.waitingSinceTs = 0;
    syncTrafficState();
    sttLog("stt_flow_stopped", {});
  }

  globalThis.startSTTFlow = startSTTFlow;
  globalThis.stopSTTFlow = stopSTTFlow;

  let role = "unknown";
  try {
    if (typeof detectPageRole === "function") role = detectPageRole();
  } catch (_) {
    // ignore
  }
  console.log("stt.js loaded; role =", role);

  if (typeof chrome !== "undefined" && chrome.runtime?.onMessage?.addListener) {
    chrome.runtime.onMessage.addListener((msg) => {
      if (msg?.to !== "stt") return;
      const payload = msg?.message ?? msg;
      if (payload && typeof payload === "object" && payload.kind === "teacher_turn_finished") {
        unlockFromTeacherDoneSignal(payload, msg?.from || payload?.meta?.source_role || null);
        return;
      }
      const text = typeof payload === "string" ? payload : payload?.text ?? JSON.stringify(payload);
      console.log("🗣️ STT console log:", { from: msg?.from, text, raw: payload });
    });
  } else {
    console.warn("chrome.runtime.onMessage unavailable; STT listener skipped.");
  }
})();
