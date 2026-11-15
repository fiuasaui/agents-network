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
    if "seen" not in cols:          # когда агент последний раз забирал почту — «в сети» в кабинете
        c.execute("ALTER TABLE agents ADD COLUMN seen REAL")
    if "asks" not in cols:          # принимает ли просьбы «разреши писать»: 0 — закрыт совсем, доверие даёт только сам
        c.execute("ALTER TABLE agents ADD COLUMN asks INTEGER DEFAULT 1")
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
    return c


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
            self.db.execute("INSERT INTO owners VALUES (?,?,?,?)", (oid, sec, n, time.time()))
        return {"owner": oid, "owner_secret": sec, "nick": n}

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
        with LOCK:
            owner = self.owner_auth(q)
            if q.get("nick"):
                norm_nick(q["nick"])    # проверить до записи
            self.db.execute("INSERT INTO agents (id, secret, name, about, open, created, owner) VALUES (?,?,?,?,0,?,?)",
                            (aid, sec, name, str(q.get("about") or "")[:4000], time.time(), owner))
            try:
                nick = self.take_nick(aid, q["nick"]) if q.get("nick") else None
            except FileExistsError:
                self.db.execute("DELETE FROM agents WHERE id=?", (aid,))
                raise
        return {"id": aid, "secret": sec, "name": name, "nick": nick}

    def profile(self, q):
        me = self.auth(q)
        with LOCK:
            if "name" in q:
                self.db.execute("UPDATE agents SET name=? WHERE id=?", (str(q["name"])[:80], me))
            if "about" in q:        # описание — роль и компетенции, может быть большим
                self.db.execute("UPDATE agents SET about=? WHERE id=?", (str(q["about"])[:4000], me))
            if "open" in q:
                self.db.execute("UPDATE agents SET open=? WHERE id=?", (1 if q["open"] else 0, me))
            if "asks" in q:
                self.db.execute("UPDATE agents SET asks=? WHERE id=?", (1 if q["asks"] else 0, me))
            if q.get("nick"):
                self.take_nick(me, q["nick"])
            r = self.db.execute("SELECT id, name, about, open, nick FROM agents WHERE id=?", (me,)).fetchone()
            trusts = [x[0] for x in self.db.execute("SELECT peer FROM trust WHERE owner=?", (me,))]
        return {"id": r[0], "nick": r[4], "name": r[1], "about": r[2], "open": bool(r[3]), "trusts": trusts}

    def trust(self, q):
        me = self.auth(q)
        with LOCK:
            raw = str(q.get("peer") or "").strip().lstrip("@").lower()
            ow = self.db.execute("SELECT id FROM owners WHERE id=? OR nick=?", (raw, raw)).fetchone() if "/" not in raw and not raw.startswith("an-") else None
            if ow and not self.db.execute("SELECT 1 FROM agents WHERE nick=?", (raw,)).fetchone():
                peer = ow[0]    # доверие человеку — всем его агентам
                if q.get("on", True):
                    self.db.execute("INSERT OR REPLACE INTO trust VALUES (?,?,?)", (me, peer, time.time()))
                else:
                    self.untrust(me, peer)
                return {"ok": True, "peer": peer, "owner": True, "trusted": bool(q.get("on", True))}
            peer = self.resolve(q.get("peer"))
            if not self.db.execute("SELECT 1 FROM agents WHERE id=?", (peer,)).fetchone():
                raise LookupError("нет агента " + peer)
            if q.get("on", True):
                self.db.execute("INSERT OR REPLACE INTO trust VALUES (?,?,?)", (me, peer, time.time()))
                self.knock_answer(me, peer, True)
            else:
                self.untrust(me, peer)
        return {"ok": True, "peer": peer, "trusted": bool(q.get("on", True)),
                "note": "доверие — от человека человеку: все агенты обеих сторон пишут друг другу"}

    def addr_of(self, aid):
        """Адрес агента словами: @человек/ник, @ник или ID."""
        r = self.db.execute("SELECT a.nick, o.nick FROM agents a LEFT JOIN owners o ON o.id=a.owner WHERE a.id=?", (aid,)).fetchone()
        return (f"@{r[1]}/{r[0]}" if r[1] and r[0] else f"@{r[0]}" if r[0] else aid) if r else aid

    def note_to(self, src, dst, text):
        """Служебное письмо сети между агентами — мимо правила «кто может писать» (ответ на просьбу разрешения)."""
        self.db.execute("INSERT INTO msgs (mid, src, dst, text, at) VALUES (?,?,?,?,?)", ("m-" + secrets.token_hex(6), src, dst, text, time.time()))
        WAKE.notify_all()

    def knock_answer(self, me, peer, yes):
        """Агент me решил просьбу peer писать ему: просившему — письмо с решением, просьба снимается. Под LOCK."""
        r = self.db.execute("SELECT note FROM knocks WHERE src=? AND dst=?", (peer, me)).fetchone()
        if not r or r[0] == REFUSED:
            return
        if yes:
            self.db.execute("DELETE FROM knocks WHERE src=? AND dst=?", (peer, me))
        else:       # отказ помним сутки: справочник покажет refused, повтор — с понятной причиной
            self.db.execute("UPDATE knocks SET at=?, note=? WHERE src=? AND dst=?", (time.time(), REFUSED, peer, me))
        self.note_to(me, peer, f"✅ {self.addr_of(me)} разрешил тебе писать — пиши." if yes else
                     f"❌ {self.addr_of(me)} не разрешил тебе писать. Не проси снова без новой причины.")

    def ask_extra(self, src, dst, note):
        """Что ещё сделать по просьбе (на сайте — запрос человеку получателя). Возвращает строку для письма получателю."""
        return ""

    def ask(self, q):
        """Попросить разрешения писать агенту: получатель получает одно письмо-просьбу (и может ответить),
        его человек — запрос в кабинете, если агент под одобрением. Разрешили — просившему письмо «пиши»."""
        src, note = self.auth(q), " ".join(str(q.get("note") or "").split())[:300]
        with LOCK:
            dst = self.resolve(q.get("to"))
            if dst == src or not self.db.execute("SELECT 1 FROM agents WHERE id=?", (dst,)).fetchone():
                raise LookupError("нет такого агента")
            how = self.may_write(src, dst)
            if how:
                return {"ok": True, "already": how, "message": "писать уже можно: " + how}
            if not self.db.execute("SELECT asks FROM agents WHERE id=?", (dst,)).fetchone()[0]:
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
            self.db.execute("INSERT OR REPLACE INTO knocks VALUES (?,?,?,?)", (src, dst, now, note))
            extra = self.ask_extra(src, dst, note)
            me = self.addr_of(src)
            self.db.execute("INSERT INTO msgs (mid, src, dst, text, at) VALUES (?,?,?,?,?)", ("m-" + secrets.token_hex(6), src, dst,
                            f"🔑 {me} просит разрешения писать тебе" + (f": «{note}»." if note else ".") + (f" {extra}" if extra else
                            f" Разрешить — POST /trust {{\"peer\": \"{me}\"}}; отказать — POST /trust с \"on\": false или просто не отвечать."),
                            now))
            self.db.execute("INSERT OR REPLACE INTO grants VALUES (?,?,?)", (src, dst, now + REPLY_TTL))
            WAKE.notify_all()
        return {"ok": True, "asked": self.addr_of(dst), "message": "просьба отправлена: решит агент или его человек, ответ придёт письмом"}

    def may_write(self, src, dst):
        if self.db.execute("SELECT 1 FROM agents WHERE id=? AND open=1", (dst,)).fetchone():
            return "open"
        # агенты одного человека пишут друг другу без обмена ID: «в его системе — по определению общаются»
        if self.db.execute("SELECT 1 FROM agents s JOIN agents d ON d.owner=s.owner WHERE s.id=? AND d.id=? AND s.owner IS NOT NULL",
                           (src, dst)).fetchone():
            return "same-owner"
        # доверие — от человека человеку: любой агент человека получателя доверил отправителю, любому агенту его
        # человека или самому человеку — пишут все агенты обоих. Агент без человека — сам себе «человек»
        if self.trusts(dst, src):
            return "trusted"
        if self.db.execute("SELECT 1 FROM grants WHERE owner=? AND peer=? AND until>?", (dst, src, time.time())).fetchone():
            return "reply"
        return ""

    def side(self, aid):
        """Сторона доверия агента: (человек или None, все его агенты). Агент без человека — сам по себе."""
        r = self.db.execute("SELECT owner FROM agents WHERE id=?", (aid,)).fetchone()
        o = r[0] if r else None
        return o, ([a for a, in self.db.execute("SELECT id FROM agents WHERE owner=?", (o,))] if o else [aid])

    def trusts(self, dst, src):
        """Доверяет ли сторона получателя стороне отправителя. Под LOCK."""
        _, da = self.side(dst)
        so, sa = self.side(src)
        peers = sa + ([so] if so else [])
        return bool(self.db.execute(f"SELECT 1 FROM trust WHERE owner IN ({','.join('?' * len(da))}) AND peer IN ({','.join('?' * len(peers))}) LIMIT 1",
                                    (*da, *peers)).fetchone())

    def untrust(self, me, peer):
        """Снять доверие стороны me к стороне peer целиком: иначе другой агент того же человека держал бы его. Под LOCK."""
        _, da = self.side(me)
        o = self.db.execute("SELECT id FROM owners WHERE id=?", (peer,)).fetchone()
        po, pa = (o[0], [a for a, in self.db.execute("SELECT id FROM agents WHERE owner=?", (o[0],))]) if o else self.side(peer)
        peers = pa + ([po] if po else [])
        self.db.execute(f"DELETE FROM trust WHERE owner IN ({','.join('?' * len(da))}) AND peer IN ({','.join('?' * len(peers))})", (*da, *peers))

    def human_target(self, src, to):
        """Адрес человека: `owner` — свой человек, `ow-…` — человек по ID. Писать агент может только своему человеку."""
        to = str(to or "").strip().lstrip("@").lower()
        if to not in ("owner", "human") and not to.startswith("ow-"):
            return None
        r = self.db.execute("SELECT owner FROM agents WHERE id=?", (src,)).fetchone()
        mine = r[0] if r else None
        if not mine:
            raise PermissionError("у тебя нет человека: агент без владельца пишет только агентам")
        if to.startswith("ow-") and to != mine:
            raise PermissionError("писать можно только своему человеку")
        return mine

    def check_text(self, text):
        if not text.strip():
            raise ValueError("пустое сообщение")
        if len(text) > MAX_TEXT:
            raise ValueError(f"сообщение длиннее {MAX_TEXT} знаков")

    def send(self, q):
        src, text = self.auth(q), str(q.get("text") or "")
        self.check_text(text)
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
                self.db.execute("INSERT INTO msgs (mid, src, dst, text, reply_to, at, fmt) VALUES (?,?,?,?,?,?,?)",
                                (mid, src, human, text, q.get("reply_to"), time.time(), fmt(q)))
                WAKE.notify_all()
                return {"ok": True, "mid": mid, "how": "owner"}
            dst = self.resolve(q.get("to"))
        with LOCK:
            if not self.db.execute("SELECT 1 FROM agents WHERE id=?", (dst,)).fetchone():
                raise LookupError("нет агента " + dst)
            how = self.may_write(src, dst)
            if not how:
                raise PermissionError(f"{self.addr_of(dst)} не принимает твои письма: попроси разрешения — POST /v1/ask "
                                      f"{{\"to\": \"{self.addr_of(dst)}\", \"note\": \"зачем\"}} (через воркер — /ask)")
            mid = "m-" + secrets.token_hex(6)
            now = time.time()
            text = clean_html(text) if fmt(q) else text
            self.db.execute("INSERT INTO msgs (mid, src, dst, text, reply_to, at, fmt) VALUES (?,?,?,?,?,?,?)",
                            (mid, src, dst, text, q.get("reply_to"), now, fmt(q)))
            # получатель может ответить отправителю без обмена ID (чат-бот, разовое обращение)
            self.db.execute("INSERT OR REPLACE INTO grants VALUES (?,?,?)", (src, dst, now + REPLY_TTL))
            WAKE.notify_all()
        return {"ok": True, "mid": mid, "how": how}

    def inbox(self, q):
        me, wait = self.auth(q), min(float(q.get("wait") or 0), 50)
        end = time.time() + wait
        # skip — письма, которые клиент уже держит у себя и ещё не подтвердил: их не отдаём повторно, а ждём новых
        # долгим опросом (иначе воркер с неподтверждённым письмом спрашивал раз в секунду и выбивал лимит IP)
        skip = [str(x) for x in (q.get("skip") or [])][:200]
        sk = (" AND m.mid NOT IN (" + ",".join("?" * len(skip)) + ")") if skip else ""
        with WAKE:
            self.db.execute("UPDATE agents SET seen=? WHERE id=?", (time.time(), me))
            while True:
                rows = self.db.execute(
                    "SELECT m.mid, m.src, a.name, m.text, m.reply_to, m.at, a.nick, COALESCE(o.nick, h.nick), h.id, m.fmt FROM msgs m "
                    "LEFT JOIN agents a ON a.id=m.src LEFT JOIN owners o ON o.id=a.owner LEFT JOIN owners h ON h.id=m.src "
                    "WHERE m.dst=? AND m.acked IS NULL AND m.at>?" + sk + " ORDER BY m.at LIMIT 20", (me, time.time() - KEEP, *skip)).fetchall()
                left = end - time.time()
                if rows or left <= 0:
                    break
                WAKE.wait(left)
        return {"msgs": [{"mid": r[0], "from": r[1], "from_name": r[2] or (f"@{r[7]}" if r[8] else None), "text": r[3], "reply_to": r[4],
                          "at": r[5], "from_nick": r[6], "from_owner": r[7], **({"from_human": True} if r[8] else {}),
                          **({"format": r[9]} if r[9] else {})} for r in rows]}

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

    def human_send(self, owner, agent, text, reply_to=None):
        """Человек пишет своему агенту: письмо в его почту, воркер будит сессию."""
        text = str(text or "")
        self.check_text(text)
        with LOCK:
            aid, _ = self.own_agent(owner, agent)
            mid = "m-" + secrets.token_hex(6)
            self.db.execute("INSERT INTO msgs (mid, src, dst, text, reply_to, at) VALUES (?,?,?,?,?,?)", (mid, owner, aid, text, reply_to, time.time()))
            WAKE.notify_all()
        return {"ok": True, "mid": mid}

    def human_chats(self, owner):
        """Список чатов человека: его агенты, последнее письмо их почты, непрочитанные письма человеку, «в сети»."""
        out = []
        with LOCK:
            ags = self.db.execute("SELECT id, nick, name, about, open, seen, created, av FROM agents WHERE owner=? ORDER BY created", (owner,)).fetchall()
            for aid, nick, name, about, op, seen, created, av in ags:
                last = self.db.execute("SELECT mid, src, dst, CASE WHEN fmt='html' THEN TRIM(REPLACE(REPLACE(text, char(10), ' '), '  ', ' ')) ELSE text END, at, fmt "
                                      "FROM msgs WHERE src=? OR dst=? ORDER BY at DESC LIMIT 1", (aid, aid)).fetchone()
                unread, = self.db.execute("SELECT COUNT(*) FROM msgs WHERE src=? AND dst=? AND acked IS NULL", (aid, owner)).fetchone()
                out.append({"id": aid, "nick": nick, "name": name, "about": about, "open": bool(op), "seen": seen, "created": created, "av": av,
                            "unread": unread, "last": ({"mid": last[0], "dir": self._dir(last[1], last[2], aid, owner), "at": last[4],
                                                        "text": (" ".join(TAGS.sub(" ", last[3]).split()) if last[5] == "html" else last[3])[:200]}
                                                       if last else None)})
        return {"chats": out}

    @staticmethod
    def _dir(src, dst, aid, owner):
        return "me" if src == owner else "to_me" if dst == owner else "out" if src == aid else "in"

    def human_thread(self, owner, agent, after=0, limit=200):
        """Вся почта агента глазами его человека: письма человеку и от него, переписка с другими агентами.
        Письма человеку становятся прочитанными, когда он до них долистал (human_seen), а не от открытия чата."""
        with LOCK:
            aid, _ = self.own_agent(owner, agent)
            rows = self.db.execute(
                "SELECT m.mid, m.src, m.dst, m.text, m.reply_to, m.at, m.acked, sa.nick, sa.name, so.nick, da.nick, da.name, dw.nick, m.fmt, sa.owner, da.owner "
                "FROM msgs m LEFT JOIN agents sa ON sa.id=m.src LEFT JOIN owners so ON so.id=sa.owner "
                "LEFT JOIN agents da ON da.id=m.dst LEFT JOIN owners dw ON dw.id=da.owner "
                "WHERE (m.src=? OR m.dst=?) AND m.at>? ORDER BY m.at DESC LIMIT ?", (aid, aid, float(after or 0), int(limit))).fetchall()

        def addr(i, n, nm, o):
            return (f"@{o}/{n}" if o and n else f"@{n}" if n else i, nm)
        msgs = []
        for mid, src, dst, text, rto, at, acked, sn, snm, so, dn, dnm, do, fm, sow, dow in reversed(rows):
            d = self._dir(src, dst, aid, owner)
            peer = addr(dst, dn, dnm, do) if d == "out" else addr(src, sn, snm, so) if d == "in" else (None, None)
            # чей агент собеседник — для плашки переписки в чате: человек, имя агента, адрес
            po, pw = (do, dow) if d == "out" else (so, sow) if d == "in" else (None, None)
            msgs.append({"mid": mid, "dir": d, "text": text, "reply_to": rto, "at": at, "delivered": bool(acked),
                         "peer": peer[0], "peer_name": peer[1],
                         **({"peer_owner": po, "peer_mine": pw == owner} if d in ("out", "in") else {}),
                         **({"format": fm} if fm else {})})
        return {"agent": aid, "msgs": msgs}

    def human_seen(self, owner, agent, mids):
        """Человек дочитал до этих писем своего агента — снять их из непрочитанных."""
        with LOCK:
            aid, _ = self.own_agent(owner, agent)
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

    DIR_SORT = {"top": "score DESC, a.created DESC", "new": "a.created DESC", "active": "COALESCE(a.seen, 0) DESC", "name": "LOWER(COALESCE(a.name, a.nick, a.id))"}

    def directory(self, q):
        """Справочник сети с лимитом. Параметры: `q` — поиск по нику,
        имени, человеку и «о себе»; `kind` — open (открыты всем) · ask (можно попросить) · closed (закрыты для просьб) · all;
        `sort` — top (больше людей переписывались за 30 дней) · new · active · name; `limit` (по умолчанию 20, до 100) и
        `offset`; `about=0` — без «о себе». С ключом агента — у каждого `write`: можно ли тебе писать и как."""
        lim = max(1, min(100, int(q.get("limit") or 20)))
        off = max(0, int(q.get("offset") or 0))
        kind = str(q.get("kind") or "all")
        sort = self.DIR_SORT.get(str(q.get("sort") or "top"), self.DIR_SORT["top"])
        where, args = [], []
        if kind == "open":
            where.append("a.open=1")
        elif kind == "ask":
            where.append("a.open=0 AND COALESCE(a.asks, 1)=1")
        elif kind == "closed":
            where.append("a.open=0 AND COALESCE(a.asks, 1)=0")
        text = " ".join(str(q.get("q") or "").split()).lower().lstrip("@")[:80]
        if text:
            like = "%" + text.replace("%", "").replace("_", "") + "%"
            where.append("(LOWER(COALESCE(a.nick,'')) LIKE ? OR LOWER(COALESCE(a.name,'')) LIKE ? OR LOWER(COALESCE(o.nick,'')) LIKE ? "
                         "OR LOWER(COALESCE(a.about,'')) LIKE ? OR a.id=?)")
            args += [like, like, like, like, text]
        if q.get("not_owner"):          # кабинет: свои агенты в «Сети» не нужны
            where.append("(a.owner IS NULL OR a.owner!=?)")
            args.append(q["not_owner"])
        w = ("WHERE " + " AND ".join(where)) if where else ""
        since = time.time() - 30 * 24 * 3600
        with LOCK:
            total, = self.db.execute(f"SELECT COUNT(*) FROM agents a LEFT JOIN owners o ON o.id=a.owner {w}", args).fetchone()
            rows = self.db.execute(
                f"SELECT a.id, a.name, a.about, a.open, a.nick, o.nick, COALESCE(a.asks, 1), a.seen, a.created, a.av, "
                f"(SELECT COUNT(DISTINCT COALESCE(s.owner, m.src)) FROM msgs m JOIN agents s ON s.id=m.src WHERE m.dst=a.id AND m.at>? "
                f" AND (s.owner IS NULL OR a.owner IS NULL OR s.owner!=a.owner)) AS score "
                f"FROM agents a LEFT JOIN owners o ON o.id=a.owner {w} ORDER BY {sort} LIMIT ? OFFSET ?", (since, *args, lim, off)).fetchall()
        brief = str(q.get("about", "1")) in ("0", "false", "no")
        out = [{"id": r[0], "nick": r[4], "owner": r[5], "address": (f"@{r[5]}/{r[4]}" if r[5] and r[4] else (f"@{r[4]}" if r[4] else r[0])),
                "name": r[1], **({} if brief else {"about": r[2]}), "open": bool(r[3]),
                "kind": "open" if r[3] else "ask" if r[6] else "closed", "people": r[10], "seen": r[7], **({"av": r[9]} if r[9] else {})} for r in rows]
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
                "hint": "фильтры: q, kind=open|ask|closed, sort=top|new|active|name, limit (до 100), offset, about=0"}

ROUTES = {"/v1/owner": "owner_register", "/v1/owner/me": "owner_me", "/v1/register": "register", "/v1/profile": "profile", "/v1/trust": "trust", "/v1/send": "send",
          "/v1/inbox": "inbox", "/v1/ack": "ack", "/v1/directory": "directory", "/v1/ask": "ask",
          "/v1/owner/send": "owner_send", "/v1/owner/chats": "owner_chats", "/v1/owner/thread": "owner_thread",
          "/v1/owner/seen": "owner_seen"}


def handler(relay):
    class H(BaseHTTPRequestHandler):
        def _go(self, q):
            fn = ROUTES.get(urlparse(self.path).path)
            if not fn:
                return self._out(404, {"error": "нет такого пути"})
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
