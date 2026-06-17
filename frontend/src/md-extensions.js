// markdown-it rules that bring md-editor-rt's live preview in line with the
// server renderer (markdown_render.py): [[wikilinks]], Obsidian callouts, and
// ==highlight==. Task lists and code highlighting are handled by md-editor-rt
// itself, so they're not re-implemented here.

// Obsidian callout types -> Korean default titles (must match the server).
const CALLOUT_TITLES = {
  note: "노트", info: "정보", tip: "팁", hint: "팁", important: "중요",
  success: "성공", check: "성공", done: "성공", question: "질문", faq: "질문",
  warning: "경고", caution: "주의", attention: "주의", danger: "위험",
  error: "오류", bug: "버그", quote: "인용", example: "예시", abstract: "요약",
};

// [[target]] / [[target|alias]] / [[target#anchor]] -> link to the /go resolver.
// An inline rule never fires inside code spans/fences, so code stays literal.
function wikilink(md, goBase) {
  md.inline.ruler.before("link", "wikilink", function (state, silent) {
    const src = state.src, start = state.pos;
    if (src.charCodeAt(start) !== 0x5b || src.charCodeAt(start + 1) !== 0x5b) return false; // [[
    const end = src.indexOf("]]", start + 2);
    if (end < 0) return false;
    const inner = src.slice(start + 2, end).trim();
    if (!inner) return false;
    if (!silent) {
      let linkpart = inner, alias = null;
      const bar = inner.indexOf("|");
      if (bar >= 0) { linkpart = inner.slice(0, bar); alias = inner.slice(bar + 1); }
      let target = linkpart, anchor = null;
      const h = linkpart.indexOf("#");
      if (h >= 0) { target = linkpart.slice(0, h); anchor = linkpart.slice(h + 1); }
      target = target.trim();
      if (!target) return false;
      const label = (alias && alias.trim()) || (target + (anchor ? "#" + anchor.trim() : ""));
      const href = goBase + encodeURIComponent(target);
      const tok = state.push("html_inline", "", 0);
      tok.content = '<a href="' + md.utils.escapeHtml(href) + '" class="wikilink">' +
        md.utils.escapeHtml(label) + "</a>";
    }
    state.pos = end + 2;
    return true;
  });
}

// ![[target]] / ![[target#heading]] -> an embed placeholder chip. The editor preview
// has no vault access to expand the target (the server reading view does that), so it
// shows a labelled chip linking to the note instead of the transcluded content.
function embed(md, goBase) {
  md.inline.ruler.before("image", "embed", function (state, silent) {
    const src = state.src, start = state.pos;
    if (src.charCodeAt(start) !== 0x21 || src.charCodeAt(start + 1) !== 0x5b ||
        src.charCodeAt(start + 2) !== 0x5b) return false; // ![[
    const end = src.indexOf("]]", start + 3);
    if (end < 0) return false;
    const inner = src.slice(start + 3, end).trim();
    if (!inner) return false;
    if (!silent) {
      let linkpart = inner.split("|")[0].trim();
      let target = linkpart, anchor = null;
      const h = linkpart.indexOf("#");
      if (h >= 0) { target = linkpart.slice(0, h).trim(); anchor = linkpart.slice(h + 1).trim(); }
      const label = target + (anchor ? " › " + anchor : "");
      const href = goBase + encodeURIComponent(target);
      const tok = state.push("html_inline", "", 0);
      tok.content = '<span class="embed-placeholder"><a href="' + md.utils.escapeHtml(href) +
        '" class="embed-title">' + md.utils.escapeHtml(label) + "</a>" +
        '<span class="embed-note">임베드</span></span>';
    }
    state.pos = end + 2;
    return true;
  });
}

// > [!type] title  ->  <div class="callout callout-type"><div class="callout-title">…
function callouts(md) {
  md.core.ruler.push("callouts", function (state) {
    const toks = state.tokens;
    for (let i = 0; i < toks.length; i++) {
      if (toks[i].type !== "blockquote_open") continue;
      const para = toks[i + 1], inline = toks[i + 2];
      if (!para || para.type !== "paragraph_open" || !inline || inline.type !== "inline") continue;
      const full = inline.content;
      const nl = full.indexOf("\n");
      const firstLine = nl >= 0 ? full.slice(0, nl) : full;
      const rest = nl >= 0 ? full.slice(nl + 1) : "";
      const m = /^\[!([A-Za-z]+)\][ \t]*(.*)$/.exec(firstLine);
      if (!m) continue;
      const type = m[1].toLowerCase();
      const title = m[2].trim() || CALLOUT_TITLES[type] || (type.charAt(0).toUpperCase() + type.slice(1));

      toks[i].tag = "div";
      toks[i].attrSet("class", "callout callout-" + type);
      let depth = 1;
      for (let j = i + 1; j < toks.length; j++) {
        if (toks[j].type === "blockquote_open") depth++;
        else if (toks[j].type === "blockquote_close") { depth--; if (!depth) { toks[j].tag = "div"; break; } }
      }
      // The first line was the title; keep only the remaining lines as the body.
      inline.content = rest;
      const reparsed = md.parseInline(rest, state.env);
      inline.children = (reparsed[0] && reparsed[0].children) || [];

      const titleTok = new state.Token("html_block", "", 0);
      titleTok.content = '<div class="callout-title">' + md.utils.escapeHtml(title) + "</div>";
      toks.splice(i + 1, 0, titleTok);
    }
  });
}

// ==text== -> <mark>text</mark> (mirrors the server's highlight pass).
function mark(md) {
  md.inline.ruler.before("emphasis", "mark", function (state, silent) {
    const src = state.src, start = state.pos;
    if (src.charCodeAt(start) !== 0x3d || src.charCodeAt(start + 1) !== 0x3d) return false; // ==
    const end = src.indexOf("==", start + 2);
    if (end < 0 || end === start + 2) return false;
    const inner = src.slice(start + 2, end);
    if (/^\s|\s$/.test(inner)) return false; // require non-space at both ends
    if (!silent) {
      state.push("mark_open", "mark", 1);
      const t = state.push("text", "", 0); t.content = inner;
      state.push("mark_close", "mark", -1);
    }
    state.pos = end + 2;
    return true;
  });
}

// Minimal frontmatter YAML parser mirroring markdown_utils._parse_simple_yaml:
// scalar `key: value`, inline `[a, b]` lists, block `- item` lists; keys lowercased.
function parseSimpleYaml(raw) {
  const meta = {};
  const lines = raw.split("\n");
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim() || line.trimStart().startsWith("#")) { i++; continue; }
    const m = /^([A-Za-z0-9_-]+):[ \t]*(.*)$/.exec(line);
    if (!m) { i++; continue; }
    const key = m[1].trim().toLowerCase();
    const val = m[2].trim();
    if (val === "") {
      const items = [];
      let j = i + 1;
      while (j < lines.length && /^[ \t]*-[ \t]+/.test(lines[j])) {
        items.push(lines[j].replace(/^[ \t]*-[ \t]+/, "").trim().replace(/^['"]|['"]$/g, ""));
        j++;
      }
      if (items.length) { meta[key] = items; i = j; continue; }
      meta[key] = ""; i++; continue;
    }
    if (val.startsWith("[") && val.endsWith("]")) {
      meta[key] = val.slice(1, -1).split(",").map((x) => x.trim().replace(/^['"]|['"]$/g, "")).filter(Boolean);
    } else {
      meta[key] = val.replace(/^['"]|['"]$/g, "");
    }
    i++;
  }
  return meta;
}

// Keys omitted from the Properties panel (title = the h1, tags = the doc-meta chips),
// matching markdown_utils.document_properties on the server.
const PROPS_OMIT = new Set(["title", "tags"]);

// Render parsed frontmatter as the SAME .doc-props panel the server emits, so the
// live preview matches the reading view. Returns "" when there's nothing to show.
function renderProps(md, meta) {
  const rows = [];
  for (const key of Object.keys(meta)) {
    if (PROPS_OMIT.has(key)) continue;
    const vals = (Array.isArray(meta[key]) ? meta[key] : [meta[key]]).map((v) => String(v).trim()).filter(Boolean);
    if (!vals.length) continue;
    const chips = vals.map((v) => '<span class="prop-chip">' + md.utils.escapeHtml(v) + "</span>").join("");
    rows.push('<div class="prop"><dt class="prop-key">' + md.utils.escapeHtml(key) +
      '</dt><dd class="prop-val">' + chips + "</dd></div>");
  }
  return rows.length ? '<dl class="doc-props" aria-label="문서 속성">' + rows.join("") + "</dl>" : "";
}

// Consume a leading `---\n…\n---` YAML block at line 0 and re-surface it as the
// Properties panel — mirrors render_markdown()'s strip so the preview never shows
// the raw frontmatter as a setext heading.
function frontmatter(md) {
  md.block.ruler.before("hr", "frontmatter", function (state, startLine, endLine, silent) {
    if (startLine !== 0 || state.sCount[startLine] !== 0) return false;
    const begin = state.bMarks[startLine] + state.tShift[startLine];
    if (state.src.slice(begin, state.eMarks[startLine]).trim() !== "---") return false;
    let nextLine = startLine + 1;
    let found = false;
    for (; nextLine < endLine; nextLine++) {
      const p = state.bMarks[nextLine] + state.tShift[nextLine];
      if (state.sCount[nextLine] === 0 && state.src.slice(p, state.eMarks[nextLine]).trim() === "---") { found = true; break; }
    }
    if (!found) return false;
    if (silent) return true;
    const yaml = state.getLines(startLine + 1, nextLine, 0, false);
    state.line = nextLine + 1;
    const tok = state.push("frontmatter", "", 0);
    tok.map = [startLine, state.line];
    tok.meta = yaml;
    tok.block = true;
    return true;
  });
  md.renderer.rules.frontmatter = function (tokens, idx) {
    return renderProps(md, parseSimpleYaml(tokens[idx].meta));
  };
}

export function installWikiExtensions(md, opts) {
  opts = opts || {};
  frontmatter(md);
  embed(md, opts.goBase || "/go?target=");
  wikilink(md, opts.goBase || "/go?target=");
  callouts(md);
  mark(md);
}
