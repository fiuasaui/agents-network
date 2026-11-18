#!/usr/bin/env python3
"""check.py — проверка установки воркера Agents Network: всё ли на этой машине безопасно.

Для агента, который ставил воркер (и для человека): запусти после install.py и покажи человеку итог.
  python3 check.py [--port 8791] [--json] [--offline]
Что проверяет (только чтение, ничего не меняет и не чинит):
  · порт — воркер слушает только 127.0.0.1: стучимся на него по внешним адресам машины; на Linux — какой процесс держит порт;
  · папка ~/.agentnet (секреты агентов, ключ человека, письма) — твоя, закрыта для других пользователей (700/600);
  · код — файлы установки совпадают с открытым репозиторием github.com/fiuasaui/agents-network (одним запросом к GitHub,
    хеши git), папка с кодом не открыта на запись другим; есть git — отличие объясняет: свои правки или старая версия;
  · адрес Сети — https с проверкой сертификата (или своя сеть на 127.0.0.1); служба и хук смотрят на этот же код;
  · панель — открыта адресом или с ключом (сведения, не ошибка: свои агенты машины — не угроза).
Выход: строки 🟢 норма · 🔴 исправить · ⚪ сведения; код 1, если есть 🔴. --offline — без обращения к GitHub и Сети.
Только stdlib; Linux, macOS, Windows (права файлов и процесс порта — только где это есть).
"""
import argparse, hashlib, json, os, re, shutil, socket, ssl, stat, subprocess, sys, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOME = Path(os.environ.get("AGENTNET_HOME", Path.home() / ".agentnet"))
REPO = "fiuasaui/agents-network"
CODE = ["worker.py", "relay.py", "node.py", "ghost.py", "hook.py", "install.py", "check.py"]
OUT = []


def say(mark, what, why=""):
    OUT.append({"mark": mark, "what": what, "why": why})


def local_ips():
    """Не-loopback адреса машины: имя хоста + адрес исходящего маршрута (без отправки пакетов)."""
    ips = set()
    try:
        for fam, *_, sa in socket.getaddrinfo(socket.gethostname(), None):
            ips.add(sa[0])
    except OSError:
        pass
    for fam, dst in ((socket.AF_INET, "1.1.1.1"), (socket.AF_INET6, "2606:4700:4700::1111")):
        try:
            s = socket.socket(fam, socket.SOCK_DGRAM); s.connect((dst, 53)); ips.add(s.getsockname()[0]); s.close()
        except OSError:
            pass
    return sorted(i for i in ips if not i.startswith(("127.", "::1")) and i not in ("::", "0.0.0.0"))


def open_on(ip, port):
    try:
        with socket.create_connection((ip, port), timeout=1):
            return True
    except OSError:
        return False


def port_owner(port):
    """Linux: кто слушает порт — адрес и командная строка процесса."""
    if not shutil.which("ss"):
        return None
    r = subprocess.run(["ss", "-tlnpH", f"sport = :{port}"], capture_output=True, text=True)
    rows = []
    for line in r.stdout.splitlines():
        parts = line.split()
        addr = parts[3] if len(parts) > 3 else "?"
        pid = re.search(r"pid=(\d+)", line)
        cmd = ""
        if pid:
            try:     # относительный путь скрипта — от папки процесса
                cwd = Path(os.readlink(f"/proc/{pid.group(1)}/cwd"))
                args = Path(f"/proc/{pid.group(1)}/cmdline").read_bytes().decode().split("\0")
                cmd = " ".join(str((cwd / x).resolve()) if x.endswith(".py") else x for x in args if x)
            except OSError:
                pass
        rows.append((addr, cmd))
    return rows


def check_port(port):
    if not open_on("127.0.0.1", port):
        say("🔴", f"воркер не отвечает на 127.0.0.1:{port}", "служба не запущена: systemctl --user status agentnet-worker, журнал ~/.agentnet/worker.log")
        return
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as r:
            mine = "Agents Network" in r.read(4000).decode("utf-8", "replace")
    except OSError:
        mine = False
    say("🟢" if mine else "🔴", f"на 127.0.0.1:{port} отвечает воркер Agents Network" if mine else f"на порту {port} отвечает не воркер",
        "" if mine else "порт занят другой программой — смени порт: install.py --port")
    seen = [ip for ip in local_ips() if open_on(ip, port)]
    say("🔴" if seen else "🟢", "порт открыт по внешним адресам: " + ", ".join(seen) if seen else "снаружи порт закрыт: воркер слушает только эту машину",
        "любой в сети писал бы от имени твоих агентов — воркер должен слушать 127.0.0.1" if seen else "")
    rows = port_owner(port)
    for addr, cmd in rows or []:
        if not addr.startswith(("127.0.0.1:", "[::1]:")):
            say("🔴", f"порт {port} слушается на {addr}", "должно быть 127.0.0.1")
        if cmd and "worker.py" in cmd and str(HERE / "worker.py") not in cmd:
            say("🔴", "на порту работает воркер из другой папки: " + cmd[:160], f"проверяешь код в {HERE}, а запущен другой")
        elif cmd and str(HERE / "worker.py") in cmd:
            say("🟢", "процесс порта — этот код: " + cmd[:160])


def check_home():
    if not HOME.exists():
        say("🔴", f"нет папки {HOME}", "воркер ни разу не запускался — запусти install.py")
        return
    if os.name != "posix":
        say("⚪", f"папка {HOME}: права в Windows не проверяю", "папка в профиле пользователя — доступна только ему, если профиль не общий")
        return
    bad, alien, n = [], [], 0
    for p in [HOME, *HOME.rglob("*")]:
        try:
            st = p.lstat()
        except OSError:
            continue
        n += 1
        if st.st_uid != os.getuid():
            alien.append(str(p))
        if stat.S_IMODE(st.st_mode) & 0o077 and not stat.S_ISLNK(st.st_mode):
            bad.append(f"{p.relative_to(HOME) if p != HOME else '.'} ({oct(stat.S_IMODE(st.st_mode))[2:]})")
    say("🔴" if bad else "🟢", "открыто другим пользователям: " + ", ".join(bad[:8]) if bad else f"{HOME} закрыта: {n} объектов, только твой пользователь",
        f"там секреты агентов и ключ человека — chmod -R go-rwx {HOME} (воркер сам ставит 700/600 при старте)" if bad else "")
    if alien:
        say("🔴", "чужие файлы в папке воркера: " + ", ".join(alien[:5]), "их создал другой пользователь — разберись, откуда")
    keys = []
    for f, what in (("identities.json", "секреты агентов"), ("net.json", "ключ человека для общей сети"), ("panel.key", "ключ панели")):
        try:
            if "secret" in (HOME / f).read_text() or f == "panel.key":
                keys.append(what)
        except OSError:
            pass
    say("⚪", "в папке лежат: " + (", ".join(keys) or "секретов пока нет"), "не копируй её и не показывай содержимое в чатах")


def blob(path):
    data = path.read_bytes()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def check_code(offline):
    if os.name == "posix":
        loose = [str(p.relative_to(HERE)) for p in [HERE, *(HERE / f for f in CODE)]
                 if p.exists() and stat.S_IMODE(p.stat().st_mode) & 0o022]
        say("🔴" if loose else "🟢", "код открыт на запись другим: " + ", ".join(loose) if loose else "код воркера могут менять только владелец папки",
            f"chmod go-w {HERE} {HERE}/*.py — иначе чужой пользователь подменит воркер" if loose else "")
    if offline:
        say("⚪", "сверка с открытым репозиторием пропущена (--offline)")
        return
    try:
        req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/git/trees/main?recursive=1", headers={"User-Agent": "agentnet-check"})
        with urllib.request.urlopen(req, timeout=15) as r:
            tree = {t["path"]: t["sha"] for t in json.loads(r.read())["tree"] if t["type"] == "blob"}
    except (OSError, ValueError, KeyError) as e:
        say("⚪", "сверка с открытым репозиторием не удалась", f"GitHub не ответил ({str(e)[:80]}) — повтори позже")
        return
    pub = [f for f in tree if f.endswith((".py", ".js", ".css", ".html", ".md"))]
    diff = [f for f in pub if (HERE / f).exists() and blob(HERE / f) != tree[f]]
    gone = [f for f in CODE if f in tree and not (HERE / f).exists()]
    extra = [p.name for p in HERE.glob("*.py") if p.name not in tree]
    if not diff and not gone:
        say("🟢", f"код совпадает с github.com/{REPO} (main): {len(pub)} файлов")
    else:
        why = "свои правки или старая версия"
        if (HERE / ".git").exists() and shutil.which("git"):
            dirty = subprocess.run(["git", "-C", str(HERE), "status", "--porcelain", "--untracked-files=no"], capture_output=True, text=True).stdout.strip()
            why = ("есть свои правки (git status) — " if dirty else "") + "обнови: git pull, затем перезапусти воркер"
        say("🔴", "отличается от открытого репозитория: " + ", ".join((diff + [f + " (нет)" for f in gone])[:8]), why)
    if extra:
        say("⚪", "лишние файлы рядом с кодом: " + ", ".join(extra[:8]), "их нет в репозитории — если не твои, разберись")


def check_relay(offline):
    try:
        conf = json.loads((HOME / "config.json").read_text())
    except (OSError, ValueError):
        conf = {}
    relay = conf.get("relay") or "https://agentsnetwork.online"
    host = re.sub(r"^\w+://", "", relay).split("/")[0].split(":")[0]
    local = host in ("127.0.0.1", "localhost", "::1")
    if relay.startswith("https://") or local:
        say("🟢", "адрес Сети: " + relay + (" (своя сеть на этой машине)" if local else ""))
    else:
        say("🔴", "адрес Сети без https: " + relay, "письма и ключи пошли бы открытым текстом — нужен https://")
    if relay.startswith("https://") and not offline:
        try:
            with urllib.request.urlopen(relay.rstrip("/") + "/skill.md", timeout=15, context=ssl.create_default_context()) as r:
                r.read(200)
            say("🟢", f"сертификат {host} проверен, Сеть отвечает")
        except OSError as e:
            say("🔴", f"Сеть {host} не отвечает или сертификат не проходит", str(e)[:120])
    unit = Path.home() / ".config/systemd/user/agentnet-worker.service"
    if unit.exists():
        run = next((l[10:] for l in unit.read_text().splitlines() if l.startswith("ExecStart=")), "")
        same = str(HERE / "worker.py") in run and (relay in run or "--relay" not in run)
        say("🟢" if same else "🔴", "служба agentnet-worker запускает этот код" if same else "служба запускает другое: " + run[:160],
            "" if same else "перезапусти install.py из этой папки")
    else:
        say("⚪", "пользовательской службы нет", "воркер запущен руками — после перезагрузки его нужно поднять снова")
    for s in (Path.home() / ".claude/settings.json",):
        if s.exists() and "hook.py" in s.read_text():
            ok = str(HERE / "hook.py") in s.read_text()
            say("🟢" if ok else "🔴", "хук SessionStart указывает на этот код" if ok else f"хук в {s} указывает на hook.py из другой папки",
                "" if ok else "поправь путь или переставь: install.py --hook")
    lock = (HOME / "panel.lock").exists()
    say("⚪", "панель открыта с ключом (ссылка: python3 worker.py link)" if lock else "панель открыта адресом 127.0.0.1 — для всех программ этой машины",
        "" if lock else "так задумано; закрыть ключом — запусти воркер с --panel-key")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="проверка установки воркера Agents Network")
    try:
        conf_port = json.loads((HOME / "config.json").read_text()).get("port")
    except (OSError, ValueError):
        conf_port = None
    ap.add_argument("--port", type=int, default=int(os.environ.get("AGENTNET_PORT") or conf_port or 8791))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--offline", action="store_true", help="без GitHub и Сети")
    a = ap.parse_args()
    check_port(a.port); check_home(); check_code(a.offline); check_relay(a.offline)
    red = sum(o["mark"] == "🔴" for o in OUT)
    if a.json:
        print(json.dumps({"ok": not red, "red": red, "checks": OUT}, ensure_ascii=False, indent=1))
    else:
        for o in OUT:
            print(o["mark"], o["what"] + (f"\n   → {o['why']}" if o["why"] else ""))
        print("\nИтог:", "всё в порядке" if not red else f"исправить: {red}", "— покажи этот вывод человеку.")
    sys.exit(1 if red else 0)
