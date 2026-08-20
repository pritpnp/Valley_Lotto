/* The scan field, and the three ways Android will let a phone have one.
 *
 * A retail gun is a keyboard. Its keystrokes only reach a web page through a
 * FOCUSED element — but the moment an element is focused, Android wants to raise
 * the on-screen keyboard over the screen you are trying to use. Every trick for
 * having one without the other works on some devices and fails on others:
 *
 *   quiet   — readonly. Android shows no keyboard for a field it can't type
 *             into, and hardware keys still arrive as keydown. Usually the best
 *             of both.
 *   nokb    — inputmode="none". Asks Android for no keyboard. On some phones
 *             this also stops the field being wired for input at all, and the
 *             gun's keystrokes go nowhere — which looks exactly like a broken
 *             scanner.
 *   compat  — an ordinary field. Always receives the gun, at the cost of the
 *             keyboard appearing. Minimising it once is usually enough.
 *
 * There is no way to detect from here which one a given phone needs, so the
 * choice is the clerk's, it takes one tap, and it is remembered on that device.
 * The buffer is built from keydown in every mode, so a readonly field — which
 * never fires an input event — still works.
 */

const SCAN_MODES = [
  {key: "quiet",  label: "No keyboard",
   hint: "The keyboard stays down and the gun still works. Start here."},
  {key: "nokb",   label: "No keyboard, other way",
   hint: "A second way of keeping the keyboard down. Try this if the first "
         + "one doesn't pick up your scans."},
  {key: "compat", label: "Keyboard may show",
   hint: "Always receives the gun, but the keyboard can appear. Minimise it "
         + "once and keep scanning. Use this only if neither of the others works."},
];

/* The Android shell exposes exactly two things, and only to this site. When it's
   there, it settles the keyboard question outright; in a plain browser these are
   no-ops and the field's own attributes do the work. */
function appBridge() {
  return (typeof window !== "undefined" && window.ValleyLotto
          && window.ValleyLotto.hasKeyboardControl) ? window.ValleyLotto : null;
}

function inApp() {
  const b = appBridge();
  try { return !!(b && b.hasKeyboardControl()); } catch (e) { return false; }
}

function appKeyboard(wanted) {
  const b = appBridge();
  if (!b) return;
  try { wanted ? b.showKeyboard() : b.hideKeyboard(); } catch (e) { /* older shell */ }
}

function scanModeKey() {
  return localStorage.getItem("scanMode") || "quiet";
}

function scanModeInfo(key) {
  if (inApp()) return APP_MODE;
  return SCAN_MODES.find(m => m.key === (key || scanModeKey())) || SCAN_MODES[0];
}

const APP_MODE = {
  key: "app", label: "Handled by the app",
  hint: "This device is running the Valley Lotto app, which holds the keyboard "
        + "down itself. Nothing here to change.",
};

class ScanField {
  /* el: the input. onScan(raw): called once per complete scan. */
  constructor(el, onScan, opts) {
    this.el = el;
    this.onScan = onScan;
    this.opts = opts || {};
    this.manual = false;
    this.buf = "";
    this.timer = null;
    this.lastRaw = "";
    this.lastAt = 0;

    el.addEventListener("keydown", e => this.onKey(e, true));
    el.addEventListener("input", () => this.onInput());
    el.addEventListener("blur", () => setTimeout(() => this.focus(), 50));
    document.addEventListener("click", e => {
      if (!e.target.closest("button, a, input, select, textarea, summary")) this.focus();
    });
    window.addEventListener("focus", () => this.focus());
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) this.focus();
    });
    // Belt and braces: a device that routes keys to the page rather than the
    // field still gets its scan through.
    document.addEventListener("keydown", e => {
      if (this.manual || document.activeElement === el) return;
      this.onKey(e, false);
    });

    this.applyMode();
  }

  applyMode() {
    const el = this.el;
    el.removeAttribute("readonly");
    el.removeAttribute("inputmode");
    if (this.manual) {
      el.setAttribute("inputmode", "numeric");
    } else {
      const mode = scanModeKey();
      if (mode === "quiet") el.setAttribute("readonly", "readonly");
      else if (mode === "nokb") el.setAttribute("inputmode", "none");
      // compat: an ordinary field, nothing to set
    }
    el.placeholder = this.manual ? "type the number, then Enter" : "scan here…";
    // In the app, the keyboard is the app's to control — a web page can only
    // ask, and Android is free to ignore it. This is the one that always works.
    appKeyboard(this.manual);
    this.focus();
  }

  focus() {
    if (this.manual || document.activeElement === this.el) return;
    try { this.el.focus({preventScroll: true}); } catch (e) { this.el.focus(); }
  }

  setManual(on) {
    this.manual = on;
    this.buf = "";
    this.el.value = "";
    this.el.blur();
    this.applyMode();
    setTimeout(() => this.el.focus(), 0);
  }

  onKey(e, fromField) {
    if (e.key === "Enter") { e.preventDefault(); this.flush(); return; }

    // An editable field types the character in itself and fires `input`, which
    // is where the buffer comes from. Appending here as well counted every
    // digit twice — "1750" arrived as "11775500".
    const selfTyping = fromField && !this.el.hasAttribute("readonly");
    if (selfTyping) return;

    if (e.key === "Backspace") {
      this.buf = this.buf.slice(0, -1);
      this.show();
      e.preventDefault();
      return;
    }
    if (e.key.length !== 1 || !/[0-9-]/.test(e.key)) return;
    // A readonly field never updates itself, so the buffer is ours to keep.
    this.buf += e.key;
    this.show();
    e.preventDefault();
    this.armAutoSubmit();
  }

  onInput() {
    // Only fires when the field is really editable (manual entry, or compat
    // mode where the browser types into it as well as firing keydown).
    const cleaned = this.el.value.replace(/[^0-9-]/g, "");
    if (cleaned !== this.el.value) this.el.value = cleaned;
    this.buf = cleaned;
    this.armAutoSubmit();
  }

  show() {
    this.el.value = this.buf;
  }

  armAutoSubmit() {
    clearTimeout(this.timer);
    // Fire once the 14 printed digits are in. Real guns send more (16 seen: the
    // printed number plus check digits), so wait a beat for the rest.
    if (/^\d{14,}$/.test(this.buf.replace(/-/g, ""))) {
      this.timer = setTimeout(() => this.flush(), 180);
    }
  }

  flush() {
    clearTimeout(this.timer);
    const raw = (this.buf || this.el.value || "").trim();
    this.buf = "";
    this.el.value = "";
    if (!raw) return;
    const now = Date.now();
    // The same code twice inside a second is the gun firing twice, unless the
    // page is waiting for exactly that as a confirmation.
    if (raw === this.lastRaw && (now - this.lastAt) < 1000 &&
        !(this.opts.allowRepeat && this.opts.allowRepeat())) return;
    this.lastRaw = raw;
    this.lastAt = now;
    this.onScan(raw);
  }
}

function setScanMode(key) {
  localStorage.setItem("scanMode", key);
  return scanModeInfo(key);
}

/* The "scanning isn't working" escape hatch: one tap moves to the next way of
   doing it and says which one is now in use. */
function cycleScanMode() {
  const i = SCAN_MODES.findIndex(m => m.key === scanModeKey());
  return setScanMode(SCAN_MODES[(i + 1) % SCAN_MODES.length].key);
}

/* Draw the choice as three chips, so the setting is something you pick on
   purpose rather than something you stumble onto by tapping repeatedly.
   `onPick` re-applies the mode to the live field. */
function renderScanModes(host, onPick) {
  host.innerHTML = "";
  if (inApp()) {
    const p = document.createElement("p");
    p.style.margin = "0";
    p.textContent = APP_MODE.hint;
    host.appendChild(p);
    return;
  }
  const current = scanModeKey();
  SCAN_MODES.forEach(m => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "modechip" + (m.key === current ? " on" : "");
    b.textContent = m.label;
    b.title = m.hint;
    b.onclick = () => { setScanMode(m.key); renderScanModes(host, onPick); onPick(m); };
    host.appendChild(b);
  });
}
