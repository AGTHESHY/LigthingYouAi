"""Existing native sessions synchronize without replaying their messages."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot.api.provider import LLMResponse
from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
from astrbot_plugin_living_characters.prompts import IMPORT
from conftest import Event
from test_webui import bound_request, decoded


def native_session(plugin, legacy=False):
    conversation = SimpleNamespace(
        user_id=Event.unified_msg_origin,
        platform_id="test",
        cid="story-one",
        title="已有会话",
        persona_id=None,
    )
    manager = SimpleNamespace(
        get_filtered_conversations=AsyncMock(return_value=([conversation], 1))
    )
    plugin.context.conversation_manager = manager
    if legacy:

        async def resolve(
            *, umo, platform_name, conversation_persona_id, provider_settings
        ):
            assert conversation_persona_id is None
            assert provider_settings == {"persona_id": "inherited-story"}
            return "inherited-story", {"prompt": "原始完整人设"}, False, None
    else:

        async def resolve(*, umo, platform_name):
            return "inherited-story", {"prompt": "原始完整人设"}, False

    plugin.context.persona_manager.resolve_selected_persona = resolve
    plugin.context.get_config = lambda _: {
        "provider_settings": {"persona_id": "inherited-story"}
    }
    return manager


@pytest.mark.parametrize("legacy", [True, False])
def test_native_catalog_then_selected_initialization(plugin, legacy):
    async def run():
        manager = native_session(plugin, legacy)
        with bound_request():
            rows = decoded(await plugin.webui.list_archives())["archives"]
            assert len(rows) == 1 and rows[0]["status"] == "pending"
            plugin.context.llm_generate.assert_not_called()
            archive_id = rows[0]["id"]
            assert (
                decoded(await plugin.webui.get_archive(archive_id))["document"] is None
            )
            assert (
                await plugin.webui.initialize_archive(archive_id)
            ).status_code == 200
            loaded = decoded(await plugin.webui.get_archive(archive_id))
            assert loaded["title"] == "已有会话"
            assert len(loaded["document"]["characters"]) == 2
            assert (
                decoded(await plugin.webui.list_archives())["archives"][0]["status"]
                == "ready"
            )
            calls = plugin.context.llm_generate.call_count
            assert (
                await plugin.webui.initialize_archive(archive_id)
            ).status_code == 200
            assert plugin.context.llm_generate.call_count == calls
        assert all(
            c.kwargs["include_history"] is False
            for c in manager.get_filtered_conversations.call_args_list
        )

    asyncio.run(run())


def test_initialization_failure_visible_and_retryable(plugin):
    async def run():
        native_session(plugin)
        plugin.context.llm_generate.return_value = LLMResponse(
            role="assistant", completion_text=""
        )
        saved = plugin.context.llm_generate.side_effect
        plugin.context.llm_generate.side_effect = None
        with bound_request():
            archive_id = decoded(await plugin.webui.list_archives())["archives"][0][
                "id"
            ]
            response = await plugin.webui.initialize_archive(archive_id)
            assert response.status_code == 422
            row = decoded(await plugin.webui.list_archives())["archives"][0]
            assert row["status"] == "failed" and "空正文" in row["error"]
            assert plugin.store.load_id(archive_id) is None
            plugin.context.llm_generate.side_effect = saved
            assert (
                await plugin.webui.initialize_archive(archive_id)
            ).status_code == 200

    asyncio.run(run())


def test_initialization_auth_and_unknown_id(plugin):
    async def run():
        native_session(plugin)
        with bound_request(username=None):
            assert (await plugin.webui.initialize_archive("a" * 64)).status_code == 401
        with bound_request():
            assert (await plugin.webui.initialize_archive("../bad")).status_code == 400
            assert (await plugin.webui.initialize_archive("a" * 64)).status_code == 422
        plugin.context.llm_generate.assert_not_called()

    asyncio.run(run())


def test_auxiliary_budget_isolated_from_live_provider(plugin):
    class CapturingProvider(ProviderOpenAIOfficial):
        async def text_chat(self, **kwargs):
            assert self.provider_config["custom_extra_body"]["max_tokens"] >= 8192
            assert (
                self.provider_config["custom_extra_body"]["reasoning_effort"] == "low"
            )
            return LLMResponse(
                role="assistant", completion_text=json.dumps({"ok": True})
            )

    async def run():
        provider = object.__new__(CapturingProvider)
        config = {"custom_extra_body": {"max_tokens": 256, "reasoning_effort": "low"}}
        provider.provider_config = config
        plugin.context.get_provider_by_id = lambda _: provider
        assert await plugin.analyze(
            Event(), IMPORT, {"persona": "neutral public fixture"}
        ) == {"ok": True}
        assert provider.provider_config is config
        assert config["custom_extra_body"]["max_tokens"] == 256
        plugin.context.llm_generate.assert_not_called()

    asyncio.run(run())
