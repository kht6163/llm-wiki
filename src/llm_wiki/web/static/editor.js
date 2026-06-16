// Markdown editor built on EasyMDE (CodeMirror 5, vendored, offline). The preview
// is rendered server-side via /api/preview so it matches the saved view exactly
// (wikilinks, callouts, task lists, sanitization). On top of EasyMDE we re-add
// [[wikilink]] autocomplete and "/" slash commands using CodeMirror's API, and wire
// image upload to /api/upload.
(function () {
  "use strict";
  var form = document.querySelector(".editform");
  var textarea = document.getElementById("editor");
  if (!form || !textarea || !window.EasyMDE) return;
  var W = window.WIKI || {};
  var csrf = (form.querySelector('input[name="csrf_token"]') || {}).value || W.csrf || "";
  var docPath = form.getAttribute("data-path") || "preview.md";

  function toast(msg) {
    var t = document.createElement("div");
    t.className = "rt-toast"; t.setAttribute("role", "status"); t.textContent = msg;
    document.body.appendChild(t);
    requestAnimationFrame(function () { t.classList.add("show"); });
    setTimeout(function () { t.classList.remove("show"); setTimeout(function () { t.remove(); }, 300); }, 3000);
  }
  function emsg(d) { return (d && d.error && (d.error.message || d.error)) || (d && d.message) || "오류"; }

  // ---- server-rendered preview (parity with the viewer) ----
  function serverPreview(plainText, preview) {
    var body = new URLSearchParams();
    body.set("content", plainText); body.set("path", docPath); body.set("csrf_token", csrf);
    fetch("/api/preview", { method: "POST", headers: { "X-CSRF-Token": csrf }, body: body, credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (d) { preview.innerHTML = (d && d.ok) ? d.html : '<p class="muted">미리보기를 불러오지 못했습니다.</p>'; })
      .catch(function () { preview.innerHTML = '<p class="muted">미리보기 오류</p>'; });
    return '<p class="muted">미리보기 로딩…</p>';
  }

  // ---- image upload -> /api/upload ----
  function uploadImage(file, onSuccess, onError) {
    var fd = new FormData(); fd.append("file", file);
    fetch("/api/upload", { method: "POST", headers: { "X-CSRF-Token": csrf }, body: fd, credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (d) { if (d && d.ok) onSuccess(d.url); else { onError(emsg(d)); toast("업로드 실패: " + emsg(d)); } })
      .catch(function () { onError("업로드 실패"); toast("업로드 실패"); });
  }

  function insertWikilink(ed) {
    var cm = ed.codemirror, sel = cm.getSelection();
    cm.replaceSelection("[[" + sel + "]]");
    if (!sel) { var c = cm.getCursor(); cm.setCursor({ line: c.line, ch: c.ch - 2 }); }
    cm.focus();
  }

  var easymde = new EasyMDE({
    element: textarea,
    autoDownloadFontAwesome: false,   // offline: toolbar glyphs come from our CSS
    spellChecker: false,
    autosave: { enabled: false },     // saving goes through the form (CAS/base_version)
    status: ["lines", "words", "cursor"],
    minHeight: "62vh",
    placeholder: "마크다운으로 작성…   ( /  슬래시 명령 ·  [[  위키링크 )",
    previewClass: ["editor-preview", "rendered"],
    previewRender: serverPreview,
    uploadImage: true,
    imageUploadFunction: uploadImage,
    toolbar: [
      "bold", "italic", "heading", "|",
      "quote", "unordered-list", "ordered-list", "code", "table", "|",
      "link", "image",
      { name: "wikilink", action: insertWikilink, className: "mde-ic mde-wikilink", title: "위키링크 [[ ]]" }, "|",
      "preview", "side-by-side", "fullscreen",
    ],
  });
  var cm = easymde.codemirror;
  var CM = cm.constructor;

  // Keep the underlying <textarea> in sync for form submit, and bind Ctrl/Cmd+S.
  form.addEventListener("submit", function () { cm.save(); });
  function save() { cm.save(); if (form.requestSubmit) form.requestSubmit(); else form.submit(); }
  cm.addKeyMap({ "Cmd-S": save, "Ctrl-S": save });

  // ---- live word/char count into the shell status bar (mirrors util.word_count) ----
  var cjkRe = /[぀-ヿ㐀-䶿一-鿿가-힣豈-﫿]/g;
  var elW = document.getElementById("sb-words"), elC = document.getElementById("sb-chars");
  function count() {
    if (!elW && !elC) return;
    var v = easymde.value() || "";
    var cjk = (v.match(cjkRe) || []).length;
    var latin = v.replace(cjkRe, " ").split(/\s+/).filter(Boolean).length;
    if (elW) elW.textContent = (cjk + latin) + " 단어";
    if (elC) elC.textContent = v.length + " 자";
  }
  cm.on("change", count); count();

  // =====================================================================
  // Completion menu: [[wikilink]] autocomplete + "/" slash commands.
  // =====================================================================
  var CARET = String.fromCharCode(0);
  function two(n) { return n < 10 ? "0" + n : "" + n; }
  function today() { var d = new Date(); return d.getFullYear() + "-" + two(d.getMonth() + 1) + "-" + two(d.getDate()); }
  function clock() { var d = new Date(); return two(d.getHours()) + ":" + two(d.getMinutes()); }

  var SLASH = [
    { label: "제목 1", hint: "h1 heading", text: "# " + CARET },
    { label: "제목 2", hint: "h2 heading", text: "## " + CARET },
    { label: "제목 3", hint: "h3 heading", text: "### " + CARET },
    { label: "글머리 목록", hint: "bullet list ul", text: "- " + CARET },
    { label: "번호 목록", hint: "ordered list ol", text: "1. " + CARET },
    { label: "할 일 (체크박스)", hint: "task todo checkbox", text: "- [ ] " + CARET },
    { label: "인용", hint: "quote blockquote", text: "> " + CARET },
    { label: "콜아웃: 정보", hint: "callout info note", text: "> [!info]\n> " + CARET },
    { label: "콜아웃: 팁", hint: "callout tip", text: "> [!tip]\n> " + CARET },
    { label: "콜아웃: 경고", hint: "callout warning warn", text: "> [!warning]\n> " + CARET },
    { label: "콜아웃: 위험", hint: "callout danger error", text: "> [!danger]\n> " + CARET },
    { label: "코드 블록", hint: "code fence block", text: "```\n" + CARET + "\n```" },
    { label: "표", hint: "table grid", text: "| 열1 | 열2 |\n| --- | --- |\n| " + CARET + " |  |" },
    { label: "구분선", hint: "hr divider rule", text: "\n---\n" + CARET },
    { label: "링크", hint: "link url", text: "[" + CARET + "](url)" },
    { label: "위키링크", hint: "wikilink internal", text: "[[" + CARET + "]]" },
    { label: "이미지", hint: "image img", text: "![" + CARET + "](url)" },
    { label: "오늘 날짜", hint: "date today", text: function () { return today() + CARET; } },
    { label: "현재 시각", hint: "time now clock", text: function () { return clock() + CARET; } },
  ];

  var menu = document.createElement("div");
  menu.className = "cm-complete"; menu.hidden = true;
  document.body.appendChild(menu);
  var mItems = [], mIndex = 0, mCtx = null;  // mCtx: {kind, from:{line,ch}}

  function menuOpen() { return !menu.hidden; }
  function closeMenu() { menu.hidden = true; mItems = []; mIndex = 0; mCtx = null; }

  function fuzzy(q, s) {
    q = q.toLowerCase(); s = s.toLowerCase();
    if (!q) return true;
    var i = 0;
    for (var j = 0; j < s.length && i < q.length; j++) if (s[j] === q[i]) i++;
    return i === q.length;
  }

  function paint() {
    menu.innerHTML = "";
    mItems.forEach(function (it, i) {
      var el = document.createElement("div");
      el.className = "cm-item" + (i === 0 ? " active" : "");
      el.innerHTML = '<span class="cm-label"></span>' + (it.sub ? '<span class="cm-sub muted"></span>' : "");
      el.querySelector(".cm-label").textContent = it.label;
      if (it.sub) el.querySelector(".cm-sub").textContent = it.sub;
      el.addEventListener("mousedown", function (e) { e.preventDefault(); pick(i); });
      menu.appendChild(el);
    });
    var c = cm.cursorCoords(true, "page");
    menu.style.left = c.left + "px";
    menu.style.top = (c.bottom + 4) + "px";
    menu.hidden = mItems.length === 0;
    mIndex = 0;
  }
  function highlight() {
    Array.prototype.forEach.call(menu.children, function (c, i) { c.classList.toggle("active", i === mIndex); });
  }

  function pick(i) {
    var it = mItems[i];
    if (!it || !mCtx) { closeMenu(); return; }
    it.apply();
    closeMenu();
    cm.focus();
  }

  function insertSnippet(from, snippet) {
    var caretMark = snippet.indexOf(CARET);
    var clean = snippet.replace(CARET, "");
    cm.replaceRange(clean, from, cm.getCursor());
    if (caretMark < 0) return;
    var before = clean.slice(0, caretMark);
    var nl = before.split("\n");
    var pos = nl.length === 1
      ? { line: from.line, ch: from.ch + caretMark }
      : { line: from.line + nl.length - 1, ch: nl[nl.length - 1].length };
    cm.setCursor(pos);
  }

  function showSlash(query, from) {
    mCtx = { kind: "slash", from: from };
    mItems = SLASH.filter(function (c) { return fuzzy(query, c.label + " " + (c.hint || "")); })
      .map(function (c) {
        return {
          label: c.label, sub: "",
          apply: function () { insertSnippet(from, typeof c.text === "function" ? c.text() : c.text); },
        };
      });
    paint();
  }

  var wikiTimer = null;
  function showWiki(query, from) {
    mCtx = { kind: "wiki", from: from };
    clearTimeout(wikiTimer);
    wikiTimer = setTimeout(function () {
      fetch("/api/complete?q=" + encodeURIComponent(query), { credentials: "same-origin" })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (!d || !d.ok || mCtx === null || mCtx.kind !== "wiki") return;
          mItems = d.items.map(function (it) {
            return {
              label: it.title, sub: it.path,
              apply: function () { cm.replaceRange("[[" + it.path + "]]", from, cm.getCursor()); },
            };
          });
          paint();
        }).catch(function () {});
    }, 120);
  }

  function scan() {
    var cur = cm.getCursor();
    var upto = cm.getLine(cur.line).slice(0, cur.ch);
    var open = upto.lastIndexOf("[[");
    if (open !== -1 && upto.indexOf("]]", open) === -1) {
      var q = upto.slice(open + 2);
      if (q.indexOf("[") === -1) { showWiki(q, { line: cur.line, ch: open }); return; }
    }
    var sm = upto.match(/(?:^|\s)\/([^\s/]*)$/);
    if (sm) { showSlash(sm[1], { line: cur.line, ch: cur.ch - sm[1].length - 1 }); return; }
    closeMenu();
  }
  cm.on("cursorActivity", scan);

  cm.addKeyMap({
    "Up": function () { if (menuOpen()) { mIndex = Math.max(0, mIndex - 1); highlight(); return; } return CM.Pass; },
    "Down": function () { if (menuOpen()) { mIndex = Math.min(mItems.length - 1, mIndex + 1); highlight(); return; } return CM.Pass; },
    "Enter": function () { if (menuOpen()) { pick(mIndex); return; } return CM.Pass; },
    "Tab": function () { if (menuOpen()) { pick(mIndex); return; } return CM.Pass; },
    "Esc": function () { if (menuOpen()) { closeMenu(); return; } return CM.Pass; },
  });
  cm.on("blur", function () { setTimeout(closeMenu, 150); });
})();
