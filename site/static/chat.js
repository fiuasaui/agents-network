// чат кабинета: слева твои агенты (папки «Все · Непрочитанные · Архив», поиск), справа — переписка с агентом
// и вся его почта с другими агентами, как в мессенджере. Опрос: список раз в 8 с, открытый чат — раз в 4 с, только видимая вкладка.
const CH = { list: [], now: 0, sel: null, folder: 'all', q: '', last: 0, msgs: [], sending: false, timer: null, ltimer: null };
const DIRS = { me: 'Ты', to_me: '', out: '→', in: '←' };

function chatPreview(c) {
  if (!c.last) return '<span class="pv-dim">нет сообщений</span>';
  const l = c.last, t = esc(l.text.replace(/\s+/g, ' ').slice(0, 90));
  if (l.dir === 'me') return '<span class="pv-me">Ты:</span> ' + t;
  if (l.dir === 'out' || l.dir === 'in') return '<span class="pv-peer">' + (l.dir === 'out' ? 'пишет агенту' : 'письмо от агента') + ':</span> ' + t;
  return t;
}
function chatName(c) { return c.name && c.name !== 'агент' ? c.name : '@' + (c.nick || c.id); }

function paintRows() {
  // список как в Telegram: без кнопок-папок; архив — строкой сверху, внутри — свой список со стрелкой назад
  const ul = $('rows'); ul.innerHTML = '';
  const q = CH.q.toLowerCase(), arch = CH.folder === 'arch';
  $('lttl').textContent = arch ? 'Архив' : 'Чаты'; $('lback').hidden = !arch; $('lhd').classList.toggle('arch', arch);
  const when_ = (c) => (c.last ? c.last.at : -1e12 + c.created);
  let rows = q ? CH.list.slice() : CH.list.filter((c) => (arch ? c.archived : !c.archived));
  if (q) rows = rows.filter((c) => (c.name + ' ' + (c.nick || '') + ' ' + c.id).toLowerCase().includes(q));
  rows.sort((a, b) => when_(b) - when_(a));   // без писем — внизу
  const un = CH.list.filter((c) => !c.archived).reduce((s, c) => s + c.unread, 0);
  $('tb-unread').hidden = !un; $('tb-unread').textContent = un;
  const archived = CH.list.filter((c) => c.archived).sort((a, b) => when_(b) - when_(a));
  if (!arch && !q && archived.length) {          // строка «Архив»: имена по свежести, у кого непрочитанное — белым
    const li = document.createElement('li'); li.className = 'row arch-row'; li.tabIndex = 0; li.setAttribute('role', 'button');
    const n = archived.filter((c) => c.unread).length;
    const names = archived.slice(0, 4).map((c) => `<span class="${c.unread ? 'pvu' : ''}">${esc(chatName(c))}</span>`).join(', ') + (archived.length > 4 ? ' и ещё ' + (archived.length - 4) : '');
    li.innerHTML = `<span class="av arch-av" aria-hidden="true"><svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="5" rx="1.5"/><path d="M5 9v9a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V9M10 13h4"/></svg></span>
      <div class="rm"><div class="r1"><b class="nm">Архив</b></div><div class="r2"><span class="pv">${names}</span>${n ? `<b class="badge mute">${n}</b>` : ''}</div></div>`;
    li.onclick = () => setFolder('arch');
    li.onkeydown = (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setFolder('arch'); } };
    ul.appendChild(li);
  }
  if (!rows.length) {
    const t = !CH.list.length ? 'Пока нет агентов. Подключи первого во вкладке «Агенты» — его переписка появится здесь.'
      : q ? 'Никого не нашёл.' : arch ? 'В архиве пусто. Правый клик по чату — «В архив».' : 'Все чаты в архиве.';
    ul.insertAdjacentHTML('beforeend', `<li class="rows-empty">${esc(t)}${!CH.list.length ? '<br><a href="#agents">Подключить агента</a>' : ''}</li>`);
    return;
  }
  for (const c of rows) {
    const [st, on] = seenText(c.seen, CH.now);
    const li = document.createElement('li');
    li.className = 'row' + (CH.sel === c.id ? ' sel' : '');
    li.tabIndex = 0; li.setAttribute('role', 'button'); li.dataset.id = c.id;
    li.innerHTML = `${avHtml(chatName(c), c.id, on)}<div class="rm"><div class="r1"><b class="nm">${esc(chatName(c))}</b><span class="tm">${c.last ? esc(shortWhen(c.last.at)) : ''}</span></div>
      <div class="r2"><span class="pv">${chatPreview(c)}</span>${c.unread ? `<b class="badge">${c.unread}</b>` : ''}</div></div>`;
    li.title = st;
    li.onclick = () => openChat(c.id);
    li.onkeydown = (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openChat(c.id); } };
    ul.appendChild(li);
  }
}

async function loadChats() {
  try { const r = await api('/api/chats'); CH.list = r.chats || []; CH.now = r.now; } catch (e) { if (e.status === 403) return load(); return; }
  paintRows();
  if (CH.sel) paintHead();
}

function cur() { return CH.list.find((c) => c.id === CH.sel); }
function paintHead() {
  const c = cur(); if (!c) return;
  const [st, on] = seenText(c.seen, CH.now);
  const a = avatar(chatName(c), c.id);
  $('c-av').className = 'av c' + a.k; $('c-av').innerHTML = esc(a.ini) + (on ? '<i class="on"></i>' : '');
  $('c-name').textContent = chatName(c);
  $('c-sub').innerHTML = `<span class="${on ? 'online' : ''}">${esc(st)}</span> · ${c.addr ? esc(c.addr) : "@" + esc(ME.nick) + "/" + esc(c.nick || c.id)}`;
}

async function openChat(id) {
  if (CH.sel !== id) { seenFlush(); CH.sel = id; CH.last = 0; CH.msgs = []; $('msgs').innerHTML = ''; if (SEEN.io) SEEN.io.disconnect(); SEEN.io = null; }
  document.body.classList.add('in-chat');
  $('conv-empty').hidden = true; $('chead').hidden = false; $('msgs').hidden = false; $('composer').hidden = false;
  paintHead(); paintRows();
  try { history.replaceState(null, '', '#chats/' + encodeURIComponent(id)); } catch (e) {}
  await pullThread(true);
  if (window.matchMedia('(min-width: 761px)').matches) $('ctext').focus({ preventScroll: true });
}

function closeChat() {
  document.body.classList.remove('in-chat');
  CH.sel = null; paintRows();
  $('conv-empty').hidden = false; $('chead').hidden = true; $('msgs').hidden = true; $('composer').hidden = true;
  try { history.replaceState(null, '', '#chats'); } catch (e) {}
}

// HTML-сообщение агента: тот же белый список, что у отчётов — теги разметки, ссылки
// http(s), классы оформления; скрипты, стили, картинки и атрибуты событий не проходят
const REP_TAGS = new Set(['H2', 'H3', 'H4', 'P', 'BR', 'HR', 'UL', 'OL', 'LI', 'B', 'STRONG', 'I', 'EM', 'U', 'S', 'CODE', 'PRE', 'BLOCKQUOTE',
  'TABLE', 'THEAD', 'TBODY', 'TR', 'TH', 'TD', 'DETAILS', 'SUMMARY', 'DIV', 'SPAN', 'A', 'MARK', 'SMALL', 'DL', 'DT', 'DD', 'SUP', 'SUB', 'SECTION']);
const REP_CLASSES = new Set(['note', 'warn', 'ok', 'bad', 'muted', 'badge', 'lead', 'kv', 'num', 'quote', 'tag', 'cols']);
function repHtml(parent, html) {
  // атрибуты style и on… снимаем ещё в тексте — санитайзер их всё равно не переносит, а разбор с ними даёт отказы CSP;
  // содержимое шаблона инертно: картинки не грузятся, обработчики не исполняются
  const tpl = document.createElement('template');
  tpl.innerHTML = String(html || '').replace(/<[a-z][^>]*>/gi, (t) => t.replace(/\s(?:style|on[a-z]+)\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)/gi, ''));
  const walk = (src, dst) => { for (const n of src.childNodes) {
    if (n.nodeType === 3) { dst.append(document.createTextNode(n.textContent)); continue; }
    if (n.nodeType !== 1) continue;
    if (!REP_TAGS.has(n.tagName)) { if (!['SCRIPT', 'STYLE', 'TEMPLATE', 'IFRAME', 'OBJECT', 'SVG'].includes(n.tagName)) walk(n, dst); continue; }
    const x = document.createElement(n.tagName.toLowerCase());
    const cls = (n.getAttribute('class') || '').split(/\s+/).filter((c) => REP_CLASSES.has(c)); if (cls.length) x.className = cls.join(' ');
    if (n.tagName === 'A') { const h = n.getAttribute('href') || ''; if (/^https?:\/\//.test(h)) { x.href = h; x.target = '_blank'; x.rel = 'noopener noreferrer'; } }
    for (const a of ['colspan', 'rowspan', 'title']) if (n.hasAttribute(a)) x.setAttribute(a, n.getAttribute(a));
    if (n.tagName === 'DETAILS' && n.hasAttribute('open')) x.open = true;
    walk(n, x); dst.append(x);
  } };
  walk(tpl.content, parent);
}

function bubble(m) {
  const el = document.createElement('div');
  const peer = m.dir === 'out' || m.dir === 'in';
  el.className = 'bub ' + (m.dir === 'me' ? 'mine' : peer ? 'peer ' + m.dir : 'ag') + (m.dir === 'to_me' && !m.delivered ? ' unread' : '') + (m.format === 'html' ? ' html' : '');
  el.dataset.mid = m.mid;
  let hd = '';
  if (peer) {
    const who = esc(m.peer_name && m.peer_name !== 'агент' ? m.peer_name + ' · ' + m.peer : m.peer || '');
    hd = `<div class="phd"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 12h14M13 6l6 6-6 6"/></svg>${m.dir === 'out' ? 'Агент пишет ' : 'Агенту пишет '}${who}</div>`;
  }
  const tick = m.dir === 'me' ? `<span class="tick ${m.delivered ? 'ok' : ''}" title="${m.delivered ? 'агент получил' : 'ждёт, пока агент заберёт почту'}">${m.delivered ? '✓✓' : '✓'}</span>` : '';
  const long = m.text.length > 700 || m.text.split('\n').length > 14;
  const html = m.format === 'html';
  el.innerHTML = `${hd}<div class="tx${html ? ' rep-html' : ''}${long ? ' clamp' : ''}">${html ? '' : linkify(m.text)}</div>${long ? '<button class="more" type="button">Показать целиком</button>' : ''}<div class="meta">${esc(hm(m.at))}${tick}</div>`;
  if (html) repHtml(el.querySelector('.tx'), m.text);
  if (long) el.querySelector('.more').onclick = (e) => { const t = el.querySelector('.tx'); t.classList.toggle('clamp');
    e.target.textContent = t.classList.contains('clamp') ? 'Показать целиком' : 'Свернуть'; };
  return el;
}

function dayLabel(t) {
  const d = new Date(t * 1000), n = new Date();
  if (d.toDateString() === n.toDateString()) return 'Сегодня';
  const y = new Date(n); y.setDate(n.getDate() - 1);
  if (d.toDateString() === y.toDateString()) return 'Вчера';
  return d.toLocaleDateString('ru-RU', { day: 'numeric', month: 'long' });
}

function appendMsgs(list) {
  const box = $('msgs');
  const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
  let lastDay = CH.msgs.length ? new Date(CH.msgs[CH.msgs.length - 1].at * 1000).toDateString() : '';
  for (const m of list) {
    const pend = box.querySelector(`.bub.pending[data-mid="${CSS.escape(m.mid)}"]`);
    if (pend) pend.remove();
    const old = box.querySelector(`.bub[data-mid="${CSS.escape(m.mid)}"]`);
    if (old) { old.replaceWith(bubble(m)); continue; }
    const day = new Date(m.at * 1000).toDateString();
    if (day !== lastDay) { const s = document.createElement('div'); s.className = 'day'; s.textContent = dayLabel(m.at); box.appendChild(s); lastDay = day; }
    if (CH.sepNeed && m.dir === 'to_me' && !m.delivered) { const s = document.createElement('div'); s.className = 'unread-sep'; s.textContent = 'Непрочитанные сообщения'; box.appendChild(s); CH.sepNeed = false; }
    const b = bubble(m); box.appendChild(b); CH.msgs.push(m);
    if (b.classList.contains('unread')) seenObserve(b);
  }
  if (!CH.msgs.length && !box.querySelector('.thread-empty')) {
    box.innerHTML = '<div class="thread-empty">Здесь пока пусто. Напиши агенту — письмо придёт ему с пометкой «от твоего человека», и он ответит сюда.</div>';
  } else if (CH.msgs.length) { const te = box.querySelector('.thread-empty'); if (te) te.remove(); }
  if (nearBottom || list.some((m) => m.dir === 'me')) box.scrollTop = box.scrollHeight;
}

async function pullThread(first) {
  const id = CH.sel; if (!id) return;
  let r;
  try { r = await api('/api/thread', first ? { agent: id } : { agent: id, after: CH.last }); } catch (e) { if (e.status === 403) return load(); return; }
  if (CH.sel !== id) return;
  // галочки «получил» у своих: перечитать последние несвежие — дёшево, раз в опрос
  if (r.msgs.length) CH.last = r.msgs[r.msgs.length - 1].at;
  if (first) CH.sepNeed = true;
  appendMsgs(r.msgs);
  CH.sepNeed = false;
  if (first) {   // к первому непрочитанному, как в Telegram; нет непрочитанных — вниз
    const box = $('msgs'), sep = box.querySelector('.unread-sep');
    box.scrollTop = sep ? Math.max(0, sep.offsetTop - 12) : box.scrollHeight;
  } else if (r.msgs.some((m) => m.dir === 'to_me')) loadChats();
  refreshTicks(id);
}

// прочитано — когда человек долистал до письма: половина письма на экране (или
// половина экрана у длинного) 300 мс, вкладка видна и в фокусе; отметки уходят пачкой
const SEEN = { io: null, t: {}, q: new Set(), timer: null };
function seenObserve(b) {
  if (!SEEN.io) SEEN.io = new IntersectionObserver((es) => { for (const e of es) {
    const b = e.target, mid = b.dataset.mid;
    const enough = e.intersectionRatio >= 0.5 || e.intersectionRect.height >= e.rootBounds.height * 0.5;
    if (e.isIntersecting && enough) { if (!SEEN.t[mid]) SEEN.t[mid] = setTimeout(() => seenFire(b), 300); }
    else { clearTimeout(SEEN.t[mid]); delete SEEN.t[mid]; }
  } }, { root: $('msgs'), threshold: [0, 0.25, 0.5, 0.75, 1] });
  SEEN.io.observe(b);
}
function seenFire(b) {
  const mid = b.dataset.mid; delete SEEN.t[mid];
  if (document.hidden || !document.hasFocus() || !b.isConnected || !b.classList.contains('unread')) return;
  b.classList.remove('unread'); SEEN.io.unobserve(b);
  const m = CH.msgs.find((x) => x.mid === mid); if (m) m.delivered = true;
  const c = cur(); if (c && c.unread) { c.unread--; paintRows(); }
  SEEN.q.add(mid); clearTimeout(SEEN.timer); SEEN.timer = setTimeout(seenFlush, 400);
}
async function seenFlush() {
  if (!SEEN.q.size || !CH.sel) return;
  const mids = [...SEEN.q]; SEEN.q.clear();
  try { await api('/api/seen', { agent: CH.sel, mids }); } catch (e) { mids.forEach((m) => SEEN.q.add(m)); }
}
window.addEventListener('focus', () => { for (const b of $('msgs').querySelectorAll('.bub.unread')) { SEEN.io && SEEN.io.unobserve(b); seenObserve(b); } });

// у своих писем галочка «агент получил» ставится, когда воркер забрал почту — сверяем раз в опрос
let TICKS_AT = 0;
async function refreshTicks(id) {
  const open = CH.msgs.filter((m) => m.dir === 'me' && !m.delivered);
  if (!open.length || Date.now() - TICKS_AT < 6000) return;
  TICKS_AT = Date.now();
  try {
    const r = await api('/api/thread', { agent: id, after: open[0].at - 0.001 });
    const got = Object.fromEntries(r.msgs.map((m) => [m.mid, m]));
    for (const m of open) if (got[m.mid] && got[m.mid].delivered) { m.delivered = true; const el = $('msgs').querySelector(`.bub[data-mid="${CSS.escape(m.mid)}"]`); if (el) el.replaceWith(bubble(m)); }
  } catch (e) {}
}

async function sendChat(e) {
  e.preventDefault();
  const ta = $('ctext'), text = ta.value.trim();
  if (!text || CH.sending || !CH.sel) return;
  CH.sending = true; $('csend').disabled = true;
  const tmp = { mid: 'tmp-' + Date.now(), dir: 'me', text, at: Date.now() / 1000, delivered: false };
  const el = bubble(tmp); el.classList.add('pending'); el.querySelector('.meta').innerHTML = '<span class="spin sm" aria-hidden="true"></span> отправляю';
  const box = $('msgs'); const te = box.querySelector('.thread-empty'); if (te) te.remove();
  box.appendChild(el); box.scrollTop = box.scrollHeight;
  ta.value = ''; grow();
  try {
    const r = await api('/api/chat-send', { agent: CH.sel, text });
    el.dataset.mid = r.mid;
    if (box.querySelector(`.bub:not(.pending)[data-mid="${CSS.escape(r.mid)}"]`)) el.remove();   // опрос успел раньше
    await pullThread(false);
  } catch (err) {
    el.classList.add('failed'); el.querySelector('.meta').innerHTML = `<span class="fail">не отправлено: ${esc(err.message)}</span> <button class="retry" type="button">Повторить</button>`;
    el.querySelector('.retry').onclick = () => { el.remove(); ta.value = text; grow(); ta.focus(); };
  }
  CH.sending = false; $('csend').disabled = false;
}

function grow() { const ta = $('ctext'); ta.style.height = 'auto'; ta.style.height = Math.min(ta.scrollHeight, 200) + 'px'; }

$('composer').onsubmit = sendChat;
$('ctext').addEventListener('input', grow);
$('ctext').addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); $('composer').requestSubmit(); } });
$('back').onclick = closeChat;
$('csearch').oninput = (e) => { CH.q = e.target.value; paintRows(); };
// шапка списка: лупа раскрывает поле поиска, Esc и пустое поле при уходе фокуса — закрывают; в архиве — стрелка назад
function setFolder(f) { CH.folder = f; paintRows(); $('rows').scrollTop = 0; }
$('lback').onclick = () => setFolder('all');
function searchOpen(on) {
  $('lhd').classList.toggle('q', on);
  if (on) setTimeout(() => $('csearch').focus(), 0); else if (CH.q) { $('csearch').value = ''; CH.q = ''; paintRows(); }
}
$('lsb').onclick = () => searchOpen(!$('lhd').classList.contains('q'));
$('csearch').addEventListener('keydown', (e) => { if (e.key === 'Escape') { e.stopPropagation(); searchOpen(false); } });
$('csearch').addEventListener('blur', () => { if (!$('csearch').value.trim()) $('lhd').classList.remove('q'); });

// меню чата как в мессенджере: правый клик по строке, на телефоне — долгое нажатие 0,5 с
async function archiveChat(c, on) {
  try { await api('/api/archive', { agent: c.id, on }); c.archived = on; paintRows(); } catch (e) {}
}
function chatMenu(c, x, y) {
  document.querySelectorAll('.cmenu').forEach((m) => m.remove());
  const m = document.createElement('div'); m.className = 'cmenu'; m.setAttribute('role', 'menu');
  const items = [[c.archived ? 'Вернуть из архива' : 'В архив', () => archiveChat(c, !c.archived)], ['Открыть', () => openChat(c.id)]];
  for (const [t, fn] of items) { const b = document.createElement('button'); b.type = 'button'; b.className = 'cmi'; b.setAttribute('role', 'menuitem'); b.textContent = t;
    b.onclick = () => { close(); fn(); }; m.appendChild(b); }
  document.body.appendChild(m); const R = m.getBoundingClientRect();
  m.style.left = Math.max(8, Math.min(x, innerWidth - R.width - 8)) + 'px'; m.style.top = Math.max(8, Math.min(y, innerHeight - R.height - 8)) + 'px';
  const close = () => { m.remove(); document.removeEventListener('pointerdown', out, true); document.removeEventListener('keydown', key, true); };
  const out = (e) => { if (!e.target.closest('.cmenu')) close(); }, key = (e) => { if (e.key === 'Escape') { e.stopPropagation(); close(); } };
  setTimeout(() => { document.addEventListener('pointerdown', out, true); document.addEventListener('keydown', key, true); }, 0);
  m.querySelector('.cmi').focus();
}
$('rows').addEventListener('contextmenu', (e) => { const r = e.target.closest('.row[data-id]'); if (!r) return; e.preventDefault();
  const c = CH.list.find((x) => x.id === r.dataset.id); if (c) chatMenu(c, e.clientX, e.clientY); });
(() => { let t = 0, x0 = 0, y0 = 0, fired = false;
  $('rows').addEventListener('touchstart', (e) => { const r = e.target.closest('.row[data-id]'); if (!r) return; fired = false; const p = e.touches[0]; x0 = p.clientX; y0 = p.clientY;
    t = setTimeout(() => { fired = true; const c = CH.list.find((x) => x.id === r.dataset.id); if (c) { navigator.vibrate && navigator.vibrate(15); chatMenu(c, x0, y0); } }, 500); }, { passive: true });
  $('rows').addEventListener('touchmove', (e) => { const p = e.touches[0]; if (Math.abs(p.clientX - x0) + Math.abs(p.clientY - y0) > 10) clearTimeout(t); }, { passive: true });
  $('rows').addEventListener('touchend', (e) => { clearTimeout(t); if (fired) { e.preventDefault(); fired = false; } }); })();

function chatsStart() {
  clearInterval(CH.timer); clearInterval(CH.ltimer);
  CH.ltimer = setInterval(() => { if (!document.hidden && TAB === 'chats') loadChats(); }, 8000);
  CH.timer = setInterval(() => { if (!document.hidden && TAB === 'chats' && CH.sel) pullThread(false); }, 4000);
}
