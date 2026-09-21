import { expect, test } from "@playwright/test";

test("an unauthenticated visitor lands on a usable login form", async ({ page }) => {
  await page.goto("/");

  await expect(page).toHaveURL(/\/login/);
  await expect(
    page.getByRole("heading", { name: "登录控制台" }),
  ).toBeVisible();
  await expect(page.getByLabel("电子邮箱")).toBeVisible();
  await expect(page.getByLabel("密码")).toBeVisible();
  await expect(page.getByRole("button", { name: "登录" })).toBeVisible();
  // The shell must render even with no Supabase configuration available.
  await expect(page.locator("body")).not.toBeEmpty();
});

test("protected pages redirect to the login form instead of erroring", async ({
  page,
}) => {
  for (const path of ["/setup", "/settings", "/performance"]) {
    await page.goto(path);
    await expect(page).toHaveURL(/\/login/, { timeout: 15_000 });
  }
});

test("the deployment check endpoint answers with the dashboard origin", async ({
  request,
  baseURL,
}) => {
  const response = await request.get("/api/deployment-check");

  expect(response.ok()).toBeTruthy();
  const payload = (await response.json()) as {
    configured: boolean;
    dashboardOrigin: string;
  };
  expect(typeof payload.configured).toBe("boolean");
  // Next normalises the request origin to localhost, so compare the port the
  // dashboard is actually served on rather than the exact host string.
  const expected = new URL(baseURL ?? "");
  const reported = new URL(payload.dashboardOrigin);
  expect(reported.port).toBe(expected.port);
  expect(["localhost", "127.0.0.1"]).toContain(reported.hostname);
});

test("diagnostics page renders without backend credentials", async ({ page }) => {
  await page.goto("/diagnostics");

  await expect(page.locator("body")).toBeVisible();
  await expect(page.getByText(/部署|检查|诊断/).first()).toBeVisible();
});
