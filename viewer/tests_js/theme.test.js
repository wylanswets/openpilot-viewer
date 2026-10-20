// Runs the theme script from base.html in a fake browser.
const {check, summary, scriptById, fakeBrowser} = require("./harness");
const source = scriptById("base.html", "theme-script");

function load(opts) {
  const b = fakeBrowser(opts);
  global.window = b.win; global.document = b.doc; global.localStorage = b.win.localStorage;
  global.CustomEvent = function (type, init) { this.type = type; this.detail = init && init.detail; };
  new Function(source)();
  b.docListeners.DOMContentLoaded.forEach(f => f());   // the page finished loading
  return b;
}
const pressed = b => b.buttons.filter(x => x.attrs["aria-pressed"] === "true").map(x => x.mode).join(",");

let b = load({systemDark: false});
check("auto with a light system: not dark", !b.classes.has("dark") && b.documentElement.style.colorScheme === "light");
check("the page is marked as auto and the Auto button is pressed", b.documentElement.attrs["data-theme-mode"] === "auto" && pressed(b) === "auto");
check("a themechange event says it is light", b.events.length >= 1 && b.events[0].detail.dark === false && b.events[0].detail.mode === "auto");

b = load({systemDark: true});
check("auto with a dark system: dark, and the native controls follow (color-scheme)", b.classes.has("dark") && b.documentElement.style.colorScheme === "dark");

b = load({systemDark: false, storage: {theme: "dark"}});
check("a saved Dark wins over a light system", b.classes.has("dark") && pressed(b) === "dark");
b = load({systemDark: true, storage: {theme: "light"}});
check("a saved Light wins over a dark system", !b.classes.has("dark") && pressed(b) === "light");
b = load({systemDark: true, storage: {theme: "banana"}});
check("an unknown saved value is treated as auto", b.classes.has("dark") && pressed(b) === "auto");

b = load({systemDark: false});
window.setTheme("dark");
check("choosing Dark applies it, saves it and presses the Dark button", b.classes.has("dark") && b.store.theme === "dark" && pressed(b) === "dark");
check("it tells the page with a themechange event", b.events[b.events.length - 1].detail.dark === true && b.events[b.events.length - 1].detail.mode === "dark");
window.setTheme("light");
check("choosing Light turns it off and saves it", !b.classes.has("dark") && b.store.theme === "light");
window.setTheme("auto");
check("choosing Auto forgets the saved choice", !("theme" in b.store) && pressed(b) === "auto");

b = load({systemDark: false});
b.buttons[2].clicks.forEach(f => f());
check("clicking the Dark button in the top bar switches to dark", b.classes.has("dark") && b.store.theme === "dark");
b.buttons[0].clicks.forEach(f => f());
check("clicking Auto goes back to following the system", !b.classes.has("dark") && !("theme" in b.store));

b = load({systemDark: false});
b.media.matches = true; b.mediaListeners.forEach(f => f());
check("while on Auto, a change of the system setting is followed", b.classes.has("dark"));
b = load({systemDark: false, storage: {theme: "light"}});
b.media.matches = true; b.mediaListeners.forEach(f => f());
check("with a manual choice the system setting is ignored", !b.classes.has("dark"));

b = load({systemDark: true, storageThrows: true});
let threw = false;
try { window.setTheme("light"); } catch (e) { threw = true; }
check("a browser that blocks storage still works (not remembered, no error)", !threw && b.classes.has("dark") === false);
b = load({noMatchMedia: true});
check("a browser without matchMedia falls back to light", !b.classes.has("dark"));

summary();
