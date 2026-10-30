// Every button that sends a form shows that it is working (a small spinner, "please wait" to a screen reader) and cannot be pressed twice.
// Forms that run their own progress (the new-order wizard) or open a download can opt out with data-no-busy.
(function () {
  "use strict";
  var RESET_AFTER_MS = 25000;                       // a form that answers with a file never leaves the page: let the button come back
  function release(btn) {
    if (!btn || !btn._kpBusy) return;
    btn._kpBusy = false;
    btn.classList.remove("is-busy"); btn.removeAttribute("aria-busy"); btn.disabled = false;
    var s = btn.querySelector(":scope > .kp-spin"); if (s) s.remove();
  }
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!form || form.tagName !== "FORM" || form.hasAttribute("data-no-busy") || form.id === "order-form") return;
    var method = (form.getAttribute("method") || "get").toLowerCase();
    if (method === "get" || method === "dialog" || form.target === "_blank") return;
    var btn = e.submitter || form.querySelector('button[type="submit"], button:not([type])');
    if (!btn || btn._kpBusy) return;
    setTimeout(function () {                        // after every other handler (a confirm that said no keeps the button as it was)
      if (e.defaultPrevented) return;
      btn._kpBusy = true;
      btn.classList.add("is-busy"); btn.setAttribute("aria-busy", "true");
      var spin = document.createElement("span"); spin.className = "kp-spin"; spin.setAttribute("aria-hidden", "true");
      btn.insertBefore(spin, btn.firstChild);
      btn.disabled = true;
      setTimeout(function () { release(btn); }, RESET_AFTER_MS);
    }, 0);
  });
  window.addEventListener("pageshow", function (e) {  // back button: the page comes back from memory with the button still busy
    if (!e.persisted) return;
    document.querySelectorAll("button.is-busy").forEach(release);
  });
})();
