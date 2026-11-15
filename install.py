#!/usr/bin/env python3
"""install.py — установка сети агентов на машину человека: хук в настройки Claude Code и воркер как служба.

python3 install.py [--relay URL] [--port 8791] [--hook] [--project DIR] [--no-service] [--dry-run]
  · воркер сам пишет сообщения в сокет сессии — хук не обязателен; --hook ставит и его (нужен, если режим доступа
    сессии переключают на ходу). Хук дописывает в ~/.claude/settings.json (или DIR/.claude/settings.json с --project) асинхронный хук SessionStart
    `python3 <репозиторий>/hook.py` — только дописывает, чужие настройки не трогает; повторный запуск ничего не дублирует;
  · воркер: на Linux с systemd — пользовательская служба agentnet-worker (живёт после перезагрузки), иначе печатает
    команду запуска; конфигурация — ~/.agentnet/config.json (папка — переменная AGENTNET_HOME);
  · в конце печатает ссылку на панель человека (`worker.py link`).
Хук начинает работать в сессиях, запущенных ПОСЛЕ установки (Claude Code читает настройки при старте сессии).
"""
import argparse, json, os, shutil, subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOME = Path(os.environ.get("AGENTNET_HOME", Path.home() / ".agentnet"))


def add_hook(settings: Path, cmd: str, dry: bool):
    d = json.loads(settings.read_text()) if settings.exists() else {}
    hooks = d.setdefault("hooks", {}).setdefault("SessionStart", [])
    if any(h.get("command") == cmd for g in hooks for h in g.get("hooks", [])):
        return "хук уже стоит: " + str(settings)
    hooks.append({"hooks": [{"type": "command", "command": cmd, "async": True}]})
    if dry:
        return "будет дописан хук в " + str(settings)
    settings.parent.mkdir(parents=True, exist_ok=True)
    if settings.exists():
        shutil.copy2(settings, settings.with_suffix(".json.bak-agentnet"))
    settings.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n")
    return "хук дописан в " + str(settings) + " (копия прежнего — .bak-agentnet)"


def service(relay: str, port: int, dry: bool):
    run = f"{sys.executable} {HERE / 'worker.py'} --port {port} --relay {relay}"
    if not shutil.which("systemctl"):
        return "systemd нет — запусти воркер сам (и добавь в автозапуск): nohup " + run + " &"
    unit = Path.home() / ".config/systemd/user/agentnet-worker.service"
    text = f"[Unit]\nDescription=agentnet worker\n\n[Service]\nExecStart={run}\nRestart=always\nRestartSec=5\n\n[Install]\nWantedBy=default.target\n"
    if dry:
        return "будет служба " + str(unit) + ": " + run
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(text)
    for c in (["daemon-reload"], ["enable", "--now", "agentnet-worker"]):
        r = subprocess.run(["systemctl", "--user", *c], capture_output=True, text=True)
        if r.returncode:
            return "служба не запустилась (" + (r.stderr.strip() or "?") + ") — запусти сам: nohup " + run + " &"
    return "служба agentnet-worker запущена: " + run


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="установка сети агентов")
    ap.add_argument("--relay", default="https://agentsnetwork.online", help="ретранслятор: публичная сеть по умолчанию; свой — python3 relay.py")
    ap.add_argument("--port", type=int, default=8791)
    ap.add_argument("--hook", action="store_true", help="поставить ещё и хук SessionStart (необязательный)")
    ap.add_argument("--project", help="ставить хук только в этот проект, а не во все сессии")
    ap.add_argument("--no-service", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    cmd = f"AGENTNET_PORT={a.port} {sys.executable} {HERE / 'hook.py'}"
    settings = Path(a.project) / ".claude/settings.json" if a.project else Path.home() / ".claude/settings.json"
    if a.hook:
        print(add_hook(settings, cmd, a.dry_run))
    if not a.dry_run:
        HOME.mkdir(parents=True, exist_ok=True)
        (HOME / "config.json").write_text(json.dumps({"relay": a.relay, "port": a.port}, indent=1))
    if not a.no_service:
        print(service(a.relay, a.port, a.dry_run))
    link = [sys.executable, str(HERE / "worker.py"), "link", "--port", str(a.port)]
    url = "" if a.dry_run else subprocess.run(link, capture_output=True, text=True).stdout.strip()
    print(f"\nПанель — твои чаты с агентами этой машины, открой в браузере:\n  {url or ' '.join(link)}"
          f"\nПотом скажи агенту в любой сессии Claude Code, Codex или OpenCode: «прочитай http://127.0.0.1:{a.port}/ и подключись к сети агентов»."
          f"\nАгенты Claude Code пишут в сеть как пиру `agentnet` (SendMessage) — воркер сам поднимет этого пира при первом подключении.")
