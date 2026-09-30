// ==UserScript==
// @name         OCI Free-Tier Capacity Grabber
// @namespace    local
// @version      2.0
// @description  Retries "Create instance" on the Oracle Cloud console until A1 capacity frees up.
// @match        https://cloud.oracle.com/*
// @match        https://*.oraclecloud.com/*
// @grant        none
// @run-at       document-idle
// ==/UserScript==

/*
 * HOW TO USE
 *   1. Go to Compute > Instances > Create instance. Fill the form COMPLETELY
 *      (shape, image, VCN/subnet, SSH key). Do NOT click Create yourself.
 *   2. F12 > Console > paste this whole file > Enter.
 *   3. Run  __ociGrab.test()   -- it outlines the button it will click, without clicking.
 *      If it says "no Create button found", fix that before starting.
 *   4. Run  __ociGrab.start()
 *   5. Stop any time with  __ociGrab.stop()
 *
 * It does NOT reload the page. Reloading destroys the form you just filled in.
 */

(() => {
  'use strict';

  const KEY = '__ociGrab.session';

  const CFG = {
    minDelaySec:   45,     // Oracle rate-limits aggressive polling; do not go below ~30
    maxDelaySec:   75,
    settleMs:      9000,   // how long to wait for the API to answer after a click
    maxAttempts:   0,      // 0 = unlimited
    createLabels:  ['create', 'créer', 'crear', 'criar', 'erstellen', 'crea', '作成', '생성'],
    dismissLabels: ['close', 'dismiss', 'ok', 'fermer', 'cerrar', 'schließen', 'chiudi'],
  };

  const CAPACITY_RE  = /out of (host )?capacity|insufficient (host )?capacity|capacity is not (currently )?available|no capacity/i;
  const RATELIMIT_RE = /too ?many ?requests|rate ?limit|429/i;
  const QUOTA_RE     = /limit ?exceeded|quota|service limit|exceeded the maximum/i;
  const AUTHFAIL_RE  = /not ?authoriz|forbidden|invalid ?parameter|notauthorizedornotfound/i;
  const LOGGEDOUT_RE = /sign in to oracle cloud|session (has )?expired|please (sign|log) ?in again/i;

  // ---------------------------------------------------------------- utils

  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const now   = () => new Date().toLocaleTimeString();
  const rnd   = (a, b) => a + Math.random() * (b - a);

  const log  = (...a) => console.log(`%c[oci ${now()}]`, 'color:#4ea1ff;font-weight:bold', ...a);
  const good = (...a) => console.log(`%c[oci ${now()}]`, 'color:#25c26e;font-weight:bold', ...a);
  const warn = (...a) => console.log(`%c[oci ${now()}]`, 'color:#e8a33d;font-weight:bold', ...a);
  const bad  = (...a) => console.log(`%c[oci ${now()}]`, 'color:#ff5f56;font-weight:bold', ...a);

  // Walks open shadow roots too -- the console mixes plain DOM and web components.
  function deepAll(sel, root = document, out = []) {
    try { out.push(...root.querySelectorAll(sel)); } catch (_) {}
    for (const el of root.querySelectorAll('*')) {
      if (el.shadowRoot) deepAll(sel, el.shadowRoot, out);
    }
    return out;
  }

  function clickable(el) {
    if (!el || el.disabled) return false;
    if (el.getAttribute('aria-disabled') === 'true') return false;
    if (/disabled/.test(el.className || '')) return false;
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return false;
    const cs = getComputedStyle(el);
    return cs.visibility !== 'hidden' && cs.display !== 'none' && cs.pointerEvents !== 'none';
  }

  const norm = el =>
    (el.innerText ?? el.textContent ?? el.value ?? '').replace(/\s+/g, ' ').trim().toLowerCase();

  // Exact label match, innermost node only, visible + enabled only.
  function buttonsLabelled(labels) {
    const hits = deepAll('button, [role="button"], input[type="submit"], .oui-button')
      .filter(el => labels.includes(norm(el)));
    // a .oui-button wrapper around a real <button> matches twice -- keep the inner one
    const innermost = hits.filter(el => !hits.some(o => o !== el && el.contains(o)));
    return innermost.filter(clickable);
  }

  function alertText() {
    const regions = deepAll('[role="alert"], [role="status"], .oui-banner, .oui-notification, .oui-toast, [class*="error"], [class*="Error"]');
    const fromRegions = regions.map(n => n.innerText || '').join(' \n ');
    // capacity wording is distinctive enough to also scan the page body as a fallback
    const body = document.body ? document.body.innerText || '' : '';
    return (fromRegions + ' \n ' + body).slice(0, 300000);
  }

  function dismissBanners() {
    let n = 0;
    for (const b of buttonsLabelled(CFG.dismissLabels)) {
      const inAlert = b.closest('[role="alert"],[role="status"],.oui-banner,.oui-notification,.oui-toast');
      if (inAlert) { b.click(); n++; }
    }
    for (const x of deepAll('[aria-label="Close"],[aria-label="Dismiss"],.oui-banner button.close')) {
      if (clickable(x)) { x.click(); n++; }
    }
    return n;
  }

  // ------------------------------------------------------------- outcomes

  function succeeded() {
    if (/\/instances\/ocid1\.instance\./i.test(location.href)) return true;
    const t = alertText();
    return /provisioning/i.test(t) && /instance details|public ip|availability domain/i.test(t);
  }

  function celebrate() {
    try {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      for (let i = 0; i < 6; i++) {
        const o = ctx.createOscillator(), g = ctx.createGain();
        o.frequency.value = 880; o.connect(g); g.connect(ctx.destination);
        g.gain.setValueAtTime(0.25, ctx.currentTime + i * 0.55);
        o.start(ctx.currentTime + i * 0.55);
        o.stop(ctx.currentTime + i * 0.55 + 0.25);
      }
    } catch (_) {}
    try {
      if (Notification.permission === 'granted') new Notification('OCI: instance created');
      else Notification.requestPermission().then(p => p === 'granted' && new Notification('OCI: instance created'));
    } catch (_) {}
    document.title = 'GOT IT -- ' + document.title;
    const el = document.createElement('div');
    el.textContent = 'INSTANCE CREATED';
    el.style.cssText = 'position:fixed;inset:0 0 auto 0;z-index:2147483647;background:#25c26e;color:#062;' +
                       'font:700 22px/60px system-ui,sans-serif;text-align:center;height:60px';
    document.body.appendChild(el);
  }

  // ---------------------------------------------------------------- state

  const load = () => { try { return JSON.parse(sessionStorage.getItem(KEY)) || null; } catch (_) { return null; } };
  const save = s => { try { sessionStorage.setItem(KEY, JSON.stringify(s)); } catch (_) {} };
  const wipe = () => { try { sessionStorage.removeItem(KEY); } catch (_) {} };

  let timer = null;
  let S = { running: false, attempts: 0, capacityMisses: 0, backoff: 1, startedAt: 0 };

  function halt(why, ok = false) {
    S.running = false;
    clearTimeout(timer);
    wipe();
    (ok ? good : bad)(`STOPPED after ${S.attempts} attempt(s): ${why}`);
  }

  function schedule() {
    if (!S.running) return;
    const secs = rnd(CFG.minDelaySec, CFG.maxDelaySec) * S.backoff;
    log(`next attempt in ${Math.round(secs)}s  (tried ${S.attempts}, ` +
        `${Math.round((Date.now() - S.startedAt) / 60000)} min elapsed)`);
    save(S);
    timer = setTimeout(tick, secs * 1000);
  }

  async function tick() {
    if (!S.running) return;

    if (succeeded()) { celebrate(); return halt('instance created', true); }
    if (LOGGEDOUT_RE.test(alertText())) return halt('session expired -- sign in again, then re-run __ociGrab.start()');
    if (CFG.maxAttempts && S.attempts >= CFG.maxAttempts) return halt(`hit maxAttempts (${CFG.maxAttempts})`);

    dismissBanners();                       // clear stale errors so what we read next is fresh
    await sleep(400);

    const btn = buttonsLabelled(CFG.createLabels)[0];
    if (!btn) {
      warn('no enabled Create button -- form closed, invalid, or a field lost its value. Retrying.');
      return schedule();
    }

    S.attempts++;
    btn.click();
    log(`attempt #${S.attempts}: clicked "${norm(btn)}"`);

    await sleep(CFG.settleMs);
    const t = alertText();

    if (succeeded())          { celebrate(); return halt('instance created', true); }
    if (QUOTA_RE.test(t))     return halt('service limit / quota exceeded -- you already hold your free allowance');
    if (AUTHFAIL_RE.test(t))  return halt('auth or bad-parameter error -- the form itself is wrong, retrying will not help');

    if (RATELIMIT_RE.test(t)) {
      S.backoff = Math.min(S.backoff * 2, 8);
      warn(`rate limited (429) -- backing off ${S.backoff}x`);
    } else if (CAPACITY_RE.test(t)) {
      S.capacityMisses++;
      S.backoff = 1;
      warn(`out of capacity (miss #${S.capacityMisses}) -- this is the normal case, keep going`);
    } else {
      S.backoff = 1;
      log('no verdict read yet (slow response?) -- will try again');
    }

    schedule();
  }

  // ------------------------------------------------------------------ api

  const api = {
    get running() { return S.running; },
    get stats()   { return { ...S }; },
    config: CFG,

    test() {
      const hits = buttonsLabelled(CFG.createLabels);
      if (!hits.length) {
        bad('no Create button found. Are you on the filled-in Create instance form?');
        bad('If your console is in another language, add its word to __ociGrab.config.createLabels');
        const sample = deepAll('button').filter(clickable).map(norm).filter(Boolean).slice(0, 40);
        console.log('visible buttons right now:', sample);
        return null;
      }
      const b = hits[0];
      b.style.outline = '4px solid #25c26e';
      b.scrollIntoView({ block: 'center' });
      good(`would click: "${norm(b)}"`, b);
      setTimeout(() => (b.style.outline = ''), 4000);
      return b;
    },

    start() {
      if (S.running) return warn('already running');
      if (!this.test()) return bad('not starting -- fix the button problem first');
      S = { running: true, attempts: 0, capacityMisses: 0, backoff: 1, startedAt: Date.now() };
      good('started. stop with __ociGrab.stop()');
      tick();
    },

    stop() { halt('stopped by user', true); },
  };

  window.__ociGrab = api;

  const prev = load();
  if (prev && prev.running) {                 // survive a page navigation (userscript mode)
    S = { ...prev, backoff: 1 };
    good(`resuming after navigation (${S.attempts} attempts so far)`);
    timer = setTimeout(tick, 5000);
  } else {
    good('loaded. run __ociGrab.test() then __ociGrab.start()');
  }
})();
