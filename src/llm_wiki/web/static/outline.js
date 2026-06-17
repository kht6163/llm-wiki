// Build the document outline (table of contents) from rendered headings and wire
// click-to-scroll. Heading ids are assigned client-side, so no renderer change is
// needed. Re-runs when realtime.js swaps the rendered body in place.
(function () {
  "use strict";
  var rendered = document.getElementById("doc-rendered");
  var outline = document.getElementById("outline");
  if (!rendered || !outline) return;
  var spy = null;

  function slug(text, used) {
    var base = text.toLowerCase().trim()
      .replace(/[^\wÀ-￿\s-]/g, "")
      .replace(/\s+/g, "-").replace(/-+/g, "-").replace(/^-|-$/g, "") || "section";
    var s = base, i = 2;
    while (used[s]) { s = base + "-" + i++; }
    used[s] = true;
    return s;
  }

  // Scroll-spy: highlight the outline entry for the heading nearest the top of the
  // view, so a long document's table of contents shows the reader's current location
  // (selection vocabulary mirrors the file tree). Class toggle only — not motion.
  function highlight(map, id) {
    Object.keys(map).forEach(function (k) {
      var on = k === id;
      map[k].classList.toggle("active", on);
      if (on) map[k].setAttribute("aria-current", "location");
      else map[k].removeAttribute("aria-current");
    });
  }

  function setupSpy(map, heads) {
    if (spy) spy.disconnect();
    if (!("IntersectionObserver" in window) || !heads.length) return;
    var visible = {};
    spy = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (e.isIntersecting) visible[e.target.id] = true; else delete visible[e.target.id];
      });
      var activeId = null;
      for (var i = 0; i < heads.length; i++) {
        if (visible[heads[i].id]) { activeId = heads[i].id; break; }  // topmost visible
      }
      if (!activeId) {  // between headings: the last one scrolled past the top band
        for (var j = heads.length - 1; j >= 0; j--) {
          if (heads[j].getBoundingClientRect().top < 120) { activeId = heads[j].id; break; }
        }
      }
      if (!activeId) activeId = heads[0].id;
      highlight(map, activeId);
    }, { rootMargin: "0px 0px -70% 0px", threshold: 0 });
    heads.forEach(function (h) { spy.observe(h); });
  }

  function build() {
    var heads = rendered.querySelectorAll("h1, h2, h3, h4, h5, h6");
    if (!heads.length) { outline.innerHTML = '<p class="muted">목차 없음</p>'; if (spy) spy.disconnect(); return; }
    var used = {};
    var map = {};
    var headList = [];
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
      map[h.id] = a;
      headList.push(h);
    });
    outline.innerHTML = "";
    outline.appendChild(ul);
    setupSpy(map, headList);
  }

  build();
  // realtime.js replaces .rendered innerHTML on live updates; rebuild after that.
  var mo = new MutationObserver(function () { build(); });
  mo.observe(rendered, { childList: true });
})();
