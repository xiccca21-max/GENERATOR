// ==UserScript==
// @name         T-Bank Mock (local test)
// @namespace    tbank-mock-local
// @version      2.0.3
// @description  Подмена ФИО, баланса, операций и чеков PDF на tbank.ru/mybank
// @match        https://www.tbank.ru/mybank/*
// @match        https://tbank.ru/mybank/*
// @match        https://www.tinkoff.ru/mybank/*
// @match        https://tinkoff.ru/mybank/*
// @run-at       document-start
// @inject-into  page
// @grant        none
// ==/UserScript==

(function () {
  'use strict';

  const CONFIG = __PDF_FORGE_CONFIG__;

  if (!CONFIG.enabled) return;

  function tbankMockRun(CFG) {
    if (window.__tbankMockRun) return;
    window.__tbankMockRun = '2.0.3';

    const CONFIG = CFG;
    const win = window;
    const nativeParse = JSON.parse;
    const receiptOpIds = new Map();
    const receiptBlobUrls = {};
    const mockState = (win.__tbankMockState = win.__tbankMockState || {
      spendingDelta: 0,
      incomeDelta: 0,
      todaySpendTotal: null,
      todayIncomeTotal: null,
      opsReady: false,
      baseline: {},
    });

    const MSK_OFFSET_MS = 3 * 60 * 60 * 1000;
    const OPS_WAIT_MS = 1200;
    const RECEIPT_PDF_PATH = '/api/common/v1/payment_receipt_pdf';

    function opDayKey(op) {
      const ms = op?.debitingTime?.milliseconds || op?.operationTime?.milliseconds;
      if (!ms) return '';
      const d = new Date(ms + MSK_OFFSET_MS);
      return `${d.getUTCFullYear()}-${d.getUTCMonth()}-${d.getUTCDate()}`;
    }

    function findTodayInterval(intervals) {
      if (!Array.isArray(intervals)) return null;
      const now = Date.now();
      const current = intervals.find((iv) => iv.start <= now && now <= iv.end);
      if (current) return current;
      return intervals.find((iv) => iv.summary?.value != null) || null;
    }

    function sumPatchedDayTotals(payload, patchedCount) {
      if (!Array.isArray(payload) || !payload.length || !patchedCount) {
        return { spending: null, income: null };
      }
      const todayKey = opDayKey(payload[0]);
      let spending = 0;
      let income = 0;
      let hasSpend = false;
      let hasIncome = false;
      for (const op of payload.slice(0, patchedCount)) {
        if (opDayKey(op) !== todayKey) continue;
        const val = Number(op.amount?.value) || 0;
        if (op.type === 'Debit') {
          spending += val;
          hasSpend = true;
        } else if (op.type === 'Credit') {
          income += val;
          hasIncome = true;
        }
      }
      return { spending: hasSpend ? spending : null, income: hasIncome ? income : null };
    }

    function patchTodayInterval(intervals, delta, baselineKey, absoluteTotal) {
      const today = findTodayInterval(intervals);
      if (!today) return;
      if (absoluteTotal != null) {
        if (!today.summary) {
          const template = intervals.find((iv) => iv.summary)?.summary;
          today.summary = {
            value: absoluteTotal,
            ...(template?.currency ? { currency: template.currency } : {}),
          };
        } else {
          today.summary.value = absoluteTotal;
        }
        return;
      }
      if (!today.summary) return;
      if (mockState.baseline[baselineKey] == null) {
        mockState.baseline[baselineKey] = Number(today.summary.value || 0);
      }
      today.summary.value = mockState.baseline[baselineKey] + (delta || 0);
    }

    function setMoney(field, value) {
      if (field && typeof field === 'object') field.value = Number(value);
    }

    function registerReceiptIds(op, index) {
      const ids = [op.payment?.paymentId, op.id, op.authorizationId, op.operationId?.value];
      ids.filter(Boolean).forEach((id) => receiptOpIds.set(String(id), index));
    }

    function receiptIndexForPaymentId(paymentId) {
      if (paymentId == null) return null;
      const idx = receiptOpIds.get(String(paymentId));
      return idx === undefined ? null : idx;
    }

    function paymentIdFromReceiptUrl(url) {
      const m = url.match(/[?&]paymentId=(\d+)/);
      return m ? m[1] : null;
    }

    function pdfBytesForIndex(index) {
      const b64 = CONFIG.receipts?.[index]?.pdfBase64;
      if (!b64) return null;
      const bin = atob(b64);
      const out = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
      return out;
    }

    function receiptBlobUrl(index) {
      if (receiptBlobUrls[index]) return receiptBlobUrls[index];
      const bytes = pdfBytesForIndex(index);
      if (!bytes) return null;
      const blob = new Blob([bytes], { type: 'application/pdf' });
      receiptBlobUrls[index] = URL.createObjectURL(blob);
      return receiptBlobUrls[index];
    }

    function isReceiptPdfRequest(url, contentType) {
      return url.includes(RECEIPT_PDF_PATH) || (contentType || '').toLowerCase().includes('pdf');
    }

    function profilePhoneE164() {
      const p = CONFIG.profile.mobilePhoneNumber;
      if (!p) return '';
      return `+${p.countryCode}${p.innerCode}${p.number}`;
    }

    function profilePhoneDisplay() {
      const p = CONFIG.profile.mobilePhoneNumber;
      if (!p) return profilePhoneE164();
      const digits = `${p.countryCode || ''}${p.innerCode || ''}${p.number || ''}`.replace(/\D/g, '');
      if (digits.length >= 11 && digits.startsWith('7')) {
        const rest = digits.slice(1);
        const inner = rest.slice(0, 3);
        const num = rest.slice(3);
        if (num.length >= 7) {
          return `+7 ${inner} ${num.slice(0, 3)}-${num.slice(3, 5)}-${num.slice(5, 7)}`;
        }
      }
      return profilePhoneE164();
    }

    function isProfileRoute() {
      const loc = `${location.href}${location.pathname}${location.search}${location.hash}`;
      return /\/profile(?:\/|$|\?|#)/i.test(loc);
    }

    function findPersonalInfoBlock(root) {
      if (!root) return null;
      return (
        root.querySelector('[data-qa-type="mobile-pf-blocks-personal-info"]') ||
        root.querySelector('[data-qa-type^="mobile-pf-blocks-personal-info"]')
      );
    }

    function patchNameDom(root) {
      const mockFirst = CONFIG.profile.firstName;
      if (!mockFirst || !root) return;
      root.querySelectorAll('[data-qa-type="mobile-homer-person-title-text"]').forEach((el) => {
        if (el.textContent !== mockFirst) el.textContent = mockFirst;
      });
    }

    function isPersonalInfoScreen(root) {
      if (!root) return false;
      if (isProfileRoute()) return true;
      const block = findPersonalInfoBlock(root);
      if (block) return true;
      const text = root.innerText || '';
      return text.includes('Ваши данные') && (text.includes('@') || /\+7[\s\d()-]{8,}/.test(text));
    }

    function patchContactDom(root) {
      if (!root || !CONFIG.profile) return;
      if (!isPersonalInfoScreen(root)) return;

      const mockEmail = CONFIG.profile.email;
      const mockPhone = profilePhoneDisplay();
      const mockDigits = mockPhone.replace(/\D/g, '');
      if (!mockEmail && !mockPhone) return;

      const block = findPersonalInfoBlock(root) || root;

      if (mockPhone) {
        block.querySelectorAll('.bbUbkWUND').forEach((el) => {
          if (el.textContent !== mockPhone) el.textContent = mockPhone;
        });
      }

      const walker = document.createTreeWalker(block, NodeFilter.SHOW_TEXT);
      let node;
      while ((node = walker.nextNode())) {
        const parent = node.parentElement;
        if (!parent || parent.closest('input,textarea,[contenteditable="true"]')) continue;
        const text = node.textContent;
        if (!text) continue;
        if (mockEmail && text.includes('@') && !text.includes(mockEmail)) {
          node.textContent = mockEmail;
          continue;
        }
        if (mockPhone) {
          const digits = text.replace(/\D/g, '');
          if (
            digits.length >= 10 &&
            digits !== mockDigits &&
            /^[78]/.test(digits) &&
            /\+?\d[\d\s()-]{9,}/.test(text)
          ) {
            node.textContent = mockPhone;
          }
        }
      }
    }

    function startDomWatcher() {
      if (win.__tbankDomWatcher) return;
      win.__tbankDomWatcher = true;

      const run = () => {
        const root = document.body || document.documentElement;
        if (!root) return;
        patchNameDom(root);
        patchContactDom(root);
      };

      const observer = new MutationObserver(run);
      const attach = () => {
        const root = document.body || document.documentElement;
        if (!root) return;
        run();
        observer.observe(root, { childList: true, subtree: true, characterData: true });
      };

      if (document.body) attach();
      else document.addEventListener('DOMContentLoaded', attach, { once: true });

      setInterval(run, 200);
    }

    function patchPhoneParts(target) {
      if (!target || typeof target !== 'object' || !CONFIG.profile.mobilePhoneNumber) return;
      const phone = CONFIG.profile.mobilePhoneNumber;
      target.countryCode = phone.countryCode;
      target.innerCode = phone.innerCode;
      target.number = phone.number;
    }

    function patchEmailValue(info) {
      if (!info || !CONFIG.profile.email) return;
      if (typeof info.email === 'string' || info.email == null) {
        info.email = CONFIG.profile.email;
        return;
      }
      if (typeof info.email === 'object') {
        info.email.emailAddress = CONFIG.profile.email;
      }
    }

    function patchPersonalInfo(info) {
      if (!info) return;
      const fullName = info.fullName;
      if (fullName) {
        fullName.firstName = CONFIG.profile.firstName;
        fullName.lastName = CONFIG.profile.lastName;
        fullName.patronymic = CONFIG.profile.patronymic;
      }
      patchEmailValue(info);
      if (CONFIG.profile.mobilePhoneNumber) {
        if (!info.mobilePhoneNumber || typeof info.mobilePhoneNumber !== 'object') {
          info.mobilePhoneNumber = {};
        }
        patchPhoneParts(info.mobilePhoneNumber);
        if (info.phoneNumber && typeof info.phoneNumber === 'object') {
          patchPhoneParts(info.phoneNumber);
        }
      }
    }

    function looksLikePhoneParts(obj) {
      return (
        obj &&
        typeof obj === 'object' &&
        'countryCode' in obj &&
        'innerCode' in obj &&
        'number' in obj &&
        typeof obj.countryCode === 'string'
      );
    }

    function patchProfileDeep(node, ctx) {
      if (!node || typeof node !== 'object') return;
      if (Array.isArray(node)) {
        node.forEach((item) => patchProfileDeep(item, ctx));
        return;
      }
      if (node.personalInfo && typeof node.personalInfo === 'object') {
        patchPersonalInfo(node.personalInfo);
        ctx = { inPersonalInfo: true };
      }
      if (ctx.inPersonalInfo && looksLikePhoneParts(node)) {
        patchPhoneParts(node);
      }
      if (ctx.inPersonalInfo && CONFIG.profile.email) {
        if (typeof node.emailAddress === 'string' && node.emailAddress.includes('@')) {
          node.emailAddress = CONFIG.profile.email;
        }
      }
      if (typeof node.phone === 'string' && node.phone.replace(/\D/g, '').length >= 10) {
        const e164 = profilePhoneE164();
        if (e164) node.phone = e164;
      }
      for (const val of Object.values(node)) {
        if (val && typeof val === 'object') patchProfileDeep(val, ctx);
      }
    }

    function patchProfilePayload(data) {
      patchPersonalInfo(data?.payload?.personalInfo);
      patchPersonalInfo(data?.personalInfo);
      if (typeof data?.email === 'string' && CONFIG.profile.email) {
        data.email = CONFIG.profile.email;
      }
      if (typeof data?.mobilePhoneNumber === 'string' && CONFIG.profile.mobilePhoneNumber) {
        data.mobilePhoneNumber = profilePhoneDisplay();
      }
      return data;
    }

    function patchProfileJson(data) {
      patchProfilePayload(data);
      patchProfileDeep(data, {});
      return data;
    }

    function patchDisplayNameRoot(data) {
      if (typeof data?.display_name === 'string') data.display_name = CONFIG.profile.displayName;
      if (typeof data?.payload?.display_name === 'string') {
        data.payload.display_name = CONFIG.profile.displayName;
      }
      if (data?.result?.name && data.result.role === 'client') {
        data.result.name = CONFIG.profile.displayName;
      }
      const e164 = profilePhoneE164();
      if (e164 && typeof data?.result?.phone === 'string') {
        data.result.phone = e164;
      }
      return data;
    }

    function patchAccounts(data) {
      const list = data?.payload;
      if (!Array.isArray(list) || !list[0]?.moneyAmount) return data;
      list[0].moneyAmount.value = CONFIG.balance.value;
      return data;
    }

    function patchHistogram(data) {
      const payload = data?.payload;
      const spending = payload?.spending;
      const earning = payload?.earning;
      const daily = (spending?.intervals?.length || 0) > 1;
      if (spending?.summary) spending.summary.value = CONFIG.spending.monthTotal;
      if (earning?.summary) earning.summary.value = CONFIG.spending.incomeTotal;
      if (daily) {
        patchTodayInterval(spending?.intervals, mockState.spendingDelta, 'spending', mockState.todaySpendTotal);
        patchTodayInterval(earning?.intervals, mockState.incomeDelta, 'income', mockState.todayIncomeTotal);
      }
      return data;
    }

    function patchBrand(op, bank) {
      if (!bank || !op.brand) return;
      if (bank.name) op.brand.name = bank.name;
      if (bank.logoFile) op.brand.logoFile = bank.logoFile;
      if (bank.logo || bank.fileLink) {
        op.brand.logo = bank.logo || bank.fileLink;
        op.brand.fileLink = bank.fileLink || bank.logo;
      }
      if (bank.baseColor) op.brand.baseColor = bank.baseColor;
      if (bank.baseTextColor) op.brand.baseTextColor = bank.baseTextColor;
      if (op.icon && op.brand.logo) op.icon = op.brand.logo;
    }

    function patchPhone(op, phone, bank, description) {
      if (!phone) return;
      if (!op.payment) op.payment = { fieldsValues: {} };
      if (!op.payment.fieldsValues) op.payment.fieldsValues = {};
      op.payment.fieldsValues.pointer = phone;
      if (bank?.name) op.payment.fieldsValues.receiverBankName = bank.name;
      if (description) op.payment.fieldsValues.maskedFIO = description;
    }

    function patchOneOperation(op, item) {
      const abs = Math.abs(Number(item.amount));
      const isDebit = Number(item.amount) < 0;
      op.type = isDebit ? 'Debit' : 'Credit';
      op.description = item.description;
      setMoney(op.amount, abs);
      setMoney(op.accountAmount, abs);
      if (typeof op.cashback === 'number') op.cashback = 0;
      setMoney(op.cashbackAmount, 0);
      const brandLabel = item.brand || item.description;
      if (op.brand) op.brand.name = item.bank?.name || brandLabel;
      if (op.category) op.category.name = item.category || op.category.name;
      if (op.merchantKey) op.merchantKey = brandLabel;
      if (item.bank) patchBrand(op, item.bank);
      if (item.phone) patchPhone(op, item.phone, item.bank, item.description);
      return op;
    }

    function patchOperations(data) {
      const payload = data?.payload;
      if (!Array.isArray(payload) || !CONFIG.operations?.length) {
        mockState.opsReady = true;
        return data;
      }
      const count = Math.min(
        CONFIG.operationsRecentCount ?? CONFIG.operations.length,
        CONFIG.operations.length,
      );
      mockState.spendingDelta = 0;
      mockState.incomeDelta = 0;
      const todayKey = payload[0] ? opDayKey(payload[0]) : '';
      for (let i = 0; i < count; i++) {
        const op = payload[i];
        if (!op) continue;
        const sameDay = !todayKey || opDayKey(op) === todayKey;
        const beforeSpend = sameDay && op.type === 'Debit' ? Number(op.amount?.value) || 0 : 0;
        const beforeIncome = sameDay && op.type === 'Credit' ? Number(op.amount?.value) || 0 : 0;
        registerReceiptIds(op, i);
        patchOneOperation(op, CONFIG.operations[i]);
        if (sameDay) {
          if (op.type === 'Debit') {
            mockState.spendingDelta += (Number(op.amount?.value) || 0) - beforeSpend;
          } else if (op.type === 'Credit') {
            mockState.incomeDelta += (Number(op.amount?.value) || 0) - beforeIncome;
          }
        }
      }
      const dayTotals = sumPatchedDayTotals(payload, count);
      mockState.todaySpendTotal = dayTotals.spending;
      mockState.todayIncomeTotal = dayTotals.income;
      mockState.opsReady = true;
      return data;
    }

    function patchReceiptLinksInJson(node) {
      if (!node || typeof node !== 'object') return;
      if (Array.isArray(node)) {
        node.forEach(patchReceiptLinksInJson);
        return;
      }
      for (const key of Object.keys(node)) {
        const val = node[key];
        if (typeof val === 'string' && val.includes(RECEIPT_PDF_PATH)) {
          const pid = paymentIdFromReceiptUrl(val);
          const idx = receiptIndexForPaymentId(pid);
          const blob = idx !== null ? receiptBlobUrl(idx) : null;
          if (blob) node[key] = blob;
        } else if (val && typeof val === 'object') {
          patchReceiptLinksInJson(val);
        }
      }
    }

    function isPersonalInfoUrl(url) {
      return (
        url.includes('short_personal_info') ||
        url.includes('/auth/personal_info') ||
        url.includes('userInfo')
      );
    }

    function pickJsonTransformer(url) {
      if (isPersonalInfoUrl(url)) return patchProfilePayload;
      if (url.includes('/accounts_light_ib')) return patchAccounts;
      if (url.includes('/operations_histogram')) return patchHistogram;
      if (/\/legacy\/v1\/operations(?:\?|$)/.test(url) && !url.includes('operations_category')) {
        return patchOperations;
      }
      if (url.includes('webim_auth') || url.includes('issueTokenByWEBIMWeb')) {
        return patchDisplayNameRoot;
      }
      if (url.includes('getResponse') || url.includes('session/issueToken')) {
        return patchDisplayNameRoot;
      }
      return null;
    }

    function shouldDeepPatchProfile(url) {
      return (
        (url.includes('tbank.ru') || url.includes('tinkoff.ru')) &&
        !url.includes('operations_histogram') &&
        !url.includes('.js') &&
        !url.includes('/cdn')
      );
    }

    function transformJson(url, raw) {
      const fn = pickJsonTransformer(url);
      if (!fn && !shouldDeepPatchProfile(url)) return raw;
      try {
        const data = nativeParse(raw);
        if (fn) fn(data);
        if (shouldDeepPatchProfile(url)) patchProfileDeep(data, {});
        if (url.includes('operations')) patchReceiptLinksInJson(data);
        return JSON.stringify(data);
      } catch (_) {
        return raw;
      }
    }

    function shouldPatchParsed(text) {
      return (
        typeof text === 'string' &&
        (text.includes('"personalInfo"') ||
          text.includes('mobilePhoneNumber') ||
          text.includes('short_personal_info'))
      );
    }

    function patchJsonParseHook() {
      if (JSON.parse.__tbankMock) return;
      JSON.parse = function (text, reviver) {
        const data = nativeParse.call(this, text, reviver);
        if (shouldPatchParsed(text)) {
          try {
            patchProfileJson(data);
          } catch (_) {
            /* ignore */
          }
        }
        return data;
      };
      JSON.parse.__tbankMock = true;
    }

    function patchResponseJsonHook() {
      if (Response.prototype.json.__tbankMock) return;
      const origJson = Response.prototype.json;
      Response.prototype.json = function (...args) {
        return origJson.apply(this, args).then((data) => {
          const url = String(this.url || '');
          const fn = pickJsonTransformer(url);
          if (fn || shouldDeepPatchProfile(url)) {
            try {
              if (fn) fn(data);
              if (shouldDeepPatchProfile(url)) patchProfileDeep(data, {});
              if (url.includes('operations')) patchReceiptLinksInJson(data);
            } catch (_) {
              /* ignore */
            }
          }
          return data;
        });
      };
      Response.prototype.json.__tbankMock = true;
    }

    function patchResponseTextHook() {
      if (Response.prototype.text.__tbankMock) return;
      const origText = Response.prototype.text;
      Response.prototype.text = function (...args) {
        return origText.apply(this, args).then((text) => {
          const url = String(this.url || '');
          if (!isPersonalInfoUrl(url) && !shouldDeepPatchProfile(url)) return text;
          const ct = this.headers?.get?.('content-type') || '';
          if (ct && !ct.includes('json') && !isPersonalInfoUrl(url)) return text;
          return transformJson(url, text);
        });
      };
      Response.prototype.text.__tbankMock = true;
    }

    function patchXHRBody(xhr, url) {
      if (!url) return;
      try {
        if (xhr.responseType === 'json' && xhr.response && typeof xhr.response === 'object') {
          const fn = pickJsonTransformer(url);
          if (fn) fn(xhr.response);
          if (shouldDeepPatchProfile(url)) patchProfileDeep(xhr.response, {});
          if (url.includes('operations')) patchReceiptLinksInJson(xhr.response);
          return;
        }

        const ct = xhr.getResponseHeader('content-type') || '';
        const canText =
          !xhr.responseType || xhr.responseType === 'text' || xhr.responseType === 'json' || xhr.responseType === '';
        if (!canText || (!ct.includes('json') && !isPersonalInfoUrl(url))) return;

        const raw = xhr.responseType === 'json' ? JSON.stringify(xhr.response) : xhr.responseText;
        if (!raw) return;
        const patched = transformJson(url, raw);
        if (patched === raw) return;

        Object.defineProperty(xhr, 'responseText', { configurable: true, value: patched });
        if (xhr.responseType === 'json') {
          Object.defineProperty(xhr, 'response', { configurable: true, value: nativeParse(patched) });
        } else {
          Object.defineProperty(xhr, 'response', { configurable: true, value: patched });
        }
      } catch (_) {
        /* ignore */
      }
    }

    async function waitForOperationsPatch(url) {
      if (!url.includes('/operations_histogram') || mockState.opsReady) return;
      const started = Date.now();
      while (!mockState.opsReady && Date.now() - started < OPS_WAIT_MS) {
        await new Promise((resolve) => setTimeout(resolve, 15));
      }
    }

    function cloneHeaders(res, contentType) {
      const h = new Headers(res.headers);
      h.delete('content-length');
      h.delete('content-encoding');
      if (contentType) h.set('content-type', contentType);
      return h;
    }

    function swapPdfResponse(url, res) {
      if (!url.includes(RECEIPT_PDF_PATH)) return null;
      const paymentId = paymentIdFromReceiptUrl(url);
      const idx = receiptIndexForPaymentId(paymentId);
      const bytes = idx !== null ? pdfBytesForIndex(idx) : null;
      if (!bytes) return null;
      return new Response(bytes, {
        status: 200,
        statusText: 'OK',
        headers: cloneHeaders(res, 'application/pdf'),
      });
    }

    function patchResponseText(url, text) {
      const patched = transformJson(url, text);
      return patched === text ? null : patched;
    }

    function patchFetch() {
      const orig = win.fetch;
      if (!orig || orig.__tbankMock) return;
      win.fetch = async function (...args) {
        const url = String(args[0]?.url || args[0] || '');
        const res = await orig.apply(this, args);
        const ct = res.headers.get('content-type') || '';
        if (isReceiptPdfRequest(url, ct)) {
          const swapped = swapPdfResponse(url, res);
          if (swapped) return swapped;
          return res;
        }
        if (!pickJsonTransformer(url) && !shouldDeepPatchProfile(url)) return res;
        if (!ct.includes('json') && !isPersonalInfoUrl(url)) return res;
        try {
          if (url.includes('/operations_histogram')) {
            await waitForOperationsPatch(url);
          }
          const text = await res.clone().text();
          const patched = transformJson(url, text);
          if (patched === text) return res;
          return new Response(patched, {
            status: res.status,
            statusText: res.statusText,
            headers: cloneHeaders(res),
          });
        } catch (_) {
          return res;
        }
      };
      win.fetch.__tbankMock = true;
    }

    function patchXHR() {
      const XHR = win.XMLHttpRequest;
      if (!XHR || XHR.__tbankMock) return;
      const origOpen = XHR.prototype.open;
      const origSend = XHR.prototype.send;
      const origAddEventListener = XHR.prototype.addEventListener;

      XHR.prototype.open = function (method, url, ...rest) {
        this.__tbankMockUrl = String(url || '');
        return origOpen.call(this, method, url, ...rest);
      };

      XHR.prototype.addEventListener = function (type, listener, ...rest) {
        if ((type === 'load' || type === 'readystatechange') && typeof listener === 'function') {
          const wrapped = function (...cbArgs) {
            if (this.readyState === 4 && this.status >= 200 && this.status < 300) {
              patchXHRBody(this, this.__tbankMockUrl || '');
            }
            return listener.apply(this, cbArgs);
          };
          return origAddEventListener.call(this, type, wrapped, ...rest);
        }
        return origAddEventListener.call(this, type, listener, ...rest);
      };

      XHR.prototype.send = function (...args) {
        const url = this.__tbankMockUrl || '';
        const origOnReadyStateChange = this.onreadystatechange;
        this.onreadystatechange = function (...cbArgs) {
          if (this.readyState === 4 && this.status >= 200 && this.status < 300) {
            if (url.includes('/operations_histogram')) {
              const started = Date.now();
              while (!mockState.opsReady && Date.now() - started < OPS_WAIT_MS) {
                /* wait */
              }
            }
            patchXHRBody(this, url);
          }
          if (origOnReadyStateChange) origOnReadyStateChange.apply(this, cbArgs);
        };
        return origSend.apply(this, args);
      };
      XHR.__tbankMock = true;
    }

    patchJsonParseHook();
    patchResponseJsonHook();
    patchResponseTextHook();
    patchFetch();
    patchXHR();
    startDomWatcher();
  }

  tbankMockRun(CONFIG);
})();
