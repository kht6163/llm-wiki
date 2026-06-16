// Build the document outline (table of contents) from rendered headings and wire
// click-to-scroll. Heading ids are assigned client-side, so no renderer change is
// needed. Re-runs when realtime.js swaps the rendered body in place.
(function () {
  "use strict";
  var rendered = document.getElementById("doc-rendered");
  var outline = document.getElementById("outline");
  if (!rendered || !outline) return;

  function slug(text, used) {
    var base = text.toLowerCase().trim()
      .replace(/[^\wÀ-￿\s-]/g, "")
      .replace(/\s+/g, "-").replace(/-+/g, "-").replace(/^-|-$/g, "") || "section";
    var s = base, i = 2;
    while (used[s]) { s = base + "-" + i++; }
    used[s] = true;
    return s;
  }

  function build() {
    var heads = rendered.querySelectorAll("h1, h2, h3, h4, h5, h6");
    if (!heads.length) { outline.innerHTML = '<p class="muted">목차 없음</p>'; return; }
    var used = {};
    var ul = document.createElement("ul");
    ul.className = "outline-list";
    Array.prototype.forEach.call(heads, function (h) {
      if (!h.id) h.id = slug(h.textContent, used); else used[h.id] = true;
      var li = document.createElement("li");
      li.className = "ol-h" + h.tagName.charAt(1);
      var a = document.createElement("a");
      a.href = "#" + h.id;
      a.textContent = h.textContent;
      a.addEventListener("click", function (e) {
        e.preventDefault();
        h.scrollIntoView({ behavior: "smooth", block: "start" });
        history.replaceState(null, "", "#" + h.id);
      });
      li.appendChild(a);
      ul.appendChild(li);
    });
    outline.innerHTML = "";
    outline.appendChild(ul);
  }

  build();
  // realtime.js replaces .rendered innerHTML on live updates; rebuild after that.
  var mo = new MutationObserver(function () { build(); });
  mo.observe(rendered, { childList: true });
})();
