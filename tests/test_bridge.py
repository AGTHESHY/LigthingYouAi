"""Compression detection and preservation of native message structure."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from astrbot.core.agent.message import Message
from astrbot_plugin_living_characters.bridge import (
    CompressionBridge,
    narrative,
    replace_summary,
)


def test_noop_compression_never_archives_even_if_revision_changed():
    async def run():
        messages = [Message(role="user", content="原文")]
        original = AsyncMock(return_value=messages)
        manager = SimpleNamespace(process=original, revision=1)
        callback = AsyncMock()
        bridge = CompressionBridge(manager, callback)
        assert await manager.process(messages, trusted_token_usage=123) is messages
        callback.assert_not_called()
        original.assert_awaited_once_with(messages, trusted_token_usage=123)
        bridge.close()
        assert manager.process is original

    asyncio.run(run())


def test_unrecognized_compressor_cannot_drop_latest_request():
    async def run():
        messages = [
            Message(role="user", content="旧剧情"),
            Message(role="user", content="当前问题"),
        ]
        manager = SimpleNamespace(
            process=AsyncMock(
                return_value=[Message(role="user", content="不兼容的替换")]
            )
        )
        callback = AsyncMock()
        CompressionBridge(manager, callback)
        result = await manager.process(messages)
        assert [m.model_dump() for m in result] == [m.model_dump() for m in messages]
        callback.assert_not_called()

    asyncio.run(run())


def test_old_summary_and_reasoning_are_not_new_evidence():
    messages = [
        Message(
            role="user", content="Our previous history conversation summary: 旧事实"
        ),
        Message(
            role="assistant",
            content="Acknowledged the summary of our previous conversation history.",
        ),
        Message(
            role="assistant",
            content=[
                {"type": "think", "think": "未发生的猜测"},
                {"type": "text", "text": "明确的剧情"},
            ],
        ),
    ]
    assert narrative(messages) == [{"role": "assistant", "content": "明确的剧情"}]


def test_summary_replacement_preserves_retained_tool_pairs():
    retained = [
        Message(role="user", content="当前任务"),
        Message(
            role="assistant",
            tool_calls=[
                {
                    "id": "call1",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": "{}"},
                }
            ],
        ),
        Message(role="tool", tool_call_id="call1", content="结果"),
    ]
    system = Message(role="system", content="权限和人设")
    result = replace_summary([system], retained, "公共事件尚未解决")
    assert result[0] is system
    assert [m.model_dump() for m in result[-3:]] == [m.model_dump() for m in retained]


def test_split_tool_round_cannot_archive_current_user_request():
    async def run():
        old = [
            Message(role="user", content="旧任务"),
            Message(role="assistant", content="已完成"),
            Message(role="user", content="当前尚未完成的任务"),
            Message(
                role="assistant",
                tool_calls=[
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "lookup", "arguments": "{}"},
                    }
                ],
            ),
            Message(role="tool", tool_call_id="c1", content="工具结果"),
        ]
        manager = SimpleNamespace(process=AsyncMock(return_value=[old[0], old[-1]]))
        callback = AsyncMock()
        CompressionBridge(manager, callback)
        result = await manager.process(old)
        assert [m.model_dump() for m in result] == [m.model_dump() for m in old]
        callback.assert_not_called()

    asyncio.run(run())
