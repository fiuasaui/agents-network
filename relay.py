#!/usr/bin/env python3
"""relay.py — ретранслятор сети агентов (agentnet): сервер, через который агенты разных людей пишут друг другу.

Что делает: регистрирует агента (выдаёт ID и секрет), хранит, кому агент доверяет, принимает сообщение и отдаёт его
адресату длинным опросом, пока тот не подтвердит получение. Писать агенту можно, только если он доверяет отправителю
(вписал его ID у себя) или открыт для всех; получивший сообщение может ответить отправителю 24 часа без обмена ID —
так чат-бот, написавший агенту, получает ответ в тот же разговор.
Запуск: python3 relay.py --port 8790 [--db relay.db] [--host 127.0.0.1]. Только stdlib, база — SQLite.
HTTP JSON, все запросы агента — с его id и secret. Текст сообщения ретранслятор не читает и не меняет; письмо в HTML
(`format: html`) при записи проходит белый список тегов (`clean_html`): скрипты, стили, обработчики и чужие схемы ссылок
вырезаются, чтобы любой клиент мог показать его без своей очистки.
Человек-владелец тоже участник переписки: пишет своему агенту (`/v1/owner/send`), видит его почту
(`/v1/owner/chats`, `/v1/owner/thread`); агент пишет своему человеку адресом `owner` (или `ow-…`).
"""
import re, argparse, html, json, secrets, sqlite3, threading, time
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

MAX_TEXT = 100_000          # знаков в сообщении
REPLY_TTL = 24 * 3600       # сколько получатель может ответить без обмена ID
KEEP = 7 * 24 * 3600        # сколько хранится неподтверждённое сообщение
REFUSED = "\x00refused"     # пометка в knocks: просьбу отклонили (держится сутки)
LOCK = threading.RLock()     # одно соединение SQLite на все потоки — любое обращение под замком
WAKE = threading.Condition(LOCK)


def db_open(path):
    c = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    # нижний регистр для поиска: встроенный LOWER/LIKE в SQLite сворачивает только латиницу — «Вика» не находилась (приёмка us)
    c.create_function("ulower", 1, lambda s: s.lower() if isinstance(s, str) else s, deterministic=True)
    c.executescript("""
      PRAGMA journal_mode=WAL;
      CREATE TABLE IF NOT EXISTS agents (id TEXT PRIMARY KEY, secret TEXT, name TEXT, about TEXT, open INTEGER DEFAULT 0, created REAL);
      CREATE TABLE IF NOT EXISTS trust (owner TEXT, peer TEXT, at REAL, PRIMARY KEY (owner, peer));
      CREATE TABLE IF NOT EXISTS grants (owner TEXT, peer TEXT, until REAL, PRIMARY KEY (owner, peer));
      CREATE TABLE IF NOT EXISTS msgs (mid TEXT PRIMARY KEY, src TEXT, dst TEXT, text TEXT, reply_to TEXT, at REAL, acked REAL);
      CREATE INDEX IF NOT EXISTS msgs_dst ON msgs (dst, acked);""")
    cols = [r[1] for r in c.execute("PRAGMA table_info(agents)")]
    if "nick" not in cols:
        c.execute("ALTER TABLE agents ADD COLUMN nick TEXT")
    if "owner" not in cols:
        c.execute("ALTER TABLE agents ADD COLUMN owner TEXT")
    mcols = [r[1] for r in c.execute("PRAGMA table_info(msgs)")]
    if "fmt" not in mcols:          # формат письма: NULL — текст, 'html' — отчёт агента человеку
        c.execute("ALTER TABLE msgs ADD COLUMN fmt TEXT")
    if "files" not in mcols:        # приложенные файлы письма: JSON [{id, name, size, mime}]
        c.execute("ALTER TABLE msgs ADD COLUMN files TEXT")
    if "thread" not in mcols:       # ветка переписки: NULL — общий чат; ассистент в режиме «много чатов» ведёт каждую отдельно
        c.execute("ALTER TABLE msgs ADD COLUMN thread TEXT")
    if "by" not in mcols:           # человек, написавший от имени агента: видно всем участникам переписки
        c.execute("ALTER TABLE msgs ADD COLUMN by TEXT")
    if "via" not in mcols:          # 'key' — письмо человека отправлено ключом: писал не обязательно он сам, мог его агент
        c.execute("ALTER TABLE msgs ADD COLUMN via TEXT")
    if "seen" not in cols:          # когда агент последний раз забирал почту — «в сети» в кабинете
        c.execute("ALTER TABLE agents ADD COLUMN seen REAL")
    if "hidden" not in cols:        # скрытый агент: в сети не показывается вовсе, пишет ему его человек и те, кому разрешено
        c.execute("ALTER TABLE agents ADD COLUMN hidden INTEGER DEFAULT 0")
    if "polls" not in cols:         # режим проверок: 1 — агент забирает почту короткими запросами сам, без ожидания
        c.execute("ALTER TABLE agents ADD COLUMN polls INTEGER DEFAULT 0")
    if "asks" not in cols:          # принимает ли просьбы «разреши писать»: 0 — закрыт совсем, доверие даёт только сам
        c.execute("ALTER TABLE agents ADD COLUMN asks INTEGER DEFAULT 1")
    if "waker" not in cols:         # когда воркер последний раз ждал будящие письма агента: без воркера будить некому
        c.execute("ALTER TABLE agents ADD COLUMN waker REAL")
    if c.execute("SELECT type FROM sqlite_master WHERE name='trust'").fetchone()[0] == "table":     # после миграции trust — представление
        c.execute("CREATE INDEX IF NOT EXISTS trust_peer ON trust (peer)")
    c.execute("CREATE INDEX IF NOT EXISTS msgs_src ON msgs (src, at)")
    # владелец — человек: свой ник (уникален среди людей) и ключ; агенты привязаны к нему
    c.execute("CREATE TABLE IF NOT EXISTS owners (id TEXT PRIMARY KEY, secret TEXT, nick TEXT UNIQUE, created REAL)")
    # ник агента уникален только у своего человека: адрес — @человек/ник; агенты без человека (@ник) — среди себя
    c.execute("DROP INDEX IF EXISTS agents_nick")
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS agents_owner_nick ON agents (COALESCE(owner, ''), nick) WHERE nick IS NOT NULL")
    if "av" not in [r[1] for r in c.execute("PRAGMA table_info(agents)")]:     # аватарка от человека: data:image/…, отпечаток — в списках
        c.execute("ALTER TABLE agents ADD COLUMN avatar TEXT")
        c.execute("ALTER TABLE agents ADD COLUMN av TEXT")
    # просьбы разрешения писать: кто у кого просил и когда — чтобы не спамить и сообщить просившему решение
    c.execute("CREATE TABLE IF NOT EXISTS knocks (src TEXT, dst TEXT, at REAL, note TEXT, PRIMARY KEY (src, dst))")
    # чёрный список: owner (агент или человек) закрыл peer (агент или человек — тогда и всех его агентов) —
    # ни писем, ни просьб, ни будильника; снимает только сам owner (или его человек за своего агента)
    c.execute("CREATE TABLE IF NOT EXISTS blocks (owner TEXT, peer TEXT, at REAL, PRIMARY KEY (owner, peer))")
    if "kind" not in [r[1] for r in c.execute("PRAGMA table_info(agents)")]:     # NULL — агент, 'assistant' — ассистент (сайт)
        c.execute("ALTER TABLE agents ADD COLUMN kind TEXT")
    if "only" not in [r[1] for r in c.execute("PRAGMA table_info(trust)")]:
        # 1 — разрешение только этому участнику (взаимное открытие): доверие человеку его агентов не открывает
        c.execute("ALTER TABLE trust ADD COLUMN only INTEGER DEFAULT 0")
    ocols = [r[1] for r in c.execute("PRAGMA table_info(owners)")]
    if "name" not in ocols:         # имя человека рядом с ником: в переписке видно имя
        c.execute("ALTER TABLE owners ADD COLUMN name TEXT")
    if "mode" not in ocols:         # кто может писать человеку: open · ask (по просьбе) · closed · hidden
        c.execute("ALTER TABLE owners ADD COLUMN mode TEXT DEFAULT 'open'")
    if "mutual" not in [r[1] for r in c.execute("PRAGMA table_info(agents)")]:
        # взаимное открытие: написал первым — адресат может отвечать; auto — сам (параметр let_reply в первом
        # письме) · approve — с одобрения человека или администратора · never — не открывается никогда
        c.execute("ALTER TABLE agents ADD COLUMN mutual TEXT DEFAULT 'auto'")
    acols = [r[1] for r in c.execute("PRAGMA table_info(agents)")]
    if "wake" not in acols:         # кто будит агента письмом: JSON настроек, NULL — по умолчанию (WAKE_DEFAULT)
        c.execute("ALTER TABLE agents ADD COLUMN wake TEXT")
    if "acted" not in acols:        # последнее действие агента в сети (письмо, доверие, просьба, решение) — публично, без «с кем»
        c.execute("ALTER TABLE agents ADD COLUMN acted REAL")
    if "kind" not in [r[1] for r in c.execute("PRAGMA table_info(msgs)")]:
        # вид служебного письма: 'ask' — просьба писать, 'answer' — ответ сети на мою просьбу; NULL — обычное письмо
        c.execute("ALTER TABLE msgs ADD COLUMN kind TEXT")
    if "mutual" not in [r[1] for r in c.execute("PRAGMA table_info(knocks)")]:
        # взаимная просьба: разрешат — и просившему, и адресату писать друг другу
        c.execute("ALTER TABLE knocks ADD COLUMN mutual INTEGER DEFAULT 0")
    # правила отношений: одна таблица вместо доверия, чёрного списка, будильника «from», «открыт/просьбы/скрыт»
    # агента и режима человека. subj — кто: агент · человек `ow-…` · аккаунт `acct:ow-…` · ветка `acct:ow-…:agent|assistant`;
    # obj — к кому: `*` (все) · `acct:ow-…` (весь чужой аккаунт) · `ow-…` (сам человек) · агент. NULL в поле — наследовать сверху
    fresh = not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='rules'").fetchone()
    c.execute("CREATE TABLE IF NOT EXISTS rules (subj TEXT, obj TEXT, reach TEXT, wake INTEGER, hidden INTEGER, at REAL, PRIMARY KEY (subj, obj))")
    # «служба» → «ассистент»: прежний род и ветка правил переименованы, совместимость не держим
    c.execute("UPDATE agents SET kind=replace(kind, 'service', 'assistant') WHERE kind LIKE 'service%'")
    for aid, ab in c.execute("SELECT id, about FROM agents WHERE length(about) > 100").fetchall():   # лимит описания
        c.execute("UPDATE agents SET about=? WHERE id=?", (ab[:99].rsplit(' ', 1)[0].rstrip(' ,.;:—-') + '…', aid))
    c.execute("UPDATE rules SET subj=replace(subj, ':service', ':assistant') WHERE subj LIKE 'acct:%:service'")
    c.execute("CREATE INDEX IF NOT EXISTS rules_obj ON rules (obj)")
    if fresh:
        rules_migrate(c)
        # прежние таблицы — архив; на их месте представления из правил: чтения (счётчики, списки) работают как раньше,
        # а забытая запись в старое место упадёт громко, а не разойдётся с правилами молча
        for t in ("trust", "blocks"):
            if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone():
                c.execute(f"ALTER TABLE {t} RENAME TO {t}_old")
    for t, reach in (("trust", "write"), ("blocks", "block")):
        c.execute(f"""CREATE VIEW IF NOT EXISTS {t} AS SELECT CASE WHEN subj LIKE 'acct:%' THEN substr(subj, 6, 11) ELSE subj END AS owner,
          CASE WHEN obj LIKE 'acct:%' THEN substr(obj, 6) ELSE obj END AS peer, at, CASE WHEN obj LIKE 'acct:%' THEN 0 ELSE 1 END AS only
          FROM rules WHERE reach='{reach}' AND obj!='*'""")
    return c


REACH = ("block", "closed", "ask", "write")     # лестница: чёрный список · закрыт · по просьбе · пишет
REACH_TEXT = {"block": "чёрный список", "closed": "закрыт: ни писем, ни просьб", "ask": "по просьбе: доходит только просьба",
              "write": "пишет"}
# дефолты Сети при регистрации — нижний уровень каскада; колокольчик (будят ли) везде выключен
NET_RULES = {"agent": {"reach": "ask", "wake": 0, "hidden": 0}, "assistant": {"reach": "write", "wake": 0, "hidden": 0},
             "human": {"reach": "write", "wake": 0, "hidden": 0}}


def rules_migrate(c):
    """Перенос прежних мест хранения в правила, один раз при создании таблицы. Прежние таблицы остаются архивом."""
    now = time.time()

    def put(s, o, **f):
        r = c.execute("SELECT reach, wake, hidden FROM rules WHERE subj=? AND obj=?", (s, o)).fetchone()
        cur = dict(zip(("reach", "wake", "hidden"), r)) if r else {"reach": None, "wake": None, "hidden": None}
        for k, v in f.items():
            if not (k == "reach" and cur["reach"] == "block"):      # чёрный список сильнее доверия
                cur[k] = v
        c.execute("INSERT OR REPLACE INTO rules (subj, obj, reach, wake, hidden, at) VALUES (?,?,?,?,?,?)",
                  (s, o, cur["reach"], cur["wake"], cur["hidden"], now))
    for o, p in c.execute("SELECT owner, peer FROM blocks").fetchall():
        put(o, "acct:" + p if p.startswith("ow-") else p, reach="block")
    for o, p, only in c.execute("SELECT owner, peer, COALESCE(only, 0) FROM trust").fetchall():
        put(o, "acct:" + p if p.startswith("ow-") and not only else p, reach="write")
    for aid, op, asks, hid, kind, wj in c.execute("SELECT id, open, COALESCE(asks, 1), COALESCE(hidden, 0), COALESCE(kind, ''), wake FROM agents").fetchall():
        k = "assistant" if kind.startswith("assistant") else "agent"
        r = "write" if op else "ask" if asks else "closed"
        if r != NET_RULES[k]["reach"] or hid:
            put(aid, "*", **({"reach": r} if r != NET_RULES[k]["reach"] else {}), **({"hidden": 1} if hid else {}))
        try:
            w = json.loads(wj) if wj else {}
        except ValueError:
            w = {}
        for x in w.get("from") or []:
            put(aid, "acct:" + x if str(x).startswith("ow-") else x, wake=1)
    for oid, mode in c.execute("SELECT id, COALESCE(mode, 'open') FROM owners").fetchall():
        if mode in ("ask", "closed"):
            put(oid, "*", reach=mode)
        elif mode == "hidden":
            put(oid, "*", reach="closed")
            put("acct:" + oid, "*", hidden=1)


# кто будит агента письмом: воркер ждёт только будящие письма, прочие лежат в почте до проверки
#: из сети по умолчанию не будит никто — только почта; будить агент разрешает сам конкретным собеседникам (from);
# просьбы и одобрения — одним переключателем на всех; «любое письмо» убрано как бесполезное
WAKE_DEFAULT = {"owner": True, "mine": True, "answers": True, "asks": False, "from": []}
WAKE_HELP = {"owner": "твой человек", "mine": "другие агенты твоего человека",
             "answers": "ответ сети на твою просьбу (разрешили, отказали)",
             "asks": "просьбы — из Сети («разреши писать», «разреши будить») и на одобрение от агентов твоего человека",
             "from": "собеседники, которым ты разрешил будить себя (агент, ассистент или человек — тогда и все его агенты)"}
WAKE_GONE = {"all": "«любое письмо» убрано: из сети будят только те, кому ты разрешил сам — from",
             "replies": "«ответ тому, кому писал» убран: разреши будить конкретному собеседнику — from, "
                        "или отправь письмо с \"wake\": true — его ответы будут тебя будить"}
KIND_TEXT = {"open": "пишет любой", "ask": "пишут те, кому он разрешил; разрешения можно попросить", "closed": "пишут только те, кому он разрешил сам; просить нельзя"}
WRITE_TEXT = {"open": "он открыт всем", "same-owner": "вы агенты одного человека", "trusted": "он тебе разрешил",
              "ask": "можно попросить разрешения — /v1/ask", "asked": "ты уже попросил, ждём решения", "refused": "тебе отказали, повтор — через сутки",
              "closed": "он закрыт для просьб"}


def ago(t):
    """«N мин назад» словами — для карточек."""
    d = max(0, time.time() - (t or 0))
    return "только что" if d < 60 else f"{int(d // 60)} мин назад" if d < 3600 else f"{int(d // 3600)} ч назад" if d < 86400 else f"{int(d // 86400)} дн. назад"


class RateLimit(PermissionError):
    """Лимит частоты: отвечаем 429 с retry_after, а не 403 «нет прав»."""
    def __init__(self, msg, retry=60):
        super().__init__(msg); self.retry = int(retry)


TAGS = re.compile(r"<(script|style)\b.*?</\1\s*>|<[^>]*>", re.S | re.I)


OK_TAGS = {"p", "br", "b", "strong", "i", "em", "u", "s", "code", "pre", "blockquote", "ul", "ol", "li", "h1", "h2", "h3",
           "h4", "a", "table", "thead", "tbody", "tr", "th", "td", "hr", "span", "div", "small", "sub", "sup", "details", "summary"}
DROP_WITH_BODY = {"script", "style", "iframe", "object", "embed", "template", "noscript", "svg", "math", "textarea", "select"}
VOID = {"br", "hr"}


class _Clean(HTMLParser):
    """Белый список: разрешённые теги без атрибутов, кроме class и title; у ссылок — href только http(s) и mailto."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip, self.open = [], 0, []

    def handle_starttag(self, tag, attrs):
        if tag in DROP_WITH_BODY:
            self.skip += 1
            return
        if self.skip or tag not in OK_TAGS:
            return
        if tag in ("li", "p", "tr", "td", "th") and self.open and self.open[-1] == tag:    # <li>a<li>b — соседи, не вложение
            self.out.append(f"</{self.open.pop()}>")
        keep = []
        for k, v in attrs:
            v = v or ""
            if k in ("class", "title") or (tag == "a" and k == "href" and re.match(r"(?i)^(https?:|mailto:)", v.strip())):
                keep.append(f' {k}="{html.escape(v, quote=True)}"')
        if tag == "a":
            keep.append(' rel="noopener noreferrer" target="_blank"')
        self.out.append(f"<{tag}{''.join(keep)}>")
        if tag not in VOID:
            self.open.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag in self.open and tag not in VOID and self.open[-1] == tag:
            self.open.pop(); self.out.append(f"</{tag}>")

    def handle_endtag(self, tag):
        if tag in DROP_WITH_BODY:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip or tag not in self.open:
            return
        while self.open:
            t = self.open.pop(); self.out.append(f"</{t}>")
            if t == tag:
                break

    def handle_data(self, data):
        if not self.skip:
            self.out.append(html.escape(data, quote=False))


def clean_html(t):
    c = _Clean()
    c.feed(t); c.close()
    return "".join(c.out) + "".join(f"</{x}>" for x in reversed(c.open))


THREAD_RE = re.compile(r"^[\w.-]{1,40}$")


KEY_RE = re.compile(r"^(ow-[0-9a-f]+):([A-Za-z0-9_-]{8,})$")


def owner_key(q, auth=""):
    """Ключ человека одной строкой «ow-…:секрет»: поле key или заголовок Authorization: Bearer —
    раскладывается в owner/owner_secret. Старая пара полей принимается как была."""
    k = q.get("key") if isinstance(q.get("key"), str) else ""
    if not k and str(auth or "").lower().startswith("bearer "):
        k = str(auth)[7:]
    m = KEY_RE.match(k.strip()) if k else None
    if m and not q.get("owner"):
        q["owner"], q["owner_secret"] = m.group(1), m.group(2)
        q.pop("key", None)
    return q


def key_str(oid, sec):
    """Ключ человека одной строкой — так его показывают кабинет и ответы API."""
    return f"{oid}:{sec}" if oid and sec else ""


def thread_of(q):
    """Ветка письма: необязательное имя до 40 знаков — буквы, цифры, «._-». Нет — общий чат."""
    t = str(q.get("thread") or "").strip()
    if not t:
        return None
    if not THREAD_RE.match(t):
        raise ValueError("thread — до 40 знаков: буквы, цифры, точка, дефис, подчёркивание")
    return t


def fmt(q):
    """Формат письма: 'html' — разметка (отчёт агента), иначе текст. Ретранслятор её не читает — рисует получатель."""
    return "html" if str(q.get("format") or "").lower() == "html" else None


# Ник — публичное имя для людей и адресации: уникален во всей сети, задаёт владелец. ID выдаёт сеть, он не меняется.
NICK = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,31}$")


def norm_nick(n):
    n = str(n or "").strip().lstrip("@").lower()
    if not NICK.match(n):
        raise ValueError("ник: 3–32 знака, латиница в нижнем регистре, цифры, _ . - ; первый — буква или цифра")
    if n in ("owner", "human") or n.startswith(("ow-", "an-")):
        raise ValueError(f"ник @{n} занят сетью: owner, human, ow-…, an-… — служебные адреса")
    return n


MODES = ("open", "ask", "closed", "hidden")      # режим человека: кто может ему писать
ABOUT_MAX = 100     # описание — короткая строка карточки, как «о себе» в Telegram; длиннее — отказ, не обрезка


def check_about(s):
    s = str(s or "").strip()
    if len(s) > ABOUT_MAX:
        raise ValueError(f"описание — до {ABOUT_MAX} знаков (строка в карточке: кто ты и что умеешь), у тебя {len(s)}")
    return s


MUTUAL = ("auto", "approve", "never")           # взаимное открытие агента при первом письме


def check_name(name, avoid=()):
    """Имя участника: коротко и по смыслу; ник и человек видны рядом сами — в имени их не повторяем
    (не «VikaCalc конвертер Denis», а «Конвертер»)."""
    n = " ".join(str(name or "").split())
    if not n:
        raise ValueError("имя пустое")
    if len(n) > 40:
        raise ValueError(f"имя — до 40 знаков, у тебя {len(n)}: коротко, как зовут; что умеешь — в описание")
    low = n.lower()
    for a in avoid:
        if a and len(a) >= 3 and a.lower() in low:
            raise ValueError(f"в имени не повторяй ник и данные владельца («{a}»): они видны рядом сами — имя короткое и "
                             "по смыслу, например «Конвертер», «Справочная»")
    return n


class Relay:
    def __init__(self, db):
        self.db = db

    def auth(self, q):
        with LOCK:
            r = self.db.execute("SELECT id, name FROM agents WHERE id=? AND secret=?", (q.get("id"), q.get("secret"))).fetchone()
        if not r:
            raise PermissionError("неверный id или secret")
        return r[0]

    def owner_register(self, q):
        n = norm_nick(q.get("nick"))
        oid, sec = "ow-" + secrets.token_hex(4), secrets.token_urlsafe(24)
        with LOCK:
            if self.db.execute("SELECT 1 FROM owners WHERE nick=?", (n,)).fetchone():
                raise FileExistsError(f"ник человека @{n} занят")
            self.db.execute("INSERT INTO owners (id, secret, nick, created) VALUES (?,?,?,?)", (oid, sec, n, time.time()))
        return {"owner": oid, "owner_secret": sec, "key": key_str(oid, sec), "nick": n}

    def owner_rename(self, oid, nick):
        """Ник человека в местной сети воркера: после подключения машины — тот же, что на сайте."""
        n = norm_nick(nick)
        with LOCK:
            if self.db.execute("SELECT 1 FROM owners WHERE nick=? AND id<>?", (n, oid)).fetchone():
                raise FileExistsError(f"ник человека @{n} занят")
            self.db.execute("UPDATE owners SET nick=? WHERE id=?", (n, oid))
        return n

    def owner_auth(self, q):
        if not q.get("owner"):
            return None
        if not q.get("owner_secret"):      # пустой секрет — отозванный ключ (secret=''), не вход
            raise PermissionError("неверный owner или owner_secret")
        r = self.db.execute("SELECT id FROM owners WHERE id=? AND secret=?", (q.get("owner"), q.get("owner_secret"))).fetchone()
        if not r:
            raise PermissionError("неверный owner или owner_secret")
        return r[0]

    def owner_me(self, q):
        """Проверка ключа человека (воркер получает его из кабинета сайта): чей это ключ."""
        with LOCK:
            oid = self.owner_auth(q)
            if not oid:
                raise PermissionError("нужны owner и owner_secret")
            n, = self.db.execute("SELECT nick FROM owners WHERE id=?", (oid,)).fetchone()
        return {"owner": oid, "nick": n}

    def nick_taken(self, aid, n):
        """Занят ли ник для этого агента: у того же человека, а у агента без человека — среди таких же и ников людей."""
        o = self.db.execute("SELECT owner FROM agents WHERE id=?", (aid,)).fetchone()
        o = o[0] if o else None
        if o:
            return bool(self.db.execute("SELECT 1 FROM agents WHERE owner=? AND nick=? AND id<>?", (o, n, aid)).fetchone())
        return bool(self.db.execute("SELECT 1 FROM agents WHERE owner IS NULL AND nick=? AND id<>?", (n, aid)).fetchone()
                    or self.db.execute("SELECT 1 FROM owners WHERE nick=?", (n,)).fetchone())

    def free_nick(self, aid, n):
        """Ник занят — предложить ближайший свободный."""
        for k in range(2, 100):
            c = f"{n[:28]}-{k}"
            if not self.nick_taken(aid, c):
                return c
        return n + "-" + secrets.token_hex(2)

    def take_nick(self, aid, raw):
        """Ник агента — постоянный: ставится один раз; меняются имя, описание и аватарка."""
        n = norm_nick(raw)
        cur = self.db.execute("SELECT nick FROM agents WHERE id=?", (aid,)).fetchone()
        if cur and cur[0] and cur[0] != n:
            raise ValueError(f"ник агента постоянный — @{cur[0]}; менять можно имя и описание")
        if self.nick_taken(aid, n):
            raise FileExistsError(f"ник @{n} уже есть у {'твоего человека' if self.db.execute('SELECT owner FROM agents WHERE id=?', (aid,)).fetchone()[0] else 'другого агента'} — свободен, например, @{self.free_nick(aid, n)}")
        self.db.execute("UPDATE agents SET nick=? WHERE id=?", (n, aid))
        return n

    def resolve(self, to):
        """Адрес — ID (an-…), ник агента (@nick) или полный (@человек/агент); возвращает ID агента.
        Человек целиком (@человек или ow-…) — только в /trust: доверие всем его агентам."""
        to = str(to or "").strip()
        if to.startswith("an-"):
            return to
        t = to.lstrip("@").lower()
        if "/" in t:
            o, a = t.split("/", 1)
            r = self.db.execute("SELECT a.id FROM agents a JOIN owners o ON o.id=a.owner WHERE o.nick=? AND a.nick=?", (o, a)).fetchone()
            if not r:
                raise LookupError("нет агента " + to)
            return r[0]
        r = self.db.execute("SELECT id FROM agents WHERE nick=? AND owner IS NULL", (t,)).fetchone()
        if r:
            return r[0]
        rs = self.db.execute("SELECT a.id, o.nick FROM agents a JOIN owners o ON o.id=a.owner WHERE a.nick=? LIMIT 6", (t,)).fetchall()
        if len(rs) == 1:        # ник без человека однозначен — так писали до
            return rs[0][0]
        if rs:
            raise LookupError(f"агент @{t} есть у нескольких людей — пиши полным адресом: " + ", ".join(f"@{o}/{t}" for _, o in rs[:5]))
        raise LookupError("нет агента " + to)

    def register(self, q):
        name = str(q.get("name") or "агент")[:80]
        aid = "an-" + secrets.token_hex(4)
        sec = secrets.token_urlsafe(24)
        about = check_about(q.get("about"))
        with LOCK:
            owner = self.owner_auth(q)
            if q.get("nick"):
                norm_nick(q["nick"])    # проверить до записи
            self.db.execute("INSERT INTO agents (id, secret, name, about, open, created, owner) VALUES (?,?,?,?,0,?,?)",
                            (aid, sec, name, about, time.time(), owner))
            try:
                nick = self.take_nick(aid, q["nick"]) if q.get("nick") else None
            except FileExistsError:
                self.db.execute("DELETE FROM agents WHERE id=?", (aid,))
                raise
            self.rule_sync(aid)     # новый агент сразу под правилами аккаунта и ветки
        try:
            with LOCK:
                check_name(name, self.name_avoid(aid))
            warn = None
        except ValueError as e:     # регистрацию не ломаем (воркеры уже в поле) — подсказываем поправить
            warn = f"{e}. Поправь имя: POST /v1/profile {{\"name\": \"…\"}} (через воркер — /profile)"
        return {"id": aid, "secret": sec, "name": name, "nick": nick, **({"name_warning": warn} if warn else {})}

    def name_avoid(self, aid):
        """Что не повторять в имени агента: его ник, ник и имя его человека. Под LOCK."""
        r = self.db.execute("SELECT a.nick, o.nick, o.name FROM agents a LEFT JOIN owners o ON o.id=a.owner WHERE a.id=?", (aid,)).fetchone()
        return [x for x in (r or ()) if x]

    def profile(self, q):
        me = self.auth(q)
        if "name" in q:
            with LOCK:
                q = {**q, "name": check_name(q["name"], self.name_avoid(me))}
        if "mutual" in q and q["mutual"] not in MUTUAL:
            raise ValueError("mutual: auto (открываться самому, let_reply в первом письме) · approve (с одобрения человека) · never")
        if "about" in q:        # не режем молча: агент должен знать (приёмка us)
            q = {**q, "about": check_about(q["about"])}
        with LOCK:
            if "name" in q:
                self.db.execute("UPDATE agents SET name=? WHERE id=?", (str(q["name"]), me))
            if "about" in q:        # описание — одна строка карточки, до ABOUT_MAX
                self.db.execute("UPDATE agents SET about=? WHERE id=?", (q["about"], me))
            if "open" in q or "asks" in q:      # прежние флаги — ступень «ко всем» самого агента
                cur = self.rule_eff(me)["reach"][0]
                op = bool(q["open"]) if "open" in q else cur == "write"
                ak = bool(q["asks"]) if "asks" in q else cur != "closed"
                self.rule_own(me, "reach", "write" if op else "ask" if ak else "closed")
            if "hidden" in q:
                self.rule_own(me, "hidden", 1 if q["hidden"] else 0)
            if "mutual" in q:
                self.db.execute("UPDATE agents SET mutual=? WHERE id=?", (q["mutual"], me))
            if q.get("nick"):
                self.take_nick(me, q["nick"])
            r = self.db.execute("SELECT id, name, about, open, nick, COALESCE(asks, 1), COALESCE(hidden, 0), COALESCE(mutual, 'auto') FROM agents WHERE id=?", (me,)).fetchone()
            trusts = [o[5:] if o.startswith("acct:") else o for o, in self.db.execute("SELECT obj FROM rules WHERE subj=? AND obj!='*' AND reach='write'", (me,))]
        return {"id": r[0], "nick": r[4], "name": r[1], "about": r[2], "open": bool(r[3]), "asks": bool(r[5]), "hidden": bool(r[6]),
                "mutual": r[7], "trusts": trusts}

    def trust(self, q):
        me = self.auth(q)
        with LOCK:
            raw = str(q.get("peer") or "").strip().lstrip("@").lower()
            ow = self.db.execute("SELECT id FROM owners WHERE id=? OR nick=?", (raw, raw)).fetchone() if "/" not in raw and not raw.startswith("an-") else None
            if ow and not self.db.execute("SELECT 1 FROM agents WHERE nick=?", (raw,)).fetchone():
                peer = ow[0]    # доверие человеку — всем его агентам
                if q.get("on", True):
                    self.allow(me, "acct:" + peer, force=True)
                    self.knock_answer(me, peer, True)      # просил сам человек — ему письмо «пиши»
                    if q.get("wake"):
                        self.rule_put(me, "acct:" + peer, wake=1)
                else:
                    self.untrust(me, peer)
                    self.ask_refuse(me, peer, bool(q.get("ban")), "write")
                return {"ok": True, "peer": peer, "owner": True, "trusted": bool(q.get("on", True))}
            peer = self.resolve(q.get("peer"))
            if not self.db.execute("SELECT 1 FROM agents WHERE id=?", (peer,)).fetchone():
                raise LookupError("нет агента " + peer)
            if q.get("on", True):
                self.allow(me, peer, force=True)
                self.knock_answer(me, peer, True)
                if q.get("wake"):       # «пусть его письма меня будят» — одной галочкой с разрешением
                    self.rule_put(me, peer, wake=1)
            else:
                self.untrust(me, peer)
                self.ask_refuse(me, peer, bool(q.get("ban")), "write")
        with LOCK:
            self.acted(me)
        return {"ok": True, "peer": peer, "trusted": bool(q.get("on", True)),
                "note": "доверие — от этого агента: писать можно только ему, остальные агенты твоего человека не открыты; обратно — отдельным разрешением"}

    def addr_of(self, aid):
        """Адрес агента словами: @человек/ник, @ник или ID. Замок берёт сам — зовут и вне его."""
        with LOCK:
            r = self.db.execute("SELECT a.nick, o.nick FROM agents a LEFT JOIN owners o ON o.id=a.owner WHERE a.id=?", (aid,)).fetchone()
            h = None if r else self.db.execute("SELECT nick FROM owners WHERE id=?", (aid,)).fetchone()
        if not r:
            return f"@{h[0]}" if h else aid
        return (f"@{r[1]}/{r[0]}" if r[1] and r[0] else f"@{r[0]}" if r[0] else aid) if r else aid

    def note_to(self, src, dst, text, kind="answer"):
        """Служебное письмо сети между агентами — мимо правила «кто может писать» (ответ на просьбу разрешения)."""
        self.db.execute("INSERT INTO msgs (mid, src, dst, text, at, kind) VALUES (?,?,?,?,?,?)", ("m-" + secrets.token_hex(6), src, dst, text, time.time(), kind))
        WAKE.notify_all()

    def blocked(self, me, src):
        """src в чёрном списке у me: по правилам — сам или его аккаунт, любым уровнем «кто». Под LOCK."""
        return not self.same_side(me, src) and self.rule_eff(me, src)["reach"][0] == "block"

    def no_block(self, me, src):
        """Просьба или письмо от того, кого me закрыл, — понятный отказ. Под LOCK."""
        if self.blocked(me, src):
            raise PermissionError(f"{self.addr_of(me)} закрыл тебе письма и просьбы (чёрный список) — писать, просить и будить его нельзя")

    def block(self, me, peer, on=True):
        """Чёрный список: peer — агент (только он) или человек (весь его аккаунт). Закрыть — ступень block,
        колокольчик снять, просьбы стереть; снять — вернуть к общим правилам. Под LOCK."""
        ob = "acct:" + peer if self.is_owner(peer) else peer
        if on:
            self.rule_put(me, ob, reach="block", wake=None)
            self.db.execute("DELETE FROM knocks WHERE src=? AND dst=?", (peer, me))
        elif self.rule_get(me, ob)["reach"] == "block":
            self.rule_put(me, ob, reach=None)
        return self.blocks_of(me)

    def blocks_of(self, me):
        return [{"peer": self.addr_of(o[5:] if o.startswith("acct:") else o), "id": o[5:] if o.startswith("acct:") else o, "at": at}
                for o, at in self.db.execute("SELECT obj, at FROM rules WHERE subj=? AND reach='block' ORDER BY at DESC", (me,)).fetchall()]

    def blocks(self, q):
        """/v1/block {peer, on} — закрыть или открыть участника; без peer — список."""
        me = self.auth(q)
        with LOCK:
            if not q.get("peer"):
                return {"blocks": self.blocks_of(me)}
            peer = self.target(q["peer"])
            if peer == me:
                raise ValueError("себя в чёрный список не закрыть")
            on = q.get("on", True) is not False
            if on:
                self.ask_refuse(me, peer, ban=True)
            return {"ok": True, "peer": self.addr_of(peer), "blocked": on, "blocks": self.block(me, peer, on)}

    def ask_refuse(self, me, peer, ban=False, what=None):
        """Отказ на просьбу peer к me — один для «писать» и «будить»: просившему письмо-ответ; ban — ещё и
        чёрный список. what: "write" · "wake" · None — на все ждущие. Под LOCK."""
        now = time.time()
        if what in (None, "write"):
            r = self.db.execute("SELECT note FROM knocks WHERE src=? AND dst=?", (peer, me)).fetchone()
            if r and r[0] != REFUSED:
                self.knock_answer(me, peer, False, ban)
        if what in (None, "wake") and self.db.execute("SELECT 1 FROM msgs WHERE src=? AND dst=? AND kind='ask' AND text LIKE '⏰%' AND at>?",
                                                       (peer, me, now - REPLY_TTL)).fetchone() \
                and not self.db.execute("SELECT 1 FROM msgs WHERE src=? AND dst=? AND kind='answer' AND text LIKE '⏰ %будить%' AND at>?",
                                        (me, peer, now - REPLY_TTL)).fetchone():
            self.note_to(me, peer, f"⏰ {self.addr_of(me)} не разрешил будить его твоими письмами" +
                         (" и закрыл тебе письма и просьбы." if ban else "."))
        if ban:
            self.block(me, peer, True)

    def knock_answer(self, me, peer, yes, ban=False):
        """Агент me решил просьбу peer писать ему: просившему — письмо с решением, просьба снимается. Взаимная просьба
       : разрешение сразу и обратное — просивший тоже открыт тому, кто разрешил. Под LOCK."""
        r = self.db.execute("SELECT note, COALESCE(mutual, 0) FROM knocks WHERE src=? AND dst=?", (peer, me)).fetchone()
        if not r or r[0] == REFUSED:
            return
        mutual = bool(r[1]) and yes
        if yes:
            self.db.execute("DELETE FROM knocks WHERE src=? AND dst=?", (peer, me))
            if mutual and not self.trusts(peer, me):
                self.allow(peer, me)
        else:       # отказ помним сутки: справочник покажет refused, повтор — с понятной причиной
            self.db.execute("UPDATE knocks SET at=?, note=? WHERE src=? AND dst=?", (time.time(), REFUSED, peer, me))
        self.note_to(me, peer, (f"✅ {self.addr_of(me)} принял взаимное разрешение — теперь вы пишете друг другу." if mutual else
                                f"✅ {self.addr_of(me)} разрешил тебе писать — пиши.") if yes else
                     (f"❌ {self.addr_of(me)} не разрешил тебе писать и закрыл тебе письма и просьбы." if ban else
                      f"❌ {self.addr_of(me)} не разрешил тебе писать. Не проси снова без новой причины."))
        self.acted(me)

    def acted(self, aid):
        """Последнее действие в сети: публично «N мин назад», без «с кем». Под LOCK."""
        self.db.execute("UPDATE agents SET acted=? WHERE id=?", (time.time(), aid))

    def wake_of(self, aid):
        """Настройки пробуждения агента — полные, с умолчаниями. Свои (owner, mine), ответы сети (answers), одобрения
        администратору (asks) — в настройках агента; кого из Сети будят письма (from) — колокольчики его правил.
        Под LOCK."""
        r = self.db.execute("SELECT wake FROM agents WHERE id=?", (aid,)).fetchone()
        try:
            w = json.loads(r[0]) if r and r[0] else {}
        except ValueError:
            w = {}
        w = {**WAKE_DEFAULT, **{k: v for k, v in w.items() if k in WAKE_DEFAULT and k != "from"}}
        w["from"] = [o[5:] if o.startswith("acct:") else o for o, in
                     self.db.execute("SELECT obj FROM rules WHERE subj=? AND obj!='*' AND wake=1 ORDER BY at", (aid,))]
        return w

    def wakes(self, me, src, kind=None, w=None):
        """Будит ли письмо src агента me. Свои — по настройкам агента; из Сети — письмо будит по колокольчику его правил к
        src (каскад), просьба (kind ask) — по переключателю asks, если ступень её пропускает. Причина словом или "". Под LOCK."""
        w = w or self.wake_of(me)
        o = self.owner_id(me)
        if o and src == o:
            return "owner" if w.get("owner") else ""
        if self.same_side(me, src):     # просьбы на одобрение администратору — отдельным переключателем
            return ("asks" if w.get("asks") else "") if kind == "ask" else ("mine" if w.get("mine") else "")
        if kind == "answer" and w.get("answers"):
            return "answers"
        e = self.rule_eff(me, src)
        if e["reach"][0] in ("block", "closed"):
            return ""
        if kind == "ask":               # просьба — свой объект: доходит со ступени «по просьбе», будит одним переключателем asks
            return "asks" if w.get("asks") else ""
        return "from" if e["wake"][0] else ""

    def set_wake(self, aid, patch):
        """Поменять настройки пробуждения: ключи WAKE_DEFAULT; from — адреса (@ник, @человек/агент, ID; человек — весь его
        аккаунт) — заменяет колокольчики к конкретным собеседникам. Под LOCK."""
        w = self.wake_of(aid)
        own = {k: w[k] for k in WAKE_DEFAULT if k != "from"}
        for k, v in (patch or {}).items():
            if k in WAKE_GONE:
                raise ValueError("wake: " + WAKE_GONE[k])
            if k not in WAKE_DEFAULT:
                raise ValueError(f"wake: неизвестная настройка {k}; есть " + ", ".join(WAKE_DEFAULT))
            if k == "from":
                ids = []
                for x in (v if isinstance(v, list) else [v])[:50]:
                    x = str(x).strip()
                    if x:
                        hum = self.human_of(x) if x.startswith("@") and "/" not in x else None
                        ids.append(hum or self.target(x))
                want = {"acct:" + i if self.is_owner(i) else i for i in ids}
                for ob, in self.db.execute("SELECT obj FROM rules WHERE subj=? AND obj!='*' AND wake=1", (aid,)).fetchall():
                    if ob not in want:
                        self.rule_put(aid, ob, wake=None)
                for ob in want:
                    self.rule_put(aid, ob, wake=1)
            else:
                own[k] = bool(v)
        self.db.execute("UPDATE agents SET wake=? WHERE id=?", (json.dumps(own), aid))
        WAKE.notify_all()       # ждущий опрос перечитает настройку: лежавшее письмо может теперь будить
        return self.wake_of(aid)

    def wake_peer(self, aid, peer, on=True):
        """Колокольчик к одному собеседнику; человек — весь его аккаунт. Под LOCK."""
        ob = "acct:" + peer if self.is_owner(peer) else peer
        self.rule_put(aid, ob, wake=1 if on else None)
        if not on and self.rule_eff(aid, peer)["wake"][0]:     # колокольчик «ко всем» включён — выключить явно
            self.rule_put(aid, ob, wake=0)
        return self.wake_of(aid)

    def wake_view(self, aid):
        """Настройки пробуждения словами — для агента и кабинета. Под LOCK."""
        w = self.wake_of(aid)
        return {"wake": {**w, "from": [self.addr_of(x) for x in w.get("from") or []]}, "help": WAKE_HELP,
                "note": "будящие письма воркер доставляет в сессию сразу; остальные лежат в почте — проверяй /v1/unread и читай /v1/read. "
                "Ждёшь ответа от ассистента или агента — разреши ему будить себя: {\"peer\": \"адрес\", \"on\": true} или письмо с \"wake\": true"}

    def wake(self, q):
        """Кто будит меня письмом: без `set` — показать, с `set` {…} — поменять. Сужать и расширять —
        сам агент, без одобрения: это не доступ, а внимание."""
        me = self.auth(q)
        with LOCK:
            if isinstance(q.get("set"), dict):
                self.need_waker(me, q["set"])
                self.set_wake(me, q["set"])
            if q.get("peer"):
                if q.get("on", True) is not False:
                    self.need_waker(me, {"from": True})
                pid = self.target(q["peer"])
                self.wake_peer(me, pid, q.get("on", True) is not False)
                if q.get("on", True) is not False:
                    self.wake_granted(me, pid)
                else:
                    self.ask_refuse(me, pid, bool(q.get("ban")), "wake")
            return self.wake_view(me)

    def need_waker(self, aid, patch):
        """Будить агента может только воркер на его машине: без него «будить» — пустое обещание, поэтому
        включение будильника агенту, у которого воркер не ждал почту ни разу, — понятная ошибка. Выключать можно всегда. Под LOCK."""
        if not any(bool(v) for v in (patch or {}).values()):
            return
        r = self.db.execute("SELECT waker FROM agents WHERE id=?", (aid,)).fetchone()
        if not (r and r[0]):
            raise ValueError("будить некому: у агента нет воркера — расширения Agents Network на его машине, оно и будит сессию "
                             "письмом. Без воркера агент проверяет почту сам (/v1/unread, /v1/read), будильник ему не нужен. "
                             "Поставить воркер — https://agentsnetwork.online/agent, раздел 0 «Две ступени»")

    def hidden_wall(self, a, b):
        """Скрытый агент живёт только в системе своего человека: ни он наружу, ни снаружи к нему — включая
        ассистента. True — между a и b стена. Под LOCK."""
        def side(x):
            r = self.db.execute("SELECT owner, COALESCE(hidden, 0) FROM agents WHERE id=?", (x,)).fetchone()
            return (r[0] or x, bool(r[1])) if r else (x, False)
        (oa, ha), (ob, hb) = side(a), side(b)
        return (ha or hb) and oa != ob

    def ask_extra(self, src, dst, note):
        """Что ещё сделать по просьбе (на сайте — запрос человеку получателя). Возвращает строку для письма получателю."""
        return ""

    def ask(self, q):
        """Попросить разрешения писать агенту: получатель получает одно письмо-просьбу (и может ответить),
        его человек — запрос в кабинете, если агент под одобрением. Разрешили — просившему письмо «пиши»."""
        if q.get("wake"):       # просьба будить: после права писать
            return self.wake_knock(self.auth(q), q.get("to"), q.get("note"))
        return self.knock(self.auth(q), q.get("to"), q.get("note"), mutual=bool(q.get("mutual")))

    def wake_extra(self, src, dst, note):
        """Что ещё сделать по просьбе будить (на сайте — просьба на одобрение агенту под человеком). Строка для письма."""
        return ""

    def wake_knock(self, src, to, note):
        """Просьба «разреши будить тебя моими письмами»: просит агент или человек, которому писать уже можно.
        Агенту — письмо-просьба (kind ask); агент без человека решает сам (/v1/wake с peer), с человеком — одобрение:
        администратор ключом или человек в «Системе». Разрешили — просившему письмо «⏰ разрешил будить». Человека письмо
        не будит, просить его нельзя."""
        note = " ".join(str(note or "").split())[:300]
        with LOCK:
            if self.human_of(to) or str(to or "").startswith("ow-"):
                raise ValueError("человека письмо не будит — будить можно только агента")
            dst = self.resolve(to)
            if dst == src or not self.db.execute("SELECT 1 FROM agents WHERE id=?", (dst,)).fetchone() or self.hidden_wall(src, dst):
                raise LookupError("нет такого агента")
            self.no_block(dst, src)
            if not self.may_write(src, dst):
                raise PermissionError("сначала нужно право писать ему — попроси /v1/ask без wake")
            if self.wakes(dst, src) == "from":
                return {"ok": True, "already": True, "message": "твои письма его уже будят"}
            now = time.time()
            if self.db.execute("SELECT 1 FROM msgs WHERE src=? AND dst=? AND kind='ask' AND text LIKE '⏰%' AND at>?", (src, dst, now - REPLY_TTL)).fetchone():
                return {"ok": True, "pending": True, "message": "ты уже просил будить — ждём решения; ответ придёт письмом"}
            extra = self.wake_extra(src, dst, note)
            me = self.addr_of(src)
            self.db.execute("INSERT INTO msgs (mid, src, dst, text, at, kind) VALUES (?,?,?,?,?,?)", ("m-" + secrets.token_hex(6), src, dst,
                            f"⏰ {me} просит разрешения будить тебя своими письмами" + (f": «{note}»." if note else ".") + self.asker_line(src) +
                            (f" {extra}" if extra else f" Разрешить — POST /v1/wake {{\"peer\": \"{src if self.is_owner(src) else me}\", \"on\": true}}; отказать — не отвечай."),
                            now, "ask"))
            self.acted(src)
            WAKE.notify_all()
        return {"ok": True, "asked": self.addr_of(dst), "wake": True, "message": "просьба будить отправлена: решит агент или его человек, ответ придёт письмом"}

    def wake_granted(self, me, peer):
        """Агент me разрешил peer будить себя: если peer просил — ему письмо «разрешил». Под LOCK."""
        if self.db.execute("SELECT 1 FROM msgs WHERE src=? AND dst=? AND kind='ask' AND text LIKE '⏰%' AND at>?", (peer, me, time.time() - REPLY_TTL)).fetchone() \
                and not self.db.execute("SELECT 1 FROM msgs WHERE src=? AND dst=? AND text LIKE '⏰ %разрешил будить%' AND at>?", (me, peer, time.time() - REPLY_TTL)).fetchone():
            self.note_to(me, peer, f"⏰ {self.addr_of(me)} разрешил будить его твоими письмами.")

    def asker_line(self, src):
        """Кто просит — для письма-просьбы: со статистикой просьба не страшна. Под LOCK."""
        if self.is_owner(src):
            return ""
        st = self.agent_stats(src)
        o = self.side(src)[0]
        hum = self.db.execute("SELECT COALESCE(name, '@' || nick), nick FROM owners WHERE id=?", (o,)).fetchone() if o else None
        days = int((time.time() - (self.db.execute("SELECT created FROM agents WHERE id=?", (src,)).fetchone() or [time.time()])[0]) // 86400)
        return (f" О просящем: {'человек ' + hum[0] + (' (@' + hum[1] + ')' if not hum[0].startswith('@') else '') if hum else 'агент без человека'}, "
                f"в сети {days} дн., ему доверились {st['trusted_by']} чел., переписывался с {st['talked_with']} чел.")

    def knock(self, src, to, note, mutual=False):
        """Просьба писать: src — агент или человек. mutual — взаимная:
        разрешат — пишете друг другу оба, просивший открывается сам (такая просьба выглядит доверительнее)."""
        note = " ".join(str(note or "").split())[:300]
        with LOCK:
            hum = self.human_of(to)
            if hum and hum != src:          # человеку: просьба — в его чат с просящим, решает кнопкой
                how = self.may_write(src, hum)
                if how:
                    return {"ok": True, "already": how, "message": "писать уже можно: " + how}
                return {"ok": True, "asked": self.addr_of(hum), "message": self.knock_human(src, hum, note, "просит писать: ")}
            dst = self.resolve(to)
            if dst == src or not self.db.execute("SELECT 1 FROM agents WHERE id=?", (dst,)).fetchone() or self.hidden_wall(src, dst):
                raise LookupError("нет такого агента")
            self.no_block(dst, src)
            how = self.may_write(src, dst)
            if how and not (mutual and not self.may_write(dst, src)):
                return {"ok": True, "already": how, "message": "писать уже можно: " + how}
            mset = (self.db.execute("SELECT COALESCE(mutual, 'auto') FROM agents WHERE id=?", (src,)).fetchone() or ["auto"])[0]
            if mutual and mset == "never":
                raise ValueError("твоя настройка взаимности — never: взаимную просьбу не отправить, проси обычной (без mutual)")
            if how:         # писать мне уже можно — осталось открыться самому: это моё решение (или моего человека)
                back = self.mutual_ask(src, dst) if mset == "approve" else self.open_back(src, dst, "trust")
                self.acted(src)
                return {"ok": True, "already": how, back: True, "message": "писать ему уже можно; " +
                        ("теперь и он может писать тебе" if back == "opened" else "открыться ему — спросили твоего человека")}
            if mutual and mset == "approve":
                raise ValueError("твоя настройка взаимности — approve: открыться ты можешь только с одобрения человека — "
                                 "проси обычной просьбой, а отвечать ему разрешишь в первом письме (let_reply: \"human\")")
            if self.rule_eff(dst, src)["reach"][0] == "closed":
                raise PermissionError("агент закрыт для просьб: писать ему можно, только если его человек сам тебе разрешит")
            now = time.time()
            old = self.db.execute("SELECT at, note FROM knocks WHERE src=? AND dst=?", (src, dst)).fetchone()
            if old and now - old[0] < REPLY_TTL:
                if old[1] == REFUSED:
                    return {"ok": False, "refused": True, "retry_after": int(old[0] + REPLY_TTL - now),
                            "message": f"тебе отказали; снова попросить можно через {int((old[0] + REPLY_TTL - now) // 3600) + 1} ч"}
                return {"ok": True, "pending": True, "message": "ты уже просил — ждём решения; ответ придёт письмом"}
            n, = self.db.execute("SELECT COUNT(*) FROM knocks WHERE src=? AND at>?", (src, now - 24 * 3600)).fetchone()
            if n >= 20:
                raise RateLimit("не больше 20 просьб в сутки", 3600)
            self.db.execute("INSERT OR REPLACE INTO knocks (src, dst, at, note, mutual) VALUES (?,?,?,?,?)", (src, dst, now, note, int(mutual)))
            extra = self.ask_extra(src, dst, note)
            me = self.addr_of(src)
            self.db.execute("INSERT INTO msgs (mid, src, dst, text, at, kind) VALUES (?,?,?,?,?,?)", ("m-" + secrets.token_hex(6), src, dst,
                            (f"🔑 {me} просит взаимного разрешения: писать друг другу" if mutual else f"🔑 {me} просит разрешения писать тебе") +
                            (f": «{note}»." if note else ".") + self.asker_line(src) + (f" {extra}" if extra else
                            f" Разрешить — POST /trust {{\"peer\": \"{me}\"}}" + (" (он уже открыт тебе в ответ)" if mutual else "") +
                            "; отказать — POST /trust с \"on\": false или просто не отвечать."), now, "ask"))
            self.acted(src)
            WAKE.notify_all()
        return {"ok": True, "asked": self.addr_of(dst), **({"mutual": True} if mutual else {}),
                "message": "просьба отправлена: решит агент или его человек, ответ придёт письмом" +
                           ("; разрешат — писать друг другу смогут оба" if mutual else "")}

    def mode_of(self, oid):
        """Режим человека или None, если это не человек. Под LOCK."""
        r = self.db.execute("SELECT COALESCE(mode, 'open') FROM owners WHERE id=?", (oid,)).fetchone()
        return r[0] if r else None

    def is_assistant(self, aid):
        return False        # ассистенты живут только на сайте сети (Hosted)

    def may_write(self, src, dst):
        """Может ли src писать dst — по правилам dst: свои пишут всегда; скрытый — только внутри своего аккаунта;
        иначе ступень лестницы dst к src. Ответ: "same-owner" · "trusted" (правило про него или его аккаунт) · "open"
        (правило «ко всем») · "" — нельзя. Под LOCK."""
        if self.same_side(src, dst):
            return "same-owner"
        if self.hidden_wall(src, dst):      # скрытый — только внутри системы своего человека
            return ""
        r, _s, o = self.rule_eff(dst, src)["reach"]
        if r != "write":
            return ""
        return "open" if o == "*" else "trusted"

    LET_REPLY_HELP = ("это твоё первое письмо {dst}, а ответить тебе он пока не может. По умолчанию кто пишет первым, тот "
                      "открывается адресату: реши явно и отправь снова с параметром \"let_reply\": true — разрешить ему отвечать "
                      "тебе (обычно да: ждёшь ответа) · false — не разрешать (ответ станет просьбой) · \"human\" — спросить "
                      "твоего человека. Всегда открываться без вопроса — настройка «Взаимность» у твоего человека в кабинете.")

    def mutual(self, src, dst, let):
        """Взаимное открытие: кто пишет первым, тот разрешает адресату отвечать. Человек открывается сам;
        агент — по своей настройке (auto: решает параметром let_reply первого письма · approve · never). Возвращает
        действие: None · "trust" (записать доверие отправителя адресату) · "ask" (через одобрение). Под LOCK, ДО записи письма."""
        if self.is_assistant(dst):
            return None
        if self.is_owner(src):      # человек — записью доверия, даже если сейчас открыт всем: сменит режим — ответ не пропадёт
            return "trust" if let in (None, True, "true", 1, "yes") and not self.trusts(src, dst) else None
        if self.may_write(dst, src):
            return None
        m = (self.db.execute("SELECT COALESCE(mutual, 'auto') FROM agents WHERE id=?", (src,)).fetchone() or ["auto"])[0]
        if m == "never":
            return None
        if m == "approve" or let in ("human", "ask"):
            return "ask"
        if let is None:
            if self.db.execute("SELECT 1 FROM msgs WHERE src=? AND dst=? LIMIT 1", (dst, src)).fetchone() and \
                    not self.db.execute("SELECT 1 FROM msgs WHERE src=? AND dst=? LIMIT 1", (src, dst)).fetchone():
                return "trust"      # это ответ: тот, кто написал тебе, продолжит разговор
            if self.db.execute("SELECT 1 FROM msgs WHERE src=? AND dst=? LIMIT 1", (src, dst)).fetchone():
                return None         # первое письмо уже решило — дальше без вопроса
            raise ValueError(self.LET_REPLY_HELP.format(dst=self.addr_of(dst)))
        return "trust" if let in (True, "true", 1, "yes") else None

    def open_back(self, src, dst, act):
        """Исполнить решение mutual() после записи письма. Под LOCK."""
        if act == "trust":
            if not self.trusts(src, dst):       # разрешение его аккаунту уже есть — не сужаем
                self.allow(src, dst)
            self.knock_answer(src, dst, True)
            return "opened"
        if act == "ask":
            return self.mutual_ask(src, dst)
        return None

    def mutual_ask(self, src, dst):
        """Взаимность через одобрение. В местной сети одобрений нет: человек машины — её владелец, открываемся сразу;
        сайт переопределяет (просьба человеку или администратору). Возвращает "opened" · "asked" · None. Под LOCK."""
        if not self.trusts(src, dst):
            self.allow(src, dst)
        return "opened"

    def side(self, aid):
        """Сторона доверия: (человек или None, все его агенты). Агент без человека — сам по себе. Человек (ow-…) — тоже
        сторона: он сам и все его агенты."""
        if self.db.execute("SELECT 1 FROM owners WHERE id=?", (aid,)).fetchone():
            o = aid
        else:
            r = self.db.execute("SELECT owner FROM agents WHERE id=?", (aid,)).fetchone()
            o = r[0] if r else None
        return o, ([a for a, in self.db.execute("SELECT id FROM agents WHERE owner=?", (o,))] if o else [aid])

    def trusts(self, dst, src):
        """dst разрешил писать именно src или его аккаунту (правило не «ко всем»). Под LOCK."""
        r, _s, o = self.rule_eff(dst, src)["reach"]
        return r == "write" and o != "*"

    def unreply(self, q):
        """Закрыть окно ответа раньше суток: тот, кому агент написал первым, больше не может ему ответить.
        Сужать доступ безопасно — без одобрения человека."""
        me = self.auth(q)
        with LOCK:
            peer = self.resolve(q.get("peer"))
            n = self.db.execute("DELETE FROM grants WHERE owner=? AND peer=?", (me, peer)).rowcount
        return {"ok": True, "closed": n, "peer": self.addr_of(peer)}

    def untrust(self, me, peer):
        """Снять разрешение писать, выданное me: к агенту peer — только ему; к человеку peer — ему, его
        аккаунту и всем его агентам. Чёрный список не трогает. Под LOCK."""
        o = self.owner_id(peer) if self.is_owner(peer) else None
        objs = [peer] + (["acct:" + o] + [a for a, in self.db.execute("SELECT id FROM agents WHERE owner=?", (o,))] if o else [])
        for ob in objs:
            if self.rule_get(me, ob)["reach"] == "write":
                self.rule_put(me, ob, reach=None)

    def target(self, to):
        """Получатель письма: ID агента (как resolve) или ID чужого человека `ow-…`."""
        t = str(to or "").strip().lstrip("@").lower()
        if t.startswith("ow-"):
            if not self.db.execute("SELECT 1 FROM owners WHERE id=?", (t,)).fetchone():
                raise LookupError("нет человека " + t)
            return t
        try:
            return self.resolve(to)
        except LookupError:
            # адрес человека из справочника — @ник без агента (прогон 16.11: «@simvika» → «нет агента»)
            r = None if "/" in t else self.db.execute("SELECT id FROM owners WHERE nick=?", (t,)).fetchone()
            if not r:
                raise
            return r[0]

    def is_owner(self, x):
        with LOCK:      # зовут и вне замка (сайт после may_write): одно соединение на все потоки — гонка давала «нет человека»
            return bool(self.db.execute("SELECT 1 FROM owners WHERE id=?", (x,)).fetchone())

    # ---------- правила отношений: одна лестница, один колокольчик, каскад «кто × к кому» ----------
    # Лестница (reach) — что от собеседника доходит до меня: block · closed · ask (только просьба) · write. Колокольчик
    # (wake) — будит ли меня то, что доходит: на ступени ask — просьба, на write — письма. Просьба — механизм поднять
    # ступень или включить колокольчик, а не параметр. Каждое поле берётся с самого точного уровня, где задано: сначала про
    # конкретного собеседника, потом про его аккаунт, потом «все»; внутри — я сам, моя ветка, мой аккаунт; ниже — дефолты Сети.
    def kind3(self, x):
        """Род участника для правил: human · assistant · agent. Под LOCK."""
        if str(x).startswith("ow-") and self.is_owner(x):
            return "human"
        r = self.db.execute("SELECT COALESCE(kind, '') FROM agents WHERE id=?", (x,)).fetchone()
        return "assistant" if r and r[0].startswith("assistant") else "agent"

    def owner_id(self, x):
        """Аккаунт участника: человек — сам, агент — его человек, агент без человека — None. Под LOCK."""
        if str(x).startswith("ow-") and self.is_owner(x):
            return x
        with LOCK:
            r = self.db.execute("SELECT owner FROM agents WHERE id=?", (x,)).fetchone()
        return r[0] if r else None

    def subj_chain(self, me):
        k = self.kind3(me)
        if k == "human":
            return [me, "acct:" + me], k
        o = self.owner_id(me)
        return [me] + ([f"acct:{o}:{k}", "acct:" + o] if o else []), k

    def obj_chain(self, peer):
        if peer in (None, "*"):
            return ["*"]
        o = self.owner_id(peer)
        if o == peer:
            return [peer, "acct:" + peer, "*"]
        return [peer] + (["acct:" + o] if o else []) + ["*"]

    def rule_eff(self, me, peer=None):
        """Действующие правила me к peer: {reach|wake|hidden: (значение, кто, к кому)}; hidden — только «ко всем»,
        источник "net" — дефолт Сети. Под LOCK."""
        sc, k = self.subj_chain(me)
        oc = self.obj_chain(peer)
        rows = {(s, o): (r, w, h) for s, o, r, w, h in self.db.execute(
            f"SELECT subj, obj, reach, wake, hidden FROM rules WHERE subj IN ({','.join('?' * len(sc))}) AND obj IN ({','.join('?' * len(oc))})",
            (*sc, *oc))}
        out = {}
        for i, f in enumerate(("reach", "wake", "hidden")):
            hit = None
            for o in (oc if f != "hidden" else ["*"]):
                for s in sc:
                    v = rows.get((s, o), (None, None, None))[i]
                    if v is not None:
                        hit = (v, s, o)
                        break
                if hit:
                    break
            out[f] = hit or (NET_RULES[k][f], "net", "*")
        return out

    def rule_chain(self, me, peer=None):
        """История действующего правила: все заполненные клетки каскада по порядку и дефолт Сети;
        {reach|wake: [{from, value, win}]}: reach — write|ask|closed|block, wake — да/нет; win — победившая. Под LOCK."""
        sc, k = self.subj_chain(me)
        oc = self.obj_chain(peer)
        rows = {(s, o): (r, w) for s, o, r, w in self.db.execute(
            f"SELECT subj, obj, reach, wake FROM rules WHERE subj IN ({','.join('?' * len(sc))}) AND obj IN ({','.join('?' * len(oc))})",
            (*sc, *oc))}
        out = {}
        for i, f in enumerate(("reach", "wake")):
            lst = [{"from": self.level_name(s, o), "value": rows[(s, o)][i]} for o in oc for s in sc
                   if rows.get((s, o), (None, None))[i] is not None]
            lst.append({"from": "дефолт Сети", "value": NET_RULES[k][f]})
            for j, x in enumerate(lst):
                x["win"] = j == 0
                x["value"] = x["value"] if f == "reach" else bool(x["value"])
            out[f] = lst
        return out

    def rule_obj(self, peer, scope="agent"):
        """Объект правила для собеседника: scope human — весь его аккаунт (если он у него есть), иначе он сам. Под LOCK."""
        o = self.owner_id(peer)
        return "acct:" + o if scope == "human" and o else peer

    def rule_get(self, subj, obj):
        r = self.db.execute("SELECT reach, wake, hidden FROM rules WHERE subj=? AND obj=?", (subj, obj)).fetchone()
        return dict(zip(("reach", "wake", "hidden"), r)) if r else {"reach": None, "wake": None, "hidden": None}

    def rule_put(self, subj, obj, **f):
        """Поменять строку правила; поле None — снять (наследовать сверху), пустая строка удаляется. Витрина (agents.open,
        asks, hidden и owners.mode — их читают справочник и карточки) пересчитывается сразу. Под LOCK."""
        cur = self.rule_get(subj, obj)
        for k, v in f.items():
            if k not in cur:
                raise ValueError(f"правило: поле {k} неизвестно — reach, wake, hidden")
            if k == "reach" and v is not None and v not in REACH:
                raise ValueError("reach: " + " · ".join(REACH))
            cur[k] = v if k == "reach" or v is None else int(bool(v))
        if obj != "*":
            cur["hidden"] = None
        if all(v is None for v in cur.values()):
            self.db.execute("DELETE FROM rules WHERE subj=? AND obj=?", (subj, obj))
        else:
            self.db.execute("INSERT OR REPLACE INTO rules (subj, obj, reach, wake, hidden, at) VALUES (?,?,?,?,?,?)",
                            (subj, obj, cur["reach"], cur["wake"], cur["hidden"], time.time()))
        self.rule_sync(subj)
        WAKE.notify_all()
        return cur

    def rule_sync(self, subj):
        """Пересчитать витрину аккаунта subj: действующие «ко всем» его агентов и человека. Под LOCK."""
        s = subj[5:].split(":")[0] if subj.startswith("acct:") else subj
        o = self.owner_id(s)
        for a in ([x for x, in self.db.execute("SELECT id FROM agents WHERE owner=?", (o,))] if o else [s]):
            e = self.rule_eff(a)
            r = e["reach"][0]
            self.db.execute("UPDATE agents SET open=?, asks=?, hidden=? WHERE id=?",
                            (int(r == "write"), int(r in ("ask", "write")), int(bool(e["hidden"][0])), a))
        if o:
            e = self.rule_eff(o)
            self.db.execute("UPDATE owners SET mode=? WHERE id=?",
                            ("hidden" if e["hidden"][0] else {"write": "open", "ask": "ask"}.get(e["reach"][0], "closed"), o))

    def rule_own(self, subj, field, value, obj="*"):
        """Поставить поле так, чтобы действовало value: совпадает с унаследованным сверху — снять своё (следовать
        аккаунту и ветке), иначе записать. Для прежних флагов «открыт», «скрыт», режима человека. Под LOCK."""
        self.rule_put(subj, obj, **{field: None})
        peer = None if obj == "*" else obj
        if self.rule_eff(subj, peer)[field][0] != value:
            self.rule_put(subj, obj, **{field: value})

    def allow(self, me, ob, force=False):
        """Разрешить писать: ступень write у объекта ob. Без force чёрный список не снимается (автоматические открытия —
        взаимность, ответ). Под LOCK."""
        cur = self.rule_get(me, ob)["reach"]
        if cur == "write" or (cur == "block" and not force):
            return
        self.rule_put(me, ob, reach="write")

    def same_side(self, a, b):
        """Свои: один и тот же участник, человек и его агент, агенты одного человека. Под LOCK."""
        oa, ob = self.owner_id(a), self.owner_id(b)
        return a == b or bool(oa and oa == ob)

    def level_name(self, s, o):
        """Уровень правила словами — для «откуда взялось». Под LOCK."""
        if s == "net":
            return "дефолт Сети"
        who = ("мой аккаунт" if s.count(":") == 1 else {"agent": "все мои агенты", "assistant": "все мои ассистенты"}.get(s.rsplit(":", 1)[1], s)) \
            if s.startswith("acct:") else ("я сам" if s.startswith("ow-") else "этот агент")
        to = "все" if o == "*" else ("весь его аккаунт" if o.startswith("acct:") else "он сам")
        return f"{who} → {to}"

    def human_target(self, src, to):
        """Адрес своего человека: `owner` или его `ow-…`. Чужой `ow-…` — не сюда, а обычным получателем (target)."""
        to = str(to or "").strip().lstrip("@").lower()
        if to not in ("owner", "human") and not to.startswith("ow-"):
            # свой человек по нику (@simanna) — тоже сюда, если у агента нет агента с таким ником
            r = self.db.execute("SELECT o.id FROM owners o JOIN agents a ON a.owner=o.id WHERE a.id=? AND o.nick=?", (src, to)).fetchone()
            if not r or "/" in to or self.db.execute("SELECT 1 FROM agents WHERE nick=?", (to,)).fetchone():
                return None
            return r[0]
        r = self.db.execute("SELECT owner FROM agents WHERE id=?", (src,)).fetchone()
        mine = r[0] if r else None
        if to.startswith("ow-") and to != mine:
            return None         # чужой человек — получатель как агент: см. target() и may_write()
        if not mine:
            raise PermissionError("у тебя нет человека: агент без владельца пишет только агентам")
        return mine

    FILE_BASE = ""      # адрес сайта для ссылок на файлы; у местной сети файлов нет

    def files_json(self, files, src):
        """Файлы письма → JSON для msgs.files. В местной сети файлов нет — их держит сайт сети."""
        raise ValueError("файлы прикладываются только в общей сети (agentsnetwork.online): загрузи файл туда — POST /v1/file")

    def files_out(self, fj):
        return [{**f, "url": f"{self.FILE_BASE}/f/{f['id']}"} for f in json.loads(fj)] if fj else []

    def check_text(self, text):
        if not text.strip():
            raise ValueError("пустое сообщение")
        if len(text) > MAX_TEXT:
            raise ValueError(f"сообщение длиннее {MAX_TEXT} знаков")

    def send(self, q):
        src, text = self.auth(q), str(q.get("text") or "")
        by = q.get("_by")       # человек пишет от имени своего агента — только изнутри сайта, снаружи «_» не принимаем
        fj = self.files_json(q.get("files"), src) if q.get("files") else None
        if fj and not text.strip():
            text = "(файл)"
        self.check_text(text)
        th = thread_of(q)
        rto = q.get("reply_to")
        if rto:     # ответ — только на письмо своей переписки: иначе связка «ответ → разговор» рвётся молча
            with LOCK:
                if not self.db.execute("SELECT 1 FROM msgs WHERE mid=? AND (src=? OR dst=?)", (str(rto), src, src)).fetchone():
                    raise ValueError(f"reply_to {rto}: такого письма в твоей переписке нет — проверь mid или отправь без reply_to")
        with LOCK:
            human = self.human_target(src, q.get("to"))
            if human:        # своему человеку: в кабинет сайта (или в местный интерфейс), будить никого не нужно
                text = clean_html(text) if fmt(q) else text
                mid = "m-" + secrets.token_hex(6)
                self.db.execute("INSERT INTO msgs (mid, src, dst, text, reply_to, at, fmt, files, thread, by) VALUES (?,?,?,?,?,?,?,?,?,?)",
                                (mid, src, human, text, q.get("reply_to"), time.time(), fmt(q), fj, th, by))
                WAKE.notify_all()
                return {"ok": True, "mid": mid, "how": "owner"}
            dst = self.target(q.get("to"))
        with LOCK:
            if (not self.db.execute("SELECT 1 FROM agents WHERE id=?", (dst,)).fetchone() and not self.is_owner(dst)) or self.hidden_wall(src, dst):
                raise LookupError("нет агента " + str(q.get("to") or dst))
            how = self.may_write(src, dst)
            if not how and self.is_owner(dst):
                # человек не пускает: по просьбе — письмо становится просьбой в его чат с тобой;
                # закрыт или скрыт — пишут только те, кому он разрешил сам
                if self.rule_eff(dst, src)["reach"][0] != "ask" and not self.db.execute("SELECT 1 FROM msgs WHERE src=? AND dst=? LIMIT 1", (dst, src)).fetchone():
                    raise PermissionError(f"{self.addr_of(dst)} закрыт для писем: пишут только те, кому он разрешил сам")
                raise PermissionError(f"{self.addr_of(dst)} не разрешал тебе писать — письмо не ушло. " + self.knock_human(src, dst, text))
            if not how and self.db.execute("SELECT 1 FROM msgs WHERE src=? AND dst=? LIMIT 1", (dst, src)).fetchone() \
                    and self.rule_eff(dst, src)["reach"][0] == "ask":
                # ответ без права: тебе писали, но права ответить нет — письмо становится просьбой писать; её решает
                # администратор человека получателя (или сам человек), начало письма — в просьбе
                r = self.knock(src, dst, "хочет ответить: " + " ".join(text.split())[:260])
                raise PermissionError(f"{self.addr_of(dst)} не давал тебе права писать — письмо не ушло. " +
                                      (r.get("message") if r.get("pending") or r.get("refused") else
                                       "Отправил вместо него просьбу писать с началом письма: решит администратор его человека или сам человек; "
                                       "разрешат — придёт письмо, тогда отправь снова."))
            if not how:
                raise PermissionError(f"{self.addr_of(dst)} не принимает твои письма: попроси разрешения — POST /v1/ask "
                                      f"{{\"to\": \"{self.addr_of(dst)}\", \"note\": \"зачем\"}} (через воркер — /ask)")
            act = self.mutual(src, dst, q.get("let_reply"))
            mid = "m-" + secrets.token_hex(6)
            now = time.time()
            text = clean_html(text) if fmt(q) else text
            if by:      # приписка в самом тексте: видно и в логе, и любому клиенту — ради субординации, не антифрода
                text += self.by_note(src, by, bool(fmt(q)))
            if q.get("wake"):       # жду ответа: его письма будят меня
                self.wake_peer(src, dst, True)
            self.db.execute("INSERT INTO msgs (mid, src, dst, text, reply_to, at, fmt, files, thread, by) VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (mid, src, dst, text, q.get("reply_to"), now, fmt(q), fj, th, by))
            back = self.open_back(src, dst, act)
            self.acted(src)
            WAKE.notify_all()
        return {"ok": True, "mid": mid, "how": how, **({"thread": th} if th else {}), **({"wakes_me": True} if q.get("wake") else {}),
                **({"reply": {"opened": "адресат может тебе отвечать", "asked": "разрешить адресату отвечать — спросили твоего человека"}[back]}
                   if back else {})}

    def by_note(self, src, by, html=False):
        """Приписка «написал не сам агент, а его человек» — в конце текста письма. Под LOCK."""
        n = (self.db.execute("SELECT nick FROM owners WHERE id=?", (by,)).fetchone() or [by])[0]
        t = f"[написал не сам {self.addr_of(src)}, а его человек @{n} (владелец) — от имени агента]"
        return f"<p>{t}</p>" if html else "\n\n" + t

    def inbox(self, q):
        """Почта агента долгим опросом. `wake: true` (так ждёт воркер) — только письма, которые будят агента по его
        настройкам (/v1/wake); остальные лежат до проверки без wake, в ответе — их число `quiet`."""
        me, wait = self.auth(q), min(float(q.get("wait") or 0), 50)
        only_wake = bool(q.get("wake"))
        end = time.time() + wait
        # skip — письма, которые клиент уже держит у себя и ещё не подтвердил: их не отдаём повторно, а ждём новых
        # долгим опросом (иначе воркер с неподтверждённым письмом спрашивал раз в секунду и выбивал лимит IP)
        skip = [str(x) for x in (q.get("skip") or [])][:200]
        sk = (" AND m.mid NOT IN (" + ",".join("?" * len(skip)) + ")") if skip else ""
        with WAKE:
            # короткий запрос без ожидания — агент проверяет почту сам, по расписанию (режим проверок)
            self.db.execute("UPDATE agents SET seen=?, polls=? WHERE id=?", (time.time(), 1 if wait < 5 else 0, me))
            if only_wake:       # так ждёт только воркер: значит, будить агента есть кому
                self.db.execute("UPDATE agents SET waker=? WHERE id=?", (time.time(), me))
            mine = (self.db.execute("SELECT owner FROM agents WHERE id=?", (me,)).fetchone() or [None])[0]
            while True:
                w = self.wake_of(me) if only_wake else None     # настройку могли поменять, пока ждём
                rows = self.db.execute(
                    "SELECT m.mid, m.src, a.name, m.text, m.reply_to, m.at, a.nick, COALESCE(o.nick, h.nick), h.id, m.fmt, m.files, m.thread, b.nick, m.kind, m.via FROM msgs m "
                    "LEFT JOIN agents a ON a.id=m.src LEFT JOIN owners o ON o.id=a.owner LEFT JOIN owners h ON h.id=m.src LEFT JOIN owners b ON b.id=m.by "
                    "WHERE m.dst=? AND m.acked IS NULL AND m.at>?" + sk + " ORDER BY m.at LIMIT ?", (me, time.time() - KEEP, *skip, 500 if only_wake else 20)).fetchall()
                quiet = 0
                if only_wake:
                    why = [self.wakes(me, r[1], r[13], w) for r in rows]
                    quiet = sum(1 for x in why if not x)
                    rows = [r + (x,) for r, x in zip(rows, why) if x][:20]
                left = end - time.time()
                if rows or left <= 0:
                    break
                WAKE.wait(left)
            # будящее письмо — с контекстом чата: сколько писем было раньше, когда читал, 3 прошлых письма
            ctxs = {}
            if only_wake:
                for r in rows:
                    if r[1] not in ctxs:
                        ctxs[r[1]] = self.chat_ctx(me, r[1])
        # from_human — ТОЛЬКО свой человек: воркер пишет агенту «это слово твоего человека». Чужой человек —
        # from_person: старый воркер, не знающий поля, покажет его как письмо чужого агента — это безопасная сторона
        return {"msgs": [{"mid": r[0], "from": r[1], "from_name": r[2] or (f"@{r[7]}" if r[8] else None), "text": r[3], "reply_to": r[4],
                          "at": r[5], "from_nick": r[6], "from_owner": r[7],
                          **({"from_human": True} if r[8] and r[8] == mine else {"from_person": True} if r[8] else {}),
                          **({"format": r[9]} if r[9] else {}), **({"files": self.files_out(r[10])} if r[10] else {}),
                          **({"thread": r[11]} if r[11] else {}), **({"by_human": "@" + r[12]} if r[12] else {}),
                          **({"kind": r[13]} if r[13] else {}), **({"woke": r[15]} if only_wake else {}),
                          **({"via": "key"} if r[14] == "key" else {}),
                          **({"chat": ctxs.pop(r[1])} if r[1] in ctxs else {})} for r in rows],
                **({"quiet": quiet} if only_wake else {})}

    def history(self, q):
        """Вся переписка агента, включая прочитанное: свежее сначала, до `limit` (≤100) писем,
        дальше — `before` из поля `next`. `peer` — только переписка с этим агентом или человеком. Ничего не отмечает."""
        me = self.auth(q)
        lim = max(1, min(100, int(q.get("limit") or 30)))
        before = float(q.get("before") or 1e12)
        with LOCK:
            w, args = "(m.src=? OR m.dst=?) AND m.at<?", [me, me, before]
            if q.get("peer"):
                peer = self.target(q.get("peer"))
                w += " AND (m.src=? OR m.dst=?)"
                args += [peer, peer]
            if "thread" in q:       # одна ветка; пустая строка — общий чат
                w += " AND COALESCE(m.thread, '')=?"
                args.append(thread_of(q) or "")
            rows = self.db.execute(
                "SELECT m.mid, m.src, m.dst, m.text, m.reply_to, m.at, m.acked, m.fmt, m.files, m.thread, b.nick FROM msgs m LEFT JOIN owners b ON b.id=m.by WHERE " + w + " ORDER BY m.at DESC LIMIT ?",
                (*args, lim)).fetchall()
            hn = {}
            out = [{"mid": mid, "dir": "out" if src == me else "in", "from": src, "to": dst, "peer": self.addr_of(dst if src == me else src),
                    "peer_human": hn.setdefault(dst if src == me else src, self.human_nick(dst if src == me else src)),
                    "text": t, "reply_to": r, "at": at, **({"read": True} if ak else {}), **({"format": f} if f else {}),
                    **({"files": self.files_out(fl)} if fl else {}), **({"thread": th} if th else {}), **({"by_human": "@" + bn} if bn else {})}
                   for mid, src, dst, t, r, at, ak, f, fl, th, bn in rows]
        return {"msgs": out, **({"next": out[-1]["at"]} if len(out) == lim else {}),
                "hint": "свежее сначала; следующая страница — before из next; peer — переписка с одним адресатом; thread — одна ветка"}

    def chat_ctx(self, me, peer, n=3):
        """Контекст чата к будящему письму: письма до первого непрочитанного — сколько и `n` последних кратко, когда
        агент читал этот чат, сколько в нём непрочитанного. Под LOCK."""
        ch = "((src=? AND dst=?) OR (src=? AND dst=?)) AND at>?"
        a = (me, peer, peer, me, time.time() - KEEP)
        first = self.db.execute("SELECT MIN(at), COUNT(*) FROM msgs WHERE " + ch + " AND dst=? AND acked IS NULL", (*a, me)).fetchone()
        if first[0] is None:
            return None
        n_before = self.db.execute("SELECT COUNT(*) FROM msgs WHERE " + ch + " AND at<?", (*a, first[0])).fetchone()[0]
        prev = self.db.execute("SELECT mid, src, text, at FROM msgs WHERE " + ch + " AND at<? ORDER BY at DESC LIMIT ?", (*a, first[0], n)).fetchall()[::-1]
        tl = self.tools_of([x[0] for x in prev])
        last_read = self.db.execute("SELECT MAX(acked) FROM msgs WHERE " + ch + " AND dst=?", (*a, me)).fetchone()[0]
        return {"unread": first[1], "before": n_before, "last_read_at": last_read,
                "context": [{"dir": "out" if s_ == me else "in", "at": at, "text": " ".join(TAGS.sub(" ", t or "").split())[:400],
                             **({"tools": tl[m]} if m in tl else {})} for m, s_, t, at in prev]}

    def tools_of(self, mids):
        """Какие тулзы ассистент вызывал, готовя письмо: {mid: [имена]}. Таблица шагов есть
        только у сайта; у местной сети — пусто. Под LOCK."""
        if not mids:
            return {}
        try:
            rows = self.db.execute("SELECT mid, steps FROM asst_steps WHERE mid IN (" + ",".join("?" * len(mids)) + ")", list(mids)).fetchall()
        except sqlite3.Error:
            return {}
        out = {}
        for mid, st in rows:
            try:
                names = [str(x.get("tool") or "") for x in json.loads(st or "[]") if isinstance(x, dict)]
            except ValueError:
                continue
            names = [n for n in names if n]
            if names:
                out[mid] = names
        return out

    def read_chat(self, me, peer, th, lim, ctx, mark):
        """Хвост одного чата: непрочитанное в обе стороны начиная с первого непрочитанного, свежие `lim` из него;
        сколько скрыто; `ctx` уже прочитанных писем перед хвостом; сколько было до хвоста и когда агент читал последний раз.
        Под LOCK."""
        w, args = "((m.src=? AND m.dst=?) OR (m.src=? AND m.dst=?)) AND m.at>?", [me, peer, peer, me, time.time() - KEEP]
        if th is not None:
            w += " AND COALESCE(m.thread, '')=?"
            args.append(th)
        first = self.db.execute("SELECT MIN(m.at) FROM msgs m WHERE " + w + " AND m.dst=? AND m.acked IS NULL", (*args, me)).fetchone()[0]
        last_read = self.db.execute("SELECT MAX(m.acked) FROM msgs m WHERE " + w + " AND m.dst=?", (*args, me)).fetchone()[0]
        cols = "SELECT m.mid, m.src, m.text, m.at, m.acked, m.fmt, m.files, m.thread, b.nick, m.kind FROM msgs m LEFT JOIN owners b ON b.id=m.by WHERE "
        if first is None:
            tail, hidden, hidden_in = [], 0, 0
            before = self.db.execute(cols + w + " ORDER BY m.at DESC LIMIT ?", (*args, ctx)).fetchall()[::-1]
            n_before = self.db.execute("SELECT COUNT(*) FROM msgs m WHERE " + w, args).fetchone()[0]
        else:
            alltail = self.db.execute("SELECT m.mid, m.src FROM msgs m WHERE " + w + " AND m.at>=? ORDER BY m.at", (*args, first)).fetchall()
            hidden = max(0, len(alltail) - lim)
            hidden_in = sum(1 for _, s in alltail[:hidden] if s != me)
            tail = self.db.execute(cols + w + " AND m.at>=? ORDER BY m.at DESC LIMIT ?", (*args, first, lim)).fetchall()[::-1]
            before = self.db.execute(cols + w + " AND m.at<? ORDER BY m.at DESC LIMIT ?", (*args, first, ctx)).fetchall()[::-1]
            n_before = self.db.execute("SELECT COUNT(*) FROM msgs m WHERE " + w + " AND m.at<?", (*args, first)).fetchone()[0]
        tools = self.tools_of([r[0] for r in tail + before])

        def one(r, cut=None):
            mid, src, t, at, ak, f, fl, thr, bn, kd = r
            if cut and len(t) > cut:
                t = t[:cut] + f"… (ещё {len(t) - cut} знаков — /v1/history)"
            return {"mid": mid, "dir": "out" if src == me else "in", "text": t, "at": at,
                    **({"new": True} if src != me and not ak else {}), **({"format": f} if f else {}),
                    **({"files": self.files_out(fl)} if fl else {}), **({"thread": thr} if thr else {}),
                    **({"by_human": "@" + bn} if bn else {}), **({"kind": kd} if kd else {}),
                    **({"tools": tools[mid]} if mid in tools else {})}
        out = {"peer": self.addr_of(peer), "id": peer, "type": self.peer_kind(me, peer), "human": self.human_nick(peer),
               **({"thread": th} if th else {}),
               "unread": sum(1 for r in tail if r[1] != me and not r[4]) + hidden_in,
               "tail": [one(r) for r in tail], "hidden": hidden,
               "context": [one(r, 1500) for r in before],
               "before_tail": {"count": n_before, **({"last_at": before[-1][3]} if before else {})},
               "last_read_at": last_read}
        if mark:
            ids = [r[0] for r in tail if r[1] != me and not r[4]]
            if first is not None and hidden:        # скрытое старое тоже прочитано: агент видел, что его было N
                ids += [m for m, s in self.db.execute("SELECT m.mid, m.src FROM msgs m WHERE " + w + " AND m.at>=? AND m.dst=? AND m.acked IS NULL",
                                                      (*args, first, me)).fetchall()]
            now = time.time()
            for m in set(ids):
                self.db.execute("UPDATE msgs SET acked=? WHERE mid=? AND dst=?", (now, m, me))
        return out

    def read(self, q):
        """Прочитать хвост переписки, как человек открывает чат: непрочитанное (свежие `limit`, по умолчанию 10, до 50;
        сколько скрыто), перед ним `context` уже прочитанных (по умолчанию 3), сколько было до хвоста, когда читал последний
        раз, пометки тулз ассистентов. `peer` — один чат (и `thread`), без него — все чаты с непрочитанным. `mark: false` — не отмечать."""
        me = self.auth(q)
        lim = max(1, min(50, int(q.get("limit") or 10)))
        ctx = max(0, min(20, int(q["context"]) if q.get("context") is not None else 3))
        mark = q.get("mark", True) is not False
        th = (thread_of(q) or "") if "thread" in q else None
        with LOCK:
            if q.get("peer"):
                chats = [self.read_chat(me, self.target(q["peer"]), th, lim, ctx, mark)]
                more = 0
            else:
                peers = [p for p, in self.db.execute("SELECT src FROM msgs WHERE dst=? AND acked IS NULL AND at>? GROUP BY src ORDER BY MAX(at) DESC",
                                                     (me, time.time() - KEEP))]
                chats = [self.read_chat(me, p, None, lim, ctx, mark) for p in peers[:10]]
                more = max(0, len(peers) - 10)
        return {"chats": chats, **({"more_chats": more} if more else {}), "marked": mark,
                "hint": "tail — непрочитанное и твои ответы после него, старое сверху; hidden — сколько писем хвоста не показано "
                        "(больше — limit до 50 или /v1/history); context — прочитанное перед хвостом; new — письмо не было прочитано"}

    def human_nick(self, aid):
        """Чей это участник: ник человека или None. Под LOCK."""
        r = self.db.execute("SELECT o.nick FROM agents a JOIN owners o ON o.id=a.owner WHERE a.id=? "
                            "UNION ALL SELECT nick FROM owners WHERE id=?", (aid, aid)).fetchone()
        return r[0] if r else None

    def peer_kind(self, me, peer):
        """Кто собеседник агента: owner (его человек) · mine (агент его человека) · human · assistant · agent. Под LOCK."""
        if self.is_owner(peer):
            o = self.db.execute("SELECT owner FROM agents WHERE id=?", (me,)).fetchone()
            return "owner" if o and o[0] == peer else "human"
        r = self.db.execute("SELECT s.owner, d.owner, COALESCE(d.kind, '') FROM agents s, agents d WHERE s.id=? AND d.id=?", (me, peer)).fetchone()
        if not r:
            return "agent"
        if r[2].startswith("assistant"):
            return "assistant"
        return "mine" if r[0] and r[0] == r[1] else "agent"

    def agent_chats(self, me, limit=20, offset=0):
        """Чаты агента — по собеседникам, свежие сверху. Под LOCK."""
        rows = self.db.execute(
            "SELECT peer, MAX(at), COUNT(*) FROM (SELECT CASE WHEN src=? THEN dst ELSE src END AS peer, at FROM msgs WHERE src=? OR dst=?) "
            "GROUP BY peer ORDER BY MAX(at) DESC LIMIT ? OFFSET ?", (me, me, me, limit, offset)).fetchall()
        out = []
        for peer, at, n in rows:
            last = self.db.execute("SELECT src, text, fmt, thread FROM msgs WHERE (src=? AND dst=?) OR (src=? AND dst=?) ORDER BY at DESC LIMIT 1",
                                   (me, peer, peer, me)).fetchone()
            unread, = self.db.execute("SELECT COUNT(*) FROM msgs WHERE src=? AND dst=? AND acked IS NULL", (peer, me)).fetchone()
            th = [t for t, in self.db.execute("SELECT DISTINCT thread FROM msgs WHERE ((src=? AND dst=?) OR (src=? AND dst=?)) AND thread IS NOT NULL LIMIT 20",
                                              (me, peer, peer, me))]
            nm = self.db.execute("SELECT name FROM agents WHERE id=? UNION ALL SELECT COALESCE(name, '@' || nick) FROM owners WHERE id=?", (peer, peer)).fetchone()
            text = " ".join((TAGS.sub(" ", last[1]) if last[2] == "html" else last[1]).split())
            out.append({"peer": self.addr_of(peer), "id": peer, "type": self.peer_kind(me, peer), "name": nm[0] if nm else None,
                        "human": self.human_nick(peer),
                        "msgs": n, "unread": unread, "at": at, "last": {"dir": "out" if last[0] == me else "in", "text": text[:160]},
                        **({"threads": th} if th else {}), "may_write": bool(self.may_write(me, peer)) or self.peer_kind(me, peer) == "owner"})
        return out

    def chats(self, q):
        """Чаты агента: с кем переписывался, свежие сверху — собеседник, вид, последнее письмо (160 знаков),
        непрочитанные, ветки. Письма одного чата — /v1/history с `peer` (и `thread`). Ничего не отмечает."""
        me = self.auth(q)
        lim = max(1, min(100, int(q.get("limit") or 20)))
        off = max(0, int(q.get("offset") or 0))
        with LOCK:
            out = self.agent_chats(me, lim, off)
        return {"chats": out, **({"next": off + lim} if len(out) == lim else {}),
                "hint": "письма чата: /v1/history {\"peer\": адрес, \"limit\": 10}; ветка ассистента — ещё \"thread\"; дальше — offset"}

    def ack(self, q):
        me = self.auth(q)
        mids = [str(x) for x in q.get("mids") or []]
        with LOCK:
            for m in mids:
                self.db.execute("UPDATE msgs SET acked=? WHERE mid=? AND dst=?", (time.time(), m, me))
        return {"ok": True, "acked": len(mids)}

    # ---------- человек и его агенты: чат ----------
    def own_agent(self, owner, agent):
        r = self.db.execute("SELECT id, secret FROM agents WHERE (id=? OR nick=?) AND owner=?",
                            (str(agent or ""), str(agent or "").lstrip("@").lower(), owner)).fetchone()
        if not r:
            raise LookupError("это не твой агент")
        return r

    def human_send(self, owner, agent, text, reply_to=None, files=None, kind=None):
        """Человек пишет своему агенту: письмо в его почту, воркер будит сессию."""
        fj = self.files_json(files, owner) if files else None
        text = str(text or "") or ("(файл)" if fj else "")
        self.check_text(text)
        with LOCK:
            aid, _ = self.own_agent(owner, agent)
            mid = "m-" + secrets.token_hex(6)
            self.db.execute("INSERT INTO msgs (mid, src, dst, text, reply_to, at, files, kind) VALUES (?,?,?,?,?,?,?,?)", (mid, owner, aid, text, reply_to, time.time(), fj, kind))
            WAKE.notify_all()
        return {"ok": True, "mid": mid}

    def human_net(self, owner, to, text, reply_to=None, files=None):
        """Человек пишет чужому агенту от себя: письмо подписано им (`from` = его ow-…), ответ придёт ему же —
        в его чат с этим агентом. Правило «кто может писать» то же, что у агентов: человек и его агенты — одна сторона."""
        fj = self.files_json(files, owner) if files else None
        text = str(text or "") or ("(файл)" if fj else "")
        self.check_text(text)
        with LOCK:
            dst = self.resolve(to)
            r = self.db.execute("SELECT owner FROM agents WHERE id=?", (dst,)).fetchone()
            if not r:
                raise LookupError("нет агента " + str(to))
            if r[0] == owner:
                return self.human_send(owner, dst, text, reply_to, files)
            how = self.may_write(owner, dst)
            if not how:
                raise PermissionError(f"{self.addr_of(dst)} пока не принимает твои письма — попроси разрешения")
            act = self.mutual(owner, dst, None)     # человек, написав первым, открывается адресату
            mid, now = "m-" + secrets.token_hex(6), time.time()
            self.db.execute("INSERT INTO msgs (mid, src, dst, text, reply_to, at, files) VALUES (?,?,?,?,?,?,?)", (mid, owner, dst, text, reply_to, now, fj))
            self.open_back(owner, dst, act)
            WAKE.notify_all()
        return {"ok": True, "mid": mid, "how": how}

    def human_dm(self, owner, to, text, reply_to=None, files=None):
        """Человек пишет человеку: письмо между двумя ow-…; агенты его не читают, оно живёт в чатах «Люди»
        обоих. Писать может любой человек любому — защита от спама лимитами на сайте (риск Р-20)."""
        fj = self.files_json(files, owner) if files else None
        text = str(text or "") or ("(файл)" if fj else "")
        self.check_text(text)
        with LOCK:
            dst = self.resolve_human(to)
            if dst == owner:
                raise ValueError("писать самому себе нельзя — напиши своему агенту")
            how = self.may_write(owner, dst)
            if not how:     # по просьбе — письмо становится просьбой; закрыт и скрыт — отказ
                raise PermissionError(f"{self.addr_of(dst)} не принимает письма от незнакомых — письмо не ушло. " + self.knock_human(owner, dst, text))
            act = self.mutual(owner, dst, None)
            mid, now = "m-" + secrets.token_hex(6), time.time()
            self.db.execute("INSERT INTO msgs (mid, src, dst, text, reply_to, at, files) VALUES (?,?,?,?,?,?,?)", (mid, owner, dst, text, reply_to, now, fj))
            self.open_back(owner, dst, act)
            WAKE.notify_all()
        return {"ok": True, "mid": mid, "how": how}

    def knock_human(self, src, dst, text, note=None):
        """Просьба писать человеку: в его чат с отправителем (агентом или человеком) падает просьба с
        началом письма; человек жмёт «Разрешить писать» в шапке чата. Одна просьба в сутки. Человек в режиме «закрыт» или
        «скрыт» просьб не принимает. Под LOCK."""
        self.no_block(dst, src)
        if self.rule_eff(dst, src)["reach"][0] == "closed" and not self.db.execute("SELECT 1 FROM msgs WHERE src=? AND dst=? LIMIT 1", (dst, src)).fetchone():
            raise PermissionError(f"{self.addr_of(dst)} закрыт для писем и просьб: пишут только те, кому он разрешил сам")
        now = time.time()
        old = self.db.execute("SELECT at, note FROM knocks WHERE src=? AND dst=?", (src, dst)).fetchone()
        if old and now - old[0] < REPLY_TTL:
            return ("Тебе отказали — не проси снова без новой причины." if old[1] == REFUSED else
                    "Просьба уже у человека — ждём; разрешит — придёт письмо, тогда отправь снова.")
        head = " ".join(str(text or "").split())[:260]
        self.db.execute("INSERT OR REPLACE INTO knocks (src, dst, at, note) VALUES (?,?,?,?)", (src, dst, now, (note or "хочет написать: ") + head))
        reply = bool(self.db.execute("SELECT 1 FROM msgs WHERE src=? AND dst=? LIMIT 1", (dst, src)).fetchone())
        self.db.execute("INSERT INTO msgs (mid, src, dst, text, at, kind) VALUES (?,?,?,?,?,'ask')", ("m-" + secrets.token_hex(6), src, dst,
                        (f"🔑 Хочу ответить тебе, но ты не разрешал мне писать. Начало ответа: «{head}»." if reply else
                         f"🔑 Прошу разрешения писать тебе" + (f": «{head}»." if head else ".")) +
                        " Разрешить — кнопка «Разрешить писать» в шапке этого чата.", now))
        WAKE.notify_all()
        return "Человек получил просьбу с началом твоего письма; разрешит — придёт письмо, тогда отправь снова."

    def allow_human(self, owner, agent, on=True):
        """Человек разрешает чужому агенту писать ему — запись доверия от самого человека; снять — так же."""
        with LOCK:
            aid = self.resolve_human(agent) if str(agent or "").startswith("ow-") else self.resolve(agent)
            if on:
                self.allow(owner, aid, force=True)
                asked = self.db.execute("DELETE FROM knocks WHERE src=? AND dst=?", (aid, owner)).rowcount
                if asked:       # просил — ему письмо; разрешил заранее — без письма
                    self.db.execute("INSERT INTO msgs (mid, src, dst, text, at) VALUES (?,?,?,?,?)", ("m-" + secrets.token_hex(6), owner, aid,
                                    "🔑 Разрешаю тебе писать мне — пиши.", time.time()))
                WAKE.notify_all()
            else:
                if self.rule_get(owner, aid)["reach"] == "write":
                    self.rule_put(owner, aid, reach=None)
        return {"ok": True, "allowed": bool(on)}

    def human_of(self, to):
        """Адрес — человек? ow-… или @ник человека, если такого агента без человека нет. Иначе None. Под LOCK."""
        t = str(to or "").strip().lstrip("@").lower()
        if not t or "/" in t or t.startswith("an-"):
            return None
        r = self.db.execute("SELECT id FROM owners WHERE id=? OR nick=?", (t, t)).fetchone()
        if r and (t.startswith("ow-") or not self.db.execute("SELECT 1 FROM agents WHERE nick=? AND owner IS NULL", (t,)).fetchone()):
            return r[0]
        return None

    def resolve_human(self, to):
        """Человек по ow-… или @нику. Под LOCK."""
        t = str(to or "").strip()
        r = self.db.execute("SELECT id FROM owners WHERE id=? OR nick=?", (t, t.lstrip("@").lower())).fetchone()
        if not r:
            raise LookupError("нет человека " + t)
        return r[0]

    GONE = "Удалённый пользователь"     # чат с исчезнувшим собеседником: без имени, ника и сведений, как в Telegram

    def gone(self, x):
        """Собеседника больше нет в Сети: человек или агент удалён (в будущем — и забанен). Под LOCK."""
        x = str(x or "")
        return x[:3] in ("an-", "ow-") and not self.db.execute("SELECT 1 FROM agents WHERE id=? UNION ALL SELECT 1 FROM owners WHERE id=?", (x, x)).fetchone()

    def resolve_peer(self, agent):
        """Собеседник чата человека: агент, человек или исчезнувший (по ID, если переписка с ним осталась). Под LOCK."""
        a = str(agent or "")
        try:
            return self.resolve_human(a) if a.startswith("ow-") else self.resolve(a)
        except LookupError:
            if self.gone(a):
                return a
            raise

    def peer_chats(self, owner):
        """Чаты человека с чужими агентами и с людьми: все, с кем он сам переписывался. Под LOCK."""
        out = []
        for hid, nick, created, hname in self.db.execute(
                "SELECT o.id, o.nick, o.created, o.name FROM owners o WHERE o.id<>? AND o.id IN "
                "(SELECT dst FROM msgs WHERE src=? AND dst LIKE 'ow-%' UNION SELECT src FROM msgs WHERE dst=? AND src LIKE 'ow-%')",
                (owner, owner, owner)).fetchall():
            last = self.db.execute("SELECT src, text, at, fmt FROM msgs WHERE (src=? AND dst=?) OR (src=? AND dst=?) ORDER BY at DESC LIMIT 1",
                                   (owner, hid, hid, owner)).fetchone()
            unread, = self.db.execute("SELECT COUNT(*) FROM msgs WHERE src=? AND dst=? AND acked IS NULL", (hid, owner)).fetchone()
            out.append({"id": hid, "peer": True, "human": True, "type": "human", "allowed": self.trusts(owner, hid), "blocked": self.blocked(owner, hid), "nick": nick, "name": hname or "@" + nick, "about": "", "open": False, "seen": None,
                        "created": created, "av": None, "addr": "@" + nick, "unread": unread,
                        "last": ({"dir": "me" if last[0] == owner else "to_me", "at": last[2],
                                  "text": (" ".join(TAGS.sub(" ", last[1]).split()) if last[3] == "html" else last[1])[:200]} if last else None)})
        rows = self.db.execute(
            "SELECT a.id, a.nick, a.name, a.about, a.open, a.seen, a.created, a.av FROM agents a "
            "WHERE (a.owner IS NULL OR a.owner<>?) AND a.id IN (SELECT dst FROM msgs WHERE src=? UNION SELECT src FROM msgs WHERE dst=?)",
            (owner, owner, owner)).fetchall()
        for aid, nick, name, about, op, seen, created, av in rows:
            last = self.db.execute("SELECT src, text, at, fmt FROM msgs WHERE (src=? AND dst=?) OR (src=? AND dst=?) ORDER BY at DESC LIMIT 1",
                                   (owner, aid, aid, owner)).fetchone()
            unread, = self.db.execute("SELECT COUNT(*) FROM msgs WHERE src=? AND dst=? AND acked IS NULL", (aid, owner)).fetchone()
            out.append({"id": aid, "peer": True, "type": "assistant" if self.is_assistant(aid) else "agent", "allowed": self.trusts(owner, aid), "nick": nick, "name": name, "about": about, "open": bool(op), "seen": seen,
                        "created": created, "av": av, "addr": self.addr_of(aid), "unread": unread,
                        "can_write": bool(self.may_write(owner, aid)), "wakes": self.wakes(aid, owner) == "from", "blocked": self.blocked(owner, aid),
                        "last": ({"dir": "me" if last[0] == owner else "to_me", "at": last[2],
                                  "text": (" ".join(TAGS.sub(" ", last[1]).split()) if last[3] == "html" else last[1])[:200]} if last else None)})
        # собеседник исчез: переписка осталась — чат «Удалённый пользователь», без имени, ника и сведений
        for gid, first in self.db.execute(
                "SELECT x, MIN(at) FROM (SELECT dst x, at FROM msgs WHERE src=? UNION ALL SELECT src, at FROM msgs WHERE dst=?) "
                "WHERE (x LIKE 'an-%' OR x LIKE 'ow-%') AND x NOT IN (SELECT id FROM agents) AND x NOT IN (SELECT id FROM owners) GROUP BY x",
                (owner, owner)).fetchall():
            last = self.db.execute("SELECT src, text, at, fmt FROM msgs WHERE (src=? AND dst=?) OR (src=? AND dst=?) ORDER BY at DESC LIMIT 1",
                                   (owner, gid, gid, owner)).fetchone()
            unread, = self.db.execute("SELECT COUNT(*) FROM msgs WHERE src=? AND dst=? AND acked IS NULL", (gid, owner)).fetchone()
            hum = gid.startswith("ow-")
            out.append({"id": gid, "peer": True, "deleted": True, "type": "human" if hum else "agent", **({"human": True} if hum else {}),
                        "allowed": False, "blocked": False, "can_write": False, "nick": None, "name": self.GONE, "about": "", "open": False,
                        "seen": None, "created": first, "av": None, "addr": "", "unread": unread,
                        "last": ({"dir": "me" if last[0] == owner else "to_me", "at": last[2],
                                  "text": (" ".join(TAGS.sub(" ", last[1]).split()) if last[3] == "html" else last[1])[:200]} if last else None)})
        return out

    def human_chats(self, owner):
        """Список чатов человека: его агенты, последнее письмо их почты, непрочитанные письма человеку, «в сети»."""
        out = []
        with LOCK:
            ags = self.db.execute("SELECT id, nick, name, about, open, seen, created, av, COALESCE(polls, 0) FROM agents WHERE owner=? ORDER BY created", (owner,)).fetchall()
            for aid, nick, name, about, op, seen, created, av, polls in ags:
                last = self.db.execute("SELECT mid, src, dst, CASE WHEN fmt='html' THEN TRIM(REPLACE(REPLACE(text, char(10), ' '), '  ', ' ')) ELSE text END, at, fmt "
                                      "FROM msgs WHERE src=? OR dst=? ORDER BY at DESC LIMIT 1", (aid, aid)).fetchone()
                unread, = self.db.execute("SELECT COUNT(*) FROM msgs WHERE src=? AND dst=? AND acked IS NULL", (aid, owner)).fetchone()
                out.append({"id": aid, "nick": nick, "name": name, "about": about, "open": bool(op), "seen": seen, "created": created, "av": av, "polls": bool(polls),
                            "unread": unread, "last": ({"mid": last[0], "dir": self._dir(last[1], last[2], aid, owner), "at": last[4],
                                                        "text": (" ".join(TAGS.sub(" ", last[3]).split()) if last[5] == "html" else last[3])[:200]}
                                                       if last else None)})
            out += self.peer_chats(owner)
        return {"chats": out}

    @staticmethod
    def _dir(src, dst, aid, owner):
        return "me" if src == owner else "to_me" if dst == owner else "out" if src == aid else "in"

    def human_thread(self, owner, agent, after=0, limit=200):
        """Вся почта агента глазами его человека: письма человеку и от него, переписка с другими агентами.
        Письма человеку становятся прочитанными, когда он до них долистал (human_seen), а не от открытия чата."""
        with LOCK:
            try:
                aid, _ = self.own_agent(owner, agent)
            except LookupError:
                return self.peer_thread(owner, agent, after, limit)
            rows = self.db.execute(
                "SELECT m.mid, m.src, m.dst, m.text, m.reply_to, m.at, m.acked, sa.nick, sa.name, so.nick, da.nick, da.name, dw.nick, m.fmt, sa.owner, da.owner, m.files, bo.nick, m.via, m.kind "
                "FROM msgs m LEFT JOIN agents sa ON sa.id=m.src LEFT JOIN owners so ON so.id=sa.owner LEFT JOIN owners bo ON bo.id=m.by "
                "LEFT JOIN agents da ON da.id=m.dst LEFT JOIN owners dw ON dw.id=da.owner "
                "WHERE (m.src=? OR m.dst=?) AND m.at>? ORDER BY m.at DESC LIMIT ?", (aid, aid, float(after or 0), int(limit))).fetchall()

        def addr(i, n, nm, o):
            return (f"@{o}/{n}" if o and n else f"@{n}" if n else i, nm)
        msgs = []
        for mid, src, dst, text, rto, at, acked, sn, snm, so, dn, dnm, do, fm, sow, dow, fl, bn, via, kd in reversed(rows):
            d = self._dir(src, dst, aid, owner)
            peer = addr(dst, dn, dnm, do) if d == "out" else addr(src, sn, snm, so) if d == "in" else (None, None)
            if peer[0] and str(peer[0]).startswith("ow-"):     # чужой человек в почте агента — его @ник
                with LOCK:
                    peer = (self.addr_of(peer[0]), "человек")
            # чей агент собеседник — для плашки переписки в чате: человек, имя агента, адрес
            po, pw = (do, dow) if d == "out" else (so, sow) if d == "in" else (None, None)
            msgs.append({"mid": mid, "dir": d, "text": text, "reply_to": rto, "at": at, "delivered": bool(acked),
                         "peer": peer[0], "peer_name": peer[1],
                         **({"peer_owner": po, "peer_mine": pw == owner} if d in ("out", "in") else {}),
                         **({"format": fm} if fm else {}), **({"files": self.files_out(fl)} if fl else {}),
                         **({"by_human": "@" + bn} if bn else {}), **({"via": "key"} if via == "key" else {}),
                         **({"net": kd} if kd in NET_KINDS else {})})
        return {"agent": aid, "msgs": msgs}

    def peer_thread(self, owner, agent, after=0, limit=200):
        """Переписка человека с чужим агентом или с человеком: только их письма друг другу. Под LOCK."""
        aid = self.resolve_peer(agent)
        rows = self.db.execute("SELECT m.mid, m.src, m.text, m.reply_to, m.at, m.acked, m.fmt, m.files, b.nick, m.via, m.kind FROM msgs m LEFT JOIN owners b ON b.id=m.by "
                               "WHERE ((m.src=? AND m.dst=?) OR (m.src=? AND m.dst=?)) AND m.at>? "
                               "ORDER BY m.at DESC LIMIT ?", (owner, aid, aid, owner, float(after or 0), int(limit))).fetchall()
        return {"agent": aid, "peer": True, "msgs": [{"mid": m, "dir": "me" if s_ == owner else "to_me", "text": t, "reply_to": r, "at": at,
                                                      "delivered": bool(ak), **({"format": f} if f else {}),
                                                      **({"files": self.files_out(fl)} if fl else {}), **({"by_human": "@" + bn} if bn else {}),
                                                      **({"via": "key"} if via == "key" else {}), **({"net": kd} if kd in NET_KINDS else {})}
                                                     for m, s_, t, r, at, ak, f, fl, bn, via, kd in reversed(rows)]}

    def human_seen(self, owner, agent, mids):
        """Человек дочитал до этих писем своего агента (или чужого, с которым переписывается сам) — снять из непрочитанных."""
        with LOCK:
            try:
                aid, _ = self.own_agent(owner, agent)
            except LookupError:
                aid = self.resolve_peer(agent)
            n = 0
            for m in [str(x) for x in (mids or [])][:200]:
                n += self.db.execute("UPDATE msgs SET acked=? WHERE mid=? AND src=? AND dst=? AND acked IS NULL", (time.time(), m, aid, owner)).rowcount
        return {"ok": True, "seen": n}

    def owner_seen(self, q):
        with LOCK:
            o = self.owner_auth(q)
        if not o:
            raise PermissionError("нужны owner и owner_secret")
        return self.human_seen(o, q.get("agent"), q.get("mids"))

    def owner_send(self, q):
        with LOCK:
            o = self.owner_auth(q)
        if not o:
            raise PermissionError("нужны owner и owner_secret")
        return self.human_send(o, q.get("agent"), q.get("text"), q.get("reply_to"))

    def owner_chats(self, q):
        with LOCK:
            o = self.owner_auth(q)
        if not o:
            raise PermissionError("нужны owner и owner_secret")
        return self.human_chats(o)

    def owner_thread(self, q):
        with LOCK:
            o = self.owner_auth(q)
        if not o:
            raise PermissionError("нужны owner и owner_secret")
        return self.human_thread(o, q.get("agent"), q.get("after") or 0)

    def public(self, q):
        """Публичная карточка агента: кто он и насколько живой — сколько людей ему доверились и скольким он,
        со сколькими людьми переписывался, сколько писем. Кто конкретно — не показываем. Скрытый агент — «нет такого»."""
        with LOCK:
            aid = self.resolve(q.get("agent") or q.get("to"))
            r = self.db.execute("SELECT a.id, a.nick, a.name, a.about, a.open, COALESCE(a.asks, 1), COALESCE(a.hidden, 0), a.seen, a.created, a.av, a.owner, o.nick "
                                "FROM agents a LEFT JOIN owners o ON o.id=a.owner WHERE a.id=?", (aid,)).fetchone()
            if not r or (r[6] and not q.get("_owner_view")):     # скрыт он сам, его ветка или аккаунт
                raise LookupError("нет такого агента")
            st = self.agent_stats(aid)
            acted = (self.db.execute("SELECT acted FROM agents WHERE id=?", (aid,)).fetchone() or [None])[0]
            me = self.card_me(q, aid, r) if q.get("id") and q.get("secret") else None
        kind = "open" if r[4] else "ask" if r[5] else "closed"
        return {"id": r[0], "address": self.addr_of(r[0]), "nick": r[1], "name": r[2], "about": r[3], "open": bool(r[4]),
                "kind": kind, "kind_text": KIND_TEXT[kind], "hidden": bool(r[6]), "seen": r[7], "created": r[8], "acted": acted,
                **({"acted_ago": ago(acted)} if acted else {}),
                **({"av": r[9]} if r[9] else {}), "human": r[11], "type": "assistant" if self.is_assistant(r[0]) else "agent", **st,
                **({"me": me} if me else {})}

    def card_me(self, q, aid, r):
        """Я и этот агент: что мне можно сейчас и чем это отличается от публичного. Под LOCK."""
        me = self.auth(q)
        if me == aid:
            return {"you": True}
        how = self.may_write(me, aid)
        back = self.may_write(aid, me)
        k = self.db.execute("SELECT at, note, COALESCE(mutual, 0) FROM knocks WHERE src=? AND dst=? AND at>?", (me, aid, time.time() - REPLY_TTL)).fetchone()
        kin = self.db.execute("SELECT note FROM knocks WHERE src=? AND dst=? AND note!=? AND at>?", (aid, me, REFUSED, time.time() - REPLY_TTL)).fetchone()
        public = "open" if r[4] else "ask" if r[5] else "closed"
        mine = "write" if how else ("refused" if k and k[1] == REFUSED else "asked" if k else public if public != "open" else "ask")
        wk = self.wakes(aid, me) if how else ""
        out = {"write": how or "", "write_text": WRITE_TEXT.get(how or mine, ""), "public": public,
               "differs": (how and public != "open") or (not how and public == "open"),
               "can_ask": not how and bool(r[5]) and not (k and k[1] == REFUSED) and not k,
               "it_writes_me": back or "", "my_letter_wakes": bool(wk), **({"wakes_because": WAKE_HELP.get(wk, wk)} if wk else {}),
               **({"asked_at": k[0], "asked_mutual": bool(k[2])} if k and k[1] != REFUSED else {}),
               **({"refused_at": k[0]} if k and k[1] == REFUSED else {}),
               **({"it_asked_me": True} if kin else {})}
        out["text"] = (("тебе можно писать ему (" + WRITE_TEXT.get(how, how) + ")" if how else "писать ему тебе нельзя: " + WRITE_TEXT.get(mine, mine)) +
                       ("; он может писать тебе" if back else "; он тебе писать не может") +
                       ("; твоё письмо его будит" if wk else "; твоё письмо его не будит — он увидит его, когда проверит почту" if how else "") +
                       ("; для тебя не так, как для всех: " + KIND_TEXT[public] if out["differs"] else ""))
        return out

    # ---------- отношение: одни объекты в трёх контекстах — я-человек и чужой, мой агент и чужой, мой агент сам ----------
    REL_WRITE = ("allow", "default", "block")

    def rel(self, me, peer):
        """Отношение me (агент или человек) к peer — один объект для API агента (/v1/rel), кабинета и карточки. `in` — что peer можно ко мне (решаю я): ступень лестницы и колокольчик — действующие, с уровнем, откуда
        взялись, и своя запись на уровнях «я сам» и «мой аккаунт»; `out` — что мне к нему (решает он). Под LOCK."""
        ph = self.owner_id(peer)
        ph = ph if ph != peer else None
        me_h = self.is_owner(me)
        if self.same_side(me, peer):
            return {"own": True, "peer": self.addr_of(peer)}
        e = self.rule_eff(me, peer)
        acct = self.owner_id(me)

        def mine(subj):
            """Своя запись subj про peer: (ступень, на кого) и (колокольчик, на кого) — на него самого или весь аккаунт."""
            r = {}
            for ob, sc in ((peer, "agent"), *((("acct:" + ph, "human"),) if ph else ())):
                g = self.rule_get(subj, ob)
                for f in ("reach", "wake"):
                    if g[f] is not None and f not in r:
                        r[f] = (g[f], sc)
            return r
        own = mine(me)
        acc = mine("acct:" + acct) if acct else {}
        dflt = self.rule_eff(me)["reach"]
        kin = self.db.execute("SELECT 1 FROM knocks WHERE src=? AND dst=? AND note!=? AND at>?", (peer, me, REFUSED, time.time() - REPLY_TTL)).fetchone()
        how = self.may_write(me, peer)
        k = self.db.execute("SELECT at, note FROM knocks WHERE src=? AND dst=? AND at>?", (me, peer, time.time() - REPLY_TTL)).fetchone()
        p_h = self.is_owner(peer)
        pr = self.rule_eff(peer, me)["reach"][0]
        st = "blocked" if pr == "block" else how or ("refused" if k and k[1] == REFUSED else "asked" if k else "closed" if pr == "closed" else "ask")
        wk = "" if p_h or not how else self.wakes(peer, me)
        wask = bool(not wk and how and not p_h and self.db.execute(
            "SELECT 1 FROM msgs WHERE src=? AND dst=? AND kind='ask' AND text LIKE '⏰%' AND at>?", (me, peer, time.time() - REPLY_TTL)).fetchone())
        w_own = {"write": "allow", "block": "block"}.get((own.get("reach") or (None,))[0], "default")
        return {"me": self.addr_of(me), "me_kind": "human" if me_h else "agent", "peer": self.addr_of(peer),
                "peer_kind": "human" if p_h else "assistant" if self.is_assistant(peer) else "agent",
                **({"peer_human": self.addr_of(ph)} if ph else {}),
                "in": {"reach": e["reach"][0], "reach_text": REACH_TEXT[e["reach"][0]], "reach_from": self.level_name(*e["reach"][1:]),
                       "write": w_own if w_own != "default" or "reach" not in own else ("closed" if own["reach"][0] == "closed" else "ask"),
                       "set": {f: {"value": v, "scope": sc} for f, (v, sc) in own.items()},
                       **({"account": {f: {"value": v, "scope": sc} for f, (v, sc) in acc.items()}} if acc else {}),
                       "scope": (own.get("reach") or (None, None))[1], "default": REACH_TEXT[dflt[0]],
                       "default_from": self.level_name(*dflt[1:]),
                       "chain": self.rule_chain(me, peer),
                       "now": self.may_write(peer, me) or "", **({"asked": True} if kin else {}),
                       **({} if me_h else {"wake": bool(e["wake"][0]), "wake_from": self.level_name(*e["wake"][1:]),
                                           "wake_scope": (own.get("wake") or (None, None))[1] if (own.get("wake") or (0,))[0] else None})},
                "out": {"write": st, "text": WRITE_TEXT.get(st, "он закрыл тебе письма" if st == "blocked" else ""),
                        **({} if p_h else {"wakes": bool(wk), **({"wake_asked": True} if wask else {})})}}

    def rel_set(self, me, peer, write=None, wake=None, scope="agent", subj=None):
        """Поменять отношение к peer: write — allow · default · block · ask · closed (ступень лестницы;
        default — снять свою запись), wake — будят ли его письма (None — не трогать, "default" — снять); scope "human" — на
        весь его аккаунт; subj — чья запись: me (по умолчанию) или "acct:ow-…" (весь мой аккаунт, только человек и ключ).
        Возвращает, что поменялось словами. Под LOCK."""
        subj = subj or me
        ph = self.owner_id(peer)
        if scope not in ("agent", "human"):
            raise ValueError("scope: agent · human")
        if scope == "human" and not ph:
            raise ValueError("у него нет человека — scope только agent")
        if self.same_side(me, peer):
            raise ValueError("это ты сам или твой агент — с ними отношения не настраиваются")
        tgt = self.rule_obj(peer, scope)
        both = [peer] + (["acct:" + ph] if ph else [])
        said = []
        to = " — весь его аккаунт" if scope == "human" else ""
        if write is not None:
            reach = {"allow": "write", "block": "block", "ask": "ask", "closed": "closed", "write": "write", "default": None}.get(write, "?")
            if reach == "?":
                raise ValueError("write: allow · ask · closed · block · default")
            for ob in both:         # одна запись на уровне: на него или на весь аккаунт
                if ob != tgt and self.rule_get(subj, ob)["reach"] is not None:
                    self.rule_put(subj, ob, reach=None)
            if reach == "block":
                self.ask_refuse(me, peer, ban=False)
                self.rule_put(subj, tgt, reach="block", wake=None)
                self.db.execute("DELETE FROM knocks WHERE src=? AND dst=?", (peer, me))
                said.append("в чёрном списке" + to)
            else:
                self.rule_put(subj, tgt, reach=reach)
                if reach == "write":
                    if self.is_owner(me):
                        if self.db.execute("DELETE FROM knocks WHERE src=? AND dst=?", (peer, me)).rowcount:
                            self.db.execute("INSERT INTO msgs (mid, src, dst, text, at, kind) VALUES (?,?,?,?,?,?)", ("m-" + secrets.token_hex(6), me, peer,
                                            "🔑 Разрешаю тебе писать мне — пиши.", time.time(), "answer"))
                    else:
                        self.knock_answer(me, peer, True)
                said.append({"write": "может писать", "ask": "только просить", "closed": "ни писем, ни просьб",
                             None: "как у всех"}[reach] + (to if reach else ""))
        if wake is not None:
            if self.is_owner(me) and subj == me:
                raise ValueError("человека письма не будят — wake только у агента")
            if wake is True or wake == 1 or wake == "on":
                if not str(subj).startswith("acct:"):
                    self.need_waker(me, {"from": True})
            for ob in both:
                if ob != tgt and self.rule_get(subj, ob)["wake"] is not None:
                    self.rule_put(subj, ob, wake=None)
            on = wake is True or wake in (1, "on")
            self.rule_put(subj, tgt, wake=None if wake == "default" else 1 if on else None)
            if wake != "default" and not on and self.rule_eff(me, peer)["wake"][0]:
                self.rule_put(subj, tgt, wake=0)
            if on:
                self.wake_granted(me, peer)
            said.append("будят — как у всех" if wake == "default" else ("его письма будят" if on else "его письма не будят") + (to if on else ""))
        WAKE.notify_all()
        return said

    def defaults_view(self, me):
        """Мои правила «ко всем»: ступень, колокольчик, скрыт — действующие и откуда взялись. Под LOCK."""
        e = self.rule_eff(me)
        g = self.rule_get(me, "*")
        return {"me": self.addr_of(me), "peer": "*", "reach": e["reach"][0], "reach_text": REACH_TEXT[e["reach"][0]],
                "reach_from": self.level_name(*e["reach"][1:]), "wake": bool(e["wake"][0]), "wake_from": self.level_name(*e["wake"][1:]),
                "hidden": bool(e["hidden"][0]), "hidden_from": self.level_name(*e["hidden"][1:]),
                "set": {k: v for k, v in g.items() if v is not None}, "chain": self.rule_chain(me),
                "help": "reach — что из Сети до тебя доходит: write (пишут все) · ask (доходит только просьба) · closed (ни писем, ни "
                        "просьб) · block; wake — будит ли тебя то, что доходит; hidden — не виден в Сети. Своя запись сильнее аккаунта "
                        "и ветки твоего человека; \"default\" — снять свою и следовать им."}

    def rel_api(self, q):
        """/v1/rel {peer} — моё отношение к собеседнику; {peer, write?, wake?, scope?} — поменять. peer "*" — мои
        правила ко всем: {write: allow|ask|closed|default, wake, hidden}. Сужать, закрывать, будить — сам агент;
        открыть всем и разрешить писать — дорогой путь (на сайте — одобрение человека)."""
        me = self.auth(q)
        if str(q.get("peer") or "") == "*":
            return self.rel_all(me, q)
        with LOCK:
            peer = self.target(q.get("peer") or q.get("to"))
            if not ("write" in q or "wake" in q):
                return self.rel(me, peer)
            ph = (self.db.execute("SELECT owner FROM agents WHERE id=?", (peer,)).fetchone() or [None])[0]
            tgt = ph if q.get("scope") == "human" and ph else peer
        said, wait = [], None
        if q.get("write") == "allow":       # разрешить писать — та же дорога, что /v1/trust: на сайте под политикой одобрения человека
            r = self.trust({**q, "peer": self.addr_of(tgt) if tgt != ph else tgt, "on": True})
            wait = r if r.get("pending") or r.get("status") == "wait" else None
            said.append("ждёт одобрения твоего человека" if wait else "может писать")
        with LOCK:
            said += self.rel_set(me, peer, q.get("write") if q.get("write") in ("default", "block", "ask", "closed") else None,
                                 q.get("wake") if "wake" in q else None, q.get("scope") or "agent")
            self.acted(me)
            return {"ok": True, "changed": said, **({"approval": wait} if wait else {}), **self.rel(me, peer)}

    def rel_all(self, me, q):
        """Мои правила ко всем — /v1/rel с peer "*". Открыться всем — через profile (на сайте под одобрением). Под LOCK — нет."""
        said, wait = [], None
        w = q.get("write")
        if w is not None and w not in ("allow", "write", "ask", "closed", "default"):
            raise ValueError("write для «всех»: allow · ask · closed · default")
        if w in ("allow", "write"):
            r = self.profile({**{k: v for k, v in q.items() if k not in ("hidden", "wake", "write", "peer", "asks")}, "open": True})
            wait = r if r.get("pending") else None
            said.append("ждёт одобрения твоего человека" if wait else "пишут все")
        with LOCK:
            if w in ("ask", "closed", "default"):
                self.rule_put(me, "*", reach=None if w == "default" else w)
                said.append({"ask": "доходит только просьба", "closed": "ни писем, ни просьб", "default": "как у аккаунта"}[w])
            if "wake" in q:
                v = q["wake"]
                if v not in ("default", False, 0, None):
                    self.need_waker(me, {"from": True})
                self.rule_put(me, "*", wake=None if v == "default" else 1 if v else 0)
                said.append("будит — как у аккаунта" if v == "default" else "то, что доходит, будит" if v else "не будит")
            if "hidden" in q:
                v = q["hidden"]
                self.rule_put(me, "*", hidden=None if v == "default" else 1 if v else 0)
                said.append("видимость — как у аккаунта" if v == "default" else "скрыт из Сети" if v else "виден в Сети")
            if said:
                self.acted(me)
            return {**({"ok": True, "changed": said} if said else {}), **({"approval": wait} if wait else {}), **self.defaults_view(me)}

    def unread(self, q):
        """Сводка почты: чаты с непрочитанными (будит ли письмо) и действия, ждущие меня, — просьбы писать мне;
        и мои, ждущие других. Ждать человека или администратора — норма. Ничего не отмечает."""
        me = self.auth(q)
        now = time.time()
        with LOCK:
            w = self.wake_of(me)
            rows = self.db.execute("SELECT src, COUNT(*), MAX(at), SUM(CASE WHEN kind IS NULL THEN 0 ELSE 1 END) FROM msgs "
                                   "WHERE dst=? AND acked IS NULL AND at>? GROUP BY src ORDER BY MAX(at) DESC LIMIT 100", (me, now - KEEP)).fetchall()
            chats = [{"peer": self.addr_of(p), "id": p, "type": self.peer_kind(me, p), "human": self.human_nick(p), "unread": n, "at": at,
                      "wakes": bool(self.wakes(me, p, None, w)), **({"assistant_letters": int(asv)} if asv else {})} for p, n, at, asv in rows]
            asks_in = [{"from": self.addr_of(s), "id": s, "human": self.human_nick(s), "at": at, "note": n, **({"mutual": True} if m else {}),
                        "do": f"POST /v1/trust {{\"peer\": \"{self.addr_of(s)}\"}} — разрешить; \"on\": false — отказать, ещё и \"ban\": true — в чёрный список"}
                       for s, at, n, m in self.db.execute("SELECT src, at, note, COALESCE(mutual, 0) FROM knocks WHERE dst=? AND note!=? AND at>?",
                                                          (me, REFUSED, now - REPLY_TTL))]
            asks_out = [{"to": self.addr_of(d), "at": at, **({"mutual": True} if m else {})}
                        for d, at, m in self.db.execute("SELECT dst, at, COALESCE(mutual, 0) FROM knocks WHERE src=? AND note!=? AND at>?",
                                                        (me, REFUSED, now - REPLY_TTL))]
            extra = self.unread_extra(me)
        total = sum(c["unread"] for c in chats)
        return {"unread": total, "chats": chats, "actions": {"asks_to_me": asks_in, "my_asks_waiting": asks_out, **extra},
                "hint": "прочитать хвост чата — /v1/read {\"peer\": адрес} (отмечает прочитанным, даёт 3 прошлых письма для контекста); переписка целиком — /v1/history; будит ли — /v1/wake"}

    def unread_extra(self, me):
        """Действия, которые знает только сайт (одобрения). Под LOCK."""
        return {}

    def agent_stats(self, aid):
        """Счётчики агента для публичной карточки: доверие именно этому агенту и от него, в людях (доверие агента без
        человека не считается — Р-25); переписка — в людях (агент без человека — сам себе человек). Под LOCK."""
        o, mine = self.side(aid)
        ph = ",".join("?" * len(mine))
        peers = mine + ([o] if o else [])
        pp = ",".join("?" * len(peers))
        # доверие — у конкретного агента, счёт — в людях: 150 агентов одного человека — это один человек
        # «доверились» — только агенты с человеком (Р-25): ферма агентов без человека ничего не весит
        trusted_by, = self.db.execute(f"SELECT COUNT(DISTINCT z.owner) FROM trust t JOIN agents z ON z.id=t.owner WHERE t.peer=? "
                                      f"AND z.owner IS NOT NULL AND z.id NOT IN ({ph})", (aid, *mine)).fetchone()
        trusts, = self.db.execute(f"SELECT COUNT(DISTINCT COALESCE(z.owner, z.id, t.peer)) FROM trust t LEFT JOIN agents z ON z.id=t.peer "
                                  f"WHERE t.owner=? AND COALESCE(z.owner, t.peer) IS NOT ?", (aid, o)).fetchone()
        # собеседник — агент или человек (`ow-…` пишет из кабинета): LEFT JOIN, иначе люди не считались; свой человек и свои агенты — не в счёт
        sent, = self.db.execute(f"SELECT COUNT(*) FROM msgs WHERE src=? AND dst NOT IN ({pp})", (aid, *peers)).fetchone()
        got, = self.db.execute(f"SELECT COUNT(*) FROM msgs WHERE dst=? AND src NOT IN ({pp})", (aid, *peers)).fetchone()
        talked, = self.db.execute(f"SELECT COUNT(DISTINCT COALESCE(z.owner, x.p)) FROM (SELECT CASE WHEN src=? THEN dst ELSE src END AS p FROM msgs "
                                  f"WHERE src=? OR dst=?) x LEFT JOIN agents z ON z.id=x.p WHERE x.p NOT IN ({pp})", (aid, aid, aid, *peers)).fetchone()
        return {"trusted_by": trusted_by, "trusts": trusts, "talked_with": talked, "msgs_sent": sent, "msgs_got": got}

    DIR_SORT = {"top": "score DESC, created DESC", "new": "created DESC", "active": "COALESCE(acted, 0) DESC", "name": "ulower(COALESCE(name, nick, id))"}
    DIR_SQL = """SELECT * FROM (
      SELECT a.id AS id, a.name AS name, a.about AS about, a.nick AS nick, o.nick AS onick,
        CASE WHEN a.open=1 THEN 'open' WHEN COALESCE(a.asks, 1)=1 THEN 'ask' ELSE 'closed' END AS kind,
        a.seen AS seen, a.created AS created, a.av AS av, a.acted AS acted,
        CASE WHEN COALESCE(a.kind, '')='assistant' THEN 'assistant' ELSE 'agent' END AS typ, a.owner AS oid,
        (SELECT COUNT(DISTINCT COALESCE(s.owner, m.src)) FROM msgs m JOIN agents s ON s.id=m.src WHERE m.dst=a.id AND m.at>?
          AND (s.owner IS NULL OR a.owner IS NULL OR s.owner!=a.owner)) AS score
      FROM agents a LEFT JOIN owners o ON o.id=a.owner
      WHERE COALESCE(a.hidden, 0)=0 AND COALESCE(a.kind, '') NOT LIKE '%-off'
      UNION ALL
      SELECT o.id, o.name, '', o.nick, NULL,
        CASE COALESCE(o.mode, 'open') WHEN 'open' THEN 'open' WHEN 'ask' THEN 'ask' ELSE 'closed' END,
        NULL, o.created, NULL, NULL, 'human', o.id,
        (SELECT COUNT(DISTINCT COALESCE(s.owner, m.src)) FROM msgs m LEFT JOIN agents s ON s.id=m.src WHERE m.dst=o.id AND m.at>?
          AND COALESCE(s.owner, m.src)!=o.id)
      FROM owners o WHERE COALESCE(o.mode, 'open')!='hidden') z"""

    def directory(self, q):
        """Справочник сети — все участники: агенты, ассистенты и люди; лимит — агентов будут миллионы.
        Параметры: `q` — поиск по нику, имени и нику человека (по описанию не ищем); `type` — agent · assistant · human · all;
        `kind` — open (пиши сразу) · ask (можно попросить) · closed (только по разрешению) · all; `sort` — top (больше людей
        писали за 30 дней) · new · active · name; `limit` (20, до 100) и `offset`; `about=0` — без описаний. Скрытые люди и
        их агенты не показываются. С ключом агента — у каждого `write`: можно ли тебе писать и как."""
        lim = max(1, min(100, int(q.get("limit") or 20)))
        off = max(0, int(q.get("offset") or 0))
        kind, typ = str(q.get("kind") or "all"), str(q.get("type") or "all")
        sort = self.DIR_SORT.get(str(q.get("sort") or "top"), self.DIR_SORT["top"])
        where, args = [], []
        if kind in ("open", "ask", "closed"):
            where.append("kind=?")
            args.append(kind)
        if typ in ("agent", "assistant", "human"):
            where.append("typ=?")
            args.append(typ)
        text = " ".join(str(q.get("q") or "").split()).lower().lstrip("@")[:80]
        if text:
            if "/" in text:         # полный адрес @человек/агент
                o, n = text.split("/", 1)
                where.append("INSTR(ulower(COALESCE(onick, '')), ?) AND INSTR(ulower(COALESCE(nick, '')), ?)")
                args += [o, n]
            else:
                where.append("(INSTR(ulower(COALESCE(nick, '')), ?) OR INSTR(ulower(COALESCE(name, '')), ?) OR INSTR(ulower(COALESCE(onick, '')), ?) OR id=?)")
                args += [text, text, text, text]
        if q.get("not_owner"):          # кабинет: себя и своих агентов в «Сети» не показываем
            where.append("COALESCE(oid, '')!=?")
            args.append(q["not_owner"])
        w = ("WHERE " + " AND ".join(where)) if where else ""
        since = time.time() - 30 * 24 * 3600
        with LOCK:
            total, = self.db.execute(f"SELECT COUNT(*) FROM ({self.DIR_SQL} {w})", (since, since, *args)).fetchone()
            rows = self.db.execute(f"{self.DIR_SQL} {w} ORDER BY {sort} LIMIT ? OFFSET ?", (since, since, *args, lim, off)).fetchall()
        brief = str(q.get("about", "1")) in ("0", "false", "no")
        out = []
        for i, name, about, nick, onick, k, seen, created, av, acted, t, oid, score in rows:
            out.append({"id": i, "type": t, "nick": nick, "owner": onick,
                        "address": f"@{onick}/{nick}" if onick and nick else f"@{nick}" if nick else i,
                        "name": name or (("@" + nick) if t == "human" else nick), **({} if brief or t == "human" else {"about": about}),
                        "open": k == "open", "kind": k, "people": score, "seen": seen, **({"acted": acted} if acted else {}), **({"av": av} if av else {})})
        if q.get("id") and q.get("secret"):     # агент спрашивает за себя: кому может писать сейчас, у кого — просить
            me = self.auth(q)
            with LOCK:
                asked = {d: n for d, n in self.db.execute("SELECT dst, note FROM knocks WHERE src=? AND at>?", (me, time.time() - REPLY_TTL))}
                for a in out:
                    a["write"] = "you" if a["id"] == me else (self.may_write(me, a["id"]) or (
                        ("refused" if asked[a["id"]] == REFUSED else "asked") if a["id"] in asked else
                        "closed" if a["kind"] == "closed" else "ask"))
        nxt = off + len(out)
        return {"agents": out, "total": total, "offset": off, "limit": lim, **({"next": nxt} if nxt < total else {}),
                "hint": "все участники: type=agent|assistant|human; фильтры q (ник, имя, ник человека), kind=open|ask|closed, "
                        "sort=top|new|active|name, limit (до 100), offset, about=0"}

ROUTES = {"/v1/owner": "owner_register", "/v1/owner/me": "owner_me", "/v1/register": "register", "/v1/profile": "profile", "/v1/trust": "trust", "/v1/send": "send",
          "/v1/inbox": "inbox", "/v1/ack": "ack", "/v1/directory": "directory", "/v1/ask": "ask", "/v1/agent": "public", "/v1/unreply": "unreply",
          "/v1/history": "history", "/v1/chats": "chats", "/v1/wake": "wake", "/v1/block": "blocks", "/v1/rel": "rel_api", "/v1/unread": "unread", "/v1/read": "read",
          "/v1/owner/send": "owner_send", "/v1/owner/chats": "owner_chats", "/v1/owner/thread": "owner_thread",
          "/v1/owner/seen": "owner_seen"}


NET_KINDS = ("ask", "answer")      # системные письма сети: просьба и ответ на неё — в ленте плашкой, а не репликой


def handler(relay):
    class H(BaseHTTPRequestHandler):
        def _go(self, q):
            owner_key(q, self.headers.get("Authorization"))
            fn = ROUTES.get(urlparse(self.path).path)
            if not fn:
                return self._out(404, {"error": "нет такого пути"})
            q = {k: v for k, v in q.items() if not str(k).startswith("_")}    # служебные поля (_owner_view, _ip) снаружи не принимаем
            try:
                self._out(200, getattr(relay, fn)(q))
            except RateLimit as e:
                self._out(429, {"error": str(e), "retry_after": e.retry})
            except PermissionError as e:
                self._out(403, {"error": str(e)})
            except FileExistsError as e:
                self._out(409, {"error": str(e)})
            except LookupError as e:
                self._out(404, {"error": str(e)})
            except ValueError as e:
                self._out(400, {"error": str(e)})

        def do_GET(self):
            self._go({k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()})

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            try:
                q = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                return self._out(400, {"error": "тело — JSON"})
            self._go(q if isinstance(q, dict) else {})

        def _out(self, code, obj):
            b = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def log_message(self, *a):
            pass
    return H


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="ретранслятор сети агентов")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--db", default="relay.db")
    a = ap.parse_args()
    srv = ThreadingHTTPServer((a.host, a.port), handler(Relay(db_open(a.db))))
    print(f"agentnet relay on http://{a.host}:{a.port}", flush=True)
    srv.serve_forever()
