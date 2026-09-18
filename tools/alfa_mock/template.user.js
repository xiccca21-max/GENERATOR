// ==UserScript==
// @name         Alfa-Bank Mock (local test)
// @namespace    alfa-mock-local
// @version      1.3.58
// @description  Подмена ФИО, баланса, операций и чеков PDF в кабинете Альфа
// @match        *://web.alfabank.ru/*
// @match        *://www.web.alfabank.ru/*
// @match        *://click.alfabank.ru/*
// @match        *://online.alfabank.ru/*
// @match        *://*.alfabank.ru/*
// @include      *://*.alfabank.ru/*
// @run-at       document-start
// @inject-into  page
// @grant        none
// ==/UserScript==

(function () {
  'use strict';

  const CONFIG = __PDF_FORGE_CONFIG__;
  if (!CONFIG.enabled) return;

  function alfaMockRun(CFG, win) {
    win = win || window;
    if (win.__alfaMockRun) return;
    win.__alfaMockRun = '1.3.60';
    try {
      if (sessionStorage.getItem('__ALFA_MOCK_VER') !== '1.3.58') {
        sessionStorage.removeItem('__ALFA_MOCK_OPS');
        sessionStorage.setItem('__ALFA_MOCK_VER', '1.3.58');
      }
    } catch (_) { /* ignore */ }

    const CONFIG = CFG;
    const nativeParse = JSON.parse;
    const receiptOpIds = new Map();
    const receiptBlobUrls = {};
    const MOCK_OPS = {};
    try {
      const savedOps = sessionStorage.getItem('__ALFA_MOCK_OPS');
      if (savedOps) Object.assign(MOCK_OPS, nativeParse(savedOps));
      Object.keys(MOCK_OPS).forEach((id) => {
        if (MOCK_OPS[id] && MOCK_OPS[id].__alfaMockNid) delete MOCK_OPS[id];
      });
      const savedSk = sessionStorage.getItem('__ALFA_DETAIL_SKELETON__');
      if (savedSk && !win.__ALFA_DETAIL_SKELETON__) {
        const parsed = nativeParse(savedSk);
        const cat = String((parsed && parsed.category && (parsed.category.name || parsed.category)) || '');
        if (parsed && Array.isArray(parsed.fields) && parsed.fields.length && /сбп/i.test(cat) && parsed.bottomBadge) {
          win.__ALFA_DETAIL_SKELETON__ = parsed;
        }
      }
      const savedDonors = sessionStorage.getItem('__ALFA_SBP_DONORS');
      if (savedDonors && !win.__ALFA_SBP_DONORS) win.__ALFA_SBP_DONORS = nativeParse(savedDonors) || [];
    } catch (_) { /* ignore */ }

    function persistMocks() {
      try {
        sessionStorage.setItem('__ALFA_MOCK_OPS', JSON.stringify(MOCK_OPS));
        if (win.__ALFA_DETAIL_SKELETON__) {
          sessionStorage.setItem('__ALFA_DETAIL_SKELETON__', JSON.stringify(win.__ALFA_DETAIL_SKELETON__));
        }
        if (win.__ALFA_SBP_DONORS) {
          sessionStorage.setItem('__ALFA_SBP_DONORS', JSON.stringify(win.__ALFA_SBP_DONORS));
        }
      } catch (_) { /* ignore */ }
    }
    const NAME_STOP = new Set([
      'главный', 'платежи', 'история', 'чаты', 'поиск', 'кредит', 'деньги',
      'продукт', 'кэшбэк', 'альфа', 'онлайн', 'текущий', 'счёт', 'счет',
      'карта', 'мир', 'поездку', 'наличными', 'новый', 'доступен', 'вам',
      'на', 'от', 'до', 'по', 'из', 'за', 'все', 'расходы', 'доходы',
      'сегодня', 'вчера', 'переводы', 'игра', 'шаблон', 'категория',
      'аналитика', 'квитанция', 'операция', 'фильтр', 'счета', 'карты',
      'справки', 'выписки', 'справка', 'выписка', 'получить', 'период',
      'выбрать', 'документы', 'документ', 'сегодня',
    ]);

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
        return `+7 ${rest.slice(0, 3)} ${rest.slice(3, 6)}-${rest.slice(6, 8)}-${rest.slice(8, 10)}`;
      }
      return profilePhoneE164();
    }

    function looksLikeFirstName(text) {
      const t = String(text || '').trim();
      if (!/^[А-ЯЁ][а-яё]{1,20}$/.test(t)) return false;
      return !NAME_STOP.has(t.toLowerCase());
    }

    // Live dump first names seen in cabinet / chat («Инесса»).
    const LIVE_CLIENT_FIRST = new Set(['инесса']);

    function forgeFirstName() {
      return String((CONFIG.profile && CONFIG.profile.firstName) || '').trim();
    }

    function rewriteClientFirstInText(text) {
      const first = forgeFirstName();
      if (!first || typeof text !== 'string' || !text) return text;
      let out = text;
      // «Приветствую, Инесса!» / «Здравствуйте, Инесса.»
      out = out.replace(
        /(Приветствую|Здравствуй(?:те)?|Добрый\s+(?:день|вечер|утро)|Привет)(,\s*)([А-ЯЁ][а-яё]{1,20})/g,
        (_, greet, sep) => `${greet}${sep}${first}`,
      );
      // Bare live dump first name (word boundary).
      LIVE_CLIENT_FIRST.forEach((live) => {
        if (live === first.toLowerCase()) return;
        const re = new RegExp(
          `(^|[^А-Яа-яЁё])(${live[0].toUpperCase()}${live.slice(1)}|${live})(?=$|[^А-Яа-яЁё])`,
          'g',
        );
        out = out.replace(re, (_, pre) => `${pre}${first}`);
      });
      return out;
    }

    function isChatView() {
      try {
        const t = ((document.body && document.body.innerText) || '').replace(/\s+/g, ' ');
        if (/Чат с банком|Альфа-Помощник|На связи Альфа/i.test(t)) return true;
        if (/\/chat|\/chats|support-chat|messenger|помощник/i.test(String(location.href || ''))) return true;
      } catch (_) { /* ignore */ }
      return false;
    }

    function formatAlfaNumber(rubles) {
      const [int, frac] = Math.abs(Number(rubles)).toFixed(2).split('.');
      return `${int.replace(/\B(?=(\d{3})+(?!\d))/g, '\u00a0')},${frac}`;
    }

    function formatAlfaMoney(rubles, signed) {
      const n = Number(rubles);
      const sign = signed && n < 0 ? '−' : '';
      return `${sign}${formatAlfaNumber(Math.abs(n))}\u00a0₽`;
    }

    function accountLast4() {
      const raw = String((CONFIG.account && CONFIG.account.last4) || '').replace(/\D/g, '');
      return raw.slice(-4);
    }

    function applyAccountMask(obj) {
      const last4 = accountLast4();
      if (!last4 || !obj || typeof obj !== 'object') return;
      Object.keys(obj).forEach((key) => {
        const val = obj[key];
        if (typeof val !== 'string') return;
        if (/[·•*]{1,2}\s*\d{4}/.test(val) || /masked|shortnumber|last4|tail|subtitle|displaynumber/i.test(key)) {
          obj[key] = val.replace(/([·•*]{1,2}\s*)\d{4}/g, '$1' + last4);
        }
        if ((key === 'last4' || key === 'lastFour' || key === 'accountLast4') && /^\d{4}$/.test(val)) {
          obj[key] = last4;
        }
      });
    }

    function patchAccountDom(root) {
      const last4 = accountLast4();
      if (!last4 || !root) return;
      const walker = root.createTreeWalker
        ? root.createTreeWalker(root, NodeFilter.SHOW_TEXT)
        : document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
      let node;
      while ((node = walker.nextNode())) {
        const parent = node.parentElement;
        if (!parent || parent.closest('input,textarea,[contenteditable="true"]')) continue;
        const raw = node.textContent || '';
        if (!/[·•*]{1,2}\s*\d{4}/.test(raw)) continue;
        const around = ((parent.closest('a,button,div,li,section,article') || parent).innerText || '');
        if (!/текущ|сч[её]т|получить|период|выписк/i.test(around) && !isStatementFormView()) continue;
        const next = raw.replace(/([·•*]{1,2}\s*)\d{4}/g, '$1' + last4);
        if (next !== raw) node.textContent = next;
      }
    }

    function isStatementsListView() {
      try {
        if (isReceiptView()) return false;
        const t = ((document.body && document.body.innerText) || '').replace(/\s+/g, ' ');
        return /Справки и выписки/i.test(t) && /Выписка по сч[её]ту/i.test(t);
      } catch (_) {
        return false;
      }
    }

    function isStatementFormView() {
      try {
        if (isReceiptView()) return false;
        const t = ((document.body && document.body.innerText) || '').replace(/\s+/g, ' ');
        if (/получить квитанцию/i.test(t)) return false;
        if (!/Выписка по сч[её]ту/i.test(t)) return false;
        if (!/\bПолучить\b/i.test(t)) return false;
        return /Период|Подтверждает операции/i.test(t);
      } catch (_) {
        return false;
      }
    }

    function urlLooksLikeReceipt(url) {
      const u = String(url || '');
      if (/квитан|receipt|cheque|payment_receipt/i.test(u)) return true;
      return /operation-info.*documents\/pdf|operations\/[^/?#]+\/documents\/pdf/i.test(u);
    }

    function urlLooksLikeStatement(url) {
      const u = String(url || '');
      if (urlLooksLikeReceipt(u)) return false;
      return /statement|выписк|справк|certificate/i.test(u);
    }

    function statementPdfBytes() {
      const b64 = CONFIG.statement && CONFIG.statement.pdfBase64;
      if (!b64) return null;
      try {
        const bin = atob(b64);
        const out = new Uint8Array(bin.length);
        for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
        return out;
      } catch (_) {
        return null;
      }
    }

    function openMockStatement() {
      if (!statementPdfBytes()) {
        alert('В userscript нет PDF выписки.\n/forge alfa: после чеков операций пришли PDF выписки → замени скрипт → перезапусти Safari.');
        return false;
      }
      showReceiptViewer('st');
      return true;
    }

    function isStatementListRow(el) {
      if (!isStatementsListView() || !el) return false;
      let n = el;
      for (let i = 0; i < 14 && n && n !== document.body; i++) {
        const t = (n.innerText || n.textContent || '').replace(/\s+/g, ' ').trim();
        if (/Выписка по сч[её]ту/i.test(t) && /сегодня/i.test(t) && t.length < 100) {
          if (/Справки и выписки/i.test(t)) {
            n = n.parentElement;
            continue;
          }
          return true;
        }
        n = n.parentElement;
      }
      return false;
    }

    function rublesToField(current, rubles) {
      const abs = Math.abs(Number(rubles));
      if (current == null || current === '') return abs;
      if (typeof current === 'string') {
        const n = Number(String(current).replace(',', '.').replace(/\s/g, ''));
        if (!Number.isFinite(n)) return String(abs);
        const converted = rublesToField(n, abs);
        return String(current).includes(',')
          ? String(converted).replace('.', ',')
          : String(converted);
      }
      const n = Number(current);
      if (!Number.isFinite(n)) return abs;
      if (Number.isInteger(n) && Math.abs(n) >= 100 && !String(current).includes('.')) {
        return Math.round(abs * 100);
      }
      return abs;
    }

    function setMoney(field, rubles) {
      if (field == null || typeof field !== 'object' || Array.isArray(field)) return;
      const abs = Math.abs(Number(rubles));
      if ('minorUnits' in field && typeof field.minorUnits === 'number' && field.minorUnits > 0 && field.minorUnits <= 10000 && 'value' in field) {
        setAlfaAmountField(field, abs);
        return;
      }
      if ('value' in field) {
        if (field.value && typeof field.value === 'object' && !Array.isArray(field.value)) {
          setMoney(field.value, rubles);
        } else if (typeof field.value === 'number' || typeof field.value === 'string') {
          field.value = rublesToField(field.value, abs);
        }
      }
      if ('amount' in field) {
        if (field.amount && typeof field.amount === 'object' && !Array.isArray(field.amount)) {
          setMoney(field.amount, rubles);
        } else if (typeof field.amount === 'number' || typeof field.amount === 'string') {
          field.amount = rublesToField(field.amount, abs);
        }
      }
      if ('rubles' in field) field.rubles = abs;
    }

    function assignMoney(obj, key, rubles) {
      if (!obj || !(key in obj)) return;
      const cur = obj[key];
      if (typeof cur === 'number' || typeof cur === 'string') {
        obj[key] = rublesToField(cur, rubles);
        return;
      }
      setMoney(cur, rubles);
    }

    function isMoneyObject(obj) {
      if (!obj || typeof obj !== 'object' || Array.isArray(obj)) return false;
      const hasCur = 'currency' in obj || 'currencyCode' in obj || 'isoCode' in obj;
      const hasVal = 'value' in obj || 'amount' in obj || 'minorUnits' in obj;
      return hasCur && hasVal;
    }

    function looksLikePaymentTemplate(obj) {
      if (!obj || typeof obj !== 'object') return false;
      return obj.frequency != null || obj.frequencyDay != null || obj.templateDescription != null;
    }

    function looksLikeAccountProduct(obj) {
      if (!obj || typeof obj !== 'object' || Array.isArray(obj)) return false;
      if (obj.direction || obj.bottomBadge || obj.mcc) return false;
      const cat = obj.category && (obj.category.name || obj.category);
      if (typeof cat === 'string' && /сбп|перевод|маркет/i.test(cat)) return false;
      const name = String(obj.name || obj.title || obj.description || obj.shortName || obj.productName || '');
      if (/текущ|накопит|кредитн|сч[её]т|депозит|alfa.?сч|альфа.?сч/i.test(name)) return true;
      if (obj.maskedPan || obj.maskedNumber || obj.cardNumber || obj.productType || obj.productKind) return true;
      if (obj.accountNumber || obj.iban || obj.eqId || obj.accountType) return true;
      return false;
    }

    function looksLikeOperation(obj) {
      if (!obj || typeof obj !== 'object' || Array.isArray(obj)) return false;
      if (looksLikePaymentTemplate(obj)) return false;
      if (looksLikeAccountProduct(obj)) return false;
      const hasAmount = obj.amount != null || obj.moneyAmount != null || obj.sum != null;
      const hasTitle =
        obj.title != null ||
        obj.name != null ||
        obj.description != null ||
        obj.purpose != null ||
        obj.counterpart != null ||
        obj.direction != null ||
        obj.subtitle != null ||
        obj.merchant != null ||
        obj.operationDate != null ||
        obj.dateTime != null ||
        obj.iconUrl != null ||
        obj.logoUrl != null;
      return hasAmount && hasTitle;
    }

    function scoreOpList(arr) {
      if (!Array.isArray(arr) || !arr.length || typeof arr[0] !== 'object') return -1;
      const first = arr[0];
      if (looksLikePaymentTemplate(first)) return 0;
      if (!looksLikeOperation(first)) return -1;
      let score = 1;
      if (first.logoUrl || first.iconUrl) score += 2;
      if (first.subtitle || first.subTitle) score += 1;
      if (first.date || first.operationDate || first.dateTime) score += 2;
      if (first.direction || first.deepLink || first.category) score += 3;
      score += Math.min(arr.length, 30);
      return score;
    }

    function looksLikeProduct(obj) {
      if (!obj || typeof obj !== 'object' || Array.isArray(obj)) return false;
      if (looksLikeOperation(obj)) return false;
      if (looksLikeAccountProduct(obj) && (obj.amount != null || obj.balance != null || obj.moneyAmount != null || obj.available != null || obj.ownFunds != null)) {
        return true;
      }
      const type = String(obj.productType || obj.type || obj.kind || obj.productKind || '');
      if (/ACCOUNT|CARD|DEPOSIT|CURRENT|SAVING|EE|EH|FY|SE/i.test(type)) return true;
      if (obj.number || obj.accountNumber || obj.account || obj.accountId || obj.maskedNumber || obj.maskedPan || obj.cardNumber) {
        return obj.amount != null || obj.balance != null || obj.moneyAmount != null || obj.available != null;
      }
      const desc = String(obj.description || obj.title || obj.name || '');
      return /сч[её]т|карт/i.test(desc) && (obj.amount != null || obj.balance != null);
    }

    function findAllOperationLists(node) {
      const found = [];
      const seen = new Set();
      function consider(arr) {
        if (!arr || seen.has(arr)) return;
        if (scoreOpList(arr) <= 0) return;
        seen.add(arr);
        found.push(arr);
      }
      function walk(cur, d) {
        if (!cur || typeof cur !== 'object' || d > 10) return;
        if (Array.isArray(cur)) {
          consider(cur);
          return;
        }
        for (const key of [
          'operations', 'transactions', 'operationList', 'feed',
          'items', 'content', 'history', 'list', 'payload', 'data',
        ]) {
          if (Array.isArray(cur[key])) consider(cur[key]);
        }
        for (const val of Object.values(cur)) {
          if (val && typeof val === 'object') walk(val, d + 1);
        }
      }
      walk(node, 0);
      found.sort((a, b) => scoreOpList(b) - scoreOpList(a) || b.length - a.length);
      return found;
    }

    function findOperationsArray(node, depth) {
      void depth;
      return findAllOperationLists(node)[0] || null;
    }

    function registerReceiptIds(op, index) {
      [op.id, op.operationId, op.reference, op.transactionId, op.documentId, op.paymentId]
        .filter((id) => id != null && id !== '')
        .forEach((id) => receiptOpIds.set(String(id), index));
    }

    Object.keys(MOCK_OPS).forEach((id) => {
      const op = MOCK_OPS[id];
      if (op) registerReceiptIds(op, op.__alfaMockIndex || 0);
    });

    function receiptIndexForId(id) {
      if (id == null) return null;
      const idx = receiptOpIds.get(String(id));
      return idx === undefined ? null : idx;
    }

    function idFromUrl(url) {
      const s = String(url || '');
      const keys = ['operationId', 'operation_id', 'transactionId', 'documentId', 'paymentId', 'id', 'reference'];
      for (const key of keys) {
        const m = s.match(new RegExp(`[?&/]${key}=([^&/?#]+)`, 'i'));
        if (m) return decodeURIComponent(m[1]);
      }
      const tail = s.match(/\/(?:operations|receipts?|cheques?|documents)\/([A-Za-z0-9._-]+)/i);
      return tail ? tail[1] : null;
    }

    function pdfBytesForIndex(index) {
      if (index === 'st' || index === 'statement') return statementPdfBytes();
      const nOps = (CONFIG.operations || []).length;
      if (typeof index === 'number' && nOps && index >= nOps) return null;
      const b64 = CONFIG.receipts?.[index]?.pdfBase64;
      if (!b64) return null;
      // Do NOT reject when statement accidentally equals receipt (forge dup PDF) —
      // that made «Получить квитанцию» alert «нет вложенного PDF» with PDF present.
      try {
        const bin = atob(b64);
        const out = new Uint8Array(bin.length);
        for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
        return out;
      } catch (_) {
        return null;
      }
    }

    function firstReceiptIndexWithPdf() {
      const n = Math.min(3, (CONFIG.operations || []).length || 0);
      for (let i = 0; i < n; i++) {
        if (pdfBytesForIndex(i)) return i;
      }
      return null;
    }

    function resolveReceiptIndex() {
      const ops = CONFIG.operations || [];
      const body = (document.body && document.body.innerText) || '';
      for (let i = 0; i < ops.length; i++) {
        const title = ops[i] && ops[i].description;
        if (title && body.indexOf(String(title)) >= 0 && pdfBytesForIndex(i)) return i;
      }
      for (let i = 0; i < ops.length; i++) {
        if (pdfBytesForIndex(i)) return i;
      }
      return firstReceiptIndexWithPdf();
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
      const u = String(url || '').toLowerCase();
      const ct = String(contentType || '').toLowerCase();
      if (/\.(png|jpe?g|gif|svg|webp|ico|js|css|woff2?|ttf|map)(\?|$)/i.test(u)) return false;
      if (/logo|icon|static\/|servicecdn|\/image/i.test(u) && !/\/receipt|квитан/i.test(u)) return false;
      if (/operations-history\/operations\/[^/?#]+\/?(\?|$)/i.test(u) && !/receipt|cheque|квитан|document/i.test(u)) return false;
      // Alfa often serves HTML «Перевод по СБП» — swap every documents/pdf hit.
      const pathOk = /\/receipts?(?:\/|\?|$)|\/cheques?(?:\/|\?|$)|квитанц|payment_receipt|document-pdf|receipt\.pdf|cheque\.pdf|documents\/pdf|operation-info-api.*pdf|pdf_viewer/i.test(u);
      const ctOk = /application\/pdf/.test(ct);
      return pathOk || (ctOk && /receipt|cheque|квитан|operation|document/i.test(u));
    }

    function applyNameToObject(obj) {
      if (!obj || typeof obj !== 'object' || Array.isArray(obj)) return;
      // Chat support / operator cards — never overwrite with client forge face.
      if (looksLikeStaffPersonObject(obj)) return;
      const hasGiven = 'firstName' in obj || 'firstname' in obj || 'first_name' in obj || 'givenName' in obj;
      const hasFamily = 'lastName' in obj || 'lastname' in obj || 'last_name' in obj || 'surname' in obj || 'familyName' in obj;
      const hasFull = typeof obj.fullName === 'string' || typeof obj.displayName === 'string' || typeof obj.fio === 'string'
        || typeof obj.name === 'string' || typeof obj.userName === 'string' || typeof obj.clientName === 'string';
      if (!hasGiven && !hasFamily && !hasFull) return;
      if ('firstName' in obj) obj.firstName = CONFIG.profile.firstName;
      if ('firstname' in obj) obj.firstname = CONFIG.profile.firstName;
      if ('first_name' in obj) obj.first_name = CONFIG.profile.firstName;
      if ('givenName' in obj) obj.givenName = CONFIG.profile.firstName;
      if ('lastName' in obj) obj.lastName = CONFIG.profile.lastName;
      if ('lastname' in obj) obj.lastname = CONFIG.profile.lastName;
      if ('last_name' in obj) obj.last_name = CONFIG.profile.lastName;
      if ('surname' in obj) obj.surname = CONFIG.profile.lastName;
      if ('familyName' in obj) obj.familyName = CONFIG.profile.lastName;
      if ('patronymic' in obj) obj.patronymic = CONFIG.profile.patronymic;
      if ('middleName' in obj) obj.middleName = CONFIG.profile.patronymic;
      const face = profileFaceName();
      if (typeof obj.fullName === 'string') obj.fullName = face;
      if (typeof obj.displayName === 'string') obj.displayName = face;
      if (typeof obj.display_name === 'string') obj.display_name = face;
      if (typeof obj.fio === 'string') obj.fio = CONFIG.profile.displayName || face;
      if (typeof obj.name === 'string' && looksLikePersonTitle(obj.name)) obj.name = face;
      if (typeof obj.userName === 'string' && looksLikePersonTitle(obj.userName)) obj.userName = face;
      if (typeof obj.clientName === 'string' && looksLikePersonTitle(obj.clientName)) obj.clientName = face;
    }

    function looksLikeStaffPersonObject(obj) {
      if (!obj || typeof obj !== 'object') return false;
      const keys = Object.keys(obj).join(' ').toLowerCase();
      const role = String(obj.role || obj.type || obj.kind || obj.position || obj.title || obj.jobTitle || '').toLowerCase();
      if (/operator|employee|agent|manager|support|advisor|author|helper|staff|bot|оператор|сотрудник|менеджер|помощник|консультант/.test(role)) {
        return true;
      }
      if ('operatorId' in obj || 'employeeId' in obj || 'agentId' in obj || 'supportId' in obj) return true;
      if ('isOperator' in obj || 'isEmployee' in obj || 'isAgent' in obj || 'isBot' in obj) return true;
      if (/operator|employee|agent|manager|support|advisor|author|sender|helper|сотрудник|оператор|менеджер|помощник|консультант/.test(keys)) {
        if ('operatorId' in obj || 'employeeId' in obj || 'agentId' in obj || 'authorId' in obj || 'senderId' in obj) {
          return true;
        }
      }
      // Message bubbles / authors: body text + author, or avatar without client phone.
      const hasBody = typeof obj.text === 'string' || typeof obj.message === 'string' || typeof obj.body === 'string'
        || typeof obj.content === 'string';
      const hasAuthor = typeof obj.author === 'string' || typeof obj.authorName === 'string'
        || typeof obj.senderName === 'string' || typeof obj.operatorName === 'string'
        || (obj.author && typeof obj.author === 'object') || (obj.sender && typeof obj.sender === 'object');
      if (hasBody && hasAuthor) return true;
      const hasAvatar = 'avatar' in obj || 'photo' in obj || 'photoUrl' in obj || 'imageUrl' in obj
        || 'avatarUrl' in obj || 'iconUrl' in obj;
      const hasClientBits = 'mobilePhoneNumber' in obj || 'phone' in obj || 'accounts' in obj
        || 'inn' in obj || 'clientId' in obj || 'customerId' in obj || 'passport' in obj;
      const hasPerson = 'firstName' in obj || 'lastName' in obj || 'fullName' in obj || 'displayName' in obj
        || (typeof obj.name === 'string' && looksLikePersonTitle(obj.name));
      if (hasPerson && hasAvatar && !hasClientBits) return true;
      return false;
    }

    function profileFaceName() {
      const first = (CONFIG.profile.firstName || '').trim();
      const last = (CONFIG.profile.lastName || '').trim();
      // Profile screen shows «Имя Фамилия» (as live dump).
      if (first && last) return `${first} ${last}`;
      return (CONFIG.profile.displayName || first || last || '').trim();
    }

    function mockDocs() {
      if (win.__ALFA_MOCK_DOCS) return win.__ALFA_MOCK_DOCS;
      let h = 2166136261;
      const seed = String(mockFingerprint() || CONFIG.profile.displayName || 'docs');
      for (let i = 0; i < seed.length; i++) {
        h ^= seed.charCodeAt(i);
        h = Math.imul(h, 16777619);
      }
      const rnd = () => {
        h = (Math.imul(h, 1664525) + 1013904223) >>> 0;
        return h;
      };
      let series = String(1000 + (rnd() % 9000));
      let number = String(100000 + (rnd() % 900000));
      // Never echo known live values from dumps / screenshots.
      if (series === '9219') series = String(1000 + ((rnd() + 17) % 9000));
      if (number === '652102') number = String(100000 + ((rnd() + 91) % 900000));
      let inn = '';
      for (let i = 0; i < 12; i++) inn += String(rnd() % 10);
      if (inn.startsWith('1650') || inn.startsWith('0')) {
        inn = String(2 + (rnd() % 7)) + inn.slice(1);
      }
      win.__ALFA_MOCK_DOCS = {
        passport: `${series} ${number}`,
        passportSeries: series,
        passportNumber: number,
        inn,
      };
      return win.__ALFA_MOCK_DOCS;
    }

    function applyDocsToObject(obj) {
      if (!obj || typeof obj !== 'object' || Array.isArray(obj)) return;
      const docs = mockDocs();
      Object.keys(obj).forEach((key) => {
        const lk = String(key).toLowerCase();
        const val = obj[key];
        if (typeof val !== 'string') return;
        const compact = val.replace(/\s/g, '');
        if (/passportseries|seriespassport|docseries/.test(lk) && /^\d{4}$/.test(compact)) {
          obj[key] = docs.passportSeries;
        } else if (/passportnumber|numberpassport|docnumber/.test(lk) && /^\d{6}$/.test(compact)) {
          obj[key] = docs.passportNumber;
        } else if (/passport|паспорт/.test(lk) && /^\d{4}\d{6}$/.test(compact)) {
          obj[key] = docs.passport;
        } else if ((/^inn$|innnumber|taxpayer|taxid|инн/.test(lk) || lk === 'inn') && /^\d{10,12}$/.test(compact)) {
          obj[key] = docs.inn;
        } else if (/^\d{4}\s+\d{6}$/.test(val.trim())) {
          obj[key] = docs.passport;
        }
      });
    }

    function cloneJson(value) {
      return nativeParse(JSON.stringify(value));
    }

    function isMockOp(op) {
      if (!op || typeof op !== 'object') return false;
      if (op.__alfaMockOp) return true;
      const id = String(op.id || op.operationId || op.transactionId || '');
      return id.indexOf('alfa-mock-') === 0;
    }

    function blobOp(op) {
      try {
        return JSON.stringify(op).toLowerCase();
      } catch (_) {
        return '';
      }
    }

    function pickVisuals(op) {
      const out = {};
      (function walk(node, path, depth) {
        if (!node || depth > 6) return;
        if (typeof node === 'string' && (/^https?:/i.test(node) || node.indexOf('data:image') === 0) && /logo|icon|image|svg|png|webp|brand|pictogram/i.test(path + node)) {
          out[path] = node.slice(0, 220);
        } else if (node && typeof node === 'object') {
          Object.keys(node).forEach((key) => walk(node[key], path ? path + '.' + key : key, depth + 1));
        }
      })(op, '', 0);
      return out;
    }

    function shortBank(bank) {
      if (!bank) return '';
      if (bank.short) return bank.short;
      const n = String(bank.name || '');
      if (/ozon|озон/i.test(n)) return 'Озон';
      if (/сбер/i.test(n)) return 'Сбербанк';
      if (/альфа/i.test(n)) return 'Альфа-Банк';
      if (/т-банк|t-bank|тинькоф/i.test(n)) return 'Т-Банк';
      if (/райф/i.test(n)) return 'Райффайзен';
      if (/втб/i.test(n)) return 'ВТБ';
      if (/бспб|bspb|санкт.?петербург|банк\s*спб/i.test(n)) return 'Банк Санкт-Петербург';
      return n.split('(')[0].trim();
    }

    const logoBlobs = {};

    function svgLogo(bg, inner) {
      const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="14" fill="${bg}"/>${inner}</svg>`;
      const cacheKey = bg + '|' + inner;
      if (logoBlobs[cacheKey]) return logoBlobs[cacheKey];
      // data: URI — blob: часто режется CSP в Alfa Online / Safari
      const uri = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg);
      logoBlobs[cacheKey] = uri;
      return uri;
    }

    const BANK_LOGOS = {
      'Сбер': svgLogo('#21A038', '<path fill="#fff" d="M16 33c9-14 22-18 33-11-9 2-16 9-20 20-3-4-7-8-13-9z"/>'),
      'Ozon': svgLogo('#005BFF', '<text x="32" y="40" text-anchor="middle" font-size="15" font-family="Arial,sans-serif" font-weight="700" fill="#fff">OZON</text>'),
      'Альфа': svgLogo('#EF3124', '<text x="32" y="43" text-anchor="middle" font-size="28" font-family="Arial,sans-serif" font-weight="700" fill="#fff">A</text>'),
      'Т-Банк': svgLogo('#FFDD2D', '<text x="32" y="45" text-anchor="middle" font-size="32" font-family="Arial,sans-serif" font-weight="700" fill="#333">T</text>'),
      'Райффайзен': svgLogo('#FFE600', '<text x="32" y="43" text-anchor="middle" font-size="26" font-family="Arial,sans-serif" font-weight="700" fill="#000">R</text>'),
      'ВТБ': svgLogo('#0A2896', '<text x="32" y="42" text-anchor="middle" font-size="18" font-family="Arial,sans-serif" font-weight="700" fill="#fff">ВТБ</text>'),
      'БСПБ': svgLogo('#FFFFFF', '<circle cx="32" cy="18" r="10" fill="#E51837"/><ellipse cx="32" cy="36" rx="14" ry="5" fill="#E51837"/><ellipse cx="32" cy="46" rx="8" ry="2" fill="#C4102E"/>'),
    };

    // Packed SBP corner badge — CDN often 404 / CSP; detail view must not stay broken.
    const SBP_BADGE_SRC = svgLogo(
      '#6B2BD9',
      '<text x="32" y="40" text-anchor="middle" font-size="18" font-family="Arial,sans-serif" font-weight="700" fill="#fff">СБП</text>',
    );

    function isSbpBadgeUrl(url) {
      return /sbp|nspk|faster.?pay|сбп|payment.?system|fps/i.test(String(url || ''));
    }

    function sbpBadgeSrc() {
      const packed = CONFIG.bankLogos && CONFIG.bankLogos.СБП;
      return packed || SBP_BADGE_SRC;
    }

    function bankLogoKey(bank) {
      if (!bank) return '';
      const n = [
        bank.short, bank.categoryName, bank.name, bank.key,
        Array.isArray(bank.needles) ? bank.needles.join(' ') : '',
      ].map((x) => String(x || '')).join(' ');
      if (/сбер|sber/i.test(n)) return 'Сбер';
      if (/ozon|озон/i.test(n)) return 'Ozon';
      if (/альфа|alfa/i.test(n)) return 'Альфа';
      if (/т-?банк|t-?bank|тинькоф/i.test(n)) return 'Т-Банк';
      if (/райф/i.test(n)) return 'Райффайзен';
      if (/втб|vtb/i.test(n)) return 'ВТБ';
      if (/бспб|bspb|санкт.?петербург|банк\s*спб/i.test(n)) return 'БСПБ';
      if (/псб|promsvyaz|промсвяз/i.test(n)) return 'ПСБ';
      return '';
    }

    function bankLogoSrc(bank) {
      if (!bank) return '';
      const key = typeof bankLogoKey === 'function' ? bankLogoKey(bank) : '';
      const packed = (CONFIG.bankLogos && key && CONFIG.bankLogos[key]) || '';
      if (packed) return packed;
      if (key && BANK_LOGOS[key]) return BANK_LOGOS[key];
      const url = bank.logoUrl || bank.iconUrl || bank.logo || bank.fileLink || '';
      if (url && !/not_found|placeholder|missing/i.test(url)) return url;
      return '';
    }

    function bankNeedles(bank) {
      const extra = Array.isArray(bank?.needles) ? bank.needles : [];
      const fromName = [bank?.short, bank?.name, bank?.key]
        .filter(Boolean)
        .map((s) => String(s).toLowerCase());
      return extra.concat(fromName).filter(Boolean);
    }

    function looksLikeSbpTransfer(op) {
      const cat = String((op && op.category && (op.category.name || op.category)) || '');
      return /сбп/i.test(cat);
    }

    function isCardKind(item) {
      const k = String((item && item.transferKind) || '').toLowerCase();
      return k === 'card' || k === 'карта';
    }

    /** Live dump: phone Alfa→Alfa = «Переводы · Альфа-Банк», no СБП, Alfa logo, no badge. */
    function isAlfaInternalKind(item) {
      if (!item || isCardKind(item)) return false;
      return bankLogoKey(item.bank) === 'Альфа';
    }

    function looksLikeAlfaInternalTransfer(op) {
      if (!op || looksLikeSbpTransfer(op)) return false;
      const cat = String((op.category && (op.category.name || op.category)) || '');
      if (/сбп/i.test(cat)) return false;
      if (/^Переводы\s*·\s*Альфа/i.test(cat)) return true;
      const logo = String(op.logoUrl || op.iconUrl || '');
      if (/alfabank/i.test(logo) && /^Переводы\s*·/i.test(cat) && looksLikePersonTitle(op.title || op.name)) {
        return true;
      }
      return false;
    }

    function looksLikeCardTransfer(op) {
      if (!op || looksLikeSbpTransfer(op)) return false;
      if (looksLikeAlfaInternalTransfer(op)) return false;
      const cat = String((op.category && (op.category.name || op.category)) || '');
      const name = String(op.name || op.title || '');
      if (/альфа-?карта/i.test(name)) return true;
      if (/^Переводы\s*·/i.test(cat) && !/сбп/i.test(cat) && (op.mcc != null || op.status || op.smartVistaFrontReference)) {
        return true;
      }
      return false;
    }

    function rememberSbpDonors(list) {
      if (!Array.isArray(list)) return;
      if (!win.__ALFA_SBP_DONORS) win.__ALFA_SBP_DONORS = [];
      list.forEach((op) => {
        if (!op || isMockOp(op) || !looksLikeSbpTransfer(op) || !op.id) return;
        if (win.__ALFA_SBP_DONORS.some((d) => String(d.id) === String(op.id))) return;
        win.__ALFA_SBP_DONORS.push(cloneJson(op));
      });
      if (win.__ALFA_SBP_DONORS.length) persistMocks();
    }

    function sbpDonorPool(list) {
      const pool = (list || []).filter((op) => op && !isMockOp(op) && looksLikeSbpTransfer(op));
      (win.__ALFA_SBP_DONORS || []).forEach((op) => {
        if (op && op.id && !pool.some((p) => String(p.id) === String(op.id))) pool.push(op);
      });
      return pool;
    }

    function findSbpTemplate(list) {
      return sbpDonorPool(list)[0] || (list || []).find((op) => {
        if (!op || isMockOp(op)) return false;
        const cat = String((op.category && (op.category.name || op.category)) || '');
        return /перевод/i.test(cat) && !/кэшбэк|cashback|аналитик/i.test(cat);
      }) || null;
    }


    function findCardTemplate(list) {
      return (list || []).find((op) => op && !isMockOp(op) && looksLikeCardTransfer(op)) || null;
    }

    function findAnyHistoryDonor(list) {
      return findSbpTemplate(list)
        || findCardTemplate(list)
        || (list || []).find((op) => op && !isMockOp(op) && looksLikeOperation(op))
        || null;
    }

    function syntheticHistoryOp(item) {
      const nid = newOpId();
      const logo = bankLogoSrc(item && item.bank) || '';
      const rub = Math.abs(Number(item && item.amount) || 0);
      const kopecks = Math.round(rub * 100);
      const neg = Number(item && item.amount) < 0;
      const iso = (item && item.dateTime) || formatAlfaIso((item && item.atMs) || Date.now());
      const title = (item && item.description) || 'Альфа-карта МИР';
      const bankName = (item && item.bank && (item.bank.categoryName || item.bank.short)) || shortBank(item && item.bank);
      let catLine;
      if (isCardKind(item)) catLine = bankName ? ('Переводы \u00b7 ' + bankName) : 'Переводы';
      else catLine = bankName ? ('Переводы \u00b7 СБП \u00b7 ' + bankName) : 'Переводы \u00b7 СБП';
      return {
        id: nid,
        operationId: newUuid(),
        clickReference: nid,
        reference: nid,
        title: title,
        name: title,
        direction: neg ? 'EXPENSE' : 'INCOME',
        dateTime: iso,
        amount: { value: neg ? -kopecks : kopecks, currency: 'RUR', minorUnits: 100 },
        category: { name: catLine },
        logoUrl: logo,
        iconUrl: logo,
        status: 'SUCCESS',
        subtitle: '',
      };
    }

    function findBrandDonor(list, bank) {
      const sbp = sbpDonorPool(list);
      if (!sbp.length) return null;
      const needles = bankNeedles(bank);
      if (needles.length) {
        const byCat = sbp.find((op) => {
          const cat = String((op.category && (op.category.name || op.category)) || '').toLowerCase();
          return needles.some((n) => n && cat.indexOf(String(n).toLowerCase()) >= 0);
        });
        if (byCat) return byCat;
      }
      return sbp[0] || null;
    }

    function newOpId() {
      const d = new Date();
      const y = String(d.getFullYear()).slice(-2);
      const m = String(d.getMonth() + 1).padStart(2, '0');
      const day = String(d.getDate()).padStart(2, '0');
      const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789';
      let tail = '';
      for (let i = 0; i < 8; i++) tail += alphabet[Math.floor(Math.random() * alphabet.length)];
      return '1' + y + m + day + 'MOCOIP' + tail;
    }

    function newUuid() {
      const hex = '0123456789ABCDEF';
      const n = (len) => {
        let s = '';
        for (let i = 0; i < len; i++) s += hex[Math.floor(Math.random() * 16)];
        return s;
      };
      return n(8) + '-' + n(4) + '-' + n(4) + '-' + n(4) + '-' + n(12);
    }

    function collectIdStrings(op) {
      const out = [];
      ['id', 'operationId', 'transactionId', 'reference', 'documentId', 'paymentId', 'clickReference'].forEach((key) => {
        const val = op && op[key];
        if (val == null || val === '') return;
        if (typeof val === 'object' && val.value != null) out.push(String(val.value));
        else if (typeof val === 'string' || typeof val === 'number') out.push(String(val));
      });
      return out;
    }

    function replaceIdStrings(node, oldIds, nid, depth) {
      if (!node || typeof node !== 'object' || depth > 10 || !oldIds.length) return;
      const walkVal = (val, setter) => {
        if (typeof val === 'string') {
          let next = val;
          oldIds.forEach((oid) => {
            if (oid && next.indexOf(oid) >= 0) next = next.split(oid).join(nid);
          });
          if (next !== val) setter(next);
        } else if (val && typeof val === 'object') {
          replaceIdStrings(val, oldIds, nid, depth + 1);
        }
      };
      if (Array.isArray(node)) {
        node.forEach((item, i) => walkVal(item, (next) => { node[i] = next; }));
        return;
      }
      Object.keys(node).forEach((key) => {
        walkVal(node[key], (next) => { node[key] = next; });
      });
    }

    function reId(op) {
      const oldIds = collectIdStrings(op).filter((id) => id && id.indexOf('alfa-mock-') !== 0);
      const nid = newOpId();
      const uuid = newUuid();
      replaceIdStrings(op, oldIds, nid, 0);
      if ('id' in op) op.id = nid;
      if ('operationId' in op) op.operationId = uuid;
      ['transactionId', 'documentId', 'paymentId'].forEach((key) => {
        if (!(key in op) || op[key] == null) return;
        if (typeof op[key] === 'object' && 'value' in op[key]) op[key].value = nid;
        else if (typeof op[key] === 'string') op[key] = nid;
      });
      op.__alfaMockOp = true;
      op.__alfaMockNid = nid;
    }

    function copyBrandVisuals(donor, target) {
      if (!donor || !target) return;
      [
        'icon', 'iconUrl', 'iconURL', 'logo', 'logoUrl', 'logoURL', 'image', 'imageUrl',
        'imageURL', 'brand', 'merchant', 'logoLight', 'logoDark', 'pictogram', 'avatar',
        'partner', 'partnerLogo', 'brandLogo', 'logoId', 'iconId', 'iconName', 'merchantLogo',
      ].forEach((key) => {
        if (key in donor) target[key] = cloneJson(donor[key]);
      });
      (function walk(src, dst, depth) {
        if (!src || !dst || typeof src !== 'object' || typeof dst !== 'object' || depth > 8) return;
        if (Array.isArray(src)) return;
        Object.keys(src).forEach((key) => {
          const sv = src[key];
          if (typeof sv === 'string' && /^https?:/i.test(sv) && /logo|icon|image|svg|png|webp|brand|pictogram/i.test(key + sv) && !isSbpBadgeUrl(key + sv)) {
            dst[key] = sv;
          } else if (sv && typeof sv === 'object' && !Array.isArray(sv) && dst[key] && typeof dst[key] === 'object') {
            walk(sv, dst[key], depth + 1);
          }
        });
      })(donor, target, 0);
    }

    function replaceLogoUrls(op, logoUrl) {
      if (!op || !logoUrl) return;
      const badge = sbpBadgeSrc();
      (function walk(node, depth) {
        if (!node || typeof node !== 'object' || depth > 8) return;
        if (Array.isArray(node)) {
          node.forEach((item) => walk(item, depth + 1));
          return;
        }
        Object.keys(node).forEach((key) => {
          const val = node[key];
          if (typeof val === 'string' && (/^https?:/i.test(val) || val.indexOf('data:image') === 0) && /logo|icon|image|svg|png|webp|brand|pictogram/i.test(key + val)) {
            if (isSbpBadgeUrl(key + val)) {
              // CDN SBP badge often broken inside detail — pin packed badge.
              node[key] = badge;
            } else {
              node[key] = logoUrl;
            }
          } else if (val && typeof val === 'object') {
            walk(val, depth + 1);
          }
        });
      })(op, 0);
    }

    function ensureSbpBadgeOnDetail(inner, item) {
      if (!inner || isCardKind(item) || isAlfaInternalKind(item)) return;
      const badge = sbpBadgeSrc();
      const b = inner.bottomBadge;
      if (b && typeof b === 'object' && !Array.isArray(b)) {
        if (!b.iconUrl && !b.logoUrl && !b.imageUrl) b.iconUrl = badge;
        else {
          ['iconUrl', 'logoUrl', 'imageUrl', 'icon', 'logo'].forEach((k) => {
            if (typeof b[k] === 'string' && (isSbpBadgeUrl(b[k]) || /^https?:/i.test(b[k]))) b[k] = badge;
          });
        }
      }
      // Some shells keep a second logo slot for the SBP corner mark.
      ['badgeUrl', 'badgeIconUrl', 'rightLogoUrl', 'additionalLogoUrl', 'partnerLogoUrl'].forEach((k) => {
        if (k in inner) inner[k] = badge;
      });
    }

    function applyBankBrand(op, bank) {
      if (!bank || !op) return;
      const logo = bankLogoSrc(bank);
      if (logo) op.logoUrl = logo;
    }

    function setCategoryLine(op, item) {
      const cat = item.category || 'Переводы';
      const bankName = (item.bank && (item.bank.categoryName || item.bank.short)) || shortBank(item.bank);
      // Live dump: card→other = «Переводы · Т-Банк»;
      // Alfa→Alfa phone = «Переводы · Альфа-Банк» (no СБП);
      // SBP other bank = «Переводы · СБП · Банк».
      let line;
      if (isCardKind(item) || isAlfaInternalKind(item)) {
        line = bankName ? `${cat} · ${bankName}` : cat;
      } else {
        line = bankName ? `${cat} · СБП · ${bankName}` : `${cat} · СБП`;
      }
      if (op.category && typeof op.category === 'object') {
        if (typeof op.category.name === 'string') op.category.name = line;
      } else {
        op.category = line;
      }
    }

    function looksLikePersonTitle(text) {
      const t = String(text || '').trim();
      if (!t || t.length > 48 || /•|₽/.test(t)) return false;
      return /^[А-ЯЁA-Z]/.test(t);
    }

    function formatAlfaIso(ms) {
      const d = new Date(Number(ms) + 3 * 3600 * 1000);
      const p = (n) => String(n).padStart(2, '0');
      return d.getUTCFullYear() + '-' + p(d.getUTCMonth() + 1) + '-' + p(d.getUTCDate()) + 'T' + p(d.getUTCHours()) + ':' + p(d.getUTCMinutes()) + ':' + p(d.getUTCSeconds()) + '.000+0300';
    }

    function stampOpTime(op, newest, item) {
      const ms = Number(item && item.atMs);
      const useMs = Number.isFinite(ms) && ms > 0 ? ms : Date.now();
      const iso = (item && item.dateTime) || formatAlfaIso(useMs);
      op.dateTime = iso;
      ['date', 'operationDate', 'dateTime', 'timestamp', 'createdAt', 'executedAt', 'operationTime', 'time'].forEach((key) => {
        const sample = key === 'dateTime' ? op[key] : (op[key] != null ? op[key] : newest && newest[key]);
        if (sample == null && key !== 'dateTime') return;
        if (typeof sample === 'number' || (sample == null && key === 'dateTime' && typeof op[key] === 'number')) {
          op[key] = (typeof sample === 'number' && sample < 1e12) ? Math.floor(useMs / 1000) : useMs;
        } else if (typeof sample === 'string' || key === 'dateTime') {
          if (typeof sample === 'string' && /^\d+$/.test(sample)) op[key] = String(sample.length > 10 ? useMs : Math.floor(useMs / 1000));
          else op[key] = iso;
        } else if (sample && typeof sample === 'object') {
          op[key] = cloneJson(sample);
          if ('milliseconds' in op[key]) op[key].milliseconds = useMs;
          if ('epochMillis' in op[key]) op[key].epochMillis = useMs;
          if (typeof op[key].value === 'string') op[key].value = iso;
          else if (typeof op[key].value === 'number') op[key].value = useMs;
        }
      });
      op.dateTime = iso;
    }

    function firstMoneyNumber(field) {
      if (typeof field === 'number' && Number.isFinite(field)) return field;
      if (typeof field === 'string' && /^-?\d/.test(field.trim())) {
        const n = Number(field.replace(/\s/g, '').replace(',', '.'));
        return Number.isFinite(n) ? n : null;
      }
      if (!field || typeof field !== 'object') return null;
      const keys = ['value', 'amount', 'minorUnits', 'sum', 'rubles', 'major', 'minor'];
      for (let i = 0; i < keys.length; i++) {
        const n = firstMoneyNumber(field[keys[i]]);
        if (n != null && Math.abs(n) > 1.5) return n;
      }
      for (let i = 0; i < keys.length; i++) {
        const n = firstMoneyNumber(field[keys[i]]);
        if (n != null) return n;
      }
      return null;
    }

    function inferDonorRubles(donor) {
      const n = Math.abs(
        firstMoneyNumber(donor && donor.amount) ||
        firstMoneyNumber(donor && donor.moneyAmount) ||
        firstMoneyNumber(donor && donor.accountAmount) ||
        firstMoneyNumber(donor && donor.sum) ||
        0
      );
      if (!n) return 0;
      if (Number.isInteger(n) && n >= 100000 && n % 100 === 0) return n / 100;
      return n;
    }

    function scaleMoneyTree(node, factor, wantNeg, depth) {
      if (!node || typeof node !== 'object' || depth > 8) return;
      if (Array.isArray(node)) {
        node.forEach((item) => scaleMoneyTree(item, factor, wantNeg, depth + 1));
        return;
      }
      const nums = [];
      Object.keys(node).forEach((key) => {
        if (typeof node[key] === 'number' && Number.isFinite(node[key])) nums.push(node[key]);
      });
      const hasBig = nums.some((n) => Math.abs(n) > 1.5);
      Object.keys(node).forEach((key) => {
        if (/^(id|date|time|timestamp|scale|precision|currency|code|iso|isoCode|numericCode|phone|millis|epoch|status|type|direction|sign)$/i.test(key)) return;
        const val = node[key];
        if (typeof val === 'number' && Number.isFinite(val)) {
          if (/minorUnits|scale|precision|fractionDigits/i.test(key) && Math.abs(val) <= 4) return;
          if (hasBig && Math.abs(val) <= 1.5) return;
          let next = Math.abs(val) * factor;
          if (Number.isInteger(val)) next = Math.round(next);
          node[key] = wantNeg ? -Math.abs(next) : Math.abs(next);
        } else if (typeof val === 'string' && /^-?\d/.test(val.trim()) && /amount|sum|value|money|total|rubles|minor/i.test(key)) {
          const n = Number(val.replace(/\s/g, '').replace(',', '.'));
          if (!Number.isFinite(n) || (hasBig && Math.abs(n) <= 1.5)) return;
          let next = Math.abs(n) * factor;
          if (Number.isInteger(n) && val.indexOf('.') < 0 && val.indexOf(',') < 0) next = Math.round(next);
          const signed = wantNeg ? -Math.abs(next) : Math.abs(next);
          node[key] = val.indexOf(',') >= 0 ? String(signed).replace('.', ',') : String(signed);
        } else if (val && typeof val === 'object') {
          scaleMoneyTree(val, factor, wantNeg, depth + 1);
        }
      });
    }

    function setAlfaAmountField(field, rubles) {
      if (!field || typeof field !== 'object' || Array.isArray(field)) return;
      const minor = typeof field.minorUnits === 'number' && field.minorUnits > 0 ? field.minorUnits : 100;
      const kopecks = Math.round(Math.abs(Number(rubles)) * minor);
      const src = field.value;
      const origNeg = typeof src === 'number' && src < 0;
      if (typeof src === 'number' || src == null || src === '') {
        field.value = origNeg ? -kopecks : kopecks;
      } else if (typeof src === 'string') {
        const n = Number(String(src).replace(',', '.').replace(/\s/g, ''));
        const neg = Number.isFinite(n) && n < 0;
        field.value = String(neg ? -kopecks : kopecks);
      }
    }

    function patchOperationAmount(clone, donor, item) {
      const userRub = Math.abs(Number(item.amount));
      if (!userRub) return;
      if (clone.amount && typeof clone.amount === 'object' && ('minorUnits' in clone.amount || 'value' in clone.amount)) {
        setAlfaAmountField(clone.amount, userRub);
        return;
      }
      const donorRub = inferDonorRubles(donor || clone);
      if (donorRub > 0) {
        const factor = userRub / donorRub;
        const wantNeg = Number(item.amount) < 0;
        [
          'amount', 'moneyAmount', 'sum', 'accountAmount', 'operationAmount',
          'debitAmount', 'creditAmount', 'displayedAmount', 'totalAmount',
        ].forEach((key) => {
          if (clone[key] != null) scaleMoneyTree(clone[key], factor, wantNeg, 0);
        });
      } else {
        assignMoney(clone, 'amount', userRub);
      }
    }

    function patchOneOperation(op, donor, item) {
      const signed = Number(item.amount);
      const isDebit = signed < 0;
      const title = item.description;
      if (typeof op.title === 'string' || 'title' in op) op.title = title;
      if (typeof op.name === 'string' || 'name' in op) op.name = title;
      if (typeof op.recipient === 'string') op.recipient = title;
      if (typeof op.shortTitle === 'string') op.shortTitle = title;
      if (typeof op.description === 'string' && looksLikePersonTitle(op.description)) op.description = title;
      if (typeof op.purpose === 'string' && looksLikePersonTitle(op.purpose)) op.purpose = title;
      if (op.counterpart && typeof op.counterpart === 'object') {
        if (typeof op.counterpart.name === 'string') op.counterpart.name = title;
        if (item.phone && typeof op.counterpart.phone === 'string') op.counterpart.phone = item.phone;
      }
      if (typeof op.direction === 'string') {
        const d = op.direction.toUpperCase();
        if (isDebit && (d === 'INCOME' || d === 'IN' || d === 'CREDIT')) {
          op.direction = d === 'IN' ? 'OUT' : d === 'CREDIT' ? 'DEBIT' : 'EXPENSE';
        }
        if (!isDebit && (d === 'EXPENSE' || d === 'OUT' || d === 'DEBIT')) {
          op.direction = d === 'OUT' ? 'IN' : d === 'DEBIT' ? 'CREDIT' : 'INCOME';
        }
      }
      patchOperationAmount(op, donor, item);
      if (item.phone && typeof op.phone === 'string') op.phone = item.phone;
      setCategoryLine(op, item);
      // Donor transfers keep personal comment/badge («Долг») — force short face text.
      if (isCardKind(item)) {
        op.comment = null;
        delete op.bottomBadge;
      } else if (isAlfaInternalKind(item)) {
        op.comment = 'Перевод денежных средств';
        delete op.bottomBadge;
      } else {
        op.comment = 'Перевод денежных средств';
        forceSbpBottomBadge(op, 'Перевод денежных средств');
      }
      if (typeof op.purpose === 'string' && !looksLikePersonTitle(op.purpose)) {
        op.purpose = isCardKind(item) ? '' : 'Перевод денежных средств';
      }
      if (isCardKind(item) || isAlfaInternalKind(item)) {
        // Live card→other / Alfa→Alfa: no SBP bottomBadge.
        delete op.bottomBadge;
        if (op.status == null) op.status = 'SUCCESS';
        if (op.subtitle == null) op.subtitle = '';
      }
      return op;
    }

    function forceSbpBottomBadge(op, text) {
      if (!op || typeof op !== 'object') return;
      const t = String(text || 'Перевод денежных средств');
      const b = op.bottomBadge;
      if (b && typeof b === 'object' && !Array.isArray(b)) {
        if (typeof b.text === 'string' || 'text' in b) b.text = t;
        else if (typeof b.title === 'string') b.title = t;
        else if (b.title && typeof b.title === 'object' && 'value' in b.title) b.title.value = t;
        else if (typeof b.name === 'string' || 'name' in b) b.name = t;
        else if (typeof b.value === 'string' || 'value' in b) b.value = t;
        else b.text = t;
      } else if (typeof b === 'string') {
        op.bottomBadge = t;
      } else {
        op.bottomBadge = { text: t };
      }
    }

    function slimNode(node, depth) {
      if (node == null || depth > 4) return node;
      if (typeof node === 'string') return node.length > 280 ? node.slice(0, 280) + '…' : node;
      if (typeof node !== 'object') return node;
      if (Array.isArray(node)) {
        return { len: node.length, sample: node[0] != null ? slimNode(node[0], depth + 1) : null };
      }
      const out = {};
      Object.keys(node).slice(0, 60).forEach((key) => {
        const val = node[key];
        if (val == null || typeof val === 'number' || typeof val === 'boolean') out[key] = val;
        else if (typeof val === 'string') out[key] = val.length > 280 ? val.slice(0, 280) + '…' : val;
        else if (Array.isArray(val)) out[key] = { len: val.length, keys: val[0] && typeof val[0] === 'object' ? Object.keys(val[0]) : val[0] };
        else if (typeof val === 'object') {
          if ('value' in val || 'currency' in val || 'minorUnits' in val || 'iconUrl' in val) out[key] = slimNode(val, depth + 1);
          else out[key] = Object.keys(val).slice(0, 40);
        }
      });
      return out;
    }

    function ensureDump() {
      if (!win.__ALFA_MOCK_DUMP__) {
        win.__ALFA_MOCK_DUMP__ = {
          history: [],
          details: [],
          rawDetails: [],
          receipts: [],
          responseUrls: [],
          expenseResponses: [],
          profileResponses: [],
          iconUrls: {},
          seenIds: {},
          seenDetail: {},
          seenResponseUrl: {},
          seenProfileUrl: {},
        };
      }
      const dump = win.__ALFA_MOCK_DUMP__;
      if (!Array.isArray(dump.responseUrls)) dump.responseUrls = [];
      if (!Array.isArray(dump.expenseResponses)) dump.expenseResponses = [];
      if (!Array.isArray(dump.profileResponses)) dump.profileResponses = [];
      if (!dump.seenResponseUrl) dump.seenResponseUrl = {};
      if (!dump.seenProfileUrl) dump.seenProfileUrl = {};
      return dump;
    }

    function opLabel(op) {
      return op && (op.name || op.title || op.shortTitle || op.description || '');
    }

    function pushHistoryList(list) {
      const dump = ensureDump();
      (list || []).forEach((op) => {
        if (!op || typeof op !== 'object' || looksLikePaymentTemplate(op) || isMockOp(op)) return;
        const id = String(op.id || op.operationId || op.transactionId || (opLabel(op) + '|' + (op.logoUrl || op.iconUrl || '') + '|' + JSON.stringify(op.amount || '')));
        if (dump.seenIds[id]) return;
        dump.seenIds[id] = 1;
        if (dump.history.length >= 200) return;
        const icon = op.logoUrl || op.iconUrl || op.iconURL || '';
        if (icon) dump.iconUrls[icon] = (dump.iconUrls[icon] || 0) + 1;
        dump.history.push({
          name: opLabel(op),
          subtitle: op.subtitle || op.subTitle || '',
          category: op.category && typeof op.category === 'object' ? op.category.name : op.category,
          type: op.type,
          paymentType: op.paymentType,
          amount: op.amount,
          logoUrl: icon || null,
          iconUrl: icon || null,
          keys: Object.keys(op),
        });
      });
    }

    function looksLikeDetailPayload(data, url) {
      const u = String(url || '').toLowerCase();
      if (/\/operations\/[a-z0-9._-]+|operation-details|operationdetail|operation_details|\/receipt|квитан|cheque|payment-document/i.test(u)) return true;
      const obj = (data && (data.operation || data.payload || data.result || data)) || data;
      if (!obj || typeof obj !== 'object' || Array.isArray(obj)) return false;
      if (findOperationsArray(obj, 0)) return false;
      return !!(obj.amount && (obj.logoUrl || obj.iconUrl || obj.name || obj.title) && (obj.recipient || obj.category || obj.state || obj.subtitle || obj.fields || obj.bottomBadge));
    }

    function looksLikeReceiptPayload(data, url) {
      const u = String(url || '').toLowerCase();
      if (/operations-history\/operations\/?(\?|$)/.test(u) && !/receipt|cheque|квитан/.test(u)) return false;
      return /receipt|cheque|квитан|document-pdf|payment_receipt/i.test(u);
    }

    function captureFromData(url, data) {
      if (!data || typeof data !== 'object') return;
      try {
        const dump = ensureDump();
        const responseUrl = String(url || '').slice(0, 500);
        if (responseUrl && !dump.seenResponseUrl[responseUrl] && dump.responseUrls.length < 150) {
          dump.seenResponseUrl[responseUrl] = 1;
          dump.responseUrls.push(responseUrl);
        }
        const raw = JSON.stringify(data);
        if (dump.expenseResponses.length < 8) {
          const urlLooksRelevant = /pfm|analytic|expense|spend|outcome|history|operation/i.test(responseUrl);
          const bodyLooksRelevant = /расход|трат|spending|expenses|expense|outcome|monthTotal|totalSpending/i.test(raw);
          if ((urlLooksRelevant || bodyLooksRelevant) && raw.length <= 1200000) {
            dump.expenseResponses.push({
              url: responseUrl,
              body: cloneJson(data),
            });
          }
        }
        if (dump.profileResponses.length < 20 && !dump.seenProfileUrl[responseUrl] && raw.length <= 1200000) {
          const urlLooksProfile = /profile|user_profile|customer|client-info|personal|passport|snils|снилс|identity|document|user\/me|customers\//i.test(responseUrl);
          const bodyLooksProfile = /паспорт|снилс|серия паспорта|номер паспорта|passportSeries|passportNumber|snils|birthDate|место рождения|инн\b/i.test(raw);
          if (urlLooksProfile || bodyLooksProfile) {
            dump.seenProfileUrl[responseUrl] = 1;
            dump.profileResponses.push({
              url: responseUrl,
              body: cloneJson(data),
            });
          }
        }
        const list = findOperationsArray(data, 0);
        if (list && list.length) pushHistoryList(list);
        if (looksLikeReceiptPayload(data, url) && dump.receipts.length < 20) {
          dump.receipts.push({ url: String(url || '').slice(0, 240), body: slimNode(data, 0) });
        } else if (looksLikeDetailPayload(data, url) && dump.details.length < 20) {
          const obj = data.operation || data.payload || data.result || data;
          const id = String((obj && (obj.id || obj.operationId)) || url || dump.details.length);
          if (!dump.seenDetail[id]) {
            dump.seenDetail[id] = 1;
            dump.details.push({ url: String(url || '').slice(0, 240), body: slimNode(obj, 0) });
            if (dump.rawDetails.length < 5) {
              dump.rawDetails.push({
                url: String(url || '').slice(0, 500),
                body: cloneJson(obj),
              });
            }
            const icon = obj && (obj.logoUrl || obj.iconUrl || obj.iconURL);
            if (icon) dump.iconUrls[icon] = (dump.iconUrls[icon] || 0) + 1;
            if (
              obj &&
              !isMockOp(obj) &&
              Array.isArray(obj.fields) &&
              obj.fields.length &&
              obj.bottomBadge &&
              Array.isArray(obj.actions) &&
              obj.actions.length === 4 &&
              hasReceiptAction(obj.actions) &&
              !win.__ALFA_DETAIL_SKELETON__
            ) {
              win.__ALFA_DETAIL_SKELETON__ = cloneJson(obj);
              persistMocks();
            }
          }
        }
      } catch (_) { /* ignore */ }
    }

    function mockFingerprint() {
      return 'v25||' + (CONFIG.operations || []).map((item) => String(item.description || '') + '|' + String(item.amount) + '|' + String(item.dateTime || '')).join('||');
    }

    function savedMocksForConfig() {
      const fp = mockFingerprint();
      const out = [];
      const seen = new Set();
      Object.keys(MOCK_OPS).forEach((id) => {
        const op = MOCK_OPS[id];
        if (!op || op.__alfaMockFp !== fp || !op.__alfaMockOp) return;
        const key = String(op.id || id);
        if (seen.has(key)) return;
        seen.add(key);
        out.push(op);
      });
      out.sort((a, b) => (a.__alfaMockIndex || 0) - (b.__alfaMockIndex || 0));
      return out.slice(0, (CONFIG.operations || []).length);
    }

    function prependOps(list, inserts) {
      if (!inserts.length) return;
      const ids = new Set();
      const titles = new Set();
      const uniq = [];
      inserts.forEach((op) => {
        if (!op) return;
        const id = String(op.id || '');
        const title = String(op.title || op.name || '');
        if (id && ids.has(id)) return;
        if (title && titles.has(title)) return;
        if (id) ids.add(id);
        if (title) titles.add(title);
        uniq.push(op);
      });
      for (let i = list.length - 1; i >= 0; i--) {
        const op = list[i];
        if (!op) continue;
        if (isMockOp(op) || (op.id && ids.has(String(op.id))) || titles.has(String(op.title || '')) || titles.has(String(op.name || ''))) {
          list.splice(i, 1);
        }
      }
      try {
        list.unshift(...uniq);
      } catch (_) {
        const rest = list.slice();
        list.length = 0;
        uniq.concat(rest).forEach((op, idx) => { list[idx] = op; });
      }
    }

    function rememberMock(op) {
      if (!op) return;
      ['id', 'operationId', 'clickReference', 'reference', '__alfaDonorId'].forEach((key) => {
        const id = op[key];
        if (id != null && id !== '') MOCK_OPS[String(id)] = op;
      });
    }

    function stripReplacedDonors(list) {
      const ids = new Set();
      Object.keys(MOCK_OPS).forEach((key) => {
        const op = MOCK_OPS[key];
        if (!op || !op.__alfaMockOp) return;
        if (op.__alfaDonorId) ids.add(String(op.__alfaDonorId));
        if (op.id) ids.add(String(op.id));
      });
      if (!ids.size) return;
      for (let i = list.length - 1; i >= 0; i--) {
        const op = list[i];
        if (!op || op.__alfaMockOp) continue;
        if (ids.has(String(op.id || ''))) list.splice(i, 1);
      }
    }

    function insertOps(list) {
      if (!CONFIG.operations?.length) return;
      const fp = mockFingerprint();
      const wanted = (CONFIG.operations || []).map((item) => item.description);
      const liveSnapshot = (list || []).filter((op) => op && !isMockOp(op));
      const template = findAnyHistoryDonor(liveSnapshot);
      for (let i = list.length - 1; i >= 0; i--) {
        const op = list[i];
        if (!op) continue;
        if (isMockOp(op) || (wanted.length && (wanted.indexOf(op.title) >= 0 || wanted.indexOf(op.name) >= 0))) {
          list.splice(i, 1);
        }
      }
      stripReplacedDonors(list);
      const saved = savedMocksForConfig().filter((op) => {
        if (!op || !op.__alfaMockOp || op.__alfaMockNid) return false;
        const item = op.__alfaMockItem;
        if (item && isCardKind(item)) return !looksLikeSbpTransfer(op);
        if (item && isAlfaInternalKind(item)) {
          return looksLikeAlfaInternalTransfer(op)
            || (!looksLikeSbpTransfer(op) && /перевод/i.test(String((op.category && (op.category.name || op.category)) || '')));
        }
        return op.bottomBadge || looksLikeSbpTransfer(op) || /перевод/i.test(String((op.category && (op.category.name || op.category)) || ''));
      });
      if (saved.length >= CONFIG.operations.length) {
        saved.forEach((op, i) => {
          const item = (op && op.__alfaMockItem) || (CONFIG.operations || [])[i];
          if (item && item.bank) {
            applyBankBrand(op, item.bank);
            const logo = bankLogoSrc(item.bank);
            if (logo) {
              op.logoUrl = logo;
              if ('iconUrl' in op || op.iconUrl) op.iconUrl = logo;
              replaceLogoUrls(op, logo);
            }
            setCategoryLine(op, item);
            if (isCardKind(item) || isAlfaInternalKind(item)) delete op.bottomBadge;
          }
          registerReceiptIds(op, i);
          rememberMock(op);
        });
        persistMocks();
        prependOps(list, saved);
        return;
      }
      const newest = liveSnapshot[0] || list[0];
      const inserts = [];
      Object.keys(MOCK_OPS).forEach((id) => {
        if (MOCK_OPS[id] && MOCK_OPS[id].__alfaMockFp !== fp) delete MOCK_OPS[id];
      });
      const usedDonors = new Set();
      const donorList = liveSnapshot.length ? liveSnapshot : (list || []);
      for (let i = 0; i < CONFIG.operations.length; i++) {
        const item = CONFIG.operations[i];
        let donor = findBrandDonor(donorList, item.bank);
        if (donor && usedDonors.has(String(donor.id))) donor = null;
        if (isCardKind(item)) {
          if (!donor || looksLikeSbpTransfer(donor) || !looksLikeCardTransfer(donor)) {
            donor = donorList.find((op) => op && !op.__alfaMockOp && looksLikeCardTransfer(op) && !usedDonors.has(String(op.id))) || donor;
          }
          if (!donor || looksLikeSbpTransfer(donor)) {
            donor = donorList.find((op) => op && !op.__alfaMockOp && looksLikeCardTransfer(op)) || donor;
          }
        } else if (isAlfaInternalKind(item)) {
          if (!donor || looksLikeSbpTransfer(donor) || !looksLikeAlfaInternalTransfer(donor)) {
            donor = donorList.find((op) => op && !op.__alfaMockOp && looksLikeAlfaInternalTransfer(op) && !usedDonors.has(String(op.id))) || donor;
          }
          if (!donor || looksLikeSbpTransfer(donor)) {
            donor = donorList.find((op) => op && !op.__alfaMockOp && looksLikeAlfaInternalTransfer(op)) || donor;
          }
        } else if (!donor) {
          donor = donorList.find((op) => op && !op.__alfaMockOp && looksLikeSbpTransfer(op) && !usedDonors.has(String(op.id))) || template;
        }
        const base = donor || template || syntheticHistoryOp(item);
        const clone = cloneJson(base);
        patchOneOperation(clone, base, item);
        applyBankBrand(clone, item.bank);
        const logo = bankLogoSrc(item.bank);
        if (logo) {
          clone.logoUrl = logo;
          if ('iconUrl' in clone || clone.iconUrl) clone.iconUrl = logo;
          replaceLogoUrls(clone, logo);
        }
        if (isCardKind(item) || isAlfaInternalKind(item)) {
          delete clone.bottomBadge;
        } else {
          if (!clone.bottomBadge) {
            const badgeDonor = donorList.find((op) => op && op.bottomBadge && looksLikeSbpTransfer(op));
            if (badgeDonor) clone.bottomBadge = cloneJson(badgeDonor.bottomBadge);
          }
          forceSbpBottomBadge(clone, 'Перевод денежных средств');
        }
        stampOpTime(clone, newest, item);
        clone.__alfaMockOp = true;
        clone.__alfaMockFp = fp;
        clone.__alfaMockIndex = i;
        clone.__alfaDonorId = String((donor || template || clone).id || clone.id || '');
        clone.__alfaKeepDonorId = true;
        clone.__alfaMockItem = {
          description: item.description,
          amount: item.amount,
          phone: item.phone,
          bank: item.bank,
          category: item.category,
          transferKind: item.transferKind || 'sbp',
          cardLast4: item.cardLast4 || '',
          dateTime: item.dateTime || clone.dateTime,
          atMs: item.atMs,
        };
        delete clone.__alfaMockNid;
        registerReceiptIds(clone, i);
        rememberMock(clone);
        if (clone.__alfaDonorId) usedDonors.add(String(clone.__alfaDonorId));
        inserts.push(clone);
        if (donor || template) ensureSkeleton(donor || template);
      }
      persistMocks();
      stripReplacedDonors(list);
      prependOps(list, inserts);
    }

    function isFirstOpsPage(data, url) {
      const info = (data && (data.pagesInfo || data.pageInfo)) || {};
      if (typeof info.page === 'number' && info.page >= 2) return false;
      const u = String(url || '');
      const m = u.match(/[?&]page=(\d+)/i);
      if (m && Number(m[1]) >= 2) return false;
      return true;
    }

    function patchOperations(data, url) {
      const lists = findAllOperationLists(data);
      if (!lists.length) return data;
      lists.forEach(rememberSbpDonors);
      if (!CONFIG.operations?.length) return data;
      if (!isFirstOpsPage(data, url)) return data;
      lists.sort((a, b) => scoreOpList(b) - scoreOpList(a));
      insertOps(lists[0]);
      return data;
    }

    function patchProduct(obj) {
      const rub = CONFIG.balance.value;
      [
        'amount', 'balance', 'available', 'ownFunds', 'moneyAmount', 'rest',
        'availableBalance', 'currentBalance', 'ownAmount', 'money', 'total',
        'availableAmount', 'balanceAmount', 'holdAmount', 'ownFundsAmount',
        'availableOwnAmount', 'ownFundsAmount', 'restAmount', 'funds',
      ].forEach((key) => assignMoney(obj, key, rub));
      if (isMoneyObject(obj)) setMoney(obj, rub);
      Object.keys(obj).forEach((key) => {
        if (/^(id|date|time|number|pan|status|type|currency|code|iban|eqId)/i.test(key)) return;
        if (isMoneyObject(obj[key])) setMoney(obj[key], rub);
      });
      applyAccountMask(obj);
    }

    let patchedCurrentAccount = false;

    function isCurrentAccountProduct(obj) {
      if (!obj || looksLikeOperation(obj)) return false;
      if (!(looksLikeAccountProduct(obj) || looksLikeProduct(obj))) return false;
      const name = String(obj.name || obj.title || obj.description || obj.shortName || obj.productName || '');
      return /текущ/i.test(name);
    }

    function walkPatchProducts(node, depth) {
      if (!node || typeof node !== 'object' || depth > 14) return;
      if (Array.isArray(node)) {
        node.forEach((item) => walkPatchProducts(item, depth + 1));
        return;
      }
      if (isCurrentAccountProduct(node)) {
        if (!patchedCurrentAccount) {
          patchProduct(node);
          patchedCurrentAccount = true;
        }
        return;
      }
      Object.values(node).forEach((val) => {
        if (val && typeof val === 'object') walkPatchProducts(val, depth + 1);
      });
    }

    function patchSpending(obj) {
      if (!obj || obj.direction || Array.isArray(obj.fields) || obj.__alfaMockOp) return;
      const spend = CONFIG.spending?.monthTotal;
      const income = CONFIG.spending?.incomeTotal;
      if (spend == null) return;
      const title = String(obj.title || obj.name || obj.label || obj.header || obj.text || '');
      if (/трат|расход|spending|expense/i.test(title) && !/доход|income/i.test(title)) {
        ['amount', 'value', 'sum', 'total', 'moneyAmount', 'balance'].forEach((key) => {
          if (key in obj) assignMoney(obj, key, spend);
        });
        if (isMoneyObject(obj)) setMoney(obj, spend);
      }
      if (income != null && /доход|income|earning/i.test(title) && !/расход|трат/.test(title)) {
        ['amount', 'value', 'sum', 'total', 'moneyAmount', 'balance'].forEach((key) => {
          if (key in obj) assignMoney(obj, key, income);
        });
        if (isMoneyObject(obj)) setMoney(obj, income);
      }
      [
        'spending', 'expenses', 'expense', 'outcome', 'debit', 'monthTotal',
        'spend', 'costs', 'expensesAmount', 'spendingAmount', 'outcomeAmount',
        'totalSpending', 'monthSpending', 'expensesTotal',
      ].forEach((key) => {
        if (key in obj) assignMoney(obj, key, spend);
      });
      if (income != null) {
        ['income', 'earning', 'credit', 'incomeTotal', 'incomeAmount', 'totalIncome', 'monthIncome'].forEach((key) => {
          if (key in obj) assignMoney(obj, key, income);
        });
      }
    }

    function patchTree(node, depth, inOp) {
      if (!node || typeof node !== 'object' || depth > 16) return;
      if (Array.isArray(node)) {
        node.forEach((item) => patchTree(item, depth + 1, inOp || looksLikeOperation(item)));
        return;
      }
      const op = inOp || looksLikeOperation(node);
      if (!op) {
        applyNameToObject(node);
        applyDocsToObject(node);
      }
      if (!op && isCurrentAccountProduct(node)) {
        if (!patchedCurrentAccount) {
          patchProduct(node);
          patchedCurrentAccount = true;
        }
        return;
      }
      if (!op) patchSpending(node);
      // Chat / helper copy: rewrite client first name inside greeting strings.
      Object.keys(node).forEach((key) => {
        const val = node[key];
        if (typeof val === 'string' && /Приветств|Здравств|Альфа-Помощник|Инесса/i.test(val)) {
          node[key] = rewriteClientFirstInText(val);
        } else if (val && typeof val === 'object') {
          patchTree(val, depth + 1, op);
        }
      });
    }

    function unwrapDetail(data) {
      if (!data || typeof data !== 'object' || Array.isArray(data)) return null;
      if (Array.isArray(data.operations) || data.pagesInfo || data.pageInfo) return null;
      if (Array.isArray(data.fields) && data.amount) return data;
      const keys = ['operation', 'payload', 'result', 'data'];
      for (let i = 0; i < keys.length; i++) {
        const inner = data[keys[i]];
        if (inner && typeof inner === 'object' && !Array.isArray(inner) && inner.amount && (Array.isArray(inner.fields) || inner.title)) {
          return inner;
        }
      }
      if (data.amount && data.title && data.id && data.direction) return data;
      return null;
    }

    function findMockForDetail(url, inner) {
      const fromUrl = mockIdFromUrl(url);
      if (fromUrl && MOCK_OPS[fromUrl]) return MOCK_OPS[fromUrl];
      if (!inner) return null;
      const ids = [inner.id, inner.operationId, inner.clickReference, inner.reference];
      for (let i = 0; i < ids.length; i++) {
        if (ids[i] != null && MOCK_OPS[String(ids[i])]) return MOCK_OPS[String(ids[i])];
      }
      const title = String((inner.title || inner.name || '')).trim();
      if (title) {
        const keys = Object.keys(MOCK_OPS);
        for (let i = 0; i < keys.length; i++) {
          const op = MOCK_OPS[keys[i]];
          if (!op || !op.__alfaMockOp) continue;
          if (op.title === title || (op.__alfaMockItem && op.__alfaMockItem.description === title)) return op;
        }
      }
      return null;
    }

    function hasReceiptAction(actions) {
      if (!Array.isArray(actions)) return false;
      return actions.some((a) => a && /квитанц|receipt|cheque/i.test(String((a && (a.label || a.type || a.deeplink || a.mobileApiPath)) || '')));
    }

    const SPLIT_ENABLED = !!CONFIG.splitCheck;
    const SPLIT_LINK = String(
      CONFIG.splitCheckLink || 'https://money-alfabank.ru/mr/wK4nRmQp8d',
    );

    function isSplitAction(a) {
      if (!a || typeof a !== 'object') return false;
      if (String(a.type || '') === 'SPLIT') return true;
      return /разделить\s*чек/i.test(String(a.label || ''));
    }

    function splitAction(opId) {
      const raw = String(opId || '');
      const enc = encodeURIComponent(raw);
      return {
        type: 'SPLIT',
        label: 'Разделить\nчек',
        icon: { name: 'glyph_two-users_m' },
        deeplink: 'alfabank:///split?operationId=' + enc,
      };
    }

    function patchActionsList(actions, opId) {
      if (!Array.isArray(actions)) return actions;
      const next = actions.filter((a) => !isSplitAction(a));
      if (SPLIT_ENABLED) next.push(splitAction(opId));
      return next;
    }

    function ensureSplitAction(detail) {
      if (!detail || typeof detail !== 'object') return;
      if (!Array.isArray(detail.actions)) {
        if (!SPLIT_ENABLED) return;
        detail.actions = [];
      }
      const opId =
        detail.id ||
        detail.operationId ||
        detail.reference ||
        detail.clickReference ||
        '';
      detail.actions = patchActionsList(detail.actions, opId);
    }

    function forceSplitLinkInTree(node, depth) {
      if (!node || depth > 36) return;
      if (Array.isArray(node)) {
        for (let i = 0; i < node.length; i++) forceSplitLinkInTree(node[i], depth + 1);
        return;
      }
      if (typeof node !== 'object') return;
      if (
        typeof node.address === 'string' &&
        /money[.-]?alfabank\.ru\/mr\//i.test(node.address)
      ) {
        if (SPLIT_ENABLED) node.address = SPLIT_LINK;
      }
      if (Array.isArray(node.actions)) {
        const opId =
          node.id ||
          node.operationId ||
          node.reference ||
          node.clickReference ||
          '';
        node.actions = patchActionsList(node.actions, opId);
      }
      const keys = Object.keys(node);
      for (let i = 0; i < keys.length; i++) {
        const k = keys[i];
        if (k === 'actions') continue;
        const v = node[k];
        if (v && typeof v === 'object') forceSplitLinkInTree(v, depth + 1);
      }
    }

    function isPaymentSplittingUrl(url) {
      return /money-request\/payment-splitting/i.test(String(url || ''));
    }

    function opIdFromSplitUrl(url) {
      const m = String(url || '').match(/payment-splitting\/([^/?#]+)/i);
      if (!m) return '';
      try {
        return decodeURIComponent(m[1]);
      } catch (_) {
        return m[1];
      }
    }

    function buildSplitPayload(opId, mock) {
      const item = (mock && mock.__alfaMockItem) || {};
      const title =
        String(
          (mock && mock.title) ||
            item.description ||
            (CONFIG.profile && CONFIG.profile.displayName) ||
            'Перевод',
        ) || 'Перевод';
      const rub = Math.abs(Number(item.amount != null ? item.amount : 0));
      const value = Math.round(rub * 100);
      let logo =
        (mock && mock.logoUrl) ||
        bankLogoSrc(item.bank) ||
        'https://alfaonline.servicecdn.ru/public/s3/static/logo-payments/logo_bank_alfabank__xl.png';
      const catName =
        (mock && mock.category && (mock.category.name || mock.category)) ||
        'Переводы';
      return {
        paymentInfo: {
          title: title,
          category: {
            id: '00052',
            name: String(catName),
            color: '#3FCDFE',
            icon: 'glyph_pfm-transfer_m',
          },
          amount: {
            value: value || 15000,
            currency: 'RUR',
            minorUnits: 100,
          },
          dateTime: (mock && mock.dateTime) || new Date().toISOString(),
          logoUrl: logo,
        },
        address: SPLIT_LINK,
        maxRecipients: 10,
        ownerScreenInfo: {
          title: 'Разделить чек',
          recipients: 'С кем делим?',
          contacts: 'Выберите контакты',
          link: 'Ссылка на пополнение',
          buttonSendRequest: 'Отправить запрос',
          buttonEdit: 'Добавить контакты',
        },
        paymentPeriod:
          'Осталось 13 дней 22 часа. Друзья сами укажут сумму перевода',
      };
    }

    function detailNeedsSbpOverlay(inner, mock) {
      const item = (mock && mock.__alfaMockItem) || {};
      // Card→other / Alfa→Alfa dump: no SBP badge — do not force SBP skeleton.
      if (isCardKind(item) || isAlfaInternalKind(item)) return false;
      if (!inner) return true;
      const cat = String((inner.category && (inner.category.name || inner.category)) || '');
      if (/кэшбэк|cashback|аналитик/i.test(cat)) return true;
      if (!hasReceiptAction(inner.actions)) return true;
      if (!inner.bottomBadge) return true;
      return false;
    }

    function ensureReceiptAction(detail, item) {
      if (!detail) return;
      const sk = win.__ALFA_DETAIL_SKELETON__;
      if (sk && Array.isArray(sk.actions) && sk.actions.length === 4 && hasReceiptAction(sk.actions)) {
        detail.actions = cloneJson(sk.actions);
        return;
      }
      const ref = String(detail.reference || detail.clickReference || detail.id || '');
      const encRef = encodeURIComponent(ref);
      const title = encodeURIComponent(String((item && item.description) || detail.title || ''));
      detail.actions = [
        {
          mobileApiPath: `v1/operation-info/operations/${encRef}/documents/pdf?isPos=false`,
          verificationEmailDeeplink: 'https://online.alfabank.ru/user_profile/detail/email',
          referenceId: ref,
          isPos: false,
          type: 'DOCUMENT',
          label: 'Получить квитанцию',
          icon: { name: 'glyph_receipt-line_m' },
          deeplink: `alfabank://pdf_viewer?url=operation-info-api/operations/${encRef}/documents/pdf?view=WEB&isPos=false`,
        },
        {
          operationType: 'FAST_PAYMENT_SYSTEM_TRANSFER',
          account: null,
          beneficiaryAccount: null,
          type: 'REPEAT',
          label: 'Повторить операцию',
          icon: { name: 'glyph_arrow-forward-big_m' },
          deeplink: `alfabank:///dashboard/phone_bank_transfers?reference=${encRef}`,
        },
        {
          type: 'CREATE_TEMPLATE',
          label: 'Создать шаблон',
          icon: { name: 'glyph_star-line_m' },
          deeplink: `alfabank:///dashboard/template_creation?reference=${encRef}`,
        },
        {
          accountNumber: null,
          phoneNumber: (item && item.phone) || null,
          type: 'CREATE_AUTO_PAYMENT',
          label: 'Создать автоплатёж',
          icon: { name: 'glyph_clock-line_m' },
          deeplink: `alfabank:///dashboard/autopay_creation?reference=${encRef}&title=${title}`,
        },
      ];
    }

    function applyMockFace(inner, mock) {
      const item = mock.__alfaMockItem || {};
      if (item.description) inner.title = item.description;
      const bankLogo = bankLogoSrc(item.bank) || bankLogoSrc(mock && mock.bank) || '';
      if (bankLogo) {
        inner.logoUrl = bankLogo;
        if ('iconUrl' in inner) inner.iconUrl = bankLogo;
      }
      const rub = Math.abs(Number(item.amount != null ? item.amount : 0));
      if (rub) {
        if (inner.amount && typeof inner.amount === 'object') setAlfaAmountField(inner.amount, rub);
        else inner.amount = { value: Math.round(rub * 100), currency: 'RUR', minorUnits: 100 };
      } else if (mock.amount) {
        inner.amount = cloneJson(mock.amount);
      }
      inner.direction = mock.direction || inner.direction || 'EXPENSE';
      stampOpTime(inner, inner, item);
      if (item.dateTime) inner.dateTime = item.dateTime;
      else if (mock.dateTime) inner.dateTime = mock.dateTime;
      // Detail category object stays short «Переводы» (dump); list uses long line.
      const shortCat = item.category || 'Переводы';
      if (inner.category && typeof inner.category === 'object') {
        if (typeof inner.category.name === 'string') inner.category.name = shortCat;
      }
      inner.loyaltyDetails = { cashbackStatusTitle: null };
      if (isCardKind(item)) {
        inner.comment = null;
        delete inner.bottomBadge;
        inner.status = inner.status || 'SUCCESS';
      } else if (isAlfaInternalKind(item)) {
        // Live dump Дамир С.: comment + no SBP badge.
        inner.comment = 'Перевод денежных средств';
        delete inner.bottomBadge;
        inner.status = inner.status || 'SUCCESS';
      } else {
        inner.comment = 'Перевод денежных средств';
        inner.status = inner.status || 'SUCCESS';
      }
      const sk = win.__ALFA_DETAIL_SKELETON__;
      if (!isCardKind(item) && !isAlfaInternalKind(item)) {
        if (!inner.bottomBadge && (mock.bottomBadge || (sk && sk.bottomBadge))) {
          inner.bottomBadge = cloneJson(mock.bottomBadge || sk.bottomBadge);
        }
        forceSbpBottomBadge(inner, 'Перевод денежных средств');
        ensureSbpBadgeOnDetail(inner, item);
      }
      if (sk && Array.isArray(sk.fields) && (!Array.isArray(inner.fields) || !inner.fields.length)) {
        inner.fields = cloneJson(sk.fields);
      }
      if (sk && hasReceiptAction(sk.actions)) inner.actions = cloneJson(sk.actions);
      patchDetailFields(inner, item, mock);
      ensureReceiptAction(inner, item);
      ensureSplitAction(inner);
    }

    function patchIncomingDetail(url, data) {
      const inner = unwrapDetail(data);
      if (!inner) return;
      const mock = findMockForDetail(url, inner);
      if (!mock || !mock.__alfaMockOp) return;
      if (detailNeedsSbpOverlay(inner, mock) && win.__ALFA_DETAIL_SKELETON__ && Array.isArray(win.__ALFA_DETAIL_SKELETON__.fields)) {
        const overlay = buildMockDetail(mock);
        Object.keys(inner).forEach((k) => { delete inner[k]; });
        Object.assign(inner, overlay);
      }
      applyMockFace(inner, mock);
    }

    function patchPfmStatistics(url, data) {
      if (!/personal-financial-manager\/statistics\/categories/i.test(String(url || ''))) return;
      const spend = CONFIG.spending?.monthTotal;
      if (spend == null || !data || typeof data !== 'object') return;
      const amount = data.expense && data.expense.total && data.expense.total.amount;
      if (amount && typeof amount === 'object') {
        const wholeRubles = Math.trunc(Math.abs(Number(spend)));
        amount.value = wholeRubles * 100;
        amount.minorUnits = 100;
        amount.currency = 'RUR';
      }
    }

    function transformData(url, data) {
      if (!data || typeof data !== 'object') return data;
      try {
        captureFromData(url, data);
        patchPfmStatistics(url, data);
        patchOperations(data, url);
        patchedCurrentAccount = false;
        walkPatchProducts(data, 0);
        patchIncomingDetail(url, data);
        patchTree(data, 0, false);
        patchIncomingDetail(url, data);
        forceSplitLinkInTree(data, 0);
      } catch (_) {
        /* ignore */
      }
      return data;
    }

    function transformJson(url, raw) {
      try {
        const data = nativeParse(raw);
        transformData(url, data);
        return JSON.stringify(data);
      } catch (_) {
        return raw;
      }
    }

    function shouldPatchParsed(text) {
      if (typeof text !== 'string' || text.length < 2 || text.length > 8e6) return false;
      const c = text.trim()[0];
      if (c !== '{' && c !== '[') return false;
      return /amount|balance|operations|accounts|firstName|layoutData|currency|products|transactions|приветств|помощник|message|chat|messages/i.test(text);
    }

    function cloneHeaders(res, contentType) {
      const h = new Headers(res.headers);
      h.delete('content-length');
      h.delete('content-encoding');
      if (contentType) h.set('content-type', contentType);
      return h;
    }

    function swapPdfResponse(url, res) {
      const ct = res.headers?.get?.('content-type') || '';
      const u = String(url || '').toLowerCase();
      // Operation «Получить квитанцию» must never get the statement PDF.
      // documents/pdf is shared; pick by screen + URL, not by leftover SPA text.
      if (
        !isReceiptView()
        && !urlLooksLikeReceipt(u)
        && (isStatementsListView() || isStatementFormView() || urlLooksLikeStatement(u))
        && statementPdfBytes()
      ) {
        if (urlLooksLikeStatement(u) || /documents\/pdf|pdf_viewer/i.test(u)) {
          return new Response(statementPdfBytes(), {
            status: 200,
            statusText: 'OK',
            headers: cloneHeaders(res, 'application/pdf'),
          });
        }
      }
      if (!isReceiptPdfRequest(url, ct)) return null;
      const id = idFromUrl(url) || mockIdFromUrl(url);
      let idx = receiptIndexForId(id);
      if (idx === null && id && MOCK_OPS[String(id)]) idx = MOCK_OPS[String(id)].__alfaMockIndex || 0;
      if (idx === null) idx = resolveReceiptIndex();
      if (idx === null) return null;
      const bytes = pdfBytesForIndex(idx);
      if (!bytes) return null;
      return new Response(bytes, {
        status: 200,
        statusText: 'OK',
        headers: cloneHeaders(res, 'application/pdf'),
      });
    }

    JSON.parse = function (text, reviver) {
      const data = nativeParse.call(this, text, reviver);
      if (shouldPatchParsed(text)) {
        try { transformData('', data); } catch (_) { /* ignore */ }
      }
      return data;
    };

    const origJson = Response.prototype.json;
    Response.prototype.json = function (...args) {
      return origJson.apply(this, args).then((data) => {
        transformData(String(this.url || ''), data);
        return data;
      });
    };

    const origText = Response.prototype.text;
    Response.prototype.text = function (...args) {
      return origText.apply(this, args).then((text) => {
        const url = String(this.url || '');
        const ct = this.headers?.get?.('content-type') || '';
        if (ct && !/json|javascript|text\/plain/i.test(ct)) return text;
        return transformJson(url, text);
      });
    };

    function viewText(field) {
      const view = field && field.view;
      if (view == null) return '';
      if (typeof view === 'string') return view;
      if (typeof view === 'object') return String(view.value || view.text || view.title || '');
      return '';
    }

    function setViewText(field, value) {
      if (!field || value == null) return;
      if (field.view == null || typeof field.view === 'string') {
        field.view = { type: 'TEXT', value: String(value) };
        return;
      }
      if (typeof field.view === 'object') {
        if ('value' in field.view) field.view.value = String(value);
        else if ('text' in field.view) field.view.text = String(value);
        else field.view.value = String(value);
      }
    }

    function patchDetailFields(detail, item, listOp) {
      const fields = detail && detail.fields;
      if (!Array.isArray(fields) || !item) return;
      const title = item.description || (listOp && listOp.title) || '';
      const phone = item.phone || '';
      const bank = (item.bank && (item.bank.categoryName || item.bank.short || item.bank.name)) || '';
      const amountText = formatAlfaMoney(item.amount, Number(item.amount) < 0);
      const analyticsAmount = Math.abs(Number(item.amount));
      const analyticsAmountText = Number.isInteger(analyticsAmount)
        ? `${String(analyticsAmount).replace(/\B(?=(\d{3})+(?!\d))/g, '\u00a0')}\u00a0₽`
        : formatAlfaMoney(analyticsAmount, false);
      const last4 = String(item.cardLast4 || '').replace(/\D/g, '').slice(-4);
      const paymentSub = bank
        ? (last4 ? `Перевод в ${bank} на карту ··${last4}` : `Перевод в ${bank} на карту`)
        : '';

      function setSduiByTitle(node, wantTitle, patch, depth) {
        if (!node || typeof node !== 'object' || depth > 14) return false;
        if (Array.isArray(node)) {
          return node.some((v) => setSduiByTitle(v, wantTitle, patch, depth + 1));
        }
        const t = node.title && typeof node.title === 'object' ? node.title.value : node.title;
        if (typeof t === 'string' && t.trim() === wantTitle) {
          if (patch.subtitle != null) {
            if (node.subtitle && typeof node.subtitle === 'object' && 'value' in node.subtitle) {
              node.subtitle.value = patch.subtitle;
            } else if ('subtitle' in node || node.dataContent) {
              if (node.dataContent && node.dataContent.subtitle && typeof node.dataContent.subtitle === 'object') {
                node.dataContent.subtitle.value = patch.subtitle;
              } else {
                node.subtitle = patch.subtitle;
              }
            }
            if (node.dataContent && typeof node.dataContent === 'object') {
              if (node.dataContent.subtitle && typeof node.dataContent.subtitle === 'object' && 'value' in node.dataContent.subtitle) {
                node.dataContent.subtitle.value = patch.subtitle;
              } else if ('subtitle' in node.dataContent) {
                node.dataContent.subtitle = typeof node.dataContent.subtitle === 'object'
                  ? Object.assign({}, node.dataContent.subtitle, { value: patch.subtitle })
                  : patch.subtitle;
              }
            }
          }
          return true;
        }
        return Object.keys(node).some((key) => setSduiByTitle(node[key], wantTitle, patch, depth + 1));
      }

      function patchAnalyticsMoney(node, depth) {
        if (!node || typeof node !== 'object' || depth > 12) return;
        if (Array.isArray(node)) {
          node.forEach((v) => patchAnalyticsMoney(v, depth + 1));
          return;
        }
        Object.keys(node).forEach((key) => {
          const val = node[key];
          if (
            key === 'value' &&
            typeof val === 'string' &&
            /^[−-]?\d{1,3}(?:[\s\u00a0\u202f]\d{3})*(?:[.,]\d{2})?[\s\u00a0\u202f]*₽$/.test(val.trim())
          ) {
            node[key] = analyticsAmountText;
          } else if (val && typeof val === 'object') {
            patchAnalyticsMoney(val, depth + 1);
          }
        });
      }
      fields.forEach((field) => {
        if (field && field.id === 'mainCategory') {
          patchAnalyticsMoney(field, 0);
        }
        const label = String((field && field.label) || '').toLowerCase();
        if (/получател|кому|получатель/i.test(label) && title) setViewText(field, title);
        else if (/телефон|phone|номер телефона/i.test(label) && phone) setViewText(field, phone);
        else if (/банк/i.test(label) && !/сбп|nspk/i.test(label) && bank) setViewText(field, bank);
        else if (/сумм/i.test(label) && amountText) setViewText(field, amountText);
        else if (/комментар/i.test(label)) {
          setViewText(field, isCardKind(item) ? '' : 'Перевод денежных средств');
        }
      });
      if (isCardKind(item)) {
        fields.forEach((field) => {
          if (!field) return;
          setSduiByTitle(field, 'Карта списания', { subtitle: title || 'Альфа-карта МИР' }, 0);
          if (paymentSub) setSduiByTitle(field, 'Детали платежа', { subtitle: paymentSub }, 0);
        });
      } else {
        fields.forEach((field) => {
          const cur = viewText(field);
          if (title && looksLikePersonTitle(cur) && cur !== title) {
            setViewText(field, title);
          }
        });
      }
    }

    function isOpDetailRequest(url) {
      return /operations-history\/operations\/[^/?#]+/i.test(String(url || ''));
    }

    function isOpsListRequest(url) {
      const u = String(url || '');
      if (isOpDetailRequest(u)) return false;
      return /operations-history\/operations\/?(\?|$|#)/i.test(u);
    }

    function mockIdFromText(text) {
      const s = String(text || '');
      const keys = Object.keys(MOCK_OPS);
      for (let i = 0; i < keys.length; i++) {
        const id = keys[i];
        if (id && s.indexOf(id) >= 0) return id;
      }
      const m = s.match(/alfa-mock-[A-Za-z0-9]+/);
      if (m && MOCK_OPS[m[0]]) return m[0];
      return null;
    }

    function mockIdFromUrl(url) {
      const s = String(url || '');
      const fromAll = mockIdFromText(s);
      if (fromAll) return fromAll;
      const tail = s.match(/operations-history\/operations\/([^/?#]+)/i);
      if (tail && MOCK_OPS[decodeURIComponent(tail[1])]) return decodeURIComponent(tail[1]);
      return null;
    }

    function buildMockDetail(op) {
      const item = op.__alfaMockItem || {};
      const sk = win.__ALFA_DETAIL_SKELETON__;
      const d = sk ? cloneJson(sk) : cloneJson(op);
      ['id', 'operationId', 'title', 'logoUrl', 'amount', 'direction', 'dateTime', 'status', 'bottomBadge', 'clickReference', 'reference', 'deepLink'].forEach((key) => {
        if (key in op && op[key] != null) d[key] = cloneJson(op[key]);
      });
      if (sk && sk.category && typeof sk.category === 'object') {
        d.category = cloneJson(sk.category);
        d.category.name = typeof op.category === 'string' ? op.category : (op.category && op.category.name) || d.category.name;
      } else {
        d.category = op.category;
      }
      d.id = op.id;
      if (op.operationId) d.operationId = op.operationId;
      d.comment = 'Перевод денежных средств';
      d.status = d.status || 'SUCCESS';
      d.__alfaMockOp = true;
      if (!Array.isArray(d.fields) && Array.isArray(sk && sk.fields)) d.fields = cloneJson(sk.fields);
      if (sk && hasReceiptAction(sk.actions)) d.actions = cloneJson(sk.actions);
      patchDetailFields(d, item, op);
      ensureReceiptAction(d, item);
      ensureSplitAction(d);
      if (d.actions && Array.isArray(d.actions) && op.id) {
        const oldIds = collectIdStrings(d).concat(collectIdStrings(sk || {}));
        replaceIdStrings(d, oldIds.filter((id) => id && id !== String(op.id)), String(op.id), 0);
        d.id = op.id;
      }
      registerReceiptIds(d, op.__alfaMockIndex || 0);
      return d;
    }

    function jsonResponse(obj) {
      return new Response(JSON.stringify(obj), {
        status: 200,
        statusText: 'OK',
        headers: { 'content-type': 'application/json' },
      });
    }

    async function hydrateSkeleton(op) {
      if (win.__ALFA_DETAIL_SKELETON__) return;
      const did = op && op.__alfaDonorId;
      if (!did) return;
      try {
        const res = await origFetch('api/v1/operations-history/operations/' + encodeURIComponent(String(did)), {
          credentials: 'same-origin',
          headers: { accept: 'application/json' },
        });
        if (!res || !res.ok) return;
        const buf = await res.arrayBuffer();
        const data = nativeParse(new TextDecoder().decode(buf));
        const obj = data && (data.fields ? data : (data.operation || data.payload || data.result || data));
        if (obj && Array.isArray(obj.fields) && obj.fields.length && hasReceiptAction(obj.actions)) {
          win.__ALFA_DETAIL_SKELETON__ = cloneJson(obj);
          persistMocks();
        }
      } catch (_) { /* ignore */ }
    }

    function ensureSkeleton(donor) {
      if (!donor) return;
      hydrateSkeleton({ __alfaDonorId: donor.id, id: donor.id });
    }

    function applyFakeXhr(xhr, body) {
      if (xhr.__alfaMockFaked) return;
      xhr.__alfaMockFaked = true;
      try {
        Object.defineProperty(xhr, 'readyState', { configurable: true, get() { return 4; } });
        Object.defineProperty(xhr, 'status', { configurable: true, get() { return 200; } });
        Object.defineProperty(xhr, 'statusText', { configurable: true, get() { return 'OK'; } });
        Object.defineProperty(xhr, 'responseText', { configurable: true, get() { return body; } });
        Object.defineProperty(xhr, 'response', {
          configurable: true,
          get() {
            if (xhr.responseType === 'json') {
              try { return nativeParse(body); } catch (_) { return null; }
            }
            return body;
          },
        });
      } catch (_) { /* ignore */ }
      try {
        if (typeof xhr.onreadystatechange === 'function') xhr.onreadystatechange();
      } catch (_) { /* ignore */ }
      try {
        if (typeof xhr.onload === 'function') xhr.onload();
      } catch (_) { /* ignore */ }
      try { xhr.dispatchEvent(new Event('readystatechange')); } catch (_) { /* ignore */ }
      try { xhr.dispatchEvent(new Event('load')); } catch (_) { /* ignore */ }
      try { xhr.dispatchEvent(new Event('loadend')); } catch (_) { /* ignore */ }
    }

    const origFetch = win.fetch.bind(win);

    async function donorsFromPages(url, init) {
      if ((win.__ALFA_SBP_DONORS || []).length) return;
      const base = String(url || win.__ALFA_LAST_OPS_URL || 'api/v1/operations-history/operations');
      const srcInit = init || win.__ALFA_LAST_OPS_INIT || { credentials: 'same-origin', headers: { accept: 'application/json' } };
      for (let page = 2; page <= 8; page++) {
        const opts = {};
        Object.keys(srcInit || {}).forEach((k) => { opts[k] = srcInit[k]; });
        opts.credentials = opts.credentials || 'same-origin';
        let reqUrl = base;
        if (typeof opts.body === 'string' && opts.body.trim().charAt(0) === '{') {
          try {
            const b = nativeParse(opts.body);
            b.page = page;
            if (b.size == null) b.size = 20;
            opts.body = JSON.stringify(b);
          } catch (_) { /* ignore */ }
        } else {
          reqUrl = reqUrl.replace(/[?&]page=\d+/i, '').replace(/[?&]size=\d+/i, '');
          const join = reqUrl.indexOf('?') >= 0 ? '&' : '?';
          reqUrl = reqUrl + join + 'page=' + page + '&size=20';
        }
        try {
          const res = await origFetch(reqUrl, opts);
          if (!res || !res.ok) continue;
          const buf = await res.arrayBuffer();
          const data = nativeParse(new TextDecoder().decode(buf));
          findAllOperationLists(data).forEach(rememberSbpDonors);
          if ((win.__ALFA_SBP_DONORS || []).length) return;
        } catch (_) { /* ignore */ }
      }
    }

    win.fetch = async function (...args) {
      const url = String(args[0]?.url || args[0] || '');
      if (SPLIT_ENABLED && isPaymentSplittingUrl(url)) {
        const sid = opIdFromSplitUrl(url);
        const mock = (sid && MOCK_OPS[sid]) || null;
        return jsonResponse(buildSplitPayload(sid, mock));
      }
      const mockId = mockIdFromUrl(url) || mockIdFromText(typeof args[1]?.body === 'string' ? args[1].body : '');
      const mock = mockId && MOCK_OPS[mockId];
      if (mock && mock.__alfaMockNid && isOpDetailRequest(url) && win.__ALFA_DETAIL_SKELETON__ && Array.isArray(win.__ALFA_DETAIL_SKELETON__.fields)) {
        try {
          return jsonResponse(buildMockDetail(mock));
        } catch (_) { /* fall through */ }
      }
      const res = await origFetch(...args);
      if (mock && mock.__alfaMockNid && !res.ok) {
        try {
          await hydrateSkeleton(mock);
          if (win.__ALFA_DETAIL_SKELETON__ && Array.isArray(win.__ALFA_DETAIL_SKELETON__.fields)) {
            return jsonResponse(buildMockDetail(mock));
          }
        } catch (_) { /* ignore */ }
      }
      const ct = res.headers.get('content-type') || '';
      if (isReceiptPdfRequest(url, ct) || /documents\/pdf|pdf_viewer/i.test(url)) {
        const swapped = swapPdfResponse(url, res);
        if (swapped) return swapped;
        // Block Alfa HTML stamp receipt when our PDF is missing — empty 404
        // is better than showing «Перевод по СБП / ИСПОЛНЕНО».
        if (/documents\/pdf|pdf_viewer|квитан|receipt/i.test(url)) {
          return new Response('PDF not embedded in userscript', {
            status: 404,
            statusText: 'Not Found',
            headers: { 'content-type': 'text/plain; charset=utf-8' },
          });
        }
      }
      const looksJson = !ct || /json|javascript|text\/plain/i.test(ct) || /alfabank\.ru/i.test(url);
      if (!looksJson) return res;
      try {
        const text = await res.clone().text();
        if (isOpsListRequest(url)) {
          win.__ALFA_LAST_OPS_URL = url;
          win.__ALFA_LAST_OPS_INIT = args[1];
          try {
            const data = nativeParse(text);
            const lists = findAllOperationLists(data);
            lists.forEach(rememberSbpDonors);
            if (isFirstOpsPage(data, url) && CONFIG.operations?.length && !findSbpTemplate(lists[0] || [])) {
              await donorsFromPages(url, args[1]);
            }
          } catch (_) { /* ignore */ }
        }
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

    const XHR = win.XMLHttpRequest;
    const origOpen = XHR.prototype.open;
    const origSend = XHR.prototype.send;
    const origAddEventListener = XHR.prototype.addEventListener;
    XHR.prototype.open = function (method, url, ...rest) {
      this.__alfaMockUrl = String(url || '');
      // Keep the real donor response; patch only its data fields below.
      return origOpen.call(this, method, url, ...rest);
    };
    XHR.prototype.addEventListener = function (type, listener, ...rest) {
      if ((type === 'load' || type === 'readystatechange' || type === 'error') && typeof listener === 'function') {
        const wrapped = function (...cbArgs) {
          const url = this.__alfaMockUrl || '';
          const mockId = mockIdFromUrl(url) || mockIdFromText(this.__alfaMockBody);
          if (!this.__alfaMockFaked && this.readyState === 4 && mockId && MOCK_OPS[mockId] && MOCK_OPS[mockId].__alfaMockNid && this.status >= 400) {
            applyFakeXhr(this, JSON.stringify(buildMockDetail(MOCK_OPS[mockId])));
          } else if (!this.__alfaMockFaked && this.readyState === 4 && this.status >= 200 && this.status < 300) {
            try {
              if (this.responseType === 'json' && this.response && typeof this.response === 'object') {
                transformData(url, this.response);
              } else {
                const ct = this.getResponseHeader('content-type') || '';
                const canText = !this.responseType || this.responseType === 'text' || this.responseType === '';
                if (canText && (!ct || /json|javascript|text\/plain/i.test(ct))) {
                  const raw = this.responseText;
                  const patched = transformJson(url, raw);
                  if (patched !== raw) {
                    Object.defineProperty(this, 'responseText', { configurable: true, value: patched });
                    Object.defineProperty(this, 'response', { configurable: true, value: patched });
                  }
                }
              }
            } catch (_) { /* ignore */ }
          }
          return listener.apply(this, cbArgs);
        };
        return origAddEventListener.call(this, type, wrapped, ...rest);
      }
      return origAddEventListener.call(this, type, listener, ...rest);
    };
    XHR.prototype.send = function (...args) {
      const url = this.__alfaMockUrl || '';
      const bodyArg = args[0];
      this.__alfaMockBody = typeof bodyArg === 'string' ? bodyArg : '';
      if (SPLIT_ENABLED && isPaymentSplittingUrl(url)) {
        const sid = opIdFromSplitUrl(url);
        const mock = (sid && MOCK_OPS[sid]) || null;
        applyFakeXhr(this, JSON.stringify(buildSplitPayload(sid, mock)));
        return;
      }
      // Short-circuit native receipt XHR → embedded Oracle PDF (blocks HTML stamp).
      if (isReceiptPdfRequest(url, '') || /documents\/pdf|pdf_viewer/i.test(url)) {
        const wantSt = (
          !isReceiptView()
          && !urlLooksLikeReceipt(url)
          && (isStatementsListView() || isStatementFormView() || urlLooksLikeStatement(url))
        );
        const idx = wantSt ? 'st' : resolveReceiptIndex();
        const bytes = idx != null ? pdfBytesForIndex(idx) : (wantSt ? statementPdfBytes() : null);
        if (bytes) {
          const xhr = this;
          const blob = new Blob([bytes], { type: 'application/pdf' });
          const finish = () => {
            try {
              Object.defineProperty(xhr, 'readyState', { configurable: true, get() { return 4; } });
              Object.defineProperty(xhr, 'status', { configurable: true, get() { return 200; } });
              Object.defineProperty(xhr, 'statusText', { configurable: true, get() { return 'OK'; } });
              Object.defineProperty(xhr, 'response', {
                configurable: true,
                get() {
                  if (xhr.responseType === 'arraybuffer') {
                    return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
                  }
                  return blob;
                },
              });
              Object.defineProperty(xhr, 'responseText', { configurable: true, get() { return ''; } });
              xhr.getResponseHeader = function (name) {
                if (String(name || '').toLowerCase() === 'content-type') return 'application/pdf';
                return null;
              };
              xhr.getAllResponseHeaders = function () {
                return 'content-type: application/pdf\r\n';
              };
            } catch (_) { /* ignore */ }
            try { if (typeof xhr.onreadystatechange === 'function') xhr.onreadystatechange(); } catch (_) { /* ignore */ }
            try { if (typeof xhr.onload === 'function') xhr.onload(); } catch (_) { /* ignore */ }
            try { xhr.dispatchEvent(new Event('readystatechange')); } catch (_) { /* ignore */ }
            try { xhr.dispatchEvent(new Event('load')); } catch (_) { /* ignore */ }
            try { xhr.dispatchEvent(new Event('loadend')); } catch (_) { /* ignore */ }
          };
          setTimeout(finish, 0);
          return;
        }
      }
      if (isOpsListRequest(url)) {
        win.__ALFA_LAST_OPS_URL = url;
        win.__ALFA_LAST_OPS_INIT = {
          credentials: 'same-origin',
          headers: { accept: 'application/json', 'content-type': 'application/json' },
          method: this.__alfaMockBody ? 'POST' : 'GET',
          body: this.__alfaMockBody || undefined,
        };
      }
      const mockId = mockIdFromUrl(url) || mockIdFromText(this.__alfaMockBody);
      const origOnReady = this.onreadystatechange;
      this.onreadystatechange = function (...cbArgs) {
        const mockHit = mockIdFromUrl(url) || mockIdFromText(this.__alfaMockBody);
        if (this.readyState === 4 && mockHit && MOCK_OPS[mockHit] && MOCK_OPS[mockHit].__alfaMockNid && this.status >= 400) {
          applyFakeXhr(this, JSON.stringify(buildMockDetail(MOCK_OPS[mockHit])));
        } else if (this.readyState === 4 && this.status >= 200 && this.status < 300) {
          const xhr = this;
          const applyPatch = () => {
            try {
              if (xhr.responseType === 'json' && xhr.response && typeof xhr.response === 'object') {
                transformData(url, xhr.response);
              } else {
                const ct = xhr.getResponseHeader('content-type') || '';
                const canText = !xhr.responseType || xhr.responseType === 'text' || xhr.responseType === '';
                if (canText && (!ct || /json|javascript|text\/plain/i.test(ct))) {
                  const raw = xhr.responseText;
                  const patched = transformJson(url, raw);
                  if (patched !== raw) {
                    Object.defineProperty(xhr, 'responseText', { configurable: true, value: patched });
                    Object.defineProperty(xhr, 'response', { configurable: true, value: patched });
                  }
                }
              }
            } catch (_) { /* ignore */ }
            if (origOnReady) origOnReady.apply(xhr, cbArgs);
          };
          if (isOpsListRequest(url) && !xhr.__alfaOpsWait) {
            let parsed = null;
            try {
              parsed = xhr.responseType === 'json' && xhr.response ? xhr.response : nativeParse(xhr.responseText);
            } catch (_) { /* ignore */ }
            const lists = parsed ? findAllOperationLists(parsed) : [];
            lists.forEach(rememberSbpDonors);
            if (parsed && isFirstOpsPage(parsed, url) && CONFIG.operations?.length && !findSbpTemplate(lists[0] || [])) {
              xhr.__alfaOpsWait = true;
              donorsFromPages(url, win.__ALFA_LAST_OPS_INIT).then(applyPatch).catch(applyPatch);
              return;
            }
          }
          applyPatch();
          return;
        }
        if (origOnReady) origOnReady.apply(this, cbArgs);
      };
      return origSend.apply(this, args);
    };

    try {
      let realMain = win.__main;
      const wrapMain = (fn) => {
        if (typeof fn !== 'function' || fn.__alfaWrapped) return fn;
        const wrapped = function (data) {
          try { transformData('__main', data); } catch (_) { /* ignore */ }
          return fn.apply(this, arguments);
        };
        wrapped.__alfaWrapped = true;
        return wrapped;
      };
      Object.defineProperty(win, '__main', {
        configurable: true,
        enumerable: true,
        get() { return realMain; },
        set(v) { realMain = wrapMain(v); },
      });
      if (typeof realMain === 'function') realMain = wrapMain(realMain);
    } catch (_) { /* ignore */ }

    const MONEY_FULL = /^\d{1,3}(?:[\s\u00a0\u202f\u2009\u2007\u2008\u200a]\d{3})*(?:[.,]\d{2})?[\s\u00a0\u202f\u2009\u2007]*₽$/;
    const MONEY_NUM = /^\d{1,3}(?:[\s\u00a0\u202f\u2009\u2007\u2008\u200a]\d{3})*(?:[.,]\d{2})$/;

    function walkShadows(root, fn) {
      if (!root) return;
      fn(root);
      try {
        root.querySelectorAll('*').forEach((el) => {
          if (el.shadowRoot) walkShadows(el.shadowRoot, fn);
        });
      } catch (_) { /* ignore */ }
    }

    function isMoneyLabel(text) {
      const t = String(text || '').replace(/[\s\u00a0\u202f\u2009\u2007\u2008\u200a]/g, ' ').trim();
      return /^\d{1,3}(?: \d{3})*(?:[.,]\d{2})? ?₽$/.test(t);
    }

    function replaceMoneyBlock(card, rubles) {
      const money = formatAlfaMoney(rubles, false);
      let best = null;
      let bestLen = 1e9;
      try {
        card.querySelectorAll('*').forEach((el) => {
          if (el.closest('input,textarea,[contenteditable="true"]')) return;
          const t = (el.innerText || '').trim();
          if (!isMoneyLabel(t)) return;
          if (t.length < bestLen) {
            best = el;
            bestLen = t.length;
          }
        });
      } catch (_) { /* ignore */ }
      if (!best) return;
      const cur = (best.innerText || '').replace(/[\s\u00a0\u202f]/g, ' ').trim();
      const want = money.replace(/[\s\u00a0\u202f]/g, ' ').trim();
      if (cur !== want) best.textContent = money;
    }

    function replaceMoneyNodes(root, rubles, signed) {
      const money = formatAlfaMoney(rubles, signed);
      const num = formatAlfaNumber(Math.abs(Number(rubles)));
      const walker = root.createTreeWalker
        ? root.createTreeWalker(root, NodeFilter.SHOW_TEXT)
        : document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
      let node;
      while ((node = walker.nextNode())) {
        const parent = node.parentElement;
        if (!parent || parent.closest('input,textarea,[contenteditable="true"]')) continue;
        const trimmed = (node.textContent || '').trim();
        if (MONEY_FULL.test(trimmed) && trimmed !== money) {
          node.textContent = money;
        } else if (
          MONEY_NUM.test(trimmed) &&
          parent.parentElement &&
          /₽/.test(parent.parentElement.textContent || '')
        ) {
          node.textContent = num;
        }
      }
      replaceMoneyBlock(root, rubles);
    }

    function productRoots(root) {
      const found = new Set();
      const add = (el) => { if (el) found.add(el); };
      try {
        root.querySelectorAll(
          '[data-test-id*="product"],[data-test-id*="account"],[data-test-id*="balance"],[data-test-id*="widget"]'
        ).forEach(add);
      } catch (_) { /* ignore */ }
      const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
      let n;
      while ((n = walker.nextNode())) {
        if (!/текущий сч[её]т|alfa.?сч[её]т|накопительн|кредитн|сч[её]т\s|карта/i.test(n.textContent || '')) continue;
        let p = n.parentElement;
        for (let i = 0; i < 8 && p; i++) {
          if ((p.innerText || '').includes('₽')) {
            add(p);
            break;
          }
          p = p.parentElement;
        }
      }
      return found;
    }

    function isHistoryView() {
      try {
        const p = (location.pathname + location.hash + location.search).toLowerCase();
        if (/histor|operations-history|\/history\b/.test(p)) return true;
      } catch (_) { /* ignore */ }
      try {
        const titles = document.querySelectorAll('h1,h2,[role="heading"]');
        for (let i = 0; i < titles.length; i++) {
          const t = (titles[i].textContent || '').trim();
          if (/^история/i.test(t)) return true;
        }
      } catch (_) { /* ignore */ }
      return false;
    }

    function isOpDetailView() {
      try {
        const p = (location.pathname + location.hash + location.search).toLowerCase();
        if (/operations-history\/operations\/[^/?#]+/.test(p)) return true;
      } catch (_) { /* ignore */ }
      const text = (document.body && document.body.innerText) || '';
      return /операция выполнена/i.test(text) && /финансовая аналитика/i.test(text);
    }

    function patchOpDetailIcons(root) {
      if (!root || !isOpDetailView()) return;
      const body = root.innerText || '';
      let item = null;
      (CONFIG.operations || []).some((op) => {
        if (op && op.description && body.indexOf(op.description) >= 0) {
          item = op;
          return true;
        }
        return false;
      });
      if (!item && (CONFIG.operations || []).length === 1) item = CONFIG.operations[0];
      if (!item) return;
      if (isCardKind(item) || isAlfaInternalKind(item)) return;
      const bankSrc = bankLogoSrc(item.bank);
      const badgeSrc = sbpBadgeSrc();
      let imgs = [];
      try {
        imgs = Array.from(root.querySelectorAll('img'));
      } catch (_) { return; }
      if (!imgs.length) return;
      // Header logos sit above «Операция выполнена» / amount — prefer top of screen.
      const scored = imgs.map((img) => {
        let r;
        try { r = img.getBoundingClientRect(); } catch (_) { r = { top: 9999, width: 0, height: 0 }; }
        const area = (r.width || 0) * (r.height || 0);
        const src = String(img.currentSrc || img.src || '');
        return { img, top: r.top || 0, area, src };
      }).filter((x) => x.area > 0 || x.src);
      scored.sort((a, b) => a.top - b.top || b.area - a.area);
      const header = scored.filter((x) => x.top < 420).slice(0, 6);
      const pool = header.length ? header : scored.slice(0, 4);
      let main = null;
      let mainArea = 0;
      pool.forEach((x) => {
        if (x.area > mainArea && !isSbpBadgeUrl(x.src)) {
          mainArea = x.area;
          main = x;
        }
      });
      if (main && bankSrc) {
        main.img.src = bankSrc;
        main.img.removeAttribute('srcset');
      }
      let badgeHit = false;
      pool.forEach((x) => {
        if (x === main) return;
        const looksBadge = isSbpBadgeUrl(x.src) || (x.area > 0 && x.area < 2200) || (main && x.area > 0 && x.area < mainArea * 0.45);
        if (!looksBadge && !imgLooksBroken(x.img)) return;
        x.img.src = badgeSrc;
        x.img.removeAttribute('srcset');
        x.img.setAttribute('data-alfa-sbp-badge', '1');
        badgeHit = true;
      });
      if (!badgeHit && main && main.img && main.img.parentElement) {
        // Inject corner badge next to bank logo when native SBP img never appeared.
        let badge = main.img.parentElement.querySelector('[data-alfa-sbp-badge="1"]');
        if (!badge) {
          badge = document.createElement('img');
          badge.setAttribute('data-alfa-sbp-badge', '1');
          badge.alt = 'СБП';
          const size = Math.max(18, Math.round((main.img.getBoundingClientRect().width || 56) * 0.38));
          badge.style.cssText = 'position:absolute;right:-2px;bottom:-2px;width:' + size + 'px;height:' + size + 'px;border-radius:50%;object-fit:cover;z-index:2;';
          const host = main.img.parentElement;
          try {
            const cs = win.getComputedStyle(host);
            if (cs && cs.position === 'static') host.style.position = 'relative';
          } catch (_) { host.style.position = 'relative'; }
          host.appendChild(badge);
        }
        badge.src = badgeSrc;
      }
    }

    function imgLooksBroken(img) {
      try {
        if (!img) return true;
        if (img.complete && img.naturalWidth === 0) return true;
      } catch (_) { /* ignore */ }
      return false;
    }

    function patchOpDetailDom(root) {
      if (!root || !isOpDetailView()) return;
      const body = root.innerText || '';
      let item = null;
      let idx = null;
      (CONFIG.operations || []).some((op, i) => {
        if (op && op.description && body.indexOf(op.description) >= 0) {
          item = op;
          idx = i;
          return true;
        }
        return false;
      });
      if (!item) {
        (CONFIG.operations || []).some((op, i) => {
          if (op && pdfBytesForIndex(i)) {
            item = op;
            idx = i;
            return true;
          }
          return false;
        });
      }
      if (!item) return;
      if (root.querySelector('[data-alfa-mock-actions="receipt"]')) return;
      // Already on screen (native SBP) — click-guard opens our PDF.
      if (/получить квитанцию/i.test(body)) return;

      const wrap = document.createElement('div');
      wrap.setAttribute('data-alfa-mock-actions', 'receipt');
      wrap.style.cssText = 'box-sizing:border-box;width:calc(100% - 38px);margin:8px 19px 0;border-top:1px solid rgba(255,255,255,.10);padding:8px 0 0;background:transparent;';
      const row = document.createElement('div');
      row.setAttribute('role', 'button');
      row.setAttribute('data-alfa-mock-action', 'receipt');
      row.style.cssText = 'box-sizing:border-box;height:58px;display:flex;align-items:center;gap:16px;padding:0 8px;color:#f5f5f7;font:400 16px/1.2 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;cursor:pointer;';
      row.innerHTML = '<span style="width:24px;height:24px;display:block;flex:0 0 24px;color:#f5f5f7;">'
        + '<svg viewBox="0 0 28 28"><path d="M7 3.5h14v21l-3-2-4 2-4-2-3 2z"/><path d="M10 9h8M10 14h8"/></svg>'
        + '</span><span>Получить квитанцию</span>';
      const svg = row.querySelector('svg');
      if (svg) svg.style.cssText = 'width:24px;height:24px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round;';
      const open = (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        if (ev.stopImmediatePropagation) ev.stopImmediatePropagation();
        openMockReceipt(idx);
      };
      row.addEventListener('click', open, true);
      row.addEventListener('touchend', open, true);
      wrap.appendChild(row);

      let anchor = null;
      try {
        root.querySelectorAll('button,a,[role="button"],div,span,p').forEach((el) => {
          if (anchor) return;
          const t = (el.textContent || '').replace(/\s+/g, ' ').trim();
          if (t === 'Повторить операцию' || t === 'Финансовая аналитика') anchor = el;
        });
      } catch (_) { /* ignore */ }
      if (anchor) {
        let section = anchor;
        for (let i = 0; i < 6 && section.parentElement; i++) {
          const p = section.parentElement;
          const text = (p.textContent || '').replace(/\s+/g, ' ').trim();
          if (/Повторить операцию|Финансовая аналитика/.test(text) && text.length < 500) {
            section = p;
            break;
          }
          section = p;
        }
        if (section.parentElement) {
          section.parentElement.insertBefore(wrap, section);
          return;
        }
      }
      root.appendChild(wrap);
    }

    function patchSpendingDom(root) {
      if (isOpDetailView()) return;
      const spend = CONFIG.spending?.monthTotal;
      if (spend == null) return;
      const replaceNearLabel = (labelNode) => {
        let box = labelNode.parentElement;
        const amountLabel = /^[−-]?\d{1,3}(?:[\s\u00a0\u202f\u2009]\d{3})*(?:[.,]\d{1,2})?(?:[\s\u00a0\u202f\u2009]*₽)?$/;
        for (let level = 0; level < 7 && box; level++, box = box.parentElement) {
          let candidate = null;
          try {
            const nodes = box.querySelectorAll('*');
            for (let i = 0; i < nodes.length; i++) {
              const el = nodes[i];
              if (el.children.length || el.closest('input,textarea,[contenteditable="true"]')) continue;
              const text = (el.textContent || '').replace(/\s+/g, ' ').trim();
              if (amountLabel.test(text)) {
                candidate = el;
                break;
              }
            }
          } catch (_) { /* ignore */ }
          if (!candidate) continue;
          const current = (candidate.textContent || '').trim();
          candidate.textContent = /₽/.test(current)
            ? formatAlfaMoney(spend, false)
            : formatAlfaNumber(Math.abs(Number(spend)));
          return true;
        }
        return false;
      };
      const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
      let n;
      while ((n = walker.nextNode())) {
        const raw = (n.textContent || '').replace(/\s+/g, ' ').trim();
        if (raw.length > 36 || !/^(расходы(?:\s+за)?|траты)\b/i.test(raw)) continue;
        if (/разделе расход/i.test(raw)) continue;
        if (replaceNearLabel(n)) continue;
        let p = n.parentElement;
        for (let i = 0; i < 6 && p; i++) {
          if (/₽/.test(p.innerText || '')) {
            replaceMoneyBlock(p, spend);
            break;
          }
          p = p.parentElement;
        }
      }
    }

    function isReceiptView() {
      try {
        const nodes = document.querySelectorAll('button,a,[role="button"]');
        const n = Math.min(nodes.length, 40);
        for (let i = 0; i < n; i++) {
          if (/получить квитанцию/i.test(nodes[i].textContent || '')) return true;
        }
      } catch (_) { /* ignore */ }
      return false;
    }

    function isProfileView() {
      try {
        const p = (location.pathname + location.hash + location.search).toLowerCase();
        if (/user.?profile|\/profile|personal|мои.?данные|documents/i.test(p)) return true;
      } catch (_) { /* ignore */ }
      const t = ((document.body && document.body.innerText) || '').replace(/\s+/g, ' ');
      return /Мои документы/i.test(t) && (/Паспорт/i.test(t) || /Базовый уровень/i.test(t) || /\bИНН\b/i.test(t));
    }

    function patchProfileDom(root) {
      if (!root || !isProfileView()) return;
      const face = profileFaceName();
      const docs = mockDocs();
      if (!face) return;
      const walker = root.createTreeWalker
        ? root.createTreeWalker(root, NodeFilter.SHOW_TEXT)
        : document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
      let node;
      while ((node = walker.nextNode())) {
        const parent = node.parentElement;
        if (!parent || parent.closest('input,textarea,[contenteditable="true"]')) continue;
        const raw = node.textContent || '';
        const trimmed = raw.trim();
        if (!trimmed) continue;

        // Full name on profile header: «Инесса Тугова» → forge Имя+Фамилия.
        if (
          looksLikePersonTitle(trimmed)
          && trimmed !== face
          && trimmed.indexOf('₽') < 0
          && !/паспорт|инн|базовый|документ|уровень/i.test(trimmed)
        ) {
          try {
            const rect = parent.getBoundingClientRect();
            if (rect.top >= 0 && rect.top < 420 && trimmed.split(/\s+/).length >= 2) {
              node.textContent = raw.replace(trimmed, face);
              continue;
            }
          } catch (_) { /* ignore */ }
        }

        // Passport card: 9219 652102
        if (/^\d{4}\s+\d{6}$/.test(trimmed) && trimmed !== docs.passport) {
          const around = ((parent.closest('a,div,li,section,article') || parent).innerText || '');
          if (/паспорт/i.test(around) || !/\bИНН\b/i.test(around)) {
            node.textContent = raw.replace(trimmed, docs.passport);
            continue;
          }
        }

        // INN card: 10–12 digits
        if (/^\d{10,12}$/.test(trimmed.replace(/\s/g, '')) && trimmed.replace(/\s/g, '') !== docs.inn) {
          const around = ((parent.closest('a,div,li,section,article') || parent).innerText || '');
          if (/\bИНН\b/i.test(around)) {
            node.textContent = raw.replace(trimmed, docs.inn);
          }
        }
      }

      // Also rewrite by card blocks if text split across nodes.
      try {
        root.querySelectorAll('a,div,li,section,article').forEach((el) => {
          const t = (el.innerText || '').replace(/\s+/g, ' ').trim();
          if (t.length > 80 || t.length < 8) return;
          if (/^Паспорт/i.test(t) || /\bПаспорт\b/i.test(t)) {
            const m = t.match(/\d{4}\s+\d{6}/);
            if (m && m[0] !== docs.passport) {
              el.querySelectorAll('*').forEach((child) => {
                if (child.children.length) return;
                const ct = (child.textContent || '').trim();
                if (/^\d{4}\s+\d{6}$/.test(ct)) child.textContent = docs.passport;
              });
            }
          }
          if (/\bИНН\b/i.test(t)) {
            el.querySelectorAll('*').forEach((child) => {
              if (child.children.length) return;
              const ct = (child.textContent || '').replace(/\s/g, '').trim();
              if (/^\d{10,12}$/.test(ct) && ct !== docs.inn) child.textContent = docs.inn;
            });
          }
        });
      } catch (_) { /* ignore */ }
    }

    function patchChatNameDom(root) {
      const first = forgeFirstName();
      if (!first || !root) return;
      const walker = root.createTreeWalker
        ? root.createTreeWalker(root, NodeFilter.SHOW_TEXT)
        : document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
      let node;
      while ((node = walker.nextNode())) {
        const parent = node.parentElement;
        if (!parent || parent.closest('input,textarea,[contenteditable="true"]')) continue;
        const raw = node.textContent || '';
        const trimmed = raw.trim();
        if (!raw) continue;
        // Agent label above bubble («Имя Фамилия») — never touch.
        if (
          looksLikePersonTitle(trimmed)
          && !/Приветств|Здравств|Помощник|Чем могу|тему|вопрос/i.test(raw)
        ) {
          continue;
        }
        if (!/Инесса|Приветств|Здравств|Помощник|Чем могу/i.test(raw)
          && !LIVE_CLIENT_FIRST.has(trimmed.toLowerCase())) {
          continue;
        }
        const next = rewriteClientFirstInText(raw);
        if (next !== raw) node.textContent = next;
      }
    }

    function patchNameDom(root) {
      if (isHistoryView() || isReceiptView()) return;
      if (isChatView()) {
        patchChatNameDom(root);
        return;
      }
      if (isProfileView()) {
        patchProfileDom(root);
        return;
      }
      const first = forgeFirstName();
      if (!first) return;
      const walker = root.createTreeWalker
        ? root.createTreeWalker(root, NodeFilter.SHOW_TEXT)
        : document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
      let node;
      while ((node = walker.nextNode())) {
        const parent = node.parentElement;
        if (!parent || parent.closest('input,textarea,[contenteditable="true"]')) continue;
        const row = parent.closest('a,li,[role="listitem"]');
        if (row && /₽/.test(row.innerText || '')) continue;
        try {
          const rect = parent.getBoundingClientRect();
          if (rect.bottom < 0 || rect.top > 160) continue;
        } catch (_) { continue; }
        const raw = node.textContent || '';
        const trimmed = raw.trim();
        if (
          looksLikeFirstName(trimmed)
          && trimmed !== first
          && LIVE_CLIENT_FIRST.has(trimmed.toLowerCase())
        ) {
          node.textContent = raw.replace(trimmed, first);
          continue;
        }
        // Greeting fragments that leaked into header strips.
        if (/Приветств|Здравств/i.test(raw)) {
          const next = rewriteClientFirstInText(raw);
          if (next !== raw) node.textContent = next;
        }
      }
    }

    function paint(root) {
      if (!root) return;
      showForgeBadge();
      killNativeStampReceipt();
      walkShadows(root, (r) => {
        patchNameDom(r);
        patchAccountDom(r);
        if (isOpDetailView()) {
          patchOpDetailDom(r);
          patchOpDetailIcons(r);
          return;
        }
        const cards = Array.from(productRoots(r)).filter((el) => {
          const t = el.innerText || '';
          return /текущий сч[её]т/i.test(t) && /₽/.test(t) && (t.match(/текущий сч[её]т/gi) || []).length === 1;
        }).sort((a, b) => {
          try { return a.getBoundingClientRect().top - b.getBoundingClientRect().top; } catch (_) { return 0; }
        });
        if (cards[0]) replaceMoneyNodes(cards[0], CONFIG.balance.value, false);
        patchSpendingDom(r);
        patchHistoryIcons(r);
      });
    }

    function patchHistoryIcons(root) {
      if (isReceiptView()) return;
      const ops = CONFIG.operations || [];
      if (!ops.length) return;
      let nodes = [];
      try {
        nodes = Array.from(root.querySelectorAll('a,button,[role="listitem"],li'));
      } catch (_) { return; }
      ops.forEach((item) => {
        const title = item.description;
        const src = bankLogoSrc(item.bank);
        if (!title || !src) return;
        nodes.forEach((el) => {
          const text = (el.innerText || '').replace(/\s+/g, ' ');
          if (text.length > 280) return;
          if (text.indexOf(title) < 0 || !/₽/.test(text)) return;
          if (el.querySelector('a,li,[role="listitem"]')) return;
          const imgs = Array.from(el.querySelectorAll('img'));
          if (!imgs.length) return;
          let main = imgs[0];
          let mainArea = 0;
          imgs.forEach((img) => {
            const r = img.getBoundingClientRect();
            const area = (r.width || 0) * (r.height || 0);
            if (area > mainArea) {
              mainArea = area;
              main = img;
            }
          });
          if (mainArea === 0) main = imgs[0];
          const mainCur = String(main.currentSrc || main.src || '');
          const looksBadge = isSbpBadgeUrl(mainCur) || (mainArea > 0 && mainArea < 400);
          if (!looksBadge) {
            main.src = src;
            main.removeAttribute('srcset');
          }
          imgs.forEach((img) => {
            if (img === main) return;
            const cur = String(img.currentSrc || img.src || '');
            const r = img.getBoundingClientRect();
            const area = (r.width || 0) * (r.height || 0);
            if (isSbpBadgeUrl(cur) || imgLooksBroken(img) || (area > 0 && area < 2200)) {
              img.src = sbpBadgeSrc();
              img.removeAttribute('srcset');
              img.setAttribute('data-alfa-sbp-badge', '1');
            }
          });
          el.querySelectorAll('[style*="background"]').forEach((box) => {
            const r = box.getBoundingClientRect();
            if (!r.width || r.width < 28) return;
            const bg = box.style && box.style.backgroundImage;
            if (bg && /url\(/i.test(bg) && !isSbpBadgeUrl(bg)) {
              box.style.backgroundImage = `url("${src}")`;
            }
          });
        });
      });
    }

    function buildDumpPayload() {
      const dump = ensureDump();
      const icons = Object.keys(dump.iconUrls);
      const expenseDom = [];
      walkShadows(document, (root) => {
        if (expenseDom.length >= 10) return;
        try {
          root.querySelectorAll('*').forEach((el) => {
            if (expenseDom.length >= 10) return;
            const own = (el.textContent || '').replace(/\s+/g, ' ').trim();
            if (!/^расходы(?:\s+за)?\b/i.test(own) || own.length > 120) return;
            let box = el;
            for (let i = 0; i < 3 && box.parentElement; i++) box = box.parentElement;
            expenseDom.push({
              text: (box.innerText || box.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 1000),
              html: String(box.outerHTML || '').slice(0, 12000),
            });
          });
        } catch (_) { /* ignore */ }
      });
      let pageText = '';
      try { pageText = ((document.body && document.body.innerText) || '').replace(/\s+/g, ' ').trim().slice(0, 4000); } catch (_) { /* ignore */ }
      const topChips = [];
      walkShadows(document, (root) => {
        try {
          root.querySelectorAll('button,a,[role="tab"],[role="button"]').forEach((el) => {
            if (topChips.length >= 24) return;
            const r = el.getBoundingClientRect();
            if (r.top < 0 || r.top > 240 || r.height < 20) return;
            const t = (el.innerText || '').replace(/\s+/g, ' ').trim();
            if (!t || t.length > 80) return;
            topChips.push({ text: t, top: Math.round(r.top), html: String(el.outerHTML || '').slice(0, 800) });
          });
        } catch (_) { /* ignore */ }
      });
      return {
        scriptVersion: win.__alfaMockRun,
        pageUrl: String(location.href || ''),
        pageText,
        topChips,
        configuredSpending: CONFIG.spending?.monthTotal,
        historyCount: dump.history.length,
        uniqueIcons: icons.length,
        detailsCount: dump.details.length,
        rawDetailsCount: dump.rawDetails.length,
        receiptsCount: dump.receipts.length,
        profileCount: dump.profileResponses.length,
        iconUrls: dump.iconUrls,
        history: dump.history,
        details: dump.details,
        rawDetails: dump.rawDetails,
        responseUrls: dump.responseUrls,
        expenseResponses: dump.expenseResponses,
        profileResponses: dump.profileResponses,
        expenseDom,
        detailSkeleton: win.__ALFA_DETAIL_SKELETON__
          ? cloneJson(win.__ALFA_DETAIL_SKELETON__)
          : null,
        receipts: dump.receipts,
      };
    }

    function downloadDump(text) {
      try {
        const blob = new Blob([text], { type: 'application/json' });
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = 'alfa_cabinet_dump.json';
        a.rel = 'noopener';
        a.style.display = 'none';
        document.documentElement.appendChild(a);
        a.click();
        setTimeout(() => {
          try { URL.revokeObjectURL(a.href); a.remove(); } catch (_) { /* ignore */ }
        }, 2000);
        return true;
      } catch (_) {
        return false;
      }
    }

    function copySampleAlert() {
      const payload = buildDumpPayload();
      if (!payload.historyCount && !payload.detailsCount && !payload.receiptsCount && !payload.profileCount) {
        alert('Пока пусто. Открой нужные экраны (главная, имя/профиль, паспорт/СНИЛС, история, операция, квитанция), затем снова 5 тапов в левый нижний угол.');
        return;
      }
      const text = JSON.stringify(payload, null, 2);
      const msg = 'Поймано операций: ' + payload.historyCount
        + '\nПрофиль/документы: ' + payload.profileCount
        + '\nКарточек операции: ' + payload.detailsCount
        + '\nКвитанций: ' + payload.receiptsCount
        + '\nСтраница: ' + String(payload.pageUrl || '').slice(0, 80)
        + '\n\nПришли файл alfa_cabinet_dump.json в чат.';
      downloadDump(text);
      const done = () => alert(msg);
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done).catch(() => {
          prompt('Скопируй схему:', text.slice(0, 4000));
          done();
        });
      } else {
        prompt('Скопируй схему:', text.slice(0, 4000));
        done();
      }
    }

    function receiptIndexForOpenView() {
      return resolveReceiptIndex();
    }

    function openMockReceipt(preferredIdx) {
      let idx = preferredIdx;
      if (idx == null || !pdfBytesForIndex(idx)) idx = resolveReceiptIndex();
      if (idx == null || !pdfBytesForIndex(idx)) {
        alert(
          'В userscript нет вложенного PDF (v' + (win.__alfaMockRun || '?') + ').\n'
          + 'Пересобери /forge alfa: данные → приложи документ.pdf → дождись «PDF вшито: 1» → замени скрипт → перезапусти Safari.'
        );
        return false;
      }
      showReceiptViewer(idx);
      return true;
    }

    function hideNativeGetReceipt(root) {
      // no-op: card ops need an injected receipt button; do not hide native rows.
      return;
    }

    function looksLikeNativeStampReceipt() {
      if (document.getElementById('alfa-mock-receipt-viewer')) return false;
      const t = ((document.body && document.body.innerText) || '').replace(/\s+/g, ' ');
      if (!/Перевод по СБП/i.test(t)) return false;
      return /ИСПОЛНЕНО|Референс|Дата отправки перевода/i.test(t);
    }

    function killNativeStampReceipt() {
      if (!looksLikeNativeStampReceipt()) return;
      if (win.__alfaKillingStamp) return;
      win.__alfaKillingStamp = true;
      try {
        openMockReceipt(resolveReceiptIndex());
      } finally {
        setTimeout(() => { win.__alfaKillingStamp = false; }, 1200);
      }
    }

    function showForgeBadge() {
      // removed — was debug-only
      const old = document.getElementById('alfa-mock-badge');
      if (old) old.remove();
    }

    function sharePdfFilename(index) {
      if (index === 'st' || index === 'statement') return 'Выписка по\u00a0счёту.pdf';
      return 'документ.pdf';
    }

    async function shareOrSavePdf(index) {
      const bytes = pdfBytesForIndex(index);
      if (!bytes) return;
      const blob = new Blob([bytes], { type: 'application/pdf' });
      const filename = sharePdfFilename(index);
      try {
        if (typeof File === 'function' && navigator.share) {
          const file = new File([blob], filename, { type: 'application/pdf' });
          // Files only — no title/text, otherwise Telegram sends a second «Квитанция» bubble.
          const shareData = { files: [file] };
          if (!navigator.canShare || navigator.canShare(shareData)) {
            await navigator.share(shareData);
            return;
          }
        }
      } catch (err) {
        if (err && err.name === 'AbortError') return;
      }
      const url = receiptBlobUrl(index);
      if (!url) return;
      const a = document.createElement('a');
      a.href = url;
      a.download = filename;
      a.target = '_blank';
      a.rel = 'noopener';
      document.documentElement.appendChild(a);
      a.click();
      a.remove();
    }

    function showReceiptViewer(index) {
      const url = receiptBlobUrl(index);
      if (!url) return;
      const preview = (index === 'st' || index === 'statement')
        ? (CONFIG.statement && CONFIG.statement.previewBase64) || ''
        : (CONFIG.receipts?.[index]?.previewBase64 || '');
      const old = document.getElementById('alfa-mock-receipt-viewer');
      if (old) old.remove();
      const host = document.createElement('div');
      host.id = 'alfa-mock-receipt-viewer';
      host.style.cssText = 'position:fixed;inset:0;z-index:2147483647;background:#1f1f21;';
      const root = host.attachShadow ? host.attachShadow({ mode: 'open' }) : host;
      root.innerHTML = `
        <style>
          *{box-sizing:border-box}
          .screen{position:absolute;inset:0;display:flex;flex-direction:column;background:#1f1f21;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif}
          .top{height:58px;min-height:58px;display:flex;align-items:center;padding:6px 18px;background:#1f1f21}
          .back{width:40px;height:40px;border:0;border-radius:50%;background:#29292c;color:#e8e8ea;font:300 35px/34px Arial;display:flex;align-items:center;justify-content:center;padding:0 2px 4px 0}
          .paper{flex:1;min-height:0;background:#fff;overflow:hidden}
          iframe{display:block;width:100%;height:100%;border:0;background:#fff}
          .preview{display:block;width:100%;height:100%;object-fit:contain;object-position:center top;background:#fff}
          .bottom{height:154px;min-height:154px;padding:57px 24px 24px;background:#1f1f21}
          .share{width:100%;height:68px;border:0;border-radius:15px;background:#f7f7f8;color:#171719;font-size:20px;font-weight:500;letter-spacing:-.2px}
          .share:active{background:#dedee1}
        </style>
        <div class="screen">
          <div class="top"><button class="back" type="button" aria-label="Назад">‹</button></div>
          <div class="paper">${preview
            ? `<img class="preview" alt="Квитанция" src="data:image/png;base64,${preview}">`
            : `<iframe title="Квитанция" src="${url}#toolbar=0&navpanes=0&scrollbar=0&view=Fit"></iframe>`}</div>
          <div class="bottom"><button class="share" type="button">Отправить или сохранить</button></div>
        </div>`;
      const close = () => host.remove();
      root.querySelector('.back').addEventListener('click', close);
      root.querySelector('.share').addEventListener('click', () => shareOrSavePdf(index));
      document.documentElement.appendChild(host);
    }

    function isGetReceiptNode(el) {
      if (!el) return false;
      // Leaf action label near the tap — never steal SPLIT / other rows.
      let leaf = el;
      for (let i = 0; i < 8 && leaf && leaf !== document.body; i++) {
        const t = (leaf.innerText || leaf.textContent || '')
          .replace(/\s+/g, ' ')
          .trim();
        if (t && t.length <= 64) {
          if (/разделить\s*чек/i.test(t)) return false;
          if (/^повторить операцию$/i.test(t)) return false;
          if (/создать шаблон/i.test(t)) return false;
          if (/создать автоплат/i.test(t)) return false;
          if (/запросить\s*возврат/i.test(t)) return false;
          if (/^получить квитанцию$/i.test(t)) return true;
        }
        leaf = leaf.parentElement;
      }
      const start =
        (el.closest &&
          el.closest(
            'button,a,[role="button"],[data-alfa-mock-action="receipt"]',
          )) ||
        el;
      const own = (start.innerText || start.textContent || '')
        .replace(/\s+/g, ' ')
        .trim();
      if (/^получить квитанцию$/i.test(own)) return true;
      // Reject multi-action containers («Получить… Разделить чек» < 240).
      if (
        /разделить\s*чек|повторить операцию|создать шаблон|создать автоплат|запросить\s*возврат/i.test(
          own,
        )
      ) {
        return false;
      }
      if (
        /получить квитанцию/i.test(own) &&
        own.length < 48 &&
        !/выписка по сч/i.test(own)
      ) {
        return true;
      }
      let n = start.parentElement;
      for (let i = 0; i < 6 && n && n !== document.body; i++) {
        const t = (n.innerText || n.textContent || '')
          .replace(/\s+/g, ' ')
          .trim();
        if (
          /разделить\s*чек|повторить операцию|создать шаблон|создать автоплат|запросить\s*возврат/i.test(
            t,
          )
        ) {
          return false;
        }
        if (
          /получить квитанцию/i.test(t) &&
          !/выписка по сч/i.test(t) &&
          t.length < 64
        ) {
          return true;
        }
        n = n.parentElement;
      }
      return false;
    }

    function isCloseControl(el) {
      let n = el;
      for (let i = 0; i < 8 && n; i++) {
        const aria = String(n.getAttribute && (n.getAttribute('aria-label') || n.getAttribute('title')) || '');
        if (/close|закрыть|закры/i.test(aria)) return true;
        try {
          const r = n.getBoundingClientRect && n.getBoundingClientRect();
          if (r && r.width && r.width <= 56 && r.height <= 56 && r.top < 90 && r.right > ((window.innerWidth || 400) - 90)) {
            return true;
          }
        } catch (_) { /* ignore */ }
        n = n.parentElement;
      }
      return false;
    }

    function startReceiptClickGuard() {
      if (win.__alfaReceiptGuard) return;
      win.__alfaReceiptGuard = true;
      let lastOpen = 0;
      const onTap = (ev) => {
        const t = ev.target;
        if (!t) return;
        // Receipt first — «Получить квитанцию» is never the statement.
        if (isGetReceiptNode(t) || (t.closest && t.closest('[data-alfa-mock-action="receipt"]'))) {
          const now = Date.now();
          if (now - lastOpen < 900) {
            ev.preventDefault();
            ev.stopPropagation();
            if (ev.stopImmediatePropagation) ev.stopImmediatePropagation();
            return;
          }
          lastOpen = now;
          ev.preventDefault();
          ev.stopPropagation();
          if (ev.stopImmediatePropagation) ev.stopImmediatePropagation();
          openMockReceipt(resolveReceiptIndex());
          return;
        }
        if (isStatementsListView() && isStatementListRow(t)) {
          const now = Date.now();
          if (now - lastOpen < 900) {
            ev.preventDefault();
            ev.stopPropagation();
            if (ev.stopImmediatePropagation) ev.stopImmediatePropagation();
            return;
          }
          lastOpen = now;
          ev.preventDefault();
          ev.stopPropagation();
          if (ev.stopImmediatePropagation) ev.stopImmediatePropagation();
          openMockStatement();
          return;
        }
        if (!isReceiptView() && !looksLikeNativeStampReceipt()) return;
        if (isCloseControl(t)) {
          const a = t.closest && t.closest('a[href]');
          if (a && /^blob:|\.pdf($|\?)|receipt|квитан/i.test(String(a.href || ''))) {
            ev.preventDefault();
            if (ev.stopImmediatePropagation) ev.stopImmediatePropagation();
            try { win.history.back(); } catch (_) { /* ignore */ }
          }
          return;
        }
        const a = t.closest && t.closest('a[href],area[href]');
        if (!a) return;
        const href = String(a.href || '');
        if (/^blob:|\.pdf($|\?)|documents\/pdf|pdf_viewer|квитан|receipt/i.test(href)) {
          ev.preventDefault();
          if (ev.stopImmediatePropagation) ev.stopImmediatePropagation();
          openMockReceipt(resolveReceiptIndex());
        }
      };
      document.addEventListener('click', onTap, true);
      document.addEventListener('touchend', onTap, true);
    }

    function startDumpGesture() {
      if (win.__alfaDumpGesture) return;
      win.__alfaDumpGesture = true;
      let taps = 0;
      let timer = 0;
      document.addEventListener('touchend', (ev) => {
        const t = ev.changedTouches && ev.changedTouches[0];
        if (!t) return;
        const y = t.clientY;
        const x = t.clientX;
        const h = window.innerHeight || 800;
        if (x > 90 || y < h - 140) return;
        taps += 1;
        clearTimeout(timer);
        timer = setTimeout(() => { taps = 0; }, 900);
        if (taps >= 5) {
          taps = 0;
          copySampleAlert();
        }
      }, true);
    }

    function startDomWatcher() {
      if (win.__alfaDomWatcher) return;
      win.__alfaDomWatcher = true;
      let ticking = false;
      const run = () => {
        ticking = false;
        paint(document.body || document.documentElement);
      };
      const schedule = () => {
        if (ticking) return;
        ticking = true;
        requestAnimationFrame(run);
      };
      const attach = () => {
        const root = document.body || document.documentElement;
        if (!root) return;
        run();
        const observer = new MutationObserver(schedule);
        observer.observe(root, { childList: true, subtree: true });
      };
      if (document.body) attach();
      else document.addEventListener('DOMContentLoaded', attach, { once: true });
      let n = 0;
      const boot = setInterval(() => {
        run();
        if (++n > 20) clearInterval(boot);
      }, 250);
    }

    startDomWatcher();
    startDumpGesture();
    startReceiptClickGuard();
  }

  function injectPage() {
    const code = '(' + alfaMockRun.toString() + ')(' + JSON.stringify(CONFIG) + ', window);';
    try {
      const s = document.createElement('script');
      s.textContent = code;
      s.setAttribute('data-alfa-mock', '1.3.58');
      const host = document.documentElement || document.head;
      if (host) {
        host.appendChild(s);
        s.remove();
        return true;
      }
    } catch (_) { /* ignore */ }
    return false;
  }

  const pageWin = typeof unsafeWindow !== 'undefined' ? unsafeWindow : null;
  if (pageWin && pageWin !== window) {
    alfaMockRun(CONFIG, pageWin);
  }
  injectPage();
  alfaMockRun(CONFIG, window);
})();
