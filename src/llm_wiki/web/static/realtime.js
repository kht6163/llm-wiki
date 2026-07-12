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

  function applyRendered(d, ev, requestId) {
    if (requestId !== refreshSeq || !d || !d.ok || !(d.version >= version)) return;
    var rendered = document.querySelector(".rendered");
    if (rendered && d.html != null) rendered.innerHTML = d.html;
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
  }

  function refreshRendered(ev) {
    var requestId = ++refreshSeq;
    fetch("/api/doc/" + encPath(path) + "/rendered", { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (d) { applyRendered(d, ev, requestId); })
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

  var ws = null, backoff = 1000, closed = false, reconnectTimer = null, refreshSeq = 0;
  var softRefreshTimer = null, softRefreshInFlight = false;

  // When the tab becomes visible or the window regains focus, quietly re-check
  // the document version (WS may have been suspended). Debounced to avoid spam.
  function softRefreshCheck() {
    if (closed || mode === "list" || !path || softRefreshInFlight) return;
    softRefreshInFlight = true;
    var requestId = ++refreshSeq;
    fetch("/api/doc/" + encPath(path) + "/rendered", { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d || !d.ok || !(d.version > version)) return;
        var ev = {
          type: "doc_changed",
          op: "update",
          path: path,
          version: d.version,
          via: d.last_via,
          updated_by: d.updated_by,
        };
        if (mode === "edit") {
          var who = whoVia(ev) ? " · " + esc(whoVia(ev)) : "";
          banner("⚠ 다른 곳에서 이 문서가 변경되었습니다 (v" + esc(d.version) + who +
                 "). 지금 저장하면 충돌로 거부될 수 있습니다. 변경분을 합치려면 다시 여세요.", "warn");
        } else {
          // Apply the payload we already have — no second round-trip.
          applyRendered(d, ev, requestId);
        }
      })
      .catch(function () {})
      .then(function () { softRefreshInFlight = false; });
  }

  function scheduleSoftRefresh() {
    if (closed || mode === "list" || !path) return;
    if (document.visibilityState && document.visibilityState !== "visible") return;
    if (softRefreshTimer !== null) clearTimeout(softRefreshTimer);
    softRefreshTimer = setTimeout(function () {
      softRefreshTimer = null;
      softRefreshCheck();
    }, 2000);
  }

  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") scheduleSoftRefresh();
  });
  window.addEventListener("focus", scheduleSoftRefresh);

  function connect() {
    if (closed) return;
    var proto = location.protocol === "https:" ? "wss:" : "ws:";
    var socket;
    try {
      socket = new WebSocket(proto + "//" + location.host + "/ws");
      ws = socket;
    } catch (e) { return; }
    socket.onopen = function () { if (socket === ws) backoff = 1000; };
    socket.onmessage = function (m) {
      if (socket !== ws) return;
      try { onEvent(JSON.parse(m.data)); } catch (e) { /* ignore malformed */ }
    };
    socket.onerror = function () { try { socket.close(); } catch (e) {} };
    socket.onclose = function () {
      if (closed || socket !== ws) return;
      reconnectTimer = setTimeout(function () { reconnectTimer = null; connect(); }, backoff);
      backoff = Math.min(backoff * 2, 30000); // exponential backoff, capped
    };
  }
  window.addEventListener("beforeunload", function () {
    closed = true;
    if (reconnectTimer !== null) { clearTimeout(reconnectTimer); reconnectTimer = null; }
    if (softRefreshTimer !== null) { clearTimeout(softRefreshTimer); softRefreshTimer = null; }
    if (ws) { try { ws.close(); } catch (e) {} }
  });
  connect();
})();
