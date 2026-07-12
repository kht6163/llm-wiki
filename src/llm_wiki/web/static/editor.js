// Markdown editor page wiring. The editor itself is md-editor-rt (React + CodeMirror 6),
// shipped as a vendored offline bundle that exposes window.WikiMdEditor.mount(el, opts).
// This file keeps all page concerns out of the bundle: form submit (CAS/base_version),
// CSRF, image upload, light/dark theme sync, and the status-bar word count.
(function () {
  "use strict";

  // ---- new-doc location control: folder + name -> hidden `path` + live preview ----
  // The location is chosen in the tree (or here), so the user types a name, not a
  // path. `.md` is implied (the server appends it); the folder is a plain field with
  // a datalist of existing folders, so creating into a new folder still works. Runs
  // before the editor guard below so it wires up even if the editor bundle is absent.
  (function locationControl() {
    var folder = document.getElementById("loc-folder");
    var name = document.getElementById("loc-name");
    var hidden = document.getElementById("loc-path");
    var preview = document.getElementById("loc-preview");
    if (!folder || !name || !hidden) return;
    function sync() {
      var f = folder.value.trim().replace(/^\/+|\/+$/g, "");
      var stem = name.value.trim().replace(/\.md$/i, "");
      var full = (f ? f + "/" : "") + stem;
      hidden.value = full ? full + ".md" : "";
      if (preview) preview.textContent = full ? "생성 위치 · " + full + ".md" : "";
    }
    folder.addEventListener("input", sync);
    name.addEventListener("input", sync);
    sync();
    // Pre-named from the tree -> let them write; otherwise focus the name first.
    if (!name.value.trim()) { name.focus(); }
  })();

  var form = document.querySelector(".editform");
  var textarea = document.getElementById("editor");
  var mountEl = document.getElementById("md-editor-mount");
  if (!form || !textarea || !mountEl || !window.WikiMdEditor) return;
  var W = window.WIKI || {};
  var csrf = (form.querySelector('input[name="csrf_token"]') || {}).value || W.csrf || "";

  function toast(msg) {
    var t = document.createElement("div");
    t.className = "rt-toast"; t.setAttribute("role", "status"); t.textContent = msg;
    document.body.appendChild(t);
    requestAnimationFrame(function () { t.classList.add("show"); });
    setTimeout(function () { t.classList.remove("show"); setTimeout(function () { t.remove(); }, 300); }, 3000);
  }

  // Upload an image to /api/upload and resolve to its URL (md-editor-rt inserts it).
  function uploadImage(file) {
    var fd = new FormData(); fd.append("file", file);
    return fetch("/api/upload", { method: "POST", headers: { "X-CSRF-Token": csrf }, body: fd, credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d && d.ok) return d.url;
        toast("업로드 실패: " + ((d && d.error && (d.error.message || d.error)) || "오류"));
        return null;
      })
      .catch(function () { toast("업로드 실패"); return null; });
  }

  function submit() { if (form.requestSubmit) form.requestSubmit(); else form.submit(); }

  // ---- live word/char count into the shell status bar (mirrors util.word_count) ----
  // The status bar lives in a later DOM block than this (content-block) script, so
  // look the elements up lazily — they may not exist yet when this file first runs.
  var cjkRe = /[぀-ヿ㐀-䶿一-鿿가-힣豈-﫿]/g;
  function count(v) {
    var elW = document.getElementById("sb-words"), elC = document.getElementById("sb-chars");
    if (!elW && !elC) return;
    v = v || "";
    var cjk = (v.match(cjkRe) || []).length;
    var latin = v.replace(cjkRe, " ").split(/\s+/).filter(Boolean).length;
    if (elW) elW.textContent = (cjk + latin) + " 단어";
    if (elC) elC.textContent = v.length + " 자";
  }

  var api = window.WikiMdEditor.mount(mountEl, {
    initialValue: textarea.value,
    theme: document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light",
    uploadImage: W.canWrite ? uploadImage : null,
    onChange: function (v) { textarea.value = v; count(v); },
    onSave: function (v) { textarea.value = v; submit(); },
  });
  mountEl.wikiEditorApi = api;
  count(textarea.value);

  // ---- unsaved-changes guard ------------------------------------------
  // Compare against the value the editor opened with, so leaving with edits in flight
  // (Cancel link or browser navigation) warns instead of silently discarding them.
  var initialValue = textarea.value;
  var saving = false;
  function isDirty() { return !saving && api.getValue() !== initialValue; }

  // Make sure the latest value reaches the form even if submit is triggered elsewhere.
  form.addEventListener("submit", function () {
    saving = true;                       // a real save is not an accidental navigation
    textarea.value = api.getValue();
  });

  window.addEventListener("beforeunload", function (e) {
    if (isDirty()) { e.preventDefault(); e.returnValue = ""; }
  });

  var cancelLink = document.getElementById("cancel-edit");
  if (cancelLink) {
    cancelLink.addEventListener("click", function (e) {
      if (isDirty() && !window.confirm("저장하지 않은 변경이 있습니다. 정말 나가시겠습니까?")) {
        e.preventDefault();
      }
    });
  }

  // ---- conflict recovery: load the server's current content into the editor ----
  // On a 409 the server renders its current body in #server-current; this lets the
  // user pull it into the editor in one click (then reapply their change) instead of
  // hand-copying out of a <pre>. Drive md-editor-rt's own CodeMirror view directly
  // (a full-document replace) so its onChange mirrors back into the textarea/count.
  var loadBtn = document.getElementById("load-current");
  var serverPre = document.getElementById("server-current");
  if (loadBtn && serverPre) {
    loadBtn.addEventListener("click", function () {
      var text = serverPre.textContent || "";
      var view = api.getView();
      if (view) {
        view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: text } });
      }
      textarea.value = text;
      count(text);
      initialValue = text;               // loading the server copy is the new baseline
      toast("서버의 현재 내용을 불러왔습니다. 변경을 다시 적용하고 저장하세요.");
    });
  }

  // Mirror the app's light/dark toggle into the editor.
  new MutationObserver(function () {
    api.setTheme(document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light");
  }).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });

  // =====================================================================
  // [[ ]] wikilink typeahead. md-editor-rt bundles its own CodeMirror, so we
  // can't add a CM extension from outside (it would be a 2nd instance). Instead
  // we drive md-editor-rt's OWN view (api.getView()) with plain transaction
  // specs and render the dropdown ourselves.
  // =====================================================================
  if (W.canWrite) setupWikiAutocomplete();

  function setupWikiAutocomplete() {
    var menu = document.createElement("div");
    menu.className = "wiki-ac"; menu.hidden = true;
    document.body.appendChild(menu);
    var items = [], index = 0, fromPos = -1, timer = null, requestSeq = 0;

    function isOpen() { return !menu.hidden; }
    function close() {
      requestSeq++; clearTimeout(timer); timer = null;
      menu.hidden = true; items = []; index = 0; fromPos = -1;
    }

    function paint(coords) {
      menu.innerHTML = "";
      items.forEach(function (it, i) {
        var row = document.createElement("div");
        row.className = "wiki-ac-item" + (i === index ? " active" : "");
        var t = document.createElement("span"); t.className = "wiki-ac-title"; t.textContent = it.title || it.path;
        var p = document.createElement("span"); p.className = "wiki-ac-path muted"; p.textContent = it.path;
        row.appendChild(t); row.appendChild(p);
        row.addEventListener("mousedown", function (e) { e.preventDefault(); pick(i); });
        menu.appendChild(row);
      });
      menu.style.left = Math.round(coords.left) + "px";
      menu.style.top = Math.round(coords.bottom + 4) + "px";
      menu.hidden = items.length === 0;
    }
    function highlight() {
      Array.prototype.forEach.call(menu.children, function (c, i) { c.classList.toggle("active", i === index); });
    }

    function pick(i) {
      var view = api.getView();
      if (!view || i < 0 || i >= items.length || fromPos < 0) { close(); return; }
      var path = items[i].path;
      var head = view.state.selection.main.head;
      var hasCloser = view.state.doc.sliceString(head, head + 2) === "]]";
      var insert = path + (hasCloser ? "" : "]]");
      view.dispatch({
        changes: { from: fromPos, to: head, insert: insert },
        selection: { anchor: fromPos + path.length + 2 },
      });
      close();
      view.focus();
    }

    function scan() {
      var requestId = ++requestSeq;
      var view = api.getView();
      if (!view) { close(); return; }
      var head = view.state.selection.main.head;
      var line = view.state.doc.lineAt(head);
      var before = view.state.doc.sliceString(line.from, head);
      var open = before.lastIndexOf("[[");
      if (open === -1 || before.indexOf("]]", open) !== -1) { close(); return; }
      var q = before.slice(open + 2);
      if (q.indexOf("[") !== -1) { close(); return; }
      fromPos = line.from + open + 2;
      var coords = view.coordsAtPos(head) || view.coordsAtPos(fromPos);
      if (!coords) { close(); return; }
      clearTimeout(timer);
      timer = setTimeout(function () {
        fetch("/api/complete?q=" + encodeURIComponent(q), { credentials: "same-origin" })
          .then(function (r) { return r.json(); })
          .then(function (d) {
            if (requestId !== requestSeq) return;
            if (!d || !d.ok || !d.items || !d.items.length) { close(); return; }
            items = d.items; index = 0; paint(coords);
          }).catch(function () { if (requestId === requestSeq) close(); });
      }, 100);
    }

    // Intercept nav keys before CodeMirror sees them (capture phase).
    mountEl.addEventListener("keydown", function (e) {
      if (!isOpen()) { if (e.key === "Escape") close(); return; }
      if (e.key === "ArrowDown") { index = Math.min(items.length - 1, index + 1); highlight(); e.preventDefault(); e.stopPropagation(); }
      else if (e.key === "ArrowUp") { index = Math.max(0, index - 1); highlight(); e.preventDefault(); e.stopPropagation(); }
      else if (e.key === "Enter" || e.key === "Tab") { pick(index); e.preventDefault(); e.stopPropagation(); }
      else if (e.key === "Escape") { close(); e.preventDefault(); e.stopPropagation(); }
    }, true);
    mountEl.addEventListener("input", scan);
    mountEl.addEventListener("keyup", function (e) {
      if (["ArrowDown", "ArrowUp", "Enter", "Tab", "Escape"].indexOf(e.key) === -1) scan();
    });
    document.addEventListener("mousedown", function (e) { if (!menu.contains(e.target)) close(); });
  }
})();
