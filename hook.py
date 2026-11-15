#!/usr/bin/env python3
"""hook.py — хук SessionStart сети агентов: слушатель внутри сессии Claude Code.

Ставится в настройки Claude Code как асинхронный хук SessionStart ("async": true — у такого хука нет предела времени,
он живёт, пока жива сессия). Ждёт у воркера сообщения для своей сессии и пишет каждое в сокет своей сессии
(CLAUDE_CODE_MESSAGING_SOCKET, механизм описан в документации Claude Code: «скрипт или хук пишет в сессию»).
Сообщение от собственного процесса сессии Claude Code доставляет при любом режиме доступа — и будит сессию.
Подтверждает воркеру только после записи в сокет. Один слушатель на сессию; воркер недоступен — ждёт и повторяет.
"""
import json, os, socket, sys, time, urllib.request, uuid

PORT = int(os.environ.get("AGENTNET_PORT", 8791))
BASE = f"http://127.0.0.1:{PORT}"
HOME = os.environ.get("AGENTNET_HOME", os.path.join(os.path.expanduser("~"), ".agentnet"))


def call(path, body, timeout):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def post_to_session(text, frm, name):
    sock = os.environ.get("CLAUDE_CODE_MESSAGING_SOCKET", "")
    path = sock[4:] if sock.startswith("uds:") else sock
    body = f'<cross-session-message from="agentnet:{frm}" from-name="agentnet · {name}">\n{text}\n</cross-session-message>'
    frame = {"msgV": 1, "msg_id": str(uuid.uuid4()), "type": "user", "message": {"role": "user", "content": body},
             "priority": "next", "from": f"agentnet:{frm}"}
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(10)
    s.connect(path)
    tok = os.environ.get("CLAUDE_CODE_MESSAGING_TOKEN")
    s.sendall(((json.dumps({"type": "auth", "token": tok}) + "\n") if tok else "").encode() + (json.dumps(frame, ensure_ascii=False) + "\n").encode())
    s.close()


def main():
    try:
        ev = json.load(sys.stdin)
    except Exception:
        ev = {}
    sid = ev.get("session_id") or os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if not sid or not os.environ.get("CLAUDE_CODE_MESSAGING_SOCKET"):
        return    # нет сокета — у сессии нет входящих (bare-режим, старый Claude Code)
    os.makedirs(os.path.join(HOME, "hooks"), exist_ok=True)
    lock = os.path.join(HOME, "hooks", sid + ".pid")
    try:
        os.kill(int(open(lock).read()), 0)
        return    # слушатель этой сессии уже жив (повторный SessionStart после сжатия)
    except (OSError, ValueError):
        pass
    with open(lock, "w") as f:
        f.write(str(os.getpid()))
    while True:
        try:
            r = call("/hook/wait", {"session": sid, "wait": 25}, timeout=40)
        except Exception:
            time.sleep(5)
            continue
        if not r.get("mid"):
            continue
        try:
            post_to_session(r["text"], r.get("from", "?"), r.get("from_name") or "агент")
        except Exception:
            time.sleep(3)    # сокет недоступен — не подтверждаем, воркер отдаст снова
            continue
        try:
            call("/hook/ack", {"session": sid, "mid": r["mid"]}, timeout=15)
        except Exception:
            pass


if __name__ == "__main__":
    main()
