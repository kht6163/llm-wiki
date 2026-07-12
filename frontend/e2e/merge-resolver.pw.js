import { expect, test } from "@playwright/test";

const USERNAME = "admin";
const PASSWORD = "e2e-secret12";

async function login(page) {
  await page.goto("/login");
  await page.getByLabel("아이디").fill(USERNAME);
  await page.getByLabel("비밀번호").fill(PASSWORD);
  await page.getByRole("button", { name: "로그인" }).click();
  await expect(page).toHaveURL(/\/$/);
}

async function replaceEditor(page, content) {
  await expect(page.locator(".cm-content")).toBeVisible();
  await page.locator("#md-editor-mount").evaluate((mount, exactContent) => {
    const view = mount.wikiEditorApi.getView();
    view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: exactContent } });
  }, content);
}

async function session(browser) {
  const context = await browser.newContext();
  const page = await context.newPage();
  await login(page);
  return { context, page };
}

async function raw(page, path) {
  const response = await page.request.get(`/doc/${path}/raw`);
  expect(response.ok()).toBeTruthy();
  return (await response.text()).replaceAll("\r\n", "\n");
}

test("분리 편집은 제안을 명시 적용한 뒤 CAS로 저장한다", async ({ browser }) => {
  const first = await session(browser);
  const stale = await session(browser);
  await first.page.goto("/doc/merge-disjoint.md/edit");
  await stale.page.goto("/doc/merge-disjoint.md/edit");

  await replaceEditor(first.page, "ONE\ntwo\nthree\n");
  await first.page.getByRole("button", { name: "저장" }).click();
  await replaceEditor(stale.page, "one\ntwo\nTHREE\n");
  const rejected = stale.page.waitForResponse((response) =>
    response.url().endsWith("/doc/merge-disjoint.md/edit") && response.request().method() === "POST"
  );
  await stale.page.getByRole("button", { name: "저장" }).click();
  expect((await rejected).status()).toBe(409);
  const proposalPayload = JSON.parse(await stale.page.locator("#merge-payload").textContent());
  expect(proposalPayload).toMatchObject({
    base: "one\r\ntwo\r\nthree\r\n",
    mine: "one\r\ntwo\r\nTHREE\r\n",
    current: "ONE\r\ntwo\r\nthree\r\n",
    merged: "ONE\r\ntwo\r\nTHREE\r\n",
    conflicts: [],
  });

  const save = stale.page.getByRole("button", { name: "저장" });
  await expect(save).toBeDisabled();
  await stale.page.getByRole("button", { name: "자동 병합 제안 적용" }).click();
  await expect(save).toBeEnabled();
  await save.click();
  await expect(stale.page).toHaveURL(/\/doc\/merge-disjoint\.md$/);
  expect(await raw(stale.page, "merge-disjoint.md")).toBe("ONE\ntwo\nTHREE\n");

  await first.context.close();
  await stale.context.close();
});

test("겹친 세 hunk를 키보드와 mine/current/manual 선택으로 해결한다", async ({ browser }) => {
  const first = await session(browser);
  const stale = await session(browser);
  await first.page.goto("/doc/merge-overlap.md/edit");
  await stale.page.goto("/doc/merge-overlap.md/edit");

  await replaceEditor(first.page, "top\nserver alpha\nkeep-a\nserver beta\nkeep-b\nserver gamma\nbottom\n");
  await first.page.getByRole("button", { name: "저장" }).click();
  await replaceEditor(stale.page, "top\nmine alpha\nkeep-a\nmine beta\nkeep-b\nmine gamma\nbottom\n");
  await stale.page.getByRole("button", { name: "저장" }).click();
  const overlapPayload = JSON.parse(await stale.page.locator("#merge-payload").textContent());
  expect(overlapPayload.conflicts).toHaveLength(3);
  expect(overlapPayload).toMatchObject({
    base: "top\r\nalpha\r\nkeep-a\r\nbeta\r\nkeep-b\r\ngamma\r\nbottom\r\n",
    mine: "top\r\nmine alpha\r\nkeep-a\r\nmine beta\r\nkeep-b\r\nmine gamma\r\nbottom\r\n",
    current: "top\r\nserver alpha\r\nkeep-a\r\nserver beta\r\nkeep-b\r\nserver gamma\r\nbottom\r\n",
  });
  await expect(stale.page.locator(".merge-conflict")).toHaveCount(3);

  const save = stale.page.getByRole("button", { name: "저장" });
  const progress = stale.page.locator("#merge-progress");
  await expect(save).toBeDisabled();
  await expect(progress).toHaveText("해결 0 / 3");
  const mineButtons = stale.page.getByRole("button", { name: "내 편집 선택" });
  await mineButtons.nth(0).focus();
  await stale.page.keyboard.press("Space");
  await expect(progress).toHaveText("해결 1 / 3");
  await expect(mineButtons.nth(1)).toBeFocused();

  await stale.page.getByRole("button", { name: "서버 현재 선택" }).nth(1).click();
  await expect(progress).toHaveText("해결 2 / 3");
  await expect(mineButtons.nth(2)).toBeFocused();
  await stale.page.getByLabel("직접 편집").nth(2).fill("manual gamma\n");
  await stale.page.getByRole("button", { name: "직접 편집 적용" }).nth(2).click();
  await expect(progress).toHaveText("해결 3 / 3");
  await expect(save).toBeFocused();
  await expect(save).toBeEnabled();
  await save.click();

  expect(await raw(stale.page, "merge-overlap.md")).toBe(
    "top\nmine alpha\nkeep-a\nserver beta\nkeep-b\nmanual gamma\nbottom\n"
  );
  await first.context.close();
  await stale.context.close();
});

test("해결 뒤 제3자 갱신은 두 번째 409와 새 resolver를 만든다", async ({ browser }) => {
  const first = await session(browser);
  const stale = await session(browser);
  const third = await session(browser);
  await first.page.goto("/doc/merge-repeat.md/edit");
  await stale.page.goto("/doc/merge-repeat.md/edit");

  await replaceEditor(first.page, "top\nserver two\nbottom\n");
  await first.page.getByRole("button", { name: "저장" }).click();
  await third.page.goto("/doc/merge-repeat.md/edit");
  await replaceEditor(stale.page, "top\nmine two\nbottom\n");
  await stale.page.getByRole("button", { name: "저장" }).click();
  await stale.page.getByRole("button", { name: "내 편집 선택" }).click();
  await expect(stale.page.getByRole("button", { name: "저장" })).toBeEnabled();

  await replaceEditor(third.page, "top\nserver three\nbottom\n");
  await third.page.getByRole("button", { name: "저장" }).click();
  const rejectedAgain = stale.page.waitForResponse((response) =>
    response.url().endsWith("/doc/merge-repeat.md/edit") && response.request().method() === "POST"
  );
  await stale.page.getByRole("button", { name: "저장" }).click();
  expect((await rejectedAgain).status()).toBe(409);
  await expect(stale.page.locator('input[name="base_version"]')).toHaveValue("3");
  await expect(stale.page.locator("#editor")).toHaveValue("top\nmine two\nbottom\n");
  await expect(stale.page.locator("#merge-progress")).toHaveText("해결 0 / 1");

  await stale.page.getByLabel("직접 편집").fill("final manual\n");
  await stale.page.getByRole("button", { name: "직접 편집 적용" }).click();
  await stale.page.getByRole("button", { name: "저장" }).click();
  expect(await raw(stale.page, "merge-repeat.md")).toBe("top\nfinal manual\nbottom\n");

  await first.context.close();
  await stale.context.close();
  await third.context.close();
});
