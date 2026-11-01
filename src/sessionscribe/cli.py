from __future__ import annotations

import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(prog="sessionscribe")
    commands = parser.add_subparsers(dest="command", required=True)
    example = commands.add_parser(
        "demo", help="synthetic audio, STT fixture, cloud fixture; no network"
    )
    example.add_argument("--root", type=Path, required=True)
    example.add_argument("--mode", choices=("meeting", "dnd"), default="meeting")
    bot = commands.add_parser("bot", help="Discord control bot; voice remains gated")
    bot.add_argument("--policy", type=Path, required=True)
    health = commands.add_parser("control-health", help="fresh control readiness for one revision")
    health.add_argument("--root", type=Path, required=True)
    health.add_argument("--revision", required=True)
    commands.add_parser("evaluate-worker", help="isolated evaluator with fixed container mounts")
    commands.add_parser("stt-worker", help="offline single-job worker with fixed container mounts")
    evaluate = commands.add_parser("evaluate", help="explicit authorized inference gateway call")
    evaluate.add_argument("--root", type=Path, required=True)
    evaluate.add_argument("--config", type=Path, required=True)
    evaluate.add_argument("--session", required=True)
    evaluate.add_argument("--gateway", required=True)
    evaluate.add_argument("--credential-file", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "control-health":
        from .health import control_ready

        raise SystemExit(0 if control_ready(args.root, args.revision) else 1)
    elif args.command == "bot":
        from .bot import run

        run(args.policy)
    elif args.command == "demo":
        from .demo import demo

        print(json.dumps(demo(args.root, args.mode), ensure_ascii=False, indent=2))
    elif args.command == "stt-worker":
        from .stt import run_handoff

        run_handoff(Path("/input"), Path("/output"), Path("/model"))
    elif args.command == "evaluate-worker":
        from .evaluator import run_handoff
        from .models import Config
        from .unix_gateway import configured_gateway

        config = Config.model_validate_json(Path("/config/instance.json").read_bytes())
        gateway = configured_gateway(
            Path("/config/gateway-origin").read_text().strip(),
            Path("/run/secrets/gateway-token").read_text().strip(),
        )
        run_handoff(Path("/input"), Path("/state"), Path("/output"), config, gateway)
    elif args.command == "evaluate":
        from .analysis import Gateway, evaluate
        from .models import Config
        from .store import Store

        config = Config.model_validate_json(args.config.read_bytes())
        store = Store(args.root, config.tenant)
        try:
            evaluate(
                store,
                args.session,
                Gateway(args.gateway, args.credential_file.read_text().strip()),
                enabled=config.cloud_enabled,
                max_calls=config.daily_cloud_calls,
                max_tokens=config.daily_cloud_tokens,
            )
        finally:
            store.close()


if __name__ == "__main__":
    main()
