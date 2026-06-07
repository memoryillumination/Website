// Fade fallback for browsers without native MPA View Transitions (non-Chrome 126+)
if (!CSS.supports("selector(@view-transition)")) {
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
