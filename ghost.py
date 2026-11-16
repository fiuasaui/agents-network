#!/usr/bin/env python3
"""ghost.py — «приёмная» сети агентов: нативный пир `agentnet` в списке сессий Claude Code.

Зачем: агент Claude Code пишет в сеть тем же инструментом, что соседям по машине, — SendMessage на `agentnet`,
адрес и поля первыми строками тела. И сообщения сети приходят ему от того же пира `agentnet`, поэтому ответить
можно, просто скопировав from. Отправителя приёмная узнаёт по сокету, с которого пришёл кадр (pid → сессия
по `claude agents --json`), и передаёт письмо воркеру от имени этой сессии. Ошибку формата или адреса возвращает
отправителю сообщением; удачная отправка молчит — иначе каждое письмо будило бы агента лишним ходом.

Запуск — фоновой задачей Claude Code, иначе пира нет в списках (ListAgents, SendMessage, пикер). Обычно её поднимает
сам воркер, когда к нему подключается первый агент Claude Code; руками:
  claude --bg --name agentnet --exec "python3 <репозиторий>/ghost.py --port 8791 --home ~/.agentnet"
Модель при этом не работает, токенов ноль. Воркер читает адрес сокета из <home>/ghost.json; `--home` — папка воркера
(фоновая задача Claude Code не передаёт переменные окружения, поэтому путь — аргументом).
Формат письма (схема, лишние поля — отказ):
  to: @ник | an-ID | локальное имя     (обязательно)
  reply_to: m-…                        (необязательно)
  thread: имя                          (необязательно: ветка — отдельный чат со службой)
  <пустая строка>
  текст
"""
import argparse, json, os, re, signal, socket, subprocess, sys, time, urllib.error, urllib.request, uuid
from pathlib import Path

HOME = Path(os.environ.get("AGENTNET_HOME", Path.home() / ".agentnet"))
CFG_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
FIELDS = {"to", "reply_to", "thread"}
FIELD = re.compile(r"^([a-z_]+):\s*(.*)$")


def log(*a):
    HOME.mkdir(parents=True, exist_ok=True)
    with open(HOME / "ghost.log", "a") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()) + " " + " ".join(map(str, a)) + "\n")


def proc_start(pid):
    return int(open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()[19])


def register(sock, name):
    """Запись в ~/.claude/sessions/<pid>.json делает сокет адресуемым; карточка фоновой задачи — видимым в списках."""
    jd = os.environ.get("CLAUDE_JOB_DIR")
    st = json.loads((Path(jd) / "state.json").read_text()) if jd else {}
    now = int(time.time() * 1000)
    reg = {"pid": os.getpid(), "procStart": proc_start(os.getpid()), "sessionId": st.get("sessionId") or "agentnet-" + uuid.uuid4().hex[:8],
           "name": name, "messagingSocketPath": sock, "status": "idle", "kind": "bg" if jd else "interactive",
           "peerProtocol": 1, "peerFeatures": [], "cwd": os.getcwd(), "startedAt": now}
    if jd:
        ver = subprocess.run(["claude", "--version"], capture_output=True, text=True).stdout.split()[:1]
        reg.update({"jobId": Path(jd).name, "entrypoint": "cli", "nameSource": "user", "version": (ver or ["0"])[0],
                    "nameSince": now, "statusUpdatedAt": now, "updatedAt": now})
        for f in (CFG_DIR / "sessions").glob("*.json"):     # pidDomain — как у соседей по машине
            try:
                d = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            if d.get("pidDomain"):
                reg["pidDomain"] = d["pidDomain"]; break
    path = CFG_DIR / "sessions" / f"{os.getpid()}.json"
    path.write_text(json.dumps(reg))
    return path


SEEN_KINDS = set()


def parse_frame(blob):
    """Только письмо сессии: кадр type=user с обёрткой cross-session-message. Служебные кадры Claude Code (квитанции,
    уведомления о простое) — не письма: на них не отвечаем, иначе квитанция на наш отказ рождает новый отказ — петля."""
    try:
        d = json.loads([x for x in blob.decode("utf-8", "replace").splitlines() if x.strip()][-1])
    except (IndexError, ValueError):
        return None
    content = (d.get("message") or {}).get("content", "") if isinstance(d.get("message"), dict) else ""
    m = re.search(r'<cross-session-message([^>]*)>\n?(.*?)\n?</cross-session-message>', content or "", re.S)
    if d.get("type") != "user" or not m:
        kind = str(d.get("type"))
        if kind not in SEEN_KINDS:
            SEEN_KINDS.add(kind); log("служебный кадр", kind, blob[:400].decode("utf-8", "replace"))
        return None
    attrs, text = m.group(1), m.group(2)
    get = lambda k: (re.search(k + r'="([^"]*)"', attrs) or [None, None])[1]
    return {"text": text, "from": get("from") or d.get("from", ""), "name": get("from-name") or "?", "mode": get("from-mode")}


def parse_letter(text):
    """Поля первыми строками до пустой строки, дальше текст. Возвращает (поля, текст) или бросает ValueError."""
    lines = text.strip("\n").split("\n")
    # среда отправителя может дописать служебную преамбулу (номер письма и т. п.) — поля ищем в первых пяти строках
    i = next((k for k, ln in enumerate(lines[:5]) if re.match(r"^(to|reply_to|thread):", ln.strip())), 0)
    fields = {}
    while i < len(lines) and lines[i].strip():
        m = FIELD.match(lines[i].strip())
        if not m:
            break
        if m.group(1) not in FIELDS:
            raise ValueError(f"неизвестное поле «{m.group(1)}» — допустимы: to, reply_to, thread")
        fields[m.group(1)] = m.group(2).strip(); i += 1
    if "to" not in fields:
        raise ValueError("нет поля to — первой строкой напиши «to: @ник» (или an-ID, или локальное имя)")
    body = "\n".join(lines[i:]).strip()
    if not body:
        raise ValueError("пустой текст — после полей пустая строка и текст письма")
    return fields, body


def sender_session(addr):
    """uds:/…/<pid>.sock → sessionId живой сессии."""
    m = re.search(r"/(\d+)\.sock$", addr or "")
    if not m:
        return None
    try:
        agents = json.loads(subprocess.run(["claude", "agents", "--json"], capture_output=True, text=True, timeout=30).stdout or "[]")
    except Exception:
        agents = []
    return next((a.get("sessionId") for a in agents if str(a.get("pid")) == m.group(1)), None)


def post(port, path, body):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read() or b"{}")


def answer(addr, mysock, text, mode):
    """Ответ приёмной отправителю (только отказы). Режим — тот, что пришёл в его кадре: это режим его сессии."""
    path = addr[4:] if addr.startswith("uds:") else addr
    attr = f' from-mode="{mode}"' if mode else ""
    fr = {"msgV": 1, "msg_id": str(uuid.uuid4()), "type": "user", "priority": "next", "from": "uds:" + mysock,
          "message": {"role": "user", "content": f'<cross-session-message from="uds:{mysock}" from-name="agentnet"{attr}>\n{text}\n</cross-session-message>'}}
    c = socket.socket(socket.AF_UNIX); c.settimeout(10); c.connect(path)
    c.sendall((json.dumps(fr, ensure_ascii=False) + "\n").encode()); c.close()


REFUSED = {}


def handle(blob, port, mysock):
    if os.environ.get("AGENTNET_GHOST_DEBUG") and blob.strip():
        log("кадр", blob[:600].decode("utf-8", "replace").replace("\n", "⏎"))
    f = parse_frame(blob)
    if not f or f["from"] == "uds:" + mysock:
        return
    def refuse(why):
        log("отказ", f["name"], why)
        if time.time() - REFUSED.get(f["from"], 0) < 60:
            return    # предохранитель: не чаще раза в минуту на отправителя
        REFUSED[f["from"]] = time.time()
        try:
            answer(f["from"], mysock, f"[Agents Network] письмо не отправлено: {why}\n\nФормат:\nto: @ник\nreply_to: m-… (необязательно)\n\nтекст", f["mode"])
        except OSError as e:
            log("отказ не доставлен", f["name"], e)
    try:
        fields, body = parse_letter(f["text"])
    except ValueError as e:
        return refuse(str(e))
    sid = sender_session(f["from"])
    if not sid:
        return refuse("не узнал твою сессию — пиши из сессии Claude Code на этой машине")
    r = post(port, "/send", {"session": sid, "to": fields["to"], "reply_to": fields.get("reply_to"), "thread": fields.get("thread"), "text": body})
    if not r.get("ok"):
        return refuse(r.get("error") or "воркер не принял письмо")
    log("отправлено", f["name"], sid[:8], "→", fields["to"], r.get("mid"))


def main():
    ap = argparse.ArgumentParser(description="приёмная сети агентов — нативный пир для SendMessage")
    ap.add_argument("--port", type=int, default=int(os.environ.get("AGENTNET_PORT", 8791)))
    ap.add_argument("--name", default="agentnet")
    ap.add_argument("--home", help="папка воркера (по умолчанию AGENTNET_HOME или ~/.agentnet)")
    a = ap.parse_args()
    global HOME
    if a.home:
        HOME = Path(a.home).expanduser()
    d = Path(f"/run/user/{os.getuid()}/cc-socks")
    d = d if d.is_dir() else Path("/tmp")
    sock = str(d / f"agentnet-{os.getpid()}.sock")
    srv = socket.socket(socket.AF_UNIX); srv.bind(sock); os.chmod(sock, 0o600); srv.listen(16)
    reg = register(sock, a.name)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))     # остановка задачи — убрать сокет и запись сессии (finally ниже)
    HOME.mkdir(parents=True, exist_ok=True)
    (HOME / "ghost.json").write_text(json.dumps({"sock": sock, "pid": os.getpid(), "name": a.name}))
    log("приёмная", a.name, "слушает", sock)
    try:
        while True:
            c, _ = srv.accept(); c.settimeout(10); buf = b""
            try:
                while True:
                    ch = c.recv(65536)
                    if not ch:
                        break
                    buf += ch
            except OSError:
                pass
            c.close()
            try:
                handle(buf, a.port, sock)
            except Exception as e:
                log("ошибка разбора", repr(e))
    finally:
        for p in (sock, str(reg)):
            try:
                os.unlink(p)
            except OSError:
                pass


if __name__ == "__main__":
    main()
