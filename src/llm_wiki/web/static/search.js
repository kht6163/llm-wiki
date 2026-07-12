(function () {
  "use strict";

  if (window.WikiSearch && window.WikiSearch.dispose) window.WikiSearch.dispose();

  var disposed = false;
  var root = null;
  var readyListener = null;

  function editable(target) {
    return target instanceof Element && Boolean(target.closest("input, textarea, select, [contenteditable]"));
  }

  function queryTokens(query) {
    var pattern = /\b(title|path|tag|has):("(?:[^"\\]|\\.)*"|\S+)/g;
    var tokens = [];
    var match;
    while ((match = pattern.exec(query)) !== null) tokens.push({ start: match.index, end: pattern.lastIndex });
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

  function onClick(event) {
    var button = event.target.closest("[data-remove-filter]");
    if (button && root.contains(button)) submitRemoval(button);
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
