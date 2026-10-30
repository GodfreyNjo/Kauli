// Nobody should have to start again because they looked at something else. This file keeps people's work where they left it:
//
//   kpDrafts  every form saves what has been typed or chosen as it happens (on this device, for this account) and offers it back
//             when the page is opened again; the form is cleared the moment it is sent. Passwords, card details and hidden fields
//             are never saved.
//   kpFiles   files picked for an order are staged in the browser's own storage, so leaving the page does not lose them.
//   kpUploads one place that knows about an upload in progress: while one runs, links move around the app in place (so the upload
//             keeps going) and a small tray shows how far it has got on every page.
//
// Everything here is best effort: if the browser refuses storage (private window, full disk) the page simply works as it always did.
(function () {
  "use strict";
  var meta = document.querySelector('meta[name="kp-user"]');
  var UID = meta ? meta.getAttribute("content") : "";
  if (!UID) return;                                             // only for signed-in people

  var DRAFT_PREFIX = "kp:d:" + UID + ":";
  var MAX_AGE_MS = 14 * 24 * 3600 * 1000;
  var FILE_MAX_AGE_MS = 3 * 24 * 3600 * 1000;
  var FILE_MAX_BYTES = 2 * 1024 * 1024 * 1024;

  function ls() { try { return window.localStorage; } catch (e) { return null; } }
  function put(key, value) { var s = ls(); if (!s) return; try { s.setItem(DRAFT_PREFIX + key, JSON.stringify({ t: Date.now(), v: value })); } catch (e) { /* full */ } }
  function get(key) {
    var s = ls(); if (!s) return null;
    try {
      var raw = JSON.parse(s.getItem(DRAFT_PREFIX + key) || "null");
      if (!raw || Date.now() - raw.t > MAX_AGE_MS) { s.removeItem(DRAFT_PREFIX + key); return null; }
      return raw.v;
    } catch (e) { return null; }
  }
  function del(key) { var s = ls(); if (s) try { s.removeItem(DRAFT_PREFIX + key); } catch (e) { /* ignore */ } }
  function allKeys(prefix) {
    var s = ls(), out = []; if (!s) return out;
    try { for (var i = 0; i < s.length; i++) { var k = s.key(i); if (k && k.indexOf(prefix) === 0) out.push(k); } } catch (e) { /* ignore */ }
    return out;
  }
  function sweep() {                                              // forget drafts nobody came back to
    var s = ls(); if (!s) return;
    allKeys("kp:d:").forEach(function (k) {
      try { var raw = JSON.parse(s.getItem(k) || "null"); if (!raw || Date.now() - raw.t > MAX_AGE_MS) s.removeItem(k); } catch (e) { s.removeItem(k); }
    });
  }

  // ------------------------------------------------------------------------------------------------------------- form drafts
  var SKIP_TYPES = /^(password|hidden|file|submit|button|reset|image)$/i;
  var SKIP_NAMES = /(token|csrf|card|cvv|cvc|secret|otp|code)$/i;
  var forms = [];                                                 // [{form, key, initial, timer}]
  var hooks = {};                                                 // form id -> {get(), set(obj)} for state the fields do not hold (a wizard's step)

  function eligible(el) {
    if (!el.name || el.disabled) return false;
    if (el.tagName === "INPUT") { if (SKIP_TYPES.test(el.type)) return false; }
    else if (el.tagName !== "TEXTAREA" && el.tagName !== "SELECT") return false;
    if (el.hasAttribute("data-no-draft") || el.closest("[data-no-draft]")) return false;
    if (SKIP_NAMES.test(el.name) || /cc-/.test(el.getAttribute("autocomplete") || "")) return false;
    return true;
  }
  function fieldsOf(form) { return Array.prototype.filter.call(form.elements, eligible); }
  function snapshot(form) {
    var seen = {}, out = [];
    fieldsOf(form).forEach(function (el) {
      var i = seen[el.name] = (seen[el.name] || 0) + 1;
      var rec = { n: el.name, i: i };
      if (el.type === "checkbox" || el.type === "radio") { rec.c = el.checked; rec.v = el.value; }
      else if (el.tagName === "SELECT" && el.multiple) rec.v = Array.prototype.filter.call(el.options, function (o) { return o.selected; }).map(function (o) { return o.value; });
      else rec.v = el.value;
      out.push(rec);
    });
    return out;
  }
  function sameRec(a, b) { return a.c === b.c && JSON.stringify(a.v) === JSON.stringify(b.v); }
  function changesOf(entry) {
    var now = snapshot(entry.form), base = entry.initial, out = [];
    now.forEach(function (rec, k) { if (!base[k] || base[k].n !== rec.n || !sameRec(rec, base[k])) out.push(rec); });
    return out;
  }
  function formKey(form, index) {
    var id = form.id || (form.getAttribute("action") || "").replace(/[?#].*$/, "") || ("form" + index);
    return location.pathname + "#" + id;
  }
  function wanted(form) {
    var method = (form.getAttribute("method") || "get").toLowerCase();
    if (method === "get" || method === "dialog" || form.hasAttribute("data-no-draft")) return false;
    if (/^\/(login|logout|signup|forgot|reset)/.test(location.pathname)) return false;
    return fieldsOf(form).length > 0;
  }
  function entryFor(form) { for (var i = 0; i < forms.length; i++) if (forms[i].form === form) return forms[i]; return null; }

  function save(entry) {
    var changes = changesOf(entry), hook = hooks[entry.form.id], extra = hook && hook.get ? hook.get() : null;
    var meaningful = extra && Object.keys(extra).some(function (k) { return extra[k] && extra[k] !== 1; });
    if (!changes.length && !meaningful) { del("f:" + entry.key); return; }
    put("f:" + entry.key, { c: changes, x: meaningful ? extra : null });
  }
  function schedule(entry) { clearTimeout(entry.timer); entry.timer = setTimeout(function () { save(entry); }, 500); }

  function apply(entry, draft) {
    var seen = {}, byName = {};
    fieldsOf(entry.form).forEach(function (el) {
      var i = seen[el.name] = (seen[el.name] || 0) + 1;
      byName[el.name + "\u0000" + i] = el;
    });
    var touched = [];
    (draft.c || []).forEach(function (rec) {
      var el = byName[rec.n + "\u0000" + rec.i];
      if (!el) return;
      if (el.type === "checkbox" || el.type === "radio") { if (el.value !== rec.v) return; el.checked = !!rec.c; }
      else if (el.tagName === "SELECT" && el.multiple) { Array.prototype.forEach.call(el.options, function (o) { o.selected = (rec.v || []).indexOf(o.value) >= 0; }); }
      else if (el.tagName === "SELECT") { if (!Array.prototype.some.call(el.options, function (o) { return o.value === rec.v; })) return; el.value = rec.v; }
      else el.value = rec.v;
      touched.push(el);
    });
    touched.forEach(function (el) {                                // let the page's own code react (prices, previews, show/hide)
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
    });
    var hook = hooks[entry.form.id];
    if (draft.x && hook && hook.set) hook.set(draft.x);
    return touched.length > 0 || !!draft.x;
  }
  function restoreBar(entry) {
    var bar = document.createElement("div");
    bar.className = "kp-restore"; bar.setAttribute("role", "status");
    var text = document.createElement("span"); text.textContent = "We kept what you had entered here. ";
    var again = document.createElement("button"); again.type = "button"; again.className = "kp-restore-reset"; again.textContent = "Start over";
    var close = document.createElement("button"); close.type = "button"; close.className = "kp-restore-x"; close.setAttribute("aria-label", "Dismiss"); close.textContent = "×";
    bar.appendChild(text); bar.appendChild(again); bar.appendChild(close);
    entry.form.insertBefore(bar, entry.form.firstChild);
    close.addEventListener("click", function () { bar.remove(); });
    again.addEventListener("click", function () {
      del("f:" + entry.key);
      if (window.kpFiles && entry.form.id === "order-form") window.kpFiles.clear("order");
      location.reload();                                           // the plain, original form: nothing half-restored left behind
    });
  }

  function scan() {
    Array.prototype.forEach.call(document.querySelectorAll("#main-content form, main form"), function (form, index) {
      if (entryFor(form) || !wanted(form)) return;
      var entry = { form: form, key: formKey(form, index), initial: snapshot(form), timer: null };
      forms.push(entry);
      var draft = get("f:" + entry.key);
      if (draft) setTimeout(function () { if (apply(entry, draft)) restoreBar(entry); }, 0);   // after the page's own start-up code
    });
  }

  document.addEventListener("input", function (e) { var f = e.target && e.target.form, en = f && entryFor(f); if (en) schedule(en); }, true);
  document.addEventListener("change", function (e) { var f = e.target && e.target.form, en = f && entryFor(f); if (en) schedule(en); }, true);
  document.addEventListener("submit", function (e) {
    var en = entryFor(e.target);
    if (!en || en.form.hasAttribute("data-draft-keep")) return;     // a form that sends itself in the background clears its own draft once it has gone
    setTimeout(function () { if (!e.defaultPrevented) { clearTimeout(en.timer); del("f:" + en.key); } }, 0);
  });
  function flush() { forms.forEach(function (en) { if (en.timer) { clearTimeout(en.timer); en.timer = null; save(en); } }); }
  window.addEventListener("pagehide", flush);
  document.addEventListener("visibilitychange", function () { if (document.visibilityState === "hidden") flush(); });

  window.kpDrafts = {
    hooks: hooks,
    touch: function (form) { var en = form && entryFor(form); if (en) schedule(en); },
    clear: function (form) { var en = form && entryFor(form); if (en) { clearTimeout(en.timer); del("f:" + en.key); } },
    put: put, get: get, del: del,                                   // for editors that keep their own state: kpDrafts.put("note:12", {...})
    clearAll: function () { allKeys("kp:d:").forEach(function (k) { try { localStorage.removeItem(k); } catch (e) { /* ignore */ } }); }
  };

  // ------------------------------------------------------------------------------------------------------------- staged files
  var DB_NAME = "kauli-persist", STORE = "files";
  function db() {
    return new Promise(function (resolve, reject) {
      if (!window.indexedDB) return reject(new Error("no storage"));
      var req = indexedDB.open(DB_NAME, 1);
      req.onupgradeneeded = function () { req.result.createObjectStore(STORE, { keyPath: "id" }); };
      req.onsuccess = function () { resolve(req.result); };
      req.onerror = function () { reject(req.error); };
    });
  }
  function tx(mode, fn) {
    return db().then(function (d) {
      return new Promise(function (resolve, reject) {
        var t = d.transaction(STORE, mode), store = t.objectStore(STORE), out = fn(store);
        t.oncomplete = function () { resolve(out && out.result !== undefined ? out.result : undefined); d.close(); };
        t.onerror = t.onabort = function () { reject(t.error); d.close(); };
      });
    });
  }
  function fileId(slot, f) { return UID + "|" + slot + "|" + f.name + "|" + f.size + "|" + f.lastModified; }
  window.kpFiles = {
    add: function (slot, file) {
      if (!file || file.size > FILE_MAX_BYTES) return Promise.resolve(false);
      return tx("readwrite", function (s) { s.put({ id: fileId(slot, file), uid: UID, slot: slot, t: Date.now(), file: file }); }).then(function () { return true; }, function () { return false; });
    },
    remove: function (slot, file) { return tx("readwrite", function (s) { s.delete(fileId(slot, file)); }).catch(function () {}); },
    list: function (slot) {
      return tx("readonly", function (s) { return s.getAll(); }).then(function (rows) {
        return (rows || []).filter(function (r) { return r.uid === UID && r.slot === slot && Date.now() - r.t < FILE_MAX_AGE_MS && r.file; })
          .sort(function (a, b) { return a.t - b.t; }).map(function (r) { return r.file; });
      }).catch(function () { return []; });
    },
    clear: function (slot) {
      return tx("readwrite", function (s) {
        var req = s.getAll();
        req.onsuccess = function () { (req.result || []).forEach(function (r) { if (r.uid === UID && (!slot || r.slot === slot)) s.delete(r.id); }); };
      }).catch(function () {});
    },
    sweep: function () {
      return tx("readwrite", function (s) {
        var req = s.getAll();
        req.onsuccess = function () { (req.result || []).forEach(function (r) { if (Date.now() - r.t > FILE_MAX_AGE_MS) s.delete(r.id); }); };
      }).catch(function () {});
    }
  };

  // ------------------------------------------------------------------------------------------------------------- uploads in progress
  var subs = [];
  var uploads = window.kpUploads = {
    active: false, label: "", pct: 0, url: null, error: null,
    set: function (patch) { Object.keys(patch).forEach(function (k) { uploads[k] = patch[k]; }); subs.slice().forEach(function (fn) { try { fn(uploads); } catch (e) { /* a listener from a page that is gone */ } }); renderTray(); },
    on: function (fn) { subs.push(fn); },
    off: function (fn) { subs = subs.filter(function (f) { return f !== fn; }); },
    reset: function () { uploads.set({ active: false, label: "", pct: 0, url: null, error: null }); }
  };

  var tray = null;
  function ensureTray() {
    if (tray) return tray;
    tray = document.createElement("div");
    tray.className = "kp-tray"; tray.setAttribute("role", "status"); tray.setAttribute("aria-live", "polite"); tray.hidden = true;
    document.body.appendChild(tray);
    return tray;
  }
  function renderTray() {
    var t = ensureTray();
    var onWizard = !!document.getElementById("order-form");        // the wizard shows its own progress
    var show = !onWizard && (uploads.active || uploads.url || uploads.error);
    t.hidden = !show;
    if (!show) return;
    t.textContent = "";
    var ring = document.createElement("span"); ring.className = "kp-ring" + (uploads.active && !uploads.pct ? " is-spinning" : ""); ring.setAttribute("aria-hidden", "true");
    ring.style.setProperty("--p", uploads.url ? 100 : uploads.pct || 0);
    var b = document.createElement("b"); b.textContent = uploads.url ? "✓" : uploads.error ? "!" : (uploads.pct ? uploads.pct + "%" : ""); ring.appendChild(b);
    var text = document.createElement("span"); text.className = "kp-tray-text";
    if (uploads.url) {
      text.appendChild(document.createTextNode("Your order has started. "));
      var a = document.createElement("a"); a.href = uploads.url; a.textContent = "Open it"; text.appendChild(a);
    } else if (uploads.error) {
      text.appendChild(document.createTextNode(uploads.error + " "));
      var a2 = document.createElement("a"); a2.href = "/client"; a2.textContent = "Go back to it"; text.appendChild(a2);
    } else {
      text.appendChild(document.createTextNode(uploads.label || "Uploading"));
      var back = document.createElement("a"); back.href = "/client"; back.textContent = "See it"; text.appendChild(document.createTextNode(" · ")); text.appendChild(back);
    }
    t.appendChild(ring); t.appendChild(text);
    if (!uploads.active) {
      var x = document.createElement("button"); x.type = "button"; x.className = "kp-tray-x"; x.setAttribute("aria-label", "Dismiss"); x.textContent = "×";
      x.addEventListener("click", function () { uploads.reset(); });
      t.appendChild(x);
    }
  }

  // While an upload runs, moving around the app must not unload the page it is running in: links swap the content in place.
  function internal(a) {
    if (!a || !a.href || a.target === "_blank" || a.hasAttribute("download") || a.getAttribute("href").charAt(0) === "#") return false;
    var u; try { u = new URL(a.href, location.href); } catch (e) { return false; }
    if (u.origin !== location.origin) return false;
    if (u.pathname === location.pathname && u.search === location.search) return false;
    if (/^\/(logout|static|login\/google|api)\b/.test(u.pathname) || /\/(download|export|share)\b/.test(u.pathname) || /\.(?!html?$)[a-z0-9]{2,5}$/i.test(u.pathname)) return false;
    return true;
  }
  document.addEventListener("click", function (e) {
    if (!uploads.active || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    var a = e.target.closest ? e.target.closest("a") : null;
    if (!internal(a) || !window.kpSoftNavigate) return;
    e.preventDefault();
    window.kpSoftNavigate(a.href, true);
  });
  window.addEventListener("popstate", function () { if (uploads.active && window.kpSoftNavigate) window.kpSoftNavigate(location.href, false); });
  document.addEventListener("kp:softnav", function () { forms = forms.filter(function (en) { return document.contains(en.form); }); scan(); renderTray(); });
  document.addEventListener("click", function (e) {              // signing out on a shared computer: nothing of this account stays behind
    var a = e.target.closest ? e.target.closest('a[href="/logout"]') : null;
    if (a) { window.kpDrafts.clearAll(); window.kpFiles.clear(); }
  }, true);

  function start() { sweep(); window.kpFiles.sweep(); scan(); renderTray(); }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
})();
