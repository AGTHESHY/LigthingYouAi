"""Verify real 4.29.4 runner registration and both persona formats."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from astrbot.api.provider import LLMResponse, Provider
from astrbot.core.agent.context.manager import ContextManager
from astrbot.core.agent.hooks import BaseAgentRunHooks
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
from conftest import Event, request


@pytest.mark.parametrize("header", ["xml", "legacy"])
def test_effective_persona_format_replacement_preserves_host_instructions(
    plugin, header
):
    async def run():
        req = request()
        if header == "legacy":
            req.system_prompt = (
                "HOST SAFETY\n# Persona Instructions\n\n原始完整人设\nHOST TOOLS"
            )
        await plugin.prepare_scene(Event(), req)
        assert "原始完整人设" not in req.system_prompt
        assert req.system_prompt.startswith("HOST SAFETY")
        assert req.system_prompt.endswith("HOST TOOLS")
        assert "谨慎剑客" in req.system_prompt

    asyncio.run(run())


def test_metadata_accepts_installed_host_version():
    from pathlib import Path

    from astrbot.core.star.star_manager import PluginManager

    metadata = PluginManager._load_plugin_metadata(str(Path(__file__).parents[1]))
    valid, error = PluginManager._validate_astrbot_version_specifier(
        metadata.astrbot_version
    )
    assert valid, error


def test_real_4294_runner_binds_archives_and_restores_without_runtime_sessions(plugin):
    from astrbot.core.pipeline.process_stage import follow_up

    if not hasattr(follow_up, "_ACTIVE_AGENT_RUNNERS"):
        pytest.skip("4.30 uses native runtime sessions instead of the 4.29 registry")

    async def run():
        event, req = Event(), request("继续")
        req.system_prompt = (
            "HOST SAFETY\n# Persona Instructions\n\n原始完整人设\nHOST TOOLS"
        )
        req.conversation.token_usage = 0
        req.contexts = [
            {"role": "user", "content": "A接受治疗"},
            {"role": "assistant", "content": "A的腿伤已经痊愈，A知道自己恢复了。"},
            {"role": "user", "content": "A在医馆喝茶"},
            {"role": "assistant", "content": "A端起茶盏。"},
        ]
        await plugin.prepare_scene(event, req)
        turn = event.get_extra("living_characters_turn")
        assert turn is not None
        scope = turn["scope"]
        provider = MagicMock(spec=Provider)
        provider.provider_config = {"id": "test-main", "max_context_tokens": 0}
        provider.get_model.return_value = "test-model"
        provider.text_chat = AsyncMock(
            return_value=LLMResponse(role="assistant", completion_text="A静坐片刻。")
        )

        class Hooks(BaseAgentRunHooks):
            async def on_agent_begin(self, run_context):
                await plugin.bind_compression(event, run_context)
                assert (
                    event.get_extra("living_characters_turn").get("bridge") is not None
                )

            async def on_agent_done(self, run_context, response):
                await plugin.finish_run(event, run_context, response)

        runner = ToolLoopAgentRunner()
        context = ContextWrapper(context=SimpleNamespace(event=event))
        await runner.reset(
            provider=provider,
            request=req,
            run_context=context,
            tool_executor=MagicMock(),
            agent_hooks=Hooks(),
            streaming=False,
            enforce_max_turns=1,
            truncate_turns=1,
        )
        follow_up.register_active_runner(event.unified_msg_origin, runner)
        try:
            async for _ in runner.step_until_done(2):
                pass
            saved = plugin.store.load(scope)
            assert saved["revision"] == 1
            assert saved["characters"]["r1"]["state"]["腿伤"] == "已痊愈"
            assert "曾在山路受伤" in req.system_prompt
            assert (
                runner.request_context_manager.process.__func__
                is ContextManager.process
            )
            provider.text_chat.assert_awaited_once()
            assert runner.get_final_llm_resp().completion_text == "A静坐片刻。"
        finally:
            follow_up.unregister_active_runner(event.unified_msg_origin, runner)
            await plugin.terminate()

    asyncio.run(run())


def test_legacy_registry_rejects_another_run_context(plugin, monkeypatch):
    from astrbot.core.pipeline.process_stage import follow_up

    if not hasattr(follow_up, "_ACTIVE_AGENT_RUNNERS"):
        pytest.skip("Legacy registry is absent on this host")

    async def run():
        event, req = Event(), request()
        original = req.system_prompt
        await plugin.prepare_scene(event, req)
        context = SimpleNamespace(context=SimpleNamespace(event=event), messages=[])
        other = SimpleNamespace(run_context=SimpleNamespace(), req=req)
        monkeypatch.setitem(
            follow_up._ACTIVE_AGENT_RUNNERS, event.unified_msg_origin, other
        )
        await plugin.bind_compression(event, context)
        assert req.system_prompt == original
        assert not event.get_extra("living_characters_turn").get("bridge")

    asyncio.run(run())
