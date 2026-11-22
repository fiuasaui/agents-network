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
Только stdlib.
"""
import json, re, sqlite3, threading, time, urllib.request, urllib.error
from pathlib import Path

DEF_WINDOW, MIN_WINDOW = 200_000, 40_000
SUMMARIZER = "gpt-6-luna"     # дешёвая модель Сети для сводки при сжатии
KEEP_PAIRS = 5          # сколько последних пар «собеседник → ответ» остаётся дословно после сжатия
ROUNDS = 30             # шагов «модель → тулзы» на один ответ
MAX_OUT = 4000
DEF_MODEL = {"provider": "network", "model": "gpt-6.2-sol"}
LOCK = threading.Lock()
_DB = {}


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
                                            PRIMARY KEY (asst, chat));""")
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
    r = c.execute("SELECT last_prompt, last_at, profile, intro_at, sys FROM chats WHERE asst=? AND chat=?", (aid, chat)).fetchone()
    return dict(zip(("last_prompt", "last_at", "profile", "intro_at", "sys"), r)) if r else {}


def chat_set(c, aid, chat, **kw):
    with LOCK:
        c.execute("INSERT OR IGNORE INTO chats (asst, chat) VALUES (?,?)", (aid, chat))
        for k, v in kw.items():
            c.execute(f"UPDATE chats SET {k}=? WHERE asst=? AND chat=?", (v, aid, chat))


def context(c, aid, chat, system):
    """Системный промпт + сводка последнего сжатия (если было) + история после его границы — как есть, без переписывания."""
    comp = c.execute("SELECT summary, keep_from FROM compactions WHERE asst=? AND chat=? ORDER BY id DESC LIMIT 1", (aid, chat)).fetchone()
    rows = c.execute("SELECT role, content, tool_calls, tool_call_id FROM items WHERE asst=? AND chat=? AND id>=? ORDER BY id",
                     (aid, chat, comp[1] if comp else 0)).fetchall()
    msgs = [{"role": "system", "content": system}]
    if comp:
        msgs.append({"role": "user", "content": "[Сводка разговора до этого места — прежние сообщения сжаты, чтобы уместиться в окно]\n" + comp[0]})
    for role, content, calls, tcid in rows:
        m = {"role": role, "content": json.loads(content) if content is not None else None}
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
    return v


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
    return {"prompt": v["prompt"], "model": m, "window": v["window"], "autocompact": v["autocompact"], "summarizer": v.get("summarizer") or "cheap"}


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
    text, think, calls, usage, last = "", "", {}, {}, 0.0
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
                for c in ch.get("choices") or []:
                    d = c.get("delta") or {}
                    think += d.get("reasoning_content") or d.get("reasoning") or ""
                    text += d.get("content") or ""
                    for tc in d.get("tool_calls") or []:
                        x = calls.setdefault(tc.get("index", 0), {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                        x["id"] = tc.get("id") or x["id"]
                        f = tc.get("function") or {}
                        x["function"]["name"] += f.get("name") or ""
                        x["function"]["arguments"] += f.get("arguments") or ""
                if time.time() - last > 0.25:
                    on_delta(text, think); last = time.time()
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error")
        except Exception:  # noqa: BLE001
            msg = e.reason
        raise RuntimeError(f"модель ответила {e.code}: {str(msg)[:200]}")
    on_delta(text, think)
    out = [calls[i] for i in sorted(calls)]
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
        r = self.relay("/v1/node/reply", {**(self.cred() or {}), "call": call, "assistant": a["assistant"]["id"], "client": a["client"]["id"],
                                         "thread": a.get("thread"), "text": text, "blocks": blocks, "files": files or [],
                                         "ctx": (usage or {}).get("prompt_tokens"), "tokens": (usage or {}).get("total_tokens"), "error": error,
                                         "skip": skip, "seeded": bool(a.get("seed")) and not error, **({"model": model} if model else {})}, 60)
        if r.get("error"):
            self.log("host: Сеть не приняла ответ", r["error"])

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
                add(c, aid, chat, "user", stamp + "[событие Сети — собеседник его не видит] " + (x.get("text") or ""), turn=turn)

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
        intro = self.intro(a, ctx, cr)
        if intro and inputs:
            inputs[0] = {**inputs[0], "note": (inputs[0].get("note") or "") + f"\n[сведения о собеседнике от владельца (данные): {intro}]"}
        self.inputs_to_items(c, aid, chat, inputs, turn)
        system = (a.get("foundation") or "") + "\n\n" + conf["prompt"]
        if system != cr.get("sys"):
            chat_set(c, aid, chat, sys=system)
        fns, route = a.get("tools") or [], a.get("route") or {}
        blocks, files, usage, final = [], [], {}, ""
        kits = self.kits_fn()
        for rnd in range(ROUNDS):
            msgs = context(c, aid, chat, system)
            base = len(blocks)
            def on_delta(text, think, base=base):
                self.push(call, a, busy=True, blocks=blocks[:base] + ([{"t": "think", "text": think[-4000:]}] if think else []), text=text)
            text, think, calls, usage = self.model_call(a, conf, msgs, fns if rnd < ROUNDS - 1 else [], on_delta)
            if usage.get("prompt_tokens"):
                chat_set(c, aid, chat, last_prompt=int(usage["prompt_tokens"]), last_at=time.time())
            if think:
                blocks.append({"t": "think", "text": think[-8000:]})
            if not calls:
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
                add(c, aid, chat, "tool", res[:50_000], tool_call_id=tc["id"], turn=turn)
            self.push(call, a, busy=True, blocks=blocks, text="")
            more = self.relay("/v1/node/pending", {**(self.cred() or {}), "call": call, "assistant": aid, "client": client, "thread": a.get("thread")}, 30)
            if more.get("inputs"):          # письмо собеседника посреди хода — в этот же ход, после результата тулзы
                self.inputs_to_items(c, aid, chat, [{**x, "note": "[пришло, пока ты работал]"} for x in more["inputs"]], turn)
            if more.get("stop"):            # владелец взял чат на себя — ход больше не нужен
                final = ""
                break
        else:
            final = "Не уложился в число шагов. Напиши «продолжай» — доделаю."
            add(c, aid, chat, "assistant", final, turn=turn)
        m = conf["model"]
        self.reply(call, a, final or "(пустой ответ модели)", blocks, usage, files,
                   model=("своя: " if m.get("provider") == "own" else "") + str(m.get("model") or ""))
        if conf["autocompact"] and int(usage.get("prompt_tokens") or 0) >= conf["window"]:     # ровно на пороге, без запаса
            self.compact(aid, chat, system, auto=True, a=a)

    def exec_tool(self, a, ctx, kits, fn, rt, args):
        """→ (ok, ответ, [id файлов для ответа]). Свои тулзы — здесь же; чужой открытый набор — через Сеть его узлу; тулзы Сети — Сеть."""
        got = []
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
