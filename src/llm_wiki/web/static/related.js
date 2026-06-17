// Lazy-load the "관련 문서" (related documents) widget. Computing it runs several
// KNN vector scans server-side, so it is fetched AFTER the page renders instead of
// blocking the document view's synchronous response. Fills the #rp-related placeholder
// in the right panel's links tab; renders nothing when there are no related docs.
(function () {
  "use strict";

  function enc(p) { return p.split("/").map(encodeURIComponent).join("/"); }

  function init() {
    var box = document.getElementById("rp-related");
    if (!box) return;
    var path = box.getAttribute("data-path");
    if (!path) return;

    box.textContent = "관련 문서 불러오는 중…";
    box.className = "rp-related muted is-loading";
    box.setAttribute("aria-busy", "true");

    function reset() {
      box.className = "rp-related";
      box.removeAttribute("aria-busy");
      box.textContent = "";
    }

    fetch("/api/doc/" + enc(path) + "/related", { headers: { Accept: "application/json" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        reset();
        var items = (data && data.related) || [];
        if (!items.length) return;  // mirror the old server behaviour: hide when empty
        var h = document.createElement("h3");
        h.textContent = "관련 문서";
        var ul = document.createElement("ul");
        ul.className = "rp-list related";
        items.forEach(function (it) {
          var li = document.createElement("li");
          var a = document.createElement("a");
          a.href = "/doc/" + enc(it.path);
          a.textContent = it.title || it.path;
          var sim = document.createElement("span");
          sim.className = "sim";
          sim.title = "유사도";
          sim.textContent = Math.round((it.score || 0) * 100) + "%";
          li.appendChild(a);
          li.appendChild(sim);
          ul.appendChild(li);
        });
        box.appendChild(h);
        box.appendChild(ul);
      })
      .catch(reset);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
