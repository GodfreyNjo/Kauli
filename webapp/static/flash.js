/* Flash messages (a saved notice, an error after a form).
 *
 * - Every message can be dismissed with a button, and is announced to screen readers (errors assertively, notices politely).
 * - A notice fades away by itself after a while, so it does not sit on the page, and the timer waits while the message is hovered or focused.
 * - An error does NOT disappear by itself: it stays until the person dismisses it (WCAG 2.2.1 timing, 3.3.1 error identification).
 * - Anything that is a lasting fact rather than a message (a failed order's explanation, "Ready. Download your files") carries data-persist and stays.
 * - The ?error= / ?notice= part of the address is dropped so a refresh does not bring the message back.
 *
 * window.kpAnnounce(text, assertive) writes to one visually hidden live region, for other scripts (in-place page updates, the upload tray).
 */
(function () {
  "use strict";
  var SHOW_MS = 10000, FADE_MS = 450;

  var live = { polite: null, assertive: null };
  function region(kind) {
    if (live[kind] && document.body.contains(live[kind])) return live[kind];
    var el = document.createElement("div");
    el.className = "kp-live"; el.setAttribute("aria-live", kind); el.setAttribute("aria-atomic", "true");
    if (kind === "assertive") el.setAttribute("role", "alert"); else el.setAttribute("role", "status");
    document.body.appendChild(el);
    live[kind] = el;
    return el;
  }
  window.kpAnnounce = function (text, assertive) {
    var el = region(assertive ? "assertive" : "polite");
    el.textContent = "";
    window.setTimeout(function () { el.textContent = text; }, 60);       // a change after insertion is what screen readers announce
  };

  function init() {
    var nodes = [].slice.call(document.querySelectorAll("main p.error, main p.ok, main .hub-flash, .auth-shell p.error, .auth-shell p.ok"));
    nodes = nodes.filter(function (el) {
      return !el.hasAttribute("data-persist") && !el.closest("[data-persist], table, details, .password-checklist") && el.textContent.trim().length > 0
        && !el.querySelector(".flash-x");
    });
    nodes.forEach(function (el, i) {
      var isError = el.classList.contains("error");
      if (!el.getAttribute("role")) el.setAttribute("role", isError ? "alert" : "status");
      var text = el.textContent.trim();
      window.setTimeout(function () { window.kpAnnounce(text, isError); }, 250 + i * 50);
      var close = document.createElement("button");
      close.type = "button"; close.className = "flash-x"; close.setAttribute("aria-label", "Dismiss message"); close.textContent = "×";
      close.setAttribute("data-no-busy", "");
      el.appendChild(close);
      var timer = null, paused = false;
      function remove() {
        clearTimeout(timer);
        el.style.transition = "opacity " + FADE_MS + "ms ease, max-height " + FADE_MS + "ms ease, margin " + FADE_MS + "ms ease, padding " + FADE_MS + "ms ease";
        el.style.overflow = "hidden";
        el.style.maxHeight = el.offsetHeight + "px";
        void el.offsetHeight;
        el.style.opacity = "0";
        el.style.maxHeight = "0";
        el.style.marginTop = el.style.marginBottom = el.style.paddingTop = el.style.paddingBottom = "0";
        window.setTimeout(function () { el.hidden = true; el.style.display = "none"; }, FADE_MS + 50);
      }
      close.addEventListener("click", function () {
        var next = document.querySelector("main h1") || document.getElementById("main-content");
        remove();
        if (next && next.focus) { if (!next.hasAttribute("tabindex")) next.setAttribute("tabindex", "-1"); next.focus({ preventScroll: true }); }
      });
      if (isError) return;                                               // errors stay until dismissed
      function hide() { if (paused) { timer = setTimeout(hide, 1500); return; } remove(); }
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
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
  document.addEventListener("kp:softnav", init);
})();
