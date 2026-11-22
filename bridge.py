"""bridge.py — мост стороннего сервиса к ассистенту Agents Network: воркер на машине владельца.

Сервис владельца (веб-чат, Telegram-бот, маркетплейс) не регистрирует своих пользователей в Сети: каждый становится
«заглушкой» — собеседником ассистента со своим ID (st-…), ником и полями карточки, которые видит только владелец.

Подключение — файл ~/.agentnet/bridges/<имя>.json (или POST 127.0.0.1:<порт>/bridge с теми же полями):
  {"name": "webchat", "assistant": "as-…" | "@ник/ассистент", "source": "Веб-чат", "webhook": "http://127.0.0.1:9000/an",
   "token": "секрет для заголовка Authorization: Bearer"}
Сервис → воркер (только с этого компьютера):
  POST /bridge/<имя>/message {user: {id, nick, avatar?, fields?}, text, thread?}  — сообщение пользователя: будит ассистента
  POST /bridge/<имя>/event   {user, event: {type, text, data?}}                  — событие сервиса: в контекст, хода не запускает
  POST /bridge/<имя>/import  {user, msgs: [{role: user|assistant|admin, text, at, tools?}]} — прошлый диалог в пустой чат
Воркер → сервис: POST на webhook — каждое письмо ассистента (и владельца от его имени) отдельным JSON по мере готовности:
  {"type": "reply", "bridge", "assistant", "user": ID в сервисе, "text", "blocks": [ход: мышление, тулзы с подписями],
   "files": [{name, url}], "by_human": true — писал человек от имени ассистента, "system": "asst_sleep" — плашка сна, "at", "mid"}
Вебхук не ответил 2xx — повтор через 10 с, до часа; потом письмо пропускается с записью в журнал. Ключ владельца
сервису не нужен: его держит воркер. Только stdlib.
"""
import json, os, re, threading, time, urllib.request
from pathlib import Path

ST = {"after": None, "sent": 0, "failed": 0, "last_error": None, "running": False}
NAME_RE = re.compile(r"^[a-z0-9_-]{1,40}$")


def bdir(home: Path) -> Path:
    d = home / "bridges"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load(home: Path, name=None):
    out = {}
    for f in sorted(bdir(home).glob("*.json")):
        try:
            b = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(b, dict) and b.get("assistant"):
            out[f.stem] = {**b, "name": f.stem}
    return out.get(name) if name else out


def save(home: Path, q, resolve):
    """Завести или поменять мост; resolve(assistant) → id ассистента владельца (as-…)."""
    name = str(q.get("name") or "").strip().lower()
    if not NAME_RE.match(name):
        raise ValueError("name — латиница, цифры, - и _, до 40 знаков")
    f = bdir(home) / f"{name}.json"
    if q.get("delete"):
        f.unlink(missing_ok=True)
        return {"ok": True, "deleted": name}
    hook = str(q.get("webhook") or "")
    if hook and not hook.startswith(("http://", "https://")):
        raise ValueError("webhook — адрес http(s)://, куда воркер шлёт ответы ассистента")
    b = {"assistant": resolve(str(q.get("assistant") or "")), "source": " ".join(str(q.get("source") or name).split())[:40],
         "webhook": hook, "token": str(q.get("token") or "")[:200]}
    f.write_text(json.dumps(b, ensure_ascii=False, indent=1))
    os.chmod(f, 0o600)
    return {"ok": True, "name": name, **{k: v for k, v in b.items() if k != "token"}, "token": bool(b["token"])}


def public(home: Path):
    return {"bridges": [{k: (bool(v) if k == "token" else v) for k, v in b.items()} for b in load(home).values()], **{k: v for k, v in ST.items() if k != "after"}}


def inbound(home: Path, name, kind, q, relay, owner_cred):
    """Сервис → Сеть: message | event | import."""
    b = load(home, name)
    if not b:
        raise LookupError(f"нет моста {name}: заведи POST /bridge {{name, assistant, webhook}}")
    o = owner_cred()
    if not o:
        raise PermissionError("воркер не подключён к сети ключом владельца")
    body = {**o, "assistant": b["assistant"], "source": b["source"], "user": q.get("user")}
    if kind == "message":
        body.update(text=q.get("text"), **({"thread": q["thread"]} if q.get("thread") else {}))
    elif kind == "event":
        body.update(event=q.get("event") or {"text": q.get("text")})
    elif kind == "import":
        body.update(msgs=q.get("msgs"))
    else:
        raise LookupError("путь моста: message, event или import")
    return relay(f"/v1/stub/{'send' if kind == 'message' else kind}", body, 120 if kind == "import" else 60)


def post(url, token, payload):
    h = {"Content-Type": "application/json", **({"Authorization": "Bearer " + token} if token else {})}
    req = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode(), headers=h)
    with urllib.request.urlopen(req, timeout=20) as r:
        if not 200 <= r.status < 300:
            raise RuntimeError(f"вебхук ответил {r.status}")


def loop(home: Path, relay, owner_cred, log):
    """Сеть → сервис: длинный опрос /v1/stub/out, каждое письмо — на webhook своего моста; курсор — в bridges/.cursor."""
    cur = bdir(home) / ".cursor"
    try:
        ST["after"] = float(cur.read_text())
    except (OSError, ValueError):
        ST["after"] = time.time()
    ST["running"] = True
    while True:
        bridges = {b["assistant"]: b for b in load(home).values() if b.get("webhook")}
        o = owner_cred()
        if not bridges or not o:
            time.sleep(15)
            continue
        r = relay("/v1/stub/out", {**o, "after": ST["after"], "wait": 25}, 40)
        if r.get("error"):
            ST["last_error"] = str(r["error"])[:200]
            time.sleep(10)
            continue
        for m in r.get("msgs") or []:
            b = bridges.get(m.get("assistant_id"))
            if b:
                payload = {"type": "reply", "bridge": b["name"], "assistant": m["assistant"], "user": m["user"],
                           **{k: m[k] for k in ("text", "blocks", "files", "by_human", "system", "thread", "at", "mid") if k in m}}
                t0 = time.time()
                while True:
                    try:
                        post(b["webhook"], b.get("token"), payload)
                        ST["sent"] += 1
                        break
                    except Exception as e:      # сервис лёг — ждём его, письма не теряем, пока не прошёл час
                        ST["last_error"] = f"{b['name']}: {str(e)[:160]}"
                        if time.time() - t0 > 3600:
                            ST["failed"] += 1
                            log("мост", b["name"], "письмо", m.get("mid"), "не доставлено за час — пропускаю:", e)
                            break
                        time.sleep(10)
            ST["after"] = m["at"]
            cur.write_text(str(ST["after"]))
        if not r.get("msgs"):
            ST["after"] = max(ST["after"], float(r.get("after") or 0))
