/* Shared behaviour for every form on the site, so each page does not have to invent it.

   1. Confirm before a destructive action.  Put data-confirm="Question?" on a button, a link or a form, and
      data-danger as well to make the confirm button red.
   2. Loading state.  On submit the button is disabled at once (no double submits) and shows data-loading="Saving..."
      (default "Working..."). A form can opt out with data-no-loading.
   3. Inline validation.  On a form with data-validate, a field is checked when the person leaves it, and again as
      they type, and the message appears under the field. The browser's own rules are used (required, type, minlength,
      pattern), so the page and the check never disagree.
*/
(function () {
  "use strict";
  var RESET_AFTER_MS = 20000;   // a download keeps the page open; do not leave its button disabled for ever

  // ---------------------------------------------------------------- 1. confirm
  var dialog = null, pending = null;

  function buildDialog() {
    dialog = document.createElement("dialog");
    dialog.className = "ux-dialog";
    dialog.setAttribute("aria-labelledby", "ux-dialog-text");
    dialog.innerHTML = '<p id="ux-dialog-text"></p><div class="ux-dialog-actions">' +
      '<button type="button" class="secondary" data-ux="cancel">Cancel</button>' +
      '<button type="button" class="btn" data-ux="ok">Confirm</button></div>';
    document.body.appendChild(dialog);
    dialog.addEventListener("click", function (e) {
      var action = e.target.getAttribute && e.target.getAttribute("data-ux");
      if (action === "cancel") { dialog.close(); pending = null; }
      if (action === "ok") { var run = pending; dialog.close(); pending = null; if (run) run(); }
    });
    dialog.addEventListener("cancel", function () { pending = null; });
  }

  function askFirst(message, danger, whenSure) {
    if (typeof HTMLDialogElement === "undefined") { if (window.confirm(message)) whenSure(); return; }
    if (!dialog) buildDialog();
    dialog.querySelector("#ux-dialog-text").textContent = message;
    dialog.querySelector('[data-ux="ok"]').className = danger ? "btn btn-danger" : "btn";
    pending = whenSure;
    dialog.showModal();
  }

  function isDanger(element) {
    return element.hasAttribute("data-danger") || /\bbtn-danger(-outline)?\b/.test(element.className);
  }

  function proceed(element, action) {      // run the action with the guard off, then put the guard straight back
    element.dataset.confirmed = "1";
    try { action(); } finally { delete element.dataset.confirmed; }
  }

  document.addEventListener("click", function (e) {      // a button or link with data-confirm
    var trigger = e.target.closest ? e.target.closest("[data-confirm]") : null;
    if (!trigger || trigger.tagName === "FORM" || trigger.dataset.confirmed === "1") return;
    e.preventDefault();
    askFirst(trigger.getAttribute("data-confirm"), isDanger(trigger), function () {
      proceed(trigger, function () {
        if (trigger.form && trigger.type === "submit") trigger.form.requestSubmit(trigger); else trigger.click();
      });
    });
  });

  document.addEventListener("submit", function (e) {     // a whole form with data-confirm
    var form = e.target;
    if (!form.hasAttribute || !form.hasAttribute("data-confirm") || form.dataset.confirmed === "1") return;
    e.preventDefault();
    askFirst(form.getAttribute("data-confirm"), isDanger(form), function () {
      proceed(form, function () { form.requestSubmit(); });
    });
  });

  window.kauliConfirm = askFirst;                        // for scripts: kauliConfirm(message, isDanger, whenSure)

  // ----------------------------------------------------------------- 2. loading
  function setBusy(button) {
    if (button.dataset.uxOriginal === undefined) button.dataset.uxOriginal = button.innerHTML;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.classList.add("is-loading");
    button.textContent = button.getAttribute("data-loading") || "Working...";
  }

  function clearBusy(button) {
    if (button.dataset.uxOriginal === undefined) return;
    button.innerHTML = button.dataset.uxOriginal;
    button.disabled = false;
    button.removeAttribute("aria-busy");
    button.classList.remove("is-loading");
    delete button.dataset.uxOriginal;
    delete button.dataset.confirmed;
  }

  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (e.defaultPrevented || !form.matches || form.matches("[data-no-loading]") || form.target === "_blank") return;
    if ((form.method || "get").toLowerCase() === "get") return;
    var buttons = [].slice.call(form.querySelectorAll('button[type="submit"], button:not([type]), input[type="submit"]'));
    buttons.forEach(function (b) { if (b.tagName === "BUTTON") setBusy(b); });
    setTimeout(function () { buttons.forEach(clearBusy); }, RESET_AFTER_MS);
  });

  window.addEventListener("pageshow", function (e) {   // back button: the page comes back from the cache
    if (!e.persisted) return;
    [].forEach.call(document.querySelectorAll("button[aria-busy]"), clearBusy);
  });

  // ------------------------------------------------------------- 3. validation
  function messageFor(field) {
    var holder = field.parentNode.querySelector(".field-error[data-for='" + (field.id || field.name) + "']");
    if (!holder) {
      holder = document.createElement("small");
      holder.className = "field-error";
      holder.setAttribute("data-for", field.id || field.name);
      holder.setAttribute("aria-live", "polite");
      field.insertAdjacentElement("afterend", holder);
    }
    return holder;
  }

  function checkField(field) {
    var ok = field.checkValidity();
    field.setAttribute("aria-invalid", ok ? "false" : "true");
    messageFor(field).textContent = ok ? "" : field.validationMessage;
    return ok;
  }

  function validated(field) { return field.form && field.form.hasAttribute("data-validate") && field.willValidate; }

  document.addEventListener("focusout", function (e) {
    if (validated(e.target) && e.target.value !== "") { e.target.dataset.touched = "1"; checkField(e.target); }
  });
  document.addEventListener("input", function (e) {
    if (validated(e.target) && e.target.dataset.touched === "1") checkField(e.target);
  });
  document.addEventListener("invalid", function (e) {
    if (validated(e.target)) { e.target.dataset.touched = "1"; checkField(e.target); }
  }, true);
})();
