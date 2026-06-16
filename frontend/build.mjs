// Build the markdown editor into a single self-contained IIFE bundle (+ one CSS
// file) under web/static/vendor/. React, md-editor-rt and all their deps are
// inlined; fonts/icons are embedded as data URIs so the result works fully
// offline and is served like any other vendored asset. Run with: npm run build.
import * as esbuild from "esbuild";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const outdir = path.resolve(here, "../src/llm_wiki/web/static/vendor");

await esbuild.build({
  entryPoints: [path.join(here, "src/entry.jsx")],
  bundle: true,
  format: "iife",
  minify: true,
  sourcemap: false,
  target: ["es2020", "chrome100", "firefox100", "safari15"],
  define: { "process.env.NODE_ENV": '"production"' },
  loader: {
    ".js": "jsx",
    ".jsx": "jsx",
    // Embed any font/image assets referenced by md-editor-rt's CSS so the bundle
    // stays self-contained (no extra files to vendor, fully offline).
    ".ttf": "dataurl",
    ".woff": "dataurl",
    ".woff2": "dataurl",
    ".eot": "dataurl",
    ".svg": "dataurl",
    ".png": "dataurl",
    ".gif": "dataurl",
  },
  outfile: path.join(outdir, "md-editor.bundle.js"),
});

console.log("built -> web/static/vendor/md-editor.bundle.{js,css}");
