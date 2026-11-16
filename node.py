"""node.py — узел владельца в Agents Network: воркер исполняет тулзы его служб на этой машине.

Набор тулз (toolkit) — файл ~/.agentnet/toolkits/<имя>.json:
  {"name": "billing", "about": "…", "public": false,
   "tools": [{"name": "get_balance", "description": "что делает и когда звать", "parameters": {JSON-схема},
              "mutates": false, "run": {"url": "http://127.0.0.1:9000/balance"}  или  {"cmd": ["python3", "/путь/tool.py"]}}]}
Сеть видит всё, кроме "run": как исполнять, знает только эта машина. Воркер объявляет наборы в Сети при старте и при
каждой правке, затем длинным опросом /v1/node/calls забирает вызовы и исполняет: url — POST JSON {tool, args, ctx},
ответ — тело; cmd — тот же JSON на stdin, ответ — stdout (таймаут 50 с). ctx.client — кто пишет службе (агент, человек,
посетитель, пользователь сайта владельца); его ставит Сеть, модель подменить не может.

🔴 Безопасность: тулза открыта собеседникам служб, к которым подключён набор, — то есть чужим людям и агентам.
Делай тулзы узкими (читают данные одного собеседника по ctx.client), меняющие помечай "mutates": true (служба зовёт их
только если владелец включил). Только stdlib.
"""
import json, os, subprocess, threading, time, urllib.request
from pathlib import Path

ST = {"declared": {}, "running": False, "last_error": None, "calls": 0}


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
            "tools": [{x: t[x] for x in ("name", "description", "parameters", "mutates", "public_note") if x in t} for t in k.get("tools", [])]}


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


def loop(home, relay, cred, log):
    """Забирать вызовы Сети и исполнять. Без наборов или без подключения — спит и проверяет снова."""
    ST["running"] = True
    last_decl = 0
    while True:
        try:
            kits = load(home)
            c = cred()
            if not kits or not c:
                time.sleep(20); continue
            if time.time() - last_decl > 600:
                declare(home, relay, cred); last_decl = time.time()
            r = relay("/v1/node/calls", {**c, "wait": 50}, 70)
            if r.get("error"):
                ST["last_error"] = r["error"]; time.sleep(10); continue
            for call in r.get("calls", []):
                def go(call=call):
                    k = kits.get(call["toolkit"])
                    ok, res = run_tool(k, call["tool"], call["args"], call["ctx"]) if k else (False, f"набора {call['toolkit']} на этой машине нет")
                    ST["calls"] += 1
                    log("node: вызов", call["toolkit"], call["tool"], "ok" if ok else "ошибка")
                    relay("/v1/node/result", {**c, "call": call["call"], "ok": ok, "result": res[:50_000]})
                threading.Thread(target=go, daemon=True).start()
        except Exception as e:  # noqa: BLE001
            ST["last_error"] = repr(e)[:200]
            time.sleep(10)


def status(home):
    return {"toolkits": [public_view(k) | {"declared": ST["declared"].get(n)} for n, k in load(home).items()],
            "calls_done": ST["calls"], "last_error": ST["last_error"], "dir": str(kits_dir(home))}
