/* Flash messages (a saved notice, an error after a form) fade away after a few seconds, so they do not sit on the page.
 * Anything that is a lasting fact rather than a message (a failed order's explanation, "Ready. Download your files") carries data-persist and stays.
 * The timer pauses while the message is hovered or focused, and the ?error= / ?notice= part of the address is dropped so a refresh does not bring it back. */
(function () {
  "use strict";
  var SHOW_MS = 7000, FADE_MS = 450;
  var nodes = [].slice.call(document.querySelectorAll("main p.error, main p.ok, main .hub-flash, .auth-shell p.error, .auth-shell p.ok"));
  nodes = nodes.filter(function (el) {
    return !el.hasAttribute("data-persist") && !el.closest("[data-persist], table, details, .password-checklist") && el.textContent.trim().length > 0;
  });
  nodes.forEach(function (el) {
    var timer = null, paused = false;
    function hide() {
      if (paused) { timer = setTimeout(hide, 1500); return; }
      el.style.transition = "opacity " + FADE_MS + "ms ease, max-height " + FADE_MS + "ms ease, margin " + FADE_MS + "ms ease, padding " + FADE_MS + "ms ease";
      el.style.overflow = "hidden";
      el.style.maxHeight = el.offsetHeight + "px";
      void el.offsetHeight;
      el.style.opacity = "0";
      el.style.maxHeight = "0";
      el.style.marginTop = el.style.marginBottom = el.style.paddingTop = el.style.paddingBottom = "0";
      setTimeout(function () { el.hidden = true; el.style.display = "none"; }, FADE_MS + 50);
    }
    el.addEventListener("mouseenter", function () { paused = true; });
    el.addEventListener("mouseleave", function () { paused = false; });
    el.addEventListener("focusin", function () { paused = true; });
    el.addEventListener("focusout", function () { paused = false; });
    timer = setTimeout(hide, SHOW_MS);
  });
  if (nodes.length && window.history && window.history.replaceState) {
    try {
      var url = new URL(window.location.href), changed = false;
      ["error", "notice", "saved", "msg"].forEach(function (k) { if (url.searchParams.has(k)) { url.searchParams.delete(k); changed = true; } });
      if (changed) window.history.replaceState(null, "", url.pathname + (url.search || "") + url.hash);
    } catch (e) { /* the page works without it */ }
  }
})();
