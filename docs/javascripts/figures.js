// The interactive figures are same-origin iframes. This script sizes each one to its content,
// so it never scrolls inside the page, and keeps its theme in step with the docs' palette
// toggle (MkDocs Material sets data-md-color-scheme on <body>: "slate" or "default").
(() => {
  const scheme = () => (document.body.getAttribute("data-md-color-scheme") === "slate" ? "dark" : "light");
  const frames = () => document.querySelectorAll("iframe.argus-figure");

  const sync = (frame) => {
    try {
      const doc = frame.contentDocument;
      if (!doc || !doc.body) return;
      // The hairline bench reads data-theme on <html>; the anatomy page reads it on <body>.
      doc.documentElement.dataset.theme = scheme();
      if (doc.body.classList.contains("iso")) doc.body.dataset.theme = scheme();
      frame.style.height = "0px";
      frame.style.height = `${doc.documentElement.scrollHeight + 2}px`;
    } catch {
      /* not loaded yet: keep the CSS height and the figure's own theme */
    }
  };

  frames().forEach((frame) => {
    frame.addEventListener("load", () => sync(frame));
    sync(frame);
  });
  new MutationObserver(() => frames().forEach(sync)).observe(document.body, {
    attributes: true,
    attributeFilter: ["data-md-color-scheme"],
  });
  let pending = 0;
  addEventListener("resize", () => {
    cancelAnimationFrame(pending);
    pending = requestAnimationFrame(() => frames().forEach(sync));
  });
})();
