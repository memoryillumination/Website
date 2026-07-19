// Simple click-to-advance product tour with a spotlight/dimming effect.
// Steps are {selector, text} pairs. A transparent full-page overlay captures
// clicks (so the tour never accidentally triggers the element it's pointing
// at, e.g. opening the file picker or submitting the form) and advances to
// the next step; the last step reports completion back to the backend.

const TOUR_STYLE_ID = "mi-tour-style";

// Injected as a plain stylesheet (not Tailwind classes) so this widget renders
// correctly even when output.css hasn't been rebuilt against this file yet —
// Tailwind's JIT scan only generates classes it has actually seen at build time.
function injectTourStyles() {
  if (document.getElementById(TOUR_STYLE_ID)) return;
  const style = document.createElement("style");
  style.id = TOUR_STYLE_ID;
  style.textContent = `
    .mi-tour-overlay {
      position: fixed;
      inset: 0;
      z-index: 200;
      cursor: pointer;
    }
    .mi-tour-tooltip {
      position: absolute;
      max-width: 260px;
      background: #fff;
      color: #0f0f0f;
      font-size: 0.9em;
      line-height: 1.4;
      padding: 12px 16px;
      border-radius: 8px;
      box-shadow: 0 4px 16px rgba(0, 0, 0, 0.25);
    }
  `;
  document.head.appendChild(style);
}

function startTour(steps, { apiBaseUrl, onComplete } = {}) {
  let index = 0;
  let target = null;

  injectTourStyles();

  const overlay = document.createElement("div");
  overlay.className = "mi-tour-overlay";

  const tooltip = document.createElement("div");
  tooltip.className = "mi-tour-tooltip";
  overlay.appendChild(tooltip);
  document.body.appendChild(overlay);

  function clearSpotlight() {
    if (!target) return;
    target.style.boxShadow = "";
    target.style.position = "";
    target.style.zIndex = "";
    target = null;
  }

  function positionTooltip() {
    if (!target) return;
    const rect = target.getBoundingClientRect();
    const top = Math.min(rect.bottom + 12, window.innerHeight - 100);
    const left = Math.max(12, Math.min(rect.left, window.innerWidth - 272));
    tooltip.style.top = `${top}px`;
    tooltip.style.left = `${left}px`;
  }

  function showStep() {
    clearSpotlight();
    const step = steps[index];
    target = document.querySelector(step.selector);

    if (!target) {
      advance();
      return;
    }

    target.scrollIntoView({ block: "center", behavior: "smooth" });
    target.style.position = "relative";
    target.style.zIndex = "150";
    target.style.boxShadow = "0 0 0 9999px rgba(0,0,0,0.6)";

    tooltip.textContent = step.text;
    positionTooltip();
  }

  function advance() {
    index += 1;
    if (index >= steps.length) {
      finish();
      return;
    }
    showStep();
  }

  function finish() {
    clearSpotlight();
    overlay.removeEventListener("click", advance);
    window.removeEventListener("resize", positionTooltip);
    window.removeEventListener("scroll", positionTooltip, true);
    overlay.remove();

    if (apiBaseUrl) {
      fetch(`${apiBaseUrl}/tour-complete`, { method: "POST", credentials: "include" });
    }
    onComplete?.();
  }

  overlay.addEventListener("click", advance);
  window.addEventListener("resize", positionTooltip);
  window.addEventListener("scroll", positionTooltip, true);

  showStep();
}

// Three example steps for the upload page.
const UPLOAD_TOUR_STEPS = [
  { selector: "#file-input", text: "Start here — choose a photo from your device." },
  { selector: "#style-options", text: "Pick Sketch or Coloring Page for your style." },
  { selector: "button[type=\"submit\"]", text: "Hit Upload and we'll turn it into your result." },
];
