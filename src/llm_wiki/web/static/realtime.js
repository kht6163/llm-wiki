// Live document-change reflection over a WebSocket. The server pushes
// {type:"doc_changed", op, path, version, ...} events; the viewer swaps rendered
// content in place, the editor warns about a concurrent change before you collide.
(function () {
  "use strict";
  var meta = document.getElementById("rt-meta");
  if (!meta || !("WebSocket" in window)) return;
  var path = meta.getAttribute("data-path");
  var mode = meta.getAttribute("data-mode") || "view"; // "view" | "edit" | "list"
  var version = parseInt(meta.getAttribute("data-version") || "0", 10);
  if (!path && mode !== "list") return;

  function encPath(p) {
    return p.split("/").map(encodeURIComponent).join("/");
  }

  // Escape text before it goes into banner innerHTML. Document paths and usernames
  // are attacker-influenced (paths allow '<'/'>'; usernames are unrestricted), and
  // both are interpolated below — without this they would execute as markup.
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  // Human-readable label for the surface that authored a change.
  function viaLabel(via) {
    return via === "mcp" ? "에이전트" : via === "cli" ? "CLI" : via === "web" ? "사람" : "";
  }

  // "에이전트(alice)" when both are known; falls back to whichever exists.
  function whoVia(ev) {
    var via = viaLabel(ev && ev.via);
    var who = ev && ev.updated_by;
    if (via && who) return via + "(" + who + ")";
    return via || who || "";
  }

  function banner(html, kind) {
    var el = document.getElementById("rt-banner");
    if (!el) {
      el = document.createElement("div");
      el.id = "rt-banner";
      el.setAttribute("role", "status");
      el.setAttribute("aria-live", "polite");
      var main = document.querySelector("main") || document.body;
      main.insertBefore(el, main.firstChild);
    }
    el.className = "rt-banner " + (kind || "");
    el.innerHTML = html;
    el.hidden = false;
  }

  function toast(text) {
    var t = document.createElement("div");
    t.className = "rt-toast";
    t.textContent = text;
    document.body.appendChild(t);
    requestAnimationFrame(function () { t.classList.add("show"); });
    setTimeout(function () {
      t.classList.remove("show");
      setTimeout(function () { t.remove(); }, 400);
    }, 4500);
  }

  function refreshRendered(ev) {
    fetch("/api/doc/" + encPath(path) + "/rendered", { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d || !d.ok) return;
        var rendered = document.querySelector(".rendered");
        if (rendered) rendered.innerHTML = d.html;
        version = d.version;
        meta.setAttribute("data-version", String(d.version));
        // Keep any base_version field (e.g. the delete form) current so it won't
        // false-conflict against the change we just absorbed.
        document.querySelectorAll('input[name="base_version"]').forEach(function (i) {
          i.value = d.version;
        });
        // Prefer the live event's surface attribution; fall back to the fetched doc.
        var who = whoVia(ev) || (d.updated_by || "");
        toast("문서가 v" + d.version + "(으)로 업데이트되었습니다" + (who ? " · " + who : ""));
      })
      .catch(function () {});
  }

  function onEvent(ev) {
    if (!ev || ev.type !== "doc_changed") return;
    if (mode === "list") {
      // Any document change may affect the listing; offer a one-click refresh
      // (shown once) rather than reshuffling rows under the reader.
      banner('문서 목록에 변경이 있습니다. <a href="">새로고침</a>', "");
      return;
    }
    if (ev.path !== path) return;
    if (ev.op === "delete") {
      banner('이 문서가 삭제되었습니다. <a href="/">문서 목록으로</a>', "warn");
      return;
    }
    if (ev.op === "move") {
      var to = ev.to || "";
      banner('이 문서가 <a href="/doc/' + encPath(to) + '">' + esc(to) +
             "</a> (으)로 이동되었습니다.", "warn");
      return;
    }
    // create / update
    if (ev.version && ev.version <= version) return; // stale or our own echo
    if (mode === "edit") {
      var who = whoVia(ev) ? " · " + esc(whoVia(ev)) : "";
      banner("⚠ 다른 곳에서 이 문서가 변경되었습니다 (v" + esc(ev.version) + who +
             "). 지금 저장하면 충돌로 거부될 수 있습니다. 변경분을 합치려면 다시 여세요.", "warn");
    } else {
      refreshRendered(ev);
    }
  }

  var ws = null, backoff = 1000, closed = false;
  function connect() {
    var proto = location.protocol === "https:" ? "wss:" : "ws:";
    try {
      ws = new WebSocket(proto + "//" + location.host + "/ws");
    } catch (e) { return; }
    ws.onopen = function () { backoff = 1000; };
    ws.onmessage = function (m) {
      try { onEvent(JSON.parse(m.data)); } catch (e) { /* ignore malformed */ }
    };
    ws.onerror = function () { try { ws.close(); } catch (e) {} };
    ws.onclose = function () {
      if (closed) return;
      setTimeout(connect, backoff);
      backoff = Math.min(backoff * 2, 30000); // exponential backoff, capped
    };
  }
  window.addEventListener("beforeunload", function () {
    closed = true;
    if (ws) { try { ws.close(); } catch (e) {} }
  });
  connect();
})();
