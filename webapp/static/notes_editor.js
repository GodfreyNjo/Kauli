/* AI Notes editor: formatting buttons, autosave, and a Saved indicator. The server sanitises the HTML on every save. */
(function () {
  "use strict";
  var body = document.getElementById("nt-body"), title = document.getElementById("nt-title"), status = document.getElementById("nt-status");
  if (!body) return;
  var id = body.dataset.noteId, timer = null, dirty = false, saving = false;

  function setStatus(t) { status.textContent = t; }
  function save() {
    if (!dirty || saving) return Promise.resolve();
    saving = true; dirty = false; setStatus("Saving...");
    return fetch("/client/notes/" + id + "/save", { method: "POST", credentials: "same-origin", keepalive: true,
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ title: title.value, body_html: body.innerHTML }) })
      .then(function (r) { if (!r.ok) throw new Error(); setStatus(dirty ? "Unsaved changes" : "Saved"); })
      .catch(function () { dirty = true; setStatus("Not saved. Check your connection."); })
      .then(function () { saving = false; if (dirty) schedule(); });
  }
  function schedule() { clearTimeout(timer); timer = setTimeout(save, 800); }
  function touch() { dirty = true; setStatus("Unsaved changes"); schedule(); }

  body.addEventListener("input", touch);
  title.addEventListener("input", touch);
  body.addEventListener("paste", function (e) {                      // plain text only, no formatting from other pages
    e.preventDefault();
    document.execCommand("insertText", false, (e.clipboardData || window.clipboardData).getData("text/plain"));
  });
  [].forEach.call(document.querySelectorAll(".nt-toolbar [data-cmd]"), function (b) {
    b.addEventListener("mousedown", function (e) { e.preventDefault(); });          // keep the selection in the note
    b.addEventListener("click", function () { document.execCommand(b.dataset.cmd, false, null); body.focus(); touch(); });
  });
  [].forEach.call(document.querySelectorAll(".nt-toolbar [data-block]"), function (b) {
    b.addEventListener("mousedown", function (e) { e.preventDefault(); });
    b.addEventListener("click", function () { document.execCommand("formatBlock", false, b.dataset.block); body.focus(); touch(); });
  });
  document.getElementById("nt-link").addEventListener("click", function () {
    var url = window.prompt("Link address (https://...)");
    if (url && /^(https?:\/\/|mailto:)/i.test(url.trim())) { body.focus(); document.execCommand("createLink", false, url.trim()); touch(); }
  });
  document.addEventListener("keydown", function (e) {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") { e.preventDefault(); dirty = true; save(); }
  });
  document.addEventListener("visibilitychange", function () { if (document.visibilityState === "hidden") save(); });
  window.addEventListener("beforeunload", function (e) { if (dirty) { e.preventDefault(); e.returnValue = ""; } });
})();
