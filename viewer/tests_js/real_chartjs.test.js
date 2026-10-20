// Runs the drive page's summary script against the REAL Chart.js (not a fake) in a simulated browser,
// toggles the theme, and checks the colours Chart.js actually uses afterwards. A fake Chart.js cannot
// show problems in how the page talks to the real library (the first version of the recolouring code
// crashed in the real one and passed every fake-library test).
//
// Needs chart.js and jsdom (not part of the project): 
//   npm install --prefix /tmp/chartlab chart.js@4.4.1 jsdom
//   NODE_PATH=/tmp/chartlab/node_modules node viewer/tests_js/real_chartjs.test.js
let Chart, registerables, JSDOM;
try {
  ({Chart, registerables} = require("chart.js"));
  ({JSDOM} = require("jsdom"));
} catch (e) {
  console.log("SKIP: chart.js and jsdom are not installed (see the comment at the top of this file)");
  process.exit(0);
}
const {check, summary, lastInlineScript} = require("./harness");
Chart.register(...registerables);
const source = lastInlineScript("viewer/drive_detail.html");

const ids = ["summary-section", "summary-numbers", "summary-bars", "summary-charts", "summary-events", "summary-range",
  "summary-zoom-in", "summary-zoom-out", "summary-pan-left", "summary-pan-right", "summary-zoom-reset"];
const html = "<!doctype html><html><body>" + ids.map(i => `<div id="${i}"></div>`).join("") +
  '<canvas id="summary-speed" width="600" height="200"></canvas><canvas id="summary-strips" width="600" height="90"></canvas></body></html>';

async function run(dark) {
  const dom = new JSDOM(html, {pretendToBeVisual: true});
  const errors = [];
  dom.window.addEventListener("error", e => errors.push(e.error || e.message));
  const fills = [];
  const ctx = new Proxy({}, {
    get: (t, k) => k === "measureText" ? () => ({width: 10}) : (k in t ? t[k] : () => {}),
    set: (t, k, v) => { if (k === "fillStyle") fills.push(v); t[k] = v; return true; },
  });
  dom.window.HTMLCanvasElement.prototype.getContext = function () { ctx.canvas = this; return ctx; };
  // jsdom has no layout, so give elements a size or Chart.js thinks the canvas is empty and draws nothing
  const proto = dom.window.HTMLElement.prototype;
  proto.getBoundingClientRect = () => ({width: 600, height: 200, top: 0, left: 0, right: 600, bottom: 200, x: 0, y: 0});
  Object.defineProperty(proto, "clientWidth", {get: () => 600, configurable: true});
  Object.defineProperty(proto, "clientHeight", {get: () => 200, configurable: true});
  global.window = dom.window; global.document = dom.window.document; global.Chart = Chart;
  global.navigator = dom.window.navigator;
  global.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} };
  global.CustomEvent = dom.window.CustomEvent;
  global.MutationObserver = dom.window.MutationObserver;   // responsive charts watch for being removed
  global.requestAnimationFrame = dom.window.requestAnimationFrame.bind(dom.window);
  global.getComputedStyle = dom.window.getComputedStyle.bind(dom.window);
  if (dark) document.documentElement.classList.add("dark");
  const t = [], mph = [], steer = [], speed = [];
  for (let i = 0; i < 120; i++) { t.push(i); mph.push(30 + i % 10); steer.push(i < 60 ? 1 : 2); speed.push(i < 30 ? 1 : 0); }
  const data = {duration_s: 120, engaged_s: 100, distance_mi: 1, avg_mph: 30, max_mph: 40, interruptions: 1,
    steer_pilot_pct: 60, steer_driver_pct: 40, speed_pilot_pct: 25, speed_driver_pct: 75,
    counts: {wheel_override: 1}, events: [[10, "wheel_override"]], series: {t, mph, steer, speed}};
  global.fetch = () => Promise.resolve({ok: true, json: () => Promise.resolve(data)});
  new dom.window.Function(source).call(dom.window);
  await new Promise(r => setTimeout(r, 50));
  return {dom, errors, fills, speed: Chart.getChart("summary-speed"), strips: Chart.getChart("summary-strips")};
}

const resolved = c => ({
  tick: c.scales.x.options.ticks.color, grid: c.scales.x.options.grid.color, xTitle: c.scales.x.options.title.color,
  yTick: c.scales.y.options.ticks.color, yTitle: c.scales.y.options.title.color,
  legend: c.legend.options.labels.color, line: c.getDatasetMeta(0).dataset.options.borderColor,
  marker: c.getDatasetMeta(1).data[0].options.borderColor,
});
const LIGHT = {tick: "#4b5563", grid: "rgba(0, 0, 0, 0.1)", xTitle: "#4b5563", yTick: "#4b5563", yTitle: "#4b5563", legend: "#4b5563", line: "#2563eb", marker: "#ffffff"};
const DARK = {tick: "#cbd5e1", grid: "rgba(148, 163, 184, 0.2)", xTitle: "#cbd5e1", yTick: "#cbd5e1", yTitle: "#cbd5e1", legend: "#cbd5e1", line: "#60a5fa", marker: "#0f172a"};
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

(async () => {
  let r = await run(false);
  check("both charts are created by the real Chart.js", !!r.speed && !!r.strips && r.errors.length === 0);
  check("light theme: the colours Chart.js uses", same(resolved(r.speed), LIGHT));

  r.dom.window.document.documentElement.classList.add("dark");
  r.fills.length = 0;
  r.dom.window.dispatchEvent(new r.dom.window.CustomEvent("themechange", {detail: {dark: true}}));
  await new Promise(res => setTimeout(res, 50));   // Chart.js may draw on the next animation frame
  check("toggling to dark raises no error", r.errors.length === 0);
  check("dark after the toggle: axes, grid, titles, legend, line and marker ring all changed", same(resolved(r.speed), DARK));
  check("the strips are drawn with the dark band colour after the toggle", r.fills.includes("#334155") && !r.fills.includes("#d1d5db"));

  r.dom.window.document.documentElement.classList.remove("dark");
  r.dom.window.dispatchEvent(new r.dom.window.CustomEvent("themechange", {detail: {dark: false}}));
  check("and back to light again", r.errors.length === 0 && same(resolved(r.speed), LIGHT));

  r = await run(true);
  check("a page opened in dark mode starts with the dark colours", r.errors.length === 0 && same(resolved(r.speed), DARK));
  summary();
})();
