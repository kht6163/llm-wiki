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
    var api = mount && mount.wikiEditorApi;
    var view = api && typeof api.getView === "function" ? api.getView() : null;
    if (view) {
      view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: text } });
    }
    textarea.value = text;
  }

  function serialize(payload, resolutions) {
    var text = payload.merged;
    var adjustment = 0;
    payload.conflicts.forEach(function (hunk, index) {
      var start = hunk.merged_start + adjustment;
      var replacement = resolutions[index];
      text = text.slice(0, start) + replacement + text.slice(start + hunk.base.length);
      adjustment += replacement.length - hunk.base.length;
    });
    return text;
  }

  function ensureError(root, resolver) {
    var error = root.querySelector("#merge-error");
    if (!error) {
      error = document.createElement("p");
      error.id = "merge-error";
      error.setAttribute("role", "alert");
      error.hidden = true;
      resolver.appendChild(error);
    }
    error.setAttribute("tabindex", "-1");
    return error;
  }

  function validCore(payload) {
    return payload && typeof payload === "object" &&
      Number.isInteger(payload.current_version) && payload.current_version >= 0 &&
      typeof payload.mine === "string" && typeof payload.current === "string" &&
      typeof payload.manual_only === "boolean" &&
      (payload.base === null || typeof payload.base === "string") && Array.isArray(payload.conflicts);
  }

  function validConflicts(payload, fields) {
    if (typeof payload.merged !== "string" || fields.length !== payload.conflicts.length) return false;
    var previousEnd = 0;
    return payload.conflicts.every(function (hunk, index) {
      if (!hunk || typeof hunk !== "object" || !Number.isInteger(hunk.start_line) ||
          hunk.start_line < 1 || !Number.isInteger(hunk.merged_start) ||
          typeof hunk.base !== "string" || typeof hunk.mine !== "string" ||
          typeof hunk.current !== "string") return false;
      var start = hunk.merged_start;
      var end = start + hunk.base.length;
      if (start < previousEnd || start < 0 || end > payload.merged.length ||
          payload.merged.slice(start, end) !== hunk.base) return false;
      previousEnd = end;
      var field = fields[index];
      var buttons = field.querySelectorAll("[data-resolution]");
      var kinds = Array.prototype.map.call(buttons, function (button) {
        return button.dataset.resolution;
      }).sort().join(",");
      return field.dataset.conflictIndex === String(index) && buttons.length === 3 &&
        kinds === "current,manual,mine" && Boolean(field.querySelector("textarea"));
    });
  }

  function init(root) {
    dispose();
    root = root || document;
    var resolver = root.querySelector("#merge-resolver");
    if (!resolver) return null;

    var cleanups = [];
    active = { cleanups: cleanups, mine: null };
    var error = ensureError(root, resolver);
    var payload = parsePayload(root);
    var form = root.querySelector(".editform");
    var save = form && form.querySelector("[data-merge-save]");
    var ready = false;
    var mode = resolver.dataset.mergeState;

    function focusTarget() {
      if (mode === "proposal") return resolver.querySelector("#apply-merge-proposal");
      var unresolved = resolver.querySelector(".merge-conflict:not(.is-resolved)");
      return unresolved ? unresolved.querySelector("[data-resolution]") : (save || error);
    }

    function blockUnresolved(event) {
      if (ready) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      var target = focusTarget();
      (target || error).focus();
    }

    if (form) listen(form, "submit", blockUnresolved, true, cleanups);

    function fail() {
      ready = false;
      error.hidden = false;
      error.textContent = "병합 데이터를 확인할 수 없습니다. 원본 편집은 보존되었습니다. 페이지를 다시 여세요.";
      if (save) save.disabled = true;
      return active;
    }

    if (!form || !save || !validCore(payload)) return fail();
    var version = form.querySelector('input[name="base_version"]');
    var textarea = form.querySelector("#editor");
    if (!version || !textarea) return fail();
    active.mine = payload.mine;
    version.value = String(payload.current_version);
    if (["manual-only", "proposal", "conflicts"].indexOf(mode) === -1) return fail();

    if (mode === "manual-only") {
      if (!payload.manual_only || payload.base !== null || payload.merged !== null ||
          payload.conflicts.length !== 0) return fail();
      ready = true;
      return active;
    }

    save.disabled = true;

    if (mode === "proposal") {
      var apply = resolver.querySelector("#apply-merge-proposal");
      if (payload.manual_only || typeof payload.base !== "string" || payload.conflicts.length !== 0 ||
          typeof payload.merged !== "string" || !apply) return fail();
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
    var progress = resolver.querySelector("#merge-progress");
    if (payload.manual_only || typeof payload.base !== "string" || !progress ||
        !validConflicts(payload, fields)) return fail();
    var resolutions = fields.map(function () { return null; });

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
