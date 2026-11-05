const buttons = [...document.querySelectorAll("[data-location]")];
const byId = (id) => document.getElementById(id);
const panel = document.querySelector(".panel");
let request;
let generation = 0;
const units = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 });
const date = (epoch) => new Date(epoch * 1000).toLocaleString();

async function load(button) {
  request?.abort();
  request = new AbortController();
  const current = ++generation;
  buttons.forEach((item) => item.setAttribute("aria-pressed", String(item === button)));
  byId("location").textContent = button.textContent;
  byId("weather").hidden = true;
  byId("source").hidden = true;
  byId("status").textContent = "Loading current conditions...";
  panel.setAttribute("aria-busy", "true");
  try {
    const response = await fetch(`/api/weather/${encodeURIComponent(button.dataset.location)}`, {
      signal: request.signal,
      credentials: "same-origin",
    });
    const data = await response.json();
    if (current !== generation) return;
    if (!response.ok) {
      const retry = response.status === 503 ? " Try again in a minute." : "";
      byId("status").textContent = `${data.error?.message || "Weather is unavailable."}${retry}`;
      return;
    }
    byId("temperature").textContent = `${units.format(data.temperature)} °C`;
    byId("condition").textContent = data.condition;
    byId("feels-like").textContent = `${units.format(data.feelsLike)} °C`;
    byId("wind").textContent = `${units.format(data.wind)} km/h`;
    byId("humidity").textContent = `${units.format(data.humidity)}%`;
    byId("observation").textContent = `Observed ${date(data.observedAt)} · Retrieved ${date(data.fetchedAt)} · Cache expires ${date(data.cacheExpiresAt)}`;
    byId("source").textContent = data.cached ? "Cached observation" : "Freshly retrieved";
    byId("source").hidden = false;
    byId("weather").hidden = false;
    byId("status").textContent = `Current weather loaded for ${button.textContent}.`;
  } catch (error) {
    if (error.name !== "AbortError" && current === generation) {
      byId("status").textContent = "Unable to load weather. Check your connection and try again.";
    }
  } finally {
    if (current === generation) panel.setAttribute("aria-busy", "false");
  }
}

buttons.forEach((button) => button.addEventListener("click", () => load(button)));
load(buttons[0]);
