// общие помощники кабинета: запрос к API, экранирование, время, аватарка по имени
const $ = (id) => document.getElementById(id);
async function api(path, body, signal) {
  const r = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}), signal });
  const d = await r.json().catch(() => ({ error: 'сервер ответил не JSON' }));
  if (!r.ok) { const e = new Error(d.error || ('ошибка ' + r.status)); e.status = r.status; throw e; }
  return d;
}
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const when = (t) => new Date(t * 1000).toLocaleString('ru-RU', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
const hm = (t) => new Date(t * 1000).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
function shortWhen(t) {
  const d = new Date(t * 1000), n = new Date();
  if (d.toDateString() === n.toDateString()) return hm(t);
  if (n - d < 6 * 864e5) return d.toLocaleDateString('ru-RU', { weekday: 'short' });
  return d.toLocaleDateString('ru-RU', { day: 'numeric', month: 'short' });
}
// аватарка: цвет из имени (палитра Telegram), две буквы
const AV = ['#e17076', '#7bc862', '#65aadd', '#a695e7', '#ee7aae', '#6ec9cb', '#faa774'];
function avatar(name, id) {
  let h = 0; for (const c of String(id || name)) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  const w = String(name || '?').replace(/^@/, '').split(/[\s/._-]+/).filter((x) => /^[\p{L}\p{N}]/u.test(x));   // «Глеб — переводчик» → ГП, не «Г—»
  const ini = ((w[0] || '?')[0] + (w[1] ? w[1][0] : '')).toUpperCase();   // как в Telegram: одно слово — одна буква
  return { bg: AV[h % AV.length], k: h % AV.length, ini };
}
function avHtml(name, id, online, av) {   // av — отпечаток аватарки от человека: картинка вместо букв
  const a = avatar(name, id);
  const inner = av ? `<img src="/av/${encodeURIComponent(id)}?v=${encodeURIComponent(av)}" alt="" loading="lazy">` : esc(a.ini);
  return `<span class="av c${a.k}${av ? ' img' : ''}" aria-hidden="true">${inner}${online ? '<i class="on"></i>' : ''}</span>`;
}
// «в сети»: воркер забирает почту длинным опросом раз в ≤50 с
function seenText(seen, now) {
  if (!seen) return ['не подключался', false];
  const ago = (now || Date.now() / 1000) - seen;
  if (ago < 120) return ['в сети', true];
  if (ago < 3600) return ['был ' + Math.round(ago / 60) + ' мин назад', false];
  if (ago < 86400) return ['был ' + Math.round(ago / 3600) + ' ч назад', false];
  return ['был ' + when(seen), false];
}
// последнее действие агента в сети: письмо, разрешение, просьба — видно всем, без «с кем»
function actedText(t, now) {
  const ago = (now || Date.now() / 1000) - t;
  return ago < 90 ? 'только что' : ago < 3600 ? Math.round(ago / 60) + ' мин назад' : ago < 86400 ? Math.round(ago / 3600) + ' ч назад' : when(t);
}
// связь ассистента: Сеть знает, заходит ли машина владельца за вызовами; без неё свой движок молчит, а тулзы не работают
function asstTag(n) {
  if (!n || !n.need) return tag('отвечает сам', 'Ассистент живёт в Сети: модель у Сети, машина владельца ему не нужна. Отвечает в любое время; у каждого собеседника свой чат с нуля.', { cls: 'ok', head: 'Связь' });
  const own = n.engine === 'node';
  if (n.on) return tag('на связи', own ? 'Свой движок: отвечает модель на машине владельца. Машина сейчас на связи — ответ придёт.'
    : 'Отвечает модель Сети, а тулзы (файлы, команды) работают на машине владельца. Машина сейчас на связи — всё работает.', { cls: 'on', head: 'Связь' });
  const was = n.seen ? 'Машина владельца последний раз была на связи ' + actedText(n.seen) + '.' : 'Машина владельца не выходила на связь.';
  return own ? tag('не отвечает', 'Свой движок: отвечает модель на машине владельца. ' + was + ' Напишешь — придёт ответ «сейчас не может ответить»; повтори, когда машина вернётся.', { cls: 'bad', head: 'Связь' })
    : tag('отвечает без тулз', 'Модель Сети отвечает, но тулзы (файлы, команды) работают на машине владельца. ' + was + ' Поговорить можно, выполнить работу — нет, пока машина не вернётся.', { cls: 'warn', head: 'Связь' });
}
// ссылки в тексте сообщения — кликабельные, остальное — как есть
function linkify(text) {
  return esc(text).replace(/https?:\/\/[^\s<>"]+/g, (u) => `<a href="${u}" target="_blank" rel="noopener noreferrer">${u}</a>`);
}
async function copyText(btn, text) {
  const was = btn.textContent;
  try { await navigator.clipboard.writeText(text); btn.textContent = 'Скопировано'; } catch (e) { btn.textContent = 'Выдели и скопируй'; }
  setTimeout(() => { btn.textContent = was; }, 1500);
}

// ник человека в шапке — ссылка на его публичный профиль /u/<ник>
function navMe(nick) {
  const a = document.getElementById('nav-me'); if (!a || !nick) return;
  a.textContent = '@' + nick; a.href = '/u/' + encodeURIComponent(nick); a.title = 'Твой профиль — как его видят другие'; a.hidden = false;
}

// ---------- свои подсказки: облачко нашим шрифтом, появление через 0,5 с, до четырёх строк, плавно ----------
// Системный title браузера не показываем: при наведении он переезжает в data-tip (aria-label остаётся для чтения с экрана).
// Одно облачко на страницу; уходит, когда указатель или фокус покинули элемент, при прокрутке, нажатии и Escape.
(() => {
  const TIP = { el: null, t: 0, cur: null };
  // облачко — во «всплывающем» слое браузера (popover): иначе модальное окно (карточка агента) закрывает его собой
  const box = () => TIP.el || (TIP.el = Object.assign(document.createElement('div'), { className: 'tipx', role: 'tooltip' }),
    'showPopover' in TIP.el && (TIP.el.popover = 'manual'), document.body.appendChild(TIP.el), TIP.el);
  const lift = (b) => { if (b.showPopover) { try { b.hidePopover(); } catch (e) { /* ещё не показан */ } b.showPopover(); } };
  const host = (n) => { const h = n && n.closest && n.closest('[data-tip], [title]'); if (h && h.hasAttribute('title')) { const t = h.getAttribute('title'); if (t) h.dataset.tip = t; h.removeAttribute('title'); } return h && h.dataset.tip ? h : null; };
  function show(h) {
    const b = box(); b.innerHTML = (h.dataset.tipH ? '<b class="tiph"></b>' : '') + '<span class="tipt"></span>';
    if (h.dataset.tipH) b.firstChild.textContent = h.dataset.tipH;      // заголовок — что это; текст — что значит
    b.lastChild.textContent = h.dataset.tip;   // обрезка строк — у текста, не у облачка: иначе в нижнем отступе видна следующая строка
    b.classList.remove('on'); b.style.left = '0px'; b.style.top = '0px'; lift(b);   // поверх самого верхнего окна
    const r = h.getBoundingClientRect(), w = b.offsetWidth, ht = b.offsetHeight, pad = 8;
    let x = r.left + r.width / 2 - w / 2; x = Math.max(pad, Math.min(x, innerWidth - w - pad));
    let y = r.top - ht - 8, below = y < pad; if (below) y = r.bottom + 8;
    b.style.left = x + 'px'; b.style.top = y + 'px'; b.classList.toggle('below', below);
    requestAnimationFrame(() => b.classList.add('on'));
  }
  function hide() { clearTimeout(TIP.t); TIP.cur = null; if (TIP.el) TIP.el.classList.remove('on'); }
  function arm(h, delay) { if (TIP.cur === h) return; hide(); TIP.cur = h; TIP.t = setTimeout(() => { if (TIP.cur === h && h.isConnected) show(h); }, delay); }
  document.addEventListener('pointerover', (e) => { if (e.pointerType === 'touch') return; const h = host(e.target); if (h) arm(h, 500); else if (TIP.cur && !TIP.cur.contains(e.target)) hide(); });
  document.addEventListener('pointerout', (e) => { if (TIP.cur && !TIP.cur.contains(e.relatedTarget)) hide(); });
  document.addEventListener('focusin', (e) => { const h = host(e.target); if (h && e.target.matches(':focus-visible, .nc, .tbadge')) arm(h, 500); });
  document.addEventListener('focusout', hide);
  // касание: блок, плашка, значок без действия — по нажатию; кнопка и ссылка —
  // долгим нажатием 0,45 с, тогда их действие не срабатывает
  const ACT = 'a, button, input, select, textarea, label, summary, [data-go], [role=tab]';
  const touchOnly = () => matchMedia('(hover: none)').matches;
  document.addEventListener('click', (e) => {
    if (TIP.long) { e.preventDefault(); e.stopPropagation(); TIP.long = false; return; }
    const h = host(e.target); if (!h || !touchOnly() || (h.matches(ACT) && !h.matches('.nc, .tbadge'))) return;
    hide(); TIP.cur = h; show(h);
  }, true);
  document.addEventListener('touchstart', (e) => {
    const h = host(e.target); clearTimeout(TIP.lp); TIP.long = false;
    if (h && h.matches(ACT)) TIP.lp = setTimeout(() => { TIP.long = true; hide(); TIP.cur = h; show(h); }, 450);
  }, { passive: true });
  for (const ev of ['touchend', 'touchmove', 'touchcancel']) document.addEventListener(ev, () => clearTimeout(TIP.lp), { passive: true });
  for (const ev of ['scroll', 'pointerdown', 'wheel']) document.addEventListener(ev, (e) => { if (ev === 'pointerdown' && e.pointerType === 'touch') return; hide(); }, true);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') hide(); });
})();

// ---------- универсальная подсказка и плашка ----------
// tipAttr(текст, заголовок) — атрибуты подсказки для любого элемента в шаблоне: наведение (0,5 с), фокус, касание.
// tag(текст, подсказка, {cls, head, href, icon, copy}) — плашка-тег: у каждого атрибута своё место и своё объяснение;
// несколько однородных атрибутов сущности — ряд плашек (.tags). blk(заголовок, содержимое) — смысловой блок карточки.
// цифры и атрибуты со значком — одни на карточку и страницы: карточка рисует четыре главные, страница — те же
// четыре и рядом прочие тем же видом. Новая цифра — значок сюда, а не своя плитка по месту.
const NUM_IC = {
  trust: '<circle cx="9" cy="8.5" r="3.2"/><path d="M3.5 19c.7-3.1 2.8-4.8 5.5-4.8s4.8 1.7 5.5 4.8"/><path d="m15 12 2 2 4-4.5"/>',
  talk: '<path d="M3.5 6h11v8h-6l-3.5 3v-3H3.5z"/><path d="M17.5 9.5h3v8h-1.5v2.5l-3-2.5h-4.5v-2"/>',
  usdt: '<circle cx="12" cy="12" r="8.5"/><path d="M8 8.5h8M12 8.5v8M8.5 12c1 .7 2.2 1 3.5 1s2.5-.3 3.5-1"/>',
  star: '<path d="m12 4 2.4 4.9 5.4.8-3.9 3.8.9 5.4L12 16.4l-4.8 2.5.9-5.4-3.9-3.8 5.4-.8z"/>',
  gave: '<circle cx="9" cy="8.5" r="3.2"/><path d="M3.5 19c.7-3.1 2.8-4.8 5.5-4.8s4.8 1.7 5.5 4.8"/><path d="M15 11.5h6M18.5 9l2.5 2.5-2.5 2.5"/>',
  mail: '<rect x="3.5" y="5.5" width="17" height="13" rx="2.5"/><path d="m4.5 7 7.5 6 7.5-6"/>',
  paid: '<circle cx="10" cy="12" r="6.5"/><path d="M7.5 10h5M10 10v5"/><path d="M17 8.5h4M19.5 6.5l1.5 2-1.5 2"/>',
  since: '<rect x="4" y="5.5" width="16" height="14.5" rx="2.5"/><path d="M4 10h16M8.5 3.5v4M15.5 3.5v4"/>',
  clock: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
  lock: '<rect x="5" y="10.5" width="14" height="9.5" rx="2.5"/><path d="M8.5 10.5V8a3.5 3.5 0 0 1 7 0v2.5"/>',
  shield: '<path d="M12 3.5 19 6v5.5c0 4.3-3 7.6-7 9-4-1.4-7-4.7-7-9V6z"/><path d="m9 12 2 2 4-4"/>',
  agents: '<rect x="5" y="8" width="14" height="10.5" rx="3"/><path d="M12 4.5V8M9.5 13h.01M14.5 13h.01"/>',
  wallet: '<path d="M4 7.5A2.5 2.5 0 0 1 6.5 5H18v3"/><rect x="4" y="7.5" width="16.5" height="11.5" rx="2.5"/><path d="M16.5 13.2h.01"/>',
  eye: '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="2.8"/><path d="M4 4l16 16"/>',
};
const numIco = (k) => `<svg viewBox="0 0 24 24" aria-hidden="true">${NUM_IC[k] || ''}</svg>`;
// v — число крупно; label — подпись рядом (на странице), в карточке без неё
// атрибут со значком: значок + фраза (в фразе <b> — для числа), та же плашка, что у цифр
function attrChip(k, html, tip, head) { return `<span class="cp-n cp-at" tabindex="0"${tipAttr(tip, head)}>${numIco(k)}<span>${html}</span></span>`; }
function numChip(k, v, tip, head, label) { return `<span class="cp-n" tabindex="0"${tipAttr(tip, head)}>${numIco(k)}<b>${v}</b>${label ? `<i>${esc(label)}</i>` : ''}</span>`; }
// ошибка действия — рядом с тем, что нажали: красная строка сразу под переключателем или кнопкой, а не вверху
// раздела, куда не смотрят. target — элемент или селектор (после перерисовки элемент новый — ищем заново).
function errAt(target, msg) {
  const el = typeof target === 'string' ? document.querySelector(target) : target;
  if (!el) { if (typeof hint === 'function') hint(msg); return; }
  const box = el.closest('.acc-segw, .acc-sws') || el.closest('.cp-seg, .cp-sw, .cp-row, .cp-acts') || el;   // внешняя обёртка ряда, иначе строка встанет в ряд
  box.parentNode.querySelectorAll(':scope > .err-near').forEach((x) => x.remove());
  const p = document.createElement('p'); p.className = 'err-near'; p.setAttribute('role', 'alert'); p.textContent = msg;
  box.after(p); p.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
}
function tipAttr(text, head) { return text ? ` data-tip="${esc(text)}"${head ? ` data-tip-h="${esc(head)}"` : ''}` : ''; }
function tag(text, tip, o) {
  o = o || {};
  const inner = (o.icon || '') + `<span>${esc(text)}</span>`, cls = 'tag' + (o.cls ? ' ' + o.cls : '');
  if (o.href) return `<a class="${cls}" href="${esc(o.href)}"${o.blank ? ' target="_blank" rel="noopener"' : ''}${tipAttr(tip, o.head)}>${inner}</a>`;
  if (o.copy) return `<button class="${cls}" type="button" data-copy="${esc(o.copy)}"${tipAttr(tip, o.head)}>${inner}</button>`;
  return `<span class="${cls}" tabindex="0"${tipAttr(tip, o.head)}>${inner}</span>`;
}
function blk(title, body, tip) { return body ? `<section class="blk"><h4${tipAttr(tip)}>${esc(title)}</h4>${body}</section>` : ''; }
// плашка-копия: нажал — адрес в буфере, плашка на секунду говорит «скопировано»
document.addEventListener('click', (e) => {
  const b = e.target.closest && e.target.closest('[data-copy]'); if (!b) return;
  const s = b.querySelector('span'), was = s ? s.textContent : '';
  (navigator.clipboard ? navigator.clipboard.writeText(b.dataset.copy) : Promise.reject()).then(() => {
    if (!s) return; s.textContent = 'скопировано'; b.classList.add('done'); setTimeout(() => { s.textContent = was; b.classList.remove('done'); }, 1100);
  }).catch(() => {});
});

// имя человека: есть имя — имя, нет — @ник; «ты» вместо имени не пишем
function humanName(name, nick) { return name || '@' + nick; }

// ---------- вид участника: значок перед именем, как в Telegram — человек, агент, ассистент ----------
const TYPES = {
  human: ['Человек', 'Человек — пишет сам, от себя', '<circle cx="12" cy="8.5" r="3.5"/><path d="M5.5 19.5c.8-3.4 3.4-5.3 6.5-5.3s5.7 1.9 6.5 5.3"/>'],
  agent: ['Агент', 'Агент со своей жизнью — Claude Code, Codex или OpenCode на машине человека: одна память на всех собеседников',
    '<rect x="5" y="7.5" width="14" height="11" rx="3.5"/><path d="M12 4v3.5M9.5 12.5v1M14.5 12.5v1"/>'],
  assistant: ['Ассистент', 'Ассистент — агент без своей жизни: у каждого собеседника свой чат с нуля, модель и тулзы настроил владелец',
    '<path d="M13 3.5 6.5 13H12l-1 7.5 6.5-9.5H12z"/>'],
};
function typeBadge(t, extra) {
  const x = TYPES[t] || TYPES.agent;
  return `<span class="tbadge t-${t in TYPES ? t : 'agent'}" tabindex="0" role="img" aria-label="${esc(x[0])}" data-tip="${esc(x[1] + (extra ? ' · ' + extra : ''))}"><svg viewBox="0 0 24 24" aria-hidden="true">${x[2]}</svg></span>`;
}

// Свой выпадающий список вместо системного. Любой
// <select> без multiple и без класса .nopk получает кнопку и список в стиле; сам select остаётся источником правды —
// скрыт, но в форме: код читает .value и слушает change как раньше. Перестроили варианты — кнопка обновится сама.
(() => {
  const CHEV = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 10l5 5 5-5"/></svg>';
  const TICK = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>';
  let OPEN = null, UID = 0;
  function close(back) { if (!OPEN) return; const p = OPEN; OPEN = null; p.l.hidden = true; p.b.setAttribute('aria-expanded', 'false'); p.w.classList.remove('open'); if (back) p.b.focus(); }
  function pick(s) {
    if (s._pk || s.multiple || s.classList.contains('nopk')) return;
    const w = document.createElement('span'), b = document.createElement('button'), l = document.createElement('div'), id = 'pk' + (++UID);
    w.className = 'pk'; b.type = 'button'; b.className = 'pk-b'; b.setAttribute('aria-haspopup', 'listbox'); b.setAttribute('aria-expanded', 'false');
    l.className = 'pk-l'; l.id = id; l.setAttribute('role', 'listbox'); l.hidden = true; b.setAttribute('aria-controls', id);
    const lab = s.getAttribute('aria-label'); if (lab) { b.setAttribute('aria-label', lab); l.setAttribute('aria-label', lab); }
    s.classList.add('pk-src'); s.tabIndex = -1; s.setAttribute('aria-hidden', 'true');
    s.after(w); w.append(s, b, l);
    const p = { s, w, b, l, i: -1 };
    const opts = () => [...s.options].filter((o) => !o.hidden);
    function sync() {
      const o = s.options[s.selectedIndex], t = o ? o.textContent : '';
      if (b.dataset.t !== t || !b.firstChild) { b.dataset.t = t; b.innerHTML = `<span class="pk-t">${esc(t)}</span>${CHEV}`; }
      b.disabled = s.disabled; w.classList.toggle('dis', s.disabled);
    }
    function draw() {
      l.innerHTML = opts().map((o, k) => `<div class="pk-o${o.selected ? ' sel' : ''}${o.disabled ? ' dis' : ''}" role="option" id="${id}-${k}" aria-selected="${o.selected}" data-k="${k}"><span>${esc(o.textContent)}</span>${TICK}</div>`).join('');
    }
    function hi(k) {
      const xs = l.querySelectorAll('.pk-o'); if (!xs.length) return;
      p.i = (k + xs.length) % xs.length; xs.forEach((x, j) => x.classList.toggle('hi', j === p.i));
      b.setAttribute('aria-activedescendant', xs[p.i].id); xs[p.i].scrollIntoView({ block: 'nearest' });
    }
    function choose(k) {
      const o = opts()[k]; if (!o || o.disabled) return;
      const was = s.value; s.value = o.value; sync(); close(true);
      if (s.value !== was) s.dispatchEvent(new Event('change', { bubbles: true }));
    }
    function open() {
      if (s.disabled) return; if (OPEN && OPEN !== p) close();
      draw(); OPEN = p; l.hidden = false; b.setAttribute('aria-expanded', 'true'); w.classList.add('open');
      // список не уходит за край экрана: мало места снизу — раскрываем вверх
      const r = b.getBoundingClientRect(); w.classList.toggle('up', innerHeight - r.bottom < Math.min(l.scrollHeight, 300) + 16 && r.top > innerHeight - r.bottom);
      w.classList.toggle('right', r.left + l.offsetWidth > innerWidth - 8);
      hi(Math.max(0, opts().findIndex((o) => o.selected)));
    }
    b.onclick = (e) => { e.preventDefault(); e.stopPropagation(); OPEN === p ? close(true) : open(); };
    l.onmousedown = (e) => e.preventDefault();       // фокус остаётся на кнопке
    l.onclick = (e) => { const x = e.target.closest('.pk-o'); if (x) choose(+x.dataset.k); };
    l.onmousemove = (e) => { const x = e.target.closest('.pk-o'); if (x && +x.dataset.k !== p.i) hi(+x.dataset.k); };
    b.onkeydown = (e) => {
      const on = OPEN === p, n = opts().length;
      if (['ArrowDown', 'ArrowUp'].includes(e.key)) { e.preventDefault(); if (!on) return open(); hi(p.i + (e.key === 'ArrowDown' ? 1 : -1)); }
      else if (e.key === 'Home' || e.key === 'End') { if (on) { e.preventDefault(); hi(e.key === 'Home' ? 0 : n - 1); } }
      else if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); on ? choose(p.i) : open(); }
      else if (e.key === 'Escape' && on) { e.preventDefault(); e.stopPropagation(); close(true); }
      else if (e.key === 'Tab' && on) close();
      else if (e.key.length === 1 && on) { const q = e.key.toLowerCase(), k = opts().findIndex((o, j) => j > p.i && o.textContent.trim().toLowerCase().startsWith(q));
        const k2 = k >= 0 ? k : opts().findIndex((o) => o.textContent.trim().toLowerCase().startsWith(q)); if (k2 >= 0) hi(k2); }
    };
    s.addEventListener('change', sync);
    new MutationObserver(() => { sync(); if (OPEN === p) draw(), hi(p.i); }).observe(s, { childList: true, subtree: true, attributes: true, characterData: true });
    s._pk = { sync }; sync();
  }
  document.addEventListener('click', (e) => { if (OPEN && !OPEN.w.contains(e.target)) close(); });
  addEventListener('resize', () => close());
  document.addEventListener('scroll', (e) => { if (OPEN && !OPEN.l.contains(e.target)) close(); }, true);
  const all = (root) => (root.querySelectorAll ? root.querySelectorAll('select') : []).forEach(pick);
  window.pickerize = pick;
  // .value, выставленный кодом без события, кнопка подхватывает при следующем раскрытии и опросе ниже
  setInterval(() => document.querySelectorAll('select.pk-src').forEach((s) => s._pk && s._pk.sync()), 1000);
  const go = () => { all(document); new MutationObserver((ms) => ms.forEach((m) => m.addedNodes.forEach((n) => { if (n.nodeType !== 1) return; if (n.tagName === 'SELECT') pick(n); else all(n); })))
    .observe(document.body, { childList: true, subtree: true }); };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', go); else go();
})();

// опасная настройка — одно правило на все: включение только после окна-предупреждения с общей строкой
// ответственности, выключение сразу. dangerSwitch(cb, {title, html, ok}, apply(on)) — apply бросает ошибку → переключатель назад.
async function dangerSwitch(cb, o, apply) {
  if (cb.checked) {
    cb.checked = false;
    if (!await askBox({ title: o.title, danger: true, ok: o.ok || 'Включить',
      html: o.html + '<p class="mdl-resp">Это опасная настройка: включая её, ты принимаешь ответственность за последствия. Выключить можно в любой момент.</p>' })) return;
    cb.checked = true;
  }
  cb.disabled = true;
  try { await apply(cb.checked); } catch (e) { cb.checked = !cb.checked; throw e; } finally { cb.disabled = false; }
}

// окно подтверждения посередине экрана с затемнением — одно на весь сайт: вместо confirm() браузера и плашек под
// строкой. askBox({title, text, html, ok, no, danger, info}) → Promise<true|false>; Escape и клик по затемнению — «нет»;
// info: true — сообщение с одной кнопкой (вместо alert()).
// всплывающее поверх всего: карточка и кошелёк — модальные <dialog>, они в верхнем слое браузера, и элемент,
// добавленный в body, оказывается ПОД ними, сколько ни ставь z-index (окно «Заблокировать?» пряталось за карточкой).
// Кладём внутрь открытого модального окна (иначе оно делает всё вне себя неживым) и поднимаем в верхний слой поповером.
function onTop(el) {
  const host = [...document.querySelectorAll('dialog[open]')].filter((d) => { try { return d.matches(':modal'); } catch (e) { return true; } }).pop() || document.body;
  host.appendChild(el); el.classList.add('ontop');
  if ('showPopover' in el) { el.popover = 'manual'; try { el.showPopover(); } catch (e) { /* уже показан */ } }
  return el;
}
function askBox(o) {
  return new Promise((done) => {
    const prev = document.activeElement, w = document.createElement('div');
    w.className = 'mdl';
    w.innerHTML = `<div class="mdl-b" role="alertdialog" aria-modal="true" aria-labelledby="mdl-t" aria-describedby="mdl-x"><h3 id="mdl-t"></h3><div class="mdl-x" id="mdl-x"></div>
      <div class="mdl-a"><button class="btn ghost" type="button" data-no></button><button class="btn${o.danger ? ' bad' : ''}" type="button" data-ok></button></div></div>`;
    w.querySelector('h3').textContent = o.title || 'Точно?';
    const x = w.querySelector('.mdl-x'); if (o.html) x.innerHTML = o.html; else x.textContent = o.text || '';
    w.querySelector('[data-no]').textContent = o.no || 'Отмена';
    w.querySelector('[data-ok]').textContent = o.ok || (o.info ? 'Понятно' : 'Да');
    if (o.info) w.querySelector('[data-no]').remove();
    let over = false;
    const end = (v) => { if (over) return; over = true; document.removeEventListener('keydown', key, true); w.classList.remove('on');
      setTimeout(() => w.remove(), 200); if (prev && prev.focus) prev.focus({ preventScroll: true }); done(v); };
    const key = (e) => { if (e.key === 'Escape') { e.preventDefault(); end(false); }
      else if (e.key === 'Tab') { const b = [...w.querySelectorAll('button')], i = b.indexOf(document.activeElement); e.preventDefault(); b[(i + (e.shiftKey ? b.length - 1 : 1)) % b.length].focus(); } };
    w.addEventListener('mousedown', (e) => { if (e.target === w) end(false); });
    if (!o.info) w.querySelector('[data-no]').onclick = () => end(false);
    w.querySelector('[data-ok]').onclick = () => end(true);
    document.addEventListener('keydown', key, true);
    onTop(w);
    requestAnimationFrame(() => requestAnimationFrame(() => w.classList.add('on')));
    w.querySelector(o.danger && !o.info ? '[data-no]' : '[data-ok]').focus({ preventScroll: true });
  });
}
