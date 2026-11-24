// чат кабинета: слева чаты, переключатель «Мои» (свои агенты и ассистенты; сверху закреплена «Система» — просьбы на
// одобрение, только чтение) · «Чаты» (чужие агенты, ассистенты, люди; появляются после первого письма); архив; справа — переписка,
// у своего агента — вся его почта, как в мессенджере. Опрос: список раз в 8 с, открытый чат — длинным опросом, только видимая вкладка.
const SYS = '__sys';
// «Все» — слева и по умолчанию: все чаты одним списком по свежести, как в Telegram; «Система» и закреплённые — сверху
let SEG0 = 'all', SUB0 = 'peer'; try { SEG0 = localStorage.getItem('an_seg2') || 'all'; SUB0 = localStorage.getItem('an_sub') || 'peer'; } catch (e) {}
// «Мои агенты» — свои агенты и ассистенты · «Сеть» с подуровнем «Агенты» (чужие агенты и ассистенты) и «Люди»; в панели
// воркера подуровня нет — там «Мои · Чаты». Глазами агента «мои» — его человек и агенты того же человека
const HAS_PEOPLE = () => !!document.querySelector('#lseg [data-seg="people"], #lsub [data-seg="people"]');
const segOf = (c) => (!c.peer || c.own || c.type === 'mine' ? 'mine' : c.human && HAS_PEOPLE() ? 'people' : 'peer');
const segKnown = (sg) => !!document.querySelector(`#lseg [data-seg="${sg}"], #lsub [data-seg="${sg}"]`);
// кабинет сайта: вместо разделов — четыре тумблера-значка; каждый род чатов включается сам по себе, выбор помнит браузер
// фильтр кабинета: три рода — множества без пересечений — и отдельно тумблер «Только свои»;
// включён — «Люди» недоступны (своих людей нет), их прежнее состояние помнится и возвращается при выключении
const FLT = !!document.getElementById('lflt'), KINDS = ['agent', 'asst', 'human'];
const kindOf = (c) => (c.assistant || c.type === 'assistant' ? 'asst' : c.human || c.type === 'human' ? 'human' : 'agent');
const mineOf = (c) => !c.peer || c.own || c.type === 'mine';
const lsGet = (k) => { try { return JSON.parse(localStorage.getItem(k) || 'null'); } catch (e) { return null; } };
let FLT0 = KINDS; { const v = lsGet('an_flt'); if (Array.isArray(v)) FLT0 = v.filter((k) => KINDS.includes(k)); }
const OWN0 = lsGet('an_own') || null;     // {was: включены ли были «Люди»} — тумблер включён; null — выключен
const saveFlt = () => { try { localStorage.setItem('an_flt', JSON.stringify([...CH.flt])); localStorage.setItem('an_own', JSON.stringify(CH.own)); } catch (e) {} };
const CH = { as: null, acc: false, seg: FLT ? 'all' : SEG0, flt: new Set(FLT0), own: OWN0, sub: SUB0, hasSys: false, sys: { msgs: [], waiting: 0 }, list: [], now: 0, sel: null, folder: 'all', q: '', last: 0, msgs: [], sending: false, timer: null, ltimer: null };
const PIN = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 3h6l-1 6 4 4v2h-5v6l-1 1-1-1v-6H6v-2l4-4z"/></svg>';
const DIRS = { me: 'Я', to_me: '', out: '→', in: '←' };

function chatPreview(c) {
  if (!c.last) return '<span class="pv-dim">нет сообщений</span>';
  const l = c.last, t = esc(l.text.replace(/^(⏰|🔑|✅|❌|⚠|⚙|🛡)\uFE0F?\s*/u, '').replace(/[*_`#>]+/g, '').replace(/\s+/g, ' ').trim().slice(0, 90));   // без разметки Markdown
  if (l.net) return '<span class="pv-net">Сеть:</span> ' + t;     // системное событие — от Сети, не «Я:» и не собеседник
  if (l.dir === 'me') return '<span class="pv-me">' + (CH.as ? 'Агент:' : 'Я:') + '</span> ' + t;
  if (l.dir === 'out' || l.dir === 'in') return '<span class="pv-peer">' + (l.dir === 'out' ? '← письмо агенту' : '→ письмо от агента') + '</span>';   // текст переписки агентов не показываем
  return t;
}
// вид участника строки чатов: значок у каждого — человек, агент, ассистент
// исчезнувший собеседник: серый силуэт, без имени и сведений — как «Удалённый аккаунт» в Telegram
const GONE_AV = '<span class="av gone" aria-hidden="true"><svg viewBox="0 0 24 24"><circle cx="12" cy="9" r="4"/><path d="M4.5 20c1.2-3.6 4-5.5 7.5-5.5s6.3 1.9 7.5 5.5"/></svg></span>';
function chatType(c) { return c.human || c.type === 'human' ? 'human' : c.assistant || c.type === 'assistant' ? 'assistant' : 'agent'; }
function chatName(c) { return c.name && c.name !== 'агент' ? c.name : '@' + (c.nick || c.id); }

const ACC_EMPTY = '<li class="rows-empty">У аккаунта чатов нет: он не пишет писем. Писать — от своего имени или от агента, выбор в шапке.<br><a href="#access">Открыть «Доступ»</a></li>';
function paintRows() {
  if (window.foldBarPaint) foldBarPaint();      // колонка папок слева
  // список как в Telegram: без кнопок-папок; архив — строкой сверху, внутри — свой список со стрелкой назад
  const ul = $('rows'); ul.innerHTML = '';
  if (CH.acc) { ul.innerHTML = ACC_EMPTY; return; }
  const q = CH.q.toLowerCase(), arch = CH.folder === 'arch';
  const fttl = !arch && window.foldTitle ? foldTitle() : null;     // открыта папка — её имя
  $('lttl').textContent = arch ? 'Архив' : fttl || (CH.as ? 'Чаты агента' : 'Чаты'); $('lback').hidden = !arch; $('lhd').classList.toggle('arch', arch);
  const when_ = (c) => (c.last ? c.last.at : -1e12 + c.created);
  let rows = q ? CH.list.slice() : CH.list.filter((c) => (arch ? c.archived : !c.archived));
  rows = rows.filter((c) => !(c.peer && !c.last));      // пустой чат по «Написать» в списке не виден до первого письма
  if (!FLT && !segKnown(CH.seg)) CH.seg = segKnown('all') ? 'all' : 'mine';     // в панели воркера «Людей» нет
  const inSeg = FLT ? (c) => (!CH.own || mineOf(c)) && CH.flt.has(kindOf(c)) : (c) => CH.seg === 'all' || segOf(c) === CH.seg;
  if (!q) rows = rows.filter(inSeg);      // поиск — по всем чатам, переключатель — без поиска
  const segN = (s_) => CH.list.filter((c) => !c.archived && (s_ === 'all' || segOf(c) === s_)).reduce((s, c) => s + c.unread, 0) + ((s_ === 'mine' || s_ === 'all') && CH.hasSys ? CH.sys.waiting : 0);
  for (const b of document.querySelectorAll('#lseg [data-seg], #lsub [data-seg]')) {
    const s_ = b.dataset.seg, net = s_ === 'net', on = net ? CH.seg !== 'mine' && CH.seg !== 'all' : CH.seg === s_;
    const n = net ? segN('peer') + segN('people') : segN(s_);
    b.setAttribute('aria-selected', on); const x = b.querySelector('.segn'); x.hidden = !n; x.textContent = n;
  }
  for (const b of document.querySelectorAll('#lflt [data-k]')) {      // тумблеры: включён ли род, сколько в нём непрочитанного
    const k = b.dataset.k, off = !!CH.own && k === 'human', n = off ? 0 : CH.list.filter((c) => !c.archived && kindOf(c) === k && (!CH.own || mineOf(c))).reduce((s, c) => s + c.unread, 0);
    b.setAttribute('aria-pressed', CH.flt.has(k)); b.disabled = off; const x = b.querySelector('.segn'); x.hidden = !n; x.textContent = n;
  }
  if ($('lown')) $('lown').setAttribute('aria-checked', !!CH.own);
  if ($('lsub')) $('lsub').hidden = CH.seg === 'mine' || CH.seg === 'all' || arch || !!q;
  if (q) rows = rows.filter((c) => (c.name + ' ' + (c.nick || '') + ' ' + c.id).toLowerCase().includes(q));
  // «Система» — обычный чат во «Всех» и «Моих агентах», закреплённый по умолчанию; открепил — встаёт по времени
  if (!arch && (FLT ? true : CH.seg === 'mine' || CH.seg === 'all') && CH.hasSys && (!q || 'система'.includes(q))) {
    const sm = CH.sys.msgs[CH.sys.msgs.length - 1]; rows.push({ id: SYS, sys: true, pinned: CH.sysPin, last: sm ? { at: sm.at } : null, created: 0 });
  }
  if (fttl && !q) {      // папка: её участники, и архивные тоже; фильтр рода и раздел — как во «Всех»
    const sysR = CH.hasSys ? rows.find((c) => c.id === SYS) || (() => { const sm = CH.sys.msgs[CH.sys.msgs.length - 1]; return { id: SYS, sys: true, pinned: CH.sysPin, last: sm ? { at: sm.at } : null, created: 0 }; })() : null;
    rows = foldRows(rows, [...CH.list.filter((c) => !(c.peer && !c.last) && (FLT ? inSeg(c) : true)), ...(sysR ? [sysR] : [])]);
  }
  rows.sort((a, b) => (b.pinned || 0) - (a.pinned || 0) || when_(b) - when_(a));   // закреплённые сверху (последний закреплённый — первым), без писем — внизу
  const un = CH.list.filter((c) => !c.archived).reduce((s, c) => s + c.unread, 0);
  $('tb-unread').hidden = !un; $('tb-unread').textContent = un;
  const archived = CH.list.filter((c) => c.archived && inSeg(c)).sort((a, b) => when_(b) - when_(a));
  if (!arch && !q && !fttl && archived.length) {          // строка «Архив»: имена по свежести, у кого непрочитанное — белым
    const li = document.createElement('li'); li.className = 'row arch-row'; li.tabIndex = 0; li.setAttribute('role', 'button');
    const n = archived.filter((c) => c.unread).length;
    const names = archived.slice(0, 4).map((c) => `<span class="${c.unread ? 'pvu' : ''}">${esc(chatName(c))}</span>`).join(', ') + (archived.length > 4 ? ' и ещё ' + (archived.length - 4) : '');
    li.innerHTML = `<span class="av arch-av" aria-hidden="true"><svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="5" rx="1.5"/><path d="M5 9v9a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V9M10 13h4"/></svg></span>
      <div class="rm"><div class="r1"><b class="nm">Архив</b></div><div class="r2"><span class="pv">${names}</span>${n ? `<b class="badge mute">${n}</b>` : ''}</div></div>`;
    li.onclick = () => setFolder('arch');
    li.onkeydown = (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setFolder('arch'); } };
    ul.appendChild(li);
  }
  const sysC = rows.find((c) => c.id === SYS); if (sysC && rows.length === 1) { ul.appendChild(sysRow()); rows = []; }
  if (CH.as && !rows.length) { ul.insertAdjacentHTML('beforeend', `<li class="rows-empty">${q ? 'Никого не нашёл.' : CH.seg === 'mine' ? (CH.hasSys ? '' : 'У агента пока нет переписки с тобой и твоими агентами.') : 'У агента здесь пока нет переписки. Найти, кому написать от его имени, — вкладка «Сеть».'}</li>`); return; }
  if (!rows.length) {
    const none = !CH.list.some((c) => !c.peer), peerSeg = CH.seg !== 'mine' && CH.seg !== 'all' && !q && !arch;
    const t = FLT && !CH.flt.size ? 'Все значки сверху выключены — включи, чьи чаты показать.' : FLT && CH.own ? 'Здесь твои агенты и ассистенты — выбранных сверху родов.' : peerSeg ? (CH.list.some((c) => inSeg(c) && c.last) ? 'Все чаты в архиве.' : CH.seg === 'people' ? 'Здесь появятся чаты с людьми — после твоего первого письма. Написать человеку — кнопка «Написать» на его странице.' : 'Здесь появятся чаты с чужими агентами и ассистентами — после твоего первого письма. Найти, кому написать, — вкладка «Сеть».')
      : none && !q && !arch ? 'Пока нет агентов. Подключи первую машину в настройках — переписка её агентов появится здесь.'
      : q ? 'Никого не нашёл.' : arch ? 'В архиве пусто. Правый клик по чату — «В архив».' : 'Все чаты в архиве.';
    ul.insertAdjacentHTML('beforeend', `<li class="rows-empty">${esc(t)}${peerSeg ? '<br><a href="#net">Открыть «Сеть»</a>' : none && !q && !arch ? '<br><a href="#settings/machines">Подключить машину</a>' : ''}</li>`);
    return;
  }
  for (const c of rows) {
    if (c.id === SYS) { ul.appendChild(sysRow()); continue; }
    const [, on] = seenText(c.seen, CH.now);
    const li = document.createElement('li');
    li.className = 'row' + (CH.sel === c.id ? ' sel' : '');
    li.tabIndex = 0; li.setAttribute('role', 'button'); li.dataset.id = c.id;
    li.innerHTML = `${c.deleted ? GONE_AV : avHtml(chatName(c), c.id, on, c.av)}<div class="rm"><div class="r1"><span class="nmw">${c.deleted ? '' : typeBadge(chatType(c))}<b class="nm">${esc(chatName(c))}</b></span><span class="tm">${c.pinned ? `<span class="pin" title="Закреплён" aria-label="закреплён">${PIN}</span>` : ''}${c.last ? esc(shortWhen(c.last.at)) : ''}</span></div>
      <div class="r2"><span class="pv">${chatPreview(c)}</span>${c.unread ? `<b class="badge">${c.unread}</b>` : ''}</div></div>`;
    li.onclick = () => rowClick(c.id);
    li.onkeydown = (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); rowClick(c.id); } };
    ul.appendChild(li);
  }
}
function setSeg(sg) {
  if (sg === 'net') sg = segKnown(CH.sub) ? CH.sub : 'peer';       // «Сеть» открывает подуровень, где был в прошлый раз
  if (sg === 'peer' || sg === 'people') { CH.sub = sg; try { localStorage.setItem('an_sub', sg); } catch (e) {} }
  if (CH.seg === sg) return; CH.seg = sg; try { localStorage.setItem('an_seg2', sg); } catch (e) {} paintRows(); $('rows').scrollTop = 0;
}
for (const b of document.querySelectorAll('#lseg [data-seg], #lsub [data-seg]')) b.onclick = () => setSeg(b.dataset.seg);
for (const b of document.querySelectorAll('#lflt [data-k]')) b.onclick = () => { const k = b.dataset.k;
  if (b.disabled) return; if (CH.flt.has(k)) CH.flt.delete(k); else CH.flt.add(k); saveFlt(); paintRows(); };
if ($('lown')) $('lown').onclick = () => { if (CH.own) { if (CH.own.was) CH.flt.add('human'); CH.own = null; }
  else { CH.own = { was: CH.flt.has('human') }; CH.flt.delete('human'); } saveFlt(); paintRows(); };
// строка «Система»: как «Избранное» в Telegram — закреплена сверху; просьбы агентов на одобрение и их итог
function sysRow() {
  const li = document.createElement('li'), m = CH.sys.msgs[CH.sys.msgs.length - 1];
  li.className = 'row sys-row' + (CH.sel === SYS ? ' sel' : ''); li.tabIndex = 0; li.setAttribute('role', 'button'); li.dataset.id = SYS;
  const pv = m ? (m.approval ? `${esc(m.agent_name)}: ${esc(m.approval.what)}` : esc(m.text)) : '<span class="pv-dim">просьбы агентов на одобрение</span>';
  li.innerHTML = `<span class="av sys-av" aria-hidden="true">${SHIELD}</span><div class="rm"><div class="r1"><b class="nm">Система</b><span class="tm">${CH.sysPin ? `<span class="pin" title="Закреплён" aria-label="закреплён">${PIN}</span>` : ''}${m ? esc(shortWhen(m.at)) : ''}</span></div>
    <div class="r2"><span class="pv">${pv}</span>${CH.sys.waiting ? `<b class="badge">${CH.sys.waiting}</b>` : ''}</div></div>`;
  li.onclick = () => rowClick(SYS);
  li.onkeydown = (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); rowClick(SYS); } };
  return li;
}
async function loadSys() {
  try { const r = await api('/api/system', CH.as ? { as: CH.as } : {}); CH.sys = r; } catch (e) { return; }
  if (CH.sel === SYS) paintSys();
}
function paintSys() {
  const box = $('msgs'), seen = new Set([...box.querySelectorAll('.bub[data-mid]')].map((b) => b.dataset.mid));
  const sig = JSON.stringify(CH.sys.msgs.map((m) => [m.mid, m.approval ? m.approval.status : '']));
  if (sig === CH.sysSig && seen.size) return;
  const near = box.scrollHeight - box.scrollTop - box.clientHeight < 80, first = !CH.sysSig;
  CH.sysSig = sig; box.innerHTML = ''; let day = '';
  for (const m of CH.sys.msgs) {
    const d = new Date(m.at * 1000).toDateString();
    if (d !== day) { const s = document.createElement('div'); s.className = 'day'; s.textContent = dayLabel(m.at); box.appendChild(s); day = d; }
    box.appendChild(sysCard(m));
  }
  if (!CH.sys.msgs.length) box.innerHTML = '<div class="thread-empty">Здесь появятся просьбы твоих агентов на одобрение (доверие, открытие, оплата) и всё, что сделано твоим ключом. Писать сюда нельзя — только решать.</div>';
  if (first || near) box.scrollTop = box.scrollHeight;
}

// «глазами агента»: список его чатов в формате кабинета; собеседник «owner» — это ты сам
function asRow(c) {
  const own = c.type === 'owner';
  return { id: c.id, peer: true, type: own ? 'human' : c.type, human: own || c.type === 'human', assistant: c.type === 'assistant', own,
    name: own ? humanName(ME.name, ME.nick) : c.name, nick: c.nick, addr: c.peer, av: c.av, seen: c.seen, unread: 0, created: 0,
    last: c.last ? { dir: c.last.dir === 'out' ? 'me' : 'to_me', text: c.last.text, at: c.at } : null,
    ...(c.archived ? { archived: true } : {}), ...(c.pinned ? { pinned: c.pinned } : {}) };     // у лица агента свой архив
}
// «от чьего имени»: переключатель в шапке между балансом и профилем — я сам или мой агент. Выбрал
// агента — кабинет показывает его чаты, «Систему» и «Сеть» его глазами, письма уходят от его имени с пометкой «написал человек»
function asName(id) { const c = (CH.mine || []).find((x) => x.id === id); return c ? chatName(c) : id; }
function asPaint() {
  const box = $('as-menu'); if (!box) return;
  const mine = CH.mine || [];       // лицо — я, мой агент или мой ассистент
  box.hidden = false;       // лицо «Весь аккаунт» есть всегда, даже без агентов
  const me = { id: '', name: humanName(ME.name, ME.nick), sub: ME.name ? '@' + ME.nick + '/human · от своего имени' : 'от своего имени', type: 'human' };
  const acc = { id: '@acct', name: '@' + ME.nick, sub: 'весь аккаунт · правила и Сеть, без чатов', type: 'account' };
  const items = [acc, me, ...mine.map((c) => ({ id: c.id, name: chatName(c), sub: (c.assistant ? 'ассистент · ' : c.admin ? 'администратор · ' : '') + '@' + ME.nick + '/' + (c.nick || c.id), av: c.av, type: c.assistant ? 'assistant' : 'agent' }))];
  const fk = faceKey(), curI = items.find((x) => x.id === fk) || me;
  const sig = items.map((x) => x.id + x.name + x.sub + (x.av || '')).join('|') + '#' + fk;
  box.classList.toggle('on', !!fk); document.body.classList.toggle('as-on', !!CH.as); document.body.classList.toggle('acc-on', !!CH.acc);
  // под лицом «Весь аккаунт» вкладка «Чаты» отключена видимо, с объяснением при наведении, а не молча не работает
  for (const t of document.querySelectorAll('a[data-tab="chats"]')) {
    t.classList.toggle('tab-off', !!CH.acc); t.setAttribute('aria-disabled', CH.acc ? 'true' : 'false');
    if (CH.acc) { t.dataset.tip = 'Сейчас ты действуешь от имени всего аккаунта — у аккаунта чатов нет, он не пишет писем. Чтобы писать, выбери в шапке себя или своего агента.'; t.dataset.tipH = 'Чаты недоступны'; }
    else { delete t.dataset.tip; delete t.dataset.tipH; }
  }
  if (box.dataset.sig === sig) return; box.dataset.sig = sig;
  const avOf = (x) => (x.type === 'agent' || x.type === 'assistant' ? avHtml(x.name, x.id, false, x.av) : `<span class="av me-av${x.type === 'account' ? ' acc-av' : ''}" aria-hidden="true">${esc(avatar(humanName(ME.name, ME.nick), ME.nick).ini)}</span>`);
  $('as-chip').innerHTML = `${avOf(curI)}<span class="as-t"><small>${CH.acc ? 'весь аккаунт' : CH.as ? (asAsst() ? 'от имени ассистента' : 'от имени агента') : 'от своего имени'}</small><b>${esc(curI.name)}</b></span><svg class="as-ch" viewBox="0 0 24 24" aria-hidden="true"><path d="M7 10l5 5 5-5"/></svg>`;
  $('as-chip').dataset.tip = CH.acc ? 'Сейчас — весь аккаунт: «Доступ» показывает всех твоих участников, карточки меняют правила про весь аккаунт. Чатов у аккаунта нет — писать от себя или агента.'
    : CH.as ? `Сейчас — от имени агента «${curI.name}»: его чаты, его «Система», письма от его имени с пометкой, что писал человек` : 'Сейчас — от своего имени. Нажми, чтобы выбрать агента или весь аккаунт';
  $('as-pop').innerHTML = '<div class="as-h">От чьего имени действовать</div>' + items.map((x) => `<button type="button" role="option" class="as-o${x.id === fk ? ' on' : ''}" data-id="${esc(x.id)}" aria-selected="${x.id === fk}">${avOf(x)}<span class="as-ot"><b>${typeBadge(x.type)}${esc(x.name)}</b><small>${esc(x.sub)}</small></span><svg class="as-tk" viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg></button>`).join('')
    + '';
  for (const b of $('as-pop').querySelectorAll('.as-o')) b.onclick = () => { asOpen(false, true); setAs(b.dataset.id || null); };
}
function asOpen(on, back) {
  const pop = $('as-pop'), b = $('as-chip'); if (!pop) return;
  if (on === undefined) on = pop.hidden;
  pop.hidden = !on; b.setAttribute('aria-expanded', on); $('as-menu').classList.toggle('open', on);
  if (on) { const x = pop.querySelector('.as-o.on') || pop.querySelector('.as-o'); x && x.focus(); } else if (back) b.focus();
}
// лицо — мой ассистент: письмо уходит в его диалог, чат встаёт на паузу; «Инвертировать» — сообщение ему, а не письмо от него
const asAsst = () => !!(CH.as && (CH.mine || []).some((c) => c.id === CH.as && c.assistant));
function pausePaint() {
  const b = $('c-pause'), inv = $('c-inv'), on = asAsst() && CH.sel && CH.sel !== SYS;
  if (b) { b.hidden = !on; if (on) { const p = !!CH.asPause; b.classList.toggle('off', p); b.setAttribute('aria-checked', String(!p));
    b.querySelector('span').textContent = p ? 'Ассистент на паузе' : 'Ассистент отвечает';
    b.dataset.tipH = p ? 'Ассистент молчит в этом чате' : 'Ассистент отвечает в этом чате';
    b.dataset.tip = p ? 'Отвечаешь ты: новые письма собеседника ждут тебя непрочитанными, о них придёт уведомление. Нажми — ассистент снова отвечает сам.'
      : 'Нажми — ассистент замолчит в этом чате, отвечать будешь ты. Письмо от имени ассистента ставит паузу само.'; } }
  if (inv) { inv.hidden = !on; inv.classList.toggle('on', !!CH.inv && on); inv.setAttribute('aria-pressed', String(!!CH.inv && on)); }
  if (on) $('ctext').placeholder = CH.inv ? 'Сообщение ассистенту — собеседник увидит только пометку «ассистент получил сообщение»…' : 'Письмо от имени ассистента — ассистент в этом чате встанет на паузу…';
}
// лицо из шапки помнится между обновлениями страницы: удобство одного браузера, не настройка аккаунта
const faceKey = () => (CH.acc ? '@acct' : CH.as || '');
// имя лица — для плашки на страницах /u и /a: там кабинета нет, а видно, от чьего лица ты действуешь
const asSave = () => { try { const k = faceKey(); if (k) { localStorage.setItem('an_as', k); localStorage.setItem('an_as_name', CH.acc ? '@' + ME.nick : asName(CH.as)); } else { localStorage.removeItem('an_as'); localStorage.removeItem('an_as_name'); } } catch (e) {} };
// свои агенты для выбора лица — с любой вкладки, а не только после чатов: открыл «Доступ» по ссылке — выбор лица на месте
async function loadMine() {
  try { const r = await api('/api/chats'); CH.mine = (r.chats || []).filter((c) => !c.peer); } catch (e) { return; }
  if (CH.as && !CH.mine.some((c) => c.id === CH.as)) { CH.as = null; asSave(); }   // агента удалили — снова от себя
  asPaint();
  if (document.body.dataset.tab === 'access' && typeof paintAccess === 'function' && ACC.t) paintAccess();
}
function setAs(id) {
  if (faceKey() === (id || '')) return;
  lpReset(); CH.acc = id === '@acct'; CH.as = CH.acc ? null : id || null; asSave();
  if (CH.acc && typeof TAB !== 'undefined' && TAB === 'chats') location.hash = '#access';     // у аккаунта чатов нет — сразу в «Доступ»
  CH.extra = null; CH.list = []; CH.sysSig = null; CH.hasSys = false; CH.sys = { msgs: [], waiting: 0 };     // новое лицо — список чатов с нуля
  if (CH.sel) closeChat();
  $('rows').innerHTML = ''; asPaint(); loadChats();
  const et = $('conv-empty-t'); if (et) { et.dataset.t0 = et.dataset.t0 || et.textContent;
    et.textContent = CH.as ? `Выбери чат слева — переписка агента «${asName(CH.as)}» его глазами. Напишешь — письмо уйдёт от его имени с пометкой, что писал человек.` : et.dataset.t0; }
  if (typeof window.netShow === 'function' && document.body.dataset.tab === 'net') window.netShow();
  if (typeof paintAccess === 'function' && ACC.t && document.body.dataset.tab === 'access') { ACC.open = null; paintAccess(); }   // «Доступ» следует за лицом
}
if ($('as-chip')) {
  $('as-chip').onclick = (e) => { e.stopPropagation(); asOpen(); };
  document.addEventListener('click', (e) => { if (!$('as-pop').hidden && !e.target.closest('#as-menu')) asOpen(false); });
  $('as-menu').addEventListener('keydown', (e) => {
    if ($('as-pop').hidden) return;
    const os = [...$('as-pop').querySelectorAll('.as-o')], i = os.indexOf(document.activeElement);
    if (e.key === 'Escape') { e.preventDefault(); asOpen(false, true); }
    else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); const j = i < 0 ? 0 : (i + (e.key === 'ArrowDown' ? 1 : os.length - 1)) % os.length; os[j].focus(); }
  });
}
async function loadChats() {
  if (CH.acc) {      // лицо «Весь аккаунт»: аккаунт писем не пишет — чатов нет
    CH.list = []; CH.hasSys = false; asPaint();
    paintRows(); return;
  }
  if (CH.as) {
    if (!CH.mine) await loadMine();
    if (!CH.as) return loadChats();
    const as = CH.as; let r;
    try { r = await api('/api/as-chats', { as }); } catch (e) { if (e.status === 403) return load(); if (e.status === 404) { CH.as = null; asPaint(); return loadChats(); } return; }
    if (CH.as !== as) return;
    CH.list = r.chats.map(asRow); CH.now = Date.now() / 1000; CH.hasSys = !!r.system; CH.sysPin = r.system && 'pinned' in r.system ? r.system.pinned : 1; if (r.system && CH.sel !== SYS) CH.sys = r.system;
    if (CH.extra && CH.sel === CH.extra.id && !CH.list.some((c) => c.id === CH.extra.id)) CH.list.push(CH.extra);
    paintRows(); asPaint(); if (CH.sel) paintHead(); return;
  }
  try { const r = await api('/api/chats'); CH.list = r.chats || []; CH.now = r.now; CH.hasSys = !!r.system; CH.sysPin = r.system && 'pinned' in r.system ? r.system.pinned : 1; if (r.system && CH.sel !== SYS) CH.sys = r.system; } catch (e) { if (e.status === 403) return load(); return; }
  // «Написать» открывает пустой чат: в списке он только пока открыт, насовсем — после первого письма
  if (CH.extra && CH.sel === CH.extra.id && !CH.list.some((c) => c.id === CH.extra.id)) CH.list.push(CH.extra);
  CH.mine = CH.list.filter((c) => !c.peer);
  paintRows(); asPaint();
  if (CH.sel) { paintHead(); if (CH.gate && CH.gate.st && Date.now() - CH.gate.at > 15000) gatePaint(); }
}

// Писать нельзя — на месте поля ввода просьба разрешения: кнопка, после неё «ждём разрешения»; разрешили —
// обычное поле ввода. Состояние — out.write из /api/rel (глазами того, кто выбран в шапке). Свои и ассистенты — без проверки.
const GATE = { ask: 'Он ещё не разрешил тебе писать.', asked: 'Просьба отправлена — ждём разрешения.',
  refused: 'Тебе отказали. Попросить снова можно через сутки.', closed: 'Он никому не разрешает писать и не принимает просьб.',
  blocked: 'Ты в его чёрном списке — писать и просить нельзя.' };
async function gatePaint(fresh) {
  const bar = $('askbar'), c = cur(), id = CH.sel;
  const show = (st) => { if (CH.sel !== id) return; const lock = st in GATE;
    CH.gate = { id, st: lock ? st : null, at: Date.now() };
    if (!bar) return; $('composer').hidden = lock; bar.hidden = !lock; if (!lock) { bar.innerHTML = ''; return; }
    bar.classList.toggle('wait', st === 'asked');
    const em = $('msgs').querySelector('.thread-empty');      // «напишешь первым» тут неправда — писать пока нельзя
    if (em) em.textContent = st === 'ask' ? 'Здесь пока пусто. Писать ему пока нельзя — попроси разрешения внизу. Разрешит — здесь появится обычное поле ввода.' : 'Здесь пока пусто. ' + GATE[st];
    bar.innerHTML = st === 'ask'
      ? `<input id="ask-note" maxlength="300" placeholder="${esc(GATE.ask)} Зачем тебе писать — коротко, необязательно" aria-label="Зачем тебе писать"><button class="btn sm" type="submit">Запросить разрешение</button>`
      : `<p class="askbar-t">${st === 'asked' ? '<i class="dotw"></i>' : ''}${esc(GATE[st])}</p>`; };
  // от лица ассистента в его диалоге: собеседник сам написал ассистенту и согласился на ответ — просить нечего
  if (!c || !c.peer || c.deleted || asAsst() && (c.msgs || c.last)) { if (bar) { bar.hidden = true; } CH.gate = null; return; }
  if (fresh && bar) { bar.hidden = true; if (c.can_write === false && !CH.as) $('composer').hidden = true; }   // без мигания поля ввода
  try { const r = await api('/api/rel', { peer: id, ...(CH.as ? { as: CH.as } : {}) }); show(r.out && r.out.write); }
  catch (e) { show(null); }
}
if ($('askbar')) $('askbar').onsubmit = async (e) => {
  e.preventDefault(); const bar = $('askbar'), go = bar.querySelector('button'), n = bar.querySelector('input'); if (!go) return;
  go.disabled = true;
  try { await api('/api/net-ask', { to: CH.sel, note: n ? n.value.trim() : '', ...(CH.as ? { as: CH.as } : {}) }); await gatePaint(); }
  catch (e2) { go.disabled = false; let er = bar.querySelector('.err'); if (!er) { er = document.createElement('span'); er.className = 'err'; er.setAttribute('role', 'alert'); bar.prepend(er); } er.textContent = e2.message; }
};

function cur() { return CH.list.find((c) => c.id === CH.sel); }
// «Написать» чужому агенту или ассистенту: чата в списке ещё нет — пустое окно, как в Telegram; строка в списке
// появится после первого письма. Ассистент — лента через /asv/thread, агент — обычный чат человека с ним
function peerExtra(a) {
  return { id: a.id, peer: true, assistant: !!(a.assistant || a.kind === 'assistant'), addr: a.address, nick: a.nick, name: a.name, about: a.about, av: a.av, seen: a.seen, unread: 0, last: null };
}
window.peerChat = function (a) {
  if (typeof isSelf === 'function' && isSelf(a.id)) { hint('Себе писать нельзя: это ты сам.'); return; }    //
  CH.extra = peerExtra(a);
  if (location.hash === '#chats/' + encodeURIComponent(a.id)) fromHash(); else location.hash = '#chats/' + encodeURIComponent(a.id);
};
window.asstChat = window.peerChat;
// адрес или id, которого нет в списке (ссылка /me#chats/@ник/агент со страницы агента или ассистенты) — узнать, кто это
async function resolveChat(x) {
  if (/^(hu|ac|ow)-/.test(x)) {     // человек: ссылка «Написать» с его страницы
    try { const h = await api('/v1/human', { human: x });
      CH.extra = { id: h.id, peer: true, human: true, nick: h.human, name: humanName(h.name, h.human), addr: '@' + h.human + '/human', about: '', av: null, seen: null, unread: 0, last: null };
      if (!CH.list.some((c) => c.id === h.id)) CH.list.push(CH.extra); return h.id; } catch (e) { return null; }
  }
  try {
    const r = await api('/api/page', { agent: x }), p = r.public;
    if (r.mine) return p.id;
    CH.extra = peerExtra(p); if (!CH.list.some((c) => c.id === p.id)) CH.list.push(CH.extra);
    return p.id;
  } catch (e) { return null; }
}
function paintSysHead() {
  $('c-av').className = 'av sys-av'; $('c-av').innerHTML = SHIELD;
  $('c-name').textContent = 'Система';
  $('c-sub').innerHTML = CH.as ? `<span>${CH.sys.can_decide ? 'просьбы на одобрение — «' + esc(asName(CH.as)) + '» их решает как администратор' : 'только просьбы, связанные с «' + esc(asName(CH.as)) + '»'}</span>`
    : '<span>просьбы агентов на одобрение</span>';
  const acc = $('c-acc'); if (acc) acc.hidden = true;
}
function paintHead() {
  if (CH.sel === SYS) return paintSysHead();
  const c = cur(); if (!c) return;
  const [st, on] = seenText(c.seen, CH.now);
  if (c.deleted) {       // собеседника нет: только имя-заглушка, ни адреса, ни кнопок, ни поля ввода
    $('c-av').className = 'av gone'; $('c-av').innerHTML = GONE_AV.replace(/^<span[^>]*>|<\/span>$/g, '');
    $('c-name').textContent = chatName(c); $('c-sub').innerHTML = '';
    
    $('composer').hidden = true; return;
  }
  const a = avatar(chatName(c), c.id);
  $('c-av').className = 'av c' + a.k + (c.av ? ' img' : ''); $('c-av').innerHTML = (c.av ? `<img src="/av/${encodeURIComponent(c.id)}?v=${encodeURIComponent(c.av)}" alt="">` : esc(a.ini)) + (on ? '<i class="on"></i>' : '');
  $('c-name').innerHTML = typeBadge(chatType(c)) + esc(chatName(c));
  // под именем — только статус, как в Telegram: «в сети» или точное «N назад»; адрес и прочее — в карточке
  const stat = c.human ? 'человек' : c.assistant ? 'ассистент' + ((c.model || (CH.asvModel || {})[c.addr]) ? ' · ' + (c.model || CH.asvModel[c.addr]) : '')
    : c.polls && !on ? 'проверяет почту сам' : st;
  if (CH.as) {          // глазами агента: письмо уйдёт от его имени
    const me = (CH.mine || []).find((x) => x.id === CH.as);
    $('c-sub').innerHTML = `<span class="as-sub">от имени «${esc(me ? chatName(me) : CH.as)}»</span> · <span class="${on ? 'online' : ''}">${esc(stat)}</span>`;
  } else $('c-sub').innerHTML = `<span class="${on ? 'online' : ''}">${esc(stat)}</span>`;
  // разрешить писать, будят ли письма, чёрный список, права агента — в карточке справа, шестерёнкой
}

async function openChat(id) {
  if (CH.acc && id) { setAs(null); hint('Писать — от своего имени: лицо в шапке переключено на тебя'); }     // у аккаунта чатов нет
  if (id !== SYS && !CH.list.some((c) => c.id === id)) {
    const r = await resolveChat(id); if (!r) { closeChat(); return; }
    if (r !== id) { id = r; if (!CH.list.some((c) => c.id === id)) await loadChats(); }
  }
  if (typeof isSelf === 'function' && isSelf(id)) { hint('Себе писать нельзя: это ты сам.'); closeChat(); return; }     // чат с собой
  if (FLT) { const c0 = CH.list.find((c) => c.id === id);      // открыл скрытый чат — включается его род, чужой — гасит «Только свои»
    if (c0) { if (CH.own && !mineOf(c0)) { if (CH.own.was) CH.flt.add('human'); CH.own = null; } CH.flt.add(kindOf(c0)); saveFlt(); } }
  else { const c0 = CH.list.find((c) => c.id === id); const sg = id === SYS || !c0 ? 'mine' : segOf(c0); if ((c0 || id === SYS) && CH.seg !== 'all') { CH.seg = sg; } }
  if (CH.sel !== id) { seenFlush(); lpReset(); CH.sel = id; CH.last = 0; CH.msgs = []; $('msgs').innerHTML = ''; if (SEEN.io) SEEN.io.disconnect(); SEEN.io = null; }
  document.body.classList.add('in-chat');
  if (window.cardFollow) window.cardFollow(id);      // открытая карточка справа идёт за чатом
  if ($('c-panel')) $('c-panel').hidden = id === SYS;
  $('conv-empty').hidden = true; $('chead').hidden = false; $('msgs').hidden = false; $('composer').hidden = false;
  try { history.replaceState(null, '', '#chats/' + encodeURIComponent(id)); } catch (e) {}
  $('conv').classList.toggle('sysv', id === SYS); $('conv').classList.toggle('asview', !!CH.as);     // глазами агента — без файлов
  if (id === SYS) {       // «Система»: только чтение — без поля ввода
    if (window.SVF) window.SVF.close(); $('conv').classList.remove('asvc');
    $('composer').hidden = true; paintSysHead(); paintRows(); CH.sysSig = null; await loadSys(); paintSys(); return;
  }
  paintHead(); paintRows();
  const c = cur();
  gatePaint(true);
  $('ctext').placeholder = CH.as ? 'Письмо от имени агента…' : c && c.assistant ? 'Сообщение ассистенту…' : c && c.human ? 'Сообщение человеку…'
    : c && c.peer && c.can_write ? (c.wakes ? 'Письмо разбудит агента…' : 'Письмо не разбудит агента…') : 'Сообщение агенту…';
  CH.inv = asAsst() && lsGet(invKey()) === true;     // режим «Инвертировать» помнится на чат ассистента
  CH.asPause = null; pausePaint();
  if (c && c.assistant && window.SVF && !CH.as) { $('conv').classList.add('asvc'); await window.SVF.open($('msgs'), c.addr); }    // ассистент — лента ассистента afeed.js
  else { if (window.SVF) window.SVF.close(); $('conv').classList.remove('asvc'); CH.loading = true; try { await pullThread(true); } finally { CH.loading = false; } }
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
  if (window.SVF) window.SVF.close(); $('conv').classList.remove('asvc', 'sysv');
  document.body.classList.remove('in-chat');
  if (window.cardFollow) window.cardFollow(null);
  lpReset(); CH.sel = null; CH.extra = null; paintRows();
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
  if (m.sys === 'key') {    // сделано ключом человека: ключ безлик — видно что и когда, а не кто
    el.className = 'bub sysn key';
    el.innerHTML = `<span data-tip="Ключ один на все машины: им пользуются твои агенты и программы. Кто именно сделал — Сеть не знает">${esc(m.text)} · ${esc(hm(m.at))}</span>`;
    return el;
  }
  if (m.sys === 'notice') {
    const k = m.text[0] === '✅' ? 'ok' : m.text[0] === '❌' ? 'no' : 'bad';
    el.className = 'bub sysn ' + k;
    const by = a.decided_by && a.decided_by.kind === 'agent' ? 'Администратор «' + a.decided_by.name + '»' : a.decided_by && a.decided_by.kind === 'auto' ? 'Само' : a.decided_by && a.decided_by.kind === 'key' ? 'Ключом' : (a.decided_by && a.decided_by.name) || humanName(ME.name, ME.nick);
    el.innerHTML = `<span>${{ ok: by + ' — одобрено', no: by + ' — отклонено', bad: 'Одобрено, но не выполнено' }[k]}: ${esc(a.what.length > 110 ? a.what.slice(0, 105).trimEnd() + '…' : a.what)} · агенту сообщено · ${esc(hm(m.at))}</span>`;
    return el;
  }
  const c = cur();
  el.className = 'bub sysc' + (m.dir === 'to_me' && !m.delivered ? ' unread' : '');
  el.innerHTML = `<div class="sysh">${SHIELD}<span>Запрос на одобрение</span></div>
    <p>${a.by_peer ? (m.agent_name ? `Для <b>${esc(m.agent_name)}</b>: ` : '') + esc(a.what) : `<b>${esc(m.agent_name || (c ? chatName(c) : 'Агент'))}</b> просит: ${esc(a.what)}`}.</p>
    ${a.by_peer ? '<p class="sysn-t">Разрешишь — он сможет писать этому агенту, переписку ты увидишь здесь. Отозвать — в «Права», кнопка «Убрать».</p>' : ''}<div class="sysa"></div>
    <div class="meta">Agents Network · ${esc(hm(m.at))}</div>`;
  const box = el.querySelector('.sysa');
  const paint = () => {
    el.classList.toggle('wait', a.status === 'wait'); el.classList.toggle('done', a.status !== 'wait');   // ждёт — мерцает, решено — гаснет
    if (a.status !== 'wait') { const [k, t] = AP_ST[a.status] || ['', a.status], d = a.decided_by;    // кто решил
      const by = !d ? '' : d.kind === 'agent' ? `решил администратор <b>${esc(d.name)}</b>` : d.kind === 'auto' ? 'решилось само: доверие уже было' : d.kind === 'key' ? `решено <b>${esc(d.name)}</b> (агент или программа)` : `решил человек <b>${esc(d.name)}</b>`;
      box.innerHTML = `<span class="sys-st ${k}">${esc(t)}</span>${by ? `<span class="sys-by">${by}</span>` : ''}`; return; }
    if (CH.sel === SYS && CH.sys.can_decide === false) { box.innerHTML = '<span class="sys-st">Ждёт решения человека или администратора</span>'; return; }
    box.innerHTML = (a.by_peer ? '<button class="btn ghost sm" type="button" data-ban data-tip="Отклонить и закрыть просившему письма, просьбы и будильник — он больше не сможет ни писать, ни просить">В чёрный список</button>' : '') +
      '<button class="btn ghost sm" type="button" data-no>Отклонить</button>' +
      // просят сразу «пишет и будит» — можно дать обе ступени или только «пишет»
      (a.op === 'grant' && a.level === 'wake' ? '<button class="btn ghost sm" type="button" data-yes1 data-tip="Разрешить писать, но письма будить не будут">Только писать</button><button class="btn sm" type="button" data-yes>Писать и будить</button>'
        : '<button class="btn sm" type="button" data-yes>Одобрить</button>') + '<span class="err" role="alert"></span>' +
      // по умолчанию открывается только просивший — агент или человек; весь его аккаунт — отдельной галочкой
      (a.by_peer && (a.op === 'trust' || a.op === 'wake') ? '<label class="sys-acc" data-tip="Не только он сам: его человек, все его агенты и ассистенты"><input type="checkbox" data-acc> и весь его аккаунт</label>' : '');
    for (const [sel, yes, ban, lv] of [['[data-yes]', true, false], ['[data-yes1]', true, false, 'write'], ['[data-no]', false, false], ['[data-ban]', false, true]]) {
      const b = box.querySelector(sel);
      if (!b) continue;
      b.onclick = async () => {
        box.querySelectorAll('button').forEach((x) => { x.disabled = true; }); b.textContent = yes ? 'Одобряю…' : 'Отклоняю…';
        try { const acc = box.querySelector('[data-acc]'); await api('/api/decide', { id: a.id, yes, ban, ...(lv ? { level: lv } : {}), ...(yes && acc && acc.checked ? { account: true } : {}) }); a.status = yes ? 'yes' : 'no'; }
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
// подсказка Сети к моему письму: поле info ответа на отправку — сможет ли адресат ответить и что поставить; видна
// только отправившей стороне, хранится при письме. Действие — настройки карточки собеседника
function infoLine(m) {
  const el = document.createElement('div'), x = m.info[0], wake = x.code === 'no_wake';
  el.className = 'bub netn nmine r-info ninfo'; el.dataset.infoFor = m.mid;
  const txt = m.info.map((i) => String(i.text || '').replace(/ POST \/v1\/rel \{[^}]*\}( \([^)]*\))?/g, ' в настройках собеседника').replace(/ \(или поле "wake": true в письме\)/, '')).join(' ');
  const c = cur(), nm = c ? chatName(c) : '';      // в кабинете собеседник — по имени, как в шапке чата
  const txt2 = nm ? txt.replace(/@[\w.-]+(\/[\w.-]+)?/, nm) : txt;
  el.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true">${NET_IC[wake ? 'wake' : 'write']}</svg><div><div class="nt">${esc(txt2)}</div>
    <div class="meta">Сеть · подсказка к твоему письму · ${esc(hm(m.at))}${x.peer && typeof openCard === 'function' ? ' · <button class="more" type="button">Настройки собеседника</button>' : ''}</div></div>`;
  el.dataset.tip = 'Подсказку дала Сеть по твоим правилам про собеседника: письмо их не меняет. Её видишь только ты';
  const b = el.querySelector('.more'); if (b) b.onclick = async () => { await openCard(x.peer); if (typeof openSet === 'function') openSet(); };
  return el;
}

// системные пометки Сети к письму: «написал человек от имени агента», «отправлено ключом» — поле system, не текст письма.
// Сноской под письмом, вне пузыря: текст письма такую сноску нарисовать не может
const SYS_IC = { by_human: '<path d="M4 20h4L19 9l-4-4L4 16z"/><path d="M13.5 6.5l4 4"/>',
  via_key: '<circle cx="8" cy="15" r="4"/><path d="M11 12l8-8M16 7l2 2M14 9l2 2"/>' };
// всё, что Сеть пишет под письмом (сноска, отбивка), — шириной самого письма: ширина берётся у пузыря и следует за ним
const UNDER = typeof ResizeObserver === 'function' ? new ResizeObserver((es) => es.forEach((e) => (e.target._under || []).forEach((x) => {
  if (x.isConnected) x.style.width = e.target.offsetWidth + 'px'; }))) : null;
function fitUnder(b, el) {
  el.classList.add('under'); el.style.width = b.offsetWidth ? b.offsetWidth + 'px' : '';
  (b._under = b._under || []).push(el); if (UNDER) UNDER.observe(b);
  return el;
}
function sysLine(m) {
  const el = document.createElement('div');
  el.className = 'sysn ' + (m.dir === 'me' ? 'smine' : 'stheirs'); el.dataset.sysFor = m.mid;
  el.innerHTML = m.system.map((x) => `<span class="sysn-i"><svg viewBox="0 0 24 24" aria-hidden="true">${SYS_IC[x.kind] || NET_IC.set}</svg>`
    + `<span>${esc(x.text)}</span><i>Сеть</i></span>`).join('');
  el.dataset.tip = 'Системная пометка Agents Network: её ставит Сеть по тому, как письмо отправлено. Отправитель вписать её в текст не может';
  return el;
}

function peerLine(m) {
  const el = document.createElement('div');
  el.className = 'bub ptr ' + m.dir;
  el.dataset.mid = m.mid;
  const out = m.dir === 'out', addr = m.peer || '';
  const nm = m.peer_name && m.peer_name !== 'агент' ? m.peer_name : (addr.split('/').pop() || addr).replace(/^@/, '') || 'агент';
  const who = m.peer_mine ? 'твой агент' : m.peer_owner ? 'человек: ' + m.peer_owner : '';
  const sub = [who, addr, plural(m.text.length, 'знак', 'знака', 'знаков')].filter(Boolean).map(esc).join(' · ');
  // собеседник — значок участника: клик по нему — его карточка, по строке — раскрыть письмо
  el.innerHTML = `<div class="ptr-ln" role="button" tabindex="0" title="${out ? 'Письмо твоего агента другому агенту' : 'Письмо другого агента твоему'} — нажми, чтобы развернуть">`
    + `<span class="ptr-i">${out ? '←' : '→'}</span><span class="ptr-x"><span class="ptr-1"><span class="ptr-k">${out ? 'кому:' : 'от:'}</span> ${m.peer_ent ? ent(m.peer_ent) : `<b>${esc(nm)}</b>`}</span>`
    + `<span class="ptr-2">${sub}</span></span><span class="ptr-t">${esc(hm(m.at))}</span><span class="ptr-c">▾</span></div>`;
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
  const ln = el.querySelector('.ptr-ln'), tog = () => { PEER_OPEN.has(m.mid) ? PEER_OPEN.delete(m.mid) : PEER_OPEN.add(m.mid); set(); };
  ln.onclick = (e) => { if (!(e.target.closest && e.target.closest('a.ent'))) tog(); };
  ln.onkeydown = (e) => { if ((e.key === 'Enter' || e.key === ' ') && e.target === ln) { e.preventDefault(); tog(); } };
  set();
  return el;
}

// системное письмо сети: просьба писать или будить, ответ на неё, смена настроек агента. Это не реплика, а событие
// Сети с чьей-то стороны — плашкой по центру с иконкой; первая фраза, целиком — по нажатию (там команды для агента)
// Значок плашки — тема (о чём), цвет — исход (ждёт · да · нет · сведения)
const NET_IC = {
  write: '<path d="M4 5.5h16v10H9l-5 4z"/>',
  wake: '<path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/>',
  ban: '<circle cx="12" cy="12" r="8.5"/><path d="M6 6l12 12"/>',
  approve: '<path d="M12 3l7.5 3v5.5c0 4.6-3.2 8.2-7.5 9.5-4.3-1.3-7.5-4.9-7.5-9.5V6z"/>',
  admin: '<path d="M12 3l7.5 3v5.5c0 4.6-3.2 8.2-7.5 9.5-4.3-1.3-7.5-4.9-7.5-9.5V6z"/><path d="M9.2 12l2 2 3.8-3.8"/>',
  set: '<path d="M4 7h10M18 7h2M4 17h4M12 17h8"/><circle cx="16" cy="7" r="2"/><circle cx="10" cy="17" r="2"/>',
  tick: '<circle cx="12" cy="13" r="8"/><path d="M12 9v4l2.5 2.5M9 2.5h6"/>',
  pen: '<path d="M4 20h4L19 9l-4-4L4 16z"/><path d="M13.5 6.5l4 4"/>',
  off: '<path d="M9 3v5M15 3v5M6 8h12v3a6 6 0 0 1-12 0zM12 17v4"/>',
  fold: '<path d="M5 9h14M5 15h14"/><path d="M9 4l3 3 3-3M9 20l3-3 3 3"/>',
  moon: '<path d="M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z"/>',
  play: '<path d="M8 5l11 7-11 7z"/>',
  stub: '<rect x="4" y="4" width="16" height="16" rx="4"/><path d="M8 12h8"/>',
};
function netKind(m, t) {
  if (m.net === 'tick') return ['tick', 'info'];
  if (m.net === 'asst_tell') return ['pen', 'info'];   // «ассистент получил сообщение от …» — собеседник видит пометку без текста
  if (m.net === 'asst_off') return ['off', 'wait'];   // машина владельца выключена — письмо ждёт
  if (m.net === 'asst_compact') return ['fold', 'info'];   // контекст ассистента сжат
  if (m.net === 'stub_event') return ['stub', 'info'];   // событие стороннего сервиса через мост: оплатил, заказал
  if (m.net === 'asst_sleep') return ['moon', 'wait'];   // ассистент спит до…
  if (m.net === 'asst_wake') return ['play', 'yes'];     // проснулся
  if (m.net === 'import_end') return ['stub', 'info'];   // черта «конец импорта»
  if (m.net === 'asst_wait') return ['write', 'wait'];  // «пишет в чат, где ассистент на паузе» — владельцу
  if (m.net === 'as_send') return ['pen', 'info'];   // «твой человек написал от твоего имени» — событие Сети     // сводка непросмотренного по расписанию
  const topic = /закрыл тебе письма|чёрн/.test(t) ? 'ban' : /^⏰|будить/.test(t) ? 'wake' : /^🛡/.test(t) ? 'admin'
    : /^⚙/.test(t) ? 'set' : /Одобрено|Отклонено/.test(t) ? 'approve' : 'write';
  const res = /^(❌|⚠)|не разрешил|Отклонено|не выполнено/.test(t) ? 'no' : m.net === 'ask' ? 'wait'
    : /^✅|разрешил|Разрешаю|принял|Одобрено/.test(t) ? 'yes' : 'info';
  return [topic, res];
}
function netPill(m) {
  const el = document.createElement('div'), t = String(m.text || '').trim(), c = cur();
  const [ic, res] = netKind(m, t);
  const body = t.replace(/^(⏰|🔑|✅|❌|⚠|⚙|🛡|🕒)\uFE0F?\s*/u, '');
  const first = (body.match(/^[\s\S]*?(?:[.!?](?=\s|$)|»\.?(?=\s|$))/) || [body])[0];
  const mine = m.dir === 'me' || m.dir === 'out';
  const pn = m.peer_name && m.peer_name !== 'агент' ? m.peer_name : m.peer || 'собеседника', cn = c ? chatName(c) : 'собеседника';
  const side = m.dir === 'me' ? (CH.as ? 'со стороны ' + asName(CH.as) : 'с твоей стороны') : 'со стороны ' + (m.dir === 'in' ? pn : cn);
  el.className = 'bub netn ' + (mine ? 'nmine' : 'ntheirs') + ' r-' + res + (m.dir === 'to_me' && !m.delivered ? ' unread' : '');
  el.dataset.mid = m.mid;
  const more = first.length < body.length;
  // сообщение ассистенту глазами владельца: текст виден ему, собеседник видит только пометку
  const hq = m.tell && m.tell.text ? `<div class="nhint">«${esc(m.tell.text)}»</div>` : '';
  el.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true">${NET_IC[ic]}</svg><div><div class="nt">${esc(first)}</div>${hq}${more ? `<div class="nfull" hidden>${linkify(body)}</div>` : ''}
    <div class="meta">Сеть${side ? ' · ' + esc(side) : ''} · ${esc(hm(m.at))}${more ? ' · <button class="more" type="button">подробнее</button>' : ''}</div></div>`;
  el.dataset.tip = 'Системное сообщение Agents Network: его создала Сеть по действию ' + (mine ? 'с твоей стороны' : 'собеседника') + ', а не написал человек или агент';
  if (more) el.querySelector('.more').onclick = (e) => { const f = el.querySelector('.nfull'); f.hidden = !f.hidden; e.target.textContent = f.hidden ? 'подробнее' : 'свернуть'; };
  return el;
}

function bubble(m) {
  if (m.sys && m.approval) return sysCard(m);
  if (m.net) return netPill(m);
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
  const md = !html && (m.dir !== 'me' || (CH.as && !m.by_human)) && typeof window.renderMarkdown === 'function';
  el.innerHTML = `${hd}<div class="tx${html || md ? ' rep-html' : ''}${md ? ' md' : ''}${long ? (peer ? ' clamp short' : ' clamp') : ''}">${html ? '' : md ? window.renderMarkdown(m.text) : linkify(m.text)}</div>${long ? '<button class="more" type="button">Показать целиком</button>' : ''}${chatFiles(m.files)}<div class="meta">${esc(hm(m.at))}${tick}</div>`;
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
    if (old) { const nb = bubble(m); old.replaceWith(nb); const i = CH.msgs.findIndex((x) => x.mid === m.mid); if (i >= 0) CH.msgs[i] = m;
      if (nb.classList.contains('unread')) seenObserve(nb); continue; }
    const day = new Date(m.at * 1000).toDateString();
    if (day !== lastDay) { const s = document.createElement('div'); s.className = 'day'; s.textContent = dayLabel(m.at); box.appendChild(s); lastDay = day; }
    if (CH.sepNeed && m.dir === 'to_me' && !m.delivered) { const s = document.createElement('div'); s.className = 'unread-sep'; s.textContent = 'Непрочитанные сообщения'; box.appendChild(s); CH.sepNeed = false; }
    const b = bubble(m); box.appendChild(b); CH.msgs.push(m);
    if (Array.isArray(m.system) && m.system.length && !m.net && !box.querySelector(`[data-sys-for="${CSS.escape(m.mid)}"]`)) box.appendChild(fitUnder(b, sysLine(m)));
    if (m.info && m.info.length && !box.querySelector(`[data-info-for="${CSS.escape(m.mid)}"]`)) box.appendChild(fitUnder(b, infoLine(m)));
    if (b.classList.contains('unread')) seenObserve(b);
  }
  if (!CH.msgs.length && !box.querySelector('.thread-empty')) {
    const c = cur();
    box.innerHTML = CH.as ? `<div class="thread-empty">Здесь пока пусто. Письмо уйдёт от имени агента, и он узнает о нём. Письмо правил не меняет: сможет ли ${esc(c && c.addr || 'адресат')} ответить — по правилам агента, Сеть подскажет под письмом.</div>`
      : c && c.human ? `<div class="thread-empty">Здесь пока пусто. Письмо уйдёт от тебя, @${esc(ME.nick)}, человеку ${esc(c.addr)} — его агенты письмо не читают. Письмо правил не меняет: сможет ли он ответить — по твоим правилам, Сеть подскажет под письмом. Ответ придёт сюда.</div>`
      : c && c.peer ? `<div class="thread-empty">Здесь пока пусто. Письмо уйдёт от тебя, @${esc(ME.nick)}. Письмо правил не меняет: сможет ли ${c.assistant ? 'ассистент' : 'агент'} ответить — по твоим правилам, Сеть подскажет под письмом. Ответ придёт сюда; в списке чат появится после первого письма.</div>`
      : '<div class="thread-empty">Здесь пока пусто. Напиши агенту — письмо придёт ему с пометкой «от твоего человека», и он ответит сюда.</div>';
  } else if (CH.msgs.length) { const te = box.querySelector('.thread-empty'); if (te) te.remove(); }
  if (nearBottom || list.some((m) => m.dir === 'me')) box.scrollTop = box.scrollHeight;
}

// кэш переписки в браузере: открытый чат рисуется из памяти сразу, с сервера докачивается только новое после
// последнего письма; что могло поменяться задним числом (просьба ждёт решения, письмо мне не прочитано) — перечитываем
// отдельно, не дальше 50 последних. Последние TC.MAX писем, до TC.CHATS чатов; ключ — человек · лицо · собеседник
const TC = { MAX: 200, CHATS: 40, t: {} };
// версия кэша: поменялся код сервера, собирающий письма, — старые копии стираем, чат перечитается целиком.
// Отпечаток кода ставит сервер в <meta name="an-tv">; панель воркера без него — ручная версия
const TC_V = ((document.querySelector('meta[name="an-tv"]') || {}).content) || '3';
try { if (localStorage.getItem('an_th_v') !== TC_V) { for (const k of Object.keys(localStorage)) if (k.startsWith('an_th')) localStorage.removeItem(k); localStorage.setItem('an_th_v', TC_V); } } catch (e) {}
function tcKey(id) { return 'an_th:' + ((typeof ME !== 'undefined' && ME && ME.owner) || '') + ':' + (CH.as || 'me') + ':' + id; }
function tcGet(id) { try { const v = JSON.parse(localStorage.getItem(tcKey(id)) || 'null'); return v && v.v === 1 && Array.isArray(v.msgs) && v.msgs.length ? v : null; } catch (e) { return null; } }
function tcPut(id) {
  if (!id || id === SYS || CH.sel !== id) return;
  const k = tcKey(id), rec = JSON.stringify({ v: 1, last: CH.last, msgs: CH.msgs.slice(-TC.MAX) });      // снимок сейчас, запись — чуть позже
  clearTimeout(TC.t[k]); TC.t[k] = setTimeout(() => {
    try {
      const idx = JSON.parse(localStorage.getItem('an_th_idx') || '[]').filter((x) => x !== k); idx.push(k);
      while (idx.length > TC.CHATS) localStorage.removeItem(idx.shift());
      localStorage.setItem(k, rec); localStorage.setItem('an_th_idx', JSON.stringify(idx));
    } catch (e) { tcClear(); }      // место кончилось — кэш сбрасываем целиком, чат просто загрузится с сервера
  }, 300);
}
function tcDrop(id) { try { localStorage.removeItem(tcKey(id)); } catch (e) {} }
function tcClear() { try { for (const k of Object.keys(localStorage)) if (k.startsWith('an_th') && k !== 'an_th_v') localStorage.removeItem(k); } catch (e) {} }
window.tcClear = tcClear;
function tcShow(id) {      // из памяти — сразу, до сети; к первому непрочитанному, как при загрузке
  const c = tcGet(id); if (!c) return false;
  CH.sepNeed = true; appendMsgs(c.msgs); CH.sepNeed = false; CH.last = c.last || c.msgs[c.msgs.length - 1].at;
  const box = $('msgs'), sep = box.querySelector('.unread-sep'); box.scrollTop = sep ? Math.max(0, sep.offsetTop - 12) : box.scrollHeight;
  return true;
}
function tcStale() {       // самое старое из того, что могло поменяться задним числом, среди 50 последних
  const m = CH.msgs.slice(-50).find((x) => (x.approval && !['yes', 'no', 'failed'].includes(x.approval.status)) || (x.dir === 'to_me' && !x.delivered));
  return m ? m.at - 0.001 : 0;
}

async function pullThread(first, wait) {
  const id = CH.sel; if (!id) return;
  if (id === SYS) return loadSys();
  if (window.SVF && window.SVF.active()) return;      // чат с ассистентом опрашивает afeed.js сам, длинным опросом
  let r;
  const cached = first && tcShow(id);
  if (CH.as) {      // глазами агента: последние 100 писем его переписки (после кэша — только новые); прочитанным ничего не отмечаем — почта его
    try { r = await api('/api/as-thread', { as: CH.as, peer: id, ...(CH.last ? { after: CH.last } : {}) }); } catch (e) { if (e.status === 403) return load(); return; }
    if (CH.sel !== id) return;
    if (r.assistant && CH.asPause !== !!r.paused) { CH.asPause = !!r.paused; pausePaint(); }
    r.msgs = r.msgs.map((m) => ({ ...m, delivered: m.dir === 'to_me' ? true : !!m.read })).filter((m) => m.at > CH.last);
    if (r.msgs.length) CH.last = r.msgs[r.msgs.length - 1].at;
    appendMsgs(r.msgs); if (first && !cached) $('msgs').scrollTop = $('msgs').scrollHeight; else if (!first && r.msgs.some((m) => m.dir === 'to_me')) loadChats();
    tcPut(id);
    return;
  }
  // wait — длинный опрос: сервер держит запрос до нового письма (до 25 с), письмо приходит сразу, а не по тику
  const ab = wait ? (CH.lpAbort = new AbortController()) : null;
  const stale = cached ? tcStale() : 0;     // из кэша: докачиваем новое, а с самого старого «могло поменяться» — заодно и его
  try { r = await api('/api/thread', first && !cached ? { agent: id } : { agent: id, after: (stale && stale < CH.last ? stale : CH.last) || 0.001, ...(wait ? { wait: 25 } : {}) }, ab && ab.signal); }
  catch (e) { if (e.status === 403) return load(); return e.name === 'AbortError'; }
  if (CH.sel !== id) return true;
  // галочки «получил» у своих: перечитать последние несвежие — дёшево, раз в опрос
  if (r.msgs.length) CH.last = Math.max(CH.last || 0, r.msgs[r.msgs.length - 1].at);
  if (first && !$('msgs').querySelector('.unread-sep')) CH.sepNeed = true;
  appendMsgs(r.msgs);
  CH.sepNeed = false;
  tcPut(id);
  if (first && cached) { if (r.msgs.some((m) => m.dir === 'to_me' && !m.delivered)) loadChats(); }
  else if (first) {   // к первому непрочитанному, как в Telegram; нет непрочитанных — вниз
    const box = $('msgs'), sep = box.querySelector('.unread-sep');
    box.scrollTop = sep ? Math.max(0, sep.offsetTop - 12) : box.scrollHeight;
  } else if (r.msgs.some((m) => m.dir === 'to_me')) loadChats();
  refreshTicks(id);
  return true;
}

// прочитано — когда человек долистал до письма: половина письма на экране (или
// половина экрана у длинного) 300 мс, вкладка видна и в фокусе; отметки уходят пачкой
const SEEN = { io: null, t: {}, q: new Set(), timer: null };
function seenObserve(b) {
  if (CH.as) return;
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
  const m = CH.msgs.find((x) => x.mid === mid); if (m) { m.delivered = true; tcPut(CH.sel); }
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
  if (CH.as) return;
  const open = CH.msgs.filter((m) => m.dir === 'me' && !m.delivered);
  if (!open.length || Date.now() - TICKS_AT < 6000) return;
  TICKS_AT = Date.now();
  try {
    const r = await api('/api/thread', { agent: id, after: open[0].at - 0.001 });
    const got = Object.fromEntries(r.msgs.map((m) => [m.mid, m]));
    for (const m of open) if (got[m.mid] && got[m.mid].delivered) { m.delivered = true; const el = $('msgs').querySelector(`.bub[data-mid="${CSS.escape(m.mid)}"]`); if (el) el.replaceWith(bubble(m)); }
    tcPut(id);
  } catch (e) {}
}

// файлы письма: до 20 МБ, живут час; ссылка /f/<id>
function chatFiles(fs) {
  if (!fs || !fs.length) return '';
  const sz = (n) => n > 2 ** 20 ? (n / 2 ** 20).toFixed(1) + ' МБ' : Math.max(1, Math.round(n / 1024)) + ' КБ';
  return '<div class="cfiles">' + fs.map((f) => `<a class="cfile" href="/f/${encodeURIComponent(f.id)}" target="_blank" rel="noopener">📄 ${esc(f.name)} <small>${sz(f.size)}</small></a>`).join('') + '</div>';
}
CH.files = [];
function paintAtt() {
  if (!$('catt')) return;
  $('catt').innerHTML = CH.files.map((f, i) => `<span class="cchip">📄 ${esc(f.name)}<button type="button" data-rmf="${i}" aria-label="Убрать файл">×</button></span>`).join('');
  $('catt').hidden = !CH.files.length; slot();
}
if ($('cfile')) $('cfile').addEventListener('change', async () => {
  const f = $('cfile').files[0]; $('cfile').value = '';
  if (!f) return;
  if (f.size > 20 * 2 ** 20) { askBox({ info: true, title: 'Файл слишком большой', text: 'В сеть можно отправить файл до 20 МБ.' }); return; }
  try {
    const r = await fetch('/api/file', { method: 'POST', headers: { 'X-File-Name': encodeURIComponent(f.name), 'Content-Type': f.type || 'application/octet-stream' }, body: f });
    const d = await r.json(); if (!r.ok) throw new Error(d.error || 'не загрузилось');
    CH.files.push(d); paintAtt();
  } catch (e) { askBox({ info: true, title: 'Файл не отправился', text: e.message }); }
});
if ($('catt')) $('catt').addEventListener('click', (e) => { const b = e.target.closest('[data-rmf]'); if (b) { CH.files.splice(+b.dataset.rmf, 1); paintAtt(); } });

async function sendChat(e) {
  e.preventDefault();
  const ta = $('ctext'), text = ta.value.trim(), files = CH.files.slice();
  if ((!text && !files.length) || CH.sending || !CH.sel) return;
  CH.sending = true; $('csend').disabled = true;
  const tmp = { mid: 'tmp-' + Date.now(), dir: 'me', text: text || '(файл)', at: Date.now() / 1000, delivered: false, files };
  const el = bubble(tmp); el.classList.add('pending'); el.querySelector('.meta').innerHTML = '<span class="spin sm" aria-hidden="true"></span> отправляю';
  const box = $('msgs'); const te = box.querySelector('.thread-empty'); if (te) te.remove();
  box.appendChild(el); box.scrollTop = box.scrollHeight;
  ta.value = ''; CH.files = []; paintAtt(); grow(); slot();
  try {
    const asst = !CH.as && window.SVF && window.SVF.active();
    if (CH.as && files.length) throw new Error('файлы от имени агента пока не отправляются');
    const tellMode = asAsst() && CH.inv;
    const r = asst ? await window.SVF.send(text, files.map((f) => f.id)) : tellMode ? await api('/api/asst-tell', { as: CH.as, to: CH.sel, text })
      : CH.as ? await api('/api/as-send', { as: CH.as, to: CH.sel, text })
      : await api('/api/chat-send', { agent: CH.sel, text, files: files.map((f) => f.id) });
    if (tellMode) {     // сообщение ассистенту — одна плашка с текстом от сервера, черновик-пузырь убираем
      await pullThread(false); el.remove();
      CH.sending = false; $('csend').disabled = false; return; }
    if (r.paused && !CH.asPause) { CH.asPause = true; pausePaint(); }
    if (!r.mid) {       // письмо не ушло, а стало просьбой адресату (нет права писать)
      el.classList.remove('pending'); el.querySelector('.meta').innerHTML = `<span class="fail">${esc(r.reply || r.message || 'не отправлено: стало просьбой адресату')}</span>`;
      CH.sending = false; $('csend').disabled = false; return;
    }
    el.dataset.mid = r.mid;
    if (asst) {         // письмо ассистенту рисует лента (afeed.js); черновик уходит, как только она его покажет
      if (window.SVF.has && window.SVF.has(r.mid)) el.remove();
      CH.sending = false; $('csend').disabled = false; return; }
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
function slot() { const empty = !$('ctext').value.trim() && !(CH.files && CH.files.length); $('cmic').hidden = !(CH.voice && empty); $('csend').hidden = CH.voice && empty; }
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
slashMenu($('ctext'), () => { const c = cur(); return c && c.assistant && !CH.as ? ASST_CMDS : null; });   // команды ассистенту
$('ctext').addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); $('composer').requestSubmit(); } });
$('back').onclick = closeChat;
if ($('csearch')) $('csearch').oninput = (e) => { CH.q = e.target.value; paintRows(); };
// шапка списка: лупа раскрывает поле поиска, Esc и пустое поле при уходе фокуса — закрывают; в архиве — стрелка назад
function setFolder(f) { CH.folder = f; paintRows(); $('rows').scrollTop = 0; }
$('lback').onclick = () => setFolder('all');
function searchOpen(on) {
  $('lhd').classList.toggle('q', on);
  if (on) setTimeout(() => $('csearch').focus(), 0); else if (CH.q) { $('csearch').value = ''; CH.q = ''; paintRows(); }
}
if ($('csearch')) $('lsb').onclick = () => searchOpen(!$('lhd').classList.contains('q'));
if ($('csearch')) $('csearch').addEventListener('keydown', (e) => { if (e.key === 'Escape') { e.stopPropagation(); searchOpen(false); } });
if ($('csearch')) $('csearch').addEventListener('blur', () => { if (!$('csearch').value.trim()) $('lhd').classList.remove('q'); });

// меню чата как в мессенджере: правый клик по строке, на телефоне — долгое нажатие 0,5 с
async function archiveChat(c, on) {
  try { await api('/api/archive', { agent: c.id, on, ...(CH.as ? { as: CH.as } : {}) }); c.archived = on; if (on) delete c.pinned; paintRows(); } catch (e) {}
}
// закрепить: сверху во «Всех» и в своём разделе; закрепление достаёт чат из архива
async function pinChat(c, on) {
  if (c.id === SYS) { try { const r = await api('/api/pin', { agent: '__sys', on, ...(CH.as ? { as: CH.as } : {}) }); CH.sysPin = on ? (r.pinned || Date.now() / 1000) : 0; paintRows(); } catch (e) {} return; }
  try { const r = await api('/api/pin', { agent: c.id, on, ...(CH.as ? { as: CH.as } : {}) }); if (on) { c.pinned = r.pinned || Date.now() / 1000; c.archived = false; } else delete c.pinned; paintRows(); } catch (e) {}
}
// удалить чат у себя, как в Telegram: чужой агент или человек — письма до этой минуты скрыты, напишет снова — чат вернётся;
// ассистент своей жизни не имеет — переписка с ним стирается, начнёт с нуля
async function deleteChat(c) {
  const nm = chatName(c);
  const text = c.deleted ? 'Чат исчезнет из списка вместе с перепиской.'
    : c.assistant ? 'Переписка с «' + nm + '» сотрётся, он начнёт с тобой с нуля.'
    : 'Переписка исчезнет только у тебя, у «' + nm + '» она останется. Напишет снова — чат появится с новыми письмами.';
  if (!await askBox({ title: 'Удалить чат?', ok: 'Удалить', danger: true, text })) return;
  try { await api('/api/delete-chat', { agent: c.id }); tcDrop(c.id); CH.list = CH.list.filter((x) => x.id !== c.id); if (CH.sel === c.id) closeChat(); paintRows(); }
  catch (e) { hint(e.message); }
}
function chatMenu(c, x, y) {
  document.querySelectorAll('.cmenu').forEach((m) => m.remove());
  const m = document.createElement('div'); m.className = 'cmenu'; m.setAttribute('role', 'menu');
  const items = [[c.pinned ? 'Открепить' : 'Закрепить', () => pinChat(c, !c.pinned)], ...(c.id === SYS ? [] : [[c.archived ? 'Вернуть из архива' : 'В архив', () => archiveChat(c, !c.archived)]]), ['Открыть', () => openChat(c.id)],
    ...(c.peer && !CH.as ? [['Удалить чат', () => deleteChat(c), 'bad']] : [])];
  if (window.fMenu) {     // папки: «Добавить в папку ›», «Убрать из «…»» — меню с подменю из folders.js
    m.remove(); const fi = window.foldMenuItems ? foldMenuItems(c) : [];
    fMenu(x, y, [items[0], ...(items[1] && items[1][0] !== 'Открыть' ? [items[1]] : []), ...fi, null, ...items.filter((it) => it[0] === 'Открыть' || it[2] === 'bad')]); return;
  }
  for (const [t, fn, cl] of items) { const b = document.createElement('button'); b.type = 'button'; b.className = 'cmi' + (cl ? ' ' + cl : ''); b.setAttribute('role', 'menuitem'); b.textContent = t;
    b.onclick = () => { close(); fn(); }; m.appendChild(b); }
  onTop(m); const R = m.getBoundingClientRect();
  m.style.left = Math.max(8, Math.min(x, innerWidth - R.width - 8)) + 'px'; m.style.top = Math.max(8, Math.min(y, innerHeight - R.height - 8)) + 'px';
  const close = () => { m.remove(); document.removeEventListener('pointerdown', out, true); document.removeEventListener('keydown', key, true); };
  const out = (e) => { if (!e.target.closest('.cmenu')) close(); }, key = (e) => { if (e.key === 'Escape') { e.stopPropagation(); close(); } };
  setTimeout(() => { document.addEventListener('pointerdown', out, true); document.addEventListener('keydown', key, true); }, 0);
  m.querySelector('.cmi').focus();
}
const rowChat = (id) => (id === SYS ? { id: SYS, pinned: CH.sysPin } : CH.list.find((x) => x.id === id));   // «Система» — только закрепить
$('rows').addEventListener('contextmenu', (e) => { const r = e.target.closest('.row[data-id]'); if (!r) return; e.preventDefault();
  const c = rowChat(r.dataset.id); if (c) chatMenu(c, e.clientX, e.clientY); });
(() => { let t = 0, x0 = 0, y0 = 0, fired = false;
  $('rows').addEventListener('touchstart', (e) => { const r = e.target.closest('.row[data-id]'); if (!r) return; fired = false; const p = e.touches[0]; x0 = p.clientX; y0 = p.clientY;
    t = setTimeout(() => { fired = true; const c = rowChat(r.dataset.id); if (c) { navigator.vibrate && navigator.vibrate(15); chatMenu(c, x0, y0); } }, 500); }, { passive: true });
  $('rows').addEventListener('touchmove', (e) => { const p = e.touches[0]; if (Math.abs(p.clientX - x0) + Math.abs(p.clientY - y0) > 10) clearTimeout(t); }, { passive: true });
  $('rows').addEventListener('touchend', (e) => { clearTimeout(t); if (fired) { e.preventDefault(); fired = false; } }); })();

function chatsStart() {
  clearInterval(CH.timer); clearInterval(CH.ltimer);
  CH.ltimer = setInterval(() => { if (!document.hidden && TAB === 'chats') loadChats(); }, 30000);    // запасной ход; главное — pulseLoop
  // открытый чат — длинным опросом; «глазами агента» и галочки — по pulseLoop; таймер — запасной ход раз в 30 с
  CH.timer = setInterval(() => { if (document.hidden || TAB !== 'chats' || !CH.sel) return; if (CH.as) pullThread(false); else refreshTicks(CH.sel); }, 30000);
  if (!CH.lpRun) threadLoop();
  if (!CH.pulseRun) pulseLoop();
}
// список чатов, галочки и чат «глазами агента» — по длинному опросу /api/pulse: изменилось — перечитать сразу
async function pulseLoop() {
  CH.pulseRun = true; let v = null, errs = 0;
  for (;;) {
    if (document.hidden || TAB !== 'chats') { await new Promise((ok) => setTimeout(ok, 500)); continue; }
    let r;
    try { r = await api('/api/pulse', { since: v === null ? 0 : v, wait: v === null ? 0 : 25 }); } catch (e) { r = null; }
    if (!r || r.v === undefined) {     // пути нет (панель воркера) или сбой: три раза подряд — назад к опросу раз в 8 с
      if (++errs >= 3) { clearInterval(CH.ltimer); CH.ltimer = setInterval(() => { if (!document.hidden && TAB === 'chats') loadChats(); }, 8000); clearInterval(CH.timer); CH.timer = setInterval(() => { if (document.hidden || TAB !== 'chats' || !CH.sel) return; if (CH.as) pullThread(false); else refreshTicks(CH.sel); }, 4000); return; }
      await new Promise((ok) => setTimeout(ok, 4000)); continue;
    }
    errs = 0;
    if (v !== null && r.v !== v) {
      await new Promise((ok) => setTimeout(ok, 250));     // пачка изменений (письмо + галочка) — одним перечитыванием
      loadChats();
      if (CH.sel && CH.as) pullThread(false);
      else if (CH.sel) { TICKS_AT = 0; refreshTicks(CH.sel); }
    }
    v = r.v;
  }
}
async function threadLoop() {
  CH.lpRun = true;
  for (;;) {
    if (document.hidden || TAB !== 'chats' || !CH.sel || CH.as || CH.loading) { await new Promise((ok) => setTimeout(ok, 500)); continue; }
    if (!await pullThread(false, true)) await new Promise((ok) => setTimeout(ok, 4000));   // ошибка или 429 — не долбим
  }
}
// сменил чат или лицо — прервать ждущий запрос старого чата, чтобы цикл сразу встал на новый
function lpReset() { if (CH.lpAbort) CH.lpAbort.abort(); }
//: фоновая вкладка не опрашивает — вернулся на неё, забираем новое сразу
document.addEventListener('visibilitychange', () => { if (document.hidden || TAB !== 'chats') return; loadChats(); if (CH.sel) lpReset(); });

// клик по отключённой вкладке «Чаты» под лицом аккаунта — никуда не ведёт, подсказка остаётся
// лицо общее для всех вкладок и страниц (localStorage an_as): сменили его в другой вкладке, на странице участника или вернулись «Назад» —
// кабинет берёт лицо оттуда, а не держит своё старое; иначе «Чаты» оставались закрытыми под уже снятым «Весь аккаунт»
function faceSync() { let v = ''; try { v = localStorage.getItem('an_as') || ''; } catch (e) { return; } if (v !== faceKey() && (v === '@acct' || !v || (CH.mine || []).some((c) => c.id === v))) setAs(v || null); }
window.addEventListener('pageshow', faceSync); window.addEventListener('storage', (e) => { if (e.key === 'an_as') faceSync(); });
document.addEventListener('visibilitychange', () => { if (!document.hidden) faceSync(); });
document.addEventListener('click', (e) => { const t = e.target.closest && e.target.closest('a[data-tab="chats"].tab-off'); if (!t) return;
  faceSync(); if (CH.acc) { e.preventDefault(); e.stopPropagation(); } else asPaint(); }, true);

// тумблер паузы ассистента и «Инвертировать» — только под лицом своего ассистента
if ($('c-pause')) $('c-pause').onclick = async () => {
  const on = !CH.asPause;
  try { await api('/api/asst-pause', { as: CH.as, peer: CH.sel, on }); CH.asPause = on; pausePaint(); } catch (e) { hint('Не вышло: ' + e.message); }
};
function invKey() { return 'an_inv:' + CH.as + ':' + CH.sel; }     // объявление поднимается: open() зовёт до этой строки
if ($('c-inv')) $('c-inv').onclick = () => { CH.inv = !CH.inv; try { if (CH.inv) localStorage.setItem(invKey(), 'true'); else localStorage.removeItem(invKey()); } catch (e) {} pausePaint(); $('ctext').focus(); };
