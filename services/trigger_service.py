from __future__ import annotations

from typing import Any, Optional

from commands import UserCommand
from .authorization import Authorizer, InboundRateLimiter

# command types that actually start paid generation and must pass rate limiting
_GENERATION_COMMANDS = {"new_topic", "regenerate"}


class TriggerService:
    """Routes an inbound UserCommand to the right SceneForge tool, gated by
    authorization and rate limiting (design §4/§6/§19).

    This is the glue between channels/commands/feishu_inbound and the SceneForge
    adapter tools. It manages the review gate (approve/revise resolve ReviewTasks)
    and dispatches generation/lifecycle commands; multi-stage *advancement*
    itself stays with the explicit tool calls / future WorkflowEngine.
    """

    def __init__(self, session_index, adapters, authorizer: Optional[Authorizer] = None,
                 rate_limiter: Optional[InboundRateLimiter] = None, workflow_engine: Any = None,
                 production_service: Any = None):
        self.session_index = session_index
        self.adapters = adapters
        self.authorizer = authorizer
        self.rate_limiter = rate_limiter
        # When set, new_topic/approve/revise drive the staged WorkflowEngine
        # (topic -> script -> storyboard -> video -> final, gated by review).
        self.workflow_engine = workflow_engine
        # When set, those commands run in the BACKGROUND (fast ack now, push the
        # result later) — required so a Feishu webhook can ack within ~3s.
        self.production_service = production_service

    @staticmethod
    def _wrap(command_type: str, result: Any) -> dict:
        return {"ok": getattr(result, "ok", False), "command_type": command_type, "payload": getattr(result, "metadata", None)}

    def _active_session_id(self, command: UserCommand) -> Optional[str]:
        if command.session_id:
            record = self.session_index.get(command.session_id)
            if record:
                return record["session_id"]
        active = self.session_index.active()
        return active["session_id"] if active else None

    async def handle_command(self, command: UserCommand, sender_id: str = "", channel: Optional[str] = None) -> dict:
        channel = channel or command.source
        if self.authorizer and not self.authorizer.is_authorized(channel, sender_id):
            return {"ok": False, "reason": "unauthorized", "command_type": command.command_type}
        if command.command_type in _GENERATION_COMMANDS and self.rate_limiter and not self.rate_limiter.allow(sender_id or channel):
            return {"ok": False, "reason": "rate_limited", "command_type": command.command_type}

        ct = command.command_type

        # Background path: ack immediately, push the result when the job finishes.
        if self.production_service is not None and ct in ("new_topic", "approve", "revise", "regenerate"):
            return self._background(ct, command, sender_id)

        if ct == "new_topic":
            if self.workflow_engine is not None:
                result = await self.workflow_engine.start_topic(command.text or "")
                return {"command_type": ct, **result}
            return self._wrap(ct, await self.adapters.sceneforge_narrative_planning({"idea": command.text or ""}))
        if ct == "regenerate":
            # "第 N 镜" is 1-based human counting; internal shot_idx is 0-based.
            n = command.shot_idx
            shot_idx = (n - 1) if (n is not None and n >= 1) else (n or 0)
            args = {"shot_idx": shot_idx}
            if command.session_id:
                args["session_id"] = command.session_id
            return self._wrap(ct, await self.adapters.sceneforge_regenerate_shot(args))
        if ct == "status":
            return {"ok": True, "command_type": ct, "snapshot": self.session_index.snapshot()}
        if ct == "publish":
            session_id = self._active_session_id(command)
            if session_id is None:
                return {"ok": False, "reason": "no_session", "command_type": ct}
            args = {"session_id": session_id}
            if sender_id:
                args["target"] = sender_id
            return self._wrap(ct, await self.adapters.sceneforge_publish(args))
        if ct == "approve":
            return await self._approve(command)
        if ct == "revise":
            return await self._revise(command)
        if ct in ("pause", "resume", "cancel"):
            return self._lifecycle(ct, command)
        return {"ok": False, "reason": "unhandled", "command_type": ct}

    def _background(self, ct: str, command: UserCommand, sender_id: str) -> dict:
        target = sender_id or None
        if ct == "new_topic":
            return self._ack(ct, self.production_service.start_topic(command.text or "", target=target), "生成剧本")
        session_id = self._active_session_id(command)
        if session_id is None:
            return {"ok": False, "reason": "no_session", "command_type": ct}
        if ct == "approve":
            return self._ack(ct, self.production_service.approve(session_id, target=target), "推进下一阶段")
        if ct == "revise":
            return self._ack(ct, self.production_service.revise(session_id, command.text or "", target=target), "重做当前阶段")
        if ct == "regenerate":
            n = command.shot_idx
            shot_idx = (n - 1) if (n is not None and n >= 1) else (n or 0)
            return self._ack(ct, self.production_service.regenerate_shot(session_id, shot_idx, target=target), f"重生成第 {n} 镜" if n else "重生成镜头")
        return {"ok": False, "reason": "unhandled", "command_type": ct}

    @staticmethod
    def _ack(ct: str, rec: dict, what: str) -> dict:
        if not rec.get("accepted"):
            return {"ok": False, "command_type": ct, "reason": "busy", "note": "当前会话正在处理上一步，请稍候再操作。"}
        return {"ok": True, "command_type": ct, "accepted": True, "job_id": rec.get("job_id"), "note": f"已开始{what}，完成后通知你。"}

    def _latest_pending_review(self, session_id: str):
        pending = [t for t in self.session_index.list_review_tasks(session_id) if t.get("status") == "pending"]
        return pending[-1] if pending else None

    async def _approve(self, command: UserCommand) -> dict:
        session_id = self._active_session_id(command)
        if session_id is None:
            return {"ok": False, "reason": "no_session", "command_type": "approve"}
        if self.workflow_engine is not None:
            return {"command_type": "approve", **await self.workflow_engine.approve(session_id)}
        task = self._latest_pending_review(session_id)
        if task is None:
            return {"ok": True, "command_type": "approve", "note": "no pending review to approve"}
        self.session_index.resolve_review_task(session_id, task["review_id"], "approved")
        result = {"ok": True, "command_type": "approve", "resolved": task["review_id"], "stage": task["stage"]}
        if task["stage"] == "final":
            self.session_index.update_stage(session_id, "completed", "Production completed")
            result["stage"] = "completed"
            result["local_completed"] = True
        return result

    async def _revise(self, command: UserCommand) -> dict:
        session_id = self._active_session_id(command)
        if session_id is None:
            return {"ok": False, "reason": "no_session", "command_type": "revise"}
        if self.workflow_engine is not None:
            return {"command_type": "revise", **await self.workflow_engine.revise(session_id, command.text or "")}
        task = self._latest_pending_review(session_id)
        if task is None:
            self.session_index.append_log("revisions", {"session_id": session_id, "instruction": command.text, "note": "no pending review"})
            return {"ok": True, "command_type": "revise", "note": "no pending review; revision recorded only", "instruction": command.text}
        self.session_index.resolve_review_task(session_id, task["review_id"], "revised")
        self.session_index.append_log("revisions", {"session_id": session_id, "review_id": task["review_id"], "stage": task["stage"], "instruction": command.text})
        return {"ok": True, "command_type": "revise", "resolved": task["review_id"], "stage": task["stage"], "instruction": command.text}

    def _lifecycle(self, ct: str, command: UserCommand) -> dict:
        session_id = self._active_session_id(command)
        if session_id is None:
            return {"ok": False, "reason": "no_session", "command_type": ct}
        stage = {"pause": "paused", "resume": "resumed", "cancel": "cancelled"}[ct]
        self.session_index.update_stage(session_id, stage, f"Session {stage} by user command")
        return {"ok": True, "command_type": ct, "session_id": session_id, "stage": stage}
