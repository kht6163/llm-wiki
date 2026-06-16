// Localize server-rendered UTC timestamps to the viewer's timezone.
// The server emits <time class="dt" datetime="2026-06-16T00:44:33Z">…UTC…</time>;
// here we rewrite the visible text to local "YYYY-MM-DD HH:MM:SS" and keep the
// original UTC value on hover. Without this script the cleaned UTC text shows.
(function () {
  "use strict";
  function pad(n) { return (n < 10 ? "0" : "") + n; }
  function fmt(d) {
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) +
      " " + pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
  }
  function localize(root) {
    (root || document).querySelectorAll("time.dt[datetime]").forEach(function (el) {
      if (el.dataset.localized) return;
      var iso = el.getAttribute("datetime");
      var d = new Date(iso);
      if (isNaN(d.getTime())) return;
      el.textContent = fmt(d);
      el.title = iso; // keep the source UTC on hover
      el.dataset.localized = "1";
    });
  }
  function run() { localize(document); }
  if (document.readyState !== "loading") run();
  else document.addEventListener("DOMContentLoaded", run);
  // Exposed so dynamically inserted timestamps can be localized too.
  window.WikiLocalizeTime = localize;
})();
