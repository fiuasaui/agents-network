"""node.py — узел владельца в Agents Network: воркер исполняет тулзы его ассистентов на этой машине.

Набор тулз (toolkit) — файл ~/.agentnet/toolkits/<имя>.json:
  {"name": "billing", "about": "…", "public": false,
   "tools": [{"name": "get_balance", "description": "что делает и когда звать", "parameters": {JSON-схема},
              "danger": false, "run": {"url": "http://127.0.0.1:9000/balance"}  или  {"cmd": ["python3", "/путь/tool.py"]}}]}
"danger": true — опасная тулза (меняет данные, тратит деньги, видит лишнее; прежнее имя пометки — "mutates"): ассистент зовёт
её, только если владелец включил. "intro": true — хук-подсказка: Сеть сама зовёт её в начале чата и после простоя, ответ
(последние заказы собеседника, тариф) идёт в контекст модели; хук-подсказка не может быть опасной.
"prompt" набора (до 8000 знаков) — его блок в системном промпте ассистента: как и когда звать эти тулзы; владелец
включает и двигает блок галочкой в кабинете.
Сеть видит всё, кроме "run": как исполнять, знает только эта машина. Воркер объявляет наборы в Сети при старте и при
каждой правке, затем длинным опросом /v1/node/calls забирает вызовы и исполняет: url — POST JSON {tool, args, ctx},
ответ — тело; cmd — тот же JSON на stdin, ответ — stdout (таймаут 50 с). Отдать собеседнику файл (PDF, видео):
тулза возвращает JSON с "attach_paths": ["/путь/файл"] — узел загрузит файлы, и ассистент приложит их к ответу. ctx.client — кто пишет ассистенту (агент, человек,
посетитель, пользователь сайта владельца); его ставит Сеть, модель подменить не может.

Как вызов подписан в чате собеседнику: "title" — название по-человечески, "emoji" — свой значок, "show" — имя
одного параметра, значение которого видно (команда терминала); прочие параметры не видны никогда. Или "caption" — постоянная подпись. Или "user_text" — шаблон подписи
с {параметрами} вызова и полями JSON-ответа тулзы: "{cmd}", "заказ {order} — статус {status}".
Ассистенты этой машины живут здесь же (host.py): Сеть присылает ход (tool=__turn__), узел ведёт историю и модель и отдаёт ответ.

🔴 Безопасность: тулза открыта собеседникам ассистентов, к которым подключён набор, — то есть чужим людям и агентам.
Делай тулзы узкими (читают данные одного собеседника по ctx.client), опасные помечай "danger": true. Только stdlib.
"""
import json, os, subprocess, threading, time, urllib.request
from pathlib import Path

TOOL_TIMEOUT, TOOL_TIMEOUT_MAX = 50, 600     # секунды на вызов тулзы; набор или тулза ставит своё поле timeout (замечание тестера 23.11)
ST = {"wake": threading.Event(), "declared": {}, "running": False, "last_error": None, "last_error_at": None, "last_ok_at": None, "calls": 0}


def kits_dir(home: Path) -> Path:
    d = home / "toolkits"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load(home: Path):
    out = {}
    for f in sorted(kits_dir(home).glob("*.json")):
        try:
            k = json.loads(f.read_text())
            out[k["name"]] = k
        except (OSError, ValueError, KeyError) as e:
            ST["last_error"] = f"{f.name}: {e}"
    return out


def public_view(k):
    return {"name": k["name"], "about": k.get("about", ""), "public": bool(k.get("public")),
            **({"prompt": k["prompt"]} if k.get("prompt") else {}),     # блок набора в системном промпте ассистента
            **({"install": k["install"]} if k.get("install") else {}),  # шаблон: как поднять такой набор у себя
            **({"forget": True} if k.get("forget") else {}),             # набор убирает за собой файлы удалённого чата
            **({"timeout": k["timeout"]} if k.get("timeout") else {}),
            "tools": [{x: t[x] for x in ("name", "description", "parameters", "mutates", "danger", "intro", "public_note", "title", "emoji", "show", "caption", "user_text", "timeout") if x in t} for t in k.get("tools", [])]}


def save(home: Path, k):
    """Сохранить набор локально (с "run") — объявит его потом declare()."""
    if not isinstance(k, dict) or not k.get("name") or not isinstance(k.get("tools"), list):
        raise ValueError("набор: {name, about, public, tools: [{name, description, parameters, mutates, run: {url|cmd}}]}")
    for t in k["tools"] + ([{"name": "forget", "run": k["forget"]}] if k.get("forget") else []):     # forget — уборка чата
        run = t.get("run") or {}
        if not (run.get("url") or run.get("cmd")):
            raise ValueError(f"тулза {t.get('name')}: нужен run.url (HTTP на этой машине) или run.cmd (команда)")
        if run.get("url") and not str(run["url"]).startswith(("http://127.0.0.1", "http://localhost", "http://[::1]")):
            raise ValueError(f"тулза {t.get('name')}: run.url — только адрес этой машины (127.0.0.1)")
    for t in [k] + k["tools"]:
        if "timeout" in t and not (isinstance(t["timeout"], (int, float)) and 1 <= t["timeout"] <= TOOL_TIMEOUT_MAX):
            raise ValueError(f"timeout {t.get('name') or 'набора'}: секунды от 1 до {TOOL_TIMEOUT_MAX}")
    f = kits_dir(home) / (k["name"] + ".json")
    f.write_text(json.dumps(k, ensure_ascii=False, indent=1))
    os.chmod(f, 0o600)
    return f


def delete(home: Path, name):
    f = kits_dir(home) / (str(name) + ".json")
    if not f.exists():
        raise LookupError("нет такого набора на этой машине")
    f.unlink()
    return {"ok": True, "note": "локально удалён; в Сети набор останется без исполнителя — удали его и в кабинете"}


def declare(home, relay, cred):
    """Объявить все наборы машины в Сети (без "run")."""
    c = cred()
    if not c:
        return {"error": "машина не подключена к сети: нужен ключ человека из кабинета (/owner)"}
    res = {}
    for name, k in load(home).items():
        r = relay("/v1/node/toolkit", {**c, **public_view(k)})
        res[name] = r.get("id") or r.get("error")
        if r.get("id"):
            ST["declared"][name] = r["id"]
    return res


def attach(res, relay_url, cred):
    """Тулза узла вернула JSON с "send_files": [пути на этой машине] (прежнее имя — "attach_paths") — залить файлы в Сеть и
    заменить на "attach": [id] и "sent": [{id, name, size}]. Файл уходит собеседнику только так — явным вызовом тулзы,
    назначение которой отправить файл; собеседник видит его сразу, в рамке этого вызова."""
    try:
        d = json.loads(res)
    except ValueError:
        return res
    if not isinstance(d, dict) or not (d.get("send_files") or d.get("attach_paths")):
        return res
    from urllib.parse import quote
    import mimetypes
    ids, sent, c = [], [], cred()
    paths = (d.pop("send_files", None) or []) + (d.pop("attach_paths", None) or [])
    for p in [Path(str(x)) for x in paths][:10]:
        if not p.is_file() or p.stat().st_size > 20 * 2**20:
            d.setdefault("send_errors", []).append(f"{p.name}: нет файла или больше 20 МБ"); continue
        req = urllib.request.Request(relay_url.rstrip("/") + "/v1/node/file", data=p.read_bytes(), headers={
            "X-Owner": c["owner"], "X-Owner-Secret": c["owner_secret"], "X-File-Name": quote(p.name),
            "Content-Type": mimetypes.guess_type(p.name)[0] or "application/octet-stream"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                f = json.loads(r.read())
                ids.append(f["id"]); sent.append({"id": f["id"], "name": f.get("name") or p.name, "size": f.get("size")})
        except Exception as e:  # noqa: BLE001
            d.setdefault("send_errors", []).append(f"{p.name}: {e}")
    d["attach"], d["sent"] = ids, sent
    return json.dumps(d, ensure_ascii=False)


def run_tool(k, tool, args, ctx):
    t = next((x for x in k.get("tools", []) if x.get("name") == tool), None)
    if not t:
        return False, f"на этой машине в наборе {k['name']} нет тулзы {tool}"
    payload = json.dumps({"tool": tool, "args": args, "ctx": ctx}, ensure_ascii=False).encode()
    run = t.get("run") or {}
    to = min(TOOL_TIMEOUT_MAX, float(t.get("timeout") or k.get("timeout") or TOOL_TIMEOUT))     # тулза → набор → 50 с
    try:
        if run.get("url"):
            req = urllib.request.Request(run["url"], data=payload, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=to) as r:
                return True, r.read().decode("utf-8", "replace")
        cmd = run["cmd"] if isinstance(run["cmd"], list) else ["/bin/sh", "-c", str(run["cmd"])]
        p = subprocess.run(cmd, input=payload, capture_output=True, timeout=to)
        if p.returncode:
            return False, (p.stdout + p.stderr).decode("utf-8", "replace")[-4000:] or f"код {p.returncode}"
        return True, p.stdout.decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001 — любой отказ тулзы — честный ответ модели
        return False, f"тулза не выполнилась: {type(e).__name__}: {e}"


def hand_in(relay, body, log, tries=8):
    """Сдать результат вызова Сети, не теряя его (аудит В-01): нет связи, лимит, сбой сервера — повтор того же результата
    ~5 мин; тулзу заново не запускаем (её действие могло бы повториться). Сеть принимает повтор уже принятого как «ок»."""
    for i in range(tries):
        r = relay("/v1/node/result", body)
        code = r.get("code") if isinstance(r, dict) else None
        if not (isinstance(r, dict) and r.get("error")):
            return True
        if not (code in (0, 429) or (code or 0) >= 500):
            log("node: Сеть не приняла результат", body.get("call"), str(r.get("error"))[:160]); return False
        time.sleep(min(60, float(r.get("retry_after") or 5) * (i + 1)))
    log("node: результат не сдан за ~5 мин", body.get("call")); return False


def loop(home, relay, cred, log, relay_url=""):
    """Забирать вызовы Сети и исполнять. Без наборов или без подключения — спит и проверяет снова."""
    ST["running"] = True
    last_decl = 0
    import host as hs       # ассистенты этой машины: история, модель и цикл тулз живут здесь
    H = hs.Host(home, relay, cred, relay_url, log, lambda: load(home), run_tool, attach)
    ST["host"] = H
    while True:
        try:
            kits = load(home)
            c = cred()
            if not c:
                ST["wake"].wait(20); ST["wake"].clear(); continue     # ключ из /owner будит сразу
            if time.time() - last_decl > 600:
                declare(home, relay, cred); last_decl = time.time()
            r = relay("/v1/node/calls", {**c, "wait": 50}, 70)
            if r.get("error"):
                ST["last_error"], ST["last_error_at"] = "опрос вызовов: " + str(r["error"])[:200], time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()); time.sleep(10); continue
            ST["last_error"], ST["last_ok_at"] = None, time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())   # связь с Сетью есть (симуляция 16.11)
            for call in r.get("calls", []):
                def go(call=call):
                    if call["tool"] in ("__turn__", "__config__", "__compact__", "__precedent__", "__forget__"):
                        got = H.handle(call)
                        ST["calls"] += 1
                        log("node: ассистент", call["tool"], call["ctx"].get("assistant") or (call["args"] or {}).get("assistant"))
                        if got is not None:
                            hand_in(relay, {**c, "call": call["call"], "ok": got[0], "result": str(got[1])[:50_000]}, log)
                        return
                    k = kits.get(call["toolkit"])
                    ok, res = run_tool(k, call["tool"], call["args"], call["ctx"]) if k else (False, f"набора {call['toolkit']} на этой машине нет")
                    if ok and relay_url:
                        res = attach(res, relay_url, cred)
                    ST["calls"] += 1
                    log("node: вызов", call["toolkit"], call["tool"], "ok" if ok else "ошибка")
                    hand_in(relay, {**c, "call": call["call"], "ok": ok, "result": res[:50_000]}, log)
                threading.Thread(target=go, daemon=True).start()
        except Exception as e:  # noqa: BLE001
            ST["last_error"], ST["last_error_at"] = "цикл узла: " + repr(e)[:200], time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
            time.sleep(10)


def status(home):
    return {"toolkits": [public_view(k) | {"declared": ST["declared"].get(n)} for n, k in load(home).items()],
            "calls_done": ST["calls"], "last_error": ST["last_error"], "last_error_at": ST["last_error_at"], "last_ok_at": ST["last_ok_at"],
            "dir": str(kits_dir(home))}
