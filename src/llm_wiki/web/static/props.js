// Inline frontmatter property editor for the reading view. The read-only panel
// (templates/view.html .doc-props) lists key -> value chips; "속성 편집" swaps in a
// small editor (key + comma-separated values per row, add/remove), and saving POSTs
// the whole set to /api/doc/<path>/properties as one CAS-guarded revision.
(function () {
  "use strict";
  var wrap = document.getElementById("doc-props-wrap");
  var W = window.WIKI || {};
  if (!wrap || !W.canWrite) return;
  var path = wrap.getAttribute("data-path");
  var version = wrap.getAttribute("data-version");

  function enc(p) { return p.split("/").map(encodeURIComponent).join("/"); }
  function msg(d) { return (d && d.error && (d.error.message || d.error)) || (d && d.message) || "오류"; }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  // Current properties from the rendered read-only panel.
  function readCurrent() {
    var rows = [];
    wrap.querySelectorAll(".doc-props .prop").forEach(function (p) {
      var vals = Array.prototype.map.call(p.querySelectorAll(".prop-chip"),
        function (c) { return c.textContent; });
      rows.push({ key: p.getAttribute("data-key"), values: vals });
    });
    return rows;
  }

  var editor = null;

  function buildEditor() {
    var form = el("div", "props-editor");
    var list = el("div", "pe-list");
    form.appendChild(list);

    function addRow(key, values) {
      var row = el("div", "pe-row");
      var k = el("input", "pe-key"); k.type = "text"; k.placeholder = "속성"; k.value = key || "";
      k.setAttribute("aria-label", "속성 이름");
      var v = el("input", "pe-val"); v.type = "text"; v.placeholder = "값 (여러 개는 쉼표로)";
      v.value = values.join(", "); v.setAttribute("aria-label", "속성 값");
      var rm = el("button", "pe-rm", "×"); rm.type = "button";
      rm.title = "이 속성 삭제"; rm.setAttribute("aria-label", "이 속성 삭제");
      rm.addEventListener("click", function () { row.remove(); });
      row.appendChild(k); row.appendChild(v); row.appendChild(rm);
      list.appendChild(row);
      return row;
    }

    readCurrent().forEach(function (r) { addRow(r.key, r.values); });

    var addBtn = el("button", "pe-add", "+ 속성 추가"); addBtn.type = "button";
    addBtn.addEventListener("click", function () { addRow("", []).querySelector(".pe-key").focus(); });

    var actions = el("div", "pe-actions");
    var save = el("button", "pe-save primary", "저장"); save.type = "button";
    var cancel = el("button", "pe-cancel", "취소"); cancel.type = "button";
    actions.appendChild(save); actions.appendChild(cancel);
    form.appendChild(addBtn); form.appendChild(actions);

    cancel.addEventListener("click", showRead);
    save.addEventListener("click", function () {
      var properties = [];
      list.querySelectorAll(".pe-row").forEach(function (row) {
        var key = row.querySelector(".pe-key").value.trim();
        if (key) properties.push({ key: key, values: row.querySelector(".pe-val").value });
      });
      save.disabled = true;
      fetch("/api/doc/" + enc(path) + "/properties", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": W.csrf },
        credentials: "same-origin",
        body: JSON.stringify({ base_version: Number(version), properties: properties }),
      }).then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
        .then(function (res) {
          if (res.ok && res.d && res.d.ok) { location.reload(); }
          else { save.disabled = false; window.alert("저장 실패: " + msg(res.d)); }
        }).catch(function () { save.disabled = false; window.alert("저장 실패"); });
    });
    return form;
  }

  function showEdit() {
    if (editor) return;
    editor = buildEditor();
    wrap.appendChild(editor);
    wrap.classList.add("editing");
    var first = editor.querySelector(".pe-key");
    if (first) first.focus();
  }
  function showRead() {
    if (editor) { editor.remove(); editor = null; }
    wrap.classList.remove("editing");
  }

  document.addEventListener("click", function (e) {
    if (e.target.closest('[data-action="edit-props"]')) { e.preventDefault(); showEdit(); }
  });
})();
