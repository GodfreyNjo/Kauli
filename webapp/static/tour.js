/* Kauli's own small onboarding tour - no bundler, no framework, no new
 * dependency, since this app has neither. Spotlights one real element at
 * a time and shows a tooltip next to it. Deliberately short (a handful
 * of steps, the real "first successful action" only) rather than a tour
 * of every feature - see the caller for the actual step list.
 *
 * Usage: KauliTour.start([{target: "#id", title: "...", text: "..."}, ...],
 *                         {onDone: function () { ... }})
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
      window.removeEventListener("resize", position);
      if (opts.onDone) opts.onDone(finished);
    }

    function position() {
      var step = steps[index];
      var el = step.target ? document.querySelector(step.target) : null;
      if (!el) {
        // The real element isn't on screen right now (a different wizard
        // pane, a hidden section) - skip straight to the next step rather
        // than pointing at nothing.
        advance();
        return;
      }
      var rect = el.getBoundingClientRect();
      overlay.style.setProperty("--tour-x", rect.left + "px");
      overlay.style.setProperty("--tour-y", rect.top + "px");
      overlay.style.setProperty("--tour-w", rect.width + "px");
      overlay.style.setProperty("--tour-h", rect.height + "px");
      el.scrollIntoView({ block: "center", behavior: "smooth" });

      var top = rect.bottom + 14;
      var left = Math.max(12, Math.min(rect.left, window.innerWidth - 320));
      if (top + 160 > window.innerHeight) top = Math.max(12, rect.top - 170);
      tooltip.style.top = top + "px";
      tooltip.style.left = left + "px";

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
    }

    function advance() {
      index += 1;
      if (index >= steps.length) {
        cleanup(true);
        return;
      }
      position();
    }

    window.addEventListener("resize", position);
    position();
  };

  window.KauliTour = KauliTour;
})();
