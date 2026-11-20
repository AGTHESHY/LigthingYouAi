"""Hook tests against the real AstrBot SDK with deterministic model results."""

import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot.api.provider import (
    DecisionProvider,
    LLMResponse,
    Provider,
    ProviderRequest,
)
from astrbot_plugin_living_characters.main import LivingCharacters
from astrbot_plugin_living_characters.prompts import IMPORT


class Event:
    unified_msg_origin = "test:group:one"

    def __init__(self):
        self.extras = {"_trace_persona_id": "story"}

    def get_extra(self, key):
        return self.extras.get(key)

    def set_extra(self, key, value):
        self.extras[key] = value


SEED = {
    "world": "医馆剧情",
    "characters": [
        {
            "id": "r1",
            "name": "A",
            "core": "谨慎剑客",
            "state": "腿伤未愈",
            "growth": "",
            "memories": [{"text": "曾在山路受伤", "known": True}],
        },
        {
            "id": "r2",
            "name": "B",
            "core": "温和医师",
            "state": "在别处出诊",
            "growth": "",
            "memories": [],
        },
    ],
}


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "astrbot_plugin_living_characters.main.StarTools.get_data_dir",
        lambda *_: tmp_path,
    )
    ctx = SimpleNamespace(
        persona_manager=SimpleNamespace(
            get_persona_v3_by_id=lambda _: {
                "prompt": "A是谨慎剑客，腿伤未愈。B是温和医师，外出出诊。"
            }
        ),
        get_current_chat_provider_id=AsyncMock(return_value="session-jev"),
        get_provider_by_id=lambda _: None,
        llm_generate=AsyncMock(),
    )
    p = LivingCharacters(ctx, {"decision_provider_id": ""})

    async def generate(**kwargs):
        if kwargs["system_prompt"] == IMPORT:
            data = copy.deepcopy(SEED)
        else:
            payload = json.loads(kwargs["prompt"])
            text = "\n".join(m["content"] for m in payload["new_evidence"])
            data = {"active": ["r1"], "changes": []}
            if "A的腿伤已经痊愈" in text:
                data["changes"] = [
                    {
                        "id": "r1",
                        "state": {"value": "腿伤痊愈", "evidence": "A的腿伤已经痊愈"},
                        "memories": [
                            {
                                "text": "腿伤经治疗痊愈",
                                "known": True,
                                "evidence": "A的腿伤已经痊愈",
                            }
                        ],
                    }
                ]
        return LLMResponse(
            role="assistant", completion_text=json.dumps(data, ensure_ascii=False)
        )

    ctx.llm_generate.side_effect = generate
    return p


def request(cid="story-one", prompt="A在医馆休息"):
    return ProviderRequest(
        prompt=prompt,
        contexts=[],
        model="jev",
        conversation=SimpleNamespace(cid=cid),
        system_prompt="HOST SAFETY\n<persona_instructions>\nA是谨慎剑客，腿伤未愈。B是温和医师，外出出诊。\n</persona_instructions>\nHOST TOOLS",
    )


def test_default_provider_and_exact_prompt_replacement(plugin):
    async def run():
        event, req = Event(), request()
        await plugin.prepare_scene(event, req)
        assert req.system_prompt.startswith("HOST SAFETY\n<persona_instructions>\n")
        assert req.system_prompt.endswith("\n</persona_instructions>\nHOST TOOLS")
        assert "温和医师" not in req.system_prompt
        assert all(
            call.kwargs["chat_provider_id"] == "session-jev"
            for call in plugin.context.llm_generate.call_args_list
        )
        assert all(
            call.kwargs["model"] == "jev"
            for call in plugin.context.llm_generate.call_args_list
        )

    asyncio.run(run())


def test_completed_turn_survives_restart_and_compression(plugin):
    async def run():
        event, req = Event(), request()
        await plugin.prepare_scene(event, req)
        scope = event.get_extra("living_characters_turn")["scope"]
        await plugin.save_growth(
            event,
            SimpleNamespace(run_end_reason="completed"),
            LLMResponse(role="assistant", completion_text="A的腿伤已经痊愈"),
        )
        saved = plugin.store.load(scope)
        assert saved["characters"]["r1"]["state"] == "腿伤痊愈"
        assert len(saved["characters"]["r1"]["memories"]) == 2
        assert saved["revision"] == 1
        # A new request no longer has the old history, simulating core compaction.
        req = request(prompt="A回想起以前受伤的日子")
        await plugin.prepare_scene(Event(), req)
        assert "腿伤痊愈" in req.system_prompt
        assert "曾在山路受伤" in req.system_prompt
        assert "腿伤未愈" not in req.system_prompt

    asyncio.run(run())


@pytest.mark.parametrize("reason", ["error", "cancelled", "max_steps"])
def test_unsuccessful_turn_does_not_commit(plugin, reason):
    async def run():
        event = Event()
        await plugin.prepare_scene(event, request())
        scope = event.get_extra("living_characters_turn")["scope"]
        before = plugin.store.load(scope)
        await plugin.save_growth(
            event,
            SimpleNamespace(run_end_reason=reason),
            LLMResponse(role="assistant", completion_text="A的腿伤已经痊愈"),
        )
        assert plugin.store.load(scope) == before

    asyncio.run(run())


def test_failed_save_has_durable_retry_before_next_compression(plugin):
    async def run():
        event = Event()
        await plugin.prepare_scene(event, request())
        scope = event.get_extra("living_characters_turn")["scope"]
        working = plugin.context.llm_generate.side_effect
        plugin.context.llm_generate.side_effect = TimeoutError(
            "secret must not be logged"
        )
        await plugin.save_growth(
            event,
            SimpleNamespace(run_end_reason="completed"),
            LLMResponse(role="assistant", completion_text="A的腿伤已经痊愈"),
        )
        pending = plugin.store.load(scope)
        assert pending["characters"]["r1"]["state"] == "腿伤未愈"
        assert pending["pending"][-1]["content"] == "A的腿伤已经痊愈"
        plugin.context.llm_generate.side_effect = working
        req = request(prompt="继续")
        await plugin.prepare_scene(Event(), req)
        saved = plugin.store.load(scope)
        assert "pending" not in saved
        assert saved["characters"]["r1"]["state"] == "腿伤痊愈"
        assert "腿伤痊愈" in req.system_prompt

    asyncio.run(run())


def test_import_failure_preserves_full_original(plugin):
    async def run():
        plugin.context.llm_generate.side_effect = ValueError("bad json")
        req = request()
        original = req.system_prompt
        await plugin.prepare_scene(Event(), req)
        assert req.system_prompt == original
        assert not list(plugin.store.directory.glob("*.json"))

    asyncio.run(run())


def test_dedicated_chat_provider_overrides_session(plugin):
    async def run():
        plugin.config["decision_provider_id"] = "dedicated"
        provider = AsyncMock(spec=Provider)
        plugin.context.get_provider_by_id = lambda _: provider
        await plugin.prepare_scene(Event(), request())
        assert all(
            c.kwargs["chat_provider_id"] == "dedicated"
            for c in plugin.context.llm_generate.call_args_list
        )
        assert all(
            "model" not in c.kwargs for c in plugin.context.llm_generate.call_args_list
        )
        plugin.context.get_current_chat_provider_id.assert_not_called()

    asyncio.run(run())


def test_native_decision_provider_uses_session_llm_for_text(plugin):
    from astrbot.api.provider import DecisionBooleanAnswer

    async def run():
        plugin.config["decision_provider_id"] = "native-decision"
        plugin.context.get_provider_by_id = lambda _: AsyncMock(spec=DecisionProvider)
        plugin.context.decision = SimpleNamespace(
            ask=AsyncMock(
                return_value={
                    "r1": DecisionBooleanAnswer(0.9),
                    "r2": DecisionBooleanAnswer(0.1),
                }
            )
        )
        req = request()
        await plugin.prepare_scene(Event(), req)
        assert "温和医师" not in req.system_prompt
        plugin.context.decision.ask.assert_awaited_once()
        assert all(
            c.kwargs["chat_provider_id"] == "session-jev"
            for c in plugin.context.llm_generate.call_args_list
        )

    asyncio.run(run())


def test_new_conversation_uses_separate_archive(plugin):
    async def run():
        first, second = Event(), Event()
        await plugin.prepare_scene(first, request("one"))
        await plugin.prepare_scene(second, request("two"))
        assert (
            first.get_extra("living_characters_turn")["scope"]
            != second.get_extra("living_characters_turn")["scope"]
        )
        assert len(list(plugin.store.directory.glob("*.json"))) == 2

    asyncio.run(run())


def test_real_core_compression_does_not_erase_character_state(plugin):
    from astrbot.core.agent.context.config import ContextConfig
    from astrbot.core.agent.context.manager import ContextManager
    from astrbot.core.agent.message import Message

    async def run():
        event = Event()
        await plugin.prepare_scene(event, request())
        scope = event.get_extra("living_characters_turn")["scope"]
        await plugin.save_growth(
            event,
            SimpleNamespace(run_end_reason="completed"),
            LLMResponse(role="assistant", completion_text="A的腿伤已经痊愈"),
        )
        next_req = request(prompt="A在医馆喝茶")
        await plugin.prepare_scene(Event(), next_req)
        summary_provider = SimpleNamespace(
            provider_config={},
            text_chat=AsyncMock(
                return_value=LLMResponse(
                    role="assistant", completion_text="故事发生在医馆。"
                )
            ),
        )
        manager = ContextManager(
            ContextConfig(
                max_context_tokens=3000,
                llm_compress_provider=summary_provider,
                llm_compress_keep_recent_ratio=0.15,
            )
        )
        messages = [
            Message(role="system", content=next_req.system_prompt),
            Message(role="user", content="早年的冒险。" * 10000),
            Message(role="assistant", content="A的腿伤已经痊愈"),
            Message(role="user", content="A在医馆喝茶"),
        ]
        compacted = await manager.process(messages)
        summary_provider.text_chat.assert_awaited_once()
        assert len(str(compacted)) < len(str(messages))
        assert plugin.store.load(scope)["characters"]["r1"]["state"] == "腿伤痊愈"
        after = request(prompt="A还记得受伤吗？")
        after.contexts = [m.model_dump() for m in compacted if m.role != "system"]
        await plugin.prepare_scene(Event(), after)
        assert "腿伤痊愈" in after.system_prompt
        assert "曾在山路受伤" in after.system_prompt
        assert "腿伤未愈" not in after.system_prompt

    asyncio.run(run())


def test_scene_failure_on_first_turn_recovers_history_next_time(plugin):
    async def run():
        working = plugin.context.llm_generate.side_effect

        async def fail_scene(**kwargs):
            if kwargs["system_prompt"] == IMPORT:
                return await working(**kwargs)
            raise TimeoutError()

        plugin.context.llm_generate.side_effect = fail_scene
        req = request()
        original = req.system_prompt
        await plugin.prepare_scene(Event(), req)
        assert req.system_prompt == original
        plugin.context.llm_generate.side_effect = working
        next_req = request(prompt="继续")
        next_req.contexts = [{"role": "assistant", "content": "A的腿伤已经痊愈"}]
        await plugin.prepare_scene(Event(), next_req)
        assert "腿伤痊愈" in next_req.system_prompt
        assert "腿伤未愈" not in next_req.system_prompt

    asyncio.run(run())


def test_old_context_is_not_accepted_as_new_change_evidence(plugin):
    async def run():
        event = Event()
        await plugin.prepare_scene(event, request())
        await plugin.save_growth(
            event,
            SimpleNamespace(run_end_reason="completed"),
            LLMResponse(role="assistant", completion_text="A的腿伤已经痊愈"),
        )

        async def replay_old_injury(**kwargs):
            return LLMResponse(
                role="assistant",
                completion_text=json.dumps(
                    {
                        "active": ["r1"],
                        "changes": [
                            {
                                "id": "r1",
                                "state": {
                                    "value": "腿伤未愈",
                                    "evidence": "A在多年前腿部受伤",
                                },
                            }
                        ],
                    }
                ),
            )

        plugin.context.llm_generate.side_effect = replay_old_injury
        next_req = request(prompt="A喝茶")
        next_req.contexts = [{"role": "user", "content": "A在多年前腿部受伤"}]
        await plugin.prepare_scene(Event(), next_req)
        assert "腿伤痊愈" in next_req.system_prompt
        assert "腿伤未愈" not in next_req.system_prompt

    asyncio.run(run())


def test_host_metadata_accepts_current_prerelease():
    from pathlib import Path

    from astrbot.core.star.star_manager import PluginManager

    metadata = PluginManager._load_plugin_metadata(str(Path(__file__).parents[1]))
    assert metadata.name == "astrbot_plugin_living_characters"
    assert PluginManager._validate_astrbot_version_specifier(metadata.astrbot_version)[
        0
    ]
