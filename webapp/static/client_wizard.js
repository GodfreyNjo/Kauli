/* Client job-submission wizard: 3 steps over one real <form> (still a
 * single POST to /client/orders - nothing here changes what the backend
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
    continueBtn.textContent = "Continue with this file";
    const discardBtn = document.createElement("button");
    discardBtn.type = "button";
    discardBtn.className = "secondary";
    discardBtn.style.marginTop = "8px";
    discardBtn.style.marginLeft = "8px";
    discardBtn.textContent = "Discard";
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
        navigator.sendBeacon("/client/wizard-event", new Blob([body], { type: "application/x-www-form-urlencoded" }));
      } else if (window.fetch) {
        fetch("/client/wizard-event", {
          method: "POST",
          headers: { "Content-Type": "application/x-www-form-urlencoded" },
          body: body, keepalive: true,
        }).catch(function () {});
      }
    } catch (e) { /* friction logging must never break the actual wizard */ }
  }

  function showStep(n) {
    currentStep = n;
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
    const file = $("#file-input").files[0];
    set("rv-file", usingYoutube ? ($("#youtube-url-input").value.trim() || "-")
                                : (file ? file.name : (resumedUploadKey ? "Your uploaded file" : "-")));
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
    const tierName = tier ? (tier.value === "ai" ? "Instant AI" : "Human-reviewed") : "";
    set("rv-service", (svc ? svc.dataset.name : "-") + (tierName ? " - " + tierName : ""));
    const total = $("#price-total");
    set("rv-total", total ? total.textContent : "-");
  }

  function step1Valid() {
    const usingYoutube = $('#source-switch [data-source="youtube"]').classList.contains("active");
    if (usingYoutube) {
      const url = $("#youtube-url-input").value.trim();
      if (!url) { alert("Paste a YouTube link first."); return false; }
      return true;
    }
    const file = $("#file-input").files[0];
    if (!file && !resumedUploadKey) { alert("Choose a file first."); return false; }
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

  function handleFile(file) {
    const dz = $("#dropzone");
    const text = $("#dropzone-text");
    if (!file) return;
    const ytField = $("#youtube-url-input");
    if (ytField) ytField.value = "";
    $('#source-switch [data-source="file"]').click();
    dz.classList.add("has-file");
    text.textContent = file.name;
    fileDurationMinutes = null;
    probeDuration(file).then((mins) => {
      fileDurationMinutes = mins;
      updatePricing();
    });
  }

  function bindDropzone() {
    const dz = $("#dropzone");
    const input = $("#file-input");
    if (!dz || !input) return;
    dz.addEventListener("click", () => input.click());
    dz.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); }
    });
    input.addEventListener("change", () => handleFile(input.files[0]));

    ["dragenter", "dragover"].forEach((evt) => {
      dz.addEventListener(evt, (e) => { e.preventDefault(); dz.classList.add("dragover"); });
    });
    ["dragleave", "drop"].forEach((evt) => {
      dz.addEventListener(evt, (e) => { e.preventDefault(); dz.classList.remove("dragover"); });
    });
    dz.addEventListener("drop", (e) => {
      const file = e.dataTransfer.files[0];
      if (file) {
        input.files = e.dataTransfer.files;
        handleFile(file);
      }
    });
  }

  // ------------------------------------------------------ live pricing ----
  function formatMoney(n) { return "$" + n.toFixed(2); }

  function updatePricing() {
    const data = $("#pricing-data");
    if (!data) return;
    const discount = Number(data.dataset.discount) || 0;
    const freeMinutes = Number(data.dataset.freeMinutes) || 0;
    const walletCredits = Number(data.dataset.walletCredits) || 0;
    const addonRate = Number(data.dataset.addonRate) || 0;
    const manualRate = Number(data.dataset.manualTranscriptionRate) || 0;
    const humanVoiceRate = Number(data.dataset.humanVoiceRate) || 0;
    const premiumVoiceRate = Number(data.dataset.premiumVoiceRate) || 0;
    const audioCleanupRate = Number(data.dataset.audioCleanupRate) || 0;
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
    const tierRadio = $('input[name="review_tier"]:checked');
    const tier = tierRadio ? tierRadio.value : "human";
    const isAiTier = tier === "ai";
    // Studio is a human-reviewed order that bundles clean-up, Scribe recognition and (on a dub) a premium voice:
    // the extra boxes are ticked for the customer while it is chosen and put back when it is not.
    const isStudio = tier === "studio";
    // Instant AI translation can target any of the wider languages (Lara).
    const aiTranslating = laraOn && isAiTier && !!selected && selected.value === "translate";
    const humanTranslating = !isAiTier && !!selected && selected.value !== "transcribe";
    $all("#target-lang-field option.ai-target").forEach((o) => {
      o.disabled = !(aiTranslating || (humanTranslating && extraLangs.includes(o.value)));
    });
    const targetSelect = $("#target-lang-field select");
    if (targetSelect && targetSelect.selectedOptions[0] && targetSelect.selectedOptions[0].disabled) targetSelect.value = "en";
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
    $("#price-service").textContent = serviceName + (isAiTier ? " - Instant AI" : (isStudio ? " - Studio" : " - Human-reviewed"));
    // Mirrors billing.FREE_MINUTES_SERVICE_LEVEL server-side - free minutes
    // only ever apply to an INSTANT AI transcription-only order, so the
    // estimate here has to match what create_order() will actually charge.
    const freeMinutesEligible = selected && selected.value === "transcribe" && isAiTier;

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
        + (cleanupChecked ? audioCleanupRate : 0) + (isStudio ? premiumAsrRate : 0);
      if (rushChecked) perMinExtras += rate * rushPct;
      $("#price-total").textContent = formatMoney(rate + perMinExtras) + "/min";
      if (addonChecked) $("#price-addon").textContent = formatMoney(addonRate) + "/min";
      if (isManual) $("#price-manual").textContent = formatMoney(manualRate) + "/min";
      if (humanVoiceChecked) $("#price-human-voice").textContent = formatMoney(humanVoiceRate) + "/min";
      if (premiumVoiceChecked) $("#price-premium-voice").textContent = formatMoney(premiumVoiceRate) + "/min";
      if (cleanupChecked) $("#price-audio-cleanup").textContent = formatMoney(audioCleanupRate) + "/min";
      if (isStudio) $("#price-studio").textContent = formatMoney(premiumAsrRate) + "/min";
      if (rushChecked) $("#price-rush").textContent = formatMoney(rate * rushPct) + "/min";
      return;
    }

    // Mirrors billing.order_cost_usd's real waterfall: free minutes first,
    // then the plan discount, THEN prepaid credits as real dollar value
    // against that discounted base (never against add-ons or rush - those
    // are priced on the full minute count/value, matching the server).
    const freeApplied = freeMinutesEligible ? Math.min(fileDurationMinutes, Math.max(0, freeMinutes)) : 0;
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
    const cleanupCost = cleanupChecked ? audioCleanupRate * fileDurationMinutes : 0;
    const studioAsrCost = isStudio ? premiumAsrRate * fileDurationMinutes : 0;
    const rushSurcharge = rushChecked ? (fileDurationMinutes * rate) * rushPct : 0;
    const total = afterDiscount - creditsAppliedUsd + addonCost + manualCost + humanVoiceCost + premiumVoiceCost + cleanupCost + studioAsrCost + rushSurcharge;

    $("#price-free").textContent = freeApplied > 0 ? freeApplied.toFixed(1) + " min"
      : (freeMinutesEligible ? "none applied" : "not eligible (transcription-only)");
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
    fetch("/client/premium-voices", { credentials: "same-origin" })
      .then((r) => r.json())
      .then((data) => {
        const keep = select.dataset.selected || "";
        select.innerHTML = "";
        if (!data.voices || !data.voices.length) {
          const o = document.createElement("option");
          o.value = ""; o.textContent = "Premium voices are not available right now";
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
  //   1. POST /client/orders/presign-upload - tiny JSON request/response,
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

    function finalizeOrder(formData) {
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
        // the dashboard directly at the POST target (/client/orders,
        // no redirect) - xhr.responseURL in that case is just that same
        // POST-only URL, which has no GET handler at all. Blindly
        // replaceState-ing to it left the address bar pointed at a URL
        // that 405'd on refresh - confirmed live ("Method Not Allowed"
        // reported by a real client). A real success always goes through
        // a redirect first, so xhr.responseURL differs from form.action
        // in that case; only fall back to the real, always-GET-able
        // dashboard URL when it doesn't.
        var landedUrl = (xhr.responseURL && xhr.responseURL !== form.action) ? xhr.responseURL : "/client";
        // A real redirect means success - the order really was created,
        // so the saved upload_key is spent and safe to forget. An error
        // (no redirect) leaves it in place so "Continue with this file"
        // is still there for a genuine retry.
        if (landedUrl !== "/client") { clearResumableUpload(); logWizardEvent("submitted"); }
        history.replaceState(null, "", landedUrl);
        document.open();
        document.write(xhr.responseText);
        document.close();
      };
      xhr.onerror = function () {
        progressLabel.textContent = "Something went wrong submitting - please try again.";
        if (submitBtn) submitBtn.disabled = false;
      };
      xhr.send(formData);
    }

    function uploadFileDirectToR2(file, formData) {
      const presignBody = new FormData();
      presignBody.append("filename", file.name);
      presignBody.append("content_type", file.type || "application/octet-stream");
      const presignXhr = new XMLHttpRequest();
      presignXhr.open("POST", "/client/orders/presign-upload", true);
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
          progressLabel.textContent = (data && data.error) || "Couldn't prepare the upload - please try again.";
          if (submitBtn) submitBtn.disabled = false;
          return;
        }
        const putXhr = new XMLHttpRequest();
        putXhr.open("PUT", data.put_url, true);
        putXhr.setRequestHeader("Content-Type", file.type || "application/octet-stream");
        putXhr.upload.addEventListener("progress", function (ev) {
          if (!ev.lengthComputable) return;
          const pct = Math.round((ev.loaded / ev.total) * 100);
          progressFill.style.width = pct + "%";
          progressLabel.textContent = "Uploading… " + pct + "%";
        });
        putXhr.onload = function () {
          uploadTransferActive = false;
          if (putXhr.status < 200 || putXhr.status >= 300) {
            progressLabel.textContent = "Upload failed - please try again.";
            if (submitBtn) submitBtn.disabled = false;
            return;
          }
          progressFill.style.width = "100%";
          progressLabel.textContent = "Upload complete - processing your order…";
          // Safely on R2 now - a refresh from here on only needs to skip
          // straight to "Continue with this file", never re-upload it.
          saveResumableUpload(data.upload_key, file.name);
          formData.delete("audio");
          formData.set("upload_key", data.upload_key);
          finalizeOrder(formData);
        };
        putXhr.onerror = function () {
          uploadTransferActive = false;
          progressLabel.textContent = "Upload failed - please try again.";
          if (submitBtn) submitBtn.disabled = false;
        };
        putXhr.send(file);
      };
      presignXhr.onerror = function () {
        uploadTransferActive = false;
        progressLabel.textContent = "Couldn't prepare the upload - please try again.";
        if (submitBtn) submitBtn.disabled = false;
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

    form.addEventListener("submit", function (e) {
      e.preventDefault();
      const fileInput = $("#file-input");
      const usingFile = fileInput && fileInput.files && fileInput.files.length > 0;
      const formData = new FormData(form);

      progressWrap.hidden = false;
      progressFill.classList.remove("indeterminate");
      progressFill.style.width = "0%";
      if (submitBtn) submitBtn.disabled = true;

      if (usingFile) {
        uploadFileDirectToR2(fileInput.files[0], formData);
      } else if (resumedUploadKey) {
        // Already safely on R2 from before a refresh - skip straight to
        // finalize, no re-upload.
        progressFill.style.width = "100%";
        progressLabel.textContent = "Submitting your already-uploaded file…";
        formData.delete("audio");
        formData.set("upload_key", resumedUploadKey);
        finalizeOrder(formData);
      } else {
        progressFill.classList.add("indeterminate");
        progressLabel.textContent = "Fetching from YouTube… this can take a few minutes for longer videos, it hasn't hung.";
        finalizeOrder(formData);
      }
    });

    offerResumableUploadIfAny();
  }

  document.addEventListener("DOMContentLoaded", () => {
    bindWizardNav();
    bindSourceSwitch();
    bindDropzone();
    bindPricing();
    bindIdempotencyKey();
    bindManualTranscriptionNote();
    bindUploadProgress();
    showStep(1);
  });
})();
