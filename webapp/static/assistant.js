/* Help assistant: topics, chat, hand-off to the team, and the Messages tab (including the team's replies). */
(function () {
  "use strict";
  var root = document.getElementById("as-root");
  if (!root) return;
  var $ = function (id) { return document.getElementById(id); };
  var panel = $("as-panel"), openBtn = $("as-open"), log = $("as-log"), input = $("as-input");
  var threadId = null, busy = false;
  var QUESTIONS = {
    product: ["What can Kauli do?", "Which languages do you support?", "How accurate is it?", "How do I make notes from a transcript?"],
    support: ["How do I download my transcript?", "My YouTube link failed", "How do I edit a transcript?", "How long does an order take?"],
    billing: ["What does each plan include?", "How do I cancel or downgrade?", "How do I earn free minutes?", "What payment methods do you accept?"]
  };

  var name = root.dataset.name;
  $("as-hello").textContent = name ? "Hi " + name + ", how can we help?" : "Hi, how can we help?";

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
    if (on) { input.focus(); checkReplies(); }
  }
  openBtn.addEventListener("click", function () { setOpen(panel.hidden); });
  $("as-close").addEventListener("click", function () { setOpen(false); openBtn.focus(); });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape" && !panel.hidden) { setOpen(false); openBtn.focus(); } });

  function bubble(sender, text) {
    var d = document.createElement("div");
    d.className = "as-msg as-" + (sender === "user" ? "user" : sender === "staff" ? "staff" : "bot");
    if (sender === "staff") { var s = document.createElement("small"); s.textContent = "Kauli team"; d.appendChild(s); }
    var p = document.createElement("p"); p.textContent = text; d.appendChild(p);
    log.appendChild(d); log.scrollTop = log.scrollHeight;
    return d;
  }
  function api(url, body) {
    return fetch(url, { method: body ? "POST" : "GET", credentials: "same-origin", headers: body ? { "Content-Type": "application/json" } : {}, body: body ? JSON.stringify(body) : undefined })
      .then(function (r) { return r.json().then(function (d) { if (!r.ok) throw new Error(d.error || "Something went wrong."); return d; }); });
  }

  function send(text, topic) {
    text = (text || "").trim();
    if (!text || busy) return;
    busy = true; show("chat"); bubble("user", text); input.value = "";
    var wait = bubble("bot", "...");
    api("/client/assistant/message", { text: text, thread_id: threadId, topic: topic || "" }).then(function (d) {
      threadId = d.thread_id; wait.querySelector("p").textContent = d.reply;
      $("as-handoff").hidden = !d.handoff || d.escalated;
      log.scrollTop = log.scrollHeight;
    }).catch(function (e) { wait.querySelector("p").textContent = e.message || "Could not reach the assistant. Check your connection."; })
      .then(function () { busy = false; });
  }
  $("as-form").addEventListener("submit", function (e) { e.preventDefault(); send(input.value); });

  [].forEach.call(document.querySelectorAll(".as-topics [data-topic]"), function (b) {
    b.addEventListener("click", function () {
      var box = $("as-chips"); box.textContent = "";
      QUESTIONS[b.dataset.topic].forEach(function (q) {
        var c = document.createElement("button"); c.type = "button"; c.textContent = q;
        c.addEventListener("click", function () { send(q, b.dataset.topic); });
        box.appendChild(c);
      });
    });
  });

  $("as-escalate").addEventListener("click", function () {
    var btn = this; btn.disabled = true;
    api("/client/assistant/escalate", { thread_id: threadId }).then(function () {
      $("as-handoff").hidden = true;
      bubble("bot", "I have passed this to the Kauli team. They will reply here and by email.");
    }).catch(function (e) { bubble("bot", e.message); }).then(function () { btn.disabled = false; });
  });

  // Messages tab: earlier conversations, including the team's replies.
  var seenKey = "kauli_as_seen";
  function loadThreads() {
    return api("/client/assistant/threads").then(function (d) {
      var ul = $("as-threads"); ul.textContent = "";
      $("as-nomsgs").hidden = d.threads.length > 0;
      d.threads.forEach(function (t) {
        var li = document.createElement("li"), b = document.createElement("button");
        b.type = "button";
        var last = t.messages.length ? t.messages[t.messages.length - 1] : null;
        b.innerHTML = "<strong></strong><span></span>";
        b.firstChild.textContent = t.subject || "Conversation";
        b.lastChild.textContent = (last && last.sender === "staff" ? "Team: " : "") + (last ? last.body.slice(0, 70) : "");
        b.addEventListener("click", function () {
          threadId = t.id; log.textContent = "";
          t.messages.forEach(function (m) { bubble(m.sender, m.body); });
          $("as-handoff").hidden = t.escalated; show("chat");
        });
        li.appendChild(b); ul.appendChild(li);
      });
      return d.threads;
    });
  }
  function checkReplies() {
    loadThreads().then(function (threads) {
      var latest = 0;
      threads.forEach(function (t) { t.messages.forEach(function (m) { if (m.sender === "staff" && m.at > latest) latest = m.at; }); });
      var seen = 0; try { seen = Number(localStorage.getItem(seenKey) || 0); } catch (e) { /* private window */ }
      $("as-dot").hidden = !(latest > seen);
      $("as-tab-msgs").dataset.latest = latest;
    }).catch(function () { /* the dot is a nicety */ });
  }
  $("as-tab-home").addEventListener("click", function () { show(threadId ? "chat" : "home"); });
  $("as-tab-msgs").addEventListener("click", function () {
    show("msgs"); loadThreads();
    try { localStorage.setItem(seenKey, $("as-tab-msgs").dataset.latest || String(Date.now() / 1000)); } catch (e) { /* private window */ }
    $("as-dot").hidden = true;
  });
  setTimeout(checkReplies, 4000);
})();
