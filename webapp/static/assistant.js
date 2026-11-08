/* The Kauli assistant: one place for questions, updates and replies from the team.
   Home / chat: ask about your orders, your account or how to use Kauli (the assistant knows your own orders and passes anything that needs a
   person to the team). Inbox: everything addressed to you in one list (order updates, replies from the team, conversations), with one unread
   count on the button and a small pop-up when something important arrives. */
(function () {
  "use strict";
  var kT = window.kauliT || function (s, v) { return s; };   // the words the scripts show, in the reader's language (see webapp/js_strings.py)
  var root = document.getElementById("as-root");
  if (!root) return;
  var $ = function (id) { return document.getElementById(id); };
  var panel = $("as-panel"), openBtn = $("as-open"), log = $("as-log"), input = $("as-input");
  var threadId = null, ctxOrder = null, busy = false, threadsCache = [];
  var QUESTIONS = {
    product: ["What can Kauli do?", "Which languages do you support?", "How accurate is it?", "How do I make notes from a transcript?"],
    support: ["Where is my latest order?", "How do I download my transcript?", "My YouTube link failed", "How long does an order take?"],
    billing: ["What does each plan include?", "How many free minutes do I have left?", "How do I earn free minutes?", "What payment methods do you accept?"]
  };

  var name = root.dataset.name;
  $("as-hello").textContent = name ? kT("Hi {name}, how can we help?", { name: name }) : kT("Hi, how can we help?");

  function show(view) {
    $("as-home").hidden = view !== "home";
    $("as-chat").hidden = view !== "chat";
    $("as-msgs").hidden = view !== "msgs";
    $("as-form").hidden = view === "msgs";
    $("as-tab-home").classList.toggle("is-on", view !== "msgs");
    $("as-tab-msgs").classList.toggle("is-on", view === "msgs");
  }
  function setOpen(on) {
    panel.hidden = !on; openBtn.setAttribute("aria-expanded", on ? "true" : "false");
    if (on) { hideToast(); input.focus(); refresh(); }
  }
  openBtn.addEventListener("click", function () { setOpen(panel.hidden); });
  $("as-close").addEventListener("click", function () { setOpen(false); openBtn.focus(); });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape" && !panel.hidden) { setOpen(false); openBtn.focus(); } });

  function bubble(sender, text) {
    var d = document.createElement("div");
    d.className = "as-msg as-" + (sender === "user" ? "user" : sender === "staff" ? "staff" : "bot");
    if (sender === "staff") { var s = document.createElement("small"); s.textContent = kT("Kauli team"); d.appendChild(s); }
    var p = document.createElement("p"); p.textContent = text; d.appendChild(p);
    log.appendChild(d); log.scrollTop = log.scrollHeight;
    return d;
  }
  function api(url, body) {
    return fetch(url, { method: body ? "POST" : "GET", credentials: "same-origin", headers: body ? { "Content-Type": "application/json" } : {}, body: body ? JSON.stringify(body) : undefined })
      .then(function (r) { return r.json().then(function (d) { if (!r.ok) throw new Error(d.error || kT("Something went wrong.")); return d; }); });
  }

  function send(text, topic) {
    text = (text || "").trim();
    if (!text || busy) return;
    busy = true; show("chat"); bubble("user", text); input.value = "";
    var wait = bubble("bot", "...");
    api("/account/assistant/message", { text: text, thread_id: threadId, topic: topic || "", order_id: ctxOrder }).then(function (d) {
      threadId = d.thread_id; wait.querySelector("p").textContent = d.reply;
      $("as-handoff").hidden = !d.handoff || d.escalated;
      log.scrollTop = log.scrollHeight;
      if (d.escalated) refresh();
    }).catch(function (e) { wait.querySelector("p").textContent = e.message || kT("Could not reach the assistant. Check your connection."); })
      .then(function () { busy = false; });
  }
  $("as-form").addEventListener("submit", function (e) { e.preventDefault(); send(input.value); });

  [].forEach.call(document.querySelectorAll(".as-topics [data-topic]"), function (b) {
    b.addEventListener("click", function () {
      var box = $("as-chips"); box.textContent = "";
      QUESTIONS[b.dataset.topic].forEach(function (q) {
        var c = document.createElement("button"); c.type = "button"; c.textContent = kT(q);
        c.addEventListener("click", function () { send(q, b.dataset.topic); });
        box.appendChild(c);
      });
    });
  });

  $("as-escalate").addEventListener("click", function () {
    var btn = this; btn.disabled = true;
    api("/account/assistant/escalate", { thread_id: threadId }).then(function () {
      $("as-handoff").hidden = true;
      bubble("bot", "I have passed this to the Kauli team. They will reply here and by email.");
    }).catch(function (e) { bubble("bot", e.message); }).then(function () { btn.disabled = false; });
  });

  // ---- the inbox: updates and replies, then earlier conversations
  function ago(ts) {
    var s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
    if (s < 60) return kT("just now");
    if (s < 3600) return kT("{n} min ago", { n: Math.floor(s / 60) });
    if (s < 86400) return kT("{n} h ago", { n: Math.floor(s / 3600) });
    return kT("{n} d ago", { n: Math.floor(s / 86400) });
  }
  function openThread(t) {
    threadId = t.id; ctxOrder = t.order_id || null; log.textContent = "";
    t.messages.forEach(function (m) { bubble(m.sender, m.body); });
    $("as-handoff").hidden = t.escalated; show("chat");
    api("/account/assistant/seen", { thread_id: t.id }).then(refresh).catch(function () { /* a nicety */ });
  }
  function drawFeed(d) {
    var ul = $("as-feed"); ul.textContent = "";
    $("as-nofeed").hidden = d.items.length > 0;
    d.items.forEach(function (it) {
      var li = document.createElement("li"); if (it.unread) li.className = "is-unread";
      var b = document.createElement("button"); b.type = "button";
      var t = document.createElement("strong"); t.textContent = it.title; b.appendChild(t);
      if (it.body) { var p = document.createElement("span"); p.textContent = it.body; b.appendChild(p); }
      var when = document.createElement("small"); when.textContent = ago(it.at); b.appendChild(when);
      b.addEventListener("click", function () {
        if (it.source === "thread") { var th = threadsCache.filter(function (x) { return x.id === it.thread_id; })[0]; if (th) openThread(th); return; }
        if (it.source === "notification" && it.unread) fetch("/notifications/" + it.id + "/read", { method: "POST", credentials: "same-origin" });
        if (it.link) location.href = it.link;
      });
      li.appendChild(b); ul.appendChild(li);
    });
  }
  function drawThreads() {
    var ul = $("as-threads"); ul.textContent = "";
    $("as-nomsgs").hidden = threadsCache.length > 0;
    threadsCache.forEach(function (t) {
      var li = document.createElement("li"), b = document.createElement("button");
      b.type = "button";
      var last = t.messages.length ? t.messages[t.messages.length - 1] : null;
      b.innerHTML = "<strong></strong><span></span>";
      b.firstChild.textContent = t.subject || kT("Conversation");
      b.lastChild.textContent = (last && last.sender === "staff" ? kT("Team:") + " " : "") + (last ? last.body.slice(0, 70) : "");
      b.addEventListener("click", function () { openThread(t); });
      li.appendChild(b); ul.appendChild(li);
    });
  }
  var toastKey = "kauli_as_toast", lastCount = -1;
  function badge(n) {
    var c = $("as-count"); c.hidden = n < 1; c.textContent = n > 9 ? "9+" : String(n);
    $("as-dot").hidden = n < 1;
  }
  function hideToast() { $("as-toast").hidden = true; }
  function toast(it) {
    $("as-toast-title").textContent = it.title; $("as-toast-body").textContent = it.body || "";
    $("as-toast").hidden = false;
    setTimeout(hideToast, 12000);
  }
  function refresh() {
    return Promise.all([api("/account/assistant/feed"), api("/account/assistant/threads")]).then(function (r) {
      var d = r[0]; threadsCache = r[1].threads;
      drawFeed(d); drawThreads(); badge(d.unread);
      // a pop-up only for something important and new, once, and never while the assistant is open
      var seen = 0; try { seen = Number(sessionStorage.getItem(toastKey) || 0); } catch (e) { /* private window */ }
      var fresh = d.items.filter(function (i) { return i.unread && i.important && i.at > seen; });
      if (fresh.length) {
        try { sessionStorage.setItem(toastKey, String(fresh[0].at)); } catch (e) { /* private window */ }
        if (panel.hidden && lastCount !== -1) toast(fresh[0]);
      }
      lastCount = d.unread;
    }).catch(function () { /* the count is a nicety */ });
  }
  $("as-toast-btn").addEventListener("click", function () { setOpen(true); show("msgs"); });
  $("as-toast-x").addEventListener("click", hideToast);
  $("as-readall").addEventListener("click", function () {
    api("/account/assistant/feed/read", {}).then(refresh).catch(function () { /* a nicety */ });
  });
  $("as-tab-home").addEventListener("click", function () { show(threadId ? "chat" : "home"); });
  $("as-tab-msgs").addEventListener("click", function () { show("msgs"); refresh(); });

  // pages can open the assistant about one order: window.kauliAssistant.open({ orderId: "...", text: "..." })
  window.kauliAssistant = {
    open: function (o) {
      o = o || {};
      setOpen(true);
      ctxOrder = o.orderId || null; threadId = null; log.textContent = ""; $("as-handoff").hidden = true;
      show("chat");
      if (ctxOrder) {
        var mine = threadsCache.filter(function (t) { return t.order_id === ctxOrder; })[0];
        if (mine) { openThread(mine); }
        else { bubble("bot", kT("Ask me anything about this order: where it is, what you can download, or what to do next.")); }
      }
      if (o.text) { input.value = o.text; input.focus(); input.setSelectionRange(input.value.length, input.value.length); }
    }
  };

  setTimeout(refresh, 2500);
  setInterval(function () { if (!document.hidden) refresh(); }, 45000);
})();
