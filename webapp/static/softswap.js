// Updating a page in place when something it was waiting for has finished: no browser reload, no flash, no jump.
// window.kpSoftRefresh() fetches the same page again and swaps only the main content; scripts inside it are run again, the scroll
// position stays where it was, and if the person is typing in a field it waits until they have finished.
(function () {
  "use strict";
  var pending = null;
  function editing(root) {
    var a = document.activeElement;
    return !!(a && root.contains(a) && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName) && (a.value || "").length && a.type !== "checkbox" && a.type !== "radio");
  }
  function runScripts(root) {
    root.querySelectorAll("script").forEach(function (old) {
      var s = document.createElement("script");
      Array.prototype.forEach.call(old.attributes, function (a) { s.setAttribute(a.name, a.value); });
      s.text = old.textContent;
      old.parentNode.replaceChild(s, old);
    });
  }
  function swap(cur, fresh, title, top) {
    var y = top ? 0 : window.scrollY;
    var hadFocus = cur.contains(document.activeElement) && document.activeElement !== cur && document.activeElement !== document.body;
    cur.style.transition = "opacity 0.15s ease"; cur.style.opacity = "0.6";
    window.setTimeout(function () {
      cur.innerHTML = fresh.innerHTML;
      runScripts(cur);
      if (title) document.title = title;
      window.scrollTo(0, y);
      cur.style.opacity = "";
      // Screen reader and keyboard users: a new page puts focus on its main heading and says what it is; an update of the same page
      // says so without stealing focus, unless the focused element was replaced (then focus goes to the heading, never to nowhere).
      if (top || hadFocus) {
        var heading = cur.querySelector("h1") || cur;
        if (!heading.hasAttribute("tabindex")) heading.setAttribute("tabindex", "-1");
        heading.focus({ preventScroll: true });
      }
      if (window.kpAnnounce) window.kpAnnounce(top ? (title || document.title) : "This page has been updated");
      document.dispatchEvent(new CustomEvent("kp:softnav"));
      window.setTimeout(function () { cur.style.transition = ""; }, 200);
    }, 150);
  }
  // Resolves true once the page shows the newer content, false if it could not be fetched (the caller keeps the page as it is).
  window.kpSoftRefresh = function () {
    if (pending) return pending;
    pending = fetch(location.pathname + location.search, { credentials: "same-origin", cache: "no-store", headers: { "X-Soft-Refresh": "1" } })
      .then(function (r) {
        if (!r.ok || /\/login/.test(r.url)) throw new Error("unavailable");
        return r.text();
      })
      .then(function (html) {
        var doc = new DOMParser().parseFromString(html, "text/html");
        var fresh = doc.getElementById("main-content"), cur = document.getElementById("main-content");
        if (!fresh || !cur) throw new Error("no content");
        var title = doc.title;
        if (fresh.innerHTML === cur.innerHTML) return true;   // nothing new yet: leave the page exactly as it is
        if (editing(cur)) {                                   // never pull the page from under someone who is typing
          document.addEventListener("focusout", function once() { document.removeEventListener("focusout", once); window.setTimeout(function () { swap(cur, fresh, title); }, 300); });
        } else {
          swap(cur, fresh, title);
        }
        return true;
      })
      .catch(function () { return false; })
      .then(function (ok) { pending = null; return ok; });
    return pending;
  };

  // Moving to another page without unloading this one (used while an upload is running, so it is not cut off). Falls back to a normal
  // visit if the page cannot be fetched.
  window.kpSoftNavigate = function (url, push) {
    return fetch(url, { credentials: "same-origin", headers: { "X-Soft-Refresh": "1" } })
      .then(function (r) {
        if (!r.ok || r.url.indexOf("/login") >= 0) throw new Error("unavailable");
        return r.text().then(function (html) { return { html: html, url: r.url }; });
      })
      .then(function (res) {
        var doc = new DOMParser().parseFromString(res.html, "text/html");
        var fresh = doc.getElementById("main-content"), cur = document.getElementById("main-content");
        if (!fresh || !cur) throw new Error("no content");
        if (push !== false) history.pushState({ kp: 1 }, "", res.url);
        var nav = document.querySelector(".sidebar-nav"), freshNav = doc.querySelector(".sidebar-nav");
        if (nav && freshNav) nav.innerHTML = freshNav.innerHTML;     // which page is highlighted, and the counts
        swap(cur, fresh, doc.title, true);
        return true;
      })
      .catch(function () { window.location.href = url; return false; });
  };
})();
