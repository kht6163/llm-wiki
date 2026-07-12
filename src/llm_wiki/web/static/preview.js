// Hover preview popovers for document links on the list/search pages: on hover,
// fetch a short plain-text excerpt and show it in a popover. Text is inserted via
// textContent (never innerHTML), so document content cannot inject markup.
(function () {
  "use strict";
  var cache = {};
  var pop = null;
  var showTimer = null;
  var hideTimer = null;
  var activePath = null;   // the link currently hovered; guards late fetch results

  function ensurePop() {
    if (!pop) {
      pop = document.createElement("div");
      pop.className = "doc-popover";
      pop.hidden = true;
      document.body.appendChild(pop);
    }
    return pop;
  }

  function apiUrl(path) {
    // Preserve "/" between path segments; the route is /api/doc/{path:path}/preview.
    var enc = path.split("/").map(encodeURIComponent).join("/");
    return "/api/doc/" + enc + "/preview";
  }

  function positionAt(a, p) {
    var r = a.getBoundingClientRect();
    p.style.left = (window.scrollX + r.left) + "px";
    p.style.top = (window.scrollY + r.bottom + 6) + "px";
  }

  function place(a, data) {
    var p = ensurePop();
    p.innerHTML = "";
    var t = document.createElement("div");
    t.className = "dp-title";
    t.textContent = data.title || "";
    var x = document.createElement("div");
    x.className = "dp-excerpt";
    x.textContent = data.excerpt || "(내용 없음)";
    p.appendChild(t);
    p.appendChild(x);
    positionAt(a, p);
    p.hidden = false;
  }

  // Placeholder popover shown the instant a fetch starts, so a slow request still
  // gives immediate feedback instead of a blank pause before the excerpt appears.
  function placeLoading(a) {
    var p = ensurePop();
    p.innerHTML = "";
    var x = document.createElement("div");
    x.className = "dp-excerpt is-loading";
    x.textContent = "불러오는 중…";
    p.appendChild(x);
    positionAt(a, p);
    p.hidden = false;
  }

  function show(a) {
    var path = decodeURIComponent(a.getAttribute("href").slice(5));
    activePath = path;
    if (cache[path]) { place(a, cache[path]); return; }
    placeLoading(a);
    fetch(apiUrl(path))
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d && d.ok) { cache[path] = d; if (activePath === path) place(a, d); }
      })
      .catch(function () {});
  }

  function hide() { activePath = null; if (pop) pop.hidden = true; }

  function linkUnder(target) {
    return target && target.closest ? target.closest('a.title[href^="/doc/"]') : null;
  }

  document.addEventListener("mouseover", function (e) {
    var a = linkUnder(e.target);
    if (!a) return;
    clearTimeout(hideTimer);
    clearTimeout(showTimer);
    showTimer = setTimeout(function () { show(a); }, 250);
  });
  document.addEventListener("mouseout", function (e) {
    var a = linkUnder(e.target);
    if (!a) return;
    clearTimeout(showTimer);
    hideTimer = setTimeout(hide, 200);
  });
})();
