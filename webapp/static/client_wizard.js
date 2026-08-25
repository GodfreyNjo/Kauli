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

  function showStep(n) {
    currentStep = n;
    $all(".wizard-pane").forEach((p) => { p.hidden = Number(p.dataset.pane) !== n; });
    $all(".wizard-step").forEach((s) => {
      const stepNum = Number(s.dataset.step);
      s.classList.toggle("active", stepNum === n);
      s.classList.toggle("done", stepNum < n);
    });
    $("#wizard-back").hidden = n === 1;
    $("#wizard-next").hidden = n === TOTAL_STEPS;
    $("#wizard-submit").hidden = n !== TOTAL_STEPS;
    if (n === 2) updatePricing();
    $(".wizard-card").scrollIntoView({ block: "start", behavior: "smooth" });
  }

  function step1Valid() {
    const usingYoutube = $('#source-switch [data-source="youtube"]').classList.contains("active");
    if (usingYoutube) {
      const url = $("#youtube-url-input").value.trim();
      if (!url) { alert("Paste a YouTube link first."); return false; }
      return true;
    }
    const file = $("#file-input").files[0];
    if (!file) { alert("Choose a file first."); return false; }
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
      s.addEventListener("click", () => {
        const target = Number(s.dataset.step);
        if (target < currentStep) showStep(target);
      });
    });
  }

  // ------------------------------------------------------- source switch ----
  function bindSourceSwitch() {
    const fileBtn = $('#source-switch [data-source="file"]');
    const ytBtn = $('#source-switch [data-source="youtube"]');
    const filePane = $("#source-file");
    const ytPane = $("#source-youtube");
    fileBtn.addEventListener("click", () => {
      fileBtn.classList.add("active"); ytBtn.classList.remove("active");
      filePane.hidden = false; ytPane.hidden = true;
    });
    ytBtn.addEventListener("click", () => {
      ytBtn.classList.add("active"); fileBtn.classList.remove("active");
      ytPane.hidden = false; filePane.hidden = true;
      fileDurationMinutes = null;
      updatePricing();
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
    const rushPct = Number(data.dataset.rushPct) || 0;
    const CREDITS_PER_DOLLAR = 10; // mirrors billing.CREDITS_PER_DOLLAR

    const selected = $('input[name="service_level"]:checked');
    const rate = selected ? Number(selected.dataset.rate) : 0;
    const serviceName = selected ? selected.dataset.name : "-";
    $("#price-service").textContent = serviceName;
    // Mirrors billing.FREE_MINUTES_SERVICE_LEVEL server-side - free minutes
    // only ever apply to a transcription-only order, so the estimate here
    // has to match what create_order() will actually charge.
    const freeMinutesEligible = selected && selected.value === "transcribe";

    // Voice cloning only ever runs on a full dub - no point asking for
    // cloning consent on a transcription/translation-only order.
    const consentField = $("#voice-clone-consent-field");
    if (consentField) consentField.hidden = !(selected && selected.value === "dub");

    // Same reasoning - a human voice actor only makes sense on a full dub.
    const humanVoiceField = $("#human-voice-field");
    const isDub = !!(selected && selected.value === "dub");
    if (humanVoiceField) humanVoiceField.hidden = !isDub;
    const humanVoiceBox = $("#human-voice-checkbox");
    const humanVoiceChecked = isDub && humanVoiceBox && humanVoiceBox.checked;
    $("#price-human-voice-row").hidden = !humanVoiceChecked;

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
        + (humanVoiceChecked ? humanVoiceRate : 0);
      if (rushChecked) perMinExtras += rate * rushPct;
      $("#price-total").textContent = formatMoney(rate + perMinExtras) + "/min";
      if (addonChecked) $("#price-addon").textContent = formatMoney(addonRate) + "/min";
      if (isManual) $("#price-manual").textContent = formatMoney(manualRate) + "/min";
      if (humanVoiceChecked) $("#price-human-voice").textContent = formatMoney(humanVoiceRate) + "/min";
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
    const rushSurcharge = rushChecked ? (fileDurationMinutes * rate) * rushPct : 0;
    const total = afterDiscount - creditsAppliedUsd + addonCost + manualCost + humanVoiceCost + rushSurcharge;

    $("#price-free").textContent = freeApplied > 0 ? freeApplied.toFixed(1) + " min"
      : (freeMinutesEligible ? "none applied" : "not eligible (transcription-only)");
    $("#price-wallet-row").hidden = creditsAppliedUsd <= 0;
    if (creditsAppliedUsd > 0) {
      $("#price-wallet").textContent = Math.round(creditsAppliedUsd * CREDITS_PER_DOLLAR) + " credits (" + formatMoney(creditsAppliedUsd) + ")";
    }
    if (addonChecked) $("#price-addon").textContent = formatMoney(addonCost);
    if (isManual) $("#price-manual").textContent = formatMoney(manualCost);
    if (humanVoiceChecked) $("#price-human-voice").textContent = formatMoney(humanVoiceCost);
    if (rushChecked) $("#price-rush").textContent = formatMoney(rushSurcharge);
    $("#price-total").textContent = formatMoney(Math.max(0, total));
  }

  function bindPricing() {
    $all('input[name="service_level"]').forEach((r) => r.addEventListener("change", updatePricing));
    const addonBox = $("#addon-checkbox");
    if (addonBox) addonBox.addEventListener("change", updatePricing);
    const humanVoiceBoxEl = $("#human-voice-checkbox");
    if (humanVoiceBoxEl) humanVoiceBoxEl.addEventListener("change", updatePricing);
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
  function bindUploadProgress() {
    const form = $("#order-form");
    const progressWrap = $("#wizard-upload-progress");
    const progressLabel = $("#wizard-upload-progress-label");
    const progressFill = $("#wizard-upload-progress-fill");
    const submitBtn = $("#wizard-submit");
    if (!form || !progressWrap || !window.XMLHttpRequest) return;

    form.addEventListener("submit", function (e) {
      e.preventDefault();
      const fileInput = $("#file-input");
      const usingFile = fileInput && fileInput.files && fileInput.files.length > 0;
      const formData = new FormData(form);
      const xhr = new XMLHttpRequest();
      xhr.open("POST", form.action, true);

      progressWrap.hidden = false;
      progressFill.classList.remove("indeterminate");
      progressFill.style.width = "0%";
      if (submitBtn) submitBtn.disabled = true;

      if (usingFile) {
        xhr.upload.addEventListener("progress", function (ev) {
          if (!ev.lengthComputable) return;
          const pct = Math.round((ev.loaded / ev.total) * 100);
          progressFill.style.width = pct + "%";
          progressLabel.textContent = "Uploading… " + pct + "%";
        });
        xhr.upload.addEventListener("load", function () {
          progressFill.style.width = "100%";
          progressLabel.textContent = "Upload complete - processing your order…";
        });
      } else {
        progressFill.classList.add("indeterminate");
        progressLabel.textContent = "Fetching from YouTube… this can take a few minutes for longer videos, it hasn't hung.";
      }

      xhr.onload = function () {
        // Same final HTML the server would have sent a normal form
        // submission to (the order page on success, the dashboard with a
        // real error message if something went wrong) - just reached
        // without a second full-page round trip, so the progress bar
        // stays visible right up until the real result is ready.
        history.replaceState(null, "", xhr.responseURL);
        document.open();
        document.write(xhr.responseText);
        document.close();
      };
      xhr.onerror = function () {
        progressLabel.textContent = "Something went wrong submitting - please try again.";
        if (submitBtn) submitBtn.disabled = false;
      };
      xhr.send(formData);
    });
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
