#!/usr/bin/env python3
"""selftest.py — самопроверка сети агентов: агент запускает её из своей сессии Claude Code после установки.

Что делает: подключает твою сессию к сети (если ещё нет), поднимает вторую фоновую сессию Claude Code «selftest-B» в
временной папке с тем же хуком, просит её подключиться, довериться тебе и отвечать «pong» на любое сообщение сети;
шлёт ей «ping» и ждёт ответа — он придёт тебе в сессию сообщением сети, а скрипт увидит его у воркера. Печатает
PASS/FAIL со временем каждого шага и убирает вторую сессию. Стоит 2–3 хода модели второй сессии.
python3 selftest.py [--port 8791] [--model haiku] [--settings FILE] [--keep]
Нужно: запуск из сессии Claude Code (есть $CLAUDE_CODE_SESSION_ID), запущенный воркер, `claude` в PATH.
"""
import argparse, json, os, secrets, subprocess, sys, tempfile, time, urllib.error, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent


def call(port, path, body=None, timeout=30):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        raw = e.read()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"text": raw.decode()}


def step(t0, ok, what):
    print(f"{'✅' if ok else '❌'} {time.time() - t0:5.1f} с  {what}", flush=True)
    if not ok:
        sys.exit(1)


def main():
    ap = argparse.ArgumentParser(description="самопроверка сети агентов")
    ap.add_argument("--port", type=int, default=8791)
    ap.add_argument("--model", default="haiku", help="модель второй сессии (дешёвая)")
    ap.add_argument("--settings", help="файл настроек второй сессии (свой провайдер и ключ)")
    ap.add_argument("--keep", action="store_true", help="не убирать вторую сессию")
    a = ap.parse_args()
    me = os.environ.get("CLAUDE_CODE_SESSION_ID")
    t0 = time.time()
    step(t0, bool(me), "запуск из сессии Claude Code")
    try:
        call(a.port, "/")
        up = True
    except Exception:
        up = False
    step(t0, up, f"воркер отвечает на 127.0.0.1:{a.port}")
    j = call(a.port, "/join", {"session": me, "name": "selftest-A"})
    step(t0, bool(j.get("id")), f"моя сессия в сети: {j.get('id')}")
    tag = secrets.token_hex(3)
    name_b = f"selftest-B-{tag}"
    d = Path(tempfile.mkdtemp(prefix="agentnet-selftest-"))
    (d / ".claude").mkdir()
    (d / ".claude/settings.json").write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [
        {"type": "command", "command": f"AGENTNET_PORT={a.port} {sys.executable} {HERE / 'hook.py'}", "async": True}]}]}}))
    prompt = (f"Ты — тестовая сессия сети агентов. 1) Прочитай http://127.0.0.1:{a.port}/ через curl. 2) Подключись с именем "
              f"«{name_b}». 3) Доверься агенту {j['id']} (/trust). 4) На каждое сообщение сети отвечай отправителю через /send "
              f"текстом «pong» и тем же словом, что пришло после «ping». Больше ничего не делай.")
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_JOB_DIR", "CLAUDE_CODE_SESSION_ID")}
    cmd = ["claude", "--bg", "--name", name_b, "--model", a.model, "--allowedTools", "Bash(curl:*)"]
    if a.settings:
        cmd += ["--settings", a.settings]
    r = subprocess.run(cmd + [prompt], cwd=d, env=env, capture_output=True, text=True, timeout=60)
    job = next((w for line in r.stdout.splitlines() for w in line.split() if len(w) == 8 and w.isalnum()), "")
    step(t0, r.returncode == 0, f"вторая сессия запущена ({job or '?'})")
    b_id = ""
    for _ in range(90):
        b_id = next((x["id"] for x in call(a.port, "/directory").get("agents", []) if x["name"] == name_b), "")
        if b_id:
            break
        time.sleep(2)
    step(t0, bool(b_id), f"вторая сессия подключилась: {b_id}")
    sent = None
    for _ in range(60):    # ждём, пока B впишет нас в доверенные
        sent = call(a.port, "/send", {"session": me, "to": b_id, "text": f"ping {tag}"})
        if sent.get("ok"):
            break
        time.sleep(2)
    step(t0, bool(sent and sent.get("ok")), f"ping ушёл ({(sent or {}).get('how') or (sent or {}).get('error')})")
    got = None
    for _ in range(90):
        got = next((m for m in call(a.port, f"/recent?session={me}").get("recent", []) if m["from"] == b_id and tag in m["text"]), None)
        if got:
            break
        time.sleep(2)
    step(t0, bool(got), "pong пришёл в мою сессию: " + (got["text"].split("\n\n")[-1][:80] if got else "нет"))
    if not a.keep and job:
        subprocess.run(["claude", "stop", job], capture_output=True)
        subprocess.run(["claude", "rm", job], capture_output=True)
        print(f"вторая сессия {job} убрана")
    print("PASS — сеть работает: вторая сессия проснулась на сообщение, ответила, ответ разбудил тебя.")


if __name__ == "__main__":
    main()
