// Reading-page Mermaid renderer. Offline IIFE: no CDN. The server emits plain
// <pre><code class="language-mermaid">; this converts those (and bare div.mermaid)
// into render targets and runs mermaid.run. startOnLoad is off — we drive it.
import mermaid from "mermaid";

function themeName() {
  return document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "default";
}

mermaid.initialize({
  startOnLoad: false,
  theme: themeName(),
  securityLevel: "strict",
});

function scopeOf(root) {
  return root && typeof root.querySelectorAll === "function" ? root : document;
}

function collectNodes(root) {
  const scope = scopeOf(root);
  const nodes = [];

  // Fenced ```mermaid blocks from the server renderer.
  scope.querySelectorAll("pre code.language-mermaid").forEach(function (code) {
    if (code.getAttribute("data-mermaid-done")) return;
    code.setAttribute("data-mermaid-done", "1");
    const pre = code.parentElement;
    if (!pre || pre.tagName !== "PRE") return;
    const div = document.createElement("div");
    div.className = "mermaid";
    div.textContent = code.textContent || "";
    div.setAttribute("data-mermaid-pending", "1");
    pre.replaceWith(div);
    nodes.push(div);
  });

  // Optional bare div.mermaid (already in DOM, not yet processed).
  scope.querySelectorAll("div.mermaid:not([data-processed]):not([data-mermaid-pending])").forEach(function (div) {
    div.setAttribute("data-mermaid-pending", "1");
    nodes.push(div);
  });

  return nodes;
}

async function run(root) {
  const nodes = collectNodes(root);
  if (!nodes.length) return;
  try {
    await mermaid.run({ nodes: nodes });
  } catch (e) {
    /* invalid diagram etc. — leave the source / partial SVG */
  }
}

function boot() {
  run(document.getElementById("doc-rendered") || document);
}

if (document.readyState !== "loading") boot();
else document.addEventListener("DOMContentLoaded", boot);

// Live viewer swaps #doc-rendered on change; re-run for new fenced blocks.
// data-mermaid-done / data-mermaid-pending keep the observer from re-processing
// nodes we already queued (replaceWith also fires the observer).
var target = document.getElementById("doc-rendered");
if (target) {
  new MutationObserver(function () {
    run(target);
  }).observe(target, { childList: true, subtree: true });
}

window.WikiMermaid = { run: run };
