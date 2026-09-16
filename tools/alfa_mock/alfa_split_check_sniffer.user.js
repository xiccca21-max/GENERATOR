// ==UserScript==
// @name         Alfa «Разделить чек» sniffer
// @namespace    alfa-split-sniffer
// @version      1.0.0
// @description  Ловит клики / fetch / XHR / URL на флоу «Разделить чек» — dump в JSON
// @match        *://web.alfabank.ru/*
// @match        *://www.web.alfabank.ru/*
// @match        *://click.alfabank.ru/*
// @match        *://online.alfabank.ru/*
// @match        *://*.alfabank.ru/*
// @run-at       document-start
// @grant        none
// ==/UserScript==

(() => {
  'use strict';
  if (window.__ALFA_SPLIT) return;

  const log = [];
  const t0 = Date.now();

  const textOf = (el) =>
    ((el && (el.innerText || el.textContent)) || '')
      .replace(/\s+/g, ' ')
      .trim()
      .slice(0, 240);

  const pathOf = (el) => {
    const parts = [];
    let n = el;
    for (let i = 0; n && i < 8; i++, n = n.parentElement) {
      const id = n.id ? `#${n.id}` : '';
      const cls =
        n.className && typeof n.className === 'string'
          ? '.' + n.className.trim().split(/\s+/).slice(0, 2).join('.')
          : '';
      parts.unshift(`${n.tagName}${id}${cls}`);
    }
    return parts.join('>');
  };

  const screenShot = () => {
    const title = document.querySelector(
      'h1,h2,[class*="title"],[data-test-id*="title"]',
    );
    const btns = [...document.querySelectorAll('button,a,[role="button"]')]
      .map((b) => textOf(b))
      .filter(Boolean)
      .slice(0, 40);
    return {
      url: location.href,
      title: textOf(title) || document.title,
      buttons: btns,
      bodySlice: textOf(document.body).slice(0, 800),
    };
  };

  const interestingRe =
    /раздел|split|share.?check|check.?split|money\.alfabank|пополнен|контакт|запрос|send.?request/i;

  const bump = () => {
    const n = document.getElementById('__alfa_split_n');
    if (n) n.textContent = String(log.length);
  };

  const push = (type, data) => {
    const row = { t: Date.now() - t0, type, ...data };
    log.push(row);
    bump();
    try {
      if (interestingRe.test(JSON.stringify(data).slice(0, 2500))) {
        console.log('%c[split]', 'color:#fc0;font-weight:bold', type, data);
      }
    } catch (_) {
      /* ignore */
    }
  };

  const dumpObj = () => ({
    capturedAt: new Date().toISOString(),
    href: location.href,
    ua: navigator.userAgent,
    events: log,
    interesting: log.filter((e) => {
      try {
        return interestingRe.test(JSON.stringify(e).slice(0, 3000));
      } catch (_) {
        return false;
      }
    }),
  });

  const downloadDump = () => {
    const blob = new Blob([JSON.stringify(dumpObj(), null, 2)], {
      type: 'application/json',
    });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `alfa_split_check_${Date.now()}.json`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  };

  const copyDump = async () => {
    const s = JSON.stringify(dumpObj(), null, 2);
    try {
      await navigator.clipboard.writeText(s);
      alert('Dump в буфере (' + log.length + ' событий).\nВставь сюда в чат.');
    } catch (_) {
      console.log(s);
      alert('Clipboard blocked — смотри консоль, скопируй вручную.');
    }
  };

  const mountPanel = () => {
    if (document.getElementById('__alfa_split_panel')) return;
    if (!document.documentElement) return;
    const box = document.createElement('div');
    box.id = '__alfa_split_panel';
    box.innerHTML = `
      <div style="position:fixed;z-index:2147483647;right:8px;bottom:8px;background:#111;color:#fff;
        border:1px solid #fc0;border-radius:12px;padding:10px 12px;font:12px/1.3 -apple-system,sans-serif;
        box-shadow:0 8px 24px rgba(0,0,0,.5);max-width:220px">
        <b style="color:#fc0">Разделить чек · sniffer</b>
        <div style="opacity:.7;margin:4px 0 8px">событий: <span id="__alfa_split_n">0</span></div>
        <button id="__alfa_split_dump" type="button" style="width:100%;padding:8px;border:0;border-radius:8px;background:#fc0;color:#000;font-weight:700;cursor:pointer">
          Скачать dump
        </button>
        <button id="__alfa_split_copy" type="button" style="width:100%;margin-top:6px;padding:8px;border:0;border-radius:8px;background:#333;color:#fff;cursor:pointer">
          copy() в буфер
        </button>
      </div>`;
    document.documentElement.appendChild(box);
    document.getElementById('__alfa_split_dump').onclick = downloadDump;
    document.getElementById('__alfa_split_copy').onclick = copyDump;
  };

  // --- clicks ---
  document.addEventListener(
    'click',
    (e) => {
      const t =
        e.target && e.target.closest
          ? e.target.closest(
              'button,a,[role="button"],[data-test-id],div,span',
            )
          : e.target;
      if (!t) return;
      const txt = textOf(t);
      push('click', {
        text: txt,
        path: pathOf(t),
        href: (t && t.href) || null,
        testId: t.getAttribute && t.getAttribute('data-test-id'),
        screen: screenShot(),
      });
    },
    true,
  );

  // --- SPA url ---
  const wrapHist = (name) => {
    const orig = history[name];
    if (typeof orig !== 'function') return;
    history[name] = function (...args) {
      const r = orig.apply(this, args);
      push('history', {
        method: name,
        url: location.href,
        screen: screenShot(),
      });
      return r;
    };
  };
  wrapHist('pushState');
  wrapHist('replaceState');
  window.addEventListener('popstate', () => {
    push('popstate', { url: location.href, screen: screenShot() });
  });

  // --- fetch ---
  const nf = window.fetch;
  window.fetch = async function (input, init) {
    const url =
      typeof input === 'string' ? input : (input && input.url) || '';
    const method =
      (init && init.method) || (input && input.method) || 'GET';
    let reqBody = null;
    try {
      if (init && init.body) {
        reqBody =
          typeof init.body === 'string'
            ? init.body.slice(0, 4000)
            : '[non-string]';
      }
    } catch (_) {
      /* ignore */
    }
    const started = Date.now() - t0;
    try {
      const res = await nf.apply(this, arguments);
      const clone = res.clone();
      let body = null;
      const ct = clone.headers.get('content-type') || '';
      try {
        if (/json/i.test(ct)) body = await clone.json();
        else body = (await clone.text()).slice(0, 2000);
      } catch (_) {
        /* ignore */
      }
      push('fetch', {
        started,
        method,
        url: String(url).slice(0, 500),
        status: res.status,
        reqBody,
        bodyPreview: body,
      });
      return res;
    } catch (err) {
      push('fetch-err', {
        started,
        method,
        url: String(url).slice(0, 500),
        err: String(err),
      });
      throw err;
    }
  };

  // --- XHR ---
  const XO = XMLHttpRequest.prototype.open;
  const XS = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url) {
    this.__split = { method, url: String(url) };
    return XO.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function (body) {
    const meta = this.__split || {};
    this.addEventListener('load', () => {
      let preview = null;
      try {
        preview = this.responseText
          ? this.responseText.slice(0, 4000)
          : null;
        try {
          preview = JSON.parse(this.responseText);
        } catch (_) {
          /* keep text */
        }
      } catch (_) {
        /* ignore */
      }
      push('xhr', {
        method: meta.method,
        url: String(meta.url || '').slice(0, 500),
        status: this.status,
        reqBody: body && String(body).slice(0, 2000),
        bodyPreview: preview,
      });
    });
    return XS.apply(this, arguments);
  };

  const boot = () => {
    mountPanel();
    push('boot', { screen: screenShot() });
    console.log(
      '%c[split] sniffer ON',
      'color:#fc0;font-weight:bold',
      '→ пройди флоу, потом «Скачать dump»',
    );
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', mountPanel, {
      once: true,
    });
    window.addEventListener('load', boot, { once: true });
  } else {
    boot();
  }
  // панель может появиться раньше body
  setInterval(mountPanel, 1000);

  window.__ALFA_SPLIT = {
    log,
    dump: dumpObj,
    screen: screenShot,
    download: downloadDump,
    copy: copyDump,
  };
})();
