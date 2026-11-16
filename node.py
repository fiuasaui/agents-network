"""node.py — узел владельца в Agents Network: воркер исполняет тулзы его служб на этой машине.

Набор тулз (toolkit) — файл ~/.agentnet/toolkits/<имя>.json:
  {"name": "billing", "about": "…", "public": false,
   "tools": [{"name": "get_balance", "description": "что делает и когда звать", "parameters": {JSON-схема},
              "danger": false, "run": {"url": "http://127.0.0.1:9000/balance"}  или  {"cmd": ["python3", "/путь/tool.py"]}}]}
"danger": true — опасная тулза (меняет данные, тратит деньги, видит лишнее; прежнее имя пометки — "mutates"): служба зовёт
её, только если владелец включил. "intro": true — хук-подсказка: Сеть сама зовёт её в начале чата и после простоя, ответ
(последние заказы собеседника, тариф) идёт в контекст модели; хук-подсказка не может быть опасной.
Сеть видит всё, кроме "run": как исполнять, знает только эта машина. Воркер объявляет наборы в Сети при старте и при
каждой правке, затем длинным опросом /v1/node/calls забирает вызовы и исполняет: url — POST JSON {tool, args, ctx},
ответ — тело; cmd — тот же JSON на stdin, ответ — stdout (таймаут 50 с). Отдать собеседнику файл (PDF, видео):
тулза возвращает JSON с "attach_paths": ["/путь/файл"] — узел загрузит файлы, и служба приложит их к ответу. ctx.client — кто пишет службе (агент, человек,
посетитель, пользователь сайта владельца); его ставит Сеть, модель подменить не может.

Свой движок (engine=node): модель службы крутится здесь — ~/.agentnet/engine.json {url, key, model}, OpenAI-совместимый
адрес; Сеть присылает ход (tool=__turn__: промпт, история, тулзы), узел гоняет модель и свои тулзы и отдаёт ответ.

🔴 Безопасность: тулза открыта собеседникам служб, к которым подключён набор, — то есть чужим людям и агентам.
Делай тулзы узкими (читают данные одного собеседника по ctx.client), опасные помечай "danger": true. Только stdlib.
"""
import json, os, subprocess, threading, time, urllib.request
from pathlib import Path

ST = {"declared": {}, "running": False, "last_error": None, "last_error_at": None, "last_ok_at": None, "calls": 0}


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
            "tools": [{x: t[x] for x in ("name", "description", "parameters", "mutates", "danger", "intro", "public_note") if x in t} for t in k.get("tools", [])]}


def save(home: Path, k):
    """Сохранить набор локально (с "run") — объявит его потом declare()."""
    if not isinstance(k, dict) or not k.get("name") or not isinstance(k.get("tools"), list):
        raise ValueError("набор: {name, about, public, tools: [{name, description, parameters, mutates, run: {url|cmd}}]}")
    for t in k["tools"]:
        run = t.get("run") or {}
        if not (run.get("url") or run.get("cmd")):
            raise ValueError(f"тулза {t.get('name')}: нужен run.url (HTTP на этой машине) или run.cmd (команда)")
        if run.get("url") and not str(run["url"]).startswith(("http://127.0.0.1", "http://localhost", "http://[::1]")):
            raise ValueError(f"тулза {t.get('name')}: run.url — только адрес этой машины (127.0.0.1)")
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
    """Тулза вернула JSON с "attach_paths": [пути] — загрузить эти файлы в Сеть и заменить на "attach": [id]."""
    try:
        d = json.loads(res)
    except ValueError:
        return res
    if not isinstance(d, dict) or not d.get("attach_paths"):
        return res
    from urllib.parse import quote
    import mimetypes
    ids, c = [], cred()
    for p in [Path(x) for x in d.pop("attach_paths")][:5]:
        if not p.is_file() or p.stat().st_size > 20 * 2**20:
            d.setdefault("attach_errors", []).append(f"{p.name}: нет файла или больше 20 МБ"); continue
        req = urllib.request.Request(relay_url.rstrip("/") + "/v1/node/file", data=p.read_bytes(), headers={
            "X-Owner": c["owner"], "X-Owner-Secret": c["owner_secret"], "X-File-Name": quote(p.name),
            "Content-Type": mimetypes.guess_type(p.name)[0] or "application/octet-stream"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                ids.append(json.loads(r.read())["id"])
        except Exception as e:  # noqa: BLE001
            d.setdefault("attach_errors", []).append(f"{p.name}: {e}")
    d["attach"] = ids
    return json.dumps(d, ensure_ascii=False)


def run_tool(k, tool, args, ctx):
    t = next((x for x in k.get("tools", []) if x.get("name") == tool), None)
    if not t:
        return False, f"на этой машине в наборе {k['name']} нет тулзы {tool}"
    payload = json.dumps({"tool": tool, "args": args, "ctx": ctx}, ensure_ascii=False).encode()
    run = t.get("run") or {}
    try:
        if run.get("url"):
            req = urllib.request.Request(run["url"], data=payload, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=50) as r:
                return True, r.read().decode("utf-8", "replace")
        cmd = run["cmd"] if isinstance(run["cmd"], list) else ["/bin/sh", "-c", str(run["cmd"])]
        p = subprocess.run(cmd, input=payload, capture_output=True, timeout=50)
        if p.returncode:
            return False, (p.stdout + p.stderr).decode("utf-8", "replace")[-4000:] or f"код {p.returncode}"
        return True, p.stdout.decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001 — любой отказ тулзы — честный ответ модели
        return False, f"тулза не выполнилась: {type(e).__name__}: {e}"


def run_turn(home, kits, args, ctx, relay_url, cred, relay=None):
    """Ход службы со своим движком: модель владельца (~/.agentnet/engine.json — OpenAI-совместимый адрес, ключ,
    модель) и его тулзы — здесь, на машине; Сеть прислала промпт, историю и список тулз, ждёт {text, steps, attach}."""
    try:
        eng = json.loads((home / "engine.json").read_text())
    except (OSError, ValueError):
        return False, "на узле нет ~/.agentnet/engine.json — {\"url\": \"…/v1/chat/completions\", \"key\": \"…\", \"model\": \"…\"}"
    route = {r["fn"]: r for r in args.get("route") or []}
    msgs = [{"role": "system", "content": args.get("system") or ""}] + list(args.get("messages") or [])
    steps, attach_ids = [], []
    for _ in range(8):
        body = {"model": eng["model"], "messages": msgs, "max_tokens": 4000, **({"tools": args["tools"]} if args.get("tools") else {})}
        req = urllib.request.Request(eng["url"], data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "Authorization": "Bearer " + eng["key"]})
        try:
            with urllib.request.urlopen(req, timeout=170) as r:
                m = json.loads(r.read())["choices"][0]["message"]
        except Exception as e:  # noqa: BLE001
            return False, f"модель владельца не ответила: {e}"
        calls = m.get("tool_calls") or []
        if not calls:
            return True, json.dumps({"text": m.get("content") or "", "steps": steps, "attach": attach_ids}, ensure_ascii=False)
        msgs.append({"role": "assistant", "content": m.get("content"), "tool_calls": calls})
        for c in calls:
            fn = c["function"]["name"]
            try:
                a = json.loads(c["function"].get("arguments") or "{}")
            except ValueError:
                a = {}
            if fn.startswith("net__") and relay:   # тулза Сети: написать, приложить, найти — исполняет Сеть от имени службы
                r = relay("/v1/node/sys", {**(cred() or {}), "service": args.get("service"), "tool": fn, "args": a})
                ok, res = bool(r.get("ok")), str(r.get("result") or r.get("error") or "")
                attach_ids += [i for i in r.get("attach") or [] if i not in attach_ids]
                steps.append({"tool": fn, "ok": ok})
                msgs.append({"role": "tool", "tool_call_id": c.get("id") or fn, "content": res[:20_000]})
                continue
            r_ = route.get(fn)
            k = kits.get(r_["toolkit"]) if r_ else None
            ok, res = run_tool(k, r_["tool"], a, ctx) if k else (False, "нет такой тулзы")
            if ok and relay_url:
                res = attach(res, relay_url, cred)
                try:
                    attach_ids += json.loads(res).get("attach") or []
                except (ValueError, AttributeError):
                    pass
            steps.append({"tool": fn, "ok": ok})
            msgs.append({"role": "tool", "tool_call_id": c.get("id") or fn, "content": res[:20_000]})
    return True, json.dumps({"text": "Не уложился в число шагов.", "steps": steps, "attach": attach_ids}, ensure_ascii=False)


def loop(home, relay, cred, log, relay_url=""):
    """Забирать вызовы Сети и исполнять. Без наборов или без подключения — спит и проверяет снова."""
    ST["running"] = True
    last_decl = 0
    while True:
        try:
            kits = load(home)
            c = cred()
            if not c or (not kits and not (home / "engine.json").exists()):
                time.sleep(20); continue
            if time.time() - last_decl > 600:
                declare(home, relay, cred); last_decl = time.time()
            r = relay("/v1/node/calls", {**c, "wait": 50}, 70)
            if r.get("error"):
                ST["last_error"], ST["last_error_at"] = "опрос вызовов: " + str(r["error"])[:200], time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()); time.sleep(10); continue
            ST["last_error"], ST["last_ok_at"] = None, time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())   # связь с Сетью есть (симуляция 16.11)
            for call in r.get("calls", []):
                def go(call=call):
                    if call["tool"] == "__turn__":
                        ok, res = run_turn(home, kits, call["args"], call["ctx"], relay_url, cred, relay)
                        ST["calls"] += 1
                        log("node: ход службы", call["ctx"].get("service"), "ok" if ok else "ошибка")
                        relay("/v1/node/result", {**c, "call": call["call"], "ok": ok, "result": res[:50_000]})
                        return
                    k = kits.get(call["toolkit"])
                    ok, res = run_tool(k, call["tool"], call["args"], call["ctx"]) if k else (False, f"набора {call['toolkit']} на этой машине нет")
                    if ok and relay_url:
                        res = attach(res, relay_url, cred)
                    ST["calls"] += 1
                    log("node: вызов", call["toolkit"], call["tool"], "ok" if ok else "ошибка")
                    relay("/v1/node/result", {**c, "call": call["call"], "ok": ok, "result": res[:50_000]})
                threading.Thread(target=go, daemon=True).start()
        except Exception as e:  # noqa: BLE001
            ST["last_error"], ST["last_error_at"] = "цикл узла: " + repr(e)[:200], time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
            time.sleep(10)


def status(home):
    return {"toolkits": [public_view(k) | {"declared": ST["declared"].get(n)} for n, k in load(home).items()],
            "calls_done": ST["calls"], "last_error": ST["last_error"], "last_error_at": ST["last_error_at"], "last_ok_at": ST["last_ok_at"],
            "dir": str(kits_dir(home))}
