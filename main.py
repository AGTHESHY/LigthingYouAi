"""Scene routing each turn; character and world growth only at compaction."""

import asyncio
import copy
import hashlib
import json
import weakref
from collections import ChainMap

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import (
    DecisionBooleanAnswer,
    DecisionBooleanQuestion,
    DecisionProvider,
    Provider,
    ProviderRequest,
)
from astrbot.api.star import Context, Star, StarTools

from .bridge import CompressionBridge, narrative, replace_summary
from .engine import (
    ArchiveStore,
    apply_candidates,
    parse_json,
    render,
    validate_candidates,
)
from .prompts import COMPACT, DECISIONS, REPAIR
from .sessions import ArchiveSessions, InitializationError
from .webui import ArchiveWebAPI


class LivingCharacters(Star):
    """Take over the effective persona while keeping host configuration intact."""

    def __init__(self, context: Context, config: AstrBotConfig):
        """Bind host models, persistent archives, and ephemeral scene caches.

        Args:
            context: AstrBot plugin services.
            config: Optional decision provider ID.
        """
        super().__init__(context)
        self.config = config
        self.store = ArchiveStore(
            StarTools.get_data_dir("astrbot_plugin_living_characters")
        )
        self.locks = weakref.WeakValueDictionary()
        self.scenes = {}
        self.bridges = set()
        self.sessions = ArchiveSessions(self)
        self.webui = ArchiveWebAPI(self)

    async def analyze(self, event, instruction, payload, model=None):
        """Use the selected chat model for JSON, never for ordinary growth ticks.

        Args:
            event: Current chat event.
            instruction: Narrow operation instructions.
            payload: Structured input without credentials.
            model: Session model override.

        Returns:
            Parsed JSON object, validated by the operation caller.

        Raises:
            Exception: Provider failure or invalid JSON.
        """
        configured = self.config.get("decision_provider_id", "").strip()
        provider = self.context.get_provider_by_id(configured) if configured else None
        if configured and not isinstance(provider, (Provider, DecisionProvider)):
            raise ValueError("Configured decision provider is unavailable")
        provider_id = (
            configured
            if isinstance(provider, Provider)
            else await self.context.get_current_chat_provider_id(
                event.unified_msg_origin
            )
        )
        kwargs = (
            {"model": model} if model and not isinstance(provider, Provider) else {}
        )
        prompt = json.dumps(payload, ensure_ascii=False)
        chat = self.context.get_provider_by_id(provider_id)
        from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial

        # Chat output limits can be tiny (e.g. 256 tokens). Scope a larger JSON
        # budget to this auxiliary call without mutating the live chat provider.
        if isinstance(chat, ProviderOpenAIOfficial):
            extra = dict(chat.provider_config.get("custom_extra_body") or {})
            key = (
                "max_completion_tokens"
                if "max_completion_tokens" in extra
                else "max_tokens"
            )
            minimum = (
                min(16384, max(8192, len(prompt) * 2))
                if instruction != DECISIONS
                else min(8192, max(1024, len(payload.get("questions", {})) * 32 + 256))
            )
            configured_limit = extra.get(key, chat.provider_config.get(key, 0))
            if (
                not isinstance(configured_limit, (int, float))
                or isinstance(configured_limit, bool)
                or configured_limit < minimum
            ):
                extra[key] = minimum
                scoped = copy.copy(chat)
                scoped.provider_config = ChainMap(
                    {"custom_extra_body": extra}, chat.provider_config
                )
                response = await asyncio.wait_for(
                    scoped.text_chat(
                        prompt=prompt, system_prompt=instruction, **kwargs
                    ),
                    timeout=90,
                )
            else:
                response = await asyncio.wait_for(
                    self.context.llm_generate(
                        chat_provider_id=provider_id,
                        system_prompt=instruction,
                        prompt=prompt,
                        **kwargs,
                    ),
                    timeout=90,
                )
        else:
            response = await asyncio.wait_for(
                self.context.llm_generate(
                    chat_provider_id=provider_id,
                    system_prompt=instruction,
                    prompt=prompt,
                    **kwargs,
                ),
                timeout=90,
            )
        if response.role != "assistant" or not response.completion_text:
            raise ValueError("No assistant JSON completion")
        return parse_json(response.completion_text)

    async def decide(
        self, event, questions, state, model=None, resolve_uncertain=False
    ):
        """Batch independent boolean questions through Jev or LLM JSON.

        Args:
            event: Current event.
            questions: Question IDs mapped to atomic statements.
            state: Only the data required to evaluate these statements.
            model: Session model override.
            resolve_uncertain: Escalate only uncertain compression checks to LLM JSON.

        Returns:
            Boolean or None per question; None means native uncertainty.

        Raises:
            Exception: Missing answers, wrong types, or a provider failure.
        """
        if not questions:
            return {}
        configured = self.config.get("decision_provider_id", "").strip()
        provider = self.context.get_provider_by_id(configured) if configured else None
        resolved = {}
        if isinstance(provider, DecisionProvider):
            answers = await asyncio.wait_for(
                self.context.decision.ask(
                    {
                        key: DecisionBooleanQuestion(instructions=text)
                        for key, text in questions.items()
                    },
                    state=state,
                    provider_id=configured,
                ),
                timeout=45,
            )
            if set(answers) != set(questions) or any(
                not isinstance(a, DecisionBooleanAnswer) or not 0 <= a.probability <= 1
                for a in answers.values()
            ):
                raise ValueError("Invalid native decisions")
            resolved = {
                key: True
                if a.probability > 0.7
                else False
                if a.probability < 0.3
                else None
                for key, a in answers.items()
            }
            if not resolve_uncertain or all(v is not None for v in resolved.values()):
                return resolved
            questions = {k: questions[k] for k, v in resolved.items() if v is None}
        data = await self.analyze(
            event, DECISIONS, {"questions": questions, "state": state}, model
        )
        if (
            set(data) != {"answers"}
            or not isinstance(data["answers"], dict)
            or set(data["answers"]) != set(questions)
            or any(type(value) is not bool for value in data["answers"].values())
        ):
            raise ValueError("Invalid LLM boolean schema")
        return {**resolved, **data["answers"]}

    async def select_scene(self, event, archive, history, prompt, scope, model):
        """Route small role rosters without sending full character cards.

        Args:
            event: Chat event.
            archive: Last compacted archive.
            history: Recent visible dialogue.
            prompt: Current user text.
            scope: Isolated story identity.
            model: Session model override.

        Returns:
            Active IDs, relevant referenced IDs, and retrieval text.
        """
        previous = self.scenes.get(
            scope, {"active": archive.get("active", []), "referenced": []}
        )
        recent = history[-4:] + [{"role": "user", "content": prompt}]
        query = "\n".join(m["content"] for m in recent)
        # If nothing refers to a role or a scene transition, continue the known
        # scene without an extra model request.
        latest = prompt + (history[-1]["content"] if history else "")
        has_cue = any(
            r["name"] in latest for r in archive["characters"].values()
        ) or any(
            w in latest
            for w in (
                "离开",
                "走进",
                "进场",
                "退场",
                "镜头",
                "场景",
                "另一边",
                "来到",
                "出现",
                "leave",
                "enter",
                "scene",
            )
        )
        if scope in self.scenes and not has_cue:
            return {**previous, "query": query}
        roster = {key: role["name"] for key, role in archive["characters"].items()}
        questions = {}
        for key, name in roster.items():
            questions[f"{key}:present"] = (
                f"在最新实际场景中，角色{name}是否在场？回忆、假设、提及不算；无离场依据时延续上一场景。"
            )
            questions[f"{key}:referenced"] = f"最新用户输入是否在讨论或询问角色{name}？"
        try:
            answers = await self.decide(
                event,
                questions,
                {
                    "roster": roster,
                    "previous_active": previous["active"],
                    "narrative": recent,
                },
                model,
            )
            active = [
                key
                for key in roster
                if answers[f"{key}:present"] is True
                or (answers[f"{key}:present"] is None and key in previous["active"])
            ]
            referenced = [
                key
                for key in roster
                if key not in active and answers[f"{key}:referenced"] is True
            ]
            selected = {"active": active, "referenced": referenced}
            self.scenes[scope] = selected
        except Exception as exc:
            logger.warning(
                "Living characters scene routing failed (%s); keeping previous scene",
                type(exc).__name__,
            )
            selected = previous
        return {**selected, "query": query}

    @filter.on_llm_request()
    async def prepare_scene(self, event: AstrMessageEvent, req: ProviderRequest):
        """Replace the resolved persona and register compaction work, not growth.

        Args:
            event: Current message event.
            req: Request after inherited persona resolution.
        """
        event.set_extra("living_characters_turn", None)
        if req.conversation is None:
            return
        selected = self.context.persona_manager.get_persona_v3_by_id(
            event.get_extra("_trace_persona_id")
        )
        original = selected.get("prompt") if selected else None
        if not original:
            return
        wrappers = (
            f"\n<persona_instructions>\n{original}\n</persona_instructions>\n",
            f"\n# Persona Instructions\n\n{original}\n",
        )
        matches = [w for w in wrappers if (req.system_prompt or "").count(w) == 1]
        if len(matches) != 1:
            return
        wrapped = matches[0]
        prefix, suffix = req.system_prompt.split(wrapped)
        scope = self.sessions.scope(
            event.unified_msg_origin,
            req.conversation.cid,
            event.get_extra("_trace_persona_id"),
            original,
        )
        try:
            scope, archive = await self.sessions.initialize(
                event,
                original,
                event.get_extra("_trace_persona_id"),
                req.conversation.cid,
                req.model,
            )
            lock = self.locks.setdefault(self.store.path(scope).stem, asyncio.Lock())
            async with lock:
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
                scene = await self.select_scene(
                    event, archive, history, req.prompt or "", scope, req.model
                )
                replacement = (
                    "\n<persona_instructions>\n"
                    + render(archive, **scene)
                    + "\n</persona_instructions>\n"
                )
                req.system_prompt = prefix + replacement + suffix
                event.set_extra(
                    "living_characters_turn",
                    {
                        "scope": scope,
                        "model": req.model,
                        "scene": scene,
                        "prefix": prefix,
                        "suffix": suffix,
                        "request": req,
                        "original_system": prefix + wrapped + suffix,
                    },
                )
        except Exception as exc:
            reason = (
                str(exc) if isinstance(exc, InitializationError) else type(exc).__name__
            )
            logger.warning(
                "Living characters initialization/request preparation failed: %s; preserving original persona",
                reason,
            )

    @filter.on_agent_begin()
    async def bind_compression(self, event, run_context):
        """Attach only to this runner after the native session has initialized it.

        Args:
            event: Current message event.
            run_context: Exact native run context used to identify its manager.
        """
        turn = event.get_extra("living_characters_turn")
        if not turn or turn.get("bridge"):
            return
        runtime = getattr(run_context.context, "_runtime", None)
        sessions = getattr(runtime, "_sessions", {})
        runner = next(
            (
                r
                for s in sessions.values()
                if (r := getattr(s, "_runner", None)) is not None
                and r.run_context is run_context
            ),
            None,
        )
        if runner is None and runtime is None:
            # AstrBot 4.29.4 registers the native runner before on_agent_begin.
            # Require exact request, context, and event identities so another
            # session or follow-up cannot attach to the wrong manager.
            try:
                from astrbot.core.pipeline.process_stage.follow_up import (
                    _ACTIVE_AGENT_RUNNERS,
                )
            except ImportError:
                pass
            else:
                candidate = _ACTIVE_AGENT_RUNNERS.get(event.unified_msg_origin)
                if (
                    candidate is not None
                    and candidate.run_context is run_context
                    and getattr(candidate, "req", None) is turn["request"]
                    and getattr(run_context.context, "event", None) is event
                ):
                    runner = candidate
        if runner is None or not hasattr(
            getattr(runner, "request_context_manager", None), "process"
        ):
            # A runtime without our verified integration must keep its original
            # persona rather than pretending that memory updates are installed.
            for m in run_context.messages:
                if m.role == "system" and m.content == turn["request"].system_prompt:
                    m.content = turn["original_system"]
            turn["request"].system_prompt = turn["original_system"]
            logger.warning(
                "Living characters compression integration unavailable; persona takeover disabled for this run"
            )
            return

        async def archive_removed(before, processed, discarded, retained):
            return await self.compact(
                event, turn, before, processed, discarded, retained
            )

        bridge = CompressionBridge(runner.request_context_manager, archive_removed)
        self.bridges.add(bridge)
        turn["bridge"] = bridge

    async def verify_candidates(self, event, archive, candidates, removed, model):
        """Evaluate independently scoped facts and preserve player authorship.

        Args:
            event: Current event.
            archive: Last confirmed fixed rules and current facts.
            candidates: Structurally validated atomic proposals.
            removed: Actual discarded history used as evidence.
            model: Session model override.

        Returns:
            Accepted candidate indices. Memory knowledge is set independently.
        """
        questions = {}
        for i, c in enumerate(candidates):
            if "id" in c:
                holder = archive["characters"][c["id"]]["name"]
                label = f"角色{holder}的{c.get('key', '经历')}：{c['value']}"
            elif c["kind"] == "faction_relation":
                label = f"势力{c['source']}与势力{c['target']}的关系：{c['value']}"
            else:
                label = f"{c.get('key', '世界事件')}：{c['value']}"
            questions[f"c{i}:supported"] = (
                f"引用原文「{c['evidence']}」是否明确支持这一个候选事实：{label}？"
            )
            questions[f"c{i}:actual"] = (
                f"原文「{c['evidence']}」描述的是实际发生的事件吗？假设、计划、否定和纯回忆不算。"
            )
            questions[f"c{i}:violates"] = (
                f"作者固定规则（含base原文中明确的永久约束）是否明确禁止「{label}」这项变化？只检查world_rules和该角色core；before_state与before_worldbook的旧状态不是禁令。"
                if "id" in c
                else f"world_rules或world_base中是否存在明确禁止「{label}」这项宏观变化的永久规则？before_worldbook中的旧局势允许随剧情改变。"
            )
            if c["kind"] in (
                "state",
                "growth",
                "knowledge",
                "world_fact",
                "faction",
                "faction_relation",
            ):
                questions[f"c{i}:current"] = (
                    f"在new_history叙述结束时，「{label}」成立吗？before_state是事件发生前的旧档案。"
                )
            if c["kind"] == "memory":
                questions[f"c{i}:known"] = (
                    f"待移除历史结束时，候选事实{label}所属角色本人是否确知该经历？其他角色或叙事者知情不算。"
                )
            if c["kind"] in (
                "faction",
                "faction_relation",
                "world_event",
                "world_fact",
            ):
                questions[f"c{i}:macro"] = (
                    f"这一个候选事实{label}是否属于宏观世界、组织或势力层面？人物私人关系不算。"
                )
        answers = await self.decide(
            event,
            questions,
            {
                "world_rules": archive["world"],
                "world_base": archive.get("world_base", ""),
                "characters": {
                    k: {
                        "name": r["name"],
                        "core": r["core"],
                        "base": r.get("base", ""),
                        "before_state": r["state"],
                        "before_knowledge": r["knowledge"],
                    }
                    for k, r in archive["characters"].items()
                },
                "before_worldbook": archive["worldbook"],
                "candidates": {f"c{i}": c for i, c in enumerate(candidates)},
                "new_history": removed,
            },
            model,
            resolve_uncertain=True,
        )
        accepted = set()
        for i, c in enumerate(candidates):
            if (
                "id" in c
                and archive["characters"][c["id"]].get("controller") == "user"
                and c["evidence"]
                not in "\n".join(m["content"] for m in removed if m["role"] == "user")
            ):
                continue
            checks = [
                v is (False if k.endswith(":violates") else True)
                for k, v in answers.items()
                if k.startswith(f"c{i}:") and not k.endswith(":known")
            ]
            if not all(checks):
                continue
            if c["kind"] == "memory":
                known = answers[f"c{i}:known"]
                if known is None:
                    continue
                c["known"] = known
            accepted.add(i)
        return accepted

    async def compact(self, event, turn, before, processed, discarded, retained):
        """Archive actually removed history and replace duplicated summary facts.

        Args:
            event: Current event.
            turn: Scoped persona and model information.
            before: Full pre-compression history for safe fallback.
            processed: Native compressed history.
            discarded: Removed message prefix.
            retained: Exact suffix retained by the native compressor.

        Returns:
            Compact public context, or untouched history on archival failure.
        """
        removed = narrative(discarded)
        lock = self.locks.setdefault(
            self.store.path(turn["scope"]).stem, asyncio.Lock()
        )
        async with lock:
            try:
                archive = self.store.load(turn["scope"])
                if archive is None:
                    raise ValueError("Missing archive")
                removed = archive.get("pending", []) + removed
                if not removed:
                    return processed
                source = "\n".join(m["content"] for m in removed)
                digest = hashlib.sha256(source.encode()).hexdigest()
                if archive.get("compaction_id") == digest:
                    updated = archive
                    summary = archive["shared_summary"]
                else:
                    configured = self.config.get("decision_provider_id", "").strip()
                    native = (
                        isinstance(
                            self.context.get_provider_by_id(configured),
                            DecisionProvider,
                        )
                        if configured
                        else False
                    )
                    if native:
                        screen = await self.decide(
                            event,
                            {
                                "has_changes": "这段待移除历史是否明确叙述了值得归档的角色事实或宏观世界事实？"
                            },
                            {
                                "discarded_history": removed,
                                "roster": {
                                    k: v["name"]
                                    for k, v in archive["characters"].items()
                                },
                            },
                            turn["model"],
                        )
                        if screen["has_changes"] is False:
                            return processed
                    previous_summary = [
                        m.model_dump().get("content", "")
                        for m in discarded
                        if m.role == "user"
                        and isinstance(m.model_dump().get("content"), str)
                        and m.model_dump()["content"].startswith(
                            "Our previous history conversation summary:"
                        )
                    ]
                    payload = {
                        "archive": {
                            k: v
                            for k, v in archive.items()
                            if k not in ("pending", "compaction_id")
                        },
                        "discarded_history": removed,
                        "retained_history": narrative(retained),
                        "previous_summary": previous_summary,
                    }
                    data = await self.analyze(event, COMPACT, payload, turn["model"])
                    try:
                        candidates, summary = validate_candidates(archive, data, source)
                    except ValueError as exc:
                        if str(exc) not in (
                            "Invalid candidate fields",
                            "Invalid compaction schema",
                        ):
                            raise
                        repaired = await self.analyze(
                            event, REPAIR, {"invalid_output": data}, turn["model"]
                        )
                        # Repairs may delete surplus fields, never rewrite a fact.
                        if (
                            not isinstance(repaired.get("candidates"), list)
                            or len(repaired["candidates"])
                            != len(data.get("candidates", []))
                            or repaired.get("shared_summary")
                            != data.get("shared_summary")
                        ):
                            raise ValueError("Repair changed archive content") from exc
                        for original, fixed in zip(
                            data["candidates"], repaired["candidates"], strict=True
                        ):
                            if not isinstance(fixed, dict) or any(
                                key not in original or original[key] != value
                                for key, value in fixed.items()
                            ):
                                raise ValueError(
                                    "Repair changed a candidate fact"
                                ) from exc
                        candidates, summary = validate_candidates(
                            archive, repaired, source
                        )
                        data = repaired
                    accepted = await self.verify_candidates(
                        event, archive, candidates, removed, turn["model"]
                    )
                    # A rejected proposal may have been omitted from the new
                    # public summary. Keep the core summary unless all passed.
                    if len(accepted) != len(candidates):
                        summary = "\n".join(
                            m.content
                            for m in processed
                            if m.role == "user"
                            and isinstance(m.content, str)
                            and m.content.startswith(
                                "Our previous history conversation summary:"
                            )
                        )
                        if not summary:
                            unresolved = list(
                                dict.fromkeys(
                                    c["evidence"]
                                    for i, c in enumerate(candidates)
                                    if i not in accepted
                                )
                            )
                            summary = (
                                data["shared_summary"]
                                + "\n未归档的原始历史片段（不代表已确认，不得推翻作者固定设定）：\n"
                                + "\n".join(unresolved)
                            )
                            if len(summary) > 16000:
                                return before
                    updated = apply_candidates(archive, candidates, accepted)
                    updated["revision"] = archive["revision"] + 1
                    updated["compaction_id"] = digest
                    updated["shared_summary"] = summary
                    updated.pop("pending", None)
                replacement = replace_summary(processed, retained, summary)
                scene = turn["scene"]
                system = (
                    turn["prefix"]
                    + "\n<persona_instructions>\n"
                    + render(updated, **scene)
                    + "\n</persona_instructions>\n"
                    + turn["suffix"]
                )
                previous_system = turn["request"].system_prompt
                for m in replacement:
                    if m.role == "system" and m.content == previous_system:
                        m.content = system
                self.store.save(turn["scope"], updated)
                turn["request"].system_prompt = system
                return replacement
            except Exception as exc:
                logger.warning(
                    "Living characters compaction archive failed (%s); retaining original history",
                    type(exc).__name__,
                )
                return before

    @filter.on_agent_done()
    async def finish_run(self, event, run_context, response):
        """Remove the per-run adapter; never update memory at turn completion.

        Args:
            event: Current event.
            run_context: Native result, unused for archival.
            response: Final response, unused for archival.
        """
        turn = event.get_extra("living_characters_turn")
        event.set_extra("living_characters_turn", None)
        if turn and (bridge := turn.get("bridge")):
            bridge.close()
            self.bridges.discard(bridge)

    async def terminate(self):
        """Restore active instance adapters when the plugin is unloaded."""
        for bridge in self.bridges:
            bridge.close()
        self.bridges.clear()
        self.scenes.clear()
        self.webui.close()
        self.sessions.references.clear()
        self.sessions.errors.clear()
