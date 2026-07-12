// Explicit three-way merge resolution for the existing CAS edit form.
(function () {
  "use strict";

  var active = null;

  function dispose() {
    if (!active) return;
    active.cleanups.forEach(function (cleanup) { cleanup(); });
    active = null;
  }

  function listen(target, type, handler, options, cleanups) {
    target.addEventListener(type, handler, options);
    cleanups.push(function () { target.removeEventListener(type, handler, options); });
  }

  function parsePayload(root) {
    var element = root.querySelector("#merge-payload");
    if (!element) return null;
    try {
      return JSON.parse(element.textContent || "");
    } catch (_error) {
      return null;
    }
  }

  function writeEditor(root, text) {
    var textarea = root.querySelector("#editor");
    var mount = root.querySelector("#md-editor-mount");
    var api = mount.wikiEditorApi;
    var view = api && typeof api.getView === "function" ? api.getView() : null;
    if (view) {
      view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: text } });
    }
    textarea.value = text;
  }

  function lineOffset(text, lineNumber) {
    var offset = 0;
    for (var line = 1; line < lineNumber; line += 1) {
      var next = text.indexOf("\n", offset);
      if (next === -1) return text.length;
      offset = next + 1;
    }
    return offset;
  }

  function serialize(payload, resolutions) {
    var merged = payload.merged;
    var cursor = 0;
    var output = "";
    payload.conflicts.forEach(function (hunk, index) {
      var position = hunk.base ? merged.indexOf(hunk.base, cursor) : lineOffset(merged, hunk.start_line);
      position = Math.max(position, cursor);
      output += merged.slice(cursor, position) + resolutions[index];
      cursor = position + hunk.base.length;
    });
    return output + merged.slice(cursor);
  }

  function init(root) {
    dispose();
    root = root || document;
    var resolver = root.querySelector("#merge-resolver");
    var payload = parsePayload(root);
    var form = root.querySelector(".editform");
    if (!resolver) return null;
    if (!payload) return null;
    if (!form) return null;

    var cleanups = [];
    active = { cleanups: cleanups, mine: payload.mine };
    var version = form.querySelector('input[name="base_version"]');
    version.value = String(payload.current_version);

    var mode = resolver.dataset.mergeState;
    if (mode === "manual-only") return active;

    var save = form.querySelector("[data-merge-save]");
    var ready = false;

    function focusTarget() {
      if (mode === "proposal") return resolver.querySelector("#apply-merge-proposal");
      var unresolved = resolver.querySelector(".merge-conflict:not(.is-resolved)");
      return unresolved ? unresolved.querySelector("[data-resolution]") : save;
    }

    function blockUnresolved(event) {
      if (ready) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      var target = focusTarget();
      target.focus();
    }

    listen(form, "submit", blockUnresolved, true, cleanups);

    if (mode === "proposal") {
      var apply = resolver.querySelector("#apply-merge-proposal");
      function applyProposal() {
        writeEditor(root, payload.merged);
        ready = true;
        save.disabled = false;
        save.focus();
      }
      listen(apply, "click", applyProposal, false, cleanups);
      return active;
    }

    var fields = Array.prototype.slice.call(resolver.querySelectorAll(".merge-conflict"));
    var resolutions = fields.map(function () { return null; });
    var progress = resolver.querySelector("#merge-progress");

    function updateProgress() {
      var count = resolutions.filter(function (resolution) { return resolution !== null; }).length;
      progress.textContent = "해결 " + count + " / " + fields.length;
      ready = count === fields.length;
      if (ready) writeEditor(root, serialize(payload, resolutions));
      save.disabled = !ready;
    }

    function resolve(index, kind, button) {
      var field = fields[index];
      var hunk = payload.conflicts[index];
      resolutions[index] = kind === "manual" ? field.querySelector("textarea").value : hunk[kind];
      field.classList.add("is-resolved");
      field.querySelectorAll("[data-resolution]").forEach(function (choice) {
        choice.setAttribute("aria-pressed", String(choice === button));
      });
      updateProgress();
      var target = focusTarget();
      target.focus();
    }

    fields.forEach(function (field, index) {
      field.querySelectorAll("[data-resolution]").forEach(function (button) {
        function choose() { resolve(index, button.dataset.resolution, button); }
        button.setAttribute("aria-pressed", "false");
        listen(button, "click", choose, false, cleanups);
      });
    });
    updateProgress();
    return active;
  }

  window.WikiMerge = { init: init, dispose: dispose };
  init();
})();
