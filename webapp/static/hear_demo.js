(function () {
  "use strict";
  var dataEl = document.getElementById("hd-data");
  if (!dataEl) return;
  var D = JSON.parse(dataEl.textContent);
  var $ = function (id) { return document.getElementById(id); };
  var audio = $("hd-audio");
  var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var state = { scenario: D.scenario, lang: D.language, playing: null };
  var BARS = 44;

  function fmt(s) { s = Math.max(0, Math.floor(s || 0)); return Math.floor(s / 60) + ":" + ("0" + (s % 60)).slice(-2); }

  // A calm, fixed wave shape so it looks the same every visit.
  function buildWave(el, seed) {
    el.textContent = "";
    for (var i = 0; i < BARS; i++) {
      var h = 22 + Math.round(70 * Math.abs(Math.sin((i + seed) * 0.62) * Math.cos((i + seed) * 0.21)));
      var b = document.createElement("i");
      b.style.height = h + "%";
      el.appendChild(b);
    }
  }

  // Split a caption into pieces the highlight can walk through (words, or short runs for Chinese).
  function pieces(text, lang) {
    if (lang === "zh") { return text.match(/.{1,2}/g) || [text]; }
    return text.split(/(\s+)/).filter(function (x) { return x.length; });
  }
  function paint(el, text, lang) {
    el.textContent = "";
    el.setAttribute("lang", lang);
    pieces(text, lang).forEach(function (p) {
      var s = document.createElement("span");
      s.textContent = p;
      el.appendChild(s);
    });
  }
  function highlight(el, frac) {
    var spans = el.children, n = spans.length, upto = Math.floor(frac * n + 0.0001);
    for (var i = 0; i < n; i++) spans[i].className = i < upto ? "done" : (i === upto ? "now" : "");
  }
  function waveProgress(el, frac) {
    var bars = el.children, on = Math.round(frac * bars.length);
    for (var i = 0; i < bars.length; i++) bars[i].className = i < on ? "on" : "";
  }

  function scenario() { return D.scenarios[state.scenario]; }
  function clipFor(which) {
    var s = scenario();
    if (which === "out" && s.service === "dub" && s.dub[state.lang]) return s.dub[state.lang];
    return s.src;
  }
  function stop() {
    audio.pause();
    state.playing = null;
    ["in", "out"].forEach(function (w) {
      $("hd-play-" + w).classList.remove("is-playing");
      $("hd-wave-" + w).classList.remove("is-live");
    });
  }

  function render() {
    stop();
    var s = scenario(), L = D.languages[state.lang];
    var translated = s.tr[state.lang];
    var dubbed = s.service === "dub";
    $("hd-service").textContent = s.service_label;
    $("hd-headline").textContent = s.headline;
    paint($("hd-text-in"), s.en, "en");
    buildWave($("hd-wave-in"), 3);
    buildWave($("hd-wave-out"), dubbed ? 11 : 3);
    $("hd-time-in").textContent = "0:00";
    $("hd-time-out").textContent = "0:00";
    $("hd-lang-name").textContent = L.name;
    $("hd-speaker").textContent = s.speaker;
    var transcript = s.service === "transcribe";
    $("hd-transcript").hidden = !transcript;
    paint($("hd-text-src"), s.en, "en");
    // transcription shows the transcript plus its translation; the other two show the translated text as captions
    paint($("hd-text-out"), translated, state.lang);
    $("hd-text-out").className = "hd-text hd-text-out" + (transcript ? " hd-translated" : "");
    var play = $("hd-play-out");
    play.setAttribute("aria-label", dubbed ? "Play the dubbed " + L.name + " audio" : "Play the audio with " + L.name + " captions");
    var avail = $("hd-avail");
    if (dubbed) {
      var live = !!L.dub_live;
      avail.textContent = live ? "Available now" : "Voice preview - " + L.name + " dubbing coming soon";
      avail.className = "hd-avail " + (live ? "now" : "soon");
    } else {
      avail.textContent = "Available now";
      avail.className = "hd-avail now";
    }
    var r = D.rates[s.service];
    $("hd-rate").innerHTML = "<strong>" + s.service_label + "</strong> from <b>$" + r.ai + "</b> per audio-minute with Instant AI, or <b>$" + r.human + "</b> human-reviewed.";
    $("hd-picked").textContent = pickedLine();
    if (window.location.pathname !== "/try") return;     // the home page keeps its own address
    try {
      var u = new URL(window.location.href);
      u.searchParams.set("use", state.scenario);
      u.searchParams.set("lang", state.lang);
      window.history.replaceState(null, "", u.toString());
    } catch (e) { /* the page works without it */ }
  }

  function pickedLine() { return ""; }          // the suggestion is shown as a "Recommended" badge, not as a sentence about the visitor

  function play(which) {
    if (state.playing === which) { stop(); return; }
    stop();
    var src = clipFor(which);
    state.playing = which;
    audio.src = src;
    audio.currentTime = 0;
    var p = audio.play();
    if (p && p.catch) p.catch(function () { stop(); });
    $("hd-play-" + which).classList.add("is-playing");
    if (!reduce) $("hd-wave-" + which).classList.add("is-live");
  }

  function tick() {
    var w = state.playing;
    if (!w) return;
    var frac = audio.duration ? audio.currentTime / audio.duration : 0;
    $("hd-time-" + w).textContent = fmt(audio.currentTime);
    waveProgress($("hd-wave-" + w), frac);
    if (w === "in") { highlight($("hd-text-in"), frac); }
    else {
      var s = scenario();
      if (s.service === "transcribe") { highlight($("hd-text-src"), frac); }
      highlight($("hd-text-out"), frac);
    }
  }
  audio.addEventListener("timeupdate", tick);
  audio.addEventListener("ended", function () {
    var w = state.playing;
    stop();
    if (w) { highlight($("hd-text-" + (w === "in" ? "in" : "out")), 1); waveProgress($("hd-wave-" + w), 1); }
  });

  $("hd-play-in").addEventListener("click", function () { play("in"); });
  $("hd-play-out").addEventListener("click", function () { play("out"); });
  [].forEach.call(document.querySelectorAll('input[name="hd-scenario"]'), function (r) {
    r.addEventListener("change", function () { state.scenario = r.value; render(); });
  });
  [].forEach.call(document.querySelectorAll('input[name="hd-lang"]'), function (r) {
    r.addEventListener("change", function () { state.lang = r.value; render(); });
  });
  render();
})();
