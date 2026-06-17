// App-shell behaviour: sidebar toggle/resize, tab switching, file-tree collapse
// state + auto-reveal, theme toggle, inline folder/doc creation, sidebar search,
// and a tree context menu. Server-rendered first; this is progressive enhancement.
(function () {
  "use strict";
  var shell = document.getElementById("app-shell");
  if (!shell) return;
  var W = window.WIKI || { canWrite: false, csrf: "" };
  var LS = window.localStorage;

  function get(k, d) { try { var v = LS.getItem(k); return v === null ? d : v; } catch (e) { return d; } }
  function set(k, v) { try { LS.setItem(k, v); } catch (e) {} }
  function enc(p) { return p.split("/").map(encodeURIComponent).join("/"); }

  function toast(msg) {
    var t = document.createElement("div");
    t.className = "rt-toast"; t.setAttribute("role", "status"); t.textContent = msg;
    document.body.appendChild(t);
    requestAnimationFrame(function () { t.classList.add("show"); });
    setTimeout(function () { t.classList.remove("show"); setTimeout(function () { t.remove(); }, 300); }, 3000);
  }

  function postForm(url, fields) {
    var body = new URLSearchParams();
    Object.keys(fields).forEach(function (k) { body.set(k, fields[k]); });
    body.set("csrf_token", W.csrf);
    return fetch(url, { method: "POST", headers: { "X-CSRF-Token": W.csrf }, body: body, credentials: "same-origin" })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, data: d }; }); });
  }

  // ---- sidebar collapse ------------------------------------------------
  // On narrow viewports the sidebars are fixed overlays over the content. They
  // must default closed (else they cover the page on load) and their open state
  // is NOT persisted — only one overlay shows at a time, dismissed by a backdrop
  // tap, Esc, or navigation (a full page load re-runs applyCollapsed → closed).
  // On wide viewports the saved collapse state applies exactly as before.
  var mqNarrow = window.matchMedia("(max-width: 860px)");
  function isNarrow() { return mqNarrow.matches; }
  var backdrop = null;

  function applyCollapsed() {
    if (isNarrow()) {
      shell.classList.add("no-left", "no-right");
    } else {
      shell.classList.toggle("no-left", get("wiki-left-collapsed", "0") === "1");
      shell.classList.toggle("no-right", get("wiki-right-collapsed", "0") === "1");
    }
    syncBackdrop();
  }
  function toggleLeft() {
    var n = !shell.classList.contains("no-left");
    shell.classList.toggle("no-left", n);
    if (isNarrow()) { if (!n) shell.classList.add("no-right"); }   // one overlay at a time
    else set("wiki-left-collapsed", n ? "1" : "0");
    syncBackdrop();
  }
  function toggleRight() {
    var n = !shell.classList.contains("no-right");
    shell.classList.toggle("no-right", n);
    if (isNarrow()) { if (!n) shell.classList.add("no-left"); }
    else set("wiki-right-collapsed", n ? "1" : "0");
    syncBackdrop();
  }
  function closeOverlays() { shell.classList.add("no-left", "no-right"); syncBackdrop(); }
  function syncBackdrop() {
    var open = isNarrow() &&
      (!shell.classList.contains("no-left") || !shell.classList.contains("no-right"));
    if (open && !backdrop) {
      backdrop = document.createElement("div");
      backdrop.className = "sb-backdrop";
      backdrop.addEventListener("click", closeOverlays);
      shell.appendChild(backdrop);
      requestAnimationFrame(function () { if (backdrop) backdrop.classList.add("show"); });
    } else if (!open && backdrop) {
      var b = backdrop; backdrop = null;
      b.classList.remove("show");
      setTimeout(function () { b.remove(); }, 200);
    }
  }
  // Re-apply on the wide<->narrow boundary so the desktop collapse state is
  // restored when widening, and overlays are forced closed when narrowing.
  if (mqNarrow.addEventListener) mqNarrow.addEventListener("change", applyCollapsed);
  else if (mqNarrow.addListener) mqNarrow.addListener(applyCollapsed);

  // The right panel is meaningful only on pages that fill it (the viewer). If it
  // has no real content, collapse it unless the user explicitly opened it.
  function autoHideRight() {
    var right = document.getElementById("sidebar-right");
    if (!right) return;
    var hasContent = right.querySelector(".rp-tabs, .rp-body, h3, .outline");
    if (!hasContent && get("wiki-right-collapsed", null) === null) shell.classList.add("no-right");
  }

  // ---- theme -----------------------------------------------------------
  function toggleTheme() {
    var cur = document.documentElement.getAttribute("data-theme");
    var next = cur === "dark" ? "light" : (cur === "light" ? "" : "dark");
    if (next) document.documentElement.setAttribute("data-theme", next);
    else document.documentElement.removeAttribute("data-theme");
    set("wiki-theme", next);
  }

  // ---- resizers --------------------------------------------------------
  function initResize(el) {
    var side = el.getAttribute("data-resize");
    el.style.touchAction = "none";   // pointer drags shouldn't scroll the page on touch
    // Pointer events cover mouse, touch, and pen with one path (mouse-only events left
    // touch/tablet users unable to resize at all).
    el.addEventListener("pointerdown", function (e) {
      e.preventDefault();
      try { el.setPointerCapture(e.pointerId); } catch (_) { /* unsupported: fall back */ }
      var startX = e.clientX;
      var varName = side === "left" ? "--left-w" : "--right-w";
      var start = parseInt(getComputedStyle(document.documentElement).getPropertyValue(varName), 10) || (side === "left" ? 260 : 300);
      document.body.classList.add("resizing");
      function move(ev) {
        var dx = ev.clientX - startX;
        var w = side === "left" ? start + dx : start - dx;
        w = Math.max(150, Math.min(560, w));
        document.documentElement.style.setProperty(varName, w + "px");
      }
      function up() {
        el.removeEventListener("pointermove", move);
        el.removeEventListener("pointerup", up);
        el.removeEventListener("pointercancel", up);
        document.body.classList.remove("resizing");
        var w = parseInt(getComputedStyle(document.documentElement).getPropertyValue(varName), 10);
        set(side === "left" ? "wiki-left-w" : "wiki-right-w", String(w));
      }
      el.addEventListener("pointermove", move);
      el.addEventListener("pointerup", up);
      el.addEventListener("pointercancel", up);
    });
  }

  // ---- tab switching (left .sb-tab / right .rp-tab) --------------------
  function initTabs(tabSel, panelAttr, tabAttr) {
    document.querySelectorAll(tabSel).forEach(function (tab) {
      tab.addEventListener("click", function () {
        var key = tab.getAttribute(tabAttr);
        var group = tab.parentElement;
        group.querySelectorAll(tabSel).forEach(function (t) {
          var on = t === tab;
          t.classList.toggle("active", on);
          t.setAttribute("aria-selected", on ? "true" : "false");
        });
        var scope = group.parentElement;
        scope.querySelectorAll("[" + panelAttr + "]").forEach(function (p) {
          p.hidden = p.getAttribute(panelAttr) !== key;
        });
        if (key === "search") { var i = document.getElementById("sb-search-input"); if (i) i.focus(); }
      });
    });
  }

  // ---- file tree: collapse state + auto-reveal -------------------------
  function openSet() {
    try { return new Set(JSON.parse(get("wiki-tree-open", "[]"))); } catch (e) { return new Set(); }
  }
  function saveOpen(s) { set("wiki-tree-open", JSON.stringify(Array.prototype.slice.call(s))); }

  function bindTree() {
    var tree = document.getElementById("file-tree");
    if (!tree) return;
    var open = openSet();
    tree.querySelectorAll("details.tree-folder").forEach(function (d) {
      if (open.has(d.getAttribute("data-folder"))) d.open = true;
      d.addEventListener("toggle", function () {
        var s = openSet();
        if (d.open) s.add(d.getAttribute("data-folder")); else s.delete(d.getAttribute("data-folder"));
        saveOpen(s);
      });
    });
    revealActive();
  }

  function revealActive() {
    var meta = document.getElementById("rt-meta");
    var path = meta && meta.getAttribute("data-path");
    var tree = document.getElementById("file-tree");
    if (!tree) return;
    tree.querySelectorAll(".tree-doc.active").forEach(function (a) { a.classList.remove("active"); });
    if (!path) return;
    var link = tree.querySelector('.tree-doc[data-doc="' + cssEsc(path) + '"]');
    if (!link) return;
    link.classList.add("active");
    var p = link.parentElement;
    while (p && p !== tree) {
      if (p.tagName === "DETAILS") p.open = true;
      p = p.parentElement;
    }
    link.scrollIntoView({ block: "nearest" });
  }
  function cssEsc(s) { return (window.CSS && CSS.escape) ? CSS.escape(s) : s.replace(/["\\]/g, "\\$&"); }

  function collapseAll() {
    var tree = document.getElementById("file-tree");
    if (!tree) return;
    tree.querySelectorAll("details.tree-folder").forEach(function (d) { d.open = false; });
    saveOpen(new Set());
  }

  function refreshTree() {
    return fetch("/api/tree", { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d || !d.ok) return;
        var tree = document.getElementById("file-tree");
        if (tree) { tree.innerHTML = renderNode(d.tree) || EMPTY_TREE_HTML; bindTree(); }
      }).catch(function () {});
  }

  // Empty-tree placeholder; mirrors the server-rendered fallback in base.html.
  var EMPTY_TREE_HTML = '<p class="tree-empty muted">아직 문서가 없습니다.' +
    (W.canWrite ? ' 위의 <b>＋ 문서</b>로 첫 노트를 만들어 보세요.' : '') + '</p>';

  // Client mirror of templates/_tree.html so a refresh keeps markup/handlers valid.
  function renderNode(node) {
    var h = "";
    (node.folders || []).forEach(function (f) {
      h += '<details class="tree-folder" data-folder="' + esc(f.path) + '">' +
        '<summary class="tree-row tree-folder-row" data-folder="' + esc(f.path) + '">' +
        '<span class="tree-twisty" aria-hidden="true"></span>' +
        '<span class="tree-label">' + esc(f.name) + '</span></summary>' +
        '<div class="tree-children">' + renderNode(f) + '</div></details>';
    });
    (node.docs || []).forEach(function (dd) {
      h += '<a class="tree-row tree-doc" href="/doc/' + enc(dd.path) + '" data-doc="' + esc(dd.path) + '">' +
        '<span class="tree-twisty tree-twisty-leaf" aria-hidden="true"></span>' +
        '<span class="tree-label">' + esc(dd.title) + '</span></a>';
    });
    return h;
  }
  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  // ---- inline create (new folder / new doc) ---------------------------
  function inlineInput(placeholder, initial, onConfirm) {
    var tree = document.getElementById("file-tree");
    if (!tree) return;
    var row = document.createElement("div");
    row.className = "tree-row tree-input-row";
    var inp = document.createElement("input");
    inp.type = "text"; inp.className = "tree-inline-input"; inp.placeholder = placeholder;
    inp.value = initial || "";
    row.appendChild(inp);
    tree.insertBefore(row, tree.firstChild);
    inp.focus(); inp.select();
    var closed = false;
    function done(commit) {
      if (closed) return;       // Enter/Escape then blur must not fire twice
      closed = true;
      var val = inp.value.trim();
      row.remove();
      if (commit && val) onConfirm(val);
    }
    inp.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); done(true); }
      else if (e.key === "Escape") { e.preventDefault(); done(false); }
    });
    inp.addEventListener("blur", function () { done(true); });
  }

  function newFolder(parent) {
    inlineInput("폴더 이름", "", function (name) {
      var path = (parent ? parent + "/" : "") + name;
      postForm("/api/folders", { path: path }).then(function (res) {
        if (res.ok && res.data.ok) { refreshTree(); toast("폴더 생성: " + res.data.path); }
        else toast("폴더 생성 실패: " + msg(res.data));
      });
    });
  }
  function newDoc(parent) {
    inlineInput("문서 경로 (예: note.md)", parent ? parent + "/" : "", function (name) {
      location.href = "/new?path=" + encodeURIComponent(name);
    });
  }
  function msg(d) { return (d && d.error && (d.error.message || d.error)) || (d && d.message) || "오류"; }

  // ---- context menu ----------------------------------------------------
  var menuEl = null;
  var menuReturnFocus = null;   // element to refocus when a keyboard-opened menu closes
  function closeMenu() {
    if (menuEl) { menuEl.remove(); menuEl = null; }
    if (menuReturnFocus) {
      try { menuReturnFocus.focus(); } catch (_) { /* gone from DOM */ }
      menuReturnFocus = null;
    }
  }
  function openMenu(x, y, items, focusFirst) {
    closeMenu();
    menuEl = document.createElement("div");
    menuEl.className = "ctx-menu";
    menuEl.setAttribute("role", "menu");
    items.forEach(function (it) {
      if (it.sep) { var s = document.createElement("div"); s.className = "ctx-sep"; menuEl.appendChild(s); return; }
      var b = document.createElement("button");
      b.type = "button"; b.className = "ctx-item" + (it.danger ? " danger" : ""); b.textContent = it.label;
      b.setAttribute("role", "menuitem");
      b.addEventListener("click", function () { menuReturnFocus = null; closeMenu(); it.run(); });
      menuEl.appendChild(b);
    });
    // Arrow/Escape navigation so the menu is operable from the keyboard.
    menuEl.addEventListener("keydown", function (e) {
      var btns = Array.prototype.slice.call(menuEl.querySelectorAll("button"));
      var i = btns.indexOf(document.activeElement);
      if (e.key === "Escape") { e.preventDefault(); closeMenu(); }
      else if (e.key === "ArrowDown") { e.preventDefault(); (btns[i + 1] || btns[0]).focus(); }
      else if (e.key === "ArrowUp") { e.preventDefault(); (btns[i - 1] || btns[btns.length - 1]).focus(); }
    });
    document.body.appendChild(menuEl);
    var w = menuEl.offsetWidth, h = menuEl.offsetHeight;
    menuEl.style.left = Math.min(x, window.innerWidth - w - 8) + "px";
    menuEl.style.top = Math.min(y, window.innerHeight - h - 8) + "px";
    if (focusFirst) { var f = menuEl.querySelector("button"); if (f) f.focus(); }
  }
  document.addEventListener("click", closeMenu);
  document.addEventListener("scroll", closeMenu, true);

  function renameDoc(path) {
    inlineRename(path, function (newPath) {
      postForm("/api/doc/" + enc(path) + "/move", { new_path: newPath }).then(function (res) {
        if (res.ok && res.data.ok) { toast("이동: " + res.data.path); refreshTree(); }
        else toast("이동 실패: " + msg(res.data));
      });
    });
  }
  function inlineRename(current, onConfirm) {
    var v = window.prompt("새 경로 (폴더 이동 포함)", current);
    if (v && v.trim() && v.trim() !== current) onConfirm(v.trim());
  }
  function deleteDoc(path) {
    if (!window.confirm("문서를 삭제할까요?\n" + path)) return;
    postForm("/doc/" + enc(path) + "/delete", {}).then(function () { refreshTree(); toast("삭제: " + path); });
  }
  function deleteFolder(path) {
    if (!window.confirm("빈 폴더를 삭제할까요?\n" + path)) return;
    postForm("/api/folders/" + enc(path) + "/delete", {}).then(function (res) {
      if (res.ok && res.data.ok) { refreshTree(); toast("폴더 삭제: " + path); }
      else toast("삭제 실패: " + msg(res.data));
    });
  }

  // Build + open the menu for whichever tree row `target` is inside. Returns the row
  // (so callers know a menu opened) or null. `focusFirst` focuses the first item, used
  // for keyboard invocation.
  function openTreeMenuFor(target, x, y, focusFirst) {
    var docRow = target.closest && target.closest(".tree-doc");
    var folderRow = target.closest && target.closest(".tree-folder-row");
    if (docRow) {
      var p = docRow.getAttribute("data-doc");
      openMenu(x, y, [
        { label: "이름 변경 / 이동", run: function () { renameDoc(p); } },
        { sep: true },
        { label: "삭제", danger: true, run: function () { deleteDoc(p); } }
      ], focusFirst);
      return docRow;
    }
    if (folderRow) {
      var f = folderRow.getAttribute("data-folder");
      openMenu(x, y, [
        { label: "새 문서", run: function () { newDoc(f); } },
        { label: "새 하위 폴더", run: function () { newFolder(f); } },
        { sep: true },
        { label: "빈 폴더 삭제", danger: true, run: function () { deleteFolder(f); } }
      ], focusFirst);
      return folderRow;
    }
    return null;
  }

  function bindContextMenu() {
    var tree = document.getElementById("file-tree");
    if (!tree || !W.canWrite) return;
    tree.addEventListener("contextmenu", function (e) {
      if (openTreeMenuFor(e.target, e.clientX, e.clientY, false)) e.preventDefault();
    });
    // Keyboard parity (WCAG 2.1.1): the ContextMenu key or Shift+F10 opens the menu at
    // the focused row, so rename/delete don't require a mouse. Focus returns to the row
    // on close.
    tree.addEventListener("keydown", function (e) {
      if (e.key !== "ContextMenu" && !(e.shiftKey && e.key === "F10")) return;
      var row = e.target.closest && (e.target.closest(".tree-doc") || e.target.closest(".tree-folder-row"));
      if (!row) return;
      var r = row.getBoundingClientRect();
      e.preventDefault();
      menuReturnFocus = row;
      openTreeMenuFor(e.target, r.left + 8, r.bottom, true);
    });
  }

  // ---- sidebar search --------------------------------------------------
  function bindSidebarSearch() {
    var inp = document.getElementById("sb-search-input");
    var out = document.getElementById("sb-search-results");
    if (!inp || !out) return;
    var t = null;
    inp.addEventListener("input", function () {
      clearTimeout(t);
      var q = inp.value.trim();
      if (!q) { out.innerHTML = ""; out.removeAttribute("aria-busy"); return; }
      out.innerHTML = '<p class="muted is-loading">검색 중…</p>';   // immediate feedback
      out.setAttribute("aria-busy", "true");
      t = setTimeout(function () {
        fetch("/api/complete?q=" + encodeURIComponent(q), { credentials: "same-origin" })
          .then(function (r) { return r.json(); })
          .then(function (d) {
            if (inp.value.trim() !== q) return;          // a newer keystroke owns the panel
            out.removeAttribute("aria-busy");
            if (!d || !d.ok) { out.innerHTML = '<p class="muted">결과 없음</p>'; return; }
            out.innerHTML = d.items.map(function (it) {
              return '<a class="sb-result" href="/doc/' + enc(it.path) + '">' +
                '<span class="sr-title">' + esc(it.title) + '</span>' +
                '<span class="sr-path muted">' + esc(it.path) + '</span></a>';
            }).join("") || '<p class="muted">결과 없음</p>';
          }).catch(function () {
            if (inp.value.trim() === q) { out.removeAttribute("aria-busy"); out.innerHTML = '<p class="muted">검색 실패</p>'; }
          });
      }, 150);
    });
  }

  // ---- action dispatch -------------------------------------------------
  document.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-action]");
    if (!btn) return;
    var a = btn.getAttribute("data-action");
    if (a === "toggle-left") { toggleLeft(); }
    else if (a === "toggle-right") { toggleRight(); }
    else if (a === "toggle-theme") { toggleTheme(); }
    else if (a === "collapse-all") { collapseAll(); }
    else if (a === "new-folder") { newFolder(""); }
    else if (a === "new-doc") { newDoc(""); }
    else if (a === "palette" && window.WikiPalette) { window.WikiPalette.openCommands(); }
    else if (a === "switcher" && window.WikiPalette) { window.WikiPalette.openSwitcher(); }
  });

  // global keyboard: Ctrl/Cmd+\ toggles the left sidebar.
  document.addEventListener("keydown", function (e) {
    if ((e.metaKey || e.ctrlKey) && e.key === "\\") { e.preventDefault(); toggleLeft(); }
    else if (e.key === "Escape" && backdrop) { closeOverlays(); }
  });

  // expose for palette.js
  window.WikiShell = {
    toggleLeft: toggleLeft, toggleRight: toggleRight, toggleTheme: toggleTheme,
    refreshTree: refreshTree, openSearchTab: function () {
      var tab = document.querySelector('.sb-tab[data-tab="search"]'); if (tab) tab.click();
      if (shell.classList.contains("no-left")) toggleLeft();
    }
  };

  applyCollapsed();
  autoHideRight();
  initTabs(".sb-tab", "data-panel", "data-tab");
  initTabs(".rp-tab", "data-rp-panel", "data-rp");
  document.querySelectorAll(".sb-resizer").forEach(initResize);
  bindTree();
  bindContextMenu();
  bindSidebarSearch();
})();
