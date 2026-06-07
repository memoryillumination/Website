// // Prefetch internal nav links during idle time so page loads feel instant
// function prefetchInternalLinks() {
//   const seen = new Set();
//   document.querySelectorAll("a[href]").forEach((link) => {
//     if (link.hostname !== window.location.hostname) return;
//     if (link.target === "_blank") return;
//     const href = link.getAttribute("href");
//     if (!href || href.startsWith("#") || href.startsWith("mailto:")) return;
//     const url = link.href;
//     if (seen.has(url)) return;
//     seen.add(url);
//     const el = document.createElement("link");
//     el.rel = "prefetch";
//     el.href = url;
//     document.head.appendChild(el);
//   });
// }
//
// if ("requestIdleCallback" in window) {
//   requestIdleCallback(prefetchInternalLinks);
// } else {
//   window.addEventListener("load", prefetchInternalLinks);
// }
