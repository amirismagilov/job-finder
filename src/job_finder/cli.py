# region MODULE_CONTRACT [DOMAIN(9): LocalControl; CONCEPT(10): OneTimeConsent, SecretInput; TECH(8): argparse]
## @file cli.py
## @brief Local controls for pairing, dry-run, autonomy, daemon and safety stop.
## @modulecontract
## @purpose Let the user configure and operate the agent without placing any secret in argv, config or logs.
## @scope CLI parsing, lifecycle and LaunchAgent installation.
## @input Hidden pairing/HTTP-provider secret and explicit consent phrases.
## @output Non-secret JSON status/reports.
## @invariants Dry-run is the run-once default; autonomous writes require stored opt-in; optional HTTP API keys are accepted only by getpass.
## @changes LAST_CHANGE: [v0.3.0 — Clarified that Keychain API-key setup applies only to the optional HTTP provider.]
## @modulemap
## FUNC 10[Defines local control surface] => build_parser
## FUNC 10[Dispatches safe runtime commands] => main
def _module_contract() -> None:
    pass
# endregion MODULE_CONTRACT
# GREP_SUMMARY: CLI, bridge pair, autonomy enable, dry-run, daemon, Keychain, LaunchAgent, hard stop
# STRUCTURE: parse -> hidden secret/consent gate -> compose service -> start/run/status -> safe JSON

import argparse
from getpass import getpass
import json
import logging
from pathlib import Path
import signal
import sys
import time

from .app import create_service
from .config import load_config
from .keychain import MacOSKeychain


ENABLE_PHRASE = "ВКЛЮЧИТЬ АВТОНОМНЫЙ РЕЖИМ"
CLEAR_STOP_PHRASE = "СНЯТЬ БЛОКИРОВКУ"


def _print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _pair(store: MacOSKeychain) -> None:
    print("Создайте pairing secret на странице настроек расширения и вставьте его ниже. Ввод скрыт.")
    value = getpass("pairing secret: ").strip().lower()
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise SystemExit("Pairing secret должен состоять из 64 шестнадцатеричных символов")
    store.set("bridge_pairing_secret", value)
    # BUG_FIX_CONTEXT: An empty stored binding had ambiguous presence semantics in Keychain;
    # deletion makes the next authenticated extension poll the only re-binding event.
    store.delete("bridge_extension_id")
    print("Pairing сохранён в macOS Keychain; значение не выводится.")


def _install_launch_agent(root: Path, interval: int) -> Path:
    executable = root / ".venv/bin/job-finder"
    destination = Path.home() / "Library/LaunchAgents/ru.hh.amir-job-finder.plist"
    destination.parent.mkdir(parents=True, exist_ok=True)
    xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>ru.hh.amir-job-finder</string>
<key>ProgramArguments</key><array><string>{executable}</string><string>daemon</string><string>run</string><string>--interval</string><string>{interval}</string></array>
<key>WorkingDirectory</key><string>{root}</string>
<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
</dict></plist>'''
    destination.write_text(xml, encoding="utf-8")
    destination.chmod(0o600)
    return destination


# region FUNC_build_parser [DOMAIN(8): LocalControl; CONCEPT(9): ExplicitCommands; TECH(8): argparse]
## @purpose Define a discoverable CLI with no secret-bearing positional or option arguments.
## @io None -> ArgumentParser
## @complexity 5
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="job-finder", description="Локальный автономный робот hh.ru 0.2")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="Безопасный локальный статус")

    bridge = sub.add_parser("bridge", help="Browser session bridge")
    bridge_sub = bridge.add_subparsers(dest="bridge_command", required=True)
    bridge_sub.add_parser("pair", help="Сохранить созданный расширением pairing secret через скрытый ввод")
    bridge_sub.add_parser("run", help="Запустить loopback bridge до Ctrl-C")

    llm = sub.add_parser("llm", help="Настройка AI provider")
    llm_sub = llm.add_subparsers(dest="llm_command", required=True)
    llm_sub.add_parser("set-key", help="Сохранить ключ HTTP LLM provider в Keychain через скрытый ввод; codex_cli ключ не требует")

    autonomy = sub.add_parser("autonomy", help="Однократное включение/отключение автономных записей")
    autonomy_sub = autonomy.add_subparsers(dest="autonomy_command", required=True)
    autonomy_sub.add_parser("enable", help="Включить после точной локальной фразы")
    autonomy_sub.add_parser("disable", help="Немедленно запретить записи")

    run = sub.add_parser("run", help="Выполнение одного цикла")
    run_sub = run.add_subparsers(dest="run_command", required=True)
    once = run_sub.add_parser("once", help="Один цикл; по умолчанию dry-run")
    once.add_argument("--live", action="store_true", help="Разрешить записи только при ранее включённой автономии")

    daemon = sub.add_parser("daemon", help="Постоянный worker и bridge")
    daemon_sub = daemon.add_subparsers(dest="daemon_command", required=True)
    daemon_run = daemon_sub.add_parser("run", help="Запустить цикл по расписанию; opt-in определяет live/dry-run")
    daemon_run.add_argument("--interval", type=int, default=None)
    daemon_sub.add_parser("install-launch-agent", help="Создать приватный LaunchAgent plist")

    safety = sub.add_parser("safety", help="Управление hard stop")
    safety_sub = safety.add_subparsers(dest="safety_command", required=True)
    safety_sub.add_parser("clear-stop", help="Снять блокировку после ручной проверки hh.ru")

    audit = sub.add_parser("audit", help="Обезличенный локальный аудит")
    audit.add_argument("--limit", type=int, default=20)
    return parser
# endregion FUNC_build_parser


# region FUNC_main [DOMAIN(9): LocalControl; CONCEPT(10): ConsentGate; TECH(8): ProcessLifecycle]
## @purpose Dispatch commands while ensuring no autonomous write can be enabled implicitly.
## @io argv -> process result
## @complexity 8
def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = build_parser().parse_args()
    store = MacOSKeychain()
    if args.command == "bridge" and args.bridge_command == "pair":
        _pair(store)
        return
    if args.command == "llm" and args.llm_command == "set-key":
        value = getpass("HTTP LLM API key (ввод скрыт): ").strip()
        if not value:
            raise SystemExit("Пустой ключ не сохранён")
        store.set("llm_api_key", value)
        print("Ключ сохранён в macOS Keychain.")
        return

    service = create_service(secrets_store=store)
    if args.command == "status":
        _print_json(service.status())
    elif args.command == "autonomy":
        if args.autonomy_command == "disable":
            service.storage.set_autonomy(False)
            print("Автономные записи отключены.")
        else:
            phrase = input(f"Введите точную фразу {ENABLE_PHRASE}: ").strip()
            if phrase != ENABLE_PHRASE:
                raise SystemExit("Фраза не совпала; автономия не включена")
            if not service.bridge.paired:
                raise SystemExit("Сначала выполните: job-finder bridge pair")
            service.storage.set_autonomy(True)
            print("Автономный режим включён однократно. Отдельные подтверждения действий не требуются.")
    elif args.command == "safety" and args.safety_command == "clear-stop":
        phrase = input(f"После ручной проверки аккаунта введите {CLEAR_STOP_PHRASE}: ").strip()
        if phrase != CLEAR_STOP_PHRASE:
            raise SystemExit("Фраза не совпала; блокировка сохранена")
        service.storage.clear_hard_stop()
        print("Safety-stop снят.")
    elif args.command == "run" and args.run_command == "once":
        _print_json(service.run_cycle(dry_run=not args.live))
        service.stop_bridge()
    elif args.command == "bridge" and args.bridge_command == "run":
        service.start_bridge()
        print("Bridge слушает только 127.0.0.1:8766. Ctrl-C для остановки.")
        try:
            signal.pause()
        except KeyboardInterrupt:
            service.stop_bridge()
    elif args.command == "daemon" and args.daemon_command == "install-launch-agent":
        cfg = load_config()
        destination = _install_launch_agent(cfg.root, cfg.autonomy.cycle_interval_seconds)
        print(f"LaunchAgent создан: {destination}. Загрузите его вручную после dry-run проверки.")
    elif args.command == "daemon" and args.daemon_command == "run":
        interval = max(60, args.interval or service.config.autonomy.cycle_interval_seconds)
        service.start_bridge()
        try:
            while True:
                _print_json(service.worker.run_cycle(dry_run=None).to_dict())
                time.sleep(interval)
        except KeyboardInterrupt:
            service.stop_bridge()
    elif args.command == "audit":
        _print_json(service.recent_audit(limit=args.limit))
    else:
        raise SystemExit("Неизвестная команда")
# endregion FUNC_main


if __name__ == "__main__":
    main()
