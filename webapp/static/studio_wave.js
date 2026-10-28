/* The Kauli "studio": a quiet audio waveform that runs behind the hero areas and the sign-in page.
   - bars rise and fall like speech, in blue with olive highlights, and lean toward the cursor
   - a playhead with a timecode sweeps across, like a timeline in an editing suite
   - it stops when it is off screen or the tab is hidden, and draws one still frame for people who prefer reduced motion
   - on the sign-in page it also lights the "what you get" points one at a time, and the wave swells with each one */
(function () {
  "use strict";
  var body = document.body;
  if (!body || !(body.classList.contains("m-public") || body.classList.contains("auth-page"))) return;
  var HOSTS = ".tk-hero, .m-hero, .hd-hero, .demo-hero, .cta-band, .auth-brand-panel.auth-welcome";
  var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var hosts = [].slice.call(document.querySelectorAll(HOSTS));
  if (!hosts.length) return;
  var pulse = 0;

  function luminance(el) {
    var m = getComputedStyle(el).backgroundColor.match(/\d+(\.\d+)?/g);
    if (!m || (m.length > 3 && Number(m[3]) === 0)) return 1;
    return (0.299 * m[0] + 0.587 * m[1] + 0.114 * m[2]) / 255;
  }
  function hash(n) { var x = Math.sin(n * 127.1) * 43758.5453; return x - Math.floor(x); }
  function pad(n) { return (n < 10 ? "0" : "") + n; }

  function mount(host) {
    var canvas = document.createElement("canvas");
    canvas.className = "sw";
    canvas.setAttribute("aria-hidden", "true");
    host.setAttribute("data-studio-wave", "");
    host.insertBefore(canvas, host.firstChild);
    var ctx = canvas.getContext("2d");
    var dark = luminance(host) < 0.45;
    var w = 0, h = 0, dpr = Math.min(window.devicePixelRatio || 1, 2), mouse = { x: -999, y: -999 };
    var running = false, last = 0, t0 = performance.now(), raf = 0;
    var isLogin = host.classList.contains("auth-welcome");
    var isHome = host.classList.contains("tk-hero");
    var big = isLogin || isHome || host.classList.contains("hd-hero");

    function size() {
      var r = host.getBoundingClientRect();
      w = Math.max(1, Math.round(r.width)); h = Math.max(1, Math.round(r.height));
      canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }

    function frame(now) {
      var t = (now - t0) / 1000;
      ctx.clearRect(0, 0, w, h);
      var step = w < 600 ? 7 : 9, bars = Math.ceil(w / step);
      // the wave sits where there is room: a floor under the sign-in points, behind the hero's call to action,
      // and centred in the slimmer bands
      var base = isLogin ? h * 0.9 : (isHome ? Math.min(h * 0.5, 300) : h * (big ? 0.72 : 0.5));
      var maxH = isLogin ? 78 : (isHome ? 100 : Math.min(h * (big ? 0.34 : 0.5), 120));
      var lift = 1 + pulse * 0.9;
      for (var i = 0; i < bars; i++) {
        var x = i * step + step / 2;
        // a slow phrase envelope times quick speech-like detail, drifting leftward
        var u = i * 0.115 - t * 0.9;
        var env = 0.5 + 0.5 * Math.sin(u * 0.55) * Math.sin(u * 0.23 + 1.3);
        var det = 0.35 + 0.65 * hash(Math.floor(u * 2.2));
        var amp = Math.pow(Math.max(env, 0.06), 1.15) * det;
        var d = Math.hypot(x - mouse.x, base - mouse.y);
        var near = Math.max(0, 1 - d / 190);
        var bh = Math.max(2, maxH * amp * lift * (1 + near * 0.9));
        var g = x / w;
        var a = (dark ? 0.10 : 0.09) + 0.22 * amp + near * 0.18;
        ctx.fillStyle = (i % 11 === 0)
          ? (dark ? "rgba(154,178,85," + (a + 0.12) + ")" : "rgba(85,107,47," + (a + 0.1) + ")")
          : (dark ? "rgba(127,176,255," + a + ")" : "rgba(37,99,235," + a + ")");
        var bw = Math.max(2, step * 0.42);
        ctx.beginPath();
        if (ctx.roundRect) ctx.roundRect(x - bw / 2, base - bh / 2, bw, bh, bw / 2); else ctx.rect(x - bw / 2, base - bh / 2, bw, bh);
        ctx.fill();
      }
      if (big) {
        // the playhead and its timecode
        var span = 14, p = (t % span) / span, px = p * w, sec = Math.floor(t % 3600);
        ctx.strokeStyle = dark ? "rgba(154,178,85,0.55)" : "rgba(85,107,47,0.45)";
        ctx.lineWidth = 1.5;
        ctx.beginPath(); ctx.moveTo(px, base - maxH - 18); ctx.lineTo(px, base + maxH + 18); ctx.stroke();
        ctx.fillStyle = dark ? "rgba(154,178,85,0.9)" : "rgba(70,88,31,0.85)";
        ctx.font = "600 11px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace";
        ctx.fillText("00:" + pad(Math.floor(sec / 60) % 60) + ":" + pad(sec % 60), Math.min(px + 8, w - 64), base - maxH - 22);
      }
      pulse *= 0.94;
    }

    function loop(now) {
      if (!running) return;
      if (now - last > 33) { last = now; frame(now); }   // about 30 frames a second is plenty
      raf = requestAnimationFrame(loop);
    }
    function start() { if (running || reduce) return; running = true; raf = requestAnimationFrame(loop); }
    function stop() { running = false; cancelAnimationFrame(raf); }

    size();
    frame(t0 + 4000);                                       // a still frame is always drawn, even with reduced motion
    host.addEventListener("pointermove", function (e) { var r = host.getBoundingClientRect(); mouse.x = e.clientX - r.left; mouse.y = e.clientY - r.top; }, { passive: true });
    host.addEventListener("pointerleave", function () { mouse.x = mouse.y = -999; });
    window.addEventListener("resize", function () { size(); if (!running) frame(t0 + 4000); });
    document.addEventListener("sw-pulse", function () { pulse = 1; });
    if ("IntersectionObserver" in window) {
      new IntersectionObserver(function (e) { if (e[0].isIntersecting && !document.hidden) start(); else stop(); }, { threshold: 0.05 }).observe(host);
    } else { start(); }
    document.addEventListener("visibilitychange", function () { if (document.hidden) stop(); else start(); });
  }

  hosts.forEach(mount);

  // sign-in page: a "live" tag, and the "what you get" points lit one after another
  var panel = document.querySelector(".auth-welcome");
  if (panel) {
    var hud = document.createElement("div");
    hud.className = "sw-hud";
    hud.setAttribute("aria-hidden", "true");
    hud.innerHTML = "<i></i>Studio live";
    panel.appendChild(hud);
    var items = [].slice.call(panel.querySelectorAll(".auth-facts li"));
    if (items.length && !reduce) {
      var k = 0, paused = false;
      var light = function () {
        items.forEach(function (li, i) { li.classList.toggle("is-lit", i === k); });
        document.dispatchEvent(new Event("sw-pulse"));
        k = (k + 1) % items.length;
      };
      items.forEach(function (li, i) { li.addEventListener("mouseenter", function () { paused = true; k = i; light(); }); });
      panel.addEventListener("mouseleave", function () { paused = false; });
      light();
      setInterval(function () { if (!paused && !document.hidden) light(); }, 3200);
    }
  }
})();
