/* Staff: the "Review & send email" dialog of the sign-up analysis. Works with content that is loaded after the page (event delegation).
   Nothing is sent until staff press "Send email"; the draft is plain text they can change first. */
(function () {
  var current = null;
  function el(id) { return document.getElementById(id); }
  function drafts() {
    var node = el('fa-drafts');
    try { return node ? JSON.parse(node.textContent) : {}; } catch (e) { return {}; }
  }
  function setMsg(text, bad) {
    var m = el('fa-dlg-msg'); if (!m) return;
    m.textContent = text || ''; m.className = 'fa-dlg-msg' + (bad ? ' is-bad' : text ? ' is-ok' : '');
  }
  function open(id) {
    var d = drafts()[id], dlg = el('fa-dialog');
    if (!d || !dlg) return;
    current = id;
    el('fa-dlg-name').textContent = d.name || d.email;
    el('fa-dlg-to').textContent = 'To: ' + d.email + (d.last_followup ? '   (last emailed ' + d.last_followup + ')' : '');
    el('fa-dlg-subject').value = d.subject;
    el('fa-dlg-body').value = d.body;
    setMsg('');
    el('fa-dlg-send').disabled = false; el('fa-dlg-send').textContent = 'Send email';
    syncMailto();
    if (typeof dlg.showModal === 'function') dlg.showModal(); else dlg.setAttribute('open', '');
  }
  function close() { var dlg = el('fa-dialog'); if (!dlg) return; if (typeof dlg.close === 'function') dlg.close(); else dlg.removeAttribute('open'); }
  function syncMailto() {
    var d = drafts()[current]; if (!d) return;
    el('fa-dlg-mailto').href = 'mailto:' + d.email + '?subject=' + encodeURIComponent(el('fa-dlg-subject').value) + '&body=' + encodeURIComponent(el('fa-dlg-body').value);
  }
  function send() {
    var d = drafts()[current], btn = el('fa-dlg-send'); if (!d) return;
    var subject = el('fa-dlg-subject').value.trim(), body = el('fa-dlg-body').value.trim();
    if (subject.length < 3 || body.length < 20) { setMsg('Write a subject and a message first.', true); return; }
    btn.disabled = true; btn.textContent = 'Sending...'; setMsg('');
    var fd = new FormData(); fd.append('subject', subject); fd.append('body', body);
    fetch('/staff/customers/' + encodeURIComponent(current) + '/followup', { method: 'POST', body: fd, credentials: 'same-origin' })
      .then(function (r) { return r.json().catch(function () { return { ok: false, error: 'Unexpected reply (' + r.status + ').' }; }); })
      .then(function (res) {
        if (res.ok) {
          setMsg('Sent to ' + d.email + '. It is saved in their message history.', false);
          btn.textContent = 'Sent';
          var row = document.querySelector('.fa-send[data-id="' + current + '"]');
          if (row) { row.disabled = true; row.textContent = 'Email sent'; }
          setTimeout(close, 1600);
        } else {
          setMsg(res.error || 'It could not be sent.', true); btn.disabled = false; btn.textContent = 'Send email';
        }
      })
      .catch(function () { setMsg('Could not reach the server. Nothing was sent.', true); btn.disabled = false; btn.textContent = 'Send email'; });
  }
  document.addEventListener('click', function (e) {
    var b = e.target.closest && e.target.closest('.fa-send');
    if (b) { e.preventDefault(); open(b.dataset.id); return; }
    if (e.target.id === 'fa-dlg-cancel') { e.preventDefault(); close(); return; }
    if (e.target.id === 'fa-dlg-send') { e.preventDefault(); send(); }
  });
  document.addEventListener('input', function (e) { if (e.target.id === 'fa-dlg-subject' || e.target.id === 'fa-dlg-body') syncMailto(); });
})();
