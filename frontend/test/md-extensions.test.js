import MarkdownIt from "markdown-it";
import { describe, expect, test } from "vitest";
import { installWikiExtensions } from "../src/md-extensions.js";

function renderer(options) {
  const md = new MarkdownIt({ html: true });
  installWikiExtensions(md, options);
  return md;
}

function ruleHarness() {
  const rules = {};
  class Token {
    constructor(type) { this.type = type; }
  }
  const md = {
    inline: { ruler: { before(_before, name, rule) { rules[name] = rule; } } },
    core: { ruler: { push(name, rule) { rules[name] = rule; } } },
    block: { ruler: { before(_before, name, rule) { rules[name] = rule; } } },
    renderer: { rules: {} },
    utils: { escapeHtml: (value) => value },
    parseInline: () => [{ children: [] }],
    Token,
  };
  installWikiExtensions(md);
  return { md, rules };
}

describe("wiki markdown extensions", () => {
  test("renders wikilinks, aliases, anchors, and a custom resolver safely", () => {
    const md = renderer({ goBase: "/resolve?q=" });
    expect(md.renderInline("[[문서]] [[문서#절|표시 <값>]]")).toBe(
      '<a href="/resolve?q=%EB%AC%B8%EC%84%9C" class="wikilink">문서</a> ' +
      '<a href="/resolve?q=%EB%AC%B8%EC%84%9C" class="wikilink">표시 &lt;값&gt;</a>',
    );
    expect(md.renderInline("[[문서# 절 ]]"))
      .toContain('class="wikilink">문서#절</a>');
  });

  test("leaves malformed wikilinks and code literals untouched", () => {
    const md = renderer();
    expect(md.renderInline("[x] [[]] [[#section]] [[open"))
      .toContain("[x] [[]] [[#section]] [[open");
    expect(md.render("`[[code]]`\n\n```\n[[fence]]\n```"))
      .not.toContain('class="wikilink"');
  });

  test("renders embed placeholders with note and heading labels", () => {
    const md = renderer();
    expect(md.renderInline("![[노트|별명]] ![[노트# 제목]]")).toBe(
      '<span class="embed-placeholder"><a href="/go?target=%EB%85%B8%ED%8A%B8" class="embed-title">노트</a>' +
      '<span class="embed-note">임베드</span></span> ' +
      '<span class="embed-placeholder"><a href="/go?target=%EB%85%B8%ED%8A%B8" class="embed-title">노트 › 제목</a>' +
      '<span class="embed-note">임베드</span></span>',
    );
  });

  test("leaves malformed embeds and embeds in code untouched", () => {
    const md = renderer();
    expect(md.renderInline("!x ![[]] ![[open")).toContain("!x ![[]] ![[open");
    expect(md.renderInline("`![[code]]`"))
      .not.toContain("embed-placeholder");
  });

  test("renders known, aliased, custom, and nested callouts with body markup", () => {
    const md = renderer();
    const tip = md.render("> [!tip]\n> **본문**");
    expect(tip).toContain('<div class="callout-title">팁</div>');
    expect(tip).toContain("<strong>본문</strong>");
    expect(md.render("> [!hint] 직접 제목\n> 본문"))
      .toContain('<div class="callout-title">직접 제목</div>');
    expect(md.render("> [!custom]\n> body"))
      .toContain('<div class="callout-title">Custom</div>');
    expect(md.render("> [!note]"))
      .toContain('<div class="callout-title">노트</div>');
    const nested = md.render("> [!note] Outer\n> text\n> > nested");
    expect(nested).toContain('class="callout callout-note"');
    expect(nested).toContain("<blockquote>");
  });

  test("keeps ordinary and incomplete blockquotes ordinary", () => {
    const md = renderer();
    expect(md.render("> ordinary")).toContain("<blockquote>");
    expect(md.render(">\n> blank")).not.toContain("callout-title");
  });

  test("renders valid marks and rejects empty, spaced, or unclosed markers", () => {
    const md = renderer();
    expect(md.renderInline("==강조==")) .toBe("<mark>강조</mark>");
    expect(md.renderInline("==== == spaced== ==spaced == ==open"))
      .not.toContain("<mark>");
    expect(md.renderInline("`==code==`")) .not.toContain("<mark>");
  });

  test("strips frontmatter and renders non-title properties as escaped chips", () => {
    const md = renderer();
    const html = md.render([
      "---",
      "# comment",
      "TITLE: Hidden",
      "tags: [one, two]",
      "status: 'active'",
      "owners: [alice, \"bob\"]",
      "topics:",
      "  - 'frontend'",
      "  - testing",
      "unsafe: '<script>'",
      "empty:",
      "invalid yaml",
      "---",
      "# Body",
    ].join("\n"));
    expect(html).toContain('<dl class="doc-props" aria-label="문서 속성">');
    expect(html).not.toContain("Hidden");
    expect(html).not.toContain("one");
    expect(html).toContain('<dt class="prop-key">status</dt>');
    expect(html).toContain('<span class="prop-chip">alice</span><span class="prop-chip">bob</span>');
    expect(html).toContain("&lt;script&gt;");
    expect(html).not.toContain('<dt class="prop-key">empty</dt>');
    expect(html).toContain("<h1>Body</h1>");
  });

  test("handles empty lists, empty metadata, and non-leading or unclosed fences", () => {
    const md = renderer();
    expect(md.render("---\ntitle: only\ntags: []\n---\nBody"))
      .not.toContain("doc-props");
    expect(md.render("---\nitems: [,]\n---\nBody"))
      .not.toContain("doc-props");
    expect(md.render("Text\n\n---\nkey: value\n---"))
      .not.toContain("doc-props");
    expect(md.render("---\nkey: value"))
      .not.toContain("doc-props");
    expect(md.render("  ---\nkey: value\n  ---"))
      .not.toContain("doc-props");
  });

  test("inline rules recognize valid syntax silently without emitting output", () => {
    const { rules } = ruleHarness();
    for (const [name, src] of [["wikilink", "[[target]]"], ["embed", "![[target]]"], ["mark", "==value=="]]) {
      const state = { src, pos: 0, pushed: [], push(...args) { this.pushed.push(args); return {}; } };
      expect(rules[name](state, true)).toBe(true);
      expect(state.pos).toBe(src.length);
      expect(state.pushed).toEqual([]);
    }
  });

  test("callout core rule ignores incomplete token shapes", () => {
    const { rules } = ruleHarness();
    const shapes = [
      [{ type: "blockquote_open" }],
      [{ type: "blockquote_open" }, { type: "heading_open" }],
      [{ type: "blockquote_open" }, { type: "paragraph_open" }],
      [{ type: "blockquote_open" }, { type: "paragraph_open" }, { type: "text" }],
    ];
    for (const tokens of shapes) {
      rules.callouts({ tokens });
      expect(tokens[0].type).toBe("blockquote_open");
    }
  });

  test("callout core rule keeps an empty body when inline parsing has no children", () => {
    const { md, rules } = ruleHarness();
    md.parseInline = () => [{}];
    const open = {
      type: "blockquote_open",
      tag: "blockquote",
      attrSet(name, value) { this[name] = value; },
    };
    const inline = { type: "inline", content: "[!note]" };
    const close = { type: "blockquote_close", tag: "blockquote" };
    const tokens = [open, { type: "paragraph_open" }, inline, close];
    rules.callouts({ tokens, env: {}, Token: md.Token });
    expect(inline.children).toEqual([]);
    expect(open.class).toBe("callout callout-note");
    expect(close.tag).toBe("div");
  });

  test("frontmatter block rule reports a silent match without consuming lines", () => {
    const { rules } = ruleHarness();
    const state = {
      src: "---\nkey: value\n---",
      sCount: [0, 0, 0],
      bMarks: [0, 4, 15],
      tShift: [0, 0, 0],
      eMarks: [3, 14, 18],
      line: 0,
    };
    expect(rules.frontmatter(state, 0, 3, true)).toBe(true);
    expect(state.line).toBe(0);
  });
});
