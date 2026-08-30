/* Kauli word-synced editor - two-step workflow: correct the Swahili ASR
 * source first, then the English translation (optionally re-translated
 * from the corrected source). Own design: word-level cells tied to
 * timestamps, click-to-seek, keyboard-driven correction - the general
 * "transcript cells synced to media" pattern used across the industry
 * (Trint, Descript, Otter, etc.), built around kauli's own Segment/Word
 * data, not modeled on any specific vendor's tool.
 */
(function () {
  "use strict";

  let ORDER_ID = null;
  // Real per-order languages (an en->sw job has these the other way
  // round from the far more common sw->en one) - set once by
  // initKauliEditor, read anywhere a message needs to name a side
  // instead of assuming it's always Swahili source / English target.
  let SOURCE_LANG_NAME = "the source language";
  let TARGET_LANG_NAME = "the target language";
  let audio = null;
  let allCells = [];      // flat list of {el, startMs, endMs} across BOTH steps, all segments
  let currentStep = "source"; // "source" | "target"
  // "Voice" isn't a third value of currentStep - it's the target step
  // (same cells, same autosave/spellcheck/re-translate) with the player
  // additionally driven by the cloned dub track instead of the original
  // source audio, so a click-to-seek or Space/arrow shortcut needs to act
  // on whichever element is actually making sound right now.
  let voiceMode = false;
  function currentAudio() {
    const vt = document.getElementById("voice-track-media");
    return voiceMode && vt ? vt : audio;
  }
  let previewing = false;
  let macros = new Array(10).fill(""); // index 0-8 = Ctrl+1..Ctrl+9, index 9 = Ctrl+0
  let bookmarkedCells = new Set(); // personal markers, local only (see loadBookmarks) - not job data
  // Ctrl+Shift+I toggles this - while true, every cell you focus (click,
  // Tab, arrow into) gets italicized automatically, for marking a whole
  // run of cells (a song, a foreign-language aside) without hitting Alt+I
  // on each one individually. See bindShortcuts and the focusin listener
  // it wires up below. Turning it off never un-italicizes anything - it
  // only stops applying it to cells you land on next.
  let italicModeActive = false;

  // Continuous-document model: cells from every segment live together in
  // one shared flow per step (see #flow-source/#flow-target in editor.html)
  // so the transcript reads as one piece of prose, not a stack of chunks -
  // segment identity survives only as each cell's data-segment-id, plus
  // this metadata map for anything that isn't in the DOM anymore (flag
  // reasons, fit score, cultural notes, MT confidence).
  let segmentMeta = {}; // segmentId -> {review_flag, review_reasons, fit_ratio, fit_status, cultural_notes, translation_confidence}
  // The raw array handed to initKauliEditor, kept live (not just its
  // page-load snapshot) - bindPreviewCaptions/onTimeUpdate-for-preview
  // reads straight off this on every tick, so updatePreviewSegment (called
  // after every save/retranslate) is what keeps the Preview tab's caption
  // bar showing what's actually in the English transcript right now,
  // instead of whatever it was when the page first loaded.
  let previewSegments = [];
  function updatePreviewSegment(segmentId, wrappedCaption, displayEndMs) {
    const seg = previewSegments.find((s) => s.segment_id === segmentId);
    if (!seg) return;
    if (wrappedCaption !== undefined) seg.wrapped_caption = wrappedCaption;
    if (displayEndMs !== undefined) seg.display_end_ms = displayEndMs;
  }
  const AUTOSAVE_DELAY_MS = 1800;
  let pendingSaveTimers = {}; // `${segmentId}:${step}` -> timeout id
  let dirtySegments = new Set();

  function $(sel, root) { return (root || document).querySelector(sel); }
  function $all(sel, root) { return Array.from((root || document).querySelectorAll(sel)); }

  function escapeHtml(s) {
    const d = document.createElement("div");
    d.textContent = s || "";
    return d.innerHTML;
  }

  // ------------------------------------------------------ cell building ----
  // A flagged segment gets no visible block of its own any more (see
  // renderAllSegments) - just a subtle underline on its cells and the
  // reasons on a native hover tooltip on the first one, so the reading
  // flow isn't broken by a callout box for every flagged sentence.
  function buildFlagTitle(meta) {
    if (!meta || !meta.review_flag) return "";
    const reasons = (meta.review_reasons || []).join("; ");
    let title = `${(meta.review_reasons || []).length} issue${(meta.review_reasons || []).length === 1 ? "" : "s"}` +
      (meta.fit_ratio != null ? ` - fit ${meta.fit_ratio} (${meta.fit_status})` : "");
    if (reasons) title += ": " + reasons;
    if (meta.cultural_notes) title += ` | Note: ${meta.cultural_notes}`;
    return title;
  }

  function buildCells(container, cells, segmentId, fallbackText, kind, flagTitle, staleTitle) {
    // kind: "source" (real timing/confidence) or "target" (approximate word
    // timing). Both can carry real gap cells now. flagTitle, when set, is
    // applied to the first cell only (hover tooltip) and every cell in the
    // segment gets the subtle .flagged-cell treatment. staleTitle (target
    // cells only) marks a translation that's fallen out of sync with a
    // since-corrected source - see the .stale-translation CSS and
    // db.translation_stale for why this is a distinct signal from
    // flagged-cell, not the same thing reused: this doesn't mean anything
    // is wrong with the English text itself, it means it may no longer
    // match what's now in step 1.
    if (cells && cells.length) {
      cells.forEach((c, i) => {
        const cell = document.createElement("span");
        cell.contentEditable = "true";
        cell.tabIndex = 0;
        cell.dataset.startMs = c.start_ms;
        cell.dataset.endMs = c.end_ms;
        cell.dataset.segmentId = segmentId;
        cell.addEventListener("focus", () => seekTo(c.start_ms, false));
        cell.addEventListener("input", () => {
          markDirty(segmentId);
          autoCapitalizeNextAfterSentenceEnd(cell);
          if (cell.classList.contains("spell-flag")) { cell.classList.remove("spell-flag"); hideSpellPopover(); }
        });

        if (c.type === "gap") {
          cell.className = "gapcell";
          cell.textContent = "";
          cell.dataset.placeholder =
            `+ tag (${(c.start_ms / 1000).toFixed(1)}–${(c.end_ms / 1000).toFixed(1)}s)`;
        } else {
          cell.className = kind === "source" ? "wordcell" : "wordcell approx";
          if (c.speaker_tag) cell.classList.add("speaker-tag-cell");
          if (c.bracket_tag) cell.classList.add("bracket-tag-cell");
          cell.textContent = c.text + " ";
          if (kind === "source" && typeof c.confidence === "number") {
            cell.style.opacity = String(Math.max(0.45, Math.min(1, c.confidence)));
          }
        }
        if (flagTitle) {
          cell.classList.add("flagged-cell");
          if (i === 0) cell.title = flagTitle;
        }
        if (staleTitle) {
          cell.classList.add("stale-translation");
          if (i === 0) cell.title = staleTitle;
        }
        if (bookmarkedCells.has(cellKey(cell))) cell.classList.add("bookmarked");
        if (c.para_start) {
          // Mirrors insertParagraphBreak's own DOM shape (real <br> sibling
          // + dataset.paraStart + .para-start class) so a paragraph break
          // saved earlier renders identically on reload, not just live -
          // this is what makes it durable instead of disappearing the
          // moment cells get rebuilt from saved text (see the
          // _tokenize_with_speaker_tag docstring in webapp/app.py).
          container.appendChild(document.createElement("br"));
          cell.dataset.paraStart = "true";
          cell.classList.add("para-start");
        }
        container.appendChild(cell);
        allCells.push({ el: cell, startMs: c.start_ms, endMs: c.end_ms });
      });
    } else {
      const fb = document.createElement("div");
      fb.className = "editor-fallback";
      fb.contentEditable = "true";
      fb.tabIndex = 0;
      fb.textContent = fallbackText || "";
      fb.dataset.segmentId = segmentId;
      if (flagTitle) { fb.classList.add("flagged-cell"); fb.title = flagTitle; }
      if (staleTitle) { fb.classList.add("stale-translation"); fb.title = staleTitle; }
      fb.addEventListener("input", () => markDirty(segmentId));
      container.appendChild(fb);
    }
  }

  function staleTranslationTitle(seg) {
    return seg && seg.translation_stale
      ? "The " + SOURCE_LANG_NAME + " source was corrected after this translation - click Re-translate " +
        "(Alt+R) to sync it, or edit this " + TARGET_LANG_NAME + " text directly."
      : null;
  }

  // Toggles the stale-translation marker on a segment's EXISTING target
  // cells in place - used when the source save / target save / retranslate
  // responses come back, so this reflects live without waiting for a
  // reload (buildCells only sets it at initial render / cell-rebuild time).
  function applyStaleTranslationUI(segmentId, stale) {
    const title = staleTranslationTitle({ translation_stale: stale }) || "";
    $all(`#flow-target [data-segment-id="${segmentId}"]`).forEach((cell, i) => {
      cell.classList.toggle("stale-translation", stale);
      if (i === 0) cell.title = title;
      else if (!stale) cell.title = "";
    });
  }

  // Reconstructs text from cells in DOM order - the whole flow container
  // when segmentId is omitted (used for the prose preview), or just one
  // segment's cells when it's given (used for per-segment autosave). A
  // cell marked as a paragraph start (see insertParagraphBreak) gets a
  // blank line before it instead of a space - that's how paragraph breaks
  // survive save/reload without needing a separate data model for them.
  function cellsText(container, segmentId) {
    let cells = $all(".wordcell, .gapcell", container);
    if (segmentId) cells = cells.filter((c) => c.dataset.segmentId === segmentId);
    if (cells.length) {
      let out = "";
      cells.forEach((c) => {
        const t = c.textContent.trim();
        if (!t) return;
        if (out && c.dataset.paraStart === "true") out += "\n\n";
        else if (out) out += " ";
        out += t;
      });
      return out;
    }
    const fb = segmentId
      ? container.querySelector(`.editor-fallback[data-segment-id="${segmentId}"]`)
      : container.querySelector(".editor-fallback");
    return fb ? fb.textContent.trim() : "";
  }

  // ---------------------------------------------------------- rendering ----
  // Continuous-document model: no per-segment block, no header, no inline
  // diagnostics callout, no per-segment Save button - a segment contributes
  // its cells directly into the shared flow (#flow-source / #flow-target)
  // so reading it feels like one piece of prose, with paragraph breaks
  // (Ctrl+Enter) doing the structuring, not mechanical ASR-chunk boundaries.
  // Everything a segment header used to show (id, timing, flag reasons,
  // cultural notes) lives in segmentMeta and surfaces as a hover tooltip on
  // a flagged segment's first cell instead (see buildFlagTitle).
  function renderAllSegments(segments) {
    const sourceFlow = $("#flow-source");
    const targetFlow = $("#flow-target");
    segments.forEach((seg) => {
      segmentMeta[seg.segment_id] = {
        review_flag: seg.review_flag, review_reasons: seg.review_reasons,
        fit_ratio: seg.fit_ratio, fit_status: seg.fit_status,
        cultural_notes: seg.cultural_notes, translation_confidence: seg.translation_confidence,
        translation_stale: seg.translation_stale, speaker_id: seg.speaker_id,
        manual_pace_pct: seg.manual_pace_pct, spell_out: seg.spell_out,
      };
      const title = buildFlagTitle(segmentMeta[seg.segment_id]);
      buildCells(sourceFlow, seg.source_cells, seg.segment_id, seg.source_final_text, "source", title);
      buildCells(targetFlow, seg.target_cells, seg.segment_id, seg.final_text, "target", title,
                 staleTranslationTitle(segmentMeta[seg.segment_id]));
    });
  }

  // ------------------------------------------------- YouTube media adapter ----
  // Wraps YT.Player behind the exact same interface a native <audio>/<video>
  // element already exposes (currentTime, paused, duration, playbackRate,
  // play(), pause(), addEventListener("timeupdate", cb)) - every other
  // function in this file talks to `audio` through that interface and
  // doesn't need to know or care which kind it actually got.
  function createYouTubeAdapter(containerId, videoId) {
    let player = null;
    let ready = false;
    const timeupdateListeners = [];
    const endedListeners = [];
    const playListeners = [];

    const adapter = {
      get currentTime() { return ready ? player.getCurrentTime() : 0; },
      set currentTime(v) { if (ready) player.seekTo(v, true); },
      get paused() { return !ready || player.getPlayerState() !== 1; },
      get duration() { return ready ? player.getDuration() : 0; },
      get playbackRate() { return ready ? player.getPlaybackRate() : 1; },
      set playbackRate(v) { if (ready) player.setPlaybackRate(v); },
      // Real YT.Player mute API - bindCustomControls' mute button needs
      // this to actually do anything for a YouTube-sourced order, not
      // just toggle a property nothing reads.
      get muted() { return ready && player.isMuted(); },
      set muted(v) { if (ready) { v ? player.mute() : player.unMute(); } },
      play() { if (ready) player.playVideo(); },
      pause() { if (ready) player.pauseVideo(); },
      addEventListener(evt, cb) {
        if (evt === "timeupdate") timeupdateListeners.push(cb);
        else if (evt === "ended") endedListeners.push(cb);
        else if (evt === "play") playListeners.push(cb);
      },
    };

    function initPlayer() {
      player = new YT.Player(containerId, {
        videoId: videoId,
        // YouTube's own default is already "don't loop" (no loop param
        // set) - onStateChange below just makes that explicit and lets the
        // editor react to it (the end-of-video burner), not a fix for an
        // actual autoplay-loop bug.
        playerVars: { rel: 0 },
        events: {
          onReady: function () {
            ready = true;
            // The IFrame API has no native timeupdate event - poll instead,
            // same granularity as a native element's typical firing rate.
            setInterval(function () {
              timeupdateListeners.forEach(function (cb) { cb(); });
            }, 250);
          },
          onStateChange: function (event) {
            if (event.data === YT.PlayerState.ENDED) endedListeners.forEach(function (cb) { cb(); });
            else if (event.data === YT.PlayerState.PLAYING) playListeners.forEach(function (cb) { cb(); });
          },
        },
      });
    }

    if (window.YT && window.YT.Player) {
      initPlayer();
    } else {
      const prev = window.onYouTubeIframeAPIReady;
      window.onYouTubeIframeAPIReady = function () {
        if (prev) prev();
        initPlayer();
      };
      const tag = document.createElement("script");
      tag.src = "https://www.youtube.com/iframe_api";
      document.head.appendChild(tag);
    }

    return adapter;
  }

  // -------------------------------------------------------- audio sync ----
  function seekTo(ms, autoplay) {
    const a = currentAudio();
    if (!a) return;
    a.currentTime = ms / 1000;
    if (autoplay) a.play();
  }

  // Shift+Space: seek to the focused cell's start and play - keeps
  // playing normally past the cell's own end, same as any other playback,
  // until the editor pauses it themselves (Space, clicking the video, or
  // the custom controls). No auto-stop.
  function playCellSnippet(cell) {
    const startMs = Number(cell.dataset.startMs);
    const a = currentAudio();
    if (!a || Number.isNaN(startMs)) return;
    a.currentTime = startMs / 1000;
    a.play();
  }

  function onTimeUpdate() {
    const t = currentAudio().currentTime * 1000;
    let active = null;
    for (const c of allCells) {
      const isPlaying = t >= c.startMs && t < c.endMs;
      c.el.classList.toggle("playing", isPlaying);
      if (isPlaying && c.el.offsetParent !== null) active = c.el; // only auto-scroll visible cells
    }
    // Don't yank the view away from a cell someone's actively typing in -
    // playback-follow only applies while nothing is focused for editing.
    // Playback itself never stops or is affected by editing a cell (nothing
    // here calls audio.pause()) - this only controls whether the page
    // scrolls to chase the currently-playing cell while you're elsewhere
    // fixing something.
    if (active && !isEditingCell(document.activeElement)) {
      active.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
  }

  function setRate(delta) {
    const a = currentAudio();
    const r = Math.max(0.5, Math.min(2.0, Math.round((a.playbackRate + delta) * 100) / 100));
    setRateAbsolute(r);
  }

  function setRateAbsolute(r) {
    currentAudio().playbackRate = r;
    const label = $("#speed-picker-label");
    if (label) label.textContent = r.toFixed(2) + "x";
    $all("#speed-picker-menu .stage-picker-item").forEach((b) => {
      b.classList.toggle("active", Number(b.dataset.rate) === r);
    });
  }

  // Shown when playback reaches the end, workspace-only - it's never part
  // of any exported deliverable (burned captions, dubbed video, ...), just
  // a signal in Ereri itself that this clip is done. Playback never
  // auto-restarts on its own; this only appears because it stopped, and
  // clears again the moment you actually press play or seek.
  function showEndBurner() {
    const el = $("#video-end-burner");
    if (el) el.hidden = false;
  }
  function hideEndBurner() {
    const el = $("#video-end-burner");
    if (el) el.hidden = true;
  }

  // -------------------------------------------------- custom video bar ----
  // Only wired up for a real uploaded <video> - YouTube keeps its own
  // native player controls, and audio-only files keep the plain native
  // <audio controls> bar (see editor.html for why).
  function formatTime(seconds) {
    if (!isFinite(seconds) || seconds < 0) seconds = 0;
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = Math.floor(seconds % 60);
    return [h, m, s].map((v) => String(v).padStart(2, "0")).join(":");
  }

  function bindCustomControls() {
    const bar = $("#custom-controls");
    // Real bug this fixes: this used to require audio.tagName === "VIDEO",
    // which is only ever true for a native <video> element - the YouTube
    // adapter (createYouTubeAdapter) is a plain JS object with no
    // tagName at all, so this returned immediately for EVERY YouTube-
    // sourced order. That meant Kauli's own fullscreen button (which
    // deliberately fullscreens .media-wrap, not the raw player - see
    // below) never had a working click handler on a YouTube order,
    // confirmed live: clicking it did nothing Kauli controlled, and
    // whatever fullscreen a client/staff member DID get into (YouTube's
    // own embedded UI, cross-origin, completely outside this app's CSS)
    // was never going to size correctly no matter what this stylesheet
    // said. Both media types belong here now - the YT adapter
    // deliberately mirrors enough of the real media-element interface
    // (currentTime/paused/duration/play/pause/muted/addEventListener)
    // for this same code to work against either one.
    if (!bar || !audio) return;

    const playBtn = $("#cc-play");
    const elapsedEl = $("#cc-elapsed");
    const durationEl = $("#cc-duration");
    const track = $("#cc-track");
    const trackFill = $("#cc-track-fill");
    const muteBtn = $("#cc-mute");
    const fullscreenBtn = $("#cc-fullscreen");

    function updatePlayIcon() { playBtn.textContent = audio.paused ? "▶" : "⏸"; }
    function updateTime() {
      elapsedEl.textContent = formatTime(audio.currentTime);
      if (audio.duration) {
        durationEl.textContent = formatTime(audio.duration);
        trackFill.style.width = ((audio.currentTime / audio.duration) * 100) + "%";
      }
    }

    playBtn.addEventListener("click", () => { audio.paused ? audio.play() : audio.pause(); });
    audio.addEventListener("play", updatePlayIcon);
    audio.addEventListener("pause", updatePlayIcon);
    audio.addEventListener("timeupdate", updateTime);
    audio.addEventListener("loadedmetadata", updateTime);

    track.addEventListener("click", (e) => {
      const rect = track.getBoundingClientRect();
      const ratio = Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width));
      if (audio.duration) audio.currentTime = ratio * audio.duration;
    });

    muteBtn.addEventListener("click", () => {
      audio.muted = !audio.muted;
      muteBtn.textContent = audio.muted ? "🔇" : "🔊";
    });

    // Fullscreens .media-wrap, not the bare <video> - that way this same
    // control bar (and the burner) stays visible and usable in fullscreen
    // instead of falling back to the browser's own native video chrome.
    // Esc exits fullscreen automatically - that's the real Fullscreen API,
    // not something this needs to implement itself.
    fullscreenBtn.addEventListener("click", () => {
      if (document.fullscreenElement) document.exitFullscreen();
      else if (bar.parentElement.requestFullscreen) bar.parentElement.requestFullscreen();
    });

    updatePlayIcon();
    updateTime();
  }

  // "Preview" tab: the actual dubbed audio (a real staff-accessible
  // download, not a copy) with the real target-step captions streaming
  // underneath it in sync - what the client actually gets, not an
  // approximation of it. Reuses the exact same segment data (start_ms/
  // end_ms/final_text) already passed into initKauliEditor.
  // Mirrors what the delivered .srt/.vtt actually shows, not the raw
  // segment text - display_end_ms (from kauli.subtitles'
  // MAX_CAPTION_DISPLAY_MS) is the caption's real on-screen WINDOW, which
  // can be shorter than the segment's own real end_ms (a long gap/sound-
  // tag segment gets capped so it doesn't linger on screen for an entire
  // musical interlude - see that module's own comment). Using the raw
  // end_ms here used to make this preview quietly lie about how long a
  // caption actually stays up, and showed the un-wrapped text instead of
  // the real, line-wrapped caption a client's file actually contains.
  function bindPreviewCaptions() {
    const previewAudio = $("#preview-audio");
    const captionEl = $("#preview-caption");
    const flagNote = $("#preview-flag-note");
    if (!previewAudio || !captionEl) return;
    previewAudio.addEventListener("timeupdate", () => {
      const t = previewAudio.currentTime * 1000;
      const seg = previewSegments.find((s) => t >= s.start_ms && t < (s.display_end_ms || s.end_ms));
      captionEl.textContent = seg && seg.wrapped_caption ? seg.wrapped_caption : " ";
      if (!flagNote) return;
      if (seg && seg.review_flag) {
        flagNote.hidden = false;
        flagNote.textContent = `⚠ This segment is still flagged: ${(seg.review_reasons || []).join(", ")}`;
      } else {
        flagNote.hidden = true;
      }
    });
  }

  // ------------------------------------------------------------- saving ----
  // No per-segment Save button any more - autosave fires a couple of
  // seconds after the last edit to a segment, and Ctrl+S forces it
  // immediately. One small status readout in the topbar (see
  // updateGlobalSaveState) replaces what used to be a save-state span per
  // segment, so the flow of correcting text isn't interrupted by having to
  // click anything.
  function updateGlobalSaveState(status, extra) {
    const el = $("#global-save-state");
    if (!el) return;
    if (status === "saving") { el.textContent = "Saving…"; el.className = "save-state dirty"; }
    else if (status === "dirty") { el.textContent = `${dirtySegments.size} unsaved change${dirtySegments.size === 1 ? "" : "s"}`; el.className = "save-state dirty"; }
    else if (status === "saved") { el.textContent = "✓ All changes saved" + (extra ? " " + extra : ""); el.className = "save-state saved"; }
    else if (status === "error") { el.textContent = "Save failed: " + extra; el.className = "save-state dirty"; }
  }

  function scheduleAutosave(segmentId) {
    const key = `${segmentId}:${currentStep}`;
    if (pendingSaveTimers[key]) clearTimeout(pendingSaveTimers[key]);
    pendingSaveTimers[key] = setTimeout(() => {
      delete pendingSaveTimers[key];
      saveSegment(segmentId, currentStep, false);
    }, AUTOSAVE_DELAY_MS);
  }

  function markDirty(segmentId) {
    dirtySegments.add(segmentId);
    updateGlobalSaveState("dirty");
    scheduleAutosave(segmentId);
  }

  async function saveSegment(segmentId, step, resynthesize) {
    const flow = step === "source" ? $("#flow-source") : $("#flow-target");
    const text = cellsText(flow, segmentId);
    updateGlobalSaveState("saving");
    try {
      let res, data;
      if (step === "source") {
        res = await fetch(`/staff/orders/${ORDER_ID}/segments/${segmentId}/source`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text }),
        });
        data = await res.json();
        if (!res.ok) throw new Error(data.error || "save failed");
        // A human has now read/corrected this segment - low-confidence
        // dimming no longer means anything useful, so clear it immediately
        // (the backend persists the same decision on next page load).
        $all(`.wordcell[data-segment-id="${segmentId}"]`, flow).forEach((c) => { c.style.opacity = ""; });
        // The English translation currently shown may now be of text that
        // no longer exists in this corrected source - reflect that on the
        // target cells immediately, not just after a reload.
        segmentMeta[segmentId] = Object.assign({}, segmentMeta[segmentId],
          { translation_stale: !!data.translation_stale });
        applyStaleTranslationUI(segmentId, !!data.translation_stale);
      } else {
        res = await fetch(`/staff/orders/${ORDER_ID}/segments/${segmentId}/save`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text, resynthesize: !!resynthesize }),
        });
        data = await res.json();
        if (!res.ok) throw new Error(data.error || "save failed");
        // Hand-editing the English is itself how you reconcile it with a
        // corrected source, same as clicking Re-translate - clear the
        // stale marker rather than leave it flagged on text someone just
        // finished dealing with.
        segmentMeta[segmentId] = Object.assign({}, segmentMeta[segmentId], { translation_stale: false });
        applyStaleTranslationUI(segmentId, false);
      }
      updatePreviewSegment(segmentId, data.wrapped_caption, data.display_end_ms);
      dirtySegments.delete(segmentId);
      updateGlobalSaveState(dirtySegments.size ? "dirty" : "saved", resynthesize ? "(+ re-synthesized)" : "");
    } catch (err) {
      updateGlobalSaveState("error", err.message);
    }
  }

  // Alt+R: re-translate the focused segment from its (possibly just-
  // corrected) source. Surgically swaps just this segment's cells out of
  // the shared target flow, in place, rather than touching anything else
  // on the page.
  async function retranslate(segmentId) {
    const targetFlow = $("#flow-target");
    const oldCells = $all(`[data-segment-id="${segmentId}"]`, targetFlow);
    if (!oldCells.length) return;
    const parent = oldCells[0].parentElement;
    const insertBefore = oldCells[oldCells.length - 1].nextSibling;
    oldCells.forEach((el) => el.classList.add("retranslating"));
    try {
      const res = await fetch(`/staff/orders/${ORDER_ID}/segments/${segmentId}/retranslate`, { method: "POST" });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "retranslate failed");

      allCells = allCells.filter((c) => c.el.dataset.segmentId !== segmentId || !targetFlow.contains(c.el));
      oldCells.forEach((el) => el.remove());

      segmentMeta[segmentId] = Object.assign({}, segmentMeta[segmentId], {
        review_flag: data.review_flag, review_reasons: data.review_reasons,
        translation_confidence: data.translation_confidence,
        translation_stale: !!data.translation_stale,
      });
      const frag = document.createDocumentFragment();
      buildCells(frag, data.target_cells, segmentId, data.final_text, "target",
                 buildFlagTitle(segmentMeta[segmentId]), staleTranslationTitle(segmentMeta[segmentId]));
      parent.insertBefore(frag, insertBefore);
      updatePreviewSegment(segmentId, data.wrapped_caption, data.display_end_ms);
    } catch (err) {
      alert("Re-translate failed: " + err.message);
      oldCells.forEach((el) => el.classList.remove("retranslating"));
    }
  }

  // Alt+T: re-run ASR on just this segment's own audio window - the
  // transcription-stage equivalent of Alt+R. Swaps BOTH the source cells
  // (fresh ASR text/timing) and, when the server auto-retranslated from it,
  // the target cells too - same "surgically replace just this segment,
  // don't touch anything else on the page" approach as retranslate().
  async function retranscribe(segmentId) {
    const sourceFlow = $("#flow-source");
    const targetFlow = $("#flow-target");
    const oldSourceCells = $all(`[data-segment-id="${segmentId}"]`, sourceFlow);
    if (!oldSourceCells.length) return;
    const sourceParent = oldSourceCells[0].parentElement;
    const sourceInsertBefore = oldSourceCells[oldSourceCells.length - 1].nextSibling;
    oldSourceCells.forEach((el) => el.classList.add("retranslating"));
    try {
      const res = await fetch(`/staff/orders/${ORDER_ID}/segments/${segmentId}/retranscribe`, { method: "POST" });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "re-transcribe failed");

      allCells = allCells.filter((c) => c.el.dataset.segmentId !== segmentId || !sourceFlow.contains(c.el));
      oldSourceCells.forEach((el) => el.remove());

      segmentMeta[segmentId] = Object.assign({}, segmentMeta[segmentId], {
        review_flag: data.review_flag, review_reasons: data.review_reasons,
        translation_confidence: data.translation_confidence,
        translation_stale: !!data.translation_stale,
      });
      const flagTitle = buildFlagTitle(segmentMeta[segmentId]);
      const sourceFrag = document.createDocumentFragment();
      buildCells(sourceFrag, data.source_cells, segmentId, data.source_final_text, "source", flagTitle);
      sourceParent.insertBefore(sourceFrag, sourceInsertBefore);

      if (data.auto_retranslated) {
        // The server re-ran MT from the fresh transcript too - the target
        // side changed, swap it the same way retranslate() does.
        const oldTargetCells = $all(`[data-segment-id="${segmentId}"]`, targetFlow);
        if (oldTargetCells.length) {
          const targetParent = oldTargetCells[0].parentElement;
          const targetInsertBefore = oldTargetCells[oldTargetCells.length - 1].nextSibling;
          allCells = allCells.filter((c) => c.el.dataset.segmentId !== segmentId || !targetFlow.contains(c.el));
          oldTargetCells.forEach((el) => el.remove());
          const targetFrag = document.createDocumentFragment();
          buildCells(targetFrag, data.target_cells, segmentId, data.final_text, "target",
                     flagTitle, staleTranslationTitle(segmentMeta[segmentId]));
          targetParent.insertBefore(targetFrag, targetInsertBefore);
        }
        updatePreviewSegment(segmentId, data.wrapped_caption, data.display_end_ms);
      } else {
        // English was already hand-finalized - untouched, just flagged
        // stale (same rule editor_save_source uses for a manual correction).
        applyStaleTranslationUI(segmentId, data.translation_stale);
      }
      if (data.retranslate_error) {
        alert("Transcript updated, but re-translating it failed: " + data.retranslate_error);
      }
    } catch (err) {
      alert("Re-transcribe failed: " + err.message);
      oldSourceCells.forEach((el) => el.classList.remove("retranslating"));
    }
  }

  // Ctrl+S: save the segment the focused cell belongs to, right now -
  // cancels any pending autosave for it so there's no duplicate request.
  function saveFocusedSegment(resynthesize) {
    const active = document.activeElement;
    if (!isEditingCell(active)) return;
    const segmentId = active.dataset.segmentId;
    if (!segmentId) return;
    const key = `${segmentId}:${currentStep}`;
    if (pendingSaveTimers[key]) { clearTimeout(pendingSaveTimers[key]); delete pendingSaveTimers[key]; }
    saveSegment(segmentId, currentStep, !!resynthesize);
  }

  function updateFlagUI(segmentId, reviewFlag, reasons) {
    segmentMeta[segmentId] = Object.assign({}, segmentMeta[segmentId], { review_flag: reviewFlag, review_reasons: reasons });
    const title = buildFlagTitle(segmentMeta[segmentId]);
    ["#flow-source", "#flow-target"].forEach((sel) => {
      const cells = $all(`[data-segment-id="${segmentId}"]`, $(sel));
      cells.forEach((cell, i) => {
        cell.classList.toggle("flagged-cell", reviewFlag);
        cell.title = i === 0 ? title : "";
      });
    });
  }

  async function toggleFlag(segmentId) {
    const res = await fetch(`/staff/orders/${ORDER_ID}/segments/${segmentId}/flag`, { method: "POST" });
    const data = await res.json();
    if (!res.ok) return;
    updateFlagUI(segmentId, data.review_flag, data.review_reasons);
  }

  // Manual speaker tag/correction for one segment - see
  // webapp/app.py's editor_set_segment_speaker. A real page reload
  // (plain form submit), not a fetch+in-place update like toggleFlag -
  // this is a rare, deliberate action (tagging who's speaking, once,
  // during an initial listen-through), not something worth a second
  // JSON-returning route just to avoid a reload for it. Once a speaker
  // is tagged, assign it a voice from the Job tab's Speakers panel.
  function setSpeakerForFocused(cell) {
    const segmentId = cell && cell.dataset.segmentId;
    if (!segmentId) return;
    const current = (segmentMeta[segmentId] && segmentMeta[segmentId].speaker_id) || "";
    const label = prompt(
      'Speaker for this segment (e.g. "Man", "Woman", "Child 1") - blank to clear:', current);
    if (label === null) return; // cancelled
    const form = document.createElement("form");
    form.method = "post";
    form.action = `/staff/orders/${ORDER_ID}/segments/${segmentId}/set-speaker`;
    const input = document.createElement("input");
    input.type = "hidden";
    input.name = "speaker_id";
    input.value = label;
    form.appendChild(input);
    document.body.appendChild(form);
    form.submit();
  }

  // --------------------------------------------------------- cell merge ----
  // Ctrl+M: the focused cell absorbs the NEXT cell in the same step - for
  // when ASR (or translation) splits one real word across two cells.
  function placeCursorAtEnd(el) {
    const range = document.createRange();
    range.selectNodeContents(el);
    range.collapse(false);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
  }

  function mergeWithNext(cell, separator) {
    if (!cell.classList.contains("wordcell") && !cell.classList.contains("gapcell")) return;
    const container = cell.parentElement;
    const siblings = $all(":scope > .wordcell, :scope > .gapcell", container);
    const idx = siblings.indexOf(cell);
    if (idx === -1 || idx === siblings.length - 1) return; // nothing after it
    const next = siblings[idx + 1];

    // Default separator is a real space - two words merged with nothing
    // between them ran together into one unreadable word ("RigathiNa"
    // instead of "Rigathi na"), a real grammar/readability regression.
    // Alt+H (hyphenate) explicitly passes "-" instead, which stays exactly
    // as tight as a real hyphenated compound should be.
    const merged = cell.textContent.trim() + (separator != null ? separator : " ") + next.textContent.trim();
    cell.textContent = merged + " ";
    cell.dataset.endMs = next.dataset.endMs;
    if (cell.classList.contains("gapcell") && merged) {
      // a gap cell that gained real text is a word now, not a sound-tag slot
      cell.className = "wordcell";
    }

    // If `next` was a paragraph start, its <br> marker would otherwise be
    // orphaned once `next` itself is removed below - real DOM litter, not
    // just a cosmetic leftover (an empty line where nothing's left to
    // start).
    if (next.dataset.paraStart === "true") {
      const prevBr = next.previousElementSibling;
      if (prevBr && prevBr.tagName === "BR") prevBr.remove();
    }
    allCells = allCells.filter((c) => c.el !== next);
    next.remove();

    const segmentId = cell.dataset.segmentId;
    markDirty(segmentId);
    cell.focus();
    placeCursorAtEnd(cell);
  }

  // ------------------------------------------------------------- macros ----
  // Ten reusable text snippets (speaker IDs, common tags, ...), bound to
  // ctrl+1..ctrl+9 and ctrl+0, inserted at the cursor in whatever cell is
  // focused. Saved per-order in this browser (not synced to the server -
  // it's an editing convenience, not job data).
  function macroStorageKey() { return `kauli_macros_${ORDER_ID}`; }
  // Just a text snippet now - no speaker/paragraph flag. A speaker tag
  // typed or inserted this way still gets merged into its own cell and
  // kept out of the spoken audio automatically (server-side pattern
  // detection, not anything macro-specific) - see insertMacroAtCursor.
  function emptyMacro() { return { text: "" }; }

  function loadMacros() {
    try {
      const raw = localStorage.getItem(macroStorageKey());
      if (raw) {
        const parsed = JSON.parse(raw);
        // Migrate older formats transparently - a plain string, or an
        // older macro object that also carried paragraphBreak/speaker
        // flags, both collapse to just the text.
        macros = Array.isArray(parsed)
          ? parsed.map((m) => ({ text: typeof m === "string" ? m : (m.text || "") }))
          : null;
      }
    } catch (e) { /* corrupt/blocked storage - just use defaults */ }
    if (!Array.isArray(macros) || macros.length !== 10) macros = new Array(10).fill(null).map(emptyMacro);
  }

  let macroSaveTimer = null;
  function persistMacros() {
    macros = $all(".macro-row").map((row) => ({
      text: row.querySelector(".macro-input").value,
    }));
    try { localStorage.setItem(macroStorageKey(), JSON.stringify(macros)); } catch (e) { /* ignore */ }
    const state = $("#macro-save-state");
    if (state) state.textContent = "saved";
  }

  // Autosaves like the transcript does - no button, just a short debounce
  // after the last change so rapid typing/clicking doesn't write on every
  // keystroke.
  function scheduleMacroSave() {
    const state = $("#macro-save-state");
    if (state) state.textContent = "saving…";
    if (macroSaveTimer) clearTimeout(macroSaveTimer);
    macroSaveTimer = setTimeout(persistMacros, AUTOSAVE_DELAY_MS);
  }

  function renderMacroTable() {
    const body = $("#macro-table-body");
    if (!body) return;
    body.innerHTML = "";
    const labels = ["ctrl+1", "ctrl+2", "ctrl+3", "ctrl+4", "ctrl+5",
                     "ctrl+6", "ctrl+7", "ctrl+8", "ctrl+9", "ctrl+0"];
    labels.forEach((label, i) => {
      const macro = macros[i] || emptyMacro();
      const row = document.createElement("div");
      row.className = "macro-row";
      row.innerHTML =
        `<kbd>Macro ${i + 1}</kbd><span class="macro-key-hint">${label}</span>` +
        `<input type="text" class="macro-input" placeholder="e.g. RIGATHI GACHAGUA: or [MUSIC PLAYING]">`;
      row.querySelector(".macro-input").value = macro.text || "";
      row.querySelector(".macro-input").addEventListener("input", scheduleMacroSave);
      body.appendChild(row);
    });
  }

  // Builds one cell exactly the way buildCells does for a real word, so a
  // speaker tag inserted mid-editing looks and behaves identically to one
  // that came from the original transcript (click-to-seek, autosave,
  // spell-check clearing, bookmark support - all of it).
  function createWordCell(text, startMs, endMs, segmentId, extraClass) {
    const cell = document.createElement("span");
    cell.contentEditable = "true";
    cell.tabIndex = 0;
    cell.className = extraClass ? `wordcell ${extraClass}` : "wordcell";
    cell.dataset.startMs = startMs;
    cell.dataset.endMs = endMs;
    cell.dataset.segmentId = segmentId;
    cell.textContent = text;
    cell.addEventListener("focus", () => seekTo(startMs, false));
    cell.addEventListener("input", () => {
      markDirty(segmentId);
      autoCapitalizeNextAfterSentenceEnd(cell);
      if (cell.classList.contains("spell-flag")) { cell.classList.remove("spell-flag"); hideSpellPopover(); }
    });
    return cell;
  }

  // Plain text insertion, always - a macro is just a reusable snippet now,
  // nothing more. A speaker tag typed or inserted this way ("RIGATHI
  // GACHAGUA: ") still ends up correctly merged into its own cell and
  // excluded from the spoken audio - that's handled automatically by the
  // server's own pattern detection (kauli.models.split_off_speaker_tag)
  // the next time this segment's cells are rebuilt from saved text, not
  // by anything macro-specific here. Use Ctrl+Enter separately if this
  // insertion should also start a new paragraph.
  function insertMacroAtCursor(cell, macro) {
    if (!macro || !macro.text) return;
    document.execCommand("insertText", false, macro.text);
  }

  // -------------------------------------------------------- paragraphs ----
  // Ctrl+Enter on a focused word: that word becomes the first word of a
  // new paragraph (highlighted, and auto-capitalised since a new paragraph
  // always starts a new sentence).
  function insertParagraphBreak(cell) {
    if (!cell.classList.contains("wordcell")) return;
    const already = cell.dataset.paraStart === "true";
    cell.dataset.paraStart = already ? "" : "true";
    cell.classList.toggle("para-start", !already);
    if (!already) {
      const m = cell.textContent.match(/^(\s*)(.)(.*)$/s);
      if (m) cell.textContent = m[1] + m[2].toUpperCase() + m[3];
      // A real <br> forces the actual line break in normal inline flow -
      // the cell itself stays a normal inline-block (see .para-start's
      // CSS comment), so every word after it keeps flowing on that same
      // new line, wrapping naturally, instead of each one getting pushed
      // onto a line of its own.
      cell.insertAdjacentElement("beforebegin", document.createElement("br"));
    } else {
      const prev = cell.previousElementSibling;
      if (prev && prev.tagName === "BR") prev.remove();
    }
    markDirty(cell.dataset.segmentId);
  }

  function removeParagraphBreak(cell) {
    if (cell.dataset.paraStart !== "true") return;
    cell.dataset.paraStart = "";
    cell.classList.remove("para-start");
    const prev = cell.previousElementSibling;
    if (prev && prev.tagName === "BR") prev.remove();
    markDirty(cell.dataset.segmentId);
  }

  // ---------------------------------------------------------- preview ----
  // Ctrl+P: read the current step as flowing prose, paragraph breaks and
  // all - this is as close as the editor gets to "what the client will
  // actually receive" without leaving the page.
  function buildPreviewHtml() {
    const flow = currentStep === "source" ? $("#flow-source") : $("#flow-target");
    const text = flow ? cellsText(flow) : "";
    const paras = text ? text.split(/\n\n+/) : [];
    return paras.map((p) => `<p>${escapeHtml(p)}</p>`).join("") ||
      `<p class="muted">Nothing to preview yet.</p>`;
  }

  function togglePreview() {
    previewing = !previewing;
    const pane = $("#transcript-pane");
    const preview = $("#preview-pane");
    if (previewing) {
      preview.innerHTML = buildPreviewHtml();
      pane.hidden = true;
      preview.hidden = false;
    } else {
      pane.hidden = false;
      preview.hidden = true;
    }
  }

  // -------------------------------------------------------- bookmarks ----
  // Personal markers ("I want to come back to this one"), separate from
  // the Flag/review-flag system which is job data shared with everyone on
  // the order. Local to this browser only, same pattern as macros above.
  function bookmarkStorageKey() { return `kauli_bookmarks_${ORDER_ID}`; }
  function cellKey(cell) { return `${cell.dataset.segmentId}:${cell.dataset.startMs}`; }

  function loadBookmarks() {
    try {
      const raw = localStorage.getItem(bookmarkStorageKey());
      if (raw) bookmarkedCells = new Set(JSON.parse(raw));
    } catch (e) { /* corrupt/blocked storage - just start empty */ }
  }

  function persistBookmarks() {
    try { localStorage.setItem(bookmarkStorageKey(), JSON.stringify(Array.from(bookmarkedCells))); }
    catch (e) { /* ignore */ }
  }

  function toggleBookmark(cell) {
    const key = cellKey(cell);
    if (bookmarkedCells.has(key)) {
      bookmarkedCells.delete(key);
      cell.classList.remove("bookmarked");
    } else {
      bookmarkedCells.add(key);
      cell.classList.add("bookmarked");
    }
    persistBookmarks();
  }

  // ------------------------------------------------- text transformation ----
  function toggleCaseFirstChar(cell) {
    const m = cell.textContent.match(/^(\s*)(\S)([\s\S]*)$/);
    if (!m) return;
    const swapped = m[2] === m[2].toUpperCase() ? m[2].toLowerCase() : m[2].toUpperCase();
    cell.textContent = m[1] + swapped + m[3];
    markDirty(cell.dataset.segmentId);
  }

  function toggleCaseAll(cell) {
    const t = cell.textContent;
    cell.textContent = /[a-z]/.test(t) ? t.toUpperCase() : t.toLowerCase();
    markDirty(cell.dataset.segmentId);
  }

  // Always uppercases (not a toggle) - Shift+. - the "start of sentence"
  // correction, distinct from Alt+U's toggle-either-way.
  function capitalizeFirstChar(cell) {
    const m = cell.textContent.match(/^(\s*)(\S)([\s\S]*)$/);
    if (!m) return;
    cell.textContent = m[1] + m[2].toUpperCase() + m[3];
    markDirty(cell.dataset.segmentId);
  }

  // Ctrl+, - forces the WHOLE cell to uppercase (always up, not a toggle -
  // distinct from Alt+Shift+U, which flips either way).
  function capitalizeWholeCell(cell) {
    cell.textContent = cell.textContent.toUpperCase();
    markDirty(cell.dataset.segmentId);
  }

  // Auto-capitalize the start of a new sentence: whenever a cell's text now
  // ends in ./!/?, the next word cell's first letter is uppercased for you.
  // Never touches a gap cell - a sound tag like "[MUSIC PLAYING]" isn't a
  // sentence and shouldn't get capitalized as if it were one. Paragraph
  // starts are handled separately (see insertParagraphBreak) and already
  // auto-capitalize themselves.
  function autoCapitalizeNextAfterSentenceEnd(cell) {
    if (!cell.classList.contains("wordcell")) return;
    if (!/[.!?]$/.test(cell.textContent.trim())) return;
    const container = cell.parentElement;
    const siblings = $all(":scope > .wordcell, :scope > .gapcell", container);
    const idx = siblings.indexOf(cell);
    if (idx === -1 || idx === siblings.length - 1) return;
    const next = siblings[idx + 1];
    if (!next.classList.contains("wordcell")) return;
    const m = next.textContent.match(/^(\s*)(\S)([\s\S]*)$/);
    if (!m || m[2] === m[2].toUpperCase()) return;
    next.textContent = m[1] + m[2].toUpperCase() + m[3];
    markDirty(next.dataset.segmentId);
  }

  // English-only heuristic. Swahili plurals are prefix-based (noun classes -
  // kitabu/vitabu, mtu/watu) and can't be derived by a suffix rule, so this
  // is deliberately a no-op-ish best-effort for the English/target step;
  // Swahili source-step pluralization still needs a manual edit.
  function pluralizeWord(word) {
    if (/[sxz]$/i.test(word) || /(ch|sh)$/i.test(word)) return word + "es";
    if (/[^aeiou]y$/i.test(word)) return word.slice(0, -1) + "ies";
    return word + "s";
  }
  function pluralizeCell(cell) {
    const m = cell.textContent.match(/^(\s*)(\S+)(\s*)$/);
    if (!m) return;
    cell.textContent = m[1] + pluralizeWord(m[2]) + m[3];
    markDirty(cell.dataset.segmentId);
  }

  // <i></i> is real SRT/VTT markup, not decoration - literal tags in the
  // plain-text cell content are exactly how italics survive into the
  // exported subtitle files (cellsText() reconstructs from plain text).
  function toggleItalics(cell) {
    const trailing = /\s$/.test(cell.textContent) ? " " : "";
    const t = cell.textContent.trim();
    const m = t.match(/^<i>([\s\S]*)<\/i>$/i);
    cell.textContent = (m ? m[1] : `<i>${t}</i>`) + trailing;
    markDirty(cell.dataset.segmentId);
  }

  // Force-applies italics - idempotent (a cell already wrapped is left
  // alone) rather than toggling, which is what italic MODE needs: landing
  // on a cell that's already italic while the mode is on should keep it
  // italic, not flip it back off the way the one-shot Alt+I toggle would.
  function applyItalicsForced(cell) {
    const trailing = /\s$/.test(cell.textContent) ? " " : "";
    const t = cell.textContent.trim();
    if (!t || /^<i>[\s\S]*<\/i>$/i.test(t)) return; // empty or already italic - nothing to do
    cell.textContent = `<i>${t}</i>` + trailing;
    markDirty(cell.dataset.segmentId);
  }

  function setItalicMode(on) {
    italicModeActive = on;
    const banner = $("#italic-mode-banner");
    if (banner) banner.hidden = !on;
  }

  // Delegated (not a per-cell listener like buildCells' own focus binding)
  // so it applies to every cell everywhere - both steps, cells rebuilt
  // after a save/retranslate, macro-inserted cells - without needing to
  // touch every place a cell gets created.
  function bindItalicMode() {
    document.addEventListener("focusin", (e) => {
      if (italicModeActive && isEditingCell(e.target)) applyItalicsForced(e.target);
    });
  }

  function prependQuoteMark(cell) {
    const m = cell.textContent.match(/^(\s*)([\s\S]*)$/);
    cell.textContent = m[1] + '"' + m[2];
    markDirty(cell.dataset.segmentId);
  }

  // Trailing double-dash: the standard transcription convention for speech
  // that's interrupted or cut off mid-word.
  function appendDashes(cell) {
    const t = cell.textContent.replace(/\s+$/, "");
    cell.textContent = (t.endsWith("--") ? t : t + "--") + " ";
    markDirty(cell.dataset.segmentId);
  }

  function toggleInaudible(cell) {
    const trailing = /\s$/.test(cell.textContent) ? " " : "";
    const t = cell.textContent.trim();
    cell.textContent = (/\[inaudible\]/i.test(t)
      ? t.replace(/\s*\[inaudible\]\s*/i, " ").trim()
      : (t ? t + " " : "") + "[inaudible]") + trailing;
    markDirty(cell.dataset.segmentId);
  }

  // ">>" prefix: the standard caption-style marker for crosstalk / a second
  // voice speaking over the current one.
  function toggleInterposingVoices(cell) {
    const m = cell.textContent.match(/^(\s*)(>>\s*)?([\s\S]*)$/);
    if (!m) return;
    cell.textContent = m[1] + (m[2] ? "" : ">> ") + m[3];
    markDirty(cell.dataset.segmentId);
  }

  // Splits the focused word cell at the text cursor into two cells, timing
  // divided proportionally by character count - the same "estimate by
  // character share" approach already used for approximate target timing.
  function splitCellAtCursor(cell) {
    if (!cell.classList.contains("wordcell")) return;
    const sel = window.getSelection();
    if (!sel.rangeCount || !cell.contains(sel.anchorNode)) return;
    const range = sel.getRangeAt(0);
    const preRange = range.cloneRange();
    preRange.selectNodeContents(cell);
    preRange.setEnd(range.startContainer, range.startOffset);
    const beforeText = preRange.toString();
    const fullText = cell.textContent;
    const afterText = fullText.slice(beforeText.length);
    if (!beforeText.trim() || !afterText.trim()) return; // nothing meaningful on one side

    // Sound/music tags like "[MUSIC PLAYING]" are one atomic unit - a
    // cursor sitting inside an unclosed "[" means splitting here would
    // break the tag in half, so refuse.
    const openBrackets = (beforeText.match(/\[/g) || []).length;
    const closeBrackets = (beforeText.match(/\]/g) || []).length;
    if (openBrackets > closeBrackets) return;

    const startMs = Number(cell.dataset.startMs);
    const endMs = Number(cell.dataset.endMs);
    const splitMs = Math.round(startMs + (endMs - startMs) * (beforeText.length / fullText.length));
    const segmentId = cell.dataset.segmentId;

    const newCell = document.createElement("span");
    newCell.contentEditable = "true";
    newCell.tabIndex = 0;
    newCell.className = cell.className;
    newCell.dataset.segmentId = segmentId;
    newCell.dataset.startMs = splitMs;
    newCell.dataset.endMs = endMs;
    newCell.textContent = afterText.trimStart();
    newCell.addEventListener("focus", () => seekTo(splitMs, false));
    newCell.addEventListener("input", () => { markDirty(segmentId); autoCapitalizeNextAfterSentenceEnd(newCell); });

    cell.textContent = beforeText.trimEnd() + " ";
    cell.dataset.endMs = String(splitMs);
    cell.insertAdjacentElement("afterend", newCell);

    const entry = allCells.find((c) => c.el === cell);
    if (entry) entry.endMs = splitMs;
    allCells.push({ el: newCell, startMs: splitMs, endMs: endMs });

    markDirty(segmentId);
  }

  // Ctrl+K: splits a multi-word cell into one cell per word - e.g. a
  // "RIGATHI GACHAGUA:" speaker tag, or any phrase that ended up merged
  // into one cell, back into individual word cells. Timing divided
  // proportionally by character count across however many words there
  // are, same estimate-by-character-share approach splitCellAtCursor
  // above already uses for a 2-way split. Refuses on a gap cell (nothing
  // to split - it's a sound tag, not transcript words) and no-ops if
  // there's only one word already.
  function splitCellIntoWords(cell) {
    if (!cell.classList.contains("wordcell")) return;
    const words = cell.textContent.trim().split(/\s+/).filter(Boolean);
    if (words.length < 2) return;

    const startMs = Number(cell.dataset.startMs);
    const endMs = Number(cell.dataset.endMs);
    const duration = Math.max(1, endMs - startMs);
    const segmentId = cell.dataset.segmentId;
    const totalChars = words.reduce((sum, w) => sum + w.length, 0) || 1;
    const baseClassName = cell.className.replace(/\b(speaker|bracket)-tag-cell\b/g, "").trim();

    const entry = allCells.find((c) => c.el === cell);
    let t = startMs;
    let lastNewCell = cell;
    words.forEach((word, i) => {
      const wordMs = Math.round(duration * (word.length / totalChars));
      const wordEnd = i === words.length - 1 ? endMs : t + wordMs;
      if (i === 0) {
        cell.textContent = word + " ";
        cell.className = baseClassName;
        cell.dataset.endMs = String(wordEnd);
        if (entry) entry.endMs = wordEnd;
      } else {
        const newCell = document.createElement("span");
        newCell.contentEditable = "true";
        newCell.tabIndex = 0;
        newCell.className = baseClassName;
        newCell.dataset.segmentId = segmentId;
        newCell.dataset.startMs = t;
        newCell.dataset.endMs = wordEnd;
        newCell.textContent = word + " ";
        newCell.addEventListener("focus", () => seekTo(t, false));
        newCell.addEventListener("input", () => { markDirty(segmentId); autoCapitalizeNextAfterSentenceEnd(newCell); });
        lastNewCell.insertAdjacentElement("afterend", newCell);
        allCells.push({ el: newCell, startMs: t, endMs: wordEnd });
        lastNewCell = newCell;
      }
      t = wordEnd;
    });
    markDirty(segmentId);
  }

  // ------------------------------------------------------- navigation ----
  // Alt+Up/Down: jump between flagged segments specifically (review triage).
  function navigateFlagged(direction) {
    const flaggedIds = Object.keys(segmentMeta).filter((id) => segmentMeta[id].review_flag);
    if (!flaggedIds.length) return;
    const active = document.activeElement;
    const currentId = active && active.dataset.segmentId;
    let idx = currentId ? flaggedIds.indexOf(currentId) : -1;
    idx = idx === -1 ? (direction > 0 ? 0 : flaggedIds.length - 1)
                      : (idx + direction + flaggedIds.length) % flaggedIds.length;
    const flow = currentStep === "source" ? $("#flow-source") : $("#flow-target");
    const firstCell = flow && flow.querySelector(`[data-segment-id="${flaggedIds[idx]}"]`);
    if (firstCell) { firstCell.focus(); firstCell.scrollIntoView({ block: "center", behavior: "smooth" }); }
  }

  // Ctrl+Up/Down: move focus to the previous/next cell overall, regardless
  // of flag state - plain sequential navigation.
  function navigateCell(direction) {
    const active = document.activeElement;
    if (!isEditingCell(active)) return;
    const pane = active.closest(".step-pane");
    if (!pane) return;
    const cells = $all(".wordcell, .gapcell, .editor-fallback", pane);
    const idx = cells.indexOf(active);
    if (idx === -1) return;
    const target = cells[Math.max(0, Math.min(cells.length - 1, idx + direction))];
    target.focus();
  }

  // ------------------------------------------------------ find/replace ----
  function toggleFindReplace() {
    const bar = $("#find-replace-bar");
    if (!bar) return;
    bar.hidden = !bar.hidden;
    if (!bar.hidden) $("#find-input").focus();
  }

  function runFindReplace() {
    const find = $("#find-input").value;
    if (!find) return;
    const replace = $("#replace-input").value;
    const flow = currentStep === "source" ? $("#flow-source") : $("#flow-target");
    let count = 0;
    $all(".wordcell, .gapcell", flow).forEach((cell) => {
      if (cell.textContent.includes(find)) {
        cell.textContent = cell.textContent.split(find).join(replace);
        count++;
        markDirty(cell.dataset.segmentId);
      }
    });
    $("#find-replace-count").textContent = `${count} cell${count === 1 ? "" : "s"} updated`;
  }

  function bindFindReplace() {
    const closeBtn = $("#find-replace-close-btn");
    const runBtn = $("#find-replace-all-btn");
    if (closeBtn) closeBtn.addEventListener("click", toggleFindReplace);
    if (runBtn) runBtn.addEventListener("click", runFindReplace);
  }

  function showShortcutsPane() {
    const btn = document.querySelector('.info-tabs button[data-pane="pane-shortcuts"]');
    if (btn) btn.click();
  }

  // ------------------------------------------------------- job return ----
  function openJobReturnModal() {
    const modal = $("#job-return-modal");
    if (!modal) return; // not rendered at all once the order can't be returned any more
    modal.hidden = false;
    const select = modal.querySelector("select[name=reason]");
    if (select) select.focus();
  }
  function closeJobReturnModal() {
    const modal = $("#job-return-modal");
    if (modal) modal.hidden = true;
  }
  function bindJobReturnModal() {
    const modal = $("#job-return-modal");
    if (!modal) return;
    $("#job-return-cancel").addEventListener("click", closeJobReturnModal);
    modal.addEventListener("click", (e) => { if (e.target === modal) closeJobReturnModal(); });
  }

  // ------------------------------------------------- voice direction ----
  // Alt+V on a focused segment: direct how ITS audio gets spoken - slower/
  // faster than the automatic fit-to-slot pace, or spelled out letter-by-
  // letter (an acronym or name a voice keeps misreading as a word). See
  // webapp/app.py's /voice-direction route - this only fires the fetch and
  // reflects the result; the actual re-render happens server-side.
  let voiceDirectionSegmentId = null;
  function openVoiceDirectionModal(segmentId) {
    const modal = $("#voice-direction-modal");
    if (!modal) return;
    voiceDirectionSegmentId = segmentId;
    const meta = segmentMeta[segmentId] || {};
    const paceSelect = modal.querySelector("select[name=pace_pct]");
    const spellCheckbox = modal.querySelector("input[name=spell_out]");
    if (paceSelect) paceSelect.value = String(meta.manual_pace_pct || 0);
    if (spellCheckbox) spellCheckbox.checked = !!meta.spell_out;
    const status = $("#voice-direction-status");
    if (status) status.textContent = "";
    modal.hidden = false;
    if (paceSelect) paceSelect.focus();
  }
  function closeVoiceDirectionModal() {
    const modal = $("#voice-direction-modal");
    if (modal) modal.hidden = true;
  }
  async function applyVoiceDirection() {
    const modal = $("#voice-direction-modal");
    if (!modal || !voiceDirectionSegmentId) return;
    const segmentId = voiceDirectionSegmentId;
    const paceSelect = modal.querySelector("select[name=pace_pct]");
    const spellCheckbox = modal.querySelector("input[name=spell_out]");
    const status = $("#voice-direction-status");
    const applyBtn = $("#voice-direction-apply");
    const body = {
      pace_pct: paceSelect ? Number(paceSelect.value) : 0,
      spell_out: spellCheckbox ? spellCheckbox.checked : false,
    };
    if (status) status.textContent = "Re-rendering…";
    if (applyBtn) applyBtn.disabled = true;
    try {
      const res = await fetch(`/staff/orders/${ORDER_ID}/segments/${segmentId}/voice-direction`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "voice direction failed");
      segmentMeta[segmentId] = Object.assign({}, segmentMeta[segmentId], {
        manual_pace_pct: data.manual_pace_pct, spell_out: data.spell_out,
        review_flag: data.review_flag, review_reasons: data.review_reasons,
      });
      if (data.dub_voice === "human") {
        if (status) status.textContent = "Saved - but this order's dub is currently the voice actor's " +
          "own recording, so no AI audio was re-rendered.";
      } else {
        closeVoiceDirectionModal();
      }
    } catch (err) {
      if (status) status.textContent = "Failed: " + err.message;
    } finally {
      if (applyBtn) applyBtn.disabled = false;
    }
  }
  function bindVoiceDirectionModal() {
    const modal = $("#voice-direction-modal");
    if (!modal) return;
    $("#voice-direction-cancel").addEventListener("click", closeVoiceDirectionModal);
    $("#voice-direction-apply").addEventListener("click", applyVoiceDirection);
    modal.addEventListener("click", (e) => { if (e.target === modal) closeVoiceDirectionModal(); });
  }

  // ---------------------------------------------------- approve split-button ----
  function bindApproveSplitButton() {
    const toggle = $("#approve-split-toggle");
    const menu = $("#approve-split-menu");
    if (!toggle || !menu) return;
    toggle.addEventListener("click", (e) => {
      e.stopPropagation();
      menu.hidden = !menu.hidden;
    });
    document.addEventListener("click", (e) => {
      if (!menu.hidden && !menu.contains(e.target) && e.target !== toggle) menu.hidden = true;
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !menu.hidden) menu.hidden = true;
    });
  }

  function bindSpeedPicker() {
    const toggle = $("#speed-picker-btn");
    const menu = $("#speed-picker-menu");
    if (!toggle || !menu) return;
    toggle.addEventListener("click", (e) => {
      e.stopPropagation();
      menu.hidden = !menu.hidden;
    });
    $all("#speed-picker-menu .stage-picker-item").forEach((btn) => {
      btn.addEventListener("click", () => {
        setRateAbsolute(Number(btn.dataset.rate));
        menu.hidden = true;
      });
    });
    document.addEventListener("click", (e) => {
      if (!menu.hidden && !menu.contains(e.target) && e.target !== toggle) menu.hidden = true;
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !menu.hidden) menu.hidden = true;
    });
  }

  // -------------------------------------------------------- spell check ----
  // Spelling only, not grammar - a real grammar checker needs a language
  // model or a paid service (LanguageTool, Grammarly, or Claude once
  // ANTHROPIC_API_KEY is set), none of which this can call for free. What's
  // here is a genuine, fully-working dictionary lookup + suggestion engine:
  // no API, no network call once the word list is loaded, runs entirely in
  // the browser. The word list is frequency-ranked from OpenSubtitles data
  // (conversational English, a good match for transcript content), ~50k
  // words - it will flag some real but uncommon words as a false positive
  // now and then; "Ignore" always wins over the dictionary.
  let dictionary = null; // Map<word, frequencyRank> (lower rank = more common), null until loaded
  let spellPopoverCell = null;

  async function loadDictionary() {
    try {
      const res = await fetch("/static/dictionary_en.txt");
      const text = await res.text();
      const words = text.split("\n").map((w) => w.trim()).filter(Boolean);
      // The file is already frequency-sorted (most common word first) - the
      // line number IS the rank, which is what lets suggestWords() prefer
      // "the" over some obscure word at the same edit distance from a typo.
      dictionary = new Map(words.map((w, i) => [w, i]));
      const btn = $("#spellcheck-btn");
      if (btn) btn.disabled = false;
    } catch (e) {
      // no dictionary, no spell check - the button just never enables.
    }
  }

  // Damerau-Levenshtein (optimal string alignment variant): counts an
  // adjacent-letter swap as one edit, not two. Plain Levenshtein treats
  // "teh" -> "the" as distance 2 (same as genuinely unrelated words),
  // which meant the obviously-intended word lost to real distance-1
  // matches for the single most common typo there is - this is the actual
  // fix, not a tiebreak.
  function levenshtein(a, b) {
    const dp = [];
    for (let i = 0; i <= a.length; i++) dp.push(new Array(b.length + 1).fill(0));
    for (let i = 0; i <= a.length; i++) dp[i][0] = i;
    for (let j = 0; j <= b.length; j++) dp[0][j] = j;
    for (let i = 1; i <= a.length; i++) {
      for (let j = 1; j <= b.length; j++) {
        const cost = a[i - 1] === b[j - 1] ? 0 : 1;
        dp[i][j] = Math.min(
          dp[i - 1][j] + 1,
          dp[i][j - 1] + 1,
          dp[i - 1][j - 1] + cost,
        );
        if (i > 1 && j > 1 && a[i - 1] === b[j - 2] && a[i - 2] === b[j - 1]) {
          dp[i][j] = Math.min(dp[i][j], dp[i - 2][j - 2] + 1);
        }
      }
    }
    return dp[a.length][b.length];
  }

  function suggestWords(word, limit) {
    if (!dictionary) return [];
    const w = word.toLowerCase();
    const candidates = [];
    dictionary.forEach((rank, dictWord) => {
      if (Math.abs(dictWord.length - w.length) > 2) return;
      if (dictWord[0] !== w[0]) return; // cheap prefilter - keeps this fast against 50k words
      const dist = levenshtein(w, dictWord);
      if (dist <= 2) candidates.push([dictWord, dist, rank]);
    });
    // Distance first, then how common the word actually is - without the
    // rank tiebreak, a typo like "teh" can lose "the" to a pile of rarer
    // words that happen to sit at the same edit distance.
    candidates.sort((a, b) => a[1] - b[1] || a[2] - b[2]);
    return candidates.slice(0, limit).map((c) => c[0]);
  }

  // The "core word" inside a cell, stripped of surrounding punctuation and
  // whitespace - what actually gets dictionary-checked. Returns "" for
  // anything that isn't real prose (a bracketed sound tag, a bare number,
  // an empty cell), which is deliberately never flagged.
  function coreWord(text) {
    const t = text.trim();
    if (!t || /^\[.*\]$/.test(t) || /^>>/.test(t)) return "";
    const m = t.match(/^\W*([A-Za-z']+)\W*$/);
    return m ? m[1] : "";
  }

  function isMisspelled(text) {
    if (!dictionary) return false;
    const w = coreWord(text);
    if (!w) return false;
    return !dictionary.has(w.toLowerCase());
  }

  function runSpellCheck() {
    if (!dictionary) return;
    const status = $("#spellcheck-status");
    const cells = $all("#flow-target .wordcell");
    let flagged = 0;
    cells.forEach((cell) => {
      const bad = isMisspelled(cell.textContent);
      cell.classList.toggle("spell-flag", bad);
      if (bad) flagged++;
    });
    if (status) status.textContent = flagged
      ? `${flagged} possible ${flagged === 1 ? "issue" : "issues"} - click a red cell for suggestions`
      : "No spelling issues found";
  }

  function hideSpellPopover() {
    const pop = $("#spell-popover");
    if (pop) pop.hidden = true;
    spellPopoverCell = null;
  }

  function applySpellSuggestion(cell, suggestion) {
    const t = cell.textContent;
    const m = t.match(/^(\s*)(\W*)([A-Za-z']+)(\W*)(\s*)$/);
    cell.textContent = m ? (m[1] + m[2] + suggestion + m[4] + m[5]) : (suggestion + " ");
    cell.classList.remove("spell-flag");
    markDirty(cell.dataset.segmentId);
    hideSpellPopover();
  }

  // "Replace all" - the same suggestion applied to every OTHER cell still
  // flagged with the identical misspelled word (case-insensitive), not
  // just the one that was clicked - for a name or term that's wrong the
  // same way in several places, so it's not a click-per-instance chore.
  function applySpellSuggestionAll(word, suggestion) {
    const target = (word || "").toLowerCase();
    if (!target) return;
    $all("#flow-target .wordcell.spell-flag").forEach((cell) => {
      if (coreWord(cell.textContent).toLowerCase() === target) {
        applySpellSuggestion(cell, suggestion);
      }
    });
  }

  function showSpellPopover(cell) {
    const pop = $("#spell-popover");
    if (!pop) return;
    spellPopoverCell = cell;
    const word = coreWord(cell.textContent);
    const suggestions = suggestWords(word, 5);
    const matchCount = $all("#flow-target .wordcell.spell-flag")
      .filter((c) => coreWord(c.textContent).toLowerCase() === word.toLowerCase()).length;
    pop.innerHTML = "";
    if (suggestions.length) {
      suggestions.forEach((s) => {
        const row = document.createElement("div");
        row.className = "spell-suggestion-row";
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "spell-suggestion";
        btn.textContent = s;
        btn.addEventListener("mousedown", (e) => { e.preventDefault(); applySpellSuggestion(cell, s); });
        row.appendChild(btn);
        if (matchCount > 1) {
          const allBtn = document.createElement("button");
          allBtn.type = "button";
          allBtn.className = "spell-suggestion-all";
          allBtn.title = `Replace all ${matchCount} occurrences of "${word}" with "${s}"`;
          allBtn.textContent = `Replace all (${matchCount})`;
          allBtn.addEventListener("mousedown", (e) => { e.preventDefault(); applySpellSuggestionAll(word, s); });
          row.appendChild(allBtn);
        }
        pop.appendChild(row);
      });
    } else {
      const none = document.createElement("div");
      none.className = "spell-none";
      none.textContent = "No suggestions - edit it directly";
      pop.appendChild(none);
    }
    const ignoreBtn = document.createElement("button");
    ignoreBtn.type = "button";
    ignoreBtn.className = "spell-ignore";
    ignoreBtn.textContent = "Ignore";
    ignoreBtn.addEventListener("mousedown", (e) => {
      e.preventDefault();
      cell.classList.remove("spell-flag");
      hideSpellPopover();
    });
    pop.appendChild(ignoreBtn);

    const rect = cell.getBoundingClientRect();
    const containerRect = pop.offsetParent ? pop.offsetParent.getBoundingClientRect() : { top: 0, left: 0 };
    pop.style.top = (rect.bottom - containerRect.top + 4) + "px";
    pop.style.left = (rect.left - containerRect.left) + "px";
    pop.hidden = false;
  }

  function bindSpellCheck() {
    const btn = $("#spellcheck-btn");
    if (btn) {
      btn.disabled = !dictionary;
      btn.addEventListener("click", runSpellCheck);
    }
    document.addEventListener("focusin", (e) => {
      if (e.target.classList && e.target.classList.contains("spell-flag")) showSpellPopover(e.target);
      else if (spellPopoverCell && e.target !== spellPopoverCell) hideSpellPopover();
    });
  }

  // --------------------------------------------------------- shortcuts ----
  function isEditingCell(el) {
    return el && (el.classList.contains("wordcell") || el.classList.contains("gapcell") ||
                  el.classList.contains("editor-fallback"));
  }

  // Broader than isEditingCell - also true for any plain form field
  // (macro value inputs, find/replace, the job-return textarea/select,
  // ...). Those aren't "editing a cell" so the cell-specific shortcuts
  // below correctly skip them, but bare Space/arrows/[/] need to type a
  // literal character into them too, not get hijacked as playback
  // shortcuts (that was the actual bug: typing a space in a macro's value
  // field paused/played the video instead of typing a space).
  function isTypingContext(el) {
    return isEditingCell(el) || (el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT"));
  }

  function bindShortcuts() {
    document.addEventListener("keydown", (e) => {
      const active = document.activeElement;
      const editing = isEditingCell(active);
      const key = e.key.toLowerCase();

      // Available regardless of whether a cell is focused. None of these
      // touch a key the browser reserves for itself (new tab, find, save
      // page, bookmark, history, downloads, address bar...) or that a text
      // field needs for native copy/paste/select-all/undo - see the
      // Shortcuts tab for why several keys from the original spec doc
      // aren't bound to what it originally asked for.
      if (e.ctrlKey && e.shiftKey && key === "s") {
        e.preventDefault();
        saveFocusedSegment(true); // save + re-synthesize audio (target step only, harmlessly ignored on source)
        return;
      }
      if (e.ctrlKey && key === "s") {
        e.preventDefault();
        saveFocusedSegment(false);
        return;
      }
      if (e.ctrlKey && !e.shiftKey && key === "p") {
        e.preventDefault();
        togglePreview();
        return;
      }
      if (e.ctrlKey && key === "/") {
        e.preventDefault();
        showShortcutsPane();
        return;
      }
      if (e.ctrlKey && e.shiftKey && key === "f") {
        e.preventDefault();
        toggleFindReplace();
        return;
      }
      if (e.ctrlKey && e.shiftKey && key === "z") {
        e.preventDefault();
        openJobReturnModal();
        return;
      }
      if (e.ctrlKey && e.shiftKey && key === "i") {
        e.preventDefault();
        setItalicMode(!italicModeActive);
        // The cell already focused when the mode is switched on counts
        // too, not just ones selected after - otherwise turning it on
        // with your cursor already sitting on the first cell you want
        // italicized would skip that exact cell.
        if (italicModeActive && editing) applyItalicsForced(active);
        return;
      }
      if (e.altKey && key === "f") {
        e.preventDefault();
        const segmentId = active && active.dataset.segmentId;
        if (segmentId) toggleFlag(segmentId);
        return;
      }
      if (e.altKey && key === "s") {
        e.preventDefault();
        setSpeakerForFocused(active);
        return;
      }
      if (e.altKey && key === "r") {
        e.preventDefault();
        const segmentId = active && active.dataset.segmentId;
        if (segmentId) retranslate(segmentId);
        return;
      }
      if (e.altKey && key === "t") {
        e.preventDefault();
        const segmentId = active && active.dataset.segmentId;
        if (segmentId) retranscribe(segmentId);
        return;
      }
      if (e.altKey && key === "v") {
        e.preventDefault();
        const segmentId = active && active.dataset.segmentId;
        if (segmentId) openVoiceDirectionModal(segmentId);
        return;
      }
      if (e.altKey && key === "arrowdown") {
        e.preventDefault();
        navigateFlagged(1);
        return;
      }
      if (e.altKey && key === "arrowup") {
        e.preventDefault();
        navigateFlagged(-1);
        return;
      }
      if (e.ctrlKey && key === "arrowdown") {
        e.preventDefault();
        navigateCell(1);
        return;
      }
      if (e.ctrlKey && key === "arrowup") {
        e.preventDefault();
        navigateCell(-1);
        return;
      }
      if (e.key === "Escape") {
        const modal = $("#job-return-modal");
        if (modal && !modal.hidden) { closeJobReturnModal(); return; }
        if (editing) {
          hideSpellPopover();
          active.blur();
          return;
        }
      }

      // Only meaningful while a cell is actually focused.
      if (editing) {
        if (e.ctrlKey && key === "enter") {
          e.preventDefault();
          insertParagraphBreak(active);
          return;
        }
        if (e.altKey && e.key === "Backspace") {
          e.preventDefault();
          removeParagraphBreak(active);
          return;
        }
        // Ctrl+Backspace does the same thing, but only when the focused
        // cell actually IS a paragraph start - otherwise it falls through
        // to the browser's own native "delete previous word", which is
        // what most people expect Ctrl+Backspace to do everywhere else.
        if (e.ctrlKey && e.key === "Backspace" && active.dataset.paraStart === "true") {
          e.preventDefault();
          removeParagraphBreak(active);
          return;
        }
        if (e.ctrlKey && key === "m") {
          e.preventDefault();
          mergeWithNext(active);
          return;
        }
        if (e.ctrlKey && /^[0-9]$/.test(e.key)) {
          e.preventDefault();
          const idx = e.key === "0" ? 9 : parseInt(e.key, 10) - 1;
          insertMacroAtCursor(active, macros[idx]);
          markDirty(active.dataset.segmentId);
          return;
        }
        if (e.altKey && key === "b") {
          e.preventDefault();
          toggleBookmark(active);
          return;
        }
        if (e.altKey && e.shiftKey && key === "u") {
          e.preventDefault();
          toggleCaseAll(active);
          return;
        }
        if (e.altKey && key === "u") {
          e.preventDefault();
          toggleCaseFirstChar(active);
          return;
        }
        if (e.ctrlKey && key === ",") {
          e.preventDefault();
          capitalizeWholeCell(active);
          return;
        }
        // e.code here too, same reason as Shift+Period below - Shift
        // changes what "," reports as e.key (to "<" on a US layout).
        if (e.shiftKey && e.code === "Comma") {
          e.preventDefault();
          toggleCaseAll(active);
          return;
        }
        if (e.altKey && key === "p") {
          e.preventDefault();
          pluralizeCell(active);
          return;
        }
        if (e.altKey && key === "i") {
          e.preventDefault();
          toggleItalics(active);
          return;
        }
        if (e.altKey && key === "q") {
          e.preventDefault();
          prependQuoteMark(active);
          return;
        }
        if (e.altKey && key === "d") {
          e.preventDefault();
          appendDashes(active);
          return;
        }
        if (e.altKey && key === "h") {
          e.preventDefault();
          mergeWithNext(active, "-");
          return;
        }
        if (e.altKey && key === "n") {
          e.preventDefault();
          toggleInaudible(active);
          return;
        }
        if (e.altKey && key === "l") {
          e.preventDefault();
          toggleInterposingVoices(active);
          return;
        }
        if (e.altKey && key === "k") {
          e.preventDefault();
          splitCellAtCursor(active);
          return;
        }
        if (e.ctrlKey && key === "k") {
          e.preventDefault();
          splitCellIntoWords(active);
          return;
        }
        // e.code (the physical key) rather than e.key here: Shift changes
        // what character "." and Space report as e.key, so checking e.key
        // would never match with Shift held.
        if (e.shiftKey && e.code === "Period") {
          e.preventDefault();
          toggleCaseFirstChar(active);
          return;
        }
        if (e.shiftKey && e.code === "Space") {
          e.preventDefault();
          playCellSnippet(active);
          return;
        }
        return; // let normal typing (and native copy/paste/select-all/undo) through for everything else
      }

      // Playback shortcuts - only when not actively typing anywhere (a
      // cell, a macro input, find/replace, ...), see isTypingContext.
      if (isTypingContext(active)) return;
      if (e.key === " ") {
        e.preventDefault();
        const a = currentAudio();
        a.paused ? a.play() : a.pause();
      } else if (e.key === "ArrowLeft") {
        e.preventDefault();
        const a = currentAudio();
        a.currentTime = Math.max(0, a.currentTime - 2);
      } else if (e.key === "ArrowRight") {
        e.preventDefault();
        const a = currentAudio();
        a.currentTime = Math.min(a.duration || 1e9, a.currentTime + 2);
      } else if (e.key === "[") {
        e.preventDefault();
        setRate(-0.25);
      } else if (e.key === "]") {
        e.preventDefault();
        setRate(0.25);
      }
    });
  }

  // ------------------------------------------------------ tabs/steps ----
  function bindTabs() {
    $all(".info-tabs button").forEach((btn) => {
      btn.addEventListener("click", () => {
        $all(".info-tabs button").forEach((b) => b.classList.remove("active"));
        $all(".info-pane").forEach((p) => (p.hidden = true));
        btn.classList.add("active");
        $("#" + btn.dataset.pane).hidden = false;
      });
    });
  }

  // Swaps the player over to the cloned dub track (or back to the
  // original) - see currentAudio() for how playback/seek/shortcuts follow
  // whichever one is actually live.
  function setVoiceMode(on) {
    const vt = document.getElementById("voice-track-media");
    voiceMode = on && !!vt;
    const original = $("#editor-media");
    const cc = $("#custom-controls");
    const burner = $("#video-end-burner");
    const banner = $("#voice-mode-banner");
    const picker = $("#voice-picker");
    if (voiceMode) {
      if (audio && typeof audio.pause === "function") audio.pause();
      if (original) original.style.display = "none";
      if (cc) cc.style.display = "none";
      if (burner) burner.hidden = true;
      if (banner) banner.hidden = false;
      if (picker) picker.hidden = false;
      vt.style.display = "block";
    } else {
      if (vt) { vt.pause(); vt.style.display = "none"; }
      if (original) original.style.display = "";
      if (cc) cc.style.display = "";
      if (banner) banner.hidden = true;
      if (picker) picker.hidden = true;
    }
  }

  // ------------------------------------------------------- voice picker ----
  // The picker itself is a plain form (full page reload on submit - a
  // whole-dub re-render is a real backend job, not a fetch-and-patch
  // interaction). All this does is poll while an XTTS clone is running in
  // the background, so the editor doesn't have to keep refreshing by hand
  // to find out when a several-minutes-long clone has finished.
  let voicePollTimer = null;
  function pollVoiceStatus() {
    if (!ORDER_ID) return;
    fetch(`/staff/orders/${ORDER_ID}/dub-voice-status`)
      .then((r) => r.json())
      .then((data) => updateVoiceStatusUI(data.job_status))
      .catch(() => {});
  }
  function updateVoiceStatusUI(jobStatus) {
    const statusEl = $("#voice-picker-status");
    const picker = $("#voice-picker");
    if (!statusEl) return;
    statusEl.className = "muted";
    if (voicePollTimer) { clearInterval(voicePollTimer); voicePollTimer = null; }
    if (jobStatus === "running") {
      statusEl.textContent = "Cloning the speaker's voice… this can take several minutes.";
      statusEl.classList.add("running");
      if (picker) picker.classList.add("pending");
      voicePollTimer = setInterval(pollVoiceStatus, 4000);
    } else if (jobStatus && jobStatus.startsWith("failed:")) {
      statusEl.textContent = "Clone failed: " + jobStatus.slice(7);
      statusEl.classList.add("failed");
      if (picker) picker.classList.remove("pending");
    } else {
      statusEl.textContent = "";
      if (picker) picker.classList.remove("pending");
    }
  }
  function bindVoicePicker() {
    const statusEl = $("#voice-picker-status");
    if (!statusEl) return;
    const initial = statusEl.dataset.initialStatus || "";
    if (initial) updateVoiceStatusUI(initial);
    const form = $("#voice-picker-form");
    if (form) {
      form.addEventListener("submit", (e) => {
        const select = $("#voice-picker-select");
        if (select && select.value === "xtts" &&
            !confirm("Clone the actual speaker's voice? This can take several minutes and " +
                     "only run on audio you have the speaker's consent to clone.")) {
          e.preventDefault();
        }
      });
    }
  }

  function bindStepSwitch() {
    const menu = $("#stage-picker-menu");
    const menuBtn = $("#stage-picker-btn");
    const label = $("#stage-picker-label");
    const doneForm = $("#voice-done-form");
    if (!menu || !menuBtn) return;

    menuBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      menu.hidden = !menu.hidden;
    });
    document.addEventListener("click", (e) => {
      if (!menu.hidden && !menu.contains(e.target) && e.target !== menuBtn) menu.hidden = true;
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !menu.hidden) menu.hidden = true;
    });

    // Only source/target/voice actually switch what's on screen - the
    // status-only items (deliverables, complete) are informational, and
    // the manual toggle forms (mark done/undo) submit and reload on their
    // own, so neither needs a click handler here.
    $all(".stage-picker-item[data-step]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const step = btn.dataset.step;
        // "voice" shows the exact same editable English cells as "target"
        // (same autosave/spellcheck/re-translate) - only the player and
        // the picker's own label change.
        currentStep = step === "voice" ? "target" : step;
        $all(".stage-picker-item[data-step]").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        if (label) label.textContent = btn.dataset.label || btn.textContent.trim();
        menu.hidden = true;

        $all(".step-source").forEach((p) => (p.hidden = currentStep !== "source"));
        $all(".step-target").forEach((p) => (p.hidden = currentStep !== "target"));
        const scBtn = $("#spellcheck-btn");
        if (scBtn) scBtn.hidden = currentStep !== "target";
        const scStatus = $("#spellcheck-status");
        if (scStatus && currentStep !== "target") scStatus.textContent = "";
        if (previewing) $("#preview-pane").innerHTML = buildPreviewHtml(); // refresh to the new step

        setVoiceMode(step === "voice");
        if (doneForm) doneForm.hidden = step !== "voice";
      });
    });
  }

  // -------------------------------------------------------------- init ----
  window.initKauliEditor = function (orderId, segments, youtubeVideoId, sourceLangName, targetLangName) {
    ORDER_ID = orderId;
    SOURCE_LANG_NAME = sourceLangName || "the source language";
    TARGET_LANG_NAME = targetLangName || "the target language";
    previewSegments = segments; // kept live from here on - see updatePreviewSegment
    loadBookmarks(); // before renderAllSegments/buildCells so bookmarked cells render marked from the start
    audio = youtubeVideoId
      ? createYouTubeAdapter("editor-media", youtubeVideoId)
      : $("#editor-media"); // native <audio>/<video> - same interface either way
    audio.addEventListener("timeupdate", onTimeUpdate);
    audio.addEventListener("ended", showEndBurner);
    audio.addEventListener("play", hideEndBurner);
    if (!youtubeVideoId) audio.addEventListener("seeked", hideEndBurner); // native only - dragging the seek bar while paused
    const burnerEl = $("#video-end-burner");
    if (burnerEl) burnerEl.addEventListener("click", () => { audio.currentTime = 0; audio.play(); });
    // Click-to-toggle on the video frame itself - native <video controls>
    // doesn't do this on its own (only the control bar responds to clicks).
    // Audio has no visual frame to click, and YouTube's own embed already
    // toggles play/pause on click, so this only applies to a native video.
    if (!youtubeVideoId && audio.tagName === "VIDEO") {
      audio.addEventListener("click", () => { audio.paused ? audio.play() : audio.pause(); });
    }
    bindCustomControls();
    bindPreviewCaptions();

    // The voice track is a fully separate, independently-controlled
    // element (native browser controls, not the custom bar) - it only
    // needs cell-highlighting wired up, same callback the original media
    // uses, so scrubbing through the dub still lights up the right cell.
    const voiceTrack = $("#voice-track-media");
    if (voiceTrack) voiceTrack.addEventListener("timeupdate", onTimeUpdate);

    renderAllSegments(segments);

    bindSpeedPicker();

    loadMacros();
    renderMacroTable();

    bindTabs();
    bindStepSwitch();
    bindFindReplace();
    bindSpellCheck();
    loadDictionary();
    bindJobReturnModal();
    bindVoiceDirectionModal();
    bindApproveSplitButton();
    bindVoicePicker();
    bindItalicMode();
    bindShortcuts();
  };
})();
