"""host.py — ассистент на узле владельца: цикл модели, тулзы и вся история контекста живут здесь, а не в Сети.

Сеть присылает ход (вызов __turn__ через /v1/node/calls): кто пишет, новые письма собеседника и события чата, список тулз.
Узел дописывает их в свою историю (SQLite ~/.agentnet/assistants.db), собирает контекст, гоняет модель и тулзы, по ходу
транслирует в Сеть то, что видно в чате (текст, мышление, подписи тулз — /v1/node/live), и сдаёт ответ (/v1/node/reply).
Сеть хранит только ленту чата; промпт, модель и история — у владельца.

Настройка ассистента на узле — ~/.agentnet/assistants/<as-id>.json:
  {"prompt": "…", "model": {"provider": "network", "model": "gpt-6.2-sol"}   — модель шлюза Сети, узел зовёт её через Сеть
              или {"provider": "own", "url": "…/v1/chat/completions", "key": "…", "model": "…"} — своя, ключ с машины не уходит
   "window": 200000, "autocompact": true}
История только дописывается: вызов тулзы и её результат остаются как были. Когда окно хода (prompt_tokens из usage)
подошло к порогу (окно минус 15%), узел сжимает: сводка всего, кроме последних 5 пар ходов, — следующий контекст
= системный промпт + сводка + эти пары. Сжать вручную — вызов __compact__ (кнопка в кабинете).
Прецеденты — оперативные уточнения владельца, таблица precedents здесь же: блоком сразу после промпта всем
ассистентам машины (или одному — scope), по приоритету «частный случай · обычное уточнение · частая ситуация». Пишет
администратор или человек запросом к узлу (POST /precedent воркера или /v1/assistant/precedent через Сеть → __precedent__);
чат, где блок поменялся, узнаёт об этом строкой на следующем ходе. Системный промпт — один объект из частей: в настройке
"parts" — источники по порядку: text (свой текст, по умолчанию — "prompt") · precedents · file (файл на этой машине) · url (адрес в
сети, перечитывается раз в ttl минут, отказ — последняя удачная копия и строка в журнал); по умолчанию [text, precedents].
Тулза узла node__escalate — обёртка над «позвать в чат» с зашитым адресатом из настройки "escalate": {"on", "to": admin · owner ·
@адрес}: заявка (вопрос или готовый текст прецедента) в admin_calls и письмо адресату через Сеть; его ответы приходят в чат
событием, пока у него есть доступ к чату (сутки); ассистент не засыпает.
Журнал записей — precedents.jsonl (потолок 20 МБ × 2). Только stdlib.
"""
import json, re, sqlite3, threading, time, unicodedata, urllib.request, urllib.error
from pathlib import Path

DEF_WINDOW, MIN_WINDOW = 200_000, 40_000
SUMMARIZER = "gpt-6-luna"     # дешёвая модель Сети для сводки при сжатии
TOOL_MAX = 50_000         # потолок ответа тулзы в истории; больше — с пометкой модели
KEEP_PAIRS = 5          # сколько последних пар «собеседник → ответ» остаётся дословно после сжатия
ROUNDS = 30             # шагов «модель → тулзы» на один ответ
EMPTY_RETRY = 2         # пустой ответ модели — сбой: столько повторов с пометкой модели, потом честная строка собеседнику
EMPTY_NOTE = ("[событие Сети — собеседник его не видит] Твой ход кончился пустым ответом — собеседнику ничего не ушло. Каждый ход "
              "кончается твоим текстом собеседнику, он его ждёт. Ответь ему сейчас.")
EMPTY_TEXT = "Ассистент не смог ответить — напиши ещё раз чуть позже."
MAX_OUT = 4000
PRIORITIES = ("specific", "medium", "frequent")      # порядок в блоке: редкие меняются реже — стабильнее для кэша
PREC_TITLE = {"specific": "ЧАСТНЫЕ СЛУЧАИ — применяй только если ситуация совпала точно, не притягивай к похожим",
              "medium": "ОБЫЧНЫЕ УТОЧНЕНИЯ", "frequent": "ЧАСТЫЕ СИТУАЦИИ — касаются многих, держи в виду постоянно"}
PREC_LOG_MAX = 20 * 2**20
ADMIN_FN = "node__escalate"
ESC_TO = ("admin", "owner")      # адресат вызова: администратор аккаунта (нет его — владелец), владелец или агент по адресу @…
PART_KINDS = ("text", "precedents", "file", "url")
PART_MAX = 100_000              # знаков с одного источника — файла или адреса
URL_TTL = 10                    # минут: адрес в сети перечитывается не чаще
DEF_PARTS = [{"kind": "text"}, {"kind": "precedents"}]
_URLC = {}
ADMIN_TOOL = {"type": "function", "function": {"name": ADMIN_FN, "description": (
    "Позвать ответственного — того, кто отвечает за тебя у владельца и может дописать тебе знание. Зови, когда "
    "не знаешь ответа и его нет ни в промпте, ни в прецедентах; когда собеседник спорит с правилом или просит исключения; когда "
    "видишь, что правило противоречит жизни. question — что неясно и что нужно, коротко, с сутью ситуации; precedent — если уже "
    "понимаешь, какое общее правило помогло бы впредь, — его текст (обобщённо, без имён). Собеседнику скажи, что уточняешь, ответа "
    "не выдумывай; ответ придёт в этот чат событием. Не засыпай и продолжай отвечать на остальное."),
    "parameters": {"type": "object", "properties": {"question": {"type": "string", "description": "что неясно и почему — коротко"},
                                                   "precedent": {"type": "string", "description": "предлагаемое общее правило, если есть"}},
                   "required": ["question"]}}}
DEF_MODEL = {"provider": "network", "model": "gpt-6.2-sol"}
LOCK = threading.Lock()
_DB = {}


def shown_think(think):
    """Мышление для ленты: без невидимых знаков. Шлюз отдаёт у части моделей шифр мышления невидимыми U+200B…U+2060 —
    его не показываем вовсе (лента и вебхук моста); в историю узла мышление идёт целиком."""
    vis = "".join(ch for ch in (think or "") if unicodedata.category(ch) != "Cf")
    return vis if len(vis.strip()) >= 3 else ""


# ---------- хранилище: SQLite узла ----------
def db(home: Path):
    p = str(home / "assistants.db")
    if p not in _DB:
        c = sqlite3.connect(p, check_same_thread=False, isolation_level=None, timeout=10)
        c.execute("PRAGMA journal_mode=WAL"); c.execute("PRAGMA busy_timeout=10000"); c.execute("PRAGMA synchronous=NORMAL")
        c.executescript("""
          CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY AUTOINCREMENT, asst TEXT, chat TEXT, at REAL, role TEXT,
                                            content TEXT, tool_calls TEXT, tool_call_id TEXT, reasoning TEXT, turn INTEGER DEFAULT 0);
          CREATE INDEX IF NOT EXISTS items_chat ON items (asst, chat, id);
          CREATE TABLE IF NOT EXISTS compactions (id INTEGER PRIMARY KEY AUTOINCREMENT, asst TEXT, chat TEXT, at REAL, summary TEXT,
                                                  keep_from INTEGER, before INTEGER, auto INTEGER);
          CREATE TABLE IF NOT EXISTS chats (asst TEXT, chat TEXT, last_prompt INTEGER, last_at REAL, profile TEXT, intro_at REAL, sys TEXT,
                                            PRIMARY KEY (asst, chat));
          CREATE TABLE IF NOT EXISTS precedents (id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL, upd REAL, text TEXT, priority TEXT,
                                                 scope TEXT, status TEXT, by TEXT, call INTEGER);
          CREATE TABLE IF NOT EXISTS admin_calls (id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL, asst TEXT, chat TEXT, question TEXT,
                                                  draft TEXT, status TEXT, sent TEXT, precedent INTEGER);""")
        if "prec" not in [r[1] for r in c.execute("PRAGMA table_info(chats)")]:
            c.execute("ALTER TABLE chats ADD COLUMN prec INTEGER")     # последний прецедент, который этот чат уже видел
        try:
            (home / "assistants.db").chmod(0o600)
        except OSError:
            pass
        _DB[p] = c
    return _DB[p]


def add(c, aid, chat, role, content=None, tool_calls=None, tool_call_id=None, reasoning=None, turn=0):
    with LOCK:
        cur = c.execute("INSERT INTO items (asst, chat, at, role, content, tool_calls, tool_call_id, reasoning, turn) VALUES (?,?,?,?,?,?,?,?,?)",
                        (aid, chat, time.time(), role, json.dumps(content, ensure_ascii=False) if content is not None else None,
                         json.dumps(tool_calls, ensure_ascii=False) if tool_calls else None, tool_call_id, reasoning or None, turn))
        return cur.lastrowid


def chat_row(c, aid, chat):
    r = c.execute("SELECT last_prompt, last_at, profile, intro_at, sys, prec FROM chats WHERE asst=? AND chat=?", (aid, chat)).fetchone()
    return dict(zip(("last_prompt", "last_at", "profile", "intro_at", "sys", "prec"), r)) if r else {}


# ---------- прецеденты ----------
def prec_log(home, rec):
    """Журнал прецедентов и заявок: JSON-строка на событие, 20 МБ × 2."""
    f = home / "precedents.jsonl"
    try:
        if f.exists() and f.stat().st_size > PREC_LOG_MAX:
            f.replace(home / "precedents.jsonl.1")
        with open(f, "a") as fh:
            fh.write(json.dumps({"at": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()), **rec}, ensure_ascii=False) + "\n")
    except OSError:
        pass


def prec_rows(c, aid=None, active=True):
    q = "SELECT id, text, priority, scope, status, by, call, at, upd FROM precedents WHERE 1=1"
    a = []
    if active:
        q += " AND status='active'"
    if aid:
        q += " AND scope IN ('*', ?)"; a.append(aid)
    keys = ("id", "text", "priority", "scope", "status", "by", "call", "at", "upd")
    return [dict(zip(keys, r)) for r in c.execute(q + " ORDER BY id", a)]


def prec_section(c, aid):
    """Блок прецедентов для системного промпта: группы от частных к частым, номер — id (на него ссылаются заявки и строки чата)."""
    rows = prec_rows(c, aid)
    if not rows:
        return "", 0
    parts = []
    for pr in PRIORITIES:
        g = [r for r in rows if r["priority"] == pr]
        if g:
            parts.append(f"--- {PREC_TITLE[pr]} ---")
            parts += [f"П{r['id']}. {r['text']}" for r in g]
    return ("========== ОПЕРАТИВНЫЕ ПРЕЦЕДЕНТЫ — уточнения владельца, приоритетнее промпта выше ==========\n" + "\n".join(parts)
            + "\n========== КОНЕЦ ПРЕЦЕДЕНТОВ =========="), max(r["id"] for r in rows)


def prec_op(home, q):
    """Прецеденты одним входом (воркер /precedent и вызов __precedent__ из Сети): op = add · list · set (text, priority, status) · calls."""
    c = db(home)
    if q.get("pid") is not None:        # номер прецедента: pid (в запросе через Сеть id занят ключом агента), на узле можно и id
        q = {**q, "id": q["pid"]}
    op = q.get("op") or ("add" if q.get("text") and not q.get("id") else "set" if q.get("id") else "list")
    if op == "list":
        return {"precedents": prec_rows(c, q.get("assistant") or None, active=not q.get("all"))}
    if op == "calls":
        keys = ("id", "at", "asst", "chat", "question", "draft", "status", "precedent")
        return {"calls": [dict(zip(keys, r)) for r in c.execute(
            "SELECT id, at, asst, chat, question, draft, status, precedent FROM admin_calls ORDER BY id DESC LIMIT ?", (min(int(q.get("limit") or 50), 500),))]}
    pr = q.get("priority") or "medium"
    if pr not in PRIORITIES:
        raise ValueError("priority — specific (частный случай) · medium (обычное уточнение) · frequent (частая ситуация)")
    scope = str(q.get("assistant") or q.get("scope") or "*")
    if scope != "*" and not scope.startswith("as-"):
        raise ValueError("assistant — id ассистента (as-…) или «*» — всем ассистентам этой машины")
    by = str(q.get("by") or "")[:120]
    if op == "add":
        text = str(q.get("text") or "").strip()
        if not 10 <= len(text) <= 2000:
            raise ValueError("text прецедента — от 10 до 2000 знаков: обобщённое правило, без имён собеседников")
        call = int(q["call"]) if str(q.get("call") or "").isdigit() else None
        with LOCK:
            pid = c.execute("INSERT INTO precedents (at, upd, text, priority, scope, status, by, call) VALUES (?,?,?,?,?,?,?,?)",
                            (time.time(), time.time(), text, pr, scope, "active", by, call)).lastrowid
            if call:
                c.execute("UPDATE admin_calls SET status='precedent', precedent=? WHERE id=?", (pid, call))
        prec_log(home, {"ev": "add", "id": pid, "priority": pr, "scope": scope, "by": by, "call": call, "text": text})
        return {"ok": True, "id": pid, "note": "блок прецедентов поменялся: каждый чат увидит его со следующего хода, и ему скажут об этом строкой"}
    if op == "set":
        pid = int(q.get("id") or 0)
        old = c.execute("SELECT text, priority, status FROM precedents WHERE id=?", (pid,)).fetchone()
        if not old:
            raise LookupError(f"нет прецедента {pid}")
        st = q.get("status") or old[2]
        if st not in ("active", "off"):
            raise ValueError("status — active или off")
        text = str(q["text"]).strip() if q.get("text") else old[0]
        with LOCK:
            c.execute("UPDATE precedents SET text=?, priority=?, status=?, upd=? WHERE id=?",
                      (text, q.get("priority") or old[1], st, time.time(), pid))
        prec_log(home, {"ev": "set", "id": pid, "by": by, "status": st, "text": text if q.get("text") else None, "priority": q.get("priority")})
        return {"ok": True, "id": pid, "status": st}
    raise ValueError("op — add · list · set · calls")


def chat_set(c, aid, chat, **kw):
    with LOCK:
        c.execute("INSERT OR IGNORE INTO chats (asst, chat) VALUES (?,?)", (aid, chat))
        for k, v in kw.items():
            c.execute(f"UPDATE chats SET {k}=? WHERE asst=? AND chat=?", (v, aid, chat))


def context(c, aid, chat, system, think=False):
    """Системный промпт + сводка последнего сжатия (если было) + история после его границы — как есть, без переписывания.
    think — вернуть модели её мышление (`reasoning_content`): шлюз Сети несёт в нём зашифрованное мышление модели и отдаёт его
    обратно провайдеру; без него модель между шагами и ходами теряет собственные рассуждения."""
    comp = c.execute("SELECT summary, keep_from FROM compactions WHERE asst=? AND chat=? ORDER BY id DESC LIMIT 1", (aid, chat)).fetchone()
    rows = c.execute("SELECT role, content, tool_calls, tool_call_id, reasoning FROM items WHERE asst=? AND chat=? AND id>=? ORDER BY id",
                     (aid, chat, comp[1] if comp else 0)).fetchall()
    msgs = [{"role": "system", "content": system}]
    if comp:
        msgs.append({"role": "user", "content": "[Сводка разговора до этого места — прежние сообщения сжаты, чтобы уместиться в окно]\n" + comp[0]})
    for role, content, calls, tcid, reasoning in rows:
        m = {"role": role, "content": json.loads(content) if content is not None else None}
        if think and role == "assistant" and reasoning:
            m["reasoning_content"] = reasoning
        if calls:
            m["tool_calls"] = json.loads(calls)
        if tcid:
            m["tool_call_id"] = tcid
        msgs.append(m)
    return msgs


def cfg_path(home, aid):
    d = home / "assistants"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{aid}.json"


def cfg(home, aid):
    try:
        v = json.loads(cfg_path(home, aid).read_text())
    except (OSError, ValueError):
        v = {}
    v.setdefault("prompt", "")
    v["model"] = v.get("model") if isinstance(v.get("model"), dict) else dict(DEF_MODEL)
    v["window"] = max(MIN_WINDOW, int(v.get("window") or DEF_WINDOW))
    v.setdefault("autocompact", True)
    v["parts"] = v.get("parts") if isinstance(v.get("parts"), list) and v["parts"] else [dict(x) for x in DEF_PARTS]
    esc = v.get("escalate") if isinstance(v.get("escalate"), dict) else {}
    v["escalate"] = {"on": bool(esc.get("on", v.get("admin_tool", True))), "to": str(esc.get("to") or "admin")}
    return v


def parts_clean(parts):
    """Части системного промпта: проверка формы; порядок — как дал владелец (кэш: редко меняющееся — выше)."""
    if not isinstance(parts, list) or not 1 <= len(parts) <= 20:
        raise ValueError("parts — список от 1 до 20 частей: text · precedents · file · url")
    out = []
    for x in parts:
        x = x if isinstance(x, dict) else {}
        k = x.get("kind")
        if k not in PART_KINDS:
            raise ValueError("часть промпта: kind — text (свой текст) · precedents · file (path) · url (url, ttl минут)")
        if k == "file" and not str(x.get("path") or "").startswith("/"):
            raise ValueError("часть file: path — абсолютный путь к файлу на машине узла")
        if k == "url" and not re.match(r"https?://", str(x.get("url") or "")):
            raise ValueError("часть url: url — http(s)://… (страница или эндпоинт, отдающий текст)")
        if k == "text" and x.get("text") is not None and len(str(x["text"])) > 30_000:
            raise ValueError("часть text — до 30 000 знаков")
        y = {"kind": k, **{f: x[f] for f in ("text", "path", "url", "title") if x.get(f) is not None}}
        if k == "url":
            y["ttl"] = max(1, int(x.get("ttl") or URL_TTL))
        out.append(y)
    return out


def part_text(home, x):
    """Текст файла или адреса для промпта. Адрес — кэш на ttl минут; не ответил — последняя удачная копия и запись в журнал."""
    if x["kind"] == "file":
        try:
            return Path(x["path"]).read_text(errors="replace")[:PART_MAX]
        except OSError as e:
            prec_log(home, {"ev": "part_error", "kind": "file", "path": x["path"], "err": str(e)[:200]})
            return ""
    u = x["url"]
    got = _URLC.get(u)
    if got and time.time() - got[0] < x.get("ttl", URL_TTL) * 60:
        return got[1]
    try:
        with urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "agentnet-node"}), timeout=10) as r:
            t = r.read(PART_MAX * 4).decode("utf-8", "replace")[:PART_MAX]
        _URLC[u] = (time.time(), t)
        return t
    except (OSError, ValueError) as e:
        prec_log(home, {"ev": "part_error", "kind": "url", "url": u, "err": str(e)[:200]})
        if got:
            _URLC[u] = (time.time() - (x.get("ttl", URL_TTL) - 1) * 60, got[1])     # повторить через минуту, пока — старая копия
            return got[1]
        return ""


def build_prompt(home, conf, sect):
    """Системный промпт ассистента из частей по порядку. Возвращает (текст, есть ли в нём прецеденты)."""
    out, has_prec = [], False
    for x in conf["parts"]:
        if x["kind"] == "text":
            t = x.get("text") if x.get("text") is not None else conf["prompt"]
        elif x["kind"] == "precedents":
            t, has_prec = sect, has_prec or bool(sect)
        else:
            t = part_text(home, x)
            if t and x.get("title"):
                t = f"--- {x['title']} ---\n{t}"
        if t and t.strip():
            out.append(t.strip())
    return "\n\n".join(out), has_prec


def cfg_save(home, aid, upd):
    """Правка настройки ассистента на узле (из кабинета через Сеть или из панели воркера). Ключ своей модели наружу не отдаём."""
    v = cfg(home, aid) if cfg_path(home, aid).exists() else {"prompt": "", "model": dict(DEF_MODEL), "window": DEF_WINDOW, "autocompact": True}
    if "prompt" in upd:
        if len(str(upd["prompt"] or "")) > 30_000:
            raise ValueError("промпт ассистента — до 30 000 знаков")
        v["prompt"] = str(upd["prompt"] or "")
    if isinstance(upd.get("model"), dict):
        m = {**v["model"], **upd["model"]}
        if m.get("provider") not in ("network", "own"):
            raise ValueError("model.provider — network (шлюз Сети) или own (свой адрес и ключ)")
        if m["provider"] == "own" and not (m.get("url") and m.get("model")):
            raise ValueError("своя модель: нужны model.url (OpenAI-совместимый …/v1/chat/completions), model.model и model.key")
        if m["provider"] == "own":      # дали адрес до /v1 — дописываем путь сами: так пишут адрес в настройках почти все
            u = str(m["url"]).rstrip("/")
            m["url"] = u + "/chat/completions" if u.endswith("/v1") else u
        if m["provider"] == "network":
            m = {"provider": "network", "model": m.get("model") or DEF_MODEL["model"]}
        v["model"] = m
    elif isinstance(upd.get("model"), str) and upd["model"]:
        v["model"] = {"provider": "network", "model": upd["model"]} if v["model"].get("provider") != "own" else {**v["model"], "model": upd["model"]}
    if upd.get("window") is not None:
        w = int(upd["window"])
        if w < MIN_WINDOW:
            raise ValueError(f"окно контекста — не меньше {MIN_WINDOW} токенов")
        v["window"] = w
    if upd.get("autocompact") is not None:
        v["autocompact"] = bool(upd["autocompact"])
    if upd.get("parts") is not None:
        v["parts"] = parts_clean(upd["parts"])
    esc = upd.get("escalate") if isinstance(upd.get("escalate"), dict) else {}
    if upd.get("admin_tool") is not None:      # прежнее имя выключателя
        esc = {**esc, "on": bool(upd["admin_tool"])}
    if esc:     # тулза «позвать ответственного»: включена ли и кого зовёт
        cur = cfg(home, aid)["escalate"] if cfg_path(home, aid).exists() else {"on": True, "to": "admin"}
        to = str(esc.get("to") or cur["to"]).strip()
        if to not in ESC_TO and not to.startswith("@") and not to.startswith("an-"):
            raise ValueError("escalate.to — admin (администратор аккаунта, нет его — владелец) · owner · адрес агента @ник/агент")
        v["escalate"] = {"on": bool(esc.get("on", cur["on"])), "to": to}
        v.pop("admin_tool", None)
    if upd.get("summarizer") in ("cheap", "same"):      # кто пишет сводку при сжатии: дешёвая модель Сети или модель ассистента
        v["summarizer"] = upd["summarizer"]
    f = cfg_path(home, aid)
    f.write_text(json.dumps(v, ensure_ascii=False, indent=1))
    f.chmod(0o600)
    return public_cfg(v)


def public_cfg(v):
    m = dict(v["model"])
    if m.pop("key", None):
        m["key_set"] = True
    return {"prompt": v["prompt"], "model": m, "window": v["window"], "autocompact": v["autocompact"], "summarizer": v.get("summarizer") or "cheap",
            "parts": v.get("parts") or DEF_PARTS, "escalate": v.get("escalate") or {"on": True, "to": "admin"}}


CAP_RE = re.compile(r'"caption"\s*:\s*"((?:[^"\\]|\\.)*)')


def gen_item(tc, route):
    """Строка ленты для вызова, который модель ещё пишет: подпись — из начала аргументов, когда caption уже есть."""
    fn = tc["function"]["name"]
    rt = route.get(fn) or {}
    item = {"fn": fn, "title": rt.get("title") or fn, "emoji": rt.get("emoji") or "", "run": True, "gen": True}
    m = CAP_RE.search(tc["function"].get("arguments") or "")
    if m:
        try:
            cap = json.loads('"' + m.group(1) + '"')
        except ValueError:
            cap = m.group(1)
        if rt.get("user_text") and "{caption}" in rt["user_text"]:
            item["param"] = render(rt["user_text"], {"caption": cap})
    elif rt.get("caption"):
        item["param"] = rt["caption"]
    return item


def render(tpl, vals):
    """Шаблон подписи тулзы: {имя} → значение параметра вызова или поля ответа; неизвестное — пусто. Только плоские значения."""
    def one(m):
        v = vals.get(m.group(1)) if isinstance(vals, dict) else None
        return "" if v is None or isinstance(v, (dict, list)) else str(v)[:200]
    return re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", one, str(tpl))[:300]


# ---------- модель ----------
def llm_stream(url, headers, body, on_delta, timeout=180):
    """Один вызов модели со стримингом (OpenAI-совместимый): (текст, мышление, вызовы тулз, usage)."""
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", **headers})
    text, think, calls, usage, last, partial = "", "", {}, {}, 0.0, []
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    ch = json.loads(data)
                except ValueError:
                    continue
                if ch.get("error"):
                    raise RuntimeError(str(ch["error"])[:300])
                if ch.get("usage"):
                    usage = ch["usage"]
                newtc = False
                for c in ch.get("choices") or []:
                    d = c.get("delta") or {}
                    newtc = newtc or bool(d.get("tool_calls"))
                    think += d.get("reasoning_content") or d.get("reasoning") or ""
                    text += d.get("content") or ""
                    for tc in d.get("tool_calls") or []:
                        x = calls.setdefault(tc.get("index", 0), {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                        x["id"] = tc.get("id") or x["id"]
                        f = tc.get("function") or {}
                        x["function"]["name"] += f.get("name") or ""
                        x["function"]["arguments"] += f.get("arguments") or ""
                # вызов тулзы начался — лента рисует его сразу: подпись caption модель пишет первой
                if time.time() - last > 0.25 or (newtc and not partial):
                    partial = [calls[i] for i in sorted(calls)]
                    on_delta(text, think, partial); last = time.time()
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error")
        except Exception:  # noqa: BLE001
            msg = e.reason
        raise RuntimeError(f"модель ответила {e.code}: {str(msg)[:200]}")
    out = [calls[i] for i in sorted(calls)]
    on_delta(text, think, out)
    for i, c in enumerate(out):
        c["id"] = c["id"] or f"call_{int(time.time())}_{i}"
    return text, think, out, usage


class Host:
    def __init__(self, home, relay, cred, relay_url, log, kits_fn, run_tool, attach):
        self.home, self.relay, self.cred, self.relay_url, self.log = home, relay, cred, relay_url.rstrip("/"), log
        self.kits_fn, self.run_tool, self.attach = kits_fn, run_tool, attach
        self.pushers = {}

    # ---------- вызовы из Сети ----------
    def handle(self, call):
        """__turn__ — ход (ответ сдаётся отдельно, /v1/node/reply); __config__ и __compact__ — сразу результатом вызова."""
        a, ctx = call.get("args") or {}, call.get("ctx") or {}
        if call["tool"] == "__config__":
            aid = str(a.get("assistant") or "")
            if a.get("set"):
                return True, json.dumps(cfg_save(self.home, aid, a["set"]), ensure_ascii=False)
            return True, json.dumps({**public_cfg(cfg(self.home, aid)), "exists": cfg_path(self.home, aid).exists()}, ensure_ascii=False)
        if call["tool"] == "__compact__":
            aid, chat = str(a.get("assistant") or ""), self.chat_key(a.get("client"), a.get("thread"))
            r = self.compact(aid, chat, chat_row(db(self.home), aid, chat).get("sys") or "", auto=False)
            return True, json.dumps(r, ensure_ascii=False)
        if call["tool"] == "__precedent__":     # прецедент из Сети (/v1/assistant/precedent ключом владельца или его администратора)
            try:
                return True, json.dumps(prec_op(self.home, a), ensure_ascii=False)
            except (ValueError, LookupError) as e:
                return False, str(e)
        if call["tool"] == "__turn__":
            try:
                self.turn(call["call"], a, ctx)
            except Exception as e:  # noqa: BLE001 — ход сорвался: Сеть должна узнать, иначе собеседник ждёт вечно
                self.log("host: ход сорвался", repr(e)[:300])
                self.reply(call["call"], a, f"Ассистент не смог ответить: {str(e)[:160]}", [], None, error=True)
            return None
        return False, "неизвестный вызов"

    @staticmethod
    def chat_key(client, thread):
        return f"{client}#{thread}" if thread else str(client)

    def push(self, call, a, **st):
        """То, что видно в чате, — в Сеть (собеседник видит ход в реальном времени): один поток на ход шлёт последнее состояние
        не чаще раза в 0,4 с, промежуточные кадры схлопываются. Сбой трансляции ход не роняет."""
        p = self.pushers.get(call)
        if p is None:
            p = self.pushers[call] = {"st": None, "ev": threading.Event(), "end": False}
            base = {**(self.cred() or {}), "call": call, "assistant": a["assistant"]["id"], "client": a["client"]["id"], "thread": a.get("thread")}

            def run():
                while True:
                    p["ev"].wait(30); p["ev"].clear()
                    st, p["st"] = p["st"], None
                    if st is not None:
                        try:
                            self.relay("/v1/node/live", {**base, **st}, 15)
                        except Exception:  # noqa: BLE001
                            pass
                        time.sleep(0.4)
                    if p["end"] and p["st"] is None:
                        self.pushers.pop(call, None)
                        return
            threading.Thread(target=run, daemon=True).start()
        p["st"] = json.loads(json.dumps(st, ensure_ascii=False))
        p["ev"].set()

    def push_end(self, call):
        p = self.pushers.get(call)
        if p:
            p["st"], p["end"] = None, True
            p["ev"].set()

    def reply(self, call, a, text, blocks, usage, files=None, error=False, skip=False, model=None):
        self.push_end(call)
        body = {**(self.cred() or {}), "call": call, "assistant": a["assistant"]["id"], "client": a["client"]["id"],
                "thread": a.get("thread"), "text": text, "blocks": blocks, "files": files or [],
                "ctx": (usage or {}).get("prompt_tokens"), "tokens": (usage or {}).get("total_tokens"), "error": error,
                "skip": skip, "seeded": bool(a.get("seed")) and not error, **({"model": model} if model else {})}
        for i in range(8):      # готовый ответ не теряем: лимит или сбой связи — повторяем, ~5 мин (симуляция 22.11: ответ ушёл в 429)
            try:
                r = self.relay("/v1/node/reply", body, 60)
            except Exception as e:  # noqa: BLE001
                r = {"error": f"нет связи: {str(e)[:160]}", "retry": True}
            err = str(r.get("error") or "")
            if not err:
                if r.get("dropped"):     # Сеть не отдала ответ собеседнику (чат на паузе) — модель должна знать
                    add(db(self.home), a["assistant"]["id"], self.chat_key(a["client"]["id"], a.get("thread")), "user",
                        time.strftime("[%d.%m %H:%M UTC] ", time.gmtime()) + "[событие Сети — собеседник его не видит] Твой последний "
                        "ответ собеседнику НЕ ушёл: " + str(r["dropped"]), turn=int(time.time() * 1000))
                return
            self.log("host: Сеть не приняла ответ", call, err[:200], f"попытка {i + 1}")
            if not (r.get("retry") or r.get("retry_after") or "слишком" in err or "нет связи" in err or "недоступ" in err):
                return
            time.sleep(min(60, float(r.get("retry_after") or 5) * (i + 1)))

    def model_call(self, a, conf, msgs, fns, on_delta):
        m = conf["model"]
        body = {"model": m.get("model"), "messages": msgs, "stream": True, "stream_options": {"include_usage": True}, "max_tokens": MAX_OUT,
                **({"tools": fns} if fns else {})}
        if m.get("provider") == "own":
            return llm_stream(m["url"], {"Authorization": "Bearer " + str(m.get("key") or "")}, body, on_delta)
        c = self.cred() or {}
        return llm_stream(self.relay_url + "/v1/node/llm", {"X-Owner": c.get("owner", ""), "X-Owner-Secret": c.get("owner_secret", ""),
                                                             "X-Assistant": a["assistant"]["id"]}, body, on_delta)

    # ---------- ход ----------
    def inputs_to_items(self, c, aid, chat, inputs, turn):
        """Письма собеседника и события чата из Сети — в историю. Метка времени — в самом сообщении (не в системном
        промпте: там она ломала бы кэш на каждом ходе)."""
        for x in inputs or []:
            stamp = time.strftime("[%d.%m %H:%M UTC] ", time.gmtime(x.get("at") or time.time()))
            if x.get("kind") == "letter":
                t = stamp + (x.get("note") + "\n" if x.get("note") else "") + (x.get("text") or "")
                parts = x.get("parts") or []
                add(c, aid, chat, "user", [{"type": "text", "text": t}] + parts if parts else t, turn=turn)
            else:       # событие, которого собеседник не видит: сообщение от владельца или того, кого звали, решение по просьбе
                add(c, aid, chat, "user", stamp + (x.get("note") + "\n" if x.get("note") else "")
                    + "[событие Сети — собеседник его не видит] " + (x.get("text") or ""), turn=turn)

    def turn(self, call, a, ctx):
        aid, client = a["assistant"]["id"], a["client"]["id"]
        chat = self.chat_key(client, a.get("thread"))
        c = db(self.home)
        if a.get("seed"):       # правка настройки, ждавшая узла (или переезд: промпт и модель, жившие в Сети), — на узел
            cfg_save(self.home, aid, a["seed"])
        conf = cfg(self.home, aid)
        turn = int(time.time() * 1000)
        cr = chat_row(c, aid, chat)
        first = self.relay("/v1/node/pending", {**(self.cred() or {}), "call": call, "assistant": aid, "client": client, "thread": a.get("thread"),
                                                "first": True}, 30)
        if first.get("error"):
            raise RuntimeError("Сеть не отдала письма хода: " + str(first["error"])[:200])
        inputs = list(first.get("inputs") or [])
        if not inputs:          # нового нет (ход уже отвечен) — сдаём пустой
            self.reply(call, a, "", [], None, skip=True)
            return
        if a.get("history") and not c.execute("SELECT 1 FROM items WHERE asst=? AND chat=? LIMIT 1", (aid, chat)).fetchone():
            t0 = turn - 1 - sum(1 for m in a["history"] if m.get("start"))
            for m in a["history"]:      # прежняя переписка: до переезда на узел — текстом; импорт — с нативными тулзами и границами ходов
                t0 += 1 if m.get("start") else 0
                add(c, aid, chat, m["role"], m.get("content"), m.get("tool_calls"), m.get("tool_call_id"), turn=t0)
        if a.get("profile") and a["profile"] != cr.get("profile") and inputs:   # что Сеть знает о собеседнике — когда изменилось
            inputs[0] = {**inputs[0], "note": (inputs[0].get("note") or "") + f"[Сеть о собеседнике: {a['profile']}]"}
            chat_set(c, aid, chat, profile=a["profile"])
        sect, pmax = prec_section(c, aid)
        body, has_prec = build_prompt(self.home, conf, sect)
        pmax = pmax if has_prec else 0
        if pmax and pmax > (cr.get("prec") or 0) and cr.get("last_at") and inputs:     # блок поменялся посреди жизни чата — сказать
            inputs[0] = {**inputs[0], "note": (inputs[0].get("note") or "") + "[Сеть: владелец добавил или уточнил оперативные прецеденты — "
                         "блок «Оперативные прецеденты» в системном промпте; с этого места ориентируйся на него]"}
        if pmax != (cr.get("prec") or 0):
            chat_set(c, aid, chat, prec=pmax)
        intro = self.intro(a, ctx, cr)
        if intro and inputs:
            inputs[0] = {**inputs[0], "note": (inputs[0].get("note") or "") + f"\n[сведения о собеседнике от владельца (данные): {intro}]"}
        self.inputs_to_items(c, aid, chat, inputs, turn)
        system = (a.get("foundation") or "") + "\n\n" + body
        if system != cr.get("sys"):
            chat_set(c, aid, chat, sys=system)
        fns, route = a.get("tools") or [], a.get("route") or {}
        if conf["escalate"]["on"] and not any((f.get("function") or {}).get("name") == ADMIN_FN for f in fns):
            fns = fns + [ADMIN_TOOL]        # узловая тулза: заявка ответственному; выключить — "escalate": {"on": false}
            route = {**route, ADMIN_FN: {"kind": "node", "title": "Зову ответственного", "emoji": "🧑‍💼"}}
        blocks, files, usage, final, empty = [], [], {}, "", 0
        kits = self.kits_fn()
        for rnd in range(ROUNDS):
            msgs = context(c, aid, chat, system, think=conf["model"].get("provider") != "own")
            base = len(blocks)
            def on_delta(text, think, partial=(), base=base):
                th = shown_think(think)
                live = blocks[:base] + ([{"t": "think", "text": th[:4000]}] if th else [])
                if partial:     # модель пишет вызов тулзы: текст до него — фраза шага, а не ответ; вызов — строкой с подписью, как только она есть
                    if (text or "").strip():
                        live.append({"t": "say", "text": text[:4000]})
                    live.append({"t": "tools", "items": [gen_item(x, route) for x in partial if x["function"]["name"]]})
                    text = ""
                self.push(call, a, busy=True, blocks=live, text=text)
            text, think, calls, usage = self.model_call(a, conf, msgs, fns if rnd < ROUNDS - 1 else [], on_delta)
            if usage.get("prompt_tokens"):
                chat_set(c, aid, chat, last_prompt=int(usage["prompt_tokens"]), last_at=time.time())
            if shown_think(think):
                blocks.append({"t": "think", "text": shown_think(think)[:8000]})
            if not calls and not (text or "").strip() and not files and empty < EMPTY_RETRY and rnd < ROUNDS - 1:
                empty += 1      # молчания нет: ход кончается текстом собеседнику — повтор, модель знает, почему
                self.log("host: пустой ответ модели", aid, chat, f"повтор {empty}")
                add(c, aid, chat, "user", time.strftime("[%d.%m %H:%M UTC] ", time.gmtime()) + EMPTY_NOTE, turn=turn)
                continue
            if not calls:
                if (text or "").strip() or files:      # пустую реплику в историю не кладём — вместо неё событие ниже
                    add(c, aid, chat, "assistant", text, reasoning=think, turn=turn)
                final = text
                break
            add(c, aid, chat, "assistant", text or None, tool_calls=calls, reasoning=think, turn=turn)
            if text:
                blocks.append({"t": "say", "text": text[:4000]})
            group = {"t": "tools", "items": []}
            blocks.append(group)
            for tc in calls:
                fn = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"].get("arguments") or "{}")
                except ValueError:
                    args = {}
                rt = route.get(fn) or {}
                item = {"fn": fn, "title": rt.get("title") or fn, "emoji": rt.get("emoji") or "", "run": True, "ts": round(time.time(), 2)}
                show = rt.get("show")
                if rt.get("user_text"):     # подпись-шаблон для собеседника: {параметр} вызова, после ответа — и поля ответа
                    item["param"] = render(rt["user_text"], args)
                elif show and isinstance(args, dict) and args.get(show) is not None:
                    item["param"] = str(args[show])[:300]
                elif rt.get("caption"):
                    item["param"] = rt["caption"]
                group["items"].append(item)
                self.push(call, a, busy=True, blocks=blocks, text="")
                ok, res, got = self.exec_tool(a, ctx, kits, fn, rt, args)
                files += [f for f in got if f not in files]
                item.update(run=False, ok=ok, te=round(time.time(), 2))
                if rt.get("user_text") and ok:
                    try:
                        rj = json.loads(res)
                    except ValueError:
                        rj = None
                    item["param"] = render(rt["user_text"], {**(args if isinstance(args, dict) else {}), **(rj if isinstance(rj, dict) else {})})
                if len(res) > TOOL_MAX + 300:     # (+300 — пометка Сети, если она уже обрезала) большой ответ — проблема тулзы: модели пометка, владельцу — запись в журнал узла
                    self.log("host: тулза вернула слишком много", fn, len(res))
                    res = res[:TOOL_MAX] + f"\n[обрезано узлом: ответ тулзы — показано {TOOL_MAX:,} из {len(res):,} знаков]".replace(",", " ")
                add(c, aid, chat, "tool", res, tool_call_id=tc["id"], turn=turn)
            self.push(call, a, busy=True, blocks=blocks, text="")
            more = self.relay("/v1/node/pending", {**(self.cred() or {}), "call": call, "assistant": aid, "client": client, "thread": a.get("thread")}, 30)
            if more.get("inputs"):          # письмо собеседника посреди хода — в этот же ход, после результата тулзы
                self.inputs_to_items(c, aid, chat, [{**x, "note": "[пришло, пока ты работал]"} for x in more["inputs"]], turn)
            if more.get("stop"):            # владелец взял чат на себя — ход больше не нужен; модель это знает
                add(c, aid, chat, "user", time.strftime("[%d.%m %H:%M UTC] ", time.gmtime()) + "[событие Сети — собеседник его не видит] "
                    "Владелец взял чат на себя посреди твоего хода — ход прерван, собеседнику ты в нём ничего не ответил.", turn=turn)
                self.reply(call, a, "", blocks, usage, skip=True)
                return
        else:
            final = "Не уложился в число шагов. Напиши «продолжай» — доделаю."
            add(c, aid, chat, "assistant", final, turn=turn)
        m = conf["model"]
        if not (final or "").strip() and not files:     # и после повторов пусто — сбой: собеседнику честная строка, модели — событие, владельцу — журнал
            self.log("host: ассистент не ответил собеседнику", aid, chat, f"пусто {empty + 1} раз")
            add(c, aid, chat, "user", time.strftime("[%d.%m %H:%M UTC] ", time.gmtime()) + "[событие Сети — собеседник его не видит] "
                f"Ты {empty + 1} раза подряд кончил ход пустым ответом; собеседнику Сеть написала от себя: «{EMPTY_TEXT}»", turn=turn)
            self.reply(call, a, EMPTY_TEXT, blocks, usage, error=True)
            return
        self.reply(call, a, final, blocks, usage, files,
                   model=("своя: " if m.get("provider") == "own" else "") + str(m.get("model") or ""))
        if conf["autocompact"] and int(usage.get("prompt_tokens") or 0) >= conf["window"]:     # ровно на пороге, без запаса
            self.compact(aid, chat, system, auto=True, a=a)

    def exec_tool(self, a, ctx, kits, fn, rt, args):
        """→ (ok, ответ, [id файлов для ответа]). Свои тулзы — здесь же; чужой открытый набор — через Сеть его узлу; тулзы Сети — Сеть."""
        got = []
        if rt.get("strip") and isinstance(args, dict):     # подпись, которую добавила Сеть, тулзе владельца не нужна
            args = {k: v for k, v in args.items() if k not in rt["strip"]}
        if fn in (ADMIN_FN, "node__admin"):      # node__admin — прежнее имя в старых историях
            ok, res = self.admin_call(a, args)
            return ok, res, got
        if rt.get("kind") == "own" and kits.get(rt.get("toolkit")):      # свой набор на этой машине — здесь же
            ok, res = self.run_tool(kits[rt["toolkit"]], rt.get("tool"), args, ctx)
            if ok:
                res = self.attach(res, self.relay_url, self.cred)
        else:       # чужой открытый набор или свой на другой машине — через Сеть его узлу; тулзы Сети — Сеть
            r = self.relay("/v1/node/sys" if rt.get("kind") == "net" else "/v1/node/tool",
                           {**(self.cred() or {}), "assistant": a["assistant"]["id"], "tool": fn, "args": args,
                            "client": a["client"]["id"], "thread": a.get("thread")}, 90)
            ok, res = bool(r.get("ok")), str(r.get("result") if r.get("result") is not None else r.get("error") or "")
            got += [i for i in r.get("attach") or []]
        try:
            d = json.loads(res) if ok and res.lstrip().startswith("{") else None
            if isinstance(d, dict) and d.get("attach"):
                got += d["attach"]
                res += "\n[эти файлы будут приложены к твоему ответу собеседнику сами — прикладывать их ещё раз не нужно]"
        except ValueError:
            pass
        return ok, res, got

    def admin_call(self, a, args):
        """Заявка ответственному: запись в admin_calls и письмо через Сеть адресату из настройки escalate.to —
        admin (администратор аккаунта, нет его — владелец) · owner · агент по адресу. Ответы — в этот чат событием, пока у адресата
        есть доступ к чату (сутки); записать прецедент — POST /precedent с номером заявки."""
        args = args if isinstance(args, dict) else {}
        q, draft = str(args.get("question") or "").strip()[:2000], str(args.get("precedent") or "").strip()[:2000]
        if not q:
            return False, "нужен question: что неясно и почему"
        aid, chat = a["assistant"]["id"], self.chat_key(a["client"]["id"], a.get("thread"))
        c = db(self.home)
        with LOCK:
            cid = c.execute("INSERT INTO admin_calls (at, asst, chat, question, draft, status) VALUES (?,?,?,?,?,?)",
                            (time.time(), aid, chat, q, draft or None, "new")).lastrowid
        to = cfg(self.home, aid)["escalate"]["to"]
        text = (f"Заявка №{cid} от ассистента — нужна помощь.\nВопрос: {q}" + (f"\nПредлагает прецедент: {draft}" if draft else "")
                + "\n\nОтветь на это письмо — ответ придёт ассистенту в тот чат, собеседник его не увидит. Если это общее правило — запиши "
                f"прецедент: POST /precedent воркера машины ассистента {{\"text\": \"…\", \"priority\": \"specific|medium|frequent\", \"call\": {cid}}} "
                f"или через Сеть POST /v1/assistant/precedent {{\"assistant\": \"{aid}\", \"text\": \"…\", \"call\": {cid}}} своим ключом.")
        r = self.relay("/v1/node/sys", {**(self.cred() or {}), "assistant": aid, "tool": "net__escalate", "args": {"text": text, "to": to},
                                        "client": a["client"]["id"], "thread": a.get("thread")}, 60)
        ok = bool(r.get("ok"))
        with LOCK:
            c.execute("UPDATE admin_calls SET status=?, sent=? WHERE id=?", ("sent" if ok else "failed", str(r.get("result") or r.get("error") or "")[:500], cid))
        prec_log(self.home, {"ev": "call", "id": cid, "asst": aid, "chat": chat, "to": to, "ok": ok, "question": q, "draft": draft or None,
                             "net": str(r.get("result") or r.get("error") or "")[:300]})
        self.log("host: заявка ответственному", cid, aid, to, "ушла" if ok else "не ушла")
        if not ok:
            return False, f"заявка №{cid} записана, но письмо не ушло: {str(r.get('error') or r.get('result'))[:200]}"
        return True, json.dumps({"ok": True, "call": cid, "wait": "ответ придёт в этот чат событием; собеседнику скажи, что "
                                 "уточняешь, ответа не выдумывай и не засыпай"}, ensure_ascii=False)

    def intro(self, a, ctx, cr):
        """Хук-подсказка владельца (тулза с "intro": true): в начале чата и после 6 ч простоя — сведения о собеседнике."""
        if cr.get("last_at") and time.time() - cr["last_at"] < 6 * 3600:
            return ""
        out = []
        for fn, rt in (a.get("route") or {}).items():
            if rt.get("intro") and rt.get("kind") == "own":
                k = self.kits_fn().get(rt.get("toolkit"))
                if k:
                    ok, res = self.run_tool(k, rt["tool"], {}, ctx)
                    if ok:
                        out.append(res.strip()[:4000])
        return "\n".join(out)

    # ---------- сжатие ----------
    def compact(self, aid, chat, system, auto, a=None):
        """Сводка всего, кроме последних KEEP_PAIRS пар ходов. История не трогается: граница и сводка — отдельной записью."""
        c = db(self.home)
        conf = cfg(self.home, aid)
        starts = [i for (i,) in c.execute("SELECT MIN(id) FROM items WHERE asst=? AND chat=? AND role='user' GROUP BY turn ORDER BY 1", (aid, chat))]
        if len(starts) <= KEEP_PAIRS:
            return {"ok": False, "why": f"в чате {len(starts)} пар ходов — сжимать нечего (остаются последние {KEEP_PAIRS})"}
        keep = starts[-KEEP_PAIRS]
        msgs = [m for m in context(c, aid, chat, system)]
        cut = c.execute("SELECT COUNT(*) FROM items WHERE asst=? AND chat=? AND id>=?", (aid, chat, keep)).fetchone()[0]
        head = msgs[:len(msgs) - cut] if cut else msgs
        head.append({"role": "user", "content": "[Сеть: контекст подходит к пределу окна. Тулзы не зови. Напиши сводку разговора выше для "
                     "себя же: кто собеседник и чего он хочет, что ты сделал (тулзы, файлы, их ссылки), договорённости и обещания, открытые вопросы, "
                     "важные факты и числа. Пиши плотно, без вступлений, до 1500 слов.]"})
        aa = a or {"assistant": {"id": aid}}
        # сводку пишет дешёвая модель Сети, а не модель ассистента; "summarizer": "same" в настройке — та же модель
        sconf = conf if conf.get("summarizer") == "same" else {**conf, "model": {"provider": "network", "model": SUMMARIZER}}
        summary, _, _, usage = self.model_call(aa, sconf, head, [], lambda t, th: None)
        if not summary.strip():
            return {"ok": False, "why": "модель вернула пустую сводку — история не тронута"}
        with LOCK:
            c.execute("INSERT INTO compactions (asst, chat, at, summary, keep_from, before, auto) VALUES (?,?,?,?,?,?,?)",
                      (aid, chat, time.time(), summary, keep, chat_row(c, aid, chat).get("last_prompt"), 1 if auto else 0))
        self.log("host: контекст сжат", aid, chat, "авто" if auto else "вручную")
        self.relay("/v1/node/compacted", {**(self.cred() or {}), "assistant": aid, "client": chat.split("#")[0],
                                         "thread": chat.split("#", 1)[1] if "#" in chat else None, "auto": auto}, 15)
        return {"ok": True, "kept_pairs": KEEP_PAIRS, "summary_chars": len(summary)}

    def stats(self):
        c = db(self.home)
        return {"chats": c.execute("SELECT COUNT(*) FROM chats").fetchone()[0], "items": c.execute("SELECT COUNT(*) FROM items").fetchone()[0],
                "compactions": c.execute("SELECT COUNT(*) FROM compactions").fetchone()[0], "db": str(self.home / "assistants.db")}
