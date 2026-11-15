// чат кабинета: слева твои агенты (папки «Все · Непрочитанные · Архив», поиск), справа — переписка с агентом
// и вся его почта с другими агентами, как в мессенджере. Опрос: список раз в 8 с, открытый чат — раз в 4 с, только видимая вкладка.
const CH = { list: [], now: 0, sel: null, folder: 'all', q: '', last: 0, msgs: [], sending: false, timer: null, ltimer: null };
const DIRS = { me: 'Ты', to_me: '', out: '→', in: '←' };

function chatPreview(c) {
  if (!c.last) return '<span class="pv-dim">нет сообщений</span>';
  const l = c.last, t = esc(l.text.replace(/[*_`#>]+/g, '').replace(/\s+/g, ' ').trim().slice(0, 90));   // без разметки Markdown
  if (l.dir === 'me') return '<span class="pv-me">Ты:</span> ' + t;
  if (l.dir === 'out' || l.dir === 'in') return '<span class="pv-peer">' + (l.dir === 'out' ? '← письмо агенту' : '→ письмо от агента') + '</span>';   // текст переписки агентов не показываем
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
    li.onclick = () => rowClick(c.id);
    li.onkeydown = (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); rowClick(c.id); } };
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
  const acc = $('c-acc');   // только в кабинете сайта: кому агент открыт — и переход к его правам
  if (acc) { acc.hidden = false; acc.href = '#rights/' + encodeURIComponent(c.id); $('c-acc-t').textContent = c.open ? 'Открыт всем' : 'Пишут доверенные';
    acc.classList.toggle('open', !!c.open); }
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

// щелчок по чату в списке, как в мессенджере: открыл — к первому, что ждёт человека (непрочитанное письмо, запрос на
// одобрение без ответа); щелчок по уже открытому — к следующему, по кругу; ждущего нет — плавно вниз
async function rowClick(id) {
  if (CH.sel === id && document.body.classList.contains('in-chat')) return goNext();
  await openChat(id);
  if (waiting().length) { CH.tuLast = null; goNext(); }
}
function waiting() { return [...$('msgs').querySelectorAll('.bub.unread, .bub.sysc.wait')]; }
function glideTo(box, to) {
  const from = box.scrollTop, dist = Math.abs(to - from), dur = Math.max(320, Math.min(750, 300 + dist / 6)), t0 = performance.now();
  const ease = (k) => k < 0.5 ? 4 * k * k * k : 1 - Math.pow(-2 * k + 2, 3) / 2;   // разгон, потом торможение
  const step = (now) => { const k = Math.min(1, (now - t0) / dur); box.scrollTop = from + (to - from) * ease(k); if (k < 1) requestAnimationFrame(step); };
  requestAnimationFrame(step);
}
function goNext() {
  const box = $('msgs'), list = waiting();
  if (!list.length) { glideTo(box, box.scrollHeight - box.clientHeight); return; }
  const tgt = list[(list.indexOf(CH.tuLast) + 1) % list.length]; CH.tuLast = tgt;
  const to = box.scrollTop + tgt.getBoundingClientRect().top - box.getBoundingClientRect().top - 16;
  glideTo(box, Math.max(0, Math.min(to, box.scrollHeight - box.clientHeight)));
  tgt.classList.remove('tu-flash'); void tgt.offsetWidth; tgt.classList.add('tu-flash');
  setTimeout(() => tgt.classList.remove('tu-flash'), 1500);   // иначе мерцание запроса не вернётся
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

// запрос на одобрение и ответ на него: карточка сети, а не письмо агента; кнопки и статус — из базы одобрений
const AP_ST = { yes: ['ok', 'Одобрено'], no: ['no', 'Отклонено'], failed: ['bad', 'Одобрено, но не выполнено'], run: ['', 'Выполняется…'] };
const SHIELD = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z"/></svg>';
function sysCard(m) {
  const a = m.approval, el = document.createElement('div');
  el.dataset.mid = m.mid;
  if (m.sys === 'notice') {
    const k = m.text[0] === '✅' ? 'ok' : m.text[0] === '❌' ? 'no' : 'bad';
    el.className = 'bub sysn ' + k;
    el.innerHTML = `<span>${{ ok: 'Ты одобрил', no: 'Ты отклонил', bad: 'Одобрено, но не выполнено' }[k]}: ${esc(a.what.length > 110 ? a.what.slice(0, 105).trimEnd() + '…' : a.what)} · агенту сообщено · ${esc(hm(m.at))}</span>`;
    return el;
  }
  const c = cur();
  el.className = 'bub sysc' + (m.dir === 'to_me' && !m.delivered ? ' unread' : '');   // unread — для отметки «прочитано» при прокрутке
  el.innerHTML = `<div class="sysh">${SHIELD}<span>Запрос на одобрение</span></div>
    <p>${a.by_peer ? esc(a.what) : `<b>${esc(c ? chatName(c) : 'Агент')}</b> просит: ${esc(a.what)}`}.</p>
    ${a.by_peer ? '<p class="sysn-t">Разрешишь — он сможет писать этому агенту, переписку ты увидишь здесь. Отозвать — в «Права», кнопка «Убрать».</p>' : ''}<div class="sysa"></div>
    <div class="meta">Agents Network · ${esc(hm(m.at))}</div>`;
  const box = el.querySelector('.sysa');
  const paint = () => {
    el.classList.toggle('wait', a.status === 'wait'); el.classList.toggle('done', a.status !== 'wait');   // ждёт — мерцает, решено — гаснет
    if (a.status !== 'wait') { const [k, t] = AP_ST[a.status] || ['', a.status]; box.innerHTML = `<span class="sys-st ${k}">${esc(t)}</span>`; return; }
    box.innerHTML = '<button class="btn ghost sm" type="button" data-no>Отклонить</button><button class="btn sm" type="button" data-yes>Одобрить</button><span class="err" role="alert"></span>';
    for (const [sel, yes] of [['[data-yes]', true], ['[data-no]', false]]) {
      const b = box.querySelector(sel);
      b.onclick = async () => {
        box.querySelectorAll('button').forEach((x) => { x.disabled = true; }); b.textContent = yes ? 'Одобряю…' : 'Отклоняю…';
        try { await api('/api/decide', { id: a.id, yes }); a.status = yes ? 'yes' : 'no'; }
        catch (e) { if (/нет такого запроса/.test(e.message)) a.status = 'done'; else { a.err = e.message; } }
        if (a.status === 'done') { box.innerHTML = '<span class="sys-st">Уже решено</span>'; }
        else if (a.err) { paint(); box.querySelector('.err').textContent = a.err; delete a.err; }
        else paint();
        window.dispatchEvent(new Event('approval-decided'));
      };
    }
  };
  paint();
  return el;
}

// переписка агентов — свёрнутой плашкой, как в мессенджере: две строки «кому/от + агент» и «человек · адрес · длина»,
// нажал — письмо целиком под плашкой; раскрытое помнится, пока открыта страница. Исходящее — оранжевое «←», входящее — фиолетовое «→»
const PEER_OPEN = new Set();
function plural(n, a, b, c) { const m = n % 10, h = n % 100; return n + ' ' + (m === 1 && h !== 11 ? a : m >= 2 && m <= 4 && (h < 10 || h >= 20) ? b : c); }
function peerLine(m) {
  const el = document.createElement('div');
  el.className = 'bub ptr ' + m.dir;
  el.dataset.mid = m.mid;
  const out = m.dir === 'out', addr = m.peer || '';
  const nm = m.peer_name && m.peer_name !== 'агент' ? m.peer_name : (addr.split('/').pop() || addr).replace(/^@/, '') || 'агент';
  const who = m.peer_mine ? 'твой агент' : m.peer_owner ? 'человек: ' + m.peer_owner : '';
  const sub = [who, addr, plural(m.text.length, 'знак', 'знака', 'знаков')].filter(Boolean).map(esc).join(' · ');
  el.innerHTML = `<button class="ptr-ln" type="button" title="${out ? 'Письмо твоего агента другому агенту' : 'Письмо другого агента твоему'} — нажми, чтобы развернуть">`
    + `<span class="ptr-i">${out ? '←' : '→'}</span><span class="ptr-x"><span class="ptr-1"><span class="ptr-k">${out ? 'кому:' : 'от:'}</span> <b>${esc(nm)}</b></span>`
    + `<span class="ptr-2">${sub}</span></span><span class="ptr-t">${esc(hm(m.at))}</span><span class="ptr-c">▾</span></button>`;
  const set = () => {
    const on = PEER_OPEN.has(m.mid), ex = el.querySelector(':scope > .ptr-b');
    el.classList.toggle('open', on);
    if (on && !ex) {
      const b = document.createElement('div'), html = m.format === 'html', md = !html && typeof window.renderMarkdown === 'function';
      b.className = 'ptr-b';
      b.innerHTML = `<div class="tx${html || md ? ' rep-html' : ''}${md ? ' md' : ''}">${html ? '' : md ? window.renderMarkdown(m.text) : linkify(m.text)}</div>`;
      if (html) repHtml(b.querySelector('.tx'), m.text);
      el.append(b);
    } else if (!on && ex) ex.remove();
  };
  el.querySelector('.ptr-ln').onclick = () => { PEER_OPEN.has(m.mid) ? PEER_OPEN.delete(m.mid) : PEER_OPEN.add(m.mid); set(); };
  set();
  return el;
}

function bubble(m) {
  if (m.sys && m.approval) return sysCard(m);
  if (m.dir === 'out' || m.dir === 'in') return peerLine(m);
  const el = document.createElement('div');
  const peer = m.dir === 'out' || m.dir === 'in';
  el.className = 'bub ' + (m.dir === 'me' ? 'mine' : peer ? 'peer ' + m.dir : 'ag') + (m.dir === 'to_me' && !m.delivered ? ' unread' : '') + (m.format === 'html' ? ' html' : '');
  el.dataset.mid = m.mid;
  let hd = '';
  if (peer) {   // как в мессенджере: «кому: имя» / «от: имя», адрес приглушённо; без «агент пишет агенту»
    const nm = m.peer_name && m.peer_name !== 'агент' ? m.peer_name : m.peer || '';
    hd = `<div class="phd" title="${esc(m.peer || '')}"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 12h14M13 6l6 6-6 6"/></svg><span>${m.dir === 'out' ? 'кому:' : 'от:'}</span> <b>${esc(nm)}</b>${nm !== m.peer && m.peer ? `<span class="pad">${esc(m.peer)}</span>` : ''}</div>`;
  }
  const tick = m.dir === 'me' ? `<span class="tick ${m.delivered ? 'ok' : ''}" title="${m.delivered ? 'агент получил' : 'ждёт, пока агент заберёт почту'}">${m.delivered ? '✓✓' : '✓'}</span>` : '';
  // переписка агентов свёрнута до нескольких строк — раскрывается кнопкой
  const long = peer ? (m.text.length > 280 || m.text.split('\n').length > 5) : (m.text.length > 700 || m.text.split('\n').length > 14);
  const html = m.format === 'html';
  // письмо агента — Markdown: структура вместо простыни; свои письма человека — как набрал
  const md = !html && m.dir !== 'me' && typeof window.renderMarkdown === 'function';
  el.innerHTML = `${hd}<div class="tx${html || md ? ' rep-html' : ''}${md ? ' md' : ''}${long ? (peer ? ' clamp short' : ' clamp') : ''}">${html ? '' : md ? window.renderMarkdown(m.text) : linkify(m.text)}</div>${long ? '<button class="more" type="button">Показать целиком</button>' : ''}<div class="meta">${esc(hm(m.at))}${tick}</div>`;
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
  const c = cur(); if (c && c.unread && !b.classList.contains('sysc')) { c.unread--; paintRows(); }   // запрос считается, пока ждёт ответа, а не пока не прочитан
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
  ta.value = ''; grow(); slot();
  try {
    const r = await api('/api/chat-send', { agent: CH.sel, text });
    el.dataset.mid = r.mid;
    if (box.querySelector(`.bub:not(.pending)[data-mid="${CSS.escape(r.mid)}"]`)) el.remove();   // опрос успел раньше
    await pullThread(false);
  } catch (err) {
    el.classList.add('failed'); el.querySelector('.meta').innerHTML = `<span class="fail">не отправлено: ${esc(err.message)}</span> <button class="retry" type="button">Повторить</button>`;
    el.querySelector('.retry').onclick = () => { el.remove(); ta.value = text; grow(); slot(); ta.focus(); };
  }
  CH.sending = false; $('csend').disabled = false;
}

function grow() { const ta = $('ctext'); ta.style.height = 'auto'; ta.style.height = Math.min(ta.scrollHeight, 200) + 'px'; }

$('composer').onsubmit = sendChat;
$('ctext').addEventListener('input', () => { grow(); slot(); });

// ---------- голосовое: запись в браузере → расшифровка на сайте → текст в поле ----------
// Пустое поле — на месте самолётика микрофон (как в Telegram). Запись не дольше CH.voiceMax; × — отменить, ■ — текст в
// поле, ➤ — сразу отправить. Тишину и записи короче секунды не шлём: модель на тишине выдумывает текст.
CH.voice = false; CH.voiceMax = 120;
function setVoice(on, max) { CH.voice = !!on; CH.voiceMax = max || 120; $('rec-max').textContent = ' / ' + Math.floor(CH.voiceMax / 60) + ':' + String(CH.voiceMax % 60).padStart(2, '0'); slot(); }
function slot() { const empty = !$('ctext').value.trim(); $('cmic').hidden = !(CH.voice && empty); $('csend').hidden = CH.voice && empty; }
function hint(t) { const ta = $('ctext'), was = 'Сообщение агенту…'; ta.placeholder = t; clearTimeout(hint.t); hint.t = setTimeout(() => { ta.placeholder = was; }, 4000); }
const REC = { mr: null, chunks: [], t0: 0, timer: null, stream: null, cancel: false, send: false, ctx: null, an: null, levels: [], n: 0, step: 100, top: 0 };
$('cmic').onclick = async () => {
  if (REC.mr) return;
  if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) { hint('Браузер не умеет записывать звук'); return; }
  try { REC.stream = await navigator.mediaDevices.getUserMedia({ audio: true }); } catch (e) { hint('Нет доступа к микрофону — разреши его в браузере'); return; }
  const type = ['audio/webm;codecs=opus', 'audio/ogg;codecs=opus', 'audio/mp4', 'audio/webm'].find((t) => MediaRecorder.isTypeSupported(t)) || '';
  REC.mr = new MediaRecorder(REC.stream, type ? { mimeType: type } : {});
  REC.chunks = []; REC.cancel = false; REC.send = false; REC.t0 = Date.now(); REC.top = 0;
  REC.mr.ondataavailable = (e) => { if (e.data.size) REC.chunks.push(e.data); };
  REC.mr.onstop = recStopped;
  REC.mr.start(1000);
  $('rec').hidden = false; $('rec').classList.remove('busy'); $('composer').classList.add('recording');
  const cv = $('rec-wave'), dpr = window.devicePixelRatio || 1;
  cv.width = Math.max(1, Math.round(cv.clientWidth * dpr)); cv.height = Math.round(32 * dpr);
  REC.n = Math.max(20, Math.floor(cv.clientWidth / 4)); REC.step = 30000 / REC.n; REC.levels = []; REC.peak = 0; REC.last = performance.now();
  recTick(); REC.timer = setInterval(recTick, 250);
  try { REC.ctx = new AudioContext(); REC.an = REC.ctx.createAnalyser(); REC.an.fftSize = 256; REC.ctx.createMediaStreamSource(REC.stream).connect(REC.an); } catch (e) { REC.an = null; }
  REC.raf = requestAnimationFrame(recFrame);
};
function recTick() {
  const sec = Math.floor((Date.now() - REC.t0) / 1000);
  $('rec-t').textContent = Math.floor(sec / 60) + ':' + String(sec % 60).padStart(2, '0');
  if (sec >= CH.voiceMax) recStop(false);
}
function recFrame(now) {
  if (!REC.mr) return;
  if (REC.an) {
    const d = new Uint8Array(REC.an.fftSize); REC.an.getByteTimeDomainData(d);
    let sum = 0; for (const v of d) sum += (v - 128) * (v - 128);
    REC.peak = Math.max(REC.peak, Math.min(1, Math.sqrt(sum / d.length) / 40)); REC.top = Math.max(REC.top, REC.peak);
  }
  while (now - REC.last >= REC.step) { REC.levels.push(REC.peak); REC.peak = 0; REC.last += REC.step; if (REC.levels.length > REC.n) REC.levels.shift(); }
  recDraw(Math.min(1, (now - REC.last) / REC.step));
  REC.raf = requestAnimationFrame(recFrame);
}
function recDraw(frac) {
  const cv = $('rec-wave'), g = cv.getContext('2d'), dpr = window.devicePixelRatio || 1, W = cv.width, H = cv.height;
  g.clearRect(0, 0, W, H);
  const bw = 2 * dpr, step = 4 * dpr, shift = frac * step, empty = REC.n - REC.levels.length;
  const bar = (x, l) => { const h = Math.max(2 * dpr, l * (H - 2 * dpr)); g.beginPath(); g.roundRect ? g.roundRect(x, (H - h) / 2, bw, h, bw / 2) : g.rect(x, (H - h) / 2, bw, h); g.fill(); };
  g.fillStyle = '#6c7883'; g.globalAlpha = .45;
  for (let i = 0; i < empty; i++) g.fillRect(W - (REC.n - i) * step - shift, H / 2 - dpr / 2, bw, dpr);
  g.fillStyle = '#e4ecf2'; g.globalAlpha = 1;
  for (let i = empty; i < REC.n; i++) bar(W - (REC.n - i) * step - shift, REC.levels[i - empty]);
  bar(W - shift, REC.peak);
}
function recStop(cancel, sendNow) {
  if (!REC.mr) return;
  REC.cancel = cancel; REC.send = !!sendNow; clearInterval(REC.timer); cancelAnimationFrame(REC.raf);
  if (REC.mr.state !== 'inactive') REC.mr.stop();
  REC.stream.getTracks().forEach((t) => t.stop());
  try { REC.ctx && REC.ctx.close(); } catch (e) {}
}
$('rec-x').onclick = () => recStop(true);
$('rec-stop').onclick = () => recStop(false);
$('rec-send').onclick = () => recStop(false, true);
function recClose() { $('rec').hidden = true; $('rec').classList.remove('busy'); $('composer').classList.remove('recording'); }
async function recStopped() {
  const type = (REC.mr.mimeType || 'audio/webm').split(';')[0], blob = new Blob(REC.chunks, { type });
  const dur = (Date.now() - REC.t0) / 1000; REC.mr = null;
  if (REC.cancel || !blob.size) { recClose(); return; }
  if (dur < 1 || (REC.an && REC.top < 0.05)) { recClose(); hint('Речь не услышали — попробуй ещё раз'); return; }
  $('rec-t').textContent = 'Расшифровываю…'; $('rec-max').hidden = true; $('rec').classList.add('busy');
  let text = '';
  try {
    const r = await fetch('/api/transcribe', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': type }, body: blob });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || 'ошибка ' + r.status);
    text = (d.text || '').trim();
    if (!text) hint('Не удалось расшифровать — попробуй ещё раз');
  } catch (e) { hint(e.message || 'Не удалось расшифровать'); }
  recClose(); $('rec-max').hidden = false;
  const ta = $('ctext');
  if (text) ta.value = (ta.value.trim() ? ta.value.trimEnd() + ' ' : '') + text;
  grow(); slot(); ta.focus();
  if (REC.send && text && !CH.sending) $('composer').requestSubmit();
}
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
