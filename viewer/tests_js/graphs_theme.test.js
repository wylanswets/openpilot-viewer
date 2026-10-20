// Runs the drive page's summary script with a fake browser and a fake Chart.js, in both themes.
const {check, summary, lastInlineScript, fakeBrowser} = require("./harness");
const source = lastInlineScript("viewer/drive_detail.html");

function makeNode() {
  const n = {children: [], style: {}, className: "", textContent: "", title: "", listeners: {},
    classList: {remove(c) { n.className = n.className.replace(c, ""); }}};
  n.appendChild = c => { n.children.push(c); return c; };
  n.addEventListener = (e, f) => { n.listeners[e] = f; };
  return n;
}

function run(dark) {
  const b = fakeBrowser({htmlClasses: dark ? ["dark"] : []});
  const ids = {};
  const doc = Object.assign({}, b.doc, {
    createElement: () => makeNode(),
    getElementById: id => ids[id] || (ids[id] = makeNode()),
  });
  const charts = [];
  const Chart = function (canvas, cfg) {
    const c = this; charts.push(c); c.config = cfg; c.options = cfg.options; c.data = cfg.data; c.updates = 0;
    c.scales = {x: {min: cfg.options.scales.x.min, max: cfg.options.scales.x.max}};
    c.update = () => { c.updates++; };
  };
  Chart.defaults = {};
  Chart.register = () => {};
  const t = [], mph = [], steer = [], speed = [];
  for (let i = 0; i < 120; i++) { t.push(i); mph.push(30 + i % 10); steer.push(i < 60 ? 1 : 2); speed.push(i < 30 ? 1 : 0); }
  const data = {duration_s: 120, engaged_s: 100, distance_mi: 1, avg_mph: 30, max_mph: 40, interruptions: 1,
    steer_pilot_pct: 60, steer_driver_pct: 40, speed_pilot_pct: 25, speed_driver_pct: 75,
    counts: {wheel_override: 1}, events: [[10, "wheel_override"]], series: {t, mph, steer, speed}};
  global.window = Object.assign(b.win, {ChartZoom: {}});
  global.document = doc; global.Chart = Chart;
  global.fetch = () => Promise.resolve({ok: true, json: () => Promise.resolve(data)});
  new Function(source)();
  return new Promise(resolve => setTimeout(() => resolve({b, charts, Chart}), 20));
}

function drawStrips(charts) {   // run the strips plugin against a recording canvas context
  const fills = [];
  const ctx = new Proxy({}, {get: (_, k) => (...a) => {}, set: (_, k, v) => { if (k === "fillStyle") fills.push(v); return true; }});
  const strips = charts[1].config.plugins[0];
  strips.beforeDraw({chartArea: {left: 70, top: 5, width: 600, height: 80}, ctx, scales: {x: {getPixelForValue: v => 70 + v * 5}}});
  return fills;
}

(async () => {
  let r = await run(false);
  check("light: chart text and grid colours", r.Chart.defaults.color === "#4b5563" && r.Chart.defaults.borderColor === "rgba(0, 0, 0, 0.1)");
  check("light: the speed line is the strong blue", r.charts[0].data.datasets[0].borderColor === "#2563eb");
  check("light: the event markers have a white ring", r.charts[0].data.datasets[1].borderColor === "#ffffff");
  let fills = drawStrips(r.charts);
  check("light: the 'not engaged' band is light grey and the labels dark", fills.includes("#d1d5db") && fills.includes("#374151"));
  check("pilot and driver colours are the same in both themes", fills.includes("#22c55e") && fills.includes("#f97316"));

  r = await run(true);
  check("dark: chart text and grid colours", r.Chart.defaults.color === "#cbd5e1" && r.Chart.defaults.borderColor === "rgba(148, 163, 184, 0.2)");
  check("dark: the speed line is lighter", r.charts[0].data.datasets[0].borderColor === "#60a5fa");
  check("dark: the event markers have a dark ring", r.charts[0].data.datasets[1].borderColor === "#0f172a");
  fills = drawStrips(r.charts);
  check("dark: the band is slate and the labels light", fills.includes("#334155") && fills.includes("#cbd5e1") && !fills.includes("#d1d5db"));
  check("dark: pilot and driver colours unchanged", fills.includes("#22c55e") && fills.includes("#f97316"));

  // toggling while the page is open
  const [speed, strips] = r.charts;
  const updatesBefore = [speed.updates, strips.updates];
  r.b.classes.delete("dark");
  r.b.win.dispatchEvent({type: "themechange", detail: {dark: false}});
  check("switching to light recolours the axes, the grid, the legend and the line", speed.options.scales.x.ticks.color === "#4b5563" && speed.options.scales.y.grid.color === "rgba(0, 0, 0, 0.1)" && speed.options.plugins.legend.labels.color === "#4b5563" && speed.data.datasets[0].borderColor === "#2563eb" && speed.data.datasets[1].borderColor === "#ffffff");
  check("...redraws both charts", speed.updates === updatesBefore[0] + 1 && strips.updates === updatesBefore[1] + 1);
  check("...and the strips draw with the light colours from then on", drawStrips(r.charts).includes("#d1d5db"));
  r.b.classes.add("dark");
  r.b.win.dispatchEvent({type: "themechange", detail: {dark: true}});
  check("switching back to dark recolours again", speed.options.scales.x.ticks.color === "#cbd5e1" && speed.data.datasets[0].borderColor === "#60a5fa" && drawStrips(r.charts).includes("#334155"));
  check("the chart axis titles follow the theme", speed.options.scales.x.title.color === "#cbd5e1");
  summary();
})();
