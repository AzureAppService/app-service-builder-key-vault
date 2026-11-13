import assert from "node:assert/strict";

const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const url = process.env.SAMPLE_URL || "http://127.0.0.1:8117";
assert.equal(new URL(url).hostname, "127.0.0.1", "UI checks must use the local fixture");
const missing = process.env.PREVIEW_SECRET_MISSING === "1";
const browser = await chromium.launch({
  executablePath: process.env.EDGE_EXECUTABLE,
  headless: true,
});
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
  const errors = [];
  let requests = 0;
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("request", (request) => { if (request.url().includes("/api/weather")) requests++; });
  await page.goto(url);
  await page.locator(".preview-notice").waitFor();
  assert.equal(await page.getByRole("link", { name: "Set up services for this deployed app" }).getAttribute("href"), "./README.md#setup");
  if (missing) {
    await page.locator("#status").filter({ hasText: "secret does not exist" }).waitFor();
    assert.equal(await page.locator("#weather").isVisible(), false);
    assert.equal(await page.locator("#source").isVisible(), false);
    const response = await page.request.get(`${url}/api/weather/seattle`);
    assert.equal(response.status(), 503);
    assert.equal((await response.json()).error.code, "secret_missing");
    const before = requests;
    await page.locator("#retry").click();
    await page.locator("#status").filter({ hasText: "secret does not exist" }).waitFor();
    assert.equal(requests, before + 1, "Explicit retry sends exactly one request");
    assert.equal(await page.locator("#weather").isVisible(), false);
  } else {
    await page.locator("#weather:not([hidden])").waitFor();
    assert.match(await page.locator("#temperature").textContent(), /12.*°C/);
    const before = requests;
    await page.locator('input[value="f"]').check();
    assert.match(await page.locator("#temperature").textContent(), /53.6.*°F/);
    assert.match(await page.locator("#wind").textContent(), /mph/);
    assert.equal(requests, before, "Units convert the current observation without fetching");
    await page.locator('input[value="c"]').check();
    await page.locator('[data-location="london"]').click();
    await page.locator("#status").filter({ hasText: "loaded for London" }).waitFor();
    await page.locator("#location-query").fill("Paris");
    await page.getByRole("button", { name: "Get weather", exact: true }).click();
    await page.locator("#status").filter({ hasText: "loaded for Paris" }).waitFor();
    assert.match(await page.locator("#observation").textContent(), /Observed.*Retrieved.*Cache expires/);
    await page.locator("#location-query").fill("Nowhere");
    await page.getByRole("button", { name: "Get weather", exact: true }).click();
    await page.locator("#retry:not([hidden])").waitFor();
    assert.equal(await page.locator("#weather").isVisible(), false);
    assert.equal(await page.locator("#source").isVisible(), false);
    await page.locator('[data-location="seattle"]').click();
    await page.locator("#weather:not([hidden])").waitFor();
    await page.route("**/health", (route) => route.abort());
    await page.locator("#health").click();
    await page.locator("#health-status").filter({ hasText: "could not be checked" }).waitFor();
    assert.equal(await page.locator("#health").isEnabled(), true);
    await page.unroute("**/health");
  }
  await page.locator("#health").click();
  await page.locator("#health-status").filter({ hasText: "HTTP 200" }).waitFor();
  assert.match(await page.locator("#health-status").textContent(), /Key Vault and WeatherAPI access are not verified/);
  assert.match(await page.locator("#setup-status").textContent(), missing ? /Service setup or access needs attention/ : /Simulated weather read only/);
  if (process.env.SCREENSHOT_DIR) await page.screenshot({ path: `${process.env.SCREENSHOT_DIR}/desktop1440.png`, fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  await page.getByText("Customize locations and appearance", { exact: true }).click();
  assert.equal(await page.getByText(/Keep the WeatherAPI credential in Key Vault/).isVisible(), true);
  if (process.env.SCREENSHOT_DIR) await page.screenshot({ path: `${process.env.SCREENSHOT_DIR}/mobile390.png`, fullPage: true });
  await page.emulateMedia({ colorScheme: "dark" });
  await page.reload();
  assert.equal(await page.locator("html").getAttribute("data-theme"), "dark");
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  if (process.env.SCREENSHOT_DIR) await page.screenshot({ path: `${process.env.SCREENSHOT_DIR}/dark-mobile390.png`, fullPage: true });
  await page.goto(`${url}/?scoutTheme=light`);
  assert.equal(await page.locator("html").getAttribute("data-theme"), "light");
  assert.equal(await page.locator("a[download]").count(), 0);
  await page.getByRole("link", { name: "Set up services for this deployed app" }).click();
  await page.locator("#setup").waitFor();
  assert.equal(await page.locator("h2").count(), 6);
  assert.ok((await page.locator("pre code").allTextContents()).every(code => !/\\\n\n/.test(code)), "Reader preserves shell continuations");
  assert.match(await page.locator("body").textContent(), /Key Vault Secrets User/);
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  await page.goBack();
  const assets = await page.locator("img").evaluateAll((images) => images.map((image) => ({ src: image.getAttribute("src"), loaded: image.complete && image.naturalWidth > 0 })));
  assert.ok(assets.every((asset) => asset.loaded));
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ sample: "key-vault-weather", fixture: missing ? "missing-secret" : "synthetic-observations", realLocalHttp: true, health: "passed", retry: missing ? "passed" : "error-and-recovery", units: missing ? "unavailable-honestly" : "passed-without-fetch", desktop: 1440, mobile: 390, darkTheme: "passed", assets, pageErrors: errors, cloudVerified: false }, null, 2));
} finally {
  await browser.close();
}
