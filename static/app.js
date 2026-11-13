const buttons = [...document.querySelectorAll("[data-location]")];
const byId = (id) => document.getElementById(id);
const panel = document.querySelector(".panel");
let request;
let generation = 0;
let selectedUnits = "c";
let observation = null;
let lastLookup = null;
const units = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 });
const date = (epoch) => new Date(epoch * 1000).toLocaleString();

function renderWeather(data) {
  const temperature = (value) => selectedUnits === "f" ? value * 9 / 5 + 32 : value;
  const suffix = selectedUnits === "f" ? "°F" : "°C";
  byId("location").textContent = data.location;
  byId("temperature").textContent = `${units.format(temperature(data.temperature))} ${suffix}`;
  byId("condition").textContent = data.condition;
  byId("feels-like").textContent = `${units.format(temperature(data.feelsLike))} ${suffix}`;
  byId("wind").textContent = selectedUnits === "f"
    ? `${units.format(data.wind / 1.609344)} mph`
    : `${units.format(data.wind)} km/h`;
  byId("humidity").textContent = `${units.format(data.humidity)}%`;
  byId("observation").textContent = `Observed ${date(data.observedAt)} · Retrieved ${date(data.fetchedAt)} · Cache expires ${date(data.cacheExpiresAt)}`;
  byId("source").textContent = data.cached ? "Cached observation" : "Freshly retrieved";
  byId("source").hidden = false;
  byId("weather").hidden = false;
}

async function load(url, label, button = null) {
  request?.abort();
  request = new AbortController();
  lastLookup = { url, label, button };
  const current = ++generation;
  buttons.forEach((item) => item.setAttribute("aria-pressed", String(item === button)));
  observation = null;
  byId("location").textContent = label;
  byId("weather").hidden = true;
  byId("source").hidden = true;
  byId("status").textContent = "Loading current conditions...";
  byId("retry").hidden = true;
  panel.setAttribute("aria-busy", "true");
  try {
    const response = await fetch(url, {
      signal: request.signal,
      credentials: "same-origin",
    });
    const data = await response.json();
    if (current !== generation) return;
    if (!response.ok) {
      byId("setup-status").textContent = ["configuration_missing", "configuration_invalid", "secret_missing", "secret_disabled", "secret_expired", "secret_invalid", "identity_unavailable", "vault_denied", "vault_unreachable", "vault_access", "provider_auth", "provider_unavailable", "provider_quota", "demo_expired"].includes(data.error?.code)
        ? "Service setup or access needs attention. Follow the setup steps for this same app; no credentials belong on this page."
        : "This request did not verify service access.";
      const retryAfter = Number(response.headers.get("Retry-After"));
      const retry = retryAfter >= 60 ? " Try again in a minute." : "";
      byId("status").textContent = `${data.error?.message || "Weather is unavailable."}${retry}`;
      byId("retry").hidden = ["configuration_missing", "configuration_invalid", "demo_expired"].includes(data.error?.code);
      return;
    }
    observation = data;
    byId("setup-status").textContent = document.querySelector(".preview-notice")
      ? "Simulated weather read only. Azure and WeatherAPI connectivity are not verified."
      : "A real weather read succeeded. Cached results do not prove current connectivity.";
    renderWeather(data);
    byId("status").textContent = `Current weather loaded for ${data.location}.`;
  } catch (error) {
    if (error.name !== "AbortError" && current === generation) {
      byId("status").textContent = "Unable to load weather. Check your connection and try again.";
      byId("retry").hidden = false;
    }
  } finally {
    if (current === generation) panel.setAttribute("aria-busy", "false");
  }
}

buttons.forEach((button) => button.addEventListener("click", () =>
  load(`/api/weather/${encodeURIComponent(button.dataset.location)}`, button.textContent, button)
));
byId("location-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const value = byId("location-query").value.trim();
  load(`/api/weather?${new URLSearchParams({ location: value })}`, value);
});
document.querySelectorAll('input[name="units"]').forEach((input) => input.addEventListener("change", () => {
  selectedUnits = input.value;
  if (observation) {
    renderWeather(observation);
    byId("status").textContent = `Showing ${selectedUnits === "f" ? "Fahrenheit and miles per hour" : "Celsius and kilometers per hour"} for ${observation.location}.`;
  }
}));
byId("retry").addEventListener("click", () => {
  if (lastLookup) load(lastLookup.url, lastLookup.label, lastLookup.button);
});
byId("health").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  byId("health-status").textContent = "Checking /health...";
  const abort = new AbortController();
  const timeout = setTimeout(() => abort.abort(), 5000);
  try {
    const response = await fetch("/health", { signal: abort.signal, credentials: "same-origin" });
    const data = await response.json();
    if (!response.ok || data.status !== "ready") throw new Error("Health unavailable");
    byId("health-status").textContent = `/health · HTTP ${response.status} · ${data.status}. Process ready; Key Vault and WeatherAPI access are not verified.`;
  } catch {
    byId("health-status").textContent = "App health could not be checked. Check your connection and retry.";
  } finally {
    clearTimeout(timeout);
    button.disabled = false;
  }
});
load(`/api/weather/${encodeURIComponent(buttons[0].dataset.location)}`, buttons[0].textContent, buttons[0]);
