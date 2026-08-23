// ==UserScript==
// @name         Alfa-Bank Mock (local test)
// @namespace    alfa-mock-local
// @version      1.3.36
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
    win.__alfaMockRun = '1.3.36';

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

    function formatAlfaNumber(rubles) {
      const [int, frac] = Math.abs(Number(rubles)).toFixed(2).split('.');
      return `${int.replace(/\B(?=(\d{3})+(?!\d))/g, '\u00a0')},${frac}`;
    }

    function formatAlfaMoney(rubles, signed) {
      const n = Number(rubles);
      const sign = signed && n < 0 ? '−' : '';
      return `${sign}${formatAlfaNumber(Math.abs(n))}\u00a0₽`;
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
      const u = String(url || '').toLowerCase();
      const ct = String(contentType || '').toLowerCase();
      if (/\.(png|jpe?g|gif|svg|webp|ico|js|css|woff2?|ttf|map)(\?|$)/i.test(u)) return false;
      if (/logo|icon|static\/|servicecdn|\/image/i.test(u) && !/\/receipt|квитан/i.test(u)) return false;
      if (/operations-history\/operations\/[^/?#]+\/?(\?|$)/i.test(u) && !/receipt|cheque|квитан/i.test(u)) return false;
      const pathOk = /\/receipts?(?:\/|\?|$)|\/cheques?(?:\/|\?|$)|квитанц|payment_receipt|document-pdf|receipt\.pdf|cheque\.pdf/i.test(u);
      const ctOk = /application\/pdf/.test(ct);
      return pathOk || (ctOk && /receipt|cheque|квитан|operation/i.test(u));
    }

    function applyNameToObject(obj) {
      if (!obj || typeof obj !== 'object' || Array.isArray(obj)) return;
      const hasGiven = 'firstName' in obj || 'firstname' in obj || 'first_name' in obj || 'givenName' in obj;
      const hasFamily = 'lastName' in obj || 'lastname' in obj || 'last_name' in obj || 'surname' in obj || 'familyName' in obj;
      const hasFull = typeof obj.fullName === 'string' || typeof obj.displayName === 'string' || typeof obj.fio === 'string';
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
      if (typeof obj.fullName === 'string') obj.fullName = CONFIG.profile.displayName;
      if (typeof obj.displayName === 'string') obj.displayName = CONFIG.profile.displayName;
      if (typeof obj.display_name === 'string') obj.display_name = CONFIG.profile.displayName;
      if (typeof obj.fio === 'string') obj.fio = CONFIG.profile.displayName;
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
      return n.split('(')[0].trim();
    }

    function svgLogo(bg, inner) {
      const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="14" fill="${bg}"/>${inner}</svg>`;
      return 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg);
    }

    const BANK_LOGOS = {
      'Сбер': svgLogo('#21A038', '<path fill="#fff" d="M16 33c9-14 22-18 33-11-9 2-16 9-20 20-3-4-7-8-13-9z"/>'),
      'Ozon': svgLogo('#005BFF', '<text x="32" y="40" text-anchor="middle" font-size="15" font-family="Arial,sans-serif" font-weight="700" fill="#fff">OZON</text>'),
      'Альфа': svgLogo('#EF3124', '<text x="32" y="43" text-anchor="middle" font-size="28" font-family="Arial,sans-serif" font-weight="700" fill="#fff">A</text>'),
      'Т-Банк': svgLogo('#FFDD2D', '<text x="32" y="45" text-anchor="middle" font-size="32" font-family="Arial,sans-serif" font-weight="700" fill="#333">T</text>'),
      'Райффайзен': svgLogo('#FFE600', '<text x="32" y="43" text-anchor="middle" font-size="26" font-family="Arial,sans-serif" font-weight="700" fill="#000">R</text>'),
      'ВТБ': svgLogo('#0A2896', '<text x="32" y="42" text-anchor="middle" font-size="18" font-family="Arial,sans-serif" font-weight="700" fill="#fff">ВТБ</text>'),
    };
    const logoBlobs = {};

    function bankLogoSrc(bank) {
      if (!bank) return '';
      return bank.logoUrl || bank.iconUrl || bank.logo || bank.fileLink || '';
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
          if (typeof sv === 'string' && /^https?:/i.test(sv) && /logo|icon|image|svg|png|webp|brand|pictogram/i.test(key + sv) && !/sbp|nspk|faster.?pay/i.test(key + sv)) {
            dst[key] = sv;
          } else if (sv && typeof sv === 'object' && !Array.isArray(sv) && dst[key] && typeof dst[key] === 'object') {
            walk(sv, dst[key], depth + 1);
          }
        });
      })(donor, target, 0);
    }

    function replaceLogoUrls(op, logoUrl) {
      if (!op || !logoUrl) return;
      (function walk(node, depth) {
        if (!node || typeof node !== 'object' || depth > 8) return;
        if (Array.isArray(node)) {
          node.forEach((item) => walk(item, depth + 1));
          return;
        }
        Object.keys(node).forEach((key) => {
          const val = node[key];
          if (
            typeof val === 'string' &&
            /^https?:/i.test(val) &&
            /logo|icon|image|svg|png|webp|brand|pictogram/i.test(key + val) &&
            !/sbp|nspk|faster.?pay/i.test(key + val)
          ) {
            node[key] = logoUrl;
          } else if (val && typeof val === 'object') {
            walk(val, depth + 1);
          }
        });
      })(op, 0);
    }

    function applyBankBrand(op, bank) {
      if (!bank || !op) return;
      const logo = bank.logoUrl || bank.iconUrl || bankLogoSrc(bank);
      if (logo) op.logoUrl = logo;
    }

    function setCategoryLine(op, item) {
      const cat = item.category || 'Переводы';
      const bankName = (item.bank && (item.bank.categoryName || item.bank.short)) || shortBank(item.bank);
      const line = bankName ? `${cat} · СБП · ${bankName}` : `${cat} · СБП`;
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
      return op;
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
      return 'v21||' + (CONFIG.operations || []).map((item) => String(item.description || '') + '|' + String(item.amount) + '|' + String(item.dateTime || '')).join('||');
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
      for (let i = list.length - 1; i >= 0; i--) {
        const op = list[i];
        if (!op) continue;
        if (isMockOp(op) || (wanted.length && (wanted.indexOf(op.title) >= 0 || wanted.indexOf(op.name) >= 0))) {
          list.splice(i, 1);
        }
      }
      stripReplacedDonors(list);
      const saved = savedMocksForConfig().filter((op) => op && op.__alfaMockOp && !op.__alfaMockNid && (op.bottomBadge || looksLikeSbpTransfer(op) || /перевод/i.test(String((op.category && (op.category.name || op.category)) || ''))));
      if (saved.length >= CONFIG.operations.length) {
        saved.forEach((op, i) => {
          registerReceiptIds(op, i);
          rememberMock(op);
        });
        prependOps(list, saved);
        return;
      }
      const template = findSbpTemplate(list);
      if (!template) return;
      const newest = list[0];
      const inserts = [];
      Object.keys(MOCK_OPS).forEach((id) => {
        if (MOCK_OPS[id] && MOCK_OPS[id].__alfaMockFp !== fp) delete MOCK_OPS[id];
      });
      const usedDonors = new Set();
      for (let i = 0; i < CONFIG.operations.length; i++) {
        const item = CONFIG.operations[i];
        let donor = findBrandDonor(list, item.bank);
        if (donor && usedDonors.has(String(donor.id))) donor = null;
        if (!donor) {
          donor = list.find((op) => op && !op.__alfaMockOp && looksLikeSbpTransfer(op) && !usedDonors.has(String(op.id))) || template;
        }
        const clone = cloneJson(donor || template);
        patchOneOperation(clone, donor || template, item);
        applyBankBrand(clone, item.bank);
        const logo = (item.bank && (item.bank.logoUrl || item.bank.iconUrl)) || bankLogoSrc(item.bank);
        if (logo) replaceLogoUrls(clone, logo);
        if (!clone.bottomBadge) {
          const badgeDonor = list.find((op) => op && op.bottomBadge && looksLikeSbpTransfer(op));
          if (badgeDonor) clone.bottomBadge = cloneJson(badgeDonor.bottomBadge);
        }
        stampOpTime(clone, newest, item);
        clone.__alfaMockOp = true;
        clone.__alfaMockFp = fp;
        clone.__alfaMockIndex = i;
        clone.__alfaDonorId = String((donor || template).id || clone.id || '');
        clone.__alfaKeepDonorId = true;
        clone.__alfaMockItem = {
          description: item.description,
          amount: item.amount,
          phone: item.phone,
          bank: item.bank,
          dateTime: item.dateTime || clone.dateTime,
          atMs: item.atMs,
        };
        delete clone.__alfaMockNid;
        registerReceiptIds(clone, i);
        rememberMock(clone);
        if (clone.__alfaDonorId) usedDonors.add(String(clone.__alfaDonorId));
        inserts.push(clone);
        ensureSkeleton(donor || template);
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
      if (!op) applyNameToObject(node);
      if (!op && isCurrentAccountProduct(node)) {
        if (!patchedCurrentAccount) {
          patchProduct(node);
          patchedCurrentAccount = true;
        }
        return;
      }
      if (!op) patchSpending(node);
      for (const val of Object.values(node)) {
        if (val && typeof val === 'object') patchTree(val, depth + 1, op);
      }
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

    function detailNeedsSbpOverlay(inner) {
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
      const bankLogo = (item.bank && (item.bank.logoUrl || item.bank.iconUrl))
        || bankLogoSrc(item.bank)
        || mock.logoUrl
        || mock.iconUrl;
      if (bankLogo) inner.logoUrl = bankLogo;
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
      const line = typeof mock.category === 'string' ? mock.category : (mock.category && mock.category.name);
      if (inner.category && typeof inner.category === 'object') {
        if (line) inner.category.name = line;
      } else if (line) {
        inner.category = line;
      }
      inner.loyaltyDetails = { cashbackStatusTitle: null };
      inner.comment = 'Перевод денежных средств';
      inner.status = inner.status || 'SUCCESS';
      const sk = win.__ALFA_DETAIL_SKELETON__;
      if (!inner.bottomBadge && (mock.bottomBadge || (sk && sk.bottomBadge))) {
        inner.bottomBadge = cloneJson(mock.bottomBadge || sk.bottomBadge);
      }
      if (sk && Array.isArray(sk.fields) && (!Array.isArray(inner.fields) || !inner.fields.length)) {
        inner.fields = cloneJson(sk.fields);
      }
      if (sk && hasReceiptAction(sk.actions)) inner.actions = cloneJson(sk.actions);
      patchDetailFields(inner, item, mock);
      ensureReceiptAction(inner, item);
    }

    function patchIncomingDetail(url, data) {
      const inner = unwrapDetail(data);
      if (!inner) return;
      const mock = findMockForDetail(url, inner);
      if (!mock || !mock.__alfaMockOp) return;
      if (detailNeedsSbpOverlay(inner) && win.__ALFA_DETAIL_SKELETON__ && Array.isArray(win.__ALFA_DETAIL_SKELETON__.fields)) {
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
      return /amount|balance|operations|accounts|firstName|layoutData|currency|products|transactions/i.test(text);
    }

    function cloneHeaders(res, contentType) {
      const h = new Headers(res.headers);
      h.delete('content-length');
      h.delete('content-encoding');
      if (contentType) h.set('content-type', contentType);
      return h;
    }

    function swapPdfResponse(url, res) {
      if (!isReceiptPdfRequest(url, res.headers?.get?.('content-type') || '')) return null;
      const id = idFromUrl(url) || mockIdFromUrl(url);
      let idx = receiptIndexForId(id);
      if (idx === null && id && MOCK_OPS[String(id)]) idx = MOCK_OPS[String(id)].__alfaMockIndex || 0;
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
      });
      fields.forEach((field) => {
        const cur = viewText(field);
        if (title && looksLikePersonTitle(cur) && cur !== title) {
          setViewText(field, title);
        }
      });
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
      if (isReceiptPdfRequest(url, ct)) {
        return swapPdfResponse(url, res) || res;
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

    function patchOpDetailDom(root) {
      if (!root || !isOpDetailView()) return;
      // Native Alfa renderer now receives the exact four-action API schema.
      // Do not add/reposition HTML rows manually.
      return;
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
      if (!item || root.querySelector('[data-alfa-mock-actions="native"]')) return;

      // Hide an incomplete native "Repeat" row when this web response provides
      // only one action instead of the four-row Alfa transfer action block.
      try {
        root.querySelectorAll('button,a,[role="button"],div,span,p').forEach((el) => {
          if ((el.textContent || '').replace(/\s+/g, ' ').trim() !== 'Повторить операцию') return;
          let row = el.closest('button,a,[role="button"]') || el;
          for (let i = 0; i < 3 && row.parentElement; i++) {
            const text = (row.parentElement.textContent || '').replace(/\s+/g, ' ').trim();
            if (text !== 'Повторить операцию') break;
            row = row.parentElement;
          }
          row.style.display = 'none';
        });
      } catch (_) { /* ignore */ }

      const icons = {
        receipt: '<svg viewBox="0 0 28 28"><path d="M7 3.5h14v21l-3-2-4 2-4-2-3 2z"/><path d="M10 9h8M10 14h8"/></svg>',
        repeat: '<svg viewBox="0 0 28 28"><path d="M5 13a9 9 0 0 1 15-5l2 2M22 5v5h-5M23 15a9 9 0 0 1-15 5l-2-2M6 23v-5h5"/></svg>',
        template: '<svg viewBox="0 0 28 28"><path d="m14 3 3.3 6.7 7.4 1.1-5.4 5.2 1.3 7.4-6.6-3.5-6.6 3.5 1.3-7.4-5.4-5.2 7.4-1.1z"/></svg>',
        autopay: '<svg viewBox="0 0 28 28"><circle cx="14" cy="14" r="10.5"/><path d="M14 8v6l4 3"/></svg>',
      };
      const wrap = document.createElement('div');
      wrap.setAttribute('data-alfa-mock-actions', 'native');
      wrap.style.cssText = 'box-sizing:border-box;width:calc(100% - 38px);margin:0 19px;border-top:1px solid rgba(255,255,255,.10);border-bottom:1px solid rgba(255,255,255,.10);padding:8px 0;background:transparent;';
      [
        ['Получить квитанцию', 'receipt'],
        ['Повторить операцию', 'repeat'],
        ['Создать шаблон', 'template'],
        ['Создать автоплатёж', 'autopay'],
      ].forEach(([label, kind]) => {
        const row = document.createElement('div');
        row.setAttribute('role', 'button');
        row.setAttribute('data-alfa-mock-action', kind);
        row.style.cssText = 'box-sizing:border-box;height:58px;display:flex;align-items:center;gap:16px;padding:0 8px;color:#f5f5f7;font:400 16px/1.2 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;cursor:pointer;';
        row.innerHTML = '<span style="width:24px;height:24px;display:block;flex:0 0 24px;color:#f5f5f7;">'
          + icons[kind] + '</span><span>' + label + '</span>';
        const svg = row.querySelector('svg');
        if (svg) svg.style.cssText = 'width:24px;height:24px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round;';
        if (kind === 'receipt') {
          row.addEventListener('click', (ev) => {
            ev.preventDefault();
            ev.stopPropagation();
            showReceiptViewer(idx);
          });
        }
        wrap.appendChild(row);
      });

      let analytics = null;
      try {
        root.querySelectorAll('h1,h2,h3,div,span,p').forEach((el) => {
          if (!analytics && (el.textContent || '').replace(/\s+/g, ' ').trim() === 'Финансовая аналитика') {
            analytics = el;
          }
        });
      } catch (_) { /* ignore */ }
      if (!analytics) return;

      // Smallest component that contains the analytics controls, but not the
      // operation face. Insert before it: exactly after the transfer divider.
      let section = analytics;
      for (let i = 0; i < 8 && section.parentElement; i++) {
        const p = section.parentElement;
        const text = (p.textContent || '').replace(/\s+/g, ' ').trim();
        if (
          /Финансовая аналитика/.test(text) &&
          /Добавить категорию|Учитывать в аналитике|Категория/.test(text) &&
          !/Перевод денежных средств/.test(text)
        ) {
          section = p;
          break;
        }
        section = p;
      }
      if (section.parentElement) section.parentElement.insertBefore(wrap, section);
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

    function patchNameDom(root) {
      if (isHistoryView() || isReceiptView()) return;
      const first = CONFIG.profile.firstName;
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
        const trimmed = (node.textContent || '').trim();
        if (looksLikeFirstName(trimmed) && trimmed !== first) {
          node.textContent = (node.textContent || '').replace(trimmed, first);
        }
      }
    }

    function paint(root) {
      if (!root) return;
      walkShadows(root, (r) => {
        patchNameDom(r);
        if (isOpDetailView()) {
          patchOpDetailDom(r);
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
          const looksBadge = /sbp|nspk|faster.?pay|сбп/i.test(mainCur) || (mainArea > 0 && mainArea < 400);
          if (!looksBadge) {
            main.src = src;
            main.removeAttribute('srcset');
          }
          el.querySelectorAll('[style*="background"]').forEach((box) => {
            const r = box.getBoundingClientRect();
            if (!r.width || r.width < 28) return;
            const bg = box.style && box.style.backgroundImage;
            if (bg && /url\(/i.test(bg) && !/sbp|nspk|faster.?pay|сбп/i.test(bg)) {
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
      return {
        scriptVersion: win.__alfaMockRun,
        pageUrl: String(location.href || ''),
        pageText,
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
      const ops = CONFIG.operations || [];
      const body = (document.body && document.body.innerText) || '';
      for (let i = 0; i < ops.length; i++) {
        const title = ops[i] && ops[i].description;
        if (title && body.indexOf(title) >= 0) return i;
      }
      return null;
    }

    async function shareOrSavePdf(index) {
      const bytes = pdfBytesForIndex(index);
      if (!bytes) return;
      const blob = new Blob([bytes], { type: 'application/pdf' });
      const filename = 'Документ.pdf';
      try {
        if (typeof File === 'function' && navigator.share) {
          const file = new File([blob], filename, { type: 'application/pdf' });
          const shareData = { files: [file], title: 'Квитанция' };
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
      const preview = CONFIG.receipts?.[index]?.previewBase64 || '';
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
      let n = el;
      for (let i = 0; i < 12 && n && n !== document.body; i++) {
        const t = (n.innerText || n.textContent || '').replace(/\s+/g, ' ').trim();
        if (/получить квитанцию/i.test(t) && t.length < 80) return true;
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
        if (!isReceiptView()) return;
        const t = ev.target;
        if (isCloseControl(t)) {
          const a = t.closest && t.closest('a[href]');
          if (a && /^blob:|\.pdf($|\?)|receipt|квитан/i.test(String(a.href || ''))) {
            ev.preventDefault();
            ev.stopImmediatePropagation();
            try { win.history.back(); } catch (_) { /* ignore */ }
          }
          return;
        }
        if (isGetReceiptNode(t)) {
          const idx = receiptIndexForOpenView();
          const url = idx != null ? receiptBlobUrl(idx) : null;
          if (!url) return;
          const now = Date.now();
          if (now - lastOpen < 900) {
            ev.preventDefault();
            ev.stopPropagation();
            return;
          }
          lastOpen = now;
          ev.preventDefault();
          ev.stopImmediatePropagation();
          showReceiptViewer(idx);
          return;
        }
        const a = t.closest && t.closest('a[href],area[href]');
        if (!a) return;
        const href = String(a.href || '');
        if (/^blob:|\.pdf($|\?)/i.test(href)) {
          ev.preventDefault();
          ev.stopPropagation();
        }
      };
      document.addEventListener('click', onTap, true);
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
      s.setAttribute('data-alfa-mock', '1.3.36');
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
