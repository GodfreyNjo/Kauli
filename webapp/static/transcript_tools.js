/* Transcript toolbar: share, download, find and replace, display toggles, shortcuts. Server rules (what can be downloaded or
   shared) are enforced by the routes; this only drives the dialogs. */
(function () {
  "use strict";
  var bar = document.getElementById("tx-tools");
  if (!bar) return;
  var orderId = bar.dataset.orderId;
  var table = document.getElementById("order-transcript-table");
  var $ = function (id) { return document.getElementById(id); };
  var store = {
    get: function (k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set: function (k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private window */ } }
  };

  // ---- dialogs
  [].forEach.call(document.querySelectorAll("[data-open]"), function (btn) {
    btn.addEventListener("click", function () {
      var target = $(btn.dataset.open);
      if (!target) return;
      var more = btn.closest("details"); if (more) more.open = false;
      if (target.tagName === "DIALOG") {
        if (!target.open) target.showModal();
        if (target.id === "tx-share-dialog") startShare();
      } else { openFind(); }
    });
  });
  [].forEach.call(document.querySelectorAll("dialog.tx-dialog"), function (d) {
    d.addEventListener("click", function (e) { if (e.target === d) d.close(); });     // click on the backdrop
  });

  // ---- display toggles
  function applyDisplay() {
    if (!table) return;
    table.classList.toggle("tx-hide-time", $("tx-hide-time").checked);
    table.classList.toggle("tx-hide-speaker", $("tx-hide-speaker").checked);
  }
  ["tx-hide-time", "tx-hide-speaker"].forEach(function (id) {
    var el = $(id);
    el.checked = store.get(id) === "1";
    el.addEventListener("change", function () { store.set(id, el.checked ? "1" : "0"); applyDisplay(); });
  });
  applyDisplay();
  var auto = $("tx-autoscroll");
  if (store.get("tx-autoscroll") === "0") auto.checked = false;
  auto.addEventListener("change", function () {
    store.set("tx-autoscroll", auto.checked ? "1" : "0");
    window.txAutoScroll = auto.checked;
  });
  window.txAutoScroll = auto.checked;

  // ---- undo / redo (only present on editable transcripts)
  var undo = $("tx-undo"), redo = $("tx-redo");
  function whenEditing(fn) { return function () { if (window.txStartEditing) window.txStartEditing(); fn(); }; }
  if (undo) undo.addEventListener("click", whenEditing(function () { document.execCommand("undo"); }));
  if (redo) redo.addEventListener("click", whenEditing(function () { document.execCommand("redo"); }));

  // ---- download
  function radio(name) { var r = document.querySelector('input[name="' + name + '"]:checked'); return r ? r.value : ""; }
  function syncDownloadOptions() {
    var fmt = radio("tx-fmt");
    var bare = fmt === "srt";                       // subtitle files have their own layout
    $("tx-split-set").hidden = bare || fmt === "csv";
    $("tx-opt-time").disabled = $("tx-opt-speaker").disabled = bare;
  }
  [].forEach.call(document.querySelectorAll('input[name="tx-fmt"]'), function (r) { r.addEventListener("change", syncDownloadOptions); });
  syncDownloadOptions();

  function exportUrl(fmt) {
    var q = ["format=" + encodeURIComponent(fmt === "clip" ? "txt" : fmt),
             "side=" + encodeURIComponent(radio("tx-side") || "source"),
             "split=" + encodeURIComponent(radio("tx-split") || "line"),
             "timestamps=" + ($("tx-opt-time").checked ? "1" : "0"),
             "speakers=" + ($("tx-opt-speaker").checked ? "1" : "0")];
    return "/account/orders/" + orderId + "/export?" + q.join("&");
  }
  function say(el, text, kind) {
    el.textContent = text || "";
    if (text && window.kpToast && kind) window.kpToast(text, kind);                // the result of an action is also shown as a clear, coloured message
  }
  $("tx-dl-go").addEventListener("click", function () {
    var fmt = radio("tx-fmt"), msg = $("tx-dl-msg"), go = $("tx-dl-go");
    go.disabled = true; say(msg, "Preparing...");
    fetch(exportUrl(fmt), { credentials: "same-origin" }).then(function (r) {
      if (!r.ok) return r.json().then(function (d) { throw new Error(d.error || "Could not prepare that file."); });
      if (fmt === "clip") {
        return r.text().then(function (text) {
          return navigator.clipboard.writeText(text).then(function () { say(msg, "Copied to the clipboard.", "success"); });
        });
      }
      var name = ((r.headers.get("Content-Disposition") || "").match(/filename="([^"]+)"/) || [])[1] || "transcript";
      return r.blob().then(function (blob) {
        var a = document.createElement("a");
        a.href = URL.createObjectURL(blob); a.download = name;
        document.body.appendChild(a); a.click(); a.remove();
        setTimeout(function () { URL.revokeObjectURL(a.href); }, 4000);
        if ($("tx-opt-media") && $("tx-opt-media").checked) {
          var m = document.createElement("a");
          m.href = "/account/orders/" + orderId + "/download/source"; m.download = "";
          document.body.appendChild(m); m.click(); m.remove();
        }
        say(msg, "Downloaded " + name, "success");
      });
    }).catch(function (e) { say(msg, e.message || "Could not download. Check your connection.", "error"); })
      .then(function () { go.disabled = false; });
  });

  // ---- share
  function post(action, extra) {
    var body = { action: action }; for (var k in (extra || {})) body[k] = extra[k];
    return fetch("/account/orders/" + orderId + "/share", { method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
      .then(function (r) { return r.json().then(function (d) { if (!r.ok) throw new Error(d.error || "Something went wrong."); return d; }); });
  }
  function startShare() {
    var msg = $("tx-share-msg"); say(msg, "");
    post("create").then(function (d) {
      $("tx-share-url").value = d.url; $("tx-share-wa").href = d.whatsapp;
    }).catch(function (e) { $("tx-share-url").value = ""; say(msg, e.message, "error"); });
  }
  $("tx-share-copy").addEventListener("click", function () {
    var u = $("tx-share-url").value; if (!u) return;
    (navigator.clipboard ? navigator.clipboard.writeText(u) : Promise.reject()).then(function () { say($("tx-share-msg"), "Link copied.", "success"); })
      .catch(function () { $("tx-share-url").select(); say($("tx-share-msg"), "Press Ctrl+C to copy.", "warning"); });
  });
  $("tx-share-send").addEventListener("click", function () {
    var to = $("tx-share-email").value.trim(), msg = $("tx-share-msg");
    if (!to) { say(msg, "Enter an email address.", "warning"); return; }
    say(msg, "Sending...");
    post("email", { to: to }).then(function () { say(msg, "Sent to " + to + ".", "success"); $("tx-share-email").value = ""; })
      .catch(function (e) { say(msg, e.message, "error"); });
  });
  $("tx-share-stop").addEventListener("click", function () {
    post("revoke").then(function () { $("tx-share-url").value = "Sharing is off."; $("tx-share-wa").removeAttribute("href"); say($("tx-share-msg"), "The link no longer works.", "info"); })
      .catch(function (e) { say($("tx-share-msg"), e.message, "error"); });
  });

  // ---- find and replace
  var panel = $("tx-find-panel"), q = $("tx-find-q"), rep = $("tx-find-r"), count = $("tx-find-count");
  var hits = [], cur = -1;
  function cells() { return [].slice.call(document.querySelectorAll("#order-transcript-table td[lang]")); }
  function clearMarks() { if (window.CSS && CSS.highlights) { CSS.highlights.delete("tx-hit"); CSS.highlights.delete("tx-hit-current"); } }
  function scan() {
    clearMarks(); hits = []; cur = -1;
    var needle = q.value;
    if (needle) {
      var lower = needle.toLowerCase();
      cells().forEach(function (c) {
        var node = c.firstChild; if (!node || node.nodeType !== 3) return;
        var text = node.data.toLowerCase(), i = 0;
        while ((i = text.indexOf(lower, i)) !== -1) {
          var r = document.createRange(); r.setStart(node, i); r.setEnd(node, i + needle.length);
          hits.push({ range: r, cell: c }); i += needle.length;
        }
      });
    }
    if (window.CSS && CSS.highlights && window.Highlight) CSS.highlights.set("tx-hit", new Highlight(hits.map(function (h) { return h.range; })));
    count.textContent = needle ? (hits.length ? hits.length + " found" : "No matches") : "";
    if (hits.length) go(0);
  }
  function go(i) {
    if (!hits.length) return;
    cur = (i + hits.length) % hits.length;
    var h = hits[cur];
    if (window.CSS && CSS.highlights && window.Highlight) CSS.highlights.set("tx-hit-current", new Highlight(h.range));
    h.cell.scrollIntoView({ block: "center", behavior: "smooth" });
    count.textContent = (cur + 1) + " of " + hits.length;
  }
  function openFind() { panel.hidden = false; q.focus(); q.select(); scan(); }
  function closeFind() { panel.hidden = true; clearMarks(); hits = []; }
  function replaceHits(list) {
    if (!rep || rep.disabled || !list.length) return;
    if (window.txStartEditing) window.txStartEditing();
    var by = new Map();
    list.forEach(function (h) { if (!by.has(h.cell)) by.set(h.cell, 0); by.set(h.cell, by.get(h.cell) + 1); });
    var needle = new RegExp(q.value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "gi");
    by.forEach(function (n, c) {
      c.textContent = c.textContent.replace(needle, function () { return rep.value; });
      c.dispatchEvent(new Event("input", { bubbles: true }));
    });
    scan();
    count.textContent = list.length + " replaced";
  }
  q.addEventListener("input", scan);
  q.addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); go(cur + (e.shiftKey ? -1 : 1)); } });
  $("tx-find-next").addEventListener("click", function () { go(cur + 1); });
  $("tx-find-prev").addEventListener("click", function () { go(cur - 1); });
  $("tx-find-close").addEventListener("click", closeFind);
  var one = $("tx-replace-one"), all = $("tx-replace-all");
  if (one) one.addEventListener("click", function () { if (hits.length) replaceHits([hits[Math.max(cur, 0)]]); });
  if (all) all.addEventListener("click", function () { replaceHits(hits.slice()); });

  // ---- keyboard
  function stepLine(dir) {
    var rows = [].slice.call(document.querySelectorAll("#order-transcript-table tr[data-segment-id]"));
    var active = document.activeElement && document.activeElement.closest ? document.activeElement.closest("tr[data-segment-id]") : null;
    var i = rows.indexOf(active); var next = rows[Math.max(0, Math.min(rows.length - 1, (i < 0 ? 0 : i + dir)))];
    if (!next) return;
    var cell = next.querySelector("td[data-edit]"); if (cell) { cell.focus(); }
    next.scrollIntoView({ block: "center", behavior: "smooth" });
  }
  document.addEventListener("keydown", function (e) {
    var mod = e.ctrlKey || e.metaKey, key = e.key.toLowerCase();
    if (mod && key === "f" && document.body.contains(table)) { e.preventDefault(); openFind(); return; }
    if (mod && key === "s") {
      var save = $("tx-save");
      if (save && !save.disabled) { e.preventDefault(); save.click(); }
      return;
    }
    if (e.altKey && (e.key === "ArrowUp" || e.key === "ArrowDown") && window.txEditing) { e.preventDefault(); stepLine(e.key === "ArrowUp" ? -1 : 1); return; }
    if (e.key === "Escape" && !panel.hidden) closeFind();
  });
})();
