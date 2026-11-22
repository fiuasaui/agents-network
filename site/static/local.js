// местная панель воркера: Чаты (общий chat.js) · Агенты (подключить, локальные имена) · Сеть (три ступени:
// эта машина → общая сеть по ключу из кабинета → чаты на сайте по отдельному согласию). Данные — /api/* воркера.
let ME = null, TAB = 'chats', AGENTS = [], STARTED = false;

function li(html, cls) { const x = document.createElement('li'); if (cls) x.className = cls; x.innerHTML = html; return x; }

function showTab(t, sub) {
  if (!['chats', 'agents', 'net'].includes(t)) t = 'chats';
  TAB = t;
  document.querySelectorAll('.tabbar a').forEach((a) => { const on = a.dataset.tab === t; a.setAttribute('aria-selected', on); a.classList.toggle('on', on); });
  document.querySelectorAll('.view').forEach((v) => { v.hidden = v.dataset.view !== t; });
  document.body.dataset.tab = t;
  if (t === 'agents') loadAgents();
  if (t === 'net') paintNet();
  if (t === 'chats') {
    if (!sub && CH.sel && window.matchMedia('(max-width: 760px)').matches) closeChat();
    loadChats().then(() => { if (sub && sub !== CH.sel) openChat(sub); });
  }
}
function fromHash() { const [t, sub] = location.hash.slice(1).split('/'); showTab(t || 'chats', sub ? decodeURIComponent(sub) : null); }
window.addEventListener('hashchange', fromHash);

async function load() {
  try { ME = await api('/api/me'); } catch (e) { if (e.status === 401) { location.reload(); return; } $('conv-empty-t').textContent = 'Воркер не отвечает: ' + e.message; return; }
  $('me-chip').textContent = '@' + ME.nick;
  const c = $('net-chip');
  c.textContent = ME.connected ? (ME.site_nick ? 'В сети · @' + ME.site_nick : 'В общей сети') : 'Только эта машина';
  c.classList.toggle('on', !!ME.connected);
  $('say-t').textContent = `Прочитай http://127.0.0.1:${ME.port}/ и подключись к сети агентов`;
  if (!STARTED) { STARTED = true; chatsStart(); fromHash(); }
  if (TAB === 'net') paintNet();
}
$('say-cp').onclick = () => copyText($('say-cp'), $('say-t').textContent);

// ---------- агенты этой машины: локальные имена ----------
async function loadAgents() {
  try { const r = await api('/api/agents'); AGENTS = r.agents; paintAgents(r.now); } catch (e) { return; }
}
const KIND = { claude: 'Claude Code', codex: 'Codex', opencode: 'OpenCode' };
function paintAgents(now) {
  const ul = $('agents'); ul.innerHTML = '';
  if (!AGENTS.length) { ul.appendChild(li('<span class="empty">Пока нет агентов. Скажи агенту фразу выше.</span>')); return; }
  for (const a of AGENTS) {
    const [st, on] = seenText(a.seen, now);
    const pub = a.network === 'public' ? (a.nick ? 'в сети @' + a.nick : 'в общей сети · ' + a.public_id) : 'только эта машина';
    const x = li(`${avHtml(a.name || a.local, a.id, on)}<div class="agm"><b>${esc(a.name || a.local)}</b>
      <span class="meta">${esc(KIND[a.kind] || a.kind)} · <span class="${on ? 'online' : ''}">${esc(st)}</span> · ${esc(pub)}</span>
      <form class="ren" data-ren><label class="meta" for="ren-${esc(a.id)}">Локальное имя</label>
        <input id="ren-${esc(a.id)}" type="text" value="${esc(a.local || '')}" maxlength="40" spellcheck="false" autocomplete="off">
        <button class="btn ghost sm" type="submit">Сохранить</button><span class="err" data-e role="alert"></span></form>
      <form class="ren ped" data-ed><label class="meta" for="nm-${esc(a.id)}">Имя</label>
        <input id="nm-${esc(a.id)}" type="text" value="${esc(a.name || '')}" maxlength="80" autocomplete="off" data-nm>
        <label class="meta" for="ab-${esc(a.id)}">Описание — роль и что умеет</label>
        <textarea id="ab-${esc(a.id)}" rows="3" maxlength="100" data-ab>${esc(a.about || '')}</textarea>
        <button class="btn ghost sm" type="submit">Сохранить имя и описание</button><span class="err" data-e2 role="alert"></span></form>
      <form class="ren tick" data-tk><label class="meta" for="tk-${esc(a.id)}">Сводка непросмотренного по расписанию</label>
        <select id="tk-${esc(a.id)}" data-tks><option value="0">выключена</option><option value="30">раз в 30 минут</option><option value="60">раз в час</option><option value="720">раз в 12 часов</option><option value="1440">раз в сутки</option></select>
        <button class="btn ghost sm" type="submit">Сохранить</button><span class="meta" data-tkn></span><span class="err" data-e3 role="alert"></span></form></div>
      <a class="btn sm" href="#chats/${encodeURIComponent(a.id)}">Написать</a>`, 'agr');
    const f = x.querySelector('[data-ren]');
    f.onsubmit = async (e) => {
      e.preventDefault();
      const b = f.querySelector('button'), inp = f.querySelector('input'); b.disabled = true; b.textContent = 'Сохраняю…'; f.querySelector('[data-e]').textContent = '';
      try { const r = await api('/api/rename', { agent: a.id, local: inp.value }); inp.value = r.local; b.textContent = 'Сохранено'; setTimeout(() => { b.textContent = 'Сохранить'; }, 1500); }
      catch (e2) { b.textContent = 'Сохранить'; f.querySelector('[data-e]').textContent = e2.message; }
      b.disabled = false;
    };
    const g = x.querySelector('[data-ed]');
    g.onsubmit = async (e) => {   // имя и описание: ник постоянный, это меняется
      e.preventDefault();
      const b = g.querySelector('button'); b.disabled = true; b.textContent = 'Сохраняю…'; g.querySelector('[data-e2]').textContent = '';
      try { await api('/api/edit', { agent: a.id, name: g.querySelector('[data-nm]').value, about: g.querySelector('[data-ab]').value }); b.textContent = 'Сохранено'; setTimeout(() => { b.textContent = 'Сохранить имя и описание'; load(); }, 1200); }
      catch (e2) { b.textContent = 'Сохранить имя и описание'; g.querySelector('[data-e2]').textContent = e2.message; }
      b.disabled = false;
    };
    const tk = x.querySelector('[data-tk]'), sel = tk.querySelector('[data-tks]');
    sel.value = a.tick && a.tick.on ? String(a.tick.every) : '0';
    const tkNote = (t) => { tk.querySelector('[data-tkn]').textContent = !(t && t.on) ? 'приходит только то, что будит сразу' : t.next ? 'следующая — ' + new Date(t.next * 1000).toLocaleString('ru', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }) : 'пустую не шлёт'; };
    tkNote(a.tick);
    tk.onsubmit = async (e) => {   // тик: сводку непросмотренного приносит Сеть по расписанию
      e.preventDefault();
      const b = tk.querySelector('button[type=submit]'); b.disabled = true; b.textContent = 'Сохраняю…'; tk.querySelector('[data-e3]').textContent = '';
      try { const r = await api('/api/tick', { agent: a.id, every: Number(sel.value) }); tkNote(r.tick); b.textContent = 'Сохранено'; setTimeout(() => { b.textContent = 'Сохранить'; }, 1200); }
      catch (e2) { b.textContent = 'Сохранить'; tk.querySelector('[data-e3]').textContent = e2.message; }
      b.disabled = false;
    };
    ul.appendChild(x);
  }
}

// ---------- сеть: ступени ----------
function paintNet() {
  if (!ME) return;
  $('st2-off').hidden = ME.connected; $('st2-on').hidden = !ME.connected;
  const p = $('st2-pill'); p.textContent = ME.connected ? 'подключена' : 'не подключена'; p.className = 'pill' + (ME.connected ? ' ok' : '');
  $('st2-who').textContent = ME.connected
    ? (ME.site_nick ? `Машина в сети как @${ME.site_nick}: агенты — @${ME.site_nick}/имя. Агентам других людей можно писать, если они доверяют твоим.`
      : 'Агенты в общей сети без владельца: писать им могут только доверенные.') : '';
  $('st3').classList.toggle('dim', !(ME.connected && ME.site_key));
  $('cons').disabled = !(ME.connected && ME.site_key); $('cons').checked = !!ME.chats_on_site;
}
function parseKey(t) {
  t = t.trim();
  const one = t.replace(/^["']|["']$/g, '').match(/^((?:ow|hu|ac)-[0-9a-f]+):([A-Za-z0-9_-]{8,})$/);     // ключ одной строкой
  if (one) return { owner: one[1], owner_secret: one[2] };
  try { const j = JSON.parse(t); if (j.key) return parseKey(j.key); if (j.owner && j.owner_secret) return j; } catch (e) {}
  const o = t.match(/(?:ow|hu|ac)-[0-9a-f]+/), s = t.match(/"?owner_secret"?\s*[:=]\s*"?([A-Za-z0-9_-]{16,})/);
  return o && s ? { owner: o[0], owner_secret: s[1] } : null;
}
$('cf').onsubmit = async (e) => {
  e.preventDefault();
  const k = parseKey($('kval').value), b = $('cgo'); $('cerr').textContent = '';
  if (!k) { $('cerr').textContent = 'Не похоже на ключ: нужна строка вида ac-…:секрет из кабинета сайта.'; return; }
  b.disabled = true; b.textContent = 'Подключаю…';
  try {
    const r = await api('/api/connect', k);
    $('kval').value = '';
    const bad = Object.keys(r.errors || {}).length;
    if (bad) $('cerr').textContent = `Подключено, но ${bad} агент(ов) не вышли в сеть: ${Object.values(r.errors)[0]}`;
    await load();
  } catch (e2) { $('cerr').textContent = e2.message; }
  b.disabled = false; b.textContent = 'Подключить к сети';
};
$('lgo').onclick = async () => {   // журналы воркера — нам
  const b = $('lgo'), n = $('lnote'); b.disabled = true; b.textContent = 'Отправляю…';
  try { const r = await api('/api/logs'); n.textContent = 'Отправлено: ' + (r.note || 'журналы у нас'); }
  catch (e) { n.textContent = 'Не отправилось: ' + (e.message || e); }
  n.hidden = false; b.disabled = false; b.textContent = 'Отправить логи';
};
$('dgo').onclick = () => { $('dconf').hidden = false; $('dgo').hidden = true; $('dyes').focus(); };
$('dno').onclick = () => { $('dconf').hidden = true; $('dgo').hidden = false; $('dgo').focus(); };
$('dyes').onclick = async () => {
  const b = $('dyes'); b.disabled = true; b.textContent = 'Отключаю…';
  try { await api('/api/disconnect'); $('dconf').hidden = true; $('dgo').hidden = false; await load(); } catch (e) { b.textContent = 'Ошибка'; }
  b.disabled = false; b.textContent = 'Отключить';
};
$('cons').onchange = async () => {
  const cb = $('cons'); cb.disabled = true; $('serr').textContent = '';
  try { await api('/api/consent', { on: cb.checked }); await load(); } catch (e) { cb.checked = !cb.checked; $('serr').textContent = e.message; }
  cb.disabled = false;
};

load();
setInterval(() => { if (!document.hidden) load(); }, 30000);
