// общие помощники кабинета: запрос к API, экранирование, время, аватарка по имени
const $ = (id) => document.getElementById(id);
async function api(path, body) {
  const r = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
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
  const w = String(name || '?').replace(/^@/, '').split(/[\s/._-]+/).filter(Boolean);
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
// ссылки в тексте сообщения — кликабельные, остальное — как есть
function linkify(text) {
  return esc(text).replace(/https?:\/\/[^\s<>"]+/g, (u) => `<a href="${u}" target="_blank" rel="noopener noreferrer">${u}</a>`);
}
async function copyText(btn, text) {
  const was = btn.textContent;
  try { await navigator.clipboard.writeText(text); btn.textContent = 'Скопировано'; } catch (e) { btn.textContent = 'Выдели и скопируй'; }
  setTimeout(() => { btn.textContent = was; }, 1500);
}
