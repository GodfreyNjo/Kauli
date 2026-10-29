(function () {
  "use strict";
  var dataEl = document.getElementById("hd-data");
  if (!dataEl) return;
  var D = JSON.parse(dataEl.textContent);
  var $ = function (id) { return document.getElementById(id); };
  var audio = $("hd-audio");
  var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var state = { scenario: D.scenario, lang: D.language, dir: D.dir || "from", playing: null };
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
  // A transcript: one line per turn, each opened by the speaker's ID and the time they start, then the words (which the highlight
  // walks through while the audio plays). Text with a single speaker has no labels.
  function paint(el, text, lang, names, starts) {
    el.textContent = "";
    el.setAttribute("lang", lang);
    var words = [];
    text.split("\n").forEach(function (line, i) {
      var turn = document.createElement("span");
      turn.className = "hd-turn";
      var label = null;
      (names || []).forEach(function (n) { if (line.indexOf(n + ": ") === 0) label = n; });
      if (label) {
        var b = document.createElement("b");
        b.className = "hd-spk";
        b.textContent = label;
        turn.appendChild(b);
        if (starts && starts[i] != null) {
          var ts = document.createElement("small");
          ts.className = "hd-ts";
          ts.textContent = fmt(starts[i]);
          turn.appendChild(ts);
        }
      }
      pieces(label ? line.slice(label.length + 2) : line, lang).forEach(function (p) {
        var s = document.createElement("span");
        s.className = "w";
        s.textContent = p;
        turn.appendChild(s);
        words.push(s);
      });
      el.appendChild(turn);
    });
    el._words = words;
  }
  function turnStarts(url) {
    var marks = (D.timing || {})[(url || "").split("/").pop()] || [];
    return marks.map(function (m) { return m[1]; });
  }
  function highlight(el, frac) {
    var spans = el._words || [], n = spans.length, upto = Math.floor(frac * n + 0.0001);
    for (var i = 0; i < n; i++) spans[i].className = i < upto ? "done" : (i === upto ? "now" : "");
  }
  function waveProgress(el, frac) {
    var bars = el.children, on = Math.round(frac * bars.length);
    for (var i = 0; i < bars.length; i++) bars[i].className = i < on ? "on" : "";
  }

  function scenario() { return D.scenarios[state.scenario]; }
  function clipFor(which) {
    var s = scenario();
    if (state.dir === "to") {                      // the visitor's language into English
      if (which === "out" && s.service === "dub") return s.src;                   // the English dub is the English clip
      return s.inlang[state.lang] || s.src;
    }
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

  // The video of the result: the dubbed (or subtitled) video with the captions burned in. Only the "into this language"
  // direction has one; the other direction keeps the audio player.
  function showVideo(s, L, dubbed, toEn) {
    var box = $("hd-video-box"), vid = $("hd-video");
    var url = !toEn && s.video && s.video[state.lang];
    if (!vid.dataset.bound) { vid.dataset.bound = "1"; vid.addEventListener("play", stop); }      // the video and the audio player never talk at once
    if (!url) {
      vid.pause();
      vid.removeAttribute("src");
      box.hidden = true;
      return;
    }
    box.hidden = false;
    if (vid.getAttribute("src") !== url) { vid.pause(); vid.setAttribute("poster", url.replace(/\.mp4$/, ".jpg")); vid.setAttribute("src", url); }
    $("hd-video-sub").textContent = dubbed
      ? "The video dubbed in " + L.name + ", with " + L.name + " captions burned into the picture."
      : "The original audio with " + L.name + " subtitles burned into the picture.";
  }

  function render() {
    stop();
    var s = scenario(), L = D.languages[state.lang];
    var toEn = state.dir === "to";
    var inText = toEn ? s.tr[state.lang] : s.en, inLang = toEn ? state.lang : "en";
    var outText = toEn ? s.en : s.tr[state.lang], outLang = toEn ? "en" : state.lang;
    var dubbed = s.service === "dub";
    $("hd-service").textContent = s.service_label;
    $("hd-headline").textContent = toEn ? (s.headline_en || s.headline) : s.headline;
    $("hd-in-title").textContent = (toEn ? L.native : "English") + ", as recorded";
    $("hd-dir-lang").textContent = L.name;
    var names = s.multi ? s.cast : [];
    var startsIn = turnStarts(clipFor("in")), startsOut = turnStarts(dubbed ? clipFor("out") : clipFor("in"));
    paint($("hd-text-in"), inText, inLang, names, startsIn);
    buildWave($("hd-wave-in"), 3);
    buildWave($("hd-wave-out"), dubbed ? 11 : 3);
    $("hd-time-in").textContent = "0:00";
    $("hd-time-out").textContent = "0:00";
    $("hd-lang-name").textContent = toEn ? "English" : L.name;
    $("hd-speaker").textContent = s.speaker;
    var transcript = s.service === "transcribe";
    $("hd-transcript").hidden = !transcript;
    $("hd-speaker").closest(".hd-line").hidden = !!s.multi;           // with several speakers each line carries its own ID
    paint($("hd-text-src"), inText, inLang, names, startsIn);
    // transcription shows the transcript plus its translation; the other two show the translated text as captions
    paint($("hd-text-out"), outText, outLang, names, startsOut);
    $("hd-text-out").className = "hd-text hd-text-out" + (transcript ? " hd-translated" : "");
    var play = $("hd-play-out");
    play.setAttribute("aria-label", dubbed ? "Play the dubbed " + (toEn ? "English" : L.name) + " audio" : "Play the audio with " + (toEn ? "English" : L.name) + " captions");
    showVideo(s, L, dubbed, toEn);
    var avail = $("hd-avail");
    if (dubbed && !toEn) {
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
      u.searchParams.set("dir", state.dir);
      window.history.replaceState(null, "", u.toString());
    } catch (e) { /* the page works without it */ }
  }

  function pickedLine() { return ""; }          // the suggestion is shown as a "Recommended" badge, not as a sentence about the visitor

  function track(name, params) { if (window.kauliTrack) window.kauliTrack(name, params); }

  function play(which) {
    if (state.playing === which) { stop(); return; }
    stop();
    track("demo_play", { scenario: state.scenario, language: state.lang, direction: state.dir, clip: which });
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
    r.addEventListener("change", function () { state.scenario = r.value; track("demo_scenario", { scenario: r.value }); render(); });
  });
  [].forEach.call(document.querySelectorAll('input[name="hd-lang"]'), function (r) {
    r.addEventListener("change", function () { state.lang = r.value; track("demo_language", { language: r.value }); render(); });
  });
  [].forEach.call(document.querySelectorAll('input[name="hd-dir"]'), function (r) {
    r.addEventListener("change", function () { state.dir = r.value; render(); });
  });
  render();
})();
