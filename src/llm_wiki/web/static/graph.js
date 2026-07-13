// Link-graph rendering, filtering, responsive node sizing and keyboard navigation.
(function () {
  "use strict";
  var canvas = document.getElementById("cy");
  if (!canvas) return;

  var cy = null;
  var kbIndex = -1;

  // Pull live theme colours from CSS tokens so light/dark changes do not rebuild
  // the graph or discard its current layout.
  function graphStyle() {
    var cs = getComputedStyle(document.documentElement);
    function value(name, fallback) { return cs.getPropertyValue(name).trim() || fallback; }
    var accent = value("--accent", "#2563eb");
    var ink = value("--fg", "#1c2128");
    var faint = value("--fg-faint", "#636b75");
    var line = value("--line", "#d5dbe2");
    var lineStrong = value("--line-strong", "#c2cad4");
    var inset = value("--inset", "#e9edf2");
    return [
      { selector: "node", style: { label: "data(label)", "font-size": "9px", "background-color": accent, width: 12, height: 12, color: ink,
        "text-valign": "bottom", "text-halign": "center", "text-wrap": "wrap" } },
      { selector: "node[?root]", style: { "background-color": ink, width: 18, height: 18, "font-size": "11px" } },
      { selector: "node[!exists]", style: { "background-color": inset, "border-width": 1, "border-style": "dashed", "border-color": lineStrong, color: faint } },
      { selector: "node:selected", style: { "border-width": 3, "border-style": "solid", "border-color": accent, "overlay-color": accent, "overlay-opacity": 0.12 } },
      { selector: "edge", style: { width: 1, "line-color": lineStrong, "target-arrow-color": lineStrong, "target-arrow-shape": "triangle", "curve-style": "bezier", "arrow-scale": 0.6 } },
      { selector: "edge[!resolved]", style: { "line-style": "dashed", "line-color": line, "target-arrow-color": line } }
    ];
  }

  // Target sizes are screen pixels; sparse graphs get larger nodes while dense
  // graphs stay legible without turning into a solid cluster.
  function nodeMetrics() {
    var count = Math.max(1, cy.nodes().length);
    var nodePx = Math.max(18, Math.min(64, 150 / Math.sqrt(count)));
    return { nodePx: nodePx, rootPx: nodePx * 1.5,
      fontPx: Math.max(12, Math.min(22, Math.round(nodePx * 0.42))) };
  }

  function applyNodeSizes(zoom, metrics) {
    cy.batch(function () {
      cy.nodes().forEach(function (node) {
        var diameter = (node.data("root") ? metrics.rootPx : metrics.nodePx) / zoom;
        node.style({ width: diameter, height: diameter,
          "font-size": (metrics.fontPx / zoom) + "px",
          "text-margin-y": 4 / zoom,
          "text-max-width": (metrics.nodePx * 2.4 / zoom) + "px" });
      });
    });
  }

  function fitAndSize() {
    if (!cy || cy.nodes().empty()) return;
    cy.resize();
    var metrics = nodeMetrics();
    cy.fit(cy.elements(), Math.round(metrics.rootPx * 0.5 + metrics.fontPx + 14));
    applyNodeSizes(cy.zoom() || 1, metrics);
  }

  function openNode(node) {
    if (node && node.data("exists")) {
      window.location.href = "/doc/" + encodeURIComponent(node.data("id"));
      return true;
    }
    return false;
  }

  async function reloadGraph() {
    var root = document.getElementById("root").value.trim();
    var depth = document.getElementById("depth").value;
    var folder = document.getElementById("folder").value.trim();
    var tag = document.getElementById("tag").value.trim();
    var includeUnresolved = document.getElementById("include-unresolved").checked;
    var info = document.getElementById("ginfo");
    var empty = document.getElementById("gempty");
    info.textContent = "불러오는 중…";
    info.classList.add("is-loading");
    info.setAttribute("aria-busy", "true");
    var url = "/api/graph?depth=" + encodeURIComponent(depth) + "&limit=500" +
      "&include_unresolved=" + (includeUnresolved ? "true" : "false");
    if (root) url += "&root=" + encodeURIComponent(root);
    if (folder) url += "&folder=" + encodeURIComponent(folder);
    if (tag) url += "&tag=" + encodeURIComponent(tag);

    var data;
    try {
      data = await (await fetch(url, { credentials: "same-origin" })).json();
    } catch (_) {
      info.classList.remove("is-loading");
      info.removeAttribute("aria-busy");
      info.textContent = "그래프를 불러오지 못했습니다.";
      return;
    }
    empty.hidden = data.nodes.length > 0;
    info.classList.remove("is-loading");
    info.removeAttribute("aria-busy");
    info.textContent = data.nodes.length + " 노드 / " + data.edges.length + " 엣지" +
      (data.truncated ? " (일부 생략됨)" : "");

    var elements = [];
    data.nodes.forEach(function (node) {
      elements.push({ data: { id: node.id, label: node.label, exists: node.exists, root: node.is_root } });
    });
    data.edges.forEach(function (edge) {
      elements.push({ data: { id: edge.id, source: edge.source, target: edge.target, resolved: edge.resolved } });
    });
    if (cy) cy.destroy();
    kbIndex = -1;
    cy = window.cytoscape({
      container: canvas,
      elements: elements,
      style: graphStyle(),
      minZoom: 0.1,
      maxZoom: 8
    });
    cy.on("tap", "node", function (event) { openNode(event.target); });

    var metrics = nodeMetrics();
    applyNodeSizes(1, metrics);
    var layout = cy.layout({ name: "cose", animate: false, fit: false,
      idealEdgeLength: Math.round(metrics.nodePx * 2.8), nodeRepulsion: 24000,
      nodeOverlap: Math.round(metrics.nodePx * 1.2), gravity: 0.3,
      componentSpacing: Math.round(metrics.nodePx * 3), padding: 30 });
    layout.one("layoutstop", fitAndSize);
    layout.run();
  }

  function applyGraphTheme() {
    if (cy) cy.style(graphStyle());
  }

  new MutationObserver(applyGraphTheme).observe(document.documentElement, {
    attributes: true,
    attributeFilter: ["data-theme"]
  });
  if (window.matchMedia) {
    var scheme = window.matchMedia("(prefers-color-scheme: dark)");
    if (scheme.addEventListener) scheme.addEventListener("change", applyGraphTheme);
    else if (scheme.addListener) scheme.addListener(applyGraphTheme);
  }

  function keyboardNodes() {
    return cy ? cy.nodes().sort(function (a, b) {
      return a.data("id").localeCompare(b.data("id"));
    }) : [];
  }

  function keyboardFocus(delta) {
    var nodes = keyboardNodes();
    if (!nodes.length) return;
    kbIndex = ((kbIndex + delta) % nodes.length + nodes.length) % nodes.length;
    var node = nodes[kbIndex];
    cy.nodes().unselect();
    node.select();
    cy.center(node);
    var live = document.getElementById("gkb");
    if (live) live.textContent = node.data("label") +
      (node.data("exists") ? "" : " (없는 문서)") + " — " + (kbIndex + 1) + "/" + nodes.length;
  }

  canvas.addEventListener("keydown", function (event) {
    if (event.key === "ArrowRight" || event.key === "ArrowDown") {
      event.preventDefault();
      keyboardFocus(1);
    } else if (event.key === "ArrowLeft" || event.key === "ArrowUp") {
      event.preventDefault();
      keyboardFocus(-1);
    } else if (event.key === "Home") {
      event.preventDefault();
      kbIndex = -1;
      keyboardFocus(1);
    } else if (event.key === "End") {
      event.preventDefault();
      kbIndex = 0;
      keyboardFocus(-1);
    } else if (event.key === "Enter") {
      var nodes = keyboardNodes();
      var node = nodes[kbIndex] || null;
      if (openNode(node)) event.preventDefault();
    }
  });

  document.getElementById("graph-apply").addEventListener("click", reloadGraph);
  ["root", "depth", "folder", "tag"].forEach(function (id) {
    document.getElementById(id).addEventListener("keydown", function (event) {
      if (event.key === "Enter") {
        event.preventDefault();
        reloadGraph();
      }
    });
  });

  // A small test/debug surface also makes the graph state inspectable without
  // reaching into Cytoscape internals from the page.
  window.WikiGraph = {
    reload: reloadGraph,
    style: graphStyle,
    fit: fitAndSize,
    keyboardFocus: keyboardFocus,
    openNode: openNode
  };
  reloadGraph();
})();
