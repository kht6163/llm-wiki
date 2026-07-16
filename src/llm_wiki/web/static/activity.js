// Lazy-load the per-document activity timeline in the right panel's 활동 tab.
// Fetched after paint (like related.js) so the document view stays on the
// critical path free of audit scans. Renders via badges + mono timestamps.
(function () {
  "use strict";

  var ACTION_LABELS = {
    doc_create: "문서 생성",
    doc_update: "문서 수정",
    doc_move: "문서 이동",
    doc_delete: "문서 삭제",
    doc_restore: "문서 복원",
    doc_purge: "완전 삭제",
    doc_reconcile: "외부 동기화",
    attachment_upload: "첨부 업로드",
    share_mint: "공유 링크 발급",
    share_revoke: "공유 링크 폐기",
  };

  var VIA_LABELS = { web: "사람", mcp: "에이전트", cli: "CLI" };
  var VIA_TITLES = {
    web: "사람이 웹에서 편집",
    mcp: "에이전트가 MCP로 편집",
    cli: "CLI/외부 동기화",
  };

  function enc(p) {
    return p.split("/").map(encodeURIComponent).join("/");
  }

  function viaBadge(via) {
    var span = document.createElement("span");
    if (via && VIA_LABELS[via]) {
      span.className = "via-badge via-" + via;
      span.title = VIA_TITLES[via];
      span.textContent = VIA_LABELS[via];
    } else if (via) {
      span.className = "via-badge";
      span.textContent = via;
    } else {
      return null;
    }
    return span;
  }

  function timeEl(ts) {
    var t = document.createElement("time");
    t.className = "dt";
    if (ts) {
      t.setAttribute("datetime", ts);
      // Cleaned UTC fallback; datetime.js localizes when present.
      t.textContent = String(ts).replace("T", " ").replace(/Z$/, "");
    } else {
      t.textContent = "—";
    }
    return t;
  }

  function renderEmpty(box, message) {
    box.className = "rp-activity muted";
    box.textContent = message || "이 문서의 활동이 없습니다";
  }

  function renderEvents(box, events) {
    box.className = "rp-activity";
    box.textContent = "";
    if (!events.length) {
      renderEmpty(box);
      return;
    }
    var ul = document.createElement("ul");
    ul.className = "rp-activity-list";
    ul.setAttribute("aria-label", "문서 활동");
    events.forEach(function (e) {
      var li = document.createElement("li");
      if (e.via === "mcp") li.className = "via-mcp-row";

      var head = document.createElement("div");
      head.className = "rp-activity-head";
      head.appendChild(timeEl(e.ts));
      var badge = viaBadge(e.via);
      if (badge) head.appendChild(badge);

      var body = document.createElement("div");
      body.className = "rp-activity-body";
      var action = document.createElement("span");
      action.className = "rp-activity-action";
      action.textContent = ACTION_LABELS[e.action] || e.action || "—";
      body.appendChild(action);
      if (e.actor) {
        var actor = document.createElement("span");
        actor.className = "rp-activity-actor muted";
        actor.textContent = e.actor;
        body.appendChild(actor);
      }
      if (e.detail) {
        var detail = document.createElement("span");
        detail.className = "rp-activity-detail muted";
        detail.textContent = e.detail;
        body.appendChild(detail);
      }
      if (e.outcome && e.outcome !== "ok") {
        var out = document.createElement("span");
        out.className = "outcome-bad";
        out.textContent = e.outcome;
        body.appendChild(out);
      }
      // Move targets ("old -> new") stay visible so rename history is legible.
      if (e.target && e.action === "doc_move") {
        var tgt = document.createElement("span");
        tgt.className = "rp-activity-target muted";
        tgt.textContent = e.target;
        body.appendChild(tgt);
      }

      li.appendChild(head);
      li.appendChild(body);
      ul.appendChild(li);
    });
    box.appendChild(ul);
    if (window.WikiLocalizeTime) window.WikiLocalizeTime(box);
  }

  function init() {
    var box = document.getElementById("rp-activity");
    if (!box) return;
    var path = box.getAttribute("data-path");
    if (!path) return;

    box.className = "rp-activity muted is-loading";
    box.setAttribute("aria-busy", "true");
    box.textContent = "활동 불러오는 중…";

    fetch("/api/doc/" + enc(path) + "/activity", {
      headers: { Accept: "application/json" },
      credentials: "same-origin",
    })
      .then(function (r) {
        return r.ok ? r.json() : null;
      })
      .then(function (data) {
        box.removeAttribute("aria-busy");
        if (!data || !data.ok) {
          renderEmpty(box, "활동을 불러오지 못했습니다");
          return;
        }
        renderEvents(box, Array.isArray(data.events) ? data.events : []);
      })
      .catch(function () {
        box.removeAttribute("aria-busy");
        renderEmpty(box, "활동을 불러오지 못했습니다");
      });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
