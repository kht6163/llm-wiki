// Editor enhancements: debounced live preview, [[wikilink]] autocomplete, Ctrl/Cmd-S save.
(function () {
  "use strict";
  var form = document.querySelector(".editform");
  var editor = document.getElementById("editor");
  var preview = document.getElementById("preview");
  var toggle = document.getElementById("preview-toggle");
  var ac = document.getElementById("ac");
  if (!form || !editor) return;

  var csrf = (form.querySelector('input[name="csrf_token"]') || {}).value || "";
  var docPath = form.getAttribute("data-path") || "preview.md";
  var slashMenuOpen = false;  // set by the slash-command module below

  // Non-blocking toast (reuses realtime.js's .rt-toast styling) so an upload error
  // doesn't yank focus out of the editor the way a modal alert() does.
  function toast(msg) {
    var t = document.createElement("div");
    t.className = "rt-toast";
    t.setAttribute("role", "status");
    t.textContent = msg;
    document.body.appendChild(t);
    requestAnimationFrame(function () { t.classList.add("show"); });
    setTimeout(function () {
      t.classList.remove("show");
      setTimeout(function () { t.remove(); }, 300);
    }, 3500);
  }

  // ---- live preview ----
  var timer = null;
  function renderPreview() {
    if (!preview || (toggle && !toggle.checked)) return;
    var body = new URLSearchParams();
    body.set("content", editor.value);
    body.set("path", docPath || "preview.md");
    body.set("csrf_token", csrf);
    fetch("/api/preview", { method: "POST", headers: { "X-CSRF-Token": csrf }, body: body })
      .then(function (r) { return r.json(); })
      .then(function (d) { if (d && d.ok) preview.innerHTML = d.html; })
      .catch(function () {});
  }
  function schedulePreview() { clearTimeout(timer); timer = setTimeout(renderPreview, 300); }
  editor.addEventListener("input", schedulePreview);

  // ---- live status-bar word/char count (mirrors util.word_count) ----
  var cjkRe = /[぀-ヿ㐀-䶿一-鿿가-힣豈-﫿]/g;
  var elWords = document.getElementById("sb-words");
  var elChars = document.getElementById("sb-chars");
  function updateCount() {
    if (!elWords && !elChars) return;
    var v = editor.value || "";
    var cjk = (v.match(cjkRe) || []).length;
    var latin = v.replace(cjkRe, " ").split(/\s+/).filter(Boolean).length;
    if (elWords) elWords.textContent = (cjk + latin) + " 단어";
    if (elChars) elChars.textContent = v.length + " 자";
  }
  editor.addEventListener("input", updateCount);
  updateCount();

  if (toggle) {
    toggle.addEventListener("change", function () {
      preview.style.display = toggle.checked ? "" : "none";
      if (toggle.checked) renderPreview();
    });
  }
  renderPreview();

  // ---- Ctrl/Cmd-S to save ----
  document.addEventListener("keydown", function (e) {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "s") {
      e.preventDefault();
      if (form.requestSubmit) form.requestSubmit(); else form.submit();
    }
  });

  // ---- formatting shortcuts (Ctrl/Cmd-B/I/K) + Tab indent ----
  function wrap(prefix, suffix) {
    var s = editor.selectionStart, e = editor.selectionEnd;
    var sel = editor.value.slice(s, e);
    editor.setRangeText(prefix + sel + suffix, s, e, "end");
    if (s === e) editor.setSelectionRange(s + prefix.length, s + prefix.length);
    else editor.setSelectionRange(s, s + prefix.length + sel.length + suffix.length);
    schedulePreview();
  }
  function makeLink() {
    var s = editor.selectionStart, e = editor.selectionEnd;
    var sel = editor.value.slice(s, e) || "텍스트";
    editor.setRangeText("[" + sel + "](url)", s, e, "end");
    var urlStart = s + 1 + sel.length + 2;  // after "[sel]("
    editor.setSelectionRange(urlStart, urlStart + 3);
    schedulePreview();
  }
  function indent(out) {
    var s = editor.selectionStart, e = editor.selectionEnd, val = editor.value;
    if (s === e && !out) { editor.setRangeText("  ", s, e, "end"); schedulePreview(); return; }
    var lineStart = val.lastIndexOf("\n", s - 1) + 1;
    var seg = val.slice(lineStart, e);
    var newSeg = out ? seg.replace(/^( {1,2}|\t)/gm, "") : seg.replace(/^/gm, "  ");
    editor.setRangeText(newSeg, lineStart, e, "end");
    editor.setSelectionRange(lineStart, lineStart + newSeg.length);
    schedulePreview();
  }
  editor.addEventListener("keydown", function (e) {
    if (slashMenuOpen) return;  // slash module owns the keys while its menu is open
    if (e.key === "Tab" && ac.hidden) { e.preventDefault(); indent(e.shiftKey); return; }
    if (!(e.metaKey || e.ctrlKey) || e.altKey) return;
    var k = e.key.toLowerCase();
    if (k === "b") { e.preventDefault(); wrap("**", "**"); }
    else if (k === "i") { e.preventDefault(); wrap("*", "*"); }
    else if (k === "k") { e.preventDefault(); makeLink(); }
    else if (k === "e") { e.preventDefault(); wrap("==", "=="); }       // highlight
    else if (k === "d") { e.preventDefault(); wrap("~~", "~~"); }       // strikethrough
  });

  // ---- image / file upload (drag & drop, paste) ----
  function insertAtCaret(text) {
    var s = editor.selectionStart, e = editor.selectionEnd;
    editor.setRangeText(text, s, e, "end");
    editor.focus();
    schedulePreview();
  }
  function uploadFiles(files) {
    Array.prototype.forEach.call(files || [], function (f) {
      var fd = new FormData();
      fd.append("file", f);
      fetch("/api/upload", { method: "POST", headers: { "X-CSRF-Token": csrf }, body: fd })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (d && d.ok && d.markdown) insertAtCaret(d.markdown + "\n");
          else toast("업로드 실패: " + ((d && d.error && (d.error.message || d.error)) || "오류"));
        })
        .catch(function () { toast("업로드 실패"); });
    });
  }
  editor.addEventListener("dragover", function (e) { e.preventDefault(); editor.classList.add("dropping"); });
  editor.addEventListener("dragleave", function () { editor.classList.remove("dropping"); });
  editor.addEventListener("drop", function (e) {
    if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length) {
      e.preventDefault(); editor.classList.remove("dropping"); uploadFiles(e.dataTransfer.files);
    }
  });
  editor.addEventListener("paste", function (e) {
    var files = e.clipboardData && e.clipboardData.files;
    if (files && files.length) { e.preventDefault(); uploadFiles(files); }
  });

  // ---- [[wikilink]] autocomplete ----
  if (!ac) return;
  var acItems = [];
  var acIndex = -1;

  function closeAc() { ac.hidden = true; acIndex = -1; }

  function openTrigger() {
    // Find an unclosed "[[" before the caret on the current segment.
    var pos = editor.selectionStart;
    var upto = editor.value.slice(0, pos);
    var open = upto.lastIndexOf("[[");
    if (open === -1) return null;
    if (upto.indexOf("]]", open) !== -1) return null;
    var frag = upto.slice(open + 2);
    if (frag.indexOf("\n") !== -1) return null;
    return { open: open, query: frag };
  }

  function showSuggestions(items) {
    acItems = items || [];
    if (!acItems.length) { closeAc(); return; }
    ac.innerHTML = "";
    acItems.forEach(function (it, i) {
      var el = document.createElement("div");
      el.className = "ac-item";
      el.textContent = it.title + "  —  " + it.path;
      el.addEventListener("mousedown", function (ev) { ev.preventDefault(); pick(i); });
      ac.appendChild(el);
    });
    var r = editor.getBoundingClientRect();
    ac.style.left = (window.scrollX + r.left + 12) + "px";
    ac.style.top = (window.scrollY + r.top + 28) + "px";
    ac.hidden = false;
    acIndex = 0;
    highlight();
  }

  function highlight() {
    Array.prototype.forEach.call(ac.children, function (c, i) {
      c.classList.toggle("active", i === acIndex);
    });
  }

  function pick(i) {
    var t = openTrigger();
    if (!t || !acItems[i]) { closeAc(); return; }
    var pos = editor.selectionStart;
    var before = editor.value.slice(0, t.open);
    var after = editor.value.slice(pos);
    var insert = "[[" + acItems[i].path + "]]";
    editor.value = before + insert + after;
    var caret = (before + insert).length;
    editor.setSelectionRange(caret, caret);
    closeAc();
    editor.focus();
    schedulePreview();
  }

  var acTimer = null;
  editor.addEventListener("input", function () {
    var t = openTrigger();
    if (!t) { closeAc(); return; }
    clearTimeout(acTimer);
    acTimer = setTimeout(function () {
      fetch("/api/complete?q=" + encodeURIComponent(t.query))
        .then(function (r) { return r.json(); })
        .then(function (d) { if (d && d.ok) showSuggestions(d.items); })
        .catch(function () {});
    }, 150);
  });

  editor.addEventListener("keydown", function (e) {
    if (ac.hidden) return;
    if (e.key === "ArrowDown") { e.preventDefault(); acIndex = Math.min(acIndex + 1, acItems.length - 1); highlight(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); acIndex = Math.max(acIndex - 1, 0); highlight(); }
    else if (e.key === "Enter" && acIndex >= 0) { e.preventDefault(); pick(acIndex); }
    else if (e.key === "Escape") { closeAc(); }
  });
  editor.addEventListener("blur", function () { setTimeout(closeAc, 150); });

  // ---- "/" slash commands ------------------------------------------------
  var CARET = String.fromCharCode(0);  // sentinel marking where the caret lands
  function two(n) { return n < 10 ? "0" + n : "" + n; }
  function today() { var d = new Date(); return d.getFullYear() + "-" + two(d.getMonth() + 1) + "-" + two(d.getDate()); }
  function clock() { var d = new Date(); return two(d.getHours()) + ":" + two(d.getMinutes()); }

  var SLASH = [
    { label: "제목 1", hint: "h1 heading title", text: "# " + CARET },
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
    { label: "현재 시각", hint: "time now clock", text: function () { return clock() + CARET; } }
  ];

  var slash = document.createElement("div");
  slash.className = "slash-menu";
  slash.hidden = true;
  document.body.appendChild(slash);
  var slashItems = [], slashIndex = 0, slashTrigger = null;

  function closeSlash() { slash.hidden = true; slashMenuOpen = false; slashIndex = 0; slashTrigger = null; }

  function slashTriggerAt() {
    var pos = editor.selectionStart;
    if (pos !== editor.selectionEnd) return null;
    var upto = editor.value.slice(0, pos);
    var line = upto.slice(upto.lastIndexOf("\n") + 1);
    var m = line.match(/(?:^|\s)\/([^\s/]*)$/);
    if (!m) return null;
    return { slashPos: pos - m[1].length - 1, query: m[1] };
  }

  function fuzzySlash(q, s) {
    q = q.toLowerCase(); s = s.toLowerCase();
    if (!q) return true;
    var i = 0;
    for (var j = 0; j < s.length && i < q.length; j++) if (s[j] === q[i]) i++;
    return i === q.length;
  }

  function showSlash(matches) {
    slashItems = matches;
    if (!matches.length) { closeSlash(); return; }
    slash.innerHTML = "";
    matches.forEach(function (it, i) {
      var el = document.createElement("div");
      el.className = "slash-item" + (i === 0 ? " active" : "");
      el.textContent = it.label;
      el.addEventListener("mousedown", function (ev) { ev.preventDefault(); pickSlash(i); });
      slash.appendChild(el);
    });
    var c = caretCoords(editor, editor.selectionStart);
    slash.style.left = c.left + "px";
    slash.style.top = c.top + "px";
    slash.hidden = false;
    slashMenuOpen = true;
    slashIndex = 0;
  }
  function highlightSlash() {
    Array.prototype.forEach.call(slash.children, function (c, i) { c.classList.toggle("active", i === slashIndex); });
  }
  function pickSlash(i) {
    var cmd = slashItems[i];
    if (!cmd || !slashTrigger) { closeSlash(); return; }
    var pos = editor.selectionStart;
    editor.setRangeText("", slashTrigger.slashPos, pos, "end");  // drop "/query"
    var at = editor.selectionStart;
    var snippet = typeof cmd.text === "function" ? cmd.text() : cmd.text;
    var caretMark = snippet.indexOf(CARET);
    var clean = snippet.replace(CARET, "");
    editor.setRangeText(clean, at, at, "end");
    var caret = caretMark >= 0 ? at + caretMark : at + clean.length;
    editor.setSelectionRange(caret, caret);
    closeSlash();
    editor.focus();
    schedulePreview();
  }

  editor.addEventListener("input", function () {
    var t = slashTriggerAt();
    if (!t) { closeSlash(); return; }
    slashTrigger = t;
    showSlash(SLASH.filter(function (c) { return fuzzySlash(t.query, c.label + " " + (c.hint || "")); }));
  });
  editor.addEventListener("keydown", function (e) {
    if (!slashMenuOpen) return;
    if (e.key === "ArrowDown") { e.preventDefault(); e.stopImmediatePropagation(); slashIndex = Math.min(slashIndex + 1, slashItems.length - 1); highlightSlash(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); e.stopImmediatePropagation(); slashIndex = Math.max(slashIndex - 1, 0); highlightSlash(); }
    else if (e.key === "Enter" || e.key === "Tab") { e.preventDefault(); e.stopImmediatePropagation(); pickSlash(slashIndex); }
    else if (e.key === "Escape") { e.preventDefault(); e.stopImmediatePropagation(); closeSlash(); }
  });
  editor.addEventListener("blur", function () { setTimeout(closeSlash, 150); });

  // Caret pixel coordinates in a <textarea> via the mirror-div technique: clone the
  // textarea's text + styles into a hidden div and measure a marker at the caret.
  function caretCoords(ta, pos) {
    var rect = ta.getBoundingClientRect();
    var style = getComputedStyle(ta);
    var div = document.createElement("div");
    ["boxSizing", "width", "paddingTop", "paddingRight", "paddingBottom", "paddingLeft",
     "borderTopWidth", "borderRightWidth", "borderBottomWidth", "borderLeftWidth",
     "fontFamily", "fontSize", "fontWeight", "fontStyle", "letterSpacing", "lineHeight",
     "textTransform", "wordSpacing", "tabSize"].forEach(function (p) { div.style[p] = style[p]; });
    div.style.position = "absolute";
    div.style.visibility = "hidden";
    div.style.whiteSpace = "pre-wrap";
    div.style.wordWrap = "break-word";
    div.style.overflow = "hidden";
    div.style.top = (rect.top + window.scrollY) + "px";
    div.style.left = (rect.left + window.scrollX) + "px";
    div.style.width = ta.clientWidth + "px";
    div.textContent = ta.value.slice(0, pos);
    var span = document.createElement("span");
    span.textContent = ta.value.slice(pos) || ".";
    div.appendChild(span);
    document.body.appendChild(div);
    var lh = parseInt(style.lineHeight, 10) || (parseInt(style.fontSize, 10) * 1.4);
    var top = rect.top + window.scrollY + span.offsetTop - ta.scrollTop + lh;
    var left = rect.left + window.scrollX + span.offsetLeft - ta.scrollLeft;
    document.body.removeChild(div);
    left = Math.min(left, rect.left + window.scrollX + ta.clientWidth - 220);
    return { left: Math.max(rect.left + window.scrollX, left), top: top };
  }
})();
