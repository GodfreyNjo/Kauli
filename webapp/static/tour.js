/* Kauli's own small onboarding tour - no bundler, no framework, no new
 * dependency, since this app has neither. Spotlights one real element at
 * a time and shows a tooltip next to it. Deliberately short (a handful
 * of steps, the real "first successful action" only) rather than a tour
 * of every feature - see the caller for the actual step list.
 *
 * Usage: KauliTour.start([{target: "#id", title: "...", text: "..."}, ...],
 *                         {onDone: function () { ... }})
 *
 * The page is scrolled to the highlighted element FIRST (instantly), and the spotlight and the tooltip are placed from where the element
 * really is afterwards, so the tooltip, with its Next and Skip buttons, is always fully on screen - also on a phone and after a scroll.
 */
(function () {
  function KauliTour() {}

  KauliTour.start = function (steps, opts) {
    opts = opts || {};
    var index = 0;
    var overlay = document.createElement("div");
    overlay.className = "tour-overlay";
    var tooltip = document.createElement("div");
    tooltip.className = "tour-tooltip";
    overlay.appendChild(tooltip);
    document.body.appendChild(overlay);

    function cleanup(finished) {
      overlay.remove();
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true);
      if (opts.onDone) opts.onDone(finished);
    }

    function currentEl() {
      var step = steps[index];
      return step && step.target ? document.querySelector(step.target) : null;
    }

    /* Put the spotlight on the element and the tooltip beside it, wherever the element is right now, keeping the tooltip inside the screen. */
    function place() {
      var el = currentEl();
      if (!el) return;
      var rect = el.getBoundingClientRect();
      overlay.style.setProperty("--tour-x", rect.left + "px");
      overlay.style.setProperty("--tour-y", rect.top + "px");
      overlay.style.setProperty("--tour-w", rect.width + "px");
      overlay.style.setProperty("--tour-h", rect.height + "px");

      var margin = 12, tw = tooltip.offsetWidth || 300, th = tooltip.offsetHeight || 160;
      var vw = document.documentElement.clientWidth || window.innerWidth, vh = window.innerHeight;
      var left = Math.max(margin, Math.min(rect.left, vw - tw - margin));
      var top = rect.bottom + 14;                                              // below the element ...
      if (top + th > vh - margin) top = rect.top - th - 14;                    // ... or above it
      if (top < margin) top = Math.max(margin, vh - th - margin);              // a tall element: pin to the bottom of the screen, still fully visible
      tooltip.style.left = left + "px";
      tooltip.style.top = top + "px";
    }

    function show() {
      var step = steps[index];
      var el = currentEl();
      if (!el) {
        // The real element isn't on screen right now (a different wizard
        // pane, a hidden section) - skip straight to the next step rather
        // than pointing at nothing.
        advance();
        return;
      }
      tooltip.innerHTML =
        '<div class="tour-tooltip-title"></div>' +
        '<div class="tour-tooltip-text"></div>' +
        '<div class="tour-tooltip-footer">' +
        '<span class="tour-tooltip-progress"></span>' +
        '<span class="tour-tooltip-actions">' +
        '<button type="button" class="tour-skip">Skip</button>' +
        '<button type="button" class="tour-next"></button>' +
        "</span></div>";
      tooltip.querySelector(".tour-tooltip-title").textContent = step.title;
      tooltip.querySelector(".tour-tooltip-text").textContent = step.text;
      tooltip.querySelector(".tour-tooltip-progress").textContent =
        (index + 1) + " of " + steps.length;
      tooltip.querySelector(".tour-next").textContent =
        index === steps.length - 1 ? "Done" : "Next →";
      tooltip.querySelector(".tour-skip").addEventListener("click", function () {
        cleanup(false);
      });
      tooltip.querySelector(".tour-next").addEventListener("click", advance);
      el.scrollIntoView({ block: "center", behavior: "auto" });               // first bring it into view, then measure
      place();
      var next = tooltip.querySelector(".tour-next");
      if (next && next.focus) next.focus({ preventScroll: true });
    }

    function advance() {
      index += 1;
      if (index >= steps.length) {
        cleanup(true);
        return;
      }
      show();
    }

    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    show();
  };

  window.KauliTour = KauliTour;
})();
