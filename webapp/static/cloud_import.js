/* Process from the cloud, on the upload step. Two ways in:
   1. Paste a share link from Google Drive, OneDrive or Dropbox ("Anyone with the link"). Needs no setup (POST /account/import/link).
   2. A button per service that has its keys in .env: the service's own picker opens in the browser and Kauli only receives the one
      file that was chosen (GET /account/import/config says which are switched on). */
(function () {
  "use strict";
  var box = document.getElementById("cloud-import");
  if (!box) return;
  var status;

  function say(text) { if (status) status.textContent = text || ""; }
  function load(src, attrs) {
    return new Promise(function (resolve, reject) {
      var existing = document.querySelector('script[src="' + src + '"]');
      if (existing && existing.dataset.ready) return resolve();
      var s = existing || document.createElement("script");
      s.src = src; s.async = true;
      Object.keys(attrs || {}).forEach(function (k) { s.setAttribute(k, attrs[k]); });
      s.onload = function () { s.dataset.ready = "1"; resolve(); };
      s.onerror = function () { reject(new Error("Could not load " + src)); };
      if (!existing) document.head.appendChild(s);
    });
  }
  function send(body, endpoint) {
    say("Importing the file...");
    return fetch(endpoint || "/account/import/fetch", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
      .then(function (r) { return r.json().then(function (d) { if (!r.ok) throw new Error(d.error || "Could not import that file."); return d; }); })
      .then(function (d) {
        if (window.kauliUseCloudFile) window.kauliUseCloudFile(d.upload_key, d.filename);
        say("Imported " + d.filename + ". Continue to the next step.");
      })
      .catch(function (e) { say(e.message || "Could not import that file."); });
  }

  var providers = {
    dropbox: function (cfg) {
      return load("https://www.dropbox.com/static/api/2/dropins.js", { id: "dropboxjs", "data-app-key": cfg.app_key }).then(function () {
        window.Dropbox.choose({
          linkType: "direct", multiselect: false, extensions: ["audio", "video"], sizeLimit: 2 * 1024 * 1024 * 1024,
          success: function (files) { if (files && files[0]) send({ provider: "dropbox", url: files[0].link, name: files[0].name }); },
          cancel: function () { say(""); }
        });
      });
    },
    onedrive: function (cfg) {
      return load("https://js.live.net/v7.2/OneDrive.js").then(function () {
        window.OneDrive.open({
          clientId: cfg.client_id, action: "download", multiSelect: false, openInNewWindow: true,
          advanced: { filter: "audio,video", redirectUri: window.location.origin + "/account" },
          success: function (res) {
            var f = res && res.value && res.value[0];
            var url = f && (f["@microsoft.graph.downloadUrl"] || f["@content.downloadUrl"]);
            if (url) send({ provider: "onedrive", url: url, name: f.name }); else say("OneDrive did not give a download address. Try again.");
          },
          cancel: function () { say(""); },
          error: function () { say("OneDrive could not open. Check that pop-ups are allowed."); }
        });
      });
    },
    google: function (cfg) {
      return load("https://apis.google.com/js/api.js").then(function () { return load("https://accounts.google.com/gsi/client"); }).then(function () {
        return new Promise(function (resolve) { window.gapi.load("picker", resolve); });
      }).then(function () {
        return new Promise(function (resolve, reject) {
          var client = window.google.accounts.oauth2.initTokenClient({
            client_id: cfg.client_id, scope: "https://www.googleapis.com/auth/drive.file",
            callback: function (r) { if (r.error) reject(new Error("Google did not allow access.")); else resolve(r.access_token); }
          });
          client.requestAccessToken({ prompt: "" });
        });
      }).then(function (token) {
        var view = new window.google.picker.DocsView().setIncludeFolders(true)
          .setMimeTypes("audio/mpeg,audio/wav,audio/x-wav,audio/mp4,audio/x-m4a,audio/ogg,audio/flac,audio/aac,video/mp4,video/quicktime,video/webm,video/x-matroska,video/mpeg");
        new window.google.picker.PickerBuilder().addView(view).setOAuthToken(token).setDeveloperKey(cfg.api_key).setAppId(cfg.app_id)
          .setCallback(function (data) {
            if (data.action === window.google.picker.Action.PICKED && data.docs && data.docs[0]) {
              send({ provider: "google", file_id: data.docs[0].id, token: token, name: data.docs[0].name });
            }
          }).build().setVisible(true);
      });
    }
  };
  var labels = { google: "Google Drive", onedrive: "OneDrive", dropbox: "Dropbox" };

  // the link form is always there: it needs no keys
  var form = document.createElement("form");
  form.className = "ci-link";
  form.innerHTML = '<span class="ci-label">Process from the cloud</span>' +
    '<span class="ci-field"><input type="url" id="ci-link" placeholder="Paste a Google Drive, OneDrive or Dropbox share link" ' +
    'aria-label="Share link from Google Drive, OneDrive or Dropbox" autocomplete="off" maxlength="2000">' +
    '<button type="submit" class="ci-go">Import</button></span>' +
    '<span class="ci-hint">Set sharing to \u201cAnyone with the link\u201d first.</span>';
  box.appendChild(form);
  var buttons = document.createElement("div");
  buttons.className = "ci-buttons";
  box.appendChild(buttons);
  status = document.createElement("span"); status.className = "ci-status"; status.setAttribute("role", "status"); status.setAttribute("aria-live", "polite");
  box.appendChild(status);
  box.hidden = false;

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    var input = document.getElementById("ci-link");
    var link = input.value.trim();
    if (!link) { say("Paste the share link first."); input.focus(); return; }
    var go = form.querySelector(".ci-go");
    go.disabled = true;
    send({ url: link }, "/account/import/link").then(function () { go.disabled = false; });
  });

  if (new URLSearchParams(window.location.search).get("source") === "cloud") {       // the home page's cloud card
    var field = document.getElementById("ci-link");
    box.scrollIntoView({ block: "center" });
    if (field) field.focus();
  }

  fetch("/account/import/config", { credentials: "same-origin" }).then(function (r) { return r.ok ? r.json() : {}; }).then(function (cfg) {
    ["google", "onedrive", "dropbox"].filter(function (k) { return cfg[k]; }).forEach(function (k) {
      var b = document.createElement("button");
      b.type = "button"; b.textContent = "Pick from " + labels[k];
      b.addEventListener("click", function () { say(""); providers[k](cfg[k]).catch(function (e) { say(e.message || "That did not work. Try again."); }); });
      buttons.appendChild(b);
    });
  }).catch(function () { /* the pickers are a convenience; pasting a link still works */ });
})();
