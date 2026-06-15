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
    if (e.key === "Tab" && ac.hidden) { e.preventDefault(); indent(e.shiftKey); return; }
    if (!(e.metaKey || e.ctrlKey) || e.altKey) return;
    var k = e.key.toLowerCase();
    if (k === "b") { e.preventDefault(); wrap("**", "**"); }
    else if (k === "i") { e.preventDefault(); wrap("*", "*"); }
    else if (k === "k") { e.preventDefault(); makeLink(); }
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
          else alert("업로드 실패: " + ((d && d.error && (d.error.message || d.error)) || "오류"));
        })
        .catch(function () { alert("업로드 실패"); });
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
})();
