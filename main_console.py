"""Console driver for the message-driven审核 workflow.

This is the console counterpart of the Feishu webhook: it feeds typed messages
through the SAME chain the message channels use —

    text -> parse_user_command -> TriggerService -> SceneForge tools

so you can exercise everything locally now and just swap in Feishu credentials
later without changing the workflow logic.

Usage:
  python main_console.py                      # interactive REPL
  python main_console.py --once "查看状态"      # run a single command
  python main_console.py --workspace .         # workspace root (default: cwd)

Commands that DO NOT call any paid API (safe to try first):
  查看状态 / 暂停 / 继续 / 取消 / 通过 / 修改：...   (approve/revise only touch ReviewTasks)
Commands that DO call the LLM / image / video models (need configs/agent.local.yaml keys):
  做一个关于…的短片   (new_topic -> narrative planning)
  重生成第 N 镜       (regenerate -> needs an existing render)
"""

import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import argparse
import asyncio
import json
from pathlib import Path

from agent_runtime.session_factory import create_session_index
from agent_runtime.sceneforge_adapters import SceneForgeAdapters
from commands import parse_user_command
from services import Authorizer, InboundRateLimiter, TriggerService, WorkflowEngine


def build_trigger(workspace: str, session_backend: str | None = None) -> TriggerService:
    root = Path(workspace).resolve()
    index = create_session_index(root, backend=session_backend)
    adapters = SceneForgeAdapters(root, index)
    # Staged workflow: topic -> script -> storyboard -> video -> final, each gated by review.
    engine = WorkflowEngine(index, root, adapters=adapters)
    # Console is a trusted local entry point: allow-all authorizer, no rate cap.
    return TriggerService(index, adapters, authorizer=Authorizer(), rate_limiter=InboundRateLimiter(), workflow_engine=engine)


async def run_once(trigger: TriggerService, text: str, user: str = "console-user") -> None:
    command = parse_user_command(text, source="console", session_id=None)
    print(f"[parsed] command_type={command.command_type} shot_idx={command.shot_idx} text={command.text!r}")
    result = await trigger.handle_command(command, sender_id=user, channel="console")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


def main() -> None:
    parser = argparse.ArgumentParser(description="SceneForge console workflow tester")
    parser.add_argument("--once", help="Run a single command and exit")
    parser.add_argument("--workspace", default=".", help="Workspace root (default: current dir)")
    parser.add_argument("--session-backend", choices=("sqlite", "json"), default=None)
    args = parser.parse_args()

    trigger = build_trigger(args.workspace, args.session_backend)

    if args.once is not None:
        asyncio.run(run_once(trigger, args.once))
        return

    print("SceneForge 控制台测试。直接输入消息，回车发送；输入 exit / quit 退出。")
    print("先试无成本命令：查看状态 / 暂停 / 通过。生成类命令（做个短片 / 重生成第N镜）会调用模型。\n")
    while True:
        try:
            text = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text:
            continue
        if text.lower() in {"exit", "quit"}:
            break
        try:
            asyncio.run(run_once(trigger, text))
        except Exception as exc:  # keep the REPL alive on tool errors
            print(f"[error] {type(exc).__name__}: {exc}")
        print()


if __name__ == "__main__":
    main()
