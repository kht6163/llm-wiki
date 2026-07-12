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
}

async function resultTitles(page) {
  return page.getByRole("list").filter({ has: page.getByRole("link", { name: /검색 워크벤치/ }) })
    .getByRole("link").allTextContents();
}

test("키보드 검색과 연산자 도움말이 검색 워크벤치로 이어진다", async ({ page }) => {
  await login(page);

  const globalSearch = page.getByRole("search").getByRole("searchbox", { name: "검색" });
  await globalSearch.focus();
  await globalSearch.fill("workbenchneedle");
  await globalSearch.press("Enter");
  await expect(page).toHaveURL(/\/search\?q=workbenchneedle$/);
  await expect(page.getByRole("heading", { name: "검색", exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "검색 워크벤치 01" })).toBeVisible();

  await page.getByRole("button", { name: "사이드바 토글", exact: true }).focus();
  await page.keyboard.press("?");
  const help = page.getByText("검색 연산자 도움말", { exact: false });
  await expect(help).toBeFocused();
  for (const operator of ["title:", "path:", "tag:", "has:"]) {
    await expect(page.getByText(operator, { exact: true })).toBeVisible();
  }
  await page.keyboard.press("Escape");
  await expect(help).toBeFocused();
  await expect(page.getByText("title:\"API guide\" 제목 포함", { exact: true })).toBeHidden();
  expectNoExternalRequests(page.context());
});

test("반복 태그와 검색 모드가 안정적인 다음·이전 페이지와 칩 제거에 유지된다", async ({ page }) => {
  await login(page);
  const query = 'workbenchneedle title:"검색 워크벤치" path:e2e-search/* tag:release tag:release has:tag';
  const params = new URLSearchParams([
    ["q", query], ["mode", "bm25"], ["folder", "e2e-search"],
    ["tag", "release"], ["tag", "todo"], ["tag", "release"],
    ["page", "1"], ["per_page", "2"],
  ]);
  await page.goto(`/search?${params}`);

  await expect(page.getByLabel("검색 방식")).toHaveValue("bm25");
  await expect(page.getByLabel("폴더 필터")).toHaveValue("e2e-search");
  const tagValues = await page.getByLabel("요청 태그 필터").getByRole("textbox")
    .evaluateAll((fields) => fields.map((field) => field.value));
  expect(tagValues).toEqual(["release", "todo", "release"]);
  const firstPage = await resultTitles(page);
  expect(firstPage).toHaveLength(2);

  await page.getByRole("link", { name: "다음" }).click();
  expect(new URL(page.url()).searchParams.getAll("tag")).toEqual(["release", "todo", "release"]);
  expect(new URL(page.url()).searchParams.get("mode")).toBe("bm25");
  expect(new URL(page.url()).searchParams.get("folder")).toBe("e2e-search");
  expect(new URL(page.url()).searchParams.get("per_page")).toBe("2");
  const secondPage = await resultTitles(page);
  expect(secondPage).toHaveLength(2);
  expect(new Set(firstPage).isDisjointFrom(new Set(secondPage))).toBeTruthy();

  await page.getByRole("link", { name: "이전" }).click();
  expect(await resultTitles(page)).toEqual(firstPage);

  const releaseChips = page.getByRole("button", { name: "필터 제거: tag:release", exact: true });
  await expect(releaseChips).toHaveCount(4);
  await releaseChips.nth(1).click();
  const removedUrl = new URL(page.url());
  expect(removedUrl.searchParams.get("q").match(/tag:release/g)).toHaveLength(1);
  expect(removedUrl.searchParams.getAll("tag")).toEqual(["release", "todo", "release"]);
  expect(removedUrl.searchParams.get("page")).toBe("1");
  await expect(page.getByRole("button", { name: "필터 제거: tag:release", exact: true })).toHaveCount(3);
  expectNoExternalRequests(page.context());
});

test("빈 결과·잘못된 연산자·검색 상한 문구를 안전하게 표시한다", async ({ page }) => {
  await login(page);

  await page.goto("/search?q=no-such-workbench-document&mode=bm25");
  await expect(page.getByText("검색 결과가 없습니다", { exact: true })).toBeVisible();
  await expect(page.getByRole("status")).toContainText("총 0건");

  const malformed = await page.goto("/search?q=workbenchneedle+has%3Aunknown&mode=bm25&tag=release&tag=todo&per_page=10");
  expect(malformed.status()).toBe(400);
  await expect(page.getByRole("alert")).toContainText("has:");
  await expect(page.getByRole("search").getByRole("searchbox", { name: "검색어" })).toHaveValue(
    "workbenchneedle has:unknown",
  );
  await expect(page.getByText("검색 연산자 도움말", { exact: false })).toBeVisible();
  expect(new URL(page.url()).searchParams.getAll("tag")).toEqual(["release", "todo"]);

  await page.goto("/search?q=workbenchneedle&mode=bm25&page=301&per_page=2");
  const status = page.getByRole("status");
  await expect(status).toContainText("600건 검색 범위 상한에 도달했습니다");
  await expect(status).toContainText("정확한 전체 건수는 알 수 없습니다");
  await expect(status).not.toContainText("총 600건");
  expectNoExternalRequests(page.context());
});

test("860px 검색 화면은 가로로 넘치지 않는다", async ({ page }) => {
  await page.setViewportSize({ width: 860, height: 900 });
  await login(page);
  await page.goto("/search?q=workbenchneedle&mode=bm25&folder=e2e-search&tag=release&tag=todo&per_page=2");
  await expect(page.getByRole("heading", { name: "검색", exact: true })).toBeVisible();

  const dimensions = await page.evaluate(() => ({
    viewport: document.documentElement.clientWidth,
    page: document.documentElement.scrollWidth,
    workbench: document.getElementById("search-workbench").getBoundingClientRect().toJSON(),
  }));
  expect(dimensions.page).toBeLessThanOrEqual(dimensions.viewport);
  expect(dimensions.workbench.x).toBeGreaterThanOrEqual(0);
  expect(dimensions.workbench.right).toBeLessThanOrEqual(dimensions.viewport);
  expectNoExternalRequests(page.context());
});
