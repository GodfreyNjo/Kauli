/* Action feedback ("Copied to the clipboard", "Saved", "Could not save"): window.kpToast(text, kind).
 *
 * kind is "success" (green), "error" (red), "warning" (amber) or "info" (blue), each with its own icon so the meaning never rests on
 * colour alone. A toast appears at the bottom centre, is announced to screen readers (errors assertively, the rest politely), goes away
 * by itself after a few seconds (waits while hovered or focused) and can be dismissed. An error stays until dismissed, like the page's
 * flash messages (WCAG 2.2.1). Respects reduced motion. */
(function () {
  "use strict";
  var ICONS = {
    success: '<path d="M5 12.5l4.2 4.2L19 7" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/>',
    error: '<path d="M7 7l10 10M17 7L7 17" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/>',
    warning: '<path d="M12 7v6.5M12 17v.01" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"/>',
    info: '<path d="M12 11v6M12 7.5v.01" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"/>'
  };
  var SHOW_MS = { success: 3500, info: 4500, warning: 7000 };
  var stack = null;

  function host() {
    if (stack && document.body.contains(stack)) return stack;
    stack = document.createElement("div");
    stack.className = "kp-toasts";
    document.body.appendChild(stack);
    return stack;
  }

  window.kpToast = function (text, kind) {
    if (!text) return null;
    kind = ICONS[kind] ? kind : "info";
    var el = document.createElement("div");
    el.className = "kp-toast kp-toast-" + kind;
    el.setAttribute("role", kind === "error" ? "alert" : "status");
    el.innerHTML = '<svg class="kp-toast-ico" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><circle cx="12" cy="12" r="11" fill="currentColor" opacity=".22"/>' + ICONS[kind] + '</svg>' +
      '<span class="kp-toast-text"></span><button type="button" class="kp-toast-x" aria-label="Dismiss message" data-no-busy>&times;</button>';
    el.querySelector(".kp-toast-text").textContent = text;
    var box = host();
    while (box.children.length >= 3) box.removeChild(box.firstChild);          // never a wall of messages
    box.appendChild(el);
    window.requestAnimationFrame(function () { el.classList.add("is-in"); });
    var timer = null, paused = false;
    function gone() {
      clearTimeout(timer);
      el.classList.remove("is-in");
      window.setTimeout(function () { if (el.parentNode) el.parentNode.removeChild(el); }, 260);
    }
    el.querySelector(".kp-toast-x").addEventListener("click", gone);
    if (kind !== "error") {
      var tick = function () { if (paused) { timer = setTimeout(tick, 1200); } else { gone(); } };
      el.addEventListener("mouseenter", function () { paused = true; });
      el.addEventListener("mouseleave", function () { paused = false; });
      el.addEventListener("focusin", function () { paused = true; });
      el.addEventListener("focusout", function () { paused = false; });
      timer = setTimeout(tick, SHOW_MS[kind] || 4500);
    }
    return el;
  };
})();
