/* Record & Transcribe: record audio in the browser on the upload step.
   While recording: a timer, a live level meter (so you can see the microphone is working) and Pause / Resume.
   When you stop: the take is added to the upload list as an ordinary file (through window.kauliAddFiles, set by client_wizard.js),
   you can play it back, and "Transcribe this" moves on to the next step. Everything after that (price, languages, submit) is the same
   as for any file. Needs a microphone and MediaRecorder; where either is missing the control stays hidden. */
(function () {
  "use strict";
  var box = document.getElementById("mk-rec");
  var button = document.getElementById("rec-toggle");
  if (!box || !button || !window.MediaRecorder || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) return;

  var label = document.getElementById("rec-label");
  var clock = document.getElementById("rec-time");
  var note = document.getElementById("rec-note");
  var pauseButton = document.getElementById("rec-pause");
  var meter = document.getElementById("rec-meter");
  var level = document.getElementById("rec-level");
  var done = document.getElementById("rec-done");
  var preview = document.getElementById("rec-preview");
  var MAX_SECONDS = 2 * 60 * 60;                         // a recording stops by itself after two hours
  var TYPES = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"];
  var recorder = null, stream = null, chunks = [], timer = null, frame = null, audioContext = null, analyser = null;
  var segmentStart = 0, banked = 0, previewUrl = null;   // banked: milliseconds recorded before the current stretch (after a pause)

  box.hidden = false;

  function say(text) { note.textContent = text || ""; }
  function pad(n) { return String(n).padStart(2, "0"); }
  function clockText(seconds) { return Math.floor(seconds / 60) + ":" + pad(Math.floor(seconds % 60)); }
  function extensionFor(mime) { return mime.indexOf("mp4") >= 0 ? "m4a" : mime.indexOf("ogg") >= 0 ? "ogg" : "webm"; }
  function elapsedSeconds() { return (banked + (recorder && recorder.state === "recording" ? Date.now() - segmentStart : 0)) / 1000; }

  function nameFor(extension) {
    var d = new Date();
    return "recording-" + d.getFullYear() + pad(d.getMonth() + 1) + pad(d.getDate()) + "-" + pad(d.getHours()) + pad(d.getMinutes()) + "." + extension;
  }

  function showIdle() {
    button.setAttribute("aria-pressed", "false");
    button.classList.remove("is-recording");
    label.textContent = done.hidden ? "Record audio" : "Record another";
    clock.hidden = true;
    meter.hidden = true;
    pauseButton.hidden = true;
  }

  function showLevel() {                                  // the loudness of the last instant, as the width of a bar
    if (!analyser) return;
    var samples = new Uint8Array(analyser.fftSize);
    analyser.getByteTimeDomainData(samples);
    var peak = 0;
    for (var i = 0; i < samples.length; i++) peak = Math.max(peak, Math.abs(samples[i] - 128));
    level.style.width = Math.min(100, Math.round((peak / 128) * 160)) + "%";
    frame = requestAnimationFrame(showLevel);
  }

  function stopMeter() {
    if (frame) cancelAnimationFrame(frame);
    frame = null;
    if (audioContext) { try { audioContext.close(); } catch (e) { /* already closed */ } }
    audioContext = analyser = null;
    level.style.width = "0";
  }

  function startMeter(s) {
    var Context = window.AudioContext || window.webkitAudioContext;
    if (!Context) return;
    audioContext = new Context();
    analyser = audioContext.createAnalyser();
    analyser.fftSize = 512;
    audioContext.createMediaStreamSource(s).connect(analyser);
    showLevel();
  }

  function tick() {
    var seconds = elapsedSeconds();
    clock.textContent = clockText(seconds);
    if (seconds >= MAX_SECONDS) stop();
  }

  function showTake(file, seconds) {
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = URL.createObjectURL(file);
    preview.src = previewUrl;
    done.hidden = false;
    say("Recorded " + clockText(seconds) + ". Play it back, or continue.");
  }

  function finish() {
    if (stream) stream.getTracks().forEach(function (t) { t.stop(); });
    clearInterval(timer);
    stopMeter();
    var seconds = banked / 1000;
    var mime = (recorder && recorder.mimeType) || "audio/webm";
    if (!chunks.length || seconds < 1) { showIdle(); say("Nothing was recorded. Try again."); return; }
    var file = new File(chunks, nameFor(extensionFor(mime)), { type: mime.split(";")[0], lastModified: Date.now() });
    file.kauliSeconds = seconds;                          // a browser recording has no length in its header, so the page keeps it
    chunks = [];
    if (window.kauliAddFiles) window.kauliAddFiles([file]);
    showTake(file, seconds);
    showIdle();
  }

  function start() {
    say("");
    navigator.mediaDevices.getUserMedia({ audio: true }).then(function (s) {
      stream = s;
      var type = TYPES.filter(function (t) { return MediaRecorder.isTypeSupported(t); })[0];
      recorder = type ? new MediaRecorder(s, { mimeType: type }) : new MediaRecorder(s);
      chunks = [];
      banked = 0;
      recorder.ondataavailable = function (e) { if (e.data && e.data.size) chunks.push(e.data); };
      recorder.onstop = finish;
      recorder.start(1000);
      segmentStart = Date.now();
      timer = setInterval(tick, 250);
      startMeter(s);
      done.hidden = true;
      button.setAttribute("aria-pressed", "true");
      button.classList.add("is-recording");
      label.textContent = "Stop";
      pauseButton.hidden = false;
      pauseButton.textContent = "Pause";
      clock.hidden = false;
      meter.hidden = false;
      clock.textContent = "0:00";
    }).catch(function () {
      say("We could not use your microphone. Allow it in your browser, then try again.");
    });
  }

  function stop() {
    if (!recorder || recorder.state === "inactive") return;
    banked = elapsedSeconds() * 1000;                    // count the stretch that is ending, then stop
    recorder.stop();
  }

  function togglePause() {
    if (!recorder) return;
    if (recorder.state === "recording") {
      banked += Date.now() - segmentStart;
      recorder.pause();
      pauseButton.textContent = "Resume";
      say("Paused.");
    } else if (recorder.state === "paused") {
      segmentStart = Date.now();
      recorder.resume();
      pauseButton.textContent = "Pause";
      say("");
    }
  }

  button.addEventListener("click", function () {
    if (recorder && recorder.state !== "inactive") stop(); else start();
  });
  pauseButton.addEventListener("click", togglePause);
  document.getElementById("rec-continue").addEventListener("click", function () {
    var next = document.getElementById("wizard-next");
    if (next) next.click();
  });
  window.addEventListener("pagehide", function () { if (recorder && recorder.state !== "inactive") stop(); });
})();
