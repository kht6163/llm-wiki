// Document-view controls for revocable, time-bound public read links.
(function () {
  "use strict";
  var toggle = document.getElementById("share-toggle");
  var panel = document.getElementById("share-panel");
  if (!toggle || !panel) return;

  var generate = document.getElementById("share-generate");
  var result = document.getElementById("share-result");
  var input = document.getElementById("share-url");
  var copy = document.getElementById("share-copy");
  var status = document.getElementById("share-status");
  var refresh = document.getElementById("share-refresh");
  var list = document.getElementById("share-links");
  var empty = document.getElementById("share-list-empty");
  var W = window.WIKI || { csrf: "" };
  var currentLinks = [];
  var loaded = false;

  function encodedPath() {
    return panel.getAttribute("data-share-path").split("/").map(encodeURIComponent).join("/");
  }

  function setStatus(message, failed) {
    status.textContent = message;
    status.classList.toggle("share-error", Boolean(failed));
  }

  function errorMessage(error, fallback) {
    return error && error.message ? error.message : fallback;
  }

  async function requestJson(url, method) {
    var options = { credentials: "same-origin" };
    if (method === "POST") {
      var body = new URLSearchParams();
      body.set("csrf_token", W.csrf);
      options.method = "POST";
      options.headers = { "X-CSRF-Token": W.csrf };
      options.body = body;
    }
    var response = await fetch(url, options);
    var data = await response.json();
    if (!response.ok || !data.ok) {
      var detail = data && data.error && (data.error.message || data.error);
      throw new Error(detail || "요청을 처리하지 못했습니다.");
    }
    return data;
  }

  function linkState(link) {
    if (link.revoked_at) return { key: "revoked", label: "취소" };
    var expiry = Date.parse(link.expires_at);
    if (Number.isFinite(expiry) && expiry <= Date.now()) return { key: "expired", label: "만료" };
    return { key: "active", label: "활성" };
  }

  function addTime(parent, label, iso) {
    var wrapper = document.createElement("span");
    wrapper.append(label + " ");
    var time = document.createElement("time");
    time.className = "dt";
    time.dateTime = iso;
    time.textContent = iso;
    wrapper.appendChild(time);
    parent.appendChild(wrapper);
  }

  function renderLinks(links) {
    list.replaceChildren();
    empty.hidden = links.length !== 0;
    links.forEach(function (link) {
      var state = linkState(link);
      var item = document.createElement("li");
      item.className = "share-link-row";
      item.dataset.linkId = String(link.id);

      var heading = document.createElement("div");
      heading.className = "share-link-heading";
      var name = document.createElement("strong");
      name.textContent = "링크 #" + link.id;
      var badge = document.createElement("span");
      badge.className = "share-state share-state-" + state.key;
      badge.textContent = state.label;
      heading.append(name, badge);
      item.appendChild(heading);

      var metadata = document.createElement("div");
      metadata.className = "share-link-meta";
      addTime(metadata, "발급", link.created_at);
      addTime(metadata, "만료", link.expires_at);
      var creator = document.createElement("span");
      creator.textContent = "발급자 " + (link.created_by_name || "알 수 없음");
      metadata.appendChild(creator);
      if (link.last_used_at) addTime(metadata, "마지막 사용", link.last_used_at);
      item.appendChild(metadata);

      if (state.key === "active") {
        var revoke = document.createElement("button");
        revoke.type = "button";
        revoke.className = "share-revoke danger";
        revoke.dataset.linkId = String(link.id);
        revoke.textContent = "취소";
        revoke.setAttribute("aria-label", "공개 링크 #" + link.id + " 취소");
        item.appendChild(revoke);
      }
      list.appendChild(item);
    });
    if (window.WikiLocalizeTime) window.WikiLocalizeTime(list);
  }

  async function loadLinks() {
    refresh.disabled = true;
    list.setAttribute("aria-busy", "true");
    try {
      var data = await requestJson("/api/doc/" + encodedPath() + "/shares", "GET");
      currentLinks = data.links || [];
      renderLinks(currentLinks);
      loaded = true;
    } catch (error) {
      setStatus(errorMessage(error, "발급 내역을 불러오지 못했습니다."), true);
    } finally {
      refresh.disabled = false;
      list.removeAttribute("aria-busy");
    }
  }

  function closePanel() {
    panel.hidden = true;
    toggle.setAttribute("aria-expanded", "false");
    toggle.focus();
  }

  toggle.addEventListener("click", function () {
    var opening = panel.hidden;
    panel.hidden = !opening;
    toggle.setAttribute("aria-expanded", opening ? "true" : "false");
    if (opening) {
      generate.focus();
      if (!loaded) loadLinks();
    }
  });

  panel.addEventListener("keydown", function (event) {
    if (event.key === "Escape") {
      event.preventDefault();
      closePanel();
    }
  });

  refresh.addEventListener("click", loadLinks);

  generate.addEventListener("click", async function () {
    generate.disabled = true;
    generate.setAttribute("aria-busy", "true");
    setStatus("공개 링크를 만드는 중입니다.", false);
    try {
      var data = await requestJson("/api/doc/" + encodedPath() + "/share", "POST");
      input.value = data.url;
      result.hidden = false;
      if (data.link) {
        currentLinks = [data.link].concat(currentLinks.filter(function (link) {
          return link.id !== data.link.id;
        }));
        renderLinks(currentLinks);
      }
      setStatus("공개 링크를 만들었습니다. 30일 후 만료됩니다.", false);
    } catch (error) {
      setStatus(errorMessage(error, "공유 링크를 만들지 못했습니다."), true);
    } finally {
      generate.disabled = false;
      generate.removeAttribute("aria-busy");
    }
  });

  list.addEventListener("click", async function (event) {
    var button = event.target.closest && event.target.closest(".share-revoke");
    if (!button) return;
    var id = Number(button.dataset.linkId);
    var link = currentLinks.find(function (item) { return item.id === id; });
    if (!link) return;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    try {
      var data = await requestJson("/api/shares/" + id + "/revoke", "POST");
      link.revoked_at = data.revoked_at;
      renderLinks(currentLinks);
      setStatus("공개 링크 #" + id + "을 취소했습니다.", false);
    } catch (error) {
      setStatus(errorMessage(error, "공개 링크를 취소하지 못했습니다."), true);
      button.disabled = false;
      button.removeAttribute("aria-busy");
    }
  });

  copy.addEventListener("click", async function () {
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(input.value);
      } else {
        input.focus();
        input.select();
        if (!document.execCommand("copy")) throw new Error("copy failed");
      }
      setStatus("공개 링크를 복사했습니다.", false);
    } catch (_) {
      input.focus();
      input.select();
      setStatus("자동 복사하지 못했습니다. 선택한 링크를 직접 복사하세요.", true);
    }
  });
})();
