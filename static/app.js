const buttons = [...document.querySelectorAll("[data-location]")];
const byId = (id) => document.getElementById(id);
const panel = document.querySelector(".panel");
let request;
let generation = 0;
let selectedUnits = "c";
let observation = null;
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
  const current = ++generation;
  buttons.forEach((item) => item.setAttribute("aria-pressed", String(item === button)));
  observation = null;
  byId("location").textContent = label;
  byId("weather").hidden = true;
  byId("source").hidden = true;
  byId("status").textContent = "Loading current conditions...";
  panel.setAttribute("aria-busy", "true");
  try {
    const response = await fetch(url, {
      signal: request.signal,
      credentials: "same-origin",
    });
    const data = await response.json();
    if (current !== generation) return;
    if (!response.ok) {
      const retryAfter = Number(response.headers.get("Retry-After"));
      const retry = retryAfter >= 60 ? " Try again in a minute." : "";
      byId("status").textContent = `${data.error?.message || "Weather is unavailable."}${retry}`;
      return;
    }
    observation = data;
    renderWeather(data);
    byId("status").textContent = `Current weather loaded for ${data.location}.`;
  } catch (error) {
    if (error.name !== "AbortError" && current === generation) {
      byId("status").textContent = "Unable to load weather. Check your connection and try again.";
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
load(`/api/weather/${encodeURIComponent(buttons[0].dataset.location)}`, buttons[0].textContent, buttons[0]);
