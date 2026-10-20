// Tiny helpers for the browser-script tests (run with: node viewer/tests_js/theme.test.js).
const fs = require("fs");
const path = require("path");

const TEMPLATES = path.join(__dirname, "..", "templates");
let passed = 0, failed = 0;

function check(name, ok) {
  if (ok) { passed++; console.log("ok   " + name); } else { failed++; console.log("FAIL " + name); }
}
function summary() {
  console.log("\n" + passed + " passed, " + failed + " failed");
  process.exit(failed ? 1 : 0);
}
function read(rel) { return fs.readFileSync(path.join(TEMPLATES, rel), "utf8"); }

// the inline script with this id (or the last inline script) of a template
function scriptById(rel, id) {
  const m = new RegExp('<script id="' + id + '">\\n([\\s\\S]*?)</script>').exec(read(rel));
  if (!m) throw new Error("no script " + id + " in " + rel);
  return m[1];
}
function lastInlineScript(rel) {
  const all = [...read(rel).matchAll(/<script>\n([\s\S]*?)<\/script>/g)];
  return all[all.length - 1][1].replace("{% url 'drive_summary' drive.route_id %}", "/drive/x/summary.json");
}

// a fake browser: <html> with a class list, localStorage, matchMedia, events, theme buttons
function fakeBrowser(opts) {
  opts = opts || {};
  const classes = new Set(opts.htmlClasses || []);
  const listeners = {};      // window events
  const docListeners = {};   // document events
  const events = [];
  const store = Object.assign({}, opts.storage || {});
  const mediaListeners = [];
  const media = {matches: !!opts.systemDark, addEventListener: (t, f) => mediaListeners.push(f)};
  const buttons = ["auto", "light", "dark"].map(mode => {
    const attrs = {"data-theme-choice": mode, "aria-pressed": "false"};
    const clicks = [];
    return {mode, attrs, clicks,
      getAttribute: k => attrs[k], setAttribute: (k, v) => { attrs[k] = v; },
      addEventListener: (t, f) => { if (t === "click") clicks.push(f); }};
  });
  const documentElement = {
    classList: {toggle: (c, on) => { if (on) classes.add(c); else classes.delete(c); }, contains: c => classes.has(c), add: c => classes.add(c), remove: c => classes.delete(c)},
    style: {}, attrs: {}, setAttribute(k, v) { this.attrs[k] = v; },
  };
  const localStorage = opts.storageThrows ? {
    getItem() { throw new Error("blocked"); }, setItem() { throw new Error("blocked"); }, removeItem() { throw new Error("blocked"); },
  } : {getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }, removeItem: k => { delete store[k]; }};
  const win = {
    matchMedia: opts.noMatchMedia ? undefined : () => media,
    localStorage,
    addEventListener: (t, f) => { (listeners[t] = listeners[t] || []).push(f); },
    dispatchEvent: e => { events.push(e); (listeners[e.type] || []).forEach(f => f(e)); },
  };
  const doc = {
    documentElement,
    querySelectorAll: sel => (sel.indexOf("data-theme-choice") >= 0 ? buttons : []),
    addEventListener: (t, f) => { (docListeners[t] = docListeners[t] || []).push(f); },
  };
  return {win, doc, documentElement, classes, events, store, buttons, media, mediaListeners, docListeners, listeners};
}

module.exports = {check, summary, read, scriptById, lastInlineScript, fakeBrowser};
