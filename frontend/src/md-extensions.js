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

export function installWikiExtensions(md, opts) {
  opts = opts || {};
  wikilink(md, opts.goBase || "/go?target=");
  callouts(md);
  mark(md);
}
