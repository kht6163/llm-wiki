// Click-to-toggle task checkboxes in the rendered view. Checkboxes render disabled
// by default; for writers this enables them and routes a click through the API
// (CAS-guarded by the current version). realtime.js refreshes the body afterward.
(function () {
  "use strict";
  var W = window.WIKI || { canWrite: false, csrf: "" };
  if (!W.canWrite) return;
  var rendered = document.getElementById("doc-rendered");
  var meta = document.getElementById("rt-meta");
  if (!rendered || !meta) return;
  var path = meta.getAttribute("data-path");

  function enc(p) { return p.split("/").map(encodeURIComponent).join("/"); }

  function enable() {
    rendered.querySelectorAll('input[type="checkbox"][data-ti]').forEach(function (box) {
      box.disabled = false;
    });
  }

  rendered.addEventListener("change", function (e) {
    var box = e.target;
    if (!box.matches || !box.matches('input[type="checkbox"][data-ti]')) return;
    var index = box.getAttribute("data-ti");
    var version = meta.getAttribute("data-version") || "";
    box.disabled = true;  // lock until the server confirms
    var body = new URLSearchParams();
    body.set("index", index);
    body.set("base_version", version);
    body.set("csrf_token", W.csrf);
    fetch("/api/doc/" + enc(path) + "/toggle-task", {
      method: "POST", headers: { "X-CSRF-Token": W.csrf }, body: body, credentials: "same-origin"
    }).then(function (r) { return r.json(); }).then(function (d) {
      if (d && d.ok) { meta.setAttribute("data-version", String(d.version)); }
      else { box.checked = !box.checked; box.disabled = false; }  // revert on failure
    }).catch(function () { box.checked = !box.checked; box.disabled = false; });
  });

  enable();
  // realtime.js swaps .rendered innerHTML on live updates; re-enable new checkboxes.
  new MutationObserver(enable).observe(rendered, { childList: true });
})();
