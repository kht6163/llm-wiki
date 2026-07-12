(function () {
  "use strict";

  if (window.WikiSearch && window.WikiSearch.dispose) window.WikiSearch.dispose();

  var disposed = false;
  var root = null;
  var readyListener = null;
  var SAVED_KEY = "wiki-saved-searches";
  var MAX_SAVED = 20;

  function editable(target) {
    return target instanceof Element && Boolean(target.closest("input, textarea, select, [contenteditable]"));
  }

  function queryTokens(query) {
    var pattern = /\b(title|path|tag|has):("(?:[^"\\]|\\.)*"|\S+)/g;
    var tokens = [];
    var match;
    while ((match = pattern.exec(query)) !== null) {
      var raw = match[2];
      var value = raw.startsWith('"') ? raw.slice(1, -1).replace(/\\"/g, '"').trim() : raw.trim();
      if (value) tokens.push({ start: match.index, end: pattern.lastIndex });
    }
    return tokens;
  }

  function removeQueryFilter(form, index) {
    var field = form.elements.q;
    var query = field.value;
    var token = queryTokens(query)[index];
    if (!token) return false;
    var end = token.end + (/[ \t\r\n]/.test(query.charAt(token.end)) ? 1 : 0);
    field.value = query.slice(0, token.start) + query.slice(end);
    return true;
  }

  function removeRequestTag(form, index) {
    var fields = form.querySelectorAll('input[name="tag"]');
    if (!fields[index]) return false;
    fields[index].remove();
    return true;
  }

  function submitRemoval(button) {
    var form = document.getElementById("search-remove-form");
    if (!form) return;
    var kind = button.dataset.removeFilter;
    var index = Number(button.dataset.filterIndex || 0);
    var changed = kind === "query" ? removeQueryFilter(form, index) :
      kind === "tag" ? removeRequestTag(form, index) : false;
    if (kind === "folder") {
      var folder = form.querySelector('input[name="folder"]');
      if (folder) folder.remove();
      changed = Boolean(folder);
    }
    if (!changed) return;
    form.elements.page.value = "1";
    form.requestSubmit();
  }

  function readSaved() {
    try {
      var raw = window.localStorage.getItem(SAVED_KEY);
      if (!raw) return [];
      var list = JSON.parse(raw);
      return Array.isArray(list) ? list : [];
    } catch (e) {
      return [];
    }
  }

  function writeSaved(list) {
    try {
      window.localStorage.setItem(SAVED_KEY, JSON.stringify(list));
    } catch (e) { /* private mode / quota */ }
  }

  function collectState() {
    var form = root && root.querySelector("form.searchform");
    if (!form) return null;
    var qEl = form.elements.q;
    var modeEl = form.elements.mode;
    var folderEl = form.elements.folder;
    var tags = [];
    form.querySelectorAll('input[name="tag"]').forEach(function (el) {
      var v = (el.value || "").trim();
      if (v) tags.push(v);
    });
    return {
      q: qEl ? String(qEl.value || "") : "",
      mode: modeEl ? String(modeEl.value || "hybrid") : "hybrid",
      folder: folderEl ? String(folderEl.value || "") : "",
      tags: tags,
    };
  }

  function buildSearchUrl(entry) {
    var params = new URLSearchParams();
    if (entry.q) params.set("q", entry.q);
    if (entry.mode) params.set("mode", entry.mode);
    if (entry.folder) params.set("folder", entry.folder);
    (entry.tags || []).forEach(function (tag) {
      if (tag) params.append("tag", tag);
    });
    var qs = params.toString();
    return qs ? "/search?" + qs : "/search";
  }

  function renderSaved() {
    var listEl = document.getElementById("saved-searches-list");
    if (!listEl) return;
    var list = readSaved();
    listEl.replaceChildren();
    list.forEach(function (entry) {
      if (!entry || !entry.name) return;
      var li = document.createElement("li");
      li.className = "saved-search-item";
      li.dataset.savedName = entry.name;

      var link = document.createElement("a");
      link.href = buildSearchUrl(entry);
      link.dataset.savedSearch = "1";
      link.className = "saved-search-link";
      link.textContent = entry.name;
      link.title = entry.q || entry.name;

      var del = document.createElement("button");
      del.type = "button";
      del.className = "saved-search-delete";
      del.dataset.deleteSaved = "1";
      del.setAttribute("aria-label", "삭제: " + entry.name);
      del.textContent = "×";

      li.appendChild(link);
      li.appendChild(del);
      listEl.appendChild(li);
    });
  }

  function saveCurrent() {
    var state = collectState();
    if (!state) return;
    var name = window.prompt("저장할 이름");
    if (name == null) return;
    name = String(name).trim();
    if (!name) return;
    var list = readSaved();
    var idx = -1;
    for (var i = 0; i < list.length; i++) {
      if (list[i] && list[i].name === name) { idx = i; break; }
    }
    var entry = {
      name: name,
      q: state.q,
      mode: state.mode,
      folder: state.folder,
      tags: state.tags.slice(),
    };
    if (idx >= 0) {
      list[idx] = entry;
    } else if (list.length >= MAX_SAVED) {
      return;
    } else {
      list.push(entry);
    }
    writeSaved(list);
    renderSaved();
  }

  function deleteSaved(name) {
    var list = readSaved().filter(function (e) { return e && e.name !== name; });
    writeSaved(list);
    renderSaved();
  }

  function onClick(event) {
    var saveBtn = event.target.closest("[data-save-search]");
    if (saveBtn && root && root.contains(saveBtn)) {
      event.preventDefault();
      saveCurrent();
      return;
    }
    var delBtn = event.target.closest("[data-delete-saved]");
    if (delBtn && root && root.contains(delBtn)) {
      event.preventDefault();
      var row = delBtn.closest("[data-saved-name]");
      if (row && row.dataset.savedName) deleteSaved(row.dataset.savedName);
      return;
    }
    var button = event.target.closest("[data-remove-filter]");
    if (button && root && root.contains(button)) submitRemoval(button);
  }

  function onKeydown(event) {
    if (event.key === "?" && !editable(event.target)) {
      var help = document.getElementById("search-help");
      if (!help) return;
      event.preventDefault();
      help.open = true;
      help.querySelector("summary").focus();
      return;
    }
    if (event.key !== "Escape") return;
    var details = event.target.closest("details[open]");
    if (!details || !root.contains(details)) return;
    event.preventDefault();
    details.open = false;
    details.querySelector("summary").focus();
  }

  function init() {
    if (disposed) return;
    root = document.getElementById("search-workbench");
    if (!root) return;
    root.addEventListener("click", onClick);
    document.addEventListener("keydown", onKeydown);
    renderSaved();
  }

  var controller = {
    dispose: function () {
      if (disposed) return;
      disposed = true;
      if (readyListener) document.removeEventListener("DOMContentLoaded", readyListener);
      if (root) root.removeEventListener("click", onClick);
      document.removeEventListener("keydown", onKeydown);
      if (window.WikiSearch === controller) delete window.WikiSearch;
    },
  };
  window.WikiSearch = controller;

  if (document.readyState === "loading") {
    readyListener = init;
    document.addEventListener("DOMContentLoaded", readyListener, { once: true });
  } else {
    init();
  }
})();
