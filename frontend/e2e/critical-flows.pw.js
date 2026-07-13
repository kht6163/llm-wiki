import { expect, test } from "@playwright/test";

const USERNAME = "admin";
const PASSWORD = "e2e-secret12";
const BASE_ORIGIN = new URL(process.env.PLAYWRIGHT_BASE_URL).origin;
const externalRequests = new WeakMap();

function guardNetwork(context) {
  if (externalRequests.has(context)) return;
  const seen = [];
  externalRequests.set(context, seen);
  context.on("request", (request) => {
    const url = new URL(request.url());
    if (["http:", "https:", "ws:", "wss:"].includes(url.protocol) && url.origin !== BASE_ORIGIN) {
      seen.push(request.url());
    }
  });
}

function expectNoExternalRequests(context) {
  expect(externalRequests.get(context)).toEqual([]);
}

async function login(page) {
  guardNetwork(page.context());
  await page.goto("/login");
  await page.getByLabel("아이디").fill(USERNAME);
  await page.getByLabel("비밀번호").fill(PASSWORD);
  await page.getByRole("button", { name: "로그인" }).click();
  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByRole("heading", { name: "문서", exact: true })).toBeVisible();
}

async function replaceEditor(page, content) {
  const editor = page.locator(".cm-content");
  await expect(editor).toBeVisible();
  await editor.click();
  await page.keyboard.press("Control+A");
  await page.keyboard.insertText(content);
}

test("로그인 후 키보드 빠른 이동으로 문서를 연다", async ({ page }) => {
  await login(page);
  await page.keyboard.press("Control+O");
  const switcher = page.getByRole("combobox", { name: "명령 또는 문서 검색" });
  await expect(switcher).toBeFocused();
  await switcher.fill("시작 안내");
  await expect(page.getByRole("option", { name: "시작 안내 start.md", exact: true })).toBeVisible();
  await switcher.press("Enter");
  await expect(page).toHaveURL(/\/doc\/start\.md$/);
  await expect(page.getByRole("heading", { name: "시작 안내", exact: true }).first()).toBeVisible();
  await expect(page.getByText("키보드 탐색 기준 문서", { exact: true })).toBeVisible();
  expectNoExternalRequests(page.context());
});

test("문서 화면의 건너뛰기·탭·리사이저를 키보드로 조작한다", async ({ page }) => {
  await login(page);
  await page.goto("/doc/start.md");

  await page.evaluate(() => {
    document.body.tabIndex = -1;
    document.body.focus();
    document.body.removeAttribute("tabindex");
  });
  await page.keyboard.press("Tab");
  const skip = page.getByRole("link", { name: "본문으로 건너뛰기" });
  await expect(skip).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.locator("#main-content")).toBeFocused();

  const outline = page.getByRole("tab", { name: "목차" });
  const links = page.getByRole("tab", { name: "링크" });
  await outline.focus();
  await outline.press("ArrowRight");
  await expect(links).toBeFocused();
  await expect(links).toHaveAttribute("aria-selected", "true");
  await links.press("Home");
  await expect(outline).toBeFocused();

  const separator = page.getByRole("separator", { name: "좌측 사이드바 너비" });
  await separator.focus();
  const initial = Number(await separator.getAttribute("aria-valuenow"));
  await separator.press("ArrowRight");
  await expect(separator).toHaveAttribute("aria-valuenow", String(initial + 10));
  await separator.press("Home");
  await expect(separator).toHaveAttribute("aria-valuenow", "150");
  await separator.press("End");
  await expect(separator).toHaveAttribute("aria-valuenow", "560");
  expectNoExternalRequests(page.context());
});

test("공개 링크를 명시적으로 발급하고 그래프에서 Tab으로 빠져나온다", async ({ page }) => {
  await login(page);
  await page.goto("/doc/start.md");
  await page.getByRole("button", { name: "공유", exact: true }).click();
  await expect(page.getByText("30일 후 자동 만료되며 언제든 이 화면에서 취소할 수 있습니다.")).toBeVisible();
  await page.getByRole("button", { name: "30일 링크 만들기" }).click();
  const shareInput = page.locator("#share-url");
  await expect(shareInput).toHaveValue(/\/share\//);
  const shareUrl = await shareInput.inputValue();
  expect(shareUrl).toContain("/share/");
  const publicView = await page.request.get(shareUrl);
  expect(publicView.ok()).toBeTruthy();
  expect(await publicView.text()).toContain("키보드 탐색 기준 문서");

  await page.goto("/graph");
  await expect(page.locator("#ginfo")).toContainText("노드");
  const graph = page.locator("#cy");
  await graph.focus();
  await page.keyboard.press("Tab");
  await expect(graph).not.toBeFocused();
  expectNoExternalRequests(page.context());
});

test("문서를 만들고 편집해 원문에 저장한다", async ({ page }, testInfo) => {
  const documentName = `브라우저 흐름-${testInfo.retry}`;
  await login(page);
  await page.getByRole("link", { name: "새 문서", exact: true }).click();
  await page.getByLabel("이름").fill(documentName);
  await page.getByLabel("제목").fill(documentName);
  await replaceEditor(page, "# 브라우저 흐름\n\n처음 저장한 본문");
  await page.getByRole("button", { name: "저장" }).click();
  await expect(page).toHaveURL(new RegExp(`/doc/.+-${testInfo.retry}\\.md$`));
  await expect(page.getByText("처음 저장한 본문", { exact: true })).toBeVisible();

  await page.getByRole("link", { name: "편집", exact: true }).click();
  await replaceEditor(page, "# 브라우저 흐름\n\n편집 뒤 저장된 본문");
  await page.getByRole("button", { name: "저장" }).click();
  await expect(page.getByText("편집 뒤 저장된 본문", { exact: true })).toBeVisible();

  const raw = await page.request.get(`/doc/${documentName}.md/raw`);
  expect(raw.ok()).toBeTruthy();
  expect((await raw.text()).replaceAll("\r\n", "\n")).toBe("# 브라우저 흐름\n\n편집 뒤 저장된 본문");
  expectNoExternalRequests(page.context());
});

test("오래된 편집을 거부하고 서버 내용을 불러와 수동 복구한다", async ({ browser }) => {
  const context = await browser.newContext();
  const first = await context.newPage();
  const stale = await context.newPage();
  await login(first);
  await first.goto("/doc/conflict.md/edit");
  await stale.goto("/doc/conflict.md/edit");
  await replaceEditor(first, "# 충돌 문서\n\n먼저 저장한 변경");
  await first.getByRole("button", { name: "저장" }).click();
  await expect(first.getByText("먼저 저장한 변경", { exact: true })).toBeVisible();

  await replaceEditor(stale, "# 충돌 문서\n\n늦게 저장한 변경");
  const rejected = stale.waitForResponse((response) =>
    response.url().endsWith("/doc/conflict.md/edit") && response.request().method() === "POST"
  );
  await stale.getByRole("button", { name: "저장" }).click();
  expect((await rejected).status()).toBe(409);
  await expect(stale.getByText("충돌로 거부됨.")).toBeVisible();
  await stale.getByText("서버의 현재 내용 보기", { exact: true }).click();
  await expect(stale.locator("#server-current")).toContainText("먼저 저장한 변경");

  await stale.getByRole("button", { name: "서버 현재 선택" }).click();
  await expect(stale.locator("#merge-progress")).toHaveText("해결 1 / 1");
  await replaceEditor(stale, "# 충돌 문서\n\n먼저 저장한 변경\n\n수동으로 다시 적용한 변경");
  await stale.getByRole("button", { name: "저장" }).click();
  await expect(stale.getByText("수동으로 다시 적용한 변경", { exact: true })).toBeVisible();

  const raw = await stale.request.get("/doc/conflict.md/raw");
  expect(raw.ok()).toBeTruthy();
  expect((await raw.text()).replaceAll("\r\n", "\n")).toContain("먼저 저장한 변경\n\n수동으로 다시 적용한 변경");
  expectNoExternalRequests(context);
  await context.close();
});
