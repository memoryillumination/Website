// Prefetch internal nav links during idle time so page loads feel instant
function prefetchInternalLinks() {
  const seen = new Set();
  document.querySelectorAll("a[href]").forEach((link) => {
    if (link.hostname !== window.location.hostname) return;
    if (link.target === "_blank") return;
    const href = link.getAttribute("href");
    if (!href || href.startsWith("#") || href.startsWith("mailto:")) return;
    const url = link.href;
    if (seen.has(url)) return;
    seen.add(url);
    const el = document.createElement("link");
    el.rel = "prefetch";
    el.href = url;
    document.head.appendChild(el);
  });
}

if ("requestIdleCallback" in window) {
  requestIdleCallback(prefetchInternalLinks);
} else {
  window.addEventListener("load", prefetchInternalLinks);
}

// Fade fallback for browsers without native MPA View Transitions (non-Chrome 126+)
if (!CSS.supports("view-transition-name: none")) {
  document.body.style.transition = "opacity 200ms ease";

  // Fade in on arrival
  document.body.style.opacity = "0";
  window.addEventListener("pageshow", () => {
    document.body.style.opacity = "1";
  });

  document.querySelectorAll("a[href]").forEach((link) => {
    if (link.hostname !== window.location.hostname) return;
    if (link.target === "_blank") return;
    if (link.getAttribute("href")?.startsWith("#")) return;
    if (link.getAttribute("href")?.startsWith("mailto:")) return;

    link.addEventListener("click", (e) => {
      e.preventDefault();
      const href = link.href;
      document.body.style.opacity = "0";
      setTimeout(() => {
        window.location.href = href;
      }, 200);
    });
  });
}
