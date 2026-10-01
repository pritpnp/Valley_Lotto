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

/* Push the keyboard down now and a few more times over the next second.
   Selecting a box raises the keyboard a moment AFTER the box is selected, and
   on some devices (the EDA52) the app never hears about it appearing, so a
   single request made beforehand loses the race. */
function holdKeyboardDown(stillWanted) {
  if (!appBridge()) return;
  // stillWanted: so tapping ⌨ inside that second isn't undone by a late push.
  [0, 120, 350, 800].forEach(ms => setTimeout(() => {
    if (stillWanted()) appKeyboard(false);
  }, ms));
}

function appKeyboard(wanted) {
  const b = appBridge();
  if (!b) return;
  try { wanted ? b.showKeyboard() : b.hideKeyboard(); } catch (e) { /* older shell */ }
}

/* A Honeywell's built-in scanner, through the app. Scans arrive as calls to
   window.onNativeScan (see base.html), not keystrokes, so the scan field never
   needs focus — and an unfocused field is one that can't raise the keyboard. */
function nativeScanner() {
  const b = appBridge();
  try { return !!(b && b.hasNativeScanner && b.hasNativeScanner()); } catch (e) { return false; }
}

/* Once a scan has arrived from the scanner itself on this device, anything that
   looks like the same scan typed in is a second copy, not a second scan. */
function nativeScanSeen() {
  try { return localStorage.getItem("nativeScanSeen") === "1"; } catch (e) { return false; }
}

/* Keep the screen awake for the length of a count, and only for that. In the
   app this is a real window flag; in a browser it's the wake-lock API where the
   browser offers one. Either way it lapses when the page is left, so a device
   sitting on a counter all day still sleeps like any other. */
let _wakeLock = null;

function keepScreenAwake() {
  const b = appBridge();
  if (b && b.keepAwake) {
    try { b.keepAwake(true); return; } catch (e) { /* older shell */ }
  }
  if (navigator.wakeLock && !_wakeLock) {
    navigator.wakeLock.request("screen")
      .then(l => { _wakeLock = l; l.addEventListener("release", () => { _wakeLock = null; }); })
      .catch(() => {});                    // refused (battery saver, no permission)
  }
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
  hint: "This device is running the Valley Lotto app, which keeps the keyboard "
        + "down itself. Nothing here to change.",
};

/* What the device is actually doing, in plain words, for the Scanner check
   page. Each line is [question, answer, good?]. */
function scannerDiagnosis() {
  const b = appBridge();
  const lines = [];
  lines.push(["Running inside the Valley Lotto app", b ? "Yes" : "No — this is a web browser", !!b]);
  if (b) {
    const knows = typeof b.hasNativeScanner === "function";
    let found = false;
    try { found = knows && b.hasNativeScanner(); } catch (e) {}
    lines.push(["App can talk to the built-in scanner",
                !knows ? "No — this is an older copy of the app. Install the newest APK."
                : found ? "Yes — a Honeywell scanner was found"
                : "No — the app didn't find a Honeywell scanner",
                knows && found]);
  }
  const seen = nativeScanSeen();
  lines.push(["Scans arriving straight from the scanner",
              seen ? "Yes — no keyboard needed"
                   : "Not yet — scans are typed into the box instead (that works too)",
              seen]);
  return lines;
}

/* Is the selection in some other box that can be typed in? */
function typingElsewhere(scanEl) {
  const a = document.activeElement;
  if (!a || a === scanEl) return false;
  const editable = (a.tagName === "INPUT" || a.tagName === "TEXTAREA" || a.tagName === "SELECT")
                   && !a.readOnly && !a.disabled && a.offsetParent !== null;
  return editable || a.isContentEditable;
}

const FORM_BITS = "input, textarea, select, label, [contenteditable]";

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
    // The field native scans go to. One scan field per page.
    window.activeScanField = this;

    el.addEventListener("keydown", e => this.onKey(e, true));
    el.addEventListener("input", () => this.onInput());
    el.addEventListener("blur", () => setTimeout(() => this.focus(), 50));
    // Every time the box is selected, in the app, keep the keyboard down.
    el.addEventListener("focus", () => {
      if (!this.manual) holdKeyboardDown(() => !this.manual);
    });
    document.addEventListener("click", e => {
      if (!e.target.closest("button, a, input, select, textarea, summary")) this.focus();
    });
    window.addEventListener("focus", () => this.focus());
    // A tap on a button or a box takes the selection off the scan box; the page
    // then takes it back, and on a phone that round trip pops the keyboard up
    // for an instant. Holding the selection where it is avoids the round trip.
    // The tap itself still happens as normal.
    document.addEventListener("mousedown", e => {
      if (this.manual || document.activeElement !== el) return;
      if (e.target.closest && e.target.closest(FORM_BITS)) return;
      e.preventDefault();
    });
    // In the app, push the keyboard down whenever it comes up uninvited, at any
    // moment, whatever raised it. On the EDA52 it can reappear after the page
    // updates (Skip did it), well after the box was selected. The app's screen
    // shrinks when the keyboard comes up, and that is the one sign of it a page
    // can see.
    if (appBridge()) {
      let tall = window.innerHeight, wide = window.innerWidth;
      window.addEventListener("resize", () => {
        if (window.innerWidth !== wide) {            // turned sideways: new baseline
          wide = window.innerWidth; tall = window.innerHeight; return;
        }
        if (window.innerHeight >= tall) { tall = window.innerHeight; return; }
        const keyboardUp = tall - window.innerHeight > 120;
        const unwanted = () => !this.manual && !typingElsewhere(el);
        if (keyboardUp && unwanted()) holdKeyboardDown(unwanted);
      });
    }
    // Typing in another box: in the app, let the keyboard up for it.
    document.addEventListener("focusin", e => {
      if (e.target !== el && typingElsewhere(el)) appKeyboard(true);
    });
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) this.focus();
    });
    // Belt and braces: a device that routes keys to the page rather than the
    // field still gets its scan through.
    document.addEventListener("keydown", e => {
      if (this.manual || document.activeElement === el) return;
      if (typingElsewhere(el)) return;  // that's someone typing, not a scan
      if (nativeScanSeen()) return;     // the scanner talks to us directly
      this.onKey(e, false);
    });

    // A scan field on screen means a count is happening.
    keepScreenAwake();
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) keepScreenAwake();   // the browser drops it on hide
    });

    this.applyMode();
  }

  applyMode() {
    const el = this.el;
    el.removeAttribute("readonly");
    el.removeAttribute("inputmode");
    if (this.manual) {
      el.setAttribute("inputmode", "numeric");
    } else if (inApp()) {
      // In the app, an ordinary editable field. Some scanners (Honeywell's
      // among them) refuse to type into a read-only one, so that "no keyboard"
      // trick made every scan vanish until the keyboard was opened. Here the
      // keyboard is the app's to push back down, so the field can stay a real
      // one.
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
    // Someone is typing in another box on the page (a box's menu, say).
    // Taking the selection back would make that box impossible to type in.
    if (typingElsewhere(this.el)) return;
    // Once the scanner has really sent a scan straight to the app, the field no
    // longer needs focus, and focus is the one thing that can bring the
    // keyboard up. Until then it keeps focus, so a scanner that only types
    // still has somewhere to type.
    if (nativeScanner() && nativeScanSeen()) return;
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

  /* A complete scan handed over whole, from the device's own scanner. Goes the
     same way as a typed one, so the double-fire guard and the "scan it again
     to confirm" exception apply exactly as they do to a gun. */
  deliver(raw) {
    // The scanner has just proved it talks to the app directly, so let go of
    // the field: nothing needs it selected now, and a selected field is what
    // the keyboard comes up for.
    if (!this.manual && document.activeElement === this.el) this.el.blur();
    clearTimeout(this.timer);
    this.buf = String(raw || "").trim();
    this.flush();
  }

  onInput() {
    if (!this.manual && nativeScanner() && nativeScanSeen()) { this.el.value = ""; return; }
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
