// Reading-page code highlighter. Uses the SAME highlight.js the editor bundles,
// so colours match exactly. The server still emits plain <pre><code> — this only
// adds token <span>s in the browser (no effect on stored/sanitized HTML).
import hljs from "highlight.js/lib/common";

function highlight() {
  document.querySelectorAll(".rendered pre code:not([data-highlighted])").forEach(function (el) {
    try { hljs.highlightElement(el); } catch (e) { /* unknown language etc. — leave plain */ }
  });
}

function run() { highlight(); }

if (document.readyState !== "loading") run();
else document.addEventListener("DOMContentLoaded", run);

// The live viewer swaps the rendered body on change; re-highlight new blocks.
// highlightElement marks elements (data-highlighted), so the :not() filter keeps
// the observer from re-processing its own span insertions.
var target = document.getElementById("doc-rendered");
if (target) new MutationObserver(function () { run(); }).observe(target, { childList: true, subtree: true });

window.WikiHljs = run;
