"""Real core compaction tests and narrow Jev/LLM routing regressions."""

import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from astrbot.api.provider import (
    DecisionBooleanAnswer,
    DecisionProvider,
    LLMResponse,
    Provider,
)
from astrbot.core.agent.context.config import ContextConfig
from astrbot.core.agent.context.manager import ContextManager
from astrbot.core.agent.message import Message
from astrbot_plugin_living_characters.bridge import CompressionBridge
from astrbot_plugin_living_characters.prompts import COMPACT, DECISIONS, IMPORT
from conftest import Event, request


async def bound(plugin, manager, prompt="A独自在医馆"):
    event, req = Event(), request(prompt)
    await plugin.prepare_scene(event, req)
    run = SimpleNamespace(
        messages=[Message(role="system", content=req.system_prompt)],
        context=SimpleNamespace(),
    )
    runner = SimpleNamespace(run_context=run, request_context_manager=manager)
    run.context._runtime = SimpleNamespace(
        _sessions={"one": SimpleNamespace(_runner=runner)}
    )
    await plugin.bind_compression(event, run)
    return event, req, run


def old_and_recent(req, padding=""):
    return [
        Message(role="system", content=req.system_prompt),
        Message(role="user", content="A接受治疗。" + padding),
        Message(
            role="assistant",
            content="A的腿伤已经痊愈。A知道自己康复了。双方约定明日出发。",
        ),
        Message(role="user", content="A在医馆喝茶"),
        Message(role="assistant", content="A端起茶盏。"),
        Message(role="user", content="继续"),
    ]


def test_ordinary_turns_never_update_or_rewrite_archive(plugin):
    async def run():
        event, req = Event(), request()
        await plugin.prepare_scene(event, req)
        turn = event.get_extra("living_characters_turn")
        path = plugin.store.path(turn["scope"])
        original = path.read_bytes()
        assert req.system_prompt.startswith(
            "HOST SAFETY"
        ) and req.system_prompt.endswith("HOST TOOLS")
        assert "原始完整人设" not in req.system_prompt
        assert "温和医师" not in req.system_prompt
        await plugin.finish_run(
            event,
            SimpleNamespace(run_end_reason="completed"),
            LLMResponse(role="assistant", completion_text="A的腿伤已经痊愈"),
        )
        assert path.read_bytes() == original
        calls = plugin.context.llm_generate.await_count
        for _ in range(3):
            e = Event()
            await plugin.prepare_scene(e, request("继续"))
            await plugin.finish_run(e, None, None)
        assert plugin.context.llm_generate.await_count == calls
        assert path.read_bytes() == original
        assert all(
            c.kwargs["system_prompt"] != COMPACT
            for c in plugin.context.llm_generate.call_args_list
        )

    asyncio.run(run())


def test_native_scene_questions_are_atomic_and_have_no_full_dossiers(plugin):
    async def run():
        plugin.config["decision_provider_id"] = "jev"
        plugin.context.get_provider_by_id = lambda _: MagicMock(spec=DecisionProvider)
        plugin.context.decision = SimpleNamespace(
            ask=AsyncMock(
                return_value={
                    "r1:present": DecisionBooleanAnswer(0.95),
                    "r1:referenced": DecisionBooleanAnswer(0.95),
                    "r2:present": DecisionBooleanAnswer(0.05),
                    "r2:referenced": DecisionBooleanAnswer(0.05),
                }
            )
        )
        req = request()
        await plugin.prepare_scene(Event(), req)
        assert plugin.context.llm_generate.await_count == 1
        assert plugin.context.llm_generate.call_args.kwargs["system_prompt"] == IMPORT
        call = plugin.context.decision.ask.call_args
        assert set(call.args[0]) == {
            "r1:present",
            "r1:referenced",
            "r2:present",
            "r2:referenced",
        }
        assert "谨慎剑客" not in json.dumps(call.kwargs["state"], ensure_ascii=False)
        assert "温和医师" not in req.system_prompt

    asyncio.run(run())


@pytest.mark.parametrize("strategy", ["turns", "tokens"])
def test_actual_core_compaction_is_the_only_growth_trigger(plugin, strategy):
    async def run():
        summary = SimpleNamespace(
            provider_config={},
            text_chat=AsyncMock(
                return_value=LLMResponse(
                    role="assistant",
                    completion_text="旧摘要：A的腿伤已经痊愈。明日出发。",
                )
            ),
        )
        manager = ContextManager(
            ContextConfig(enforce_max_turns=1, truncate_turns=1)
            if strategy == "turns"
            else ContextConfig(
                max_context_tokens=3000,
                llm_compress_provider=summary,
                llm_compress_keep_recent_ratio=0.15,
            )
        )
        original_class_method = ContextManager.process
        event, req, context = await bound(plugin, manager)
        turn = event.get_extra("living_characters_turn")
        before = plugin.store.load(turn["scope"])
        assert before["revision"] == 0
        messages = old_and_recent(req, "背景。" * 12000 if strategy == "tokens" else "")
        result = await manager.process(messages)
        saved = plugin.store.load(turn["scope"])
        assert saved["revision"] == 1
        assert saved["characters"]["r1"]["state"]["腿伤"] == "已痊愈"
        assert saved["characters"]["r1"]["state"]["位置"] == "医馆"
        assert len(saved["characters"]["r1"]["memories"]) == 2
        assert any("明日出发" in str(m.content) for m in result)
        assert "腿伤已经痊愈" not in str(
            [m.content for m in result if m.role != "system"]
        )
        assert "已痊愈" in result[0].content and "曾在山路受伤" in result[0].content
        assert result[-1].content == "继续"
        if strategy == "tokens":
            summary.text_chat.assert_awaited_once()
        assert ContextManager.process is original_class_method
        await plugin.finish_run(event, context, None)
        assert manager.process.__func__ is original_class_method

    asyncio.run(run())


def test_under_threshold_no_extraction_and_no_archive_write(plugin):
    async def run():
        manager = ContextManager(ContextConfig(max_context_tokens=100000))
        event, req, _ = await bound(plugin, manager)
        turn = event.get_extra("living_characters_turn")
        path = plugin.store.path(turn["scope"])
        snapshot = path.read_bytes()
        plugin.context.llm_generate.reset_mock()
        result = await manager.process(old_and_recent(req))
        assert result[-1].content == "继续"
        plugin.context.llm_generate.assert_not_called()
        assert path.read_bytes() == snapshot

    asyncio.run(run())


def test_compaction_failure_keeps_full_history_and_old_archive(plugin):
    async def run():
        manager = ContextManager(ContextConfig(enforce_max_turns=1))
        event, req, _ = await bound(plugin, manager)
        turn = event.get_extra("living_characters_turn")
        path = plugin.store.path(turn["scope"])
        snapshot = path.read_bytes()
        plugin.context.llm_generate.side_effect = TimeoutError(
            "credentials must not be logged"
        )
        messages = old_and_recent(req)
        result = await manager.process(messages)
        assert [m.model_dump() for m in result] == [m.model_dump() for m in messages]
        assert path.read_bytes() == snapshot

    asyncio.run(run())


def test_retained_history_is_not_archived_ahead_of_time(plugin):
    async def run():
        manager = ContextManager(ContextConfig(enforce_max_turns=2))
        event, req, _ = await bound(plugin, manager)
        # The quoted cure now occurs only in a retained round, not removed history.
        messages = [
            Message(role="system", content=req.system_prompt),
            Message(role="user", content="早前的闲谈"),
            Message(role="assistant", content="天气不错"),
            Message(role="user", content="另一段旧闲谈"),
            Message(role="assistant", content="仍然没有新事件"),
            Message(role="user", content="A的腿伤已经痊愈"),
            Message(role="assistant", content="A走动"),
            Message(role="user", content="继续"),
        ]
        result = await manager.process(messages)
        assert [m.model_dump() for m in result] == [m.model_dump() for m in messages]
        assert (
            plugin.store.load(event.get_extra("living_characters_turn")["scope"])[
                "revision"
            ]
            == 0
        )

    asyncio.run(run())


@pytest.mark.parametrize(
    "bad",
    [
        {"answers": {"one": "true"}},
        {"answers": {"extra": True}},
        {"answers": {"one": 1}},
        {"answers": {"one": True}, "explanation": "bad"},
    ],
)
def test_llm_json_fallback_is_strict(plugin, bad):
    async def run():
        plugin.context.llm_generate.side_effect = None
        plugin.context.llm_generate.return_value = LLMResponse(
            role="assistant", completion_text=json.dumps(bad)
        )
        with pytest.raises(ValueError):
            await plugin.decide(Event(), {"one": "唯一命题？"}, {})

    asyncio.run(run())


def test_missing_jev_uses_current_session_json_model(plugin):
    async def run():
        result = await plugin.decide(Event(), {"fact": "事实成立？"}, {})
        assert result == {"fact": True}
        assert (
            plugin.context.llm_generate.call_args.kwargs["chat_provider_id"]
            == "session-provider"
        )
        assert (
            plugin.context.llm_generate.call_args.kwargs["system_prompt"] == DECISIONS
        )

    asyncio.run(run())


def test_dedicated_chat_provider_is_supported(plugin):
    async def run():
        plugin.config["decision_provider_id"] = "dedicated-chat"
        plugin.context.get_provider_by_id = lambda _: MagicMock(spec=Provider)
        await plugin.decide(Event(), {"fact": "事实成立？"}, {}, "session-model")
        assert (
            plugin.context.llm_generate.call_args.kwargs["chat_provider_id"]
            == "dedicated-chat"
        )
        assert "model" not in plugin.context.llm_generate.call_args.kwargs

    asyncio.run(run())


def test_atomic_verification_checks_each_mutation_separately(plugin):
    async def run():
        manager = ContextManager(ContextConfig(enforce_max_turns=1))
        event, req, _ = await bound(plugin, manager)
        await manager.process(old_and_recent(req))
        decisions = [
            json.loads(c.kwargs["prompt"])["questions"]
            for c in plugin.context.llm_generate.call_args_list
            if c.kwargs["system_prompt"] == DECISIONS
        ]
        verification = decisions[-1]
        assert set(verification) == {
            "c0:supported",
            "c0:actual",
            "c0:violates",
            "c0:current",
            "c1:supported",
            "c1:actual",
            "c1:violates",
            "c1:known",
        }

    asyncio.run(run())


def test_private_knowledge_does_not_spread_implicitly(plugin):
    async def run():
        manager = ContextManager(ContextConfig(enforce_max_turns=1))
        event, req, _ = await bound(plugin, manager)
        turn = event.get_extra("living_characters_turn")
        await manager.process(old_and_recent(req))
        saved = plugin.store.load(turn["scope"])
        assert saved["characters"]["r1"]["knowledge"]["救命者"] == "不知道是谁"
        assert saved["characters"]["r2"]["knowledge"]["救命者"] == "知道自己曾暗中救A"

    asyncio.run(run())


def test_rejected_hard_constraint_cannot_rewrite_core(plugin):
    async def run():
        manager = ContextManager(ContextConfig(enforce_max_turns=1))
        event, req, _ = await bound(plugin, manager)
        working = plugin.context.llm_generate.side_effect

        async def reject(**kwargs):
            response = await working(**kwargs)
            if kwargs["system_prompt"] == DECISIONS:
                data = json.loads(response.completion_text)
                if "c0:violates" in data["answers"]:
                    data["answers"]["c0:violates"] = True
                    response.completion_text = json.dumps(data)
            return response

        plugin.context.llm_generate.side_effect = reject
        await manager.process(old_and_recent(req))
        saved = plugin.store.load(event.get_extra("living_characters_turn")["scope"])
        assert saved["characters"]["r1"]["state"]["腿伤"] == "未愈"
        assert "无法恢复" in saved["characters"]["r1"]["core"]

    asyncio.run(run())


def test_scene_switch_loads_role_but_does_not_grow_archive(plugin):
    async def run():
        event = Event()
        await plugin.prepare_scene(event, request())
        path = plugin.store.path(event.get_extra("living_characters_turn")["scope"])
        snapshot = path.read_bytes()
        working = plugin.context.llm_generate.side_effect

        async def switch(**kwargs):
            if kwargs["system_prompt"] == DECISIONS:
                return LLMResponse(
                    role="assistant",
                    completion_text=json.dumps(
                        {
                            "answers": {
                                "r1:present": False,
                                "r1:referenced": False,
                                "r2:present": True,
                                "r2:referenced": True,
                            }
                        }
                    ),
                )
            return await working(**kwargs)

        plugin.context.llm_generate.side_effect = switch
        req = request("镜头转到B")
        await plugin.prepare_scene(Event(), req)
        assert "温和医师" in req.system_prompt
        assert "谨慎剑客" not in req.system_prompt
        assert path.read_bytes() == snapshot

    asyncio.run(run())


def test_unknown_runtime_restores_original_persona_and_preserves_tools(plugin):
    async def run():
        event, req = Event(), request()
        original = req.system_prompt
        await plugin.prepare_scene(event, req)
        context = SimpleNamespace(
            context=SimpleNamespace(),
            messages=[Message(role="system", content=req.system_prompt)],
        )
        await plugin.bind_compression(event, context)
        assert req.system_prompt == original
        assert context.messages[0].content == original

    asyncio.run(run())


def test_adapter_is_per_instance_and_restored_on_unload(plugin):
    async def run():
        first, other = ContextManager(ContextConfig()), ContextManager(ContextConfig())
        await bound(plugin, first)
        assert first.process.__self__.__class__ is CompressionBridge
        assert other.process.__func__ is ContextManager.process
        await plugin.terminate()
        assert first.process.__func__ is ContextManager.process

    asyncio.run(run())


def test_archive_scopes_separate_conversations_and_inherited_personas(plugin):
    async def run():
        first, second = Event(), Event()
        await plugin.prepare_scene(first, request(cid="one"))
        await plugin.prepare_scene(second, request(cid="two"))
        assert (
            first.get_extra("living_characters_turn")["scope"]
            != second.get_extra("living_characters_turn")["scope"]
        )

    asyncio.run(run())


def test_native_uncertainty_keeps_previous_scene(plugin):
    async def run():
        event = Event()
        await plugin.prepare_scene(event, request())
        plugin.config["decision_provider_id"] = "jev"
        plugin.context.get_provider_by_id = lambda _: MagicMock(spec=DecisionProvider)
        plugin.context.decision = SimpleNamespace(
            ask=AsyncMock(
                return_value={
                    "r1:present": DecisionBooleanAnswer(0.5),
                    "r1:referenced": DecisionBooleanAnswer(0.2),
                    "r2:present": DecisionBooleanAnswer(0.5),
                    "r2:referenced": DecisionBooleanAnswer(0.2),
                }
            )
        )
        req = request("场景继续")
        await plugin.prepare_scene(Event(), req)
        assert "谨慎剑客" in req.system_prompt and "温和医师" not in req.system_prompt

    asyncio.run(run())


def test_worldbook_growth_is_committed_only_with_real_compaction(plugin):
    async def run():
        plugin.test_proposals = {
            "candidates": [
                {
                    "kind": "faction_relation",
                    "source": "北盟",
                    "target": "南盟",
                    "value": "结盟",
                    "evidence": "北盟与南盟结盟",
                },
                {
                    "id": "r1",
                    "kind": "state",
                    "key": "对B关系",
                    "value": "仍不信任",
                    "evidence": "A仍不信任B",
                },
            ],
            "shared_summary": "两盟已结盟，私人关系尚待缓和。",
        }
        manager = ContextManager(ContextConfig(enforce_max_turns=1))
        event, req, _ = await bound(plugin, manager)
        scope = event.get_extra("living_characters_turn")["scope"]
        assert plugin.store.load(scope)["worldbook"]["relations"][0]["value"] == "敌对"
        messages = old_and_recent(req)
        messages[1].content = "北盟与南盟结盟，但A仍不信任B。"
        messages[2].content = "两盟完成签约。"
        await manager.process(messages)
        saved = plugin.store.load(scope)
        assert saved["worldbook"]["relations"][0]["value"] == "结盟"
        assert saved["characters"]["r1"]["state"]["对B关系"] == "仍不信任"

    asyncio.run(run())


def test_assistant_cannot_author_player_state_changes(plugin):
    async def run():
        manager = ContextManager(ContextConfig(enforce_max_turns=1))
        event, req, _ = await bound(plugin, manager)
        scope = event.get_extra("living_characters_turn")["scope"]
        archive = plugin.store.load(scope)
        archive["characters"]["r1"]["controller"] = "user"
        plugin.store.save(scope, archive)
        # Even if a model mistakenly approves it, the evidence is exclusively
        # assistant-authored, so player control is protected deterministically.
        await manager.process(old_and_recent(req))
        saved = plugin.store.load(scope)
        assert (
            saved["characters"]["r1"]["state"] == archive["characters"]["r1"]["state"]
        )
        assert (
            saved["characters"]["r1"]["memories"]
            == archive["characters"]["r1"]["memories"]
        )
        assert "未归档" in saved["shared_summary"]

    asyncio.run(run())


def test_native_no_change_screen_skips_expensive_archive_llm(plugin):
    async def run():
        manager = ContextManager(ContextConfig(enforce_max_turns=1))
        event, req, _ = await bound(plugin, manager)
        scope = event.get_extra("living_characters_turn")["scope"]
        snapshot = plugin.store.path(scope).read_bytes()
        plugin.config["decision_provider_id"] = "jev"
        plugin.context.get_provider_by_id = lambda _: MagicMock(spec=DecisionProvider)
        plugin.context.decision = SimpleNamespace(
            ask=AsyncMock(return_value={"has_changes": DecisionBooleanAnswer(0.01)})
        )
        plugin.context.llm_generate.reset_mock()
        await manager.process(old_and_recent(req))
        plugin.context.llm_generate.assert_not_called()
        assert plugin.store.path(scope).read_bytes() == snapshot

    asyncio.run(run())


def test_only_uncertain_native_checks_escalate_to_llm_json(plugin):
    async def run():
        plugin.config["decision_provider_id"] = "jev"
        plugin.context.get_provider_by_id = lambda _: MagicMock(spec=DecisionProvider)
        plugin.context.decision = SimpleNamespace(
            ask=AsyncMock(
                return_value={
                    "sure": DecisionBooleanAnswer(0.95),
                    "uncertain": DecisionBooleanAnswer(0.5),
                    "negative": DecisionBooleanAnswer(0.05),
                }
            )
        )
        result = await plugin.decide(
            Event(),
            {
                "sure": "明确事实？",
                "uncertain": "不确定事实？",
                "negative": "不成立的事实？",
            },
            {},
            resolve_uncertain=True,
        )
        assert result == {"sure": True, "uncertain": True, "negative": False}
        payload = json.loads(plugin.context.llm_generate.call_args.kwargs["prompt"])
        assert set(payload["questions"]) == {"uncertain"}

    asyncio.run(run())


@pytest.mark.parametrize("alter_value", [False, True])
def test_structural_json_repair_cannot_change_fact_content(plugin, alter_value):
    from astrbot_plugin_living_characters.prompts import REPAIR

    async def run():
        plugin.test_proposals = {
            "candidates": [
                {
                    "id": "world",
                    "kind": "world_fact",
                    "key": "时代",
                    "value": "旧历二年",
                    "evidence": "进入旧历二年",
                }
            ],
            "shared_summary": "新的年份，剧情继续。",
        }
        manager = ContextManager(ContextConfig(enforce_max_turns=1))
        event, req, _ = await bound(plugin, manager)
        scope = event.get_extra("living_characters_turn")["scope"]
        working = plugin.context.llm_generate.side_effect

        async def repair(**kwargs):
            if kwargs["system_prompt"] == REPAIR:
                fixed = copy.deepcopy(plugin.test_proposals)
                fixed["candidates"][0].pop("id")
                if alter_value:
                    fixed["candidates"][0]["value"] = "编造的年份"
                return LLMResponse(role="assistant", completion_text=json.dumps(fixed))
            return await working(**kwargs)

        plugin.context.llm_generate.side_effect = repair
        messages = old_and_recent(req)
        messages[1].content = "时间进入旧历二年"
        await manager.process(messages)
        saved = plugin.store.load(scope)
        assert saved["worldbook"]["facts"]["时代"] == (
            "旧历元年" if alter_value else "旧历二年"
        )

    asyncio.run(run())


def test_repeated_compaction_snapshot_is_idempotent(plugin):
    async def run():
        manager = ContextManager(ContextConfig(enforce_max_turns=1))
        event, req, _ = await bound(plugin, manager)
        messages = old_and_recent(req)
        await manager.process(messages)
        scope = event.get_extra("living_characters_turn")["scope"]
        first = plugin.store.load(scope)
        plugin.context.llm_generate.reset_mock()
        await manager.process(messages)
        assert plugin.store.load(scope)["revision"] == first["revision"]
        assert plugin.store.load(scope)["characters"] == first["characters"]
        plugin.context.llm_generate.assert_not_called()

    asyncio.run(run())
