var kT = window.kauliT || function (s, v) { return s; };   // the words the scripts show, in the reader's language (see webapp/js_strings.py)
/* Client job-submission wizard: 3 steps over one real <form> (still a
 * single POST to /account/orders - nothing here changes what the backend
 * receives, it just paces how much of the form is on screen at once).
 * Also drives the drag-and-drop file zone and a live, client-side cost
 * estimate that mirrors billing.order_cost_usd's math so what someone
 * sees here roughly matches what they're actually charged.
 */
(function () {
  "use strict";

  function $(sel, root) { return (root || document).querySelector(sel); }
  function $all(sel, root) { return Array.from((root || document).querySelectorAll(sel)); }

  // ------------------------------------------------------------ wizard ----
  let currentStep = 1;
  const TOTAL_STEPS = 3;

  // ------------------------------------------- surviving a refresh ----
  // Real problem this fixes: a client mid-upload (or one who'd just
  // finished uploading but hadn't hit final Submit yet) who refreshed
  // the page used to lose everything - the whole file, gone, with no way
  // back except picking it and uploading it again from scratch. Once the
  // file has actually finished its PUT to R2, there's no reason a
  // refresh should cost anything: the bytes are already safely stored,
  // only the small metadata-only finalize POST is still needed. Persist
  // just enough (the R2 object key + original filename) to skip
  // re-uploading, not the whole wizard's other fields (language/service/
  // instructions) - re-picking a dropdown is trivial next to re-uploading
  // a real file; not worth the complexity of persisting everything.
  const RESUME_KEY = "kauli_pending_upload";
  let resumedUploadKey = null;
  let uploadTransferActive = false;
  // What an upload in progress looks like to the rest of the app (the tray that follows it around, persist.js), and what is kept
  // so that leaving this page never means starting again.
  const kpUp = (patch) => { if (window.kpUploads) window.kpUploads.set(patch); };
  const stillHere = (form) => !!form && document.getElementById("order-form") === form;     // they may have moved on while this ran
  const DRAFT_KEY = "f:" + location.pathname + "#order-form";
  let pendingStep = null;
  function clearStaged() {
    if (window.kpFiles) window.kpFiles.clear("order");
    if (window.kpDrafts) window.kpDrafts.del(DRAFT_KEY);
  }

  function saveResumableUpload(uploadKey, filename) {
    try {
      sessionStorage.setItem(RESUME_KEY, JSON.stringify({ uploadKey, filename }));
    } catch (e) { /* private browsing / storage disabled - resume just won't be offered, not fatal */ }
  }
  function clearResumableUpload() {
    resumedUploadKey = null;
    try { sessionStorage.removeItem(RESUME_KEY); } catch (e) { /* ignore */ }
  }
  function offerResumableUploadIfAny() {
    let saved;
    try { saved = JSON.parse(sessionStorage.getItem(RESUME_KEY) || "null"); } catch (e) { saved = null; }
    if (!saved || !saved.uploadKey) return;
    const dz = $("#dropzone");
    if (!dz) return;
    const banner = document.createElement("div");
    banner.className = "modal-warning";
    banner.style.marginBottom = "12px";
    banner.innerHTML =
      "You have an already-uploaded file waiting to submit: <strong></strong>. " +
      "Continue with it, or discard and pick a different file.";
    banner.querySelector("strong").textContent = saved.filename;
    const continueBtn = document.createElement("button");
    continueBtn.type = "button";
    continueBtn.className = "secondary";
    continueBtn.style.marginTop = "8px";
    continueBtn.textContent = kT("Continue with this file");
    const discardBtn = document.createElement("button");
    discardBtn.type = "button";
    discardBtn.className = "secondary";
    discardBtn.style.marginTop = "8px";
    discardBtn.style.marginLeft = "8px";
    discardBtn.textContent = kT("Discard");
    banner.appendChild(continueBtn);
    banner.appendChild(discardBtn);
    dz.parentNode.insertBefore(banner, dz);
    continueBtn.addEventListener("click", function () {
      resumedUploadKey = saved.uploadKey;
      dz.classList.add("has-file");
      const text = $("#dropzone-text");
      if (text) text.textContent = saved.filename + " (already uploaded - ready to submit)";
      banner.remove();
    });
    discardBtn.addEventListener("click", function () {
      clearResumableUpload();
      banner.remove();
    });
  }

  // A file the customer picked from Google Drive, OneDrive or Dropbox (static/cloud_import.js): it is already on the server, so
  // the order is submitted with its key instead of uploading anything.
  window.kauliUseCloudFile = function (uploadKey, filename) {
    fileQueue.length = 0;
    if (typeof renderQueue === "function") renderQueue();
    resumedUploadKey = uploadKey;
    const dz = $("#dropzone");
    if (dz) dz.classList.add("has-file");
    const text = $("#dropzone-text");
    if (text) text.textContent = filename + " (imported - ready to submit)";
  };

  // ------------------------------------------------------ friction log ----
  // Real, in-house signal of where clients actually get stuck - see
  // db.log_wizard_step / db.client_funnel_stats and /staff/ops. Never
  // sent anywhere outside this app's own database. sendBeacon (fire-and-
  // forget, survives the page unloading) when available, a keepalive
  // fetch otherwise - either way this must never be able to slow down or
  // break the wizard itself, hence the try/catch and the empty .catch.
  function logWizardEvent(step) {
    try {
      var body = "step=" + encodeURIComponent(step);
      if (navigator.sendBeacon) {
        navigator.sendBeacon("/account/wizard-event", new Blob([body], { type: "application/x-www-form-urlencoded" }));
      } else if (window.fetch) {
        fetch("/account/wizard-event", {
          method: "POST",
          headers: { "Content-Type": "application/x-www-form-urlencoded" },
          body: body, keepalive: true,
        }).catch(function () {});
      }
    } catch (e) { /* friction logging must never break the actual wizard */ }
  }

  function showStep(n) {
    currentStep = n;
    if (window.kpDrafts) window.kpDrafts.touch($("#order-form"));                    // the step they are on is kept too
    logWizardEvent(String(n));
    $all(".wizard-pane").forEach((p) => { p.hidden = Number(p.dataset.pane) !== n; });
    $all(".wizard-step[data-step]").forEach((s) => {
      const stepNum = Number(s.dataset.step);
      s.classList.toggle("active", stepNum === n);
      s.classList.toggle("done", stepNum < n);
      if (stepNum === n) s.setAttribute("aria-current", "step"); else s.removeAttribute("aria-current");
      s.setAttribute("aria-disabled", String(stepNum >= n));   // only earlier steps are clickable
    });
    $("#wizard-back").hidden = n === 1;
    $("#wizard-next").hidden = n === TOTAL_STEPS;
    $("#wizard-submit").hidden = n !== TOTAL_STEPS;
    if (n === 2) updatePricing();
    if (n === 3) { updatePricing(); updateReview(); }
    $(".wizard-card").scrollIntoView({ block: "start", behavior: "smooth" });
  }

  // "Review your order" summary on the last step - read straight off the
  // controls the person already filled in, so it can never disagree with
  // what is actually submitted.
  function updateReview() {
    const set = (id, text) => { const el = $("#" + id); if (el) el.textContent = text; };
    const usingYoutube = $('#source-switch [data-source="youtube"]').classList.contains("active");
    const file = fileQueue[0];
    set("rv-file", usingYoutube ? ($("#youtube-url-input").value.trim() || "-")
                                : (fileQueue.length > 1 ? fileQueue.length + " files" : file ? file.name : (resumedUploadKey ? "Your uploaded file" : "-")));
    const src = $("#source-lang-select");
    const tgtField = $("#target-lang-field");
    const tgt = tgtField ? tgtField.querySelector("select") : null;
    const srcText = src && src.selectedOptions[0] ? src.selectedOptions[0].text : "";
    const showsTarget = tgtField && !tgtField.hidden;
    const svc = $('input[name="service_level"]:checked');
    const wantsTranslation = svc && svc.value !== "transcribe";
    set("rv-langs", showsTarget && wantsTranslation && tgt && tgt.selectedOptions[0]
        ? srcText + " \u2192 " + tgt.selectedOptions[0].text : srcText);
    const tier = $('input[name="review_tier"]:checked');
    const tierName = tier ? (tier.value === "ai" ? kT("Instant AI") : kT("Human-reviewed")) : "";
    set("rv-service", (svc ? kT(svc.dataset.name) : "-") + (tierName ? " - " + tierName : ""));
    const total = $("#price-total");
    set("rv-total", total ? total.textContent : "-");
    // The rate and the length that make up the total, shown beside it: "$0.12 / min  x 1:17 min".
    const perMin = svc ? parseFloat(tier && tier.value !== "ai" ? svc.dataset.rateHuman : svc.dataset.rateAi) : NaN;
    set("rv-rate", isNaN(perMin) ? "-" : formatMoney(perMin));
    const length = $("#price-duration");
    set("rv-duration", length && length.textContent && length.textContent !== "\u2014" ? "\u00d7 " + length.textContent : "");
  }

  function step1Valid() {
    const usingYoutube = $('#source-switch [data-source="youtube"]').classList.contains("active");
    if (usingYoutube) {
      const url = $("#youtube-url-input").value.trim();
      if (!url) { alert(kT("Paste a YouTube link first.")); return false; }
      return true;
    }
    const file = fileQueue[0];
    if (!file && !resumedUploadKey) { alert(kT("Choose a file first.")); return false; }
    return true;
  }

  function bindWizardNav() {
    $("#wizard-next").addEventListener("click", () => {
      if (currentStep === 1 && !step1Valid()) return;
      if (currentStep < TOTAL_STEPS) showStep(currentStep + 1);
    });
    $("#wizard-back").addEventListener("click", () => {
      if (currentStep > 1) showStep(currentStep - 1);
    });
    $all(".wizard-step[data-step]").forEach((s) => {
      // Clicking an earlier, already-visited step jumps back to it -
      // clicking ahead does nothing (still gated by step1Valid()).
      // Real keyboard access: focusable, announced as a button, Enter/Space.
      s.tabIndex = 0;
      s.setAttribute("role", "button");
      const go = () => {
        const target = Number(s.dataset.step);
        if (target < currentStep) showStep(target);
      };
      s.addEventListener("click", go);
      s.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); }
      });
    });
  }

  // ------------------------------------------------------- source switch ----
  function bindSourceSwitch() {
    const fileBtn = $('#source-switch [data-source="file"]');
    const ytBtn = $('#source-switch [data-source="youtube"]');
    const fileCard = $("#card-file");
    const ytCard = $("#card-youtube");
    const ytInput = $("#youtube-url-input");
    // The two cards are always visible; the hidden buttons still carry the chosen source
    // (the rest of this file reads it from them), and the cards just show which one is live.
    function setSource(which) {
      const yt = which === "youtube";
      ytBtn.classList.toggle("active", yt);
      fileBtn.classList.toggle("active", !yt);
      if (fileCard) fileCard.classList.toggle("is-active", !yt);
      if (ytCard) ytCard.classList.toggle("is-active", yt);
      if (yt) {
        fileDurationMinutes = null;
        updatePricing();
      }
    }
    fileBtn.addEventListener("click", () => setSource("file"));
    ytBtn.addEventListener("click", () => setSource("youtube"));
    setSource(ytBtn.classList.contains("active") ? "youtube" : "file");
    if (ytInput) {
      // typing or pasting a link makes YouTube the source
      ytInput.addEventListener("input", () => { if (ytInput.value.trim()) ytBtn.click(); });
      ytInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { e.preventDefault(); const go = $("#youtube-go"); if (go) go.click(); }
      });
    }
    const go = $("#youtube-go");
    if (go) go.addEventListener("click", () => {
      ytBtn.click();
      $("#wizard-next").click();                       // same checks as pressing Next
    });
  }

  // -------------------------------------------------------- drag & drop ----
  let fileDurationMinutes = null;
  let fileQueue = [];

  function probeDuration(file) {
    return new Promise((resolve) => {
      const el = document.createElement(file.type.startsWith("video/") ? "video" : "audio");
      el.preload = "metadata";
      const url = URL.createObjectURL(file);
      el.src = url;
      el.onloadedmetadata = () => {
        URL.revokeObjectURL(url);
        resolve(isFinite(el.duration) ? el.duration / 60 : null);
      };
      el.onerror = () => { URL.revokeObjectURL(url); resolve(null); };
    });
  }

  // Several files at once: each one becomes its own order with the same settings (see submitBatch).
  const MAX_FILES = 10;
  const fileState = new Map();                      // file -> { text, tone } while a batch is being sent
  let durationToken = 0;

  function sizeLabel(bytes) {
    return bytes >= 1048576 ? (bytes / 1048576).toFixed(bytes >= 10485760 ? 0 : 1) + " MB" : Math.max(1, Math.round(bytes / 1024)) + " KB";
  }

  function renderQueue() {
    const list = $("#file-queue");
    if (!list) return;
    list.textContent = "";
    fileQueue.forEach((f) => {
      const li = document.createElement("li");
      const st = fileState.get(f);
      if (st && st.tone) li.className = "is-" + st.tone;
      const name = document.createElement("span"); name.className = "fq-name"; name.textContent = f.name; name.title = f.name;
      const meta = document.createElement("span"); meta.className = "fq-meta"; meta.textContent = st ? st.text : sizeLabel(f.size);
      li.appendChild(name); li.appendChild(meta);
      if (!st || st.tone === "bad") {
        const x = document.createElement("button");
        x.type = "button"; x.className = "fq-remove"; x.setAttribute("aria-label", "Remove " + f.name); x.textContent = "\u00d7";
        x.addEventListener("click", () => { fileState.delete(f); fileQueue = fileQueue.filter((q) => q !== f); if (window.kpFiles) window.kpFiles.remove("order", f); syncQueue(); });
        li.appendChild(x);
      }
      list.appendChild(li);
    });
  }

  function syncQueue() {
    const dz = $("#dropzone");
    const text = $("#dropzone-text");
    renderQueue();
    fileDurationMinutes = null;
    if (!fileQueue.length) {
      dz.classList.remove("has-file");
      text.textContent = kT("Drag & drop files or click to upload");
      updatePricing();
      return;
    }
    dz.classList.add("has-file");
    text.textContent = fileQueue.length === 1 ? fileQueue[0].name : fileQueue.length + " files selected - click to add more";
    updatePricing();
    // the price estimate covers every file in the list
    const token = ++durationToken;
    Promise.all(fileQueue.map(probeDuration)).then((mins) => {
      if (token !== durationToken) return;
      const known = mins.filter((m) => m != null);
      fileDurationMinutes = known.length === mins.length ? known.reduce((x, y) => x + y, 0) : null;
      updatePricing();
    });
  }

  function addFiles(list, restored) {
    const incoming = Array.from(list || []);
    if (!incoming.length) return;
    const ytField = $("#youtube-url-input");
    if (ytField) ytField.value = "";
    $('#source-switch [data-source="file"]').click();
    let skipped = 0;
    incoming.forEach((f) => {
      if (fileQueue.some((q) => q.name === f.name && q.size === f.size && q.lastModified === f.lastModified)) return;
      if (fileQueue.length >= MAX_FILES) { skipped++; return; }
      fileQueue.push(f);
      if (!restored && window.kpFiles) window.kpFiles.add("order", f);            // kept in the browser, so leaving this page does not lose it
    });
    const note = $("#file-queue-note");
    if (note) note.textContent = skipped ? "You can send up to " + MAX_FILES + " files at once. The rest were left out." : "";
    syncQueue();
  }

  function bindDropzone() {
    const dz = $("#dropzone");
    const input = $("#file-input");
    if (!dz || !input) return;
    dz.addEventListener("click", () => input.click());
    dz.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); }
    });
    input.addEventListener("change", () => { addFiles(input.files); input.value = ""; });   // clearing lets the same file be picked again

    ["dragenter", "dragover"].forEach((evt) => {
      dz.addEventListener(evt, (e) => { e.preventDefault(); dz.classList.add("dragover"); });
    });
    ["dragleave", "drop"].forEach((evt) => {
      dz.addEventListener(evt, (e) => { e.preventDefault(); dz.classList.remove("dragover"); });
    });
    dz.addEventListener("drop", (e) => addFiles(e.dataTransfer.files));
  }

  // ------------------------------------------------------ live pricing ----
  function formatMoney(n) { return "$" + n.toFixed(2); }

  function updatePricing() {
    const data = $("#pricing-data");
    if (!data) return;
    const discount = Number(data.dataset.discount) || 0;
    const freeMinutes = Number(data.dataset.freeMinutes) || 0;
    const humanMinutes = Number(data.dataset.humanMinutes) || 0;      // included human-reviewed minutes left this month
    const walletCredits = Number(data.dataset.walletCredits) || 0;
    const addonRate = Number(data.dataset.addonRate) || 0;
    const manualRate = Number(data.dataset.manualTranscriptionRate) || 0;
    const humanVoiceRate = Number(data.dataset.humanVoiceRate) || 0;
    const premiumVoiceRate = Number(data.dataset.premiumVoiceRate) || 0;
    const audioCleanupRate = Number(data.dataset.audioCleanupRate) || 0;
    const audioCleanupRateAi = Number(data.dataset.audioCleanupRateAi) || 0;
    const premiumAsrRate = Number(data.dataset.premiumAsrRate) || 0;
    const rushPct = Number(data.dataset.rushPct) || 0;
    const CREDITS_PER_DOLLAR = 10; // mirrors billing.CREDITS_PER_DOLLAR

    let selected = $('input[name="service_level"]:checked');

    // Review tier: Instant AI vs Human-reviewed. A manual-transcription
    // language (no ASR model - a person types it) is always human-reviewed,
    // mirroring create_order's server-side override.
    const tierLangSelect = $("#source-lang-select");
    const tierManualLangs = tierLangSelect ? (tierLangSelect.dataset.manualLangs || "").split(",").filter(Boolean) : [];
    const aiOnlyLangs = tierLangSelect ? (tierLangSelect.dataset.aiOnlyLangs || "").split(",").filter(Boolean) : [];
    // Chinese, German and Spanish when switched on: not Instant-AI-only, they can also be human-reviewed.
    const extraLangs = tierLangSelect ? (tierLangSelect.dataset.extraReviewLangs || "").split(",").filter(Boolean) : [];
    const tierForced = !!(tierLangSelect && tierManualLangs.includes(tierLangSelect.value));
    // The ~70 extra languages: machine transcription only - no reviewer,
    // translator or voice exists for them, so lock to Instant AI +
    // Transcription only (mirrors create_order's server-side check).
    const aiOnlyLang = !!(tierLangSelect && aiOnlyLangs.includes(tierLangSelect.value));
    const laraOn = !!(tierLangSelect && tierLangSelect.dataset.lara === "1");
    if (aiOnlyLang) {
      const aiRadio = $('input[name="review_tier"][value="ai"]');
      if (aiRadio) aiRadio.checked = true;
      // With Lara Translate connected the wider languages can also be
      // translated (Instant AI); dubbing is never available for them.
      const cur = $('input[name="service_level"]:checked');
      const trRadio = $('input[name="service_level"][value="transcribe"]');
      if (trRadio && (!laraOn || !cur || cur.value === "dub")) trRadio.checked = true;
    }
    $all('input[name="service_level"]').forEach((r) => {
      r.disabled = aiOnlyLang && (r.value === "dub" || (!laraOn && r.value !== "transcribe"));
    });
    selected = $('input[name="service_level"]:checked'); // the forced choice above may have changed it
    const targetField = $("#target-lang-field");
    if (tierForced) {
      const humanRadio = $('input[name="review_tier"][value="human"]');
      if (humanRadio) humanRadio.checked = true;
    }
    $all('input[name="review_tier"]').forEach((r) => {
      r.disabled = (tierForced && r.value !== "human") || (aiOnlyLang && r.value !== "ai");
    });
    const forcedNote = $("#review-tier-forced-note");
    if (forcedNote) {
      forcedNote.hidden = !(tierForced || aiOnlyLang);
      forcedNote.textContent = aiOnlyLang
        ? (laraOn
            ? "This language is Instant AI only: transcription and translation, with no dubbing or human review."
            : "This language is available as Instant AI transcription only (no translation, dubbing or human review).")
        : "This language is transcribed by one of our team by hand, so it is always human-reviewed.";
    }
    // Studio is made for dubbing: it is offered with the Dubbing service only, and a switch to anything else puts the choice back on Human-reviewed.
    const studioOption = $("#studio-option");
    const studioWanted = !!(selected && selected.value === "dub");
    if (studioOption) {
      studioOption.hidden = !studioWanted;
      const studioRadio = $('input[name="review_tier"][value="studio"]');
      if (!studioWanted && studioRadio && studioRadio.checked) { const h = $('input[name="review_tier"][value="human"]'); if (h) h.checked = true; }
    }
    const tierRadio = $('input[name="review_tier"]:checked');
    const tier = tierRadio ? tierRadio.value : "human";
    const isAiTier = tier === "ai";
    // Clean-up costs less on an Instant AI order.
    const cleanupUnit = isAiTier && audioCleanupRateAi ? audioCleanupRateAi : audioCleanupRate;
    [].forEach.call(document.querySelectorAll(".cleanup-price"), (el) => { el.textContent = "+" + formatMoney(cleanupUnit) + "/min"; });
    // A dub has its own extras (voice, voice actor, cloning consent): show them the first time a dub is chosen.
    const extrasBox = $("#extras");
    if (extrasBox && selected && selected.value === "dub" && !extrasBox.dataset.shown) { extrasBox.open = true; extrasBox.dataset.shown = "1"; }
    // How exact the transcript is, and the other advanced options, are instructions for a human editor. Instant AI has none, so they
    // are hidden for it (the server ignores them too) and shown again when Human review is chosen.
    ["verbatim-fieldset", "verbatim-note", "advanced-settings", "speaker-label-field"].forEach((id) => { const el = $("#" + id); if (el) el.hidden = isAiTier; });
    if (isAiTier) { const spk = $('input[name="instr_speaker_ids"]'); if (spk) spk.checked = false; }     // speaker labels are an editor's work
    const cleanNote = $("#ai-clean-note");
    if (cleanNote) cleanNote.hidden = !isAiTier;
    if (isAiTier) { const clean = $('input[name="instr_verbatim_level"][value="clean_read"]'); if (clean) clean.checked = true; }
    // Studio is a human-reviewed order that bundles clean-up, Scribe recognition and (on a dub) a premium voice:
    // the extra boxes are ticked for the customer while it is chosen and put back when it is not.
    const isStudio = tier === "studio";
    // Instant AI translation can target any of the wider languages (Lara).
    // Picking another language while Transcription is chosen turns the order into a translation, so those languages stay selectable then.
    const aiTranslating = laraOn && isAiTier && !!selected && (selected.value === "translate" || selected.value === "transcribe");
    const humanTranslating = !isAiTier && !!selected;
    $all("#target-lang-field option.ai-target").forEach((o) => {
      o.disabled = !(aiTranslating || (humanTranslating && extraLangs.includes(o.value)));
    });
    const targetSelect = $("#target-lang-field select");
    if (targetSelect && targetSelect.selectedOptions[0] && targetSelect.selectedOptions[0].disabled) targetSelect.value = "same";
    // "Same language" is a plain transcript; any other language is a translation. The service follows the languages, and says so.
    const switchNote = $("#translation-switch-note");
    if (targetSelect) {
      const srcNow = ($("#source-lang-select") || {}).value;
      const svcNow = selected ? selected.value : "transcribe";
      let switched = false;
      if (svcNow === "transcribe" && targetSelect.value !== "same" && targetSelect.value !== srcNow) {
        const tr = $('input[name="service_level"][value="translate"]');
        if (tr && !tr.disabled) { tr.checked = true; selected = tr; switched = true; targetSelect.dataset.auto = "1"; } else targetSelect.value = "same";
      } else if (svcNow !== "transcribe" && (targetSelect.value === "same" || targetSelect.value === srcNow)) {
        const pick = srcNow === "en" ? "sw" : "en";
        const opt = targetSelect.querySelector('option[value="' + pick + '"]');
        if (opt && !opt.disabled) targetSelect.value = pick;
      }
      if (switchNote) { if (switched) switchNote.hidden = false; else if (!selected || selected.value === "transcribe" || targetSelect.value === "same") switchNote.hidden = true; }
    }
    if (targetField) targetField.hidden = aiOnlyLang && !aiTranslating;
    // Per-service rate for the chosen tier, from the account's own price card.
    $all('input[name="service_level"]').forEach((r) => {
      r.dataset.rate = isAiTier ? r.dataset.rateAi : r.dataset.rateHuman;
    });
    $all(".tier-price").forEach((el) => { el.textContent = "$" + (isAiTier ? el.dataset.ai : el.dataset.human); });
    // Rush = manually jumping the human review queue; an instant order has none.
    const rushField = $("#rush-checkbox");
    if (rushField) {
      const rushLabel = rushField.closest("label");
      if (rushLabel) rushLabel.hidden = isAiTier;
      if (isAiTier) rushField.checked = false;
    }

    const rate = selected ? Number(selected.dataset.rate) : 0;
    const serviceName = selected ? selected.dataset.name : "-";
    $("#price-service").textContent = kT(serviceName) + (isAiTier ? " - " + kT("Instant AI") : (isStudio ? " - " + kT("Studio") : " - " + kT("Human-reviewed")));
    // Mirrors billing.allowance_covers server-side - included minutes only ever
    // apply to an INSTANT AI order (transcription, and translation on paid plans), so the
    // estimate here has to match what create_order() will actually charge.
    const allowanceTranslate = data.dataset.allowanceTranslate === "1";      // paid plans: the included minutes cover translation too
    const freeMinutesEligible = !!selected && isAiTier && (selected.value === "transcribe" || (allowanceTranslate && selected.value === "translate"));

    // Voice cloning only ever runs on a full dub - no point asking for
    // cloning consent on a transcription/translation-only order.
    const consentField = $("#voice-clone-consent-field");
    if (consentField) consentField.hidden = !(selected && selected.value === "dub");

    // Same reasoning - a human voice actor only makes sense on a full dub,
    // and only on the human-reviewed tier (it's a human step).
    const humanVoiceField = $("#human-voice-field");
    const isDub = !!(selected && selected.value === "dub") && !isAiTier;
    if (humanVoiceField) humanVoiceField.hidden = !isDub;
    const humanVoiceBox = $("#human-voice-checkbox");
    const humanVoiceChecked = isDub && humanVoiceBox && humanVoiceBox.checked;
    $("#price-human-voice-row").hidden = !humanVoiceChecked;

    // Premium AI voice (ElevenLabs): a dub into English or Swahili only - no model speaks Kikuyu.
    const premiumField = $("#premium-voice-field");
    const targetSel = document.querySelector('select[name="target_lang"]');
    const premiumLangs = (data.dataset.premiumLangs || "en,sw").split(",").filter(Boolean);
    const premiumOk = !!(selected && selected.value === "dub") && !!targetSel && premiumLangs.includes(targetSel.value);
    if (premiumField) premiumField.hidden = !premiumOk;
    const premiumBox = $("#premium-voice-checkbox");
    if (premiumBox) {
      if (isStudio && premiumOk) { premiumBox.checked = true; premiumBox.dataset.forced = "1"; }
      else if (premiumBox.dataset.forced) { premiumBox.checked = false; delete premiumBox.dataset.forced; }
    }
    const premiumToggle = $("#premium-voice-toggle");
    if (premiumToggle) premiumToggle.hidden = isStudio;
    const premiumVoiceChecked = premiumOk && premiumBox && premiumBox.checked;
    const premiumPicker = $("#premium-voice-picker");
    if (premiumPicker) premiumPicker.hidden = !premiumVoiceChecked;
    if (premiumVoiceChecked) loadPremiumVoices();
    $("#price-premium-voice-row").hidden = !premiumVoiceChecked;

    // Audio clean-up applies to every service level.
    const cleanupBox = $("#audio-cleanup-checkbox");
    if (cleanupBox) {
      if (isStudio) { cleanupBox.checked = true; cleanupBox.dataset.forced = "1"; }
      else if (cleanupBox.dataset.forced) { cleanupBox.checked = false; delete cleanupBox.dataset.forced; }
    }
    const cleanupField = $("#audio-cleanup-field");
    if (cleanupField) cleanupField.hidden = isStudio;
    $("#price-studio-row").hidden = !isStudio;
    const cleanupChecked = !!(cleanupBox && cleanupBox.checked);
    $("#price-audio-cleanup-row").hidden = !cleanupChecked;

    const usingYoutube = $('#source-switch [data-source="youtube"]').classList.contains("active");
    const durationEl = $("#price-duration");
    if (usingYoutube) {
      durationEl.textContent = "confirmed after fetch";
    } else if (fileDurationMinutes != null) {
      const m = Math.floor(fileDurationMinutes);
      const s = Math.round((fileDurationMinutes - m) * 60);
      durationEl.textContent = m + ":" + String(s).padStart(2, "0") + " min";
    } else {
      durationEl.textContent = "—";
    }

    const addonBox = $("#addon-checkbox");
    const addonChecked = addonBox && addonBox.checked;
    $("#price-addon-row").hidden = !addonChecked;

    const langSelect = $("#source-lang-select");
    const manualLangs = langSelect ? (langSelect.dataset.manualLangs || "").split(",").filter(Boolean) : [];
    const isManual = langSelect && manualLangs.includes(langSelect.value);
    $("#price-manual-row").hidden = !isManual;

    const rushBox = $("#rush-checkbox");
    const rushChecked = rushBox && rushBox.checked;
    $("#price-rush-row").hidden = !rushChecked;

    if (fileDurationMinutes == null || usingYoutube) {
      // No real duration yet - show the rate itself so the page isn't
      // just blank, but don't pretend to total something unknown. Still
      // has to fold in any addon/surcharge that's already known to apply
      // (checked video add-on, manual-transcription language, or rush) -
      // showing just the bare service rate here understates the real
      // per-minute cost the moment any of those is active.
      $("#price-free").textContent = "-";
      let perMinExtras = (addonChecked ? addonRate : 0) + (isManual ? manualRate : 0)
        + (humanVoiceChecked ? humanVoiceRate : 0) + (premiumVoiceChecked ? premiumVoiceRate : 0)
        + (cleanupChecked ? cleanupUnit : 0) + (isStudio ? premiumAsrRate : 0);
      if (rushChecked) perMinExtras += rate * rushPct;
      $("#price-total").textContent = formatMoney(rate + perMinExtras) + "/min";
      if (addonChecked) $("#price-addon").textContent = formatMoney(addonRate) + "/min";
      if (isManual) $("#price-manual").textContent = formatMoney(manualRate) + "/min";
      if (humanVoiceChecked) $("#price-human-voice").textContent = formatMoney(humanVoiceRate) + "/min";
      if (premiumVoiceChecked) $("#price-premium-voice").textContent = formatMoney(premiumVoiceRate) + "/min";
      if (cleanupChecked) $("#price-audio-cleanup").textContent = formatMoney(cleanupUnit) + "/min";
      if (isStudio) $("#price-studio").textContent = formatMoney(premiumAsrRate) + "/min";
      if (rushChecked) $("#price-rush").textContent = formatMoney(rate * rushPct) + "/min";
      return;
    }

    // Mirrors billing.order_cost_usd's real waterfall: free minutes first,
    // then the plan discount, THEN prepaid credits as real dollar value
    // against that discounted base (never against add-ons or rush - those
    // are priced on the full minute count/value, matching the server).
    // Human-reviewed transcription: the plan's included human minutes come off first (server: billing.human_allowance_covers).
    const humanIncludedEligible = !!selected && !isAiTier && !isStudio && selected.value === "transcribe";
    const freeApplied = freeMinutesEligible ? Math.min(fileDurationMinutes, Math.max(0, freeMinutes))
      : (humanIncludedEligible ? Math.min(fileDurationMinutes, Math.max(0, humanMinutes)) : 0);
    const billableMinutes = Math.max(0, fileDurationMinutes - freeApplied);
    const gross = billableMinutes * rate;
    const discountAmount = gross * discount;
    const afterDiscount = gross - discountAmount;
    const creditsValueUsd = Math.max(0, walletCredits) / CREDITS_PER_DOLLAR;
    const creditsAppliedUsd = Math.min(afterDiscount, creditsValueUsd);
    const addonCost = addonChecked ? addonRate * fileDurationMinutes : 0;
    // Manual-transcription surcharge, like video_deliverables, is priced
    // against the FULL minute count - see billing.order_cost_usd's own
    // comment on why add-ons aren't reduced by free minutes/credits.
    const manualCost = isManual ? manualRate * fileDurationMinutes : 0;
    const humanVoiceCost = humanVoiceChecked ? humanVoiceRate * fileDurationMinutes : 0;
    const premiumVoiceCost = premiumVoiceChecked ? premiumVoiceRate * fileDurationMinutes : 0;
    const cleanupCost = cleanupChecked ? cleanupUnit * fileDurationMinutes : 0;
    const studioAsrCost = isStudio ? premiumAsrRate * fileDurationMinutes : 0;
    const rushSurcharge = rushChecked ? (fileDurationMinutes * rate) * rushPct : 0;
    const total = afterDiscount - creditsAppliedUsd + addonCost + manualCost + humanVoiceCost + premiumVoiceCost + cleanupCost + studioAsrCost + rushSurcharge;

    $("#price-free").textContent = freeApplied > 0 ? freeApplied.toFixed(1) + " min"
      : ((freeMinutesEligible || (humanIncludedEligible && humanMinutes > 0)) ? "none applied" : "not eligible");
    $("#price-wallet-row").hidden = creditsAppliedUsd <= 0;
    if (creditsAppliedUsd > 0) {
      $("#price-wallet").textContent = Math.round(creditsAppliedUsd * CREDITS_PER_DOLLAR) + " credits (" + formatMoney(creditsAppliedUsd) + ")";
    }
    if (addonChecked) $("#price-addon").textContent = formatMoney(addonCost);
    if (isManual) $("#price-manual").textContent = formatMoney(manualCost);
    if (humanVoiceChecked) $("#price-human-voice").textContent = formatMoney(humanVoiceCost);
    if (premiumVoiceChecked) $("#price-premium-voice").textContent = formatMoney(premiumVoiceCost);
    if (cleanupChecked) $("#price-audio-cleanup").textContent = formatMoney(cleanupCost);
    if (isStudio) $("#price-studio").textContent = formatMoney(studioAsrCost);
    if (rushChecked) $("#price-rush").textContent = formatMoney(rushSurcharge);
    $("#price-total").textContent = formatMoney(Math.max(0, total));
  }

  // The premium voice list comes from the server (ElevenLabs' ready-made voices); fetched once, on first use.
  let premiumVoicesLoaded = false;
  function loadPremiumVoices() {
    const select = $("#premium-voice-select");
    if (!select || premiumVoicesLoaded) return;
    premiumVoicesLoaded = true;
    fetch("/account/premium-voices", { credentials: "same-origin" })
      .then((r) => r.json())
      .then((data) => {
        const keep = select.dataset.selected || "";
        select.innerHTML = "";
        if (!data.voices || !data.voices.length) {
          const o = document.createElement("option");
          o.value = ""; o.textContent = kT("Premium voices are not available right now");
          select.appendChild(o);
          return;
        }
        data.voices.forEach((v, i) => {
          const o = document.createElement("option");
          o.value = v.id;
          o.textContent = v.name + (v.description ? " - " + v.description : "");
          if (v.id === keep || (!keep && i === 0)) o.selected = true;
          select.appendChild(o);
        });
      })
      .catch(() => { premiumVoicesLoaded = false; });
  }

  function bindPricing() {
    $all('input[name="service_level"]').forEach((r) => r.addEventListener("change", updatePricing));
    $all('input[name="review_tier"]').forEach((r) => r.addEventListener("change", updatePricing));
    const addonBox = $("#addon-checkbox");
    if (addonBox) addonBox.addEventListener("change", updatePricing);
    const humanVoiceBoxEl = $("#human-voice-checkbox");
    if (humanVoiceBoxEl) humanVoiceBoxEl.addEventListener("change", updatePricing);
    const premiumBoxEl = $("#premium-voice-checkbox");
    if (premiumBoxEl) premiumBoxEl.addEventListener("change", updatePricing);
    const cleanupBoxEl = $("#audio-cleanup-checkbox");
    if (cleanupBoxEl) cleanupBoxEl.addEventListener("change", updatePricing);
    const targetSelEl = document.querySelector('select[name="target_lang"]');
    // The client's explicit choice wins: choosing Transcription means "same language", and choosing "Same language" puts a service the
    // wizard switched by itself back to Transcription. These run first (capture) so the sync above does not undo them.
    document.addEventListener("change", (e) => {
      const el = e.target;
      if (!el || !targetSelEl) return;
      if (el.name === "service_level" && el.value === "transcribe") { targetSelEl.value = "same"; delete targetSelEl.dataset.auto; }
      if (el === targetSelEl && el.value === "same" && el.dataset.auto) {
        const tr = $('input[name="service_level"][value="transcribe"]');
        if (tr && !tr.disabled) tr.checked = true;
        delete el.dataset.auto;
      }
    }, true);
    if (targetSelEl) targetSelEl.addEventListener("change", updatePricing);
    const rushBox = $("#rush-checkbox");
    if (rushBox) rushBox.addEventListener("change", updatePricing);
    const langSelect = $("#source-lang-select");
    if (langSelect) langSelect.addEventListener("change", updatePricing);
  }

  // Reads the language codes straight off the select's own data attribute
  // (rendered server-side from app.py's MANUAL_TRANSCRIPTION_LANGUAGES)
  // instead of hardcoding "ki" here too - one source of truth for which
  // languages get the manual-transcription note.
  function bindManualTranscriptionNote() {
    const select = $("#source-lang-select");
    const note = $("#manual-transcription-note");
    if (!select || !note) return;
    const manualLangs = (select.dataset.manualLangs || "").split(",").filter(Boolean);
    function update() { note.hidden = !manualLangs.includes(select.value); }
    select.addEventListener("change", update);
    update();
  }

  // -------------------------------------------------------------- init ----
  // One key per page load, sent with the form - a resubmit (double-click,
  // a retried request after a slow/dropped connection) carries the SAME
  // key, so the backend recognizes it as the same attempt instead of
  // creating (and charging for) a second order. crypto.randomUUID needs
  // no polyfill in any browser this app already targets.
  function bindIdempotencyKey() {
    const field = $("#idempotency-key-field");
    if (field && window.crypto && crypto.randomUUID) field.value = crypto.randomUUID();
  }

  // --------------------------------------------------- upload progress ----
  // Real percentage for a file upload (XHR exposes real byte progress -
  // nothing fabricated). For a YouTube link there's no equivalent: the
  // fetch happens server-side inside the same request/response cycle, so
  // there's no byte count to report yet - an honest "still working"
  // indicator beats a fake percentage we can't actually back up.
  //
  // A selected FILE (not a YouTube link) goes through two real requests,
  // not one:
  //   1. POST /account/orders/presign-upload - tiny JSON request/response,
  //      goes through this app and the Tunnel same as always (no size
  //      concern - it carries no file bytes).
  //   2. PUT the raw file straight to the presigned R2 URL - never
  //      touches this app or the Cloudflare Tunnel in front of it at
  //      all, so the Tunnel's 100MB-per-request body cap (confirmed as
  //      the real cause of big uploads stalling before ever reaching
  //      this app) simply doesn't apply to this request.
  // Only once that PUT succeeds does the real order-creation POST go out
  // - carrying the R2 object's key instead of the file itself, so that
  // request is small too. Same visible progress bar and same
  // document.write full-page-swap finish as before either way.
  function bindUploadProgress() {
    const form = $("#order-form");
    const progressWrap = $("#wizard-upload-progress");
    const progressLabel = $("#wizard-upload-progress-label");
    const progressFill = $("#wizard-upload-progress-fill");
    const submitBtn = $("#wizard-submit");
    if (!form || !progressWrap || !window.XMLHttpRequest) return;

    // After the bytes are up, the server still has real work to do before the order page can open. Show it as steps so
    // nobody thinks the page has frozen. For a file the steps move on a timer; for a YouTube link the first step (the
    // download) can take minutes, so it stays on that step until the server answers.
    let stepTimer = null;
    // What the last step honestly says. An order that has to be paid for first is not "started" yet: it goes to payment. One that is free or
    // covered by the plan does start. With a link whose length is not known yet there is no price to go by, so the step stays neutral.
    function finalStepLabel() {
      const el = $("#price-total");
      const text = el ? el.textContent : "";
      if (!text || /\/\s*min/i.test(text)) return kT("Confirming your order");
      const amount = parseFloat(text.replace(/[^0-9.]/g, ""));
      return amount > 0.004 ? kT("Preparing your payment") : kT("Starting your order");
    }
    function startFinalizeSteps(first, auto) {
      let list = $("#wizard-finalize-steps");
      if (!list) {
        list = document.createElement("ol");
        list.id = "wizard-finalize-steps"; list.className = "wizard-steps";
        progressWrap.appendChild(list);
      }
      const labels = [first, kT("Checking your audio"), kT("Working out your price"), finalStepLabel()];
      let i = auto ? 1 : 0;
      function paint() {
        list.textContent = "";
        labels.forEach((t, k) => {
          const li = document.createElement("li");
          li.className = k < i ? "is-done" : (k === i ? "is-current" : "");
          li.textContent = t;
          list.appendChild(li);
        });
      }
      paint();
      progressFill.classList.add("indeterminate");
      progressLabel.textContent = kT("Setting up your order");
      clearInterval(stepTimer);
      if (auto) stepTimer = setInterval(() => { if (i < labels.length - 1) { i += 1; paint(); } }, 2200);
    }
    function stopFinalizeSteps() {
      clearInterval(stepTimer); stepTimer = null;
      const list = $("#wizard-finalize-steps");
      if (list) list.remove();
      progressFill.classList.remove("indeterminate");
    }

    function failUpload(message) {
      progressLabel.textContent = message;
      if (submitBtn) submitBtn.disabled = false;
      kpUp({ active: false, error: message });
    }

    // Back on this page while an upload started earlier is still running (the person looked at something else and came back):
    // show where it has got to, and finish the job when it ends.
    function attachToRunningUpload() {
      const up = window.kpUploads;
      if (!up) return;
      if (!up.active && up.url) {
        progressWrap.hidden = false;
        progressLabel.textContent = kT("Your last upload finished and the order has started.") + " ";
        const a = document.createElement("a"); a.href = up.url; a.textContent = kT("Open it");
        progressLabel.appendChild(a);
        up.reset();
        return;
      }
      if (!up.active) return;
      progressWrap.hidden = false;
      if (submitBtn) submitBtn.disabled = true;
      const paint = () => {
        progressFill.style.width = (up.pct || 0) + "%";
        progressLabel.textContent = (up.label || "Uploading") + (up.pct ? " " + up.pct + "%" : "");
      };
      paint();
      const listener = () => {
        if (!document.getElementById("order-form")) { up.off(listener); return; }
        if (up.active) { paint(); return; }
        up.off(listener);
        if (up.url) window.location.href = up.url;
        else { progressLabel.textContent = up.error || "The upload stopped."; if (submitBtn) submitBtn.disabled = false; }
      };
      up.on(listener);
    }

    function finalizeOrder(formData, first, auto) {
      startFinalizeSteps(first || kT("Upload complete"), auto !== false);
      const xhr = new XMLHttpRequest();
      xhr.open("POST", form.action, true);
      xhr.onload = function () {
        // Same final HTML the server would have sent a normal form
        // submission to (the order page on success, the dashboard with a
        // real error message if something went wrong) - just reached
        // without a second full-page round trip, so the progress bar
        // stays visible right up until the real result is ready.
        //
        // Real bug this fixes: on a VALIDATION error, the server renders
        // the dashboard directly at the POST target (/account/orders,
        // no redirect) - xhr.responseURL in that case is just that same
        // POST-only URL, which has no GET handler at all. Blindly
        // replaceState-ing to it left the address bar pointed at a URL
        // that 405'd on refresh - confirmed live ("Method Not Allowed"
        // reported by a real client). A real success always goes through
        // a redirect first, so xhr.responseURL differs from form.action
        // in that case; only fall back to the real, always-GET-able
        // dashboard URL when it doesn't.
        var landedUrl = (xhr.responseURL && xhr.responseURL !== form.action) ? xhr.responseURL : "/account";
        // A real redirect means success - the order really was created,
        // so the saved upload_key is spent and safe to forget. An error
        // (no redirect) leaves it in place so "Continue with this file"
        // is still there for a genuine retry.
        if (landedUrl !== "/account") { clearResumableUpload(); logWizardEvent("submitted"); clearStaged(); kpUp({ active: false, url: landedUrl }); }
        else kpUp({ active: false, error: "Your order could not be started." });
        if (!stillHere(form)) return;               // they moved on: the tray tells them, and the page they are on is left alone
        if (landedUrl !== "/account") {
          // Success: the order exists and its page (the payment page, or the order itself) is an ordinary address. Go there the normal way, so this page
          // stays exactly as it is until the next one is ready. Rewriting the whole document in place dropped the styles for a moment and let
          // unsized icons fill the screen.
          window.location.replace(landedUrl);
          return;
        }
        // A problem with the form: the server answered with the form page itself, shown in place with the message.
        history.replaceState(null, "", landedUrl);
        document.open();
        document.write(xhr.responseText);
        document.close();
      };
      xhr.onerror = function () {
        stopFinalizeSteps();
        failUpload("Something went wrong submitting - please try again.");
      };
      xhr.send(formData);
    }

    function uploadFileDirectToR2(file, formData) {
      const presignBody = new FormData();
      presignBody.append("filename", file.name);
      presignBody.append("content_type", file.type || "application/octet-stream");
      const presignXhr = new XMLHttpRequest();
      presignXhr.open("POST", "/account/orders/presign-upload", true);
      uploadTransferActive = true;
      presignXhr.onload = function () {
        let data;
        try {
          data = JSON.parse(presignXhr.responseText);
        } catch (err) {
          data = null;
        }
        if (presignXhr.status !== 200 || !data || !data.put_url) {
          uploadTransferActive = false;
          failUpload((data && data.error) || "Couldn't prepare the upload - please try again.");
          return;
        }
        const putXhr = new XMLHttpRequest();
        putXhr.open("PUT", data.put_url, true);
        putXhr.setRequestHeader("Content-Type", file.type || "application/octet-stream");
        putXhr.upload.addEventListener("progress", function (ev) {
          if (!ev.lengthComputable) return;
          const pct = Math.round((ev.loaded / ev.total) * 100);
          progressFill.style.width = pct + "%";
          progressLabel.textContent = kT("Uploading… {pct}%", { pct: pct });
          kpUp({ pct: pct });
        });
        putXhr.onload = function () {
          uploadTransferActive = false;
          if (putXhr.status < 200 || putXhr.status >= 300) {
            failUpload("Upload failed - please try again.");
            return;
          }
          progressFill.style.width = "100%";
          // Safely on R2 now - a refresh from here on only needs to skip
          // straight to "Continue with this file", never re-upload it.
          saveResumableUpload(data.upload_key, file.name);
          formData.delete("audio");
          formData.set("upload_key", data.upload_key);
          finalizeOrder(formData, kT("Upload complete"), true);
        };
        putXhr.onerror = function () {
          uploadTransferActive = false;
          failUpload("Upload failed - please try again.");
        };
        putXhr.send(file);
      };
      presignXhr.onerror = function () {
        uploadTransferActive = false;
        failUpload("Couldn't prepare the upload - please try again.");
      };
      presignXhr.send(presignBody);
    }

    // Warn before an accidental refresh/close ONLY while raw file bytes
    // are actually in flight - once the R2 PUT finishes, upload_key is
    // already safely saved (see saveResumableUpload) and there's nothing
    // left to lose, so this deliberately stops guarding at that point.
    window.addEventListener("beforeunload", function (e) {
      if (!uploadTransferActive) return;
      e.preventDefault();
      e.returnValue = "";
    });

    function send(method, url, body, onProgress, headers) {
      return new Promise((resolve, reject) => {
        const x = new XMLHttpRequest();
        x.open(method, url, true);
        Object.keys(headers || {}).forEach((h) => x.setRequestHeader(h, headers[h]));
        if (onProgress && x.upload) x.upload.addEventListener("progress", (ev) => { if (ev.lengthComputable) onProgress(Math.round((ev.loaded / ev.total) * 100)); });
        x.onload = () => resolve(x);
        x.onerror = () => reject(new Error("Network problem. Check your connection and try again."));
        x.send(body);
      });
    }

    function errorFrom(html) {
      const m = /class="error"[^>]*>([\s\S]*?)<\/p>/.exec(html || "");
      if (!m) return "This file could not be submitted.";
      const t = document.createElement("textarea");
      t.innerHTML = m[1].replace(/<[^>]+>/g, "");
      return t.value.trim() || "This file could not be submitted.";
    }

    async function submitBatch() {
      const files = fileQueue.slice();
      const bar = (done, cur) => {
        const pct = Math.round(((done + cur / 100) / files.length) * 100);
        progressFill.style.width = pct + "%";
        kpUp({ pct: pct, label: kT("Uploading {n} files", { n: files.length }) });
      };
      progressFill.classList.remove("indeterminate");
      let okCount = 0, lastUrl = "";
      const failed = [];
      for (let i = 0; i < files.length; i++) {
        const f = files[i];
        const setRow = (text, tone) => { fileState.set(f, { text: text, tone: tone || "" }); renderQueue(); };
        progressLabel.textContent = kT("Sending file {i} of {n}", { i: i + 1, n: files.length });
        setRow("Uploading 0%", "busy");
        try {
          const pre = new FormData();
          pre.append("filename", f.name);
          pre.append("content_type", f.type || "application/octet-stream");
          const px = await send("POST", "/account/orders/presign-upload", pre);
          let data = null;
          try { data = JSON.parse(px.responseText); } catch (err) { data = null; }
          if (px.status !== 200 || !data || !data.put_url) throw new Error((data && data.error) || "Could not prepare the upload.");
          uploadTransferActive = true;
          const put = await send("PUT", data.put_url, f, (pct) => { setRow("Uploading " + pct + "%", "busy"); bar(i, pct); },
                                 { "Content-Type": f.type || "application/octet-stream" });
          uploadTransferActive = false;
          if (put.status < 200 || put.status >= 300) throw new Error("The upload did not finish.");
          setRow(finalStepLabel(), "busy");
          const fd = new FormData(form);
          fd.delete("audio");
          fd.set("upload_key", data.upload_key);
          fd.set("idempotency_key", window.crypto && crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + "-" + i);
          const res = await send("POST", form.action, fd);
          const landed = res.responseURL || "";
          if (landed && landed !== form.action && /\/account\/orders\/[^/?#]+/.test(landed)) {
            okCount += 1; lastUrl = landed; setRow("Submitted", "ok");
          } else {
            throw new Error(errorFrom(res.responseText));
          }
        } catch (err) {
          uploadTransferActive = false;
          setRow(err.message, "bad");
          failed.push(f);
        }
        bar(i + 1, 0);
      }
      if (!failed.length) {
        progressLabel.textContent = okCount + " orders started";
        const dest = okCount === 1 ? lastUrl : "/account/pending";
        clearStaged();
        kpUp({ active: false, url: dest });
        if (stillHere(form)) window.location.href = dest;
        return;
      }
      progressLabel.textContent = okCount + " started, " + failed.length + " did not go through. Fix or remove those and press Submit again.";
      kpUp({ active: false, error: okCount + " started, " + failed.length + " did not go through." });
      fileQueue = failed;
      failed.forEach((f) => { const st = fileState.get(f); if (st) st.tone = "bad"; });
      renderQueue();
      if (submitBtn) submitBtn.disabled = false;
    }

    // Asks the server what this one file will cost (it only needs the length the browser has already read, never the file). Resolves to the
    // pay-first page's address when payment is due before upload, otherwise to null: the order carries on and uploads as before.
    // Any trouble asking also resolves to null, so a problem here can never stop an order.
    function askPrepay() {
      return new Promise((resolve) => {
        if (fileQueue.length !== 1 || fileDurationMinutes == null) { resolve(null); return; }
        const fd = new FormData(form);
        fd.delete("audio"); fd.delete("style_guide");
        fd.set("minutes", String(fileDurationMinutes));
        fd.set("name", fileQueue[0].name);
        fetch("/account/orders/quote", { method: "POST", body: fd, credentials: "same-origin" })
          .then((r) => (r.ok ? r.json() : null))
          .then((d) => resolve(d && d.can_prepay && d.needs_payment && d.prepay_url ? d.prepay_url : null))
          .catch(() => resolve(null));
      });
    }

    form.addEventListener("submit", function (e) {
      e.preventDefault();
      const youtubeActive = $('#source-switch [data-source="youtube"]').classList.contains("active");
      const usingFile = fileQueue.length > 0 && !youtubeActive;
      const formData = new FormData(form);
      kpUp({ active: true, label: usingFile ? (fileQueue.length > 1 ? kT("Uploading {n} files", { n: fileQueue.length }) : kT("Uploading {name}", { name: fileQueue[0].name })) : finalStepLabel(),
             pct: 0, url: null, error: null });

      progressWrap.hidden = false;
      progressFill.classList.remove("indeterminate");
      progressFill.style.width = "0%";
      if (submitBtn) submitBtn.disabled = true;

      if (usingFile && fileQueue.length > 1) {
        submitBatch();
      } else if (usingFile) {
        // Pay first, then upload: when this order is going to need payment, the customer pays before a single byte of the file goes up.
        progressLabel.textContent = kT("Checking your price");
        askPrepay().then((payUrl) => {
          if (payUrl) { kpUp({ active: false, error: null }); window.location.href = payUrl; return; }
          uploadFileDirectToR2(fileQueue[0], formData);
        });
      } else if (resumedUploadKey) {
        // Already safely on R2 from before a refresh - skip straight to
        // finalize, no re-upload.
        progressFill.style.width = "100%";
        formData.delete("audio");
        formData.set("upload_key", resumedUploadKey);
        finalizeOrder(formData, kT("Upload complete"), true);
      } else {
        finalizeOrder(formData, "Fetching your video from YouTube", false);
        progressLabel.textContent = kT("Fetching from YouTube. Longer videos take a few minutes, and it has not hung.");
      }
    });

    offerResumableUploadIfAny();
    attachToRunningUpload();
  }

  // ------------------------------------------------ where they left off ----
  // The choices already made and the step they were on come back through persist.js; the files they picked come back from the
  // browser's own storage, so nothing has to be chosen or uploaded again until they press Submit.
  function applyPendingStep() {
    if (!pendingStep || pendingStep < 2) return;
    const yt = $("#youtube-url-input");
    const usingYt = yt && yt.value.trim() && $('#source-switch [data-source="youtube"]').classList.contains("active");
    if (fileQueue.length || resumedUploadKey || usingYt) {
      const n = Math.min(pendingStep, TOTAL_STEPS);
      pendingStep = null;
      showStep(n);
    }
  }
  function registerDraftHooks() {
    if (!window.kpDrafts) return;
    window.kpDrafts.hooks["order-form"] = {
      get: () => ({ step: currentStep }),
      set: (o) => { pendingStep = o && o.step; applyPendingStep(); },
    };
  }
  // Back from paying first (?prepaid=1): the file was waiting in the browser, and now goes up on its own. If the browser no longer has it,
  // the customer is told to choose it again, and that nothing more will be charged.
  function cameBackFromPaying() { return new URLSearchParams(window.location.search).get("prepaid") === "1"; }
  function prepaidBanner(text) {
    const form = $("#order-form");
    if (!form) return;
    let p = $("#prepaid-banner");
    if (!p) { p = document.createElement("p"); p.id = "prepaid-banner"; p.className = "ok"; p.setAttribute("role", "status"); form.insertBefore(p, form.firstChild); }
    p.textContent = text;
  }
  function continueAfterPaying() {
    prepaidBanner(kT("Payment received. Uploading your file now."));
    showStep(TOTAL_STEPS);
    setTimeout(() => { const b = $("#wizard-submit"); if (b && !b.disabled) b.click(); }, 1200);
  }
  function restoreStagedFiles() {
    if (!window.kpFiles || (window.kpUploads && window.kpUploads.active)) { if (cameBackFromPaying()) prepaidBanner(kT("Payment received. Choose your file and press Submit. You will not be charged again.")); return; }
    window.kpFiles.list("order").then((files) => {
      if (!files.length || fileQueue.length) { if (cameBackFromPaying() && !fileQueue.length) prepaidBanner(kT("Payment received. Choose your file and press Submit. You will not be charged again.")); return; }
      addFiles(files, true);
      const note = $("#file-queue-note");
      if (note) note.textContent = files.length > 1 ? kT("Your files are still here from before. Nothing is uploaded until you press Submit.") : kT("Your file is still here from before. Nothing is uploaded until you press Submit.");
      applyPendingStep();
      if (cameBackFromPaying() && files.length === 1) continueAfterPaying();
    });
  }

  function init() {
    if (!$("#order-form")) return;
    bindWizardNav();
    bindSourceSwitch();
    bindDropzone();
    bindPricing();
    bindIdempotencyKey();
    bindManualTranscriptionNote();
    bindUploadProgress();
    registerDraftHooks();
    restoreStagedFiles();
    showStep(1);
    // the dashboard's "Paste a YouTube link" card lands here with ?source=youtube
    if (new URLSearchParams(window.location.search).get("source") === "youtube") {
      const yt = $('#source-switch [data-source="youtube"]');
      if (yt) yt.click();
    }
  }
  // Run now when the page was swapped in place (the DOMContentLoaded of a page that is already loaded never comes again).
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
})();
