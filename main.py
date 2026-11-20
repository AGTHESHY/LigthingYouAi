"""AstrBot hooks for scene-aware, persistent character growth."""

import asyncio
import copy
import hashlib
import json
import weakref

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import (
    DecisionBooleanAnswer,
    DecisionBooleanQuestion,
    DecisionProvider,
    LLMResponse,
    Provider,
    ProviderRequest,
)
from astrbot.api.star import Context, Star, StarTools

from .engine import ArchiveStore, apply_update, new_archive, parse_json, render
from .prompts import IMPORT, UPDATE


class LivingCharacters(Star):
    """Replace the selected persona with a scene-specific evolving archive."""

    def __init__(self, context: Context, config: AstrBotConfig):
        """Bind host services and keep story files outside the plugin code.

        Args:
            context: AstrBot plugin services.
            config: Optional decision provider selection.
        """
        super().__init__(context)
        self.config = config
        self.store = ArchiveStore(
            StarTools.get_data_dir("astrbot_plugin_living_characters")
        )
        self.locks = weakref.WeakValueDictionary()

    async def analyze(self, event, instruction: str, payload: dict, model=None) -> dict:
        """Ask a chat-capable model for bounded, validated JSON.

        Args:
            event: Current chat event used to resolve the session model.
            instruction: Archive operation instructions.
            payload: Narrative and archive, excluding API credentials.
            model: Optional per-request model override for session fallback.

        Returns:
            Parsed model JSON object.

        Raises:
            Exception: Provider, timeout, or JSON validation fails.
        """
        configured = self.config.get("decision_provider_id", "").strip()
        provider = self.context.get_provider_by_id(configured) if configured else None
        if configured and not isinstance(provider, (Provider, DecisionProvider)):
            raise ValueError("Configured decision provider is unavailable")
        provider_id = (
            configured
            if isinstance(provider, Provider)
            else (
                await self.context.get_current_chat_provider_id(
                    event.unified_msg_origin
                )
            )
        )
        kwargs = (
            {"model": model} if model and not isinstance(provider, Provider) else {}
        )
        response = await asyncio.wait_for(
            self.context.llm_generate(
                chat_provider_id=provider_id,
                system_prompt=instruction,
                prompt=json.dumps(payload, ensure_ascii=False),
                **kwargs,
            ),
            timeout=90,
        )
        if response.role != "assistant" or not response.completion_text:
            raise ValueError("Archive model did not return an assistant completion")
        return parse_json(response.completion_text)

    async def update(
        self,
        event,
        archive: dict,
        narrative: list,
        model=None,
        evidence: list | None = None,
    ) -> dict:
        """Extract evidenced changes and optionally use native scene decisions.

        Args:
            event: Session event.
            archive: Existing archive.
            narrative: Confirmed dialogue plus the latest user input.
            model: Session request model override.
            evidence: New narrative allowed to support changes; history is context only.

        Returns:
            Independently validated scene archive.

        Raises:
            Exception: Analysis or decision output is invalid.
        """
        # Evidence is checked against text values, never against escaped JSON.
        source = "\n".join(
            item["content"] for item in (narrative if evidence is None else evidence)
        )
        data = await self.analyze(
            event,
            UPDATE,
            {
                "archive": archive,
                "narrative": narrative,
                "new_evidence": narrative if evidence is None else evidence,
            },
            model,
        )
        result = apply_update(archive, data, source)
        configured = self.config.get("decision_provider_id", "").strip()
        provider = self.context.get_provider_by_id(configured) if configured else None
        if isinstance(provider, DecisionProvider):
            questions = {
                key: DecisionBooleanQuestion(
                    instructions=f"角色 {role['name']} 在最新场景中实际在场吗？仅被提及、回忆或假设不算在场；没有离场依据时延续上一场景。"
                )
                for key, role in result["characters"].items()
            }
            answers = await asyncio.wait_for(
                self.context.decision.ask(
                    questions,
                    state={
                        "narrative": narrative,
                        "previous_active": archive["active"],
                    },
                    provider_id=configured,
                ),
                timeout=45,
            )
            if set(answers) != set(questions) or any(
                not isinstance(answer, DecisionBooleanAnswer)
                or not 0 <= answer.probability <= 1
                for answer in answers.values()
            ):
                raise ValueError("Invalid scene decision answers")
            result["active"] = [
                key for key, answer in answers.items() if answer.probability >= 0.5
            ]
        return result

    @filter.on_llm_request()
    async def prepare_scene(self, event: AstrMessageEvent, req: ProviderRequest):
        """Update before core compression and inject only the current scene.

        Args:
            event: Current message event.
            req: Request after AstrBot has injected the selected persona.
        """
        event.set_extra("living_characters_turn", None)
        # Restrict replacement to the exact selected persona, preserving all
        # host safety, skill, workspace, and tool instructions around it.
        if req.conversation is None:
            return
        selected = self.context.persona_manager.get_persona_v3_by_id(
            event.get_extra("_trace_persona_id")
        )
        original = selected.get("prompt") if selected else None
        if not original:
            return
        wrapped = f"\n<persona_instructions>\n{original}\n</persona_instructions>\n"
        if (req.system_prompt or "").count(wrapped) != 1:
            return
        start = req.system_prompt.index(wrapped) + len("\n<persona_instructions>\n")
        end = start + len(original)
        source_hash = hashlib.sha256(original.encode()).hexdigest()
        scope = json.dumps(
            [
                event.unified_msg_origin,
                req.conversation.cid,
                event.get_extra("_trace_persona_id"),
                source_hash,
            ]
        )
        lock = self.locks.setdefault(scope, asyncio.Lock())
        async with lock:
            try:
                archive = self.store.load(scope)
                history = []
                for item in req.contexts:
                    if item.get("role") not in ("user", "assistant"):
                        continue
                    content = item.get("content", "")
                    if isinstance(content, list):
                        content = "\n".join(
                            p.get("text", "")
                            for p in content
                            if isinstance(p, dict) and p.get("type") == "text"
                        )
                    if isinstance(content, str) and content:
                        history.append({"role": item["role"], "content": content})
                if archive is None:
                    archive = new_archive(
                        await self.analyze(
                            event, IMPORT, {"persona": original}, req.model
                        )
                    )
                    self.store.save(scope, archive)
                if not archive.get("scene_ready") and history:
                    archive = await self.update(event, archive, history, req.model)
                    archive["scene_ready"] = True
                    self.store.save(scope, archive)
                if archive.get("pending"):
                    pending = archive["pending"]
                    try:
                        recovered = await self.update(
                            event, archive, pending, req.model
                        )
                        recovered.pop("pending", None)
                        recovered["scene_ready"] = True
                        recovered["revision"] += 1
                        self.store.save(scope, recovered)
                        archive = recovered
                    except Exception as exc:
                        logger.warning(
                            "Living characters pending recovery failed (%s); retaining queued narrative",
                            type(exc).__name__,
                        )
                current = [{"role": "user", "content": req.prompt or ""}]
                narrative = history[-8:] + archive.get("pending", []) + current
                try:
                    scene = await self.update(
                        event,
                        archive,
                        narrative,
                        req.model,
                        evidence=archive.get("pending", []) + current,
                    )
                except Exception as exc:
                    # Never log provider exception text, which can contain
                    # credentials or full narrative payloads.
                    logger.warning(
                        "Living characters scene analysis failed (%s); retaining archive",
                        type(exc).__name__,
                    )
                    if not archive.get("scene_ready"):
                        return
                    scene = copy.deepcopy(archive)
                req.system_prompt = (
                    req.system_prompt[:start] + render(scene) + req.system_prompt[end:]
                )
                event.set_extra(
                    "living_characters_turn",
                    {
                        "scope": scope,
                        "model": req.model,
                        "history": history[-8:],
                        "evidence": current,
                    },
                )
            except Exception as exc:
                logger.warning(
                    "Living characters import/load failed (%s); preserving original persona",
                    type(exc).__name__,
                )

    @filter.on_agent_done()
    async def save_growth(
        self, event: AstrMessageEvent, run_context, response: LLMResponse | None
    ):
        """Save the final successful turn once, excluding tool-loop intermediates.

        Args:
            event: Current message event.
            run_context: Agent result including completion or cancellation status.
            response: Final model reply, possibly absent on failure.
        """
        turn = event.get_extra("living_characters_turn")
        event.set_extra("living_characters_turn", None)
        if (
            not turn
            or run_context.run_end_reason != "completed"
            or not response
            or response.role != "assistant"
            or not response.completion_text
        ):
            return
        scope = turn["scope"]
        lock = self.locks.setdefault(scope, asyncio.Lock())
        async with lock:
            try:
                archive = self.store.load(scope)
                if archive is None:
                    return
                # Reload under the lock so overlapping requests cannot overwrite
                # archives with a stale snapshot from their request hook.
                completed = turn["evidence"] + [
                    {"role": "assistant", "content": response.completion_text}
                ]
                pending = archive.get("pending", []) + completed
                archive["pending"] = pending
                # Preserve unprocessed events before a possibly failing network
                # call, so the next request can recover after core compression.
                self.store.save(scope, archive)
                narrative = turn["history"] + pending
                updated = await self.update(
                    event, archive, narrative, turn["model"], evidence=pending
                )
                updated.pop("pending", None)
                updated["scene_ready"] = True
                updated["revision"] = archive["revision"] + 1
                self.store.save(scope, updated)
            except Exception as exc:
                logger.warning(
                    "Living characters archive update failed (%s); retaining previous data",
                    type(exc).__name__,
                )
