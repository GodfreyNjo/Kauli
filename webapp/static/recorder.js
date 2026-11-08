/* Record audio in the browser on the upload step. The recording becomes an ordinary file in the upload list (through
   window.kauliAddFiles, set by client_wizard.js), so everything after it (price, languages, submit) works as for any file.
   Needs a microphone and MediaRecorder; where either is missing the control stays hidden. */
(function () {
  "use strict";
  var box = document.getElementById("mk-rec");
  var button = document.getElementById("rec-toggle");
  if (!box || !button || !window.MediaRecorder || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) return;

  var label = document.getElementById("rec-label");
  var clock = document.getElementById("rec-time");
  var note = document.getElementById("rec-note");
  var MAX_SECONDS = 2 * 60 * 60;                         // a recording stops by itself after two hours
  var TYPES = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"];
  var recorder = null, stream = null, chunks = [], timer = null, startedAt = 0;

  box.hidden = false;

  function say(text) { note.textContent = text || ""; }
  function pad(n) { return String(n).padStart(2, "0"); }
  function clockText(seconds) { return Math.floor(seconds / 60) + ":" + pad(Math.floor(seconds % 60)); }
  function extensionFor(mime) { return mime.indexOf("mp4") >= 0 ? "m4a" : mime.indexOf("ogg") >= 0 ? "ogg" : "webm"; }

  function nameFor(extension) {
    var d = new Date();
    return "recording-" + d.getFullYear() + pad(d.getMonth() + 1) + pad(d.getDate()) + "-" + pad(d.getHours()) + pad(d.getMinutes()) + "." + extension;
  }

  function showIdle() {
    button.setAttribute("aria-pressed", "false");
    button.classList.remove("is-recording");
    label.textContent = "Record audio";
    clock.hidden = true;
  }

  function tick() {
    var seconds = (Date.now() - startedAt) / 1000;
    clock.textContent = clockText(seconds);
    if (seconds >= MAX_SECONDS) stop();
  }

  function finish() {
    if (stream) stream.getTracks().forEach(function (t) { t.stop(); });
    clearInterval(timer);
    var seconds = (Date.now() - startedAt) / 1000;
    var mime = (recorder && recorder.mimeType) || "audio/webm";
    showIdle();
    if (!chunks.length || seconds < 1) { say("Nothing was recorded. Try again."); return; }
    var file = new File(chunks, nameFor(extensionFor(mime)), { type: mime.split(";")[0], lastModified: Date.now() });
    file.kauliSeconds = seconds;
    chunks = [];
    if (window.kauliAddFiles) { window.kauliAddFiles([file]); say("Recording added (" + clockText(seconds) + "). Continue to the next step."); }
  }

  function start() {
    say("");
    navigator.mediaDevices.getUserMedia({ audio: true }).then(function (s) {
      stream = s;
      var type = TYPES.filter(function (t) { return MediaRecorder.isTypeSupported(t); })[0];
      recorder = type ? new MediaRecorder(s, { mimeType: type }) : new MediaRecorder(s);
      chunks = [];
      recorder.ondataavailable = function (e) { if (e.data && e.data.size) chunks.push(e.data); };
      recorder.onstop = finish;
      recorder.start(1000);
      startedAt = Date.now();
      timer = setInterval(tick, 250);
      button.setAttribute("aria-pressed", "true");
      button.classList.add("is-recording");
      label.textContent = "Stop recording";
      clock.hidden = false;
      clock.textContent = "0:00";
    }).catch(function () {
      say("We could not use your microphone. Allow it in your browser, then try again.");
    });
  }

  function stop() { if (recorder && recorder.state !== "inactive") recorder.stop(); }

  button.addEventListener("click", function () {
    if (recorder && recorder.state === "recording") stop(); else start();
  });
  window.addEventListener("pagehide", function () { if (recorder && recorder.state === "recording") stop(); });
})();
