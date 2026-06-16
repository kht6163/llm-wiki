// Command palette (Ctrl/Cmd+P) and quick switcher (Ctrl/Cmd+O): one keyboard modal,
// two data sources. Commands are a static registry; the switcher hits /api/complete.
(function () {
  "use strict";
  var W = window.WIKI || { canWrite: false };
  var overlay = document.getElementById("cmd-overlay");
  var input = document.getElementById("cmd-input");
  var list = document.getElementById("cmd-list");
  if (!overlay || !input || !list) return;

  function shell() { return window.WikiShell || {}; }

  // Static command registry. `keep` items only show when the predicate passes.
  var COMMANDS = [
    { label: "새 문서 만들기", hint: "create", run: function () { location.href = "/new"; }, keep: function () { return W.canWrite; } },
    { label: "문서 목록", hint: "home list", run: function () { location.href = "/"; } },
    { label: "그래프 보기", hint: "graph", run: function () { location.href = "/graph"; } },
    { label: "활동 피드", hint: "activity feed changes audit", run: function () { location.href = "/activity"; }, keep: function () { return W.canWrite; } },
    { label: "태그 보기", hint: "tags", run: function () { location.href = "/tags"; } },
    { label: "깨진 링크", hint: "broken links", run: function () { location.href = "/broken-links"; } },
    { label: "검색 페이지", hint: "search", run: function () { location.href = "/search"; } },
    { label: "API 키 / 설정", hint: "settings keys", run: function () { location.href = "/settings"; } },
    { label: "사용자 관리", hint: "admin users", run: function () { location.href = "/admin/users"; }, keep: function () { return W.canAdmin; } },
    { label: "좌측 사이드바 토글", hint: "toggle left sidebar", run: function () { (shell().toggleLeft || noop)(); } },
    { label: "우측 패널 토글", hint: "toggle right panel", run: function () { (shell().toggleRight || noop)(); } },
    { label: "라이트/다크 테마 전환", hint: "theme dark light", run: function () { (shell().toggleTheme || noop)(); } },
    { label: "로그아웃", hint: "logout", run: function () { location.href = "/logout"; } }
  ];
  function noop() {}

  var mode = "commands"; // "commands" | "switcher"
  var items = [];        // current rendered items: {label, sub, run}
  var index = 0;
  var searchTimer = null;

  function open(m) {
    mode = m;
    overlay.hidden = false;
    input.value = "";
    input.placeholder = m === "switcher" ? "문서 이름/경로로 이동…" : "명령 입력…";
    render("");
    setTimeout(function () { input.focus(); }, 0);
  }
  function close() { overlay.hidden = true; items = []; index = 0; }

  // subsequence fuzzy: every char of q appears in order within s.
  function fuzzy(q, s) {
    q = q.toLowerCase(); s = s.toLowerCase();
    if (!q) return true;
    var i = 0;
    for (var j = 0; j < s.length && i < q.length; j++) if (s[j] === q[i]) i++;
    return i === q.length;
  }

  function render(q) {
    if (mode === "commands") {
      items = COMMANDS.filter(function (c) { return (!c.keep || c.keep()); })
        .filter(function (c) { return fuzzy(q, c.label + " " + (c.hint || "")); })
        .map(function (c) { return { label: c.label, sub: "", run: c.run }; });
      paint();
    } else {
      clearTimeout(searchTimer);
      var query = q.trim();
      if (!query) { items = []; paint(); return; }
      searchTimer = setTimeout(function () {
        fetch("/api/complete?q=" + encodeURIComponent(query), { credentials: "same-origin" })
          .then(function (r) { return r.json(); })
          .then(function (d) {
            var found = (d && d.ok ? d.items : []).map(function (it) {
              return { label: it.title, sub: it.path, run: function () { location.href = "/doc/" + enc(it.path); } };
            });
            if (W.canWrite) {
              found.push({ label: "새 문서: " + query, sub: "생성", run: function () { location.href = "/new?path=" + encodeURIComponent(query); } });
            }
            items = found; paint();
          }).catch(function () { items = []; paint(); });
      }, 130);
    }
  }

  function paint() {
    index = 0;
    list.innerHTML = "";
    if (!items.length) {
      var li = document.createElement("li");
      li.className = "cmd-empty muted"; li.textContent = "결과 없음";
      list.appendChild(li);
      return;
    }
    items.forEach(function (it, i) {
      var li = document.createElement("li");
      li.className = "cmd-item" + (i === 0 ? " active" : "");
      li.setAttribute("role", "option");
      li.innerHTML = '<span class="cmd-label"></span>' + (it.sub ? '<span class="cmd-sub muted"></span>' : "");
      li.querySelector(".cmd-label").textContent = it.label;
      if (it.sub) li.querySelector(".cmd-sub").textContent = it.sub;
      li.addEventListener("mousedown", function (e) { e.preventDefault(); run(i); });
      li.addEventListener("mousemove", function () { setIndex(i); });
      list.appendChild(li);
    });
  }
  function setIndex(i) {
    index = i;
    Array.prototype.forEach.call(list.children, function (c, j) { c.classList.toggle("active", j === i); });
  }
  function run(i) {
    var it = items[i];
    if (it) { close(); it.run(); }
  }
  function enc(p) { return p.split("/").map(encodeURIComponent).join("/"); }

  input.addEventListener("input", function () { render(input.value); });
  input.addEventListener("keydown", function (e) {
    if (e.key === "ArrowDown") { e.preventDefault(); setIndex(Math.min(index + 1, items.length - 1)); scrollActive(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setIndex(Math.max(index - 1, 0)); scrollActive(); }
    else if (e.key === "Enter") { e.preventDefault(); run(index); }
    else if (e.key === "Escape") { e.preventDefault(); close(); }
  });
  function scrollActive() { var a = list.children[index]; if (a) a.scrollIntoView({ block: "nearest" }); }

  overlay.addEventListener("mousedown", function (e) { if (e.target === overlay) close(); });

  document.addEventListener("keydown", function (e) {
    var mod = e.metaKey || e.ctrlKey;
    if (!mod) return;
    var k = e.key.toLowerCase();
    // Don't hijack while typing in the editor/inputs except for the dedicated combos.
    if (k === "o") { e.preventDefault(); overlay.hidden ? open("switcher") : close(); }
    else if (k === "p" && !e.shiftKey) { e.preventDefault(); overlay.hidden ? open("commands") : close(); }
  });

  window.WikiPalette = {
    openCommands: function () { open("commands"); },
    openSwitcher: function () { open("switcher"); }
  };
})();
