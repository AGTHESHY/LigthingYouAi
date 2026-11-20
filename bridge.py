"""Per-run compression integration for the verified AstrBot native runtime."""

import copy

from astrbot.core.agent.message import Message


def narrative(messages: list) -> list[dict]:
    """Extract visible dialogue without tool outputs or synthetic summary pairs.

    Args:
        messages: Native message objects.

    Returns:
        Visible user and assistant text, excluding old generated summaries.
    """
    result = []
    for message in messages:
        if message.role not in ("user", "assistant"):
            continue
        data = message.model_dump()
        content = data.get("content", "")
        if isinstance(content, list):
            content = "\n".join(
                p.get("text", "") for p in content if p.get("type") == "text"
            )
        if not isinstance(content, str) or not content:
            continue
        if (
            content.startswith("Our previous history conversation summary:")
            or content
            == "Acknowledged the summary of our previous conversation history."
        ):
            continue
        result.append({"role": message.role, "content": content})
    return result


class CompressionBridge:
    """Observe one native manager, without patching any global AstrBot class."""

    def __init__(self, manager, callback):
        """Bind one request manager and a transactional archival callback.

        Args:
            manager: This runner's ContextManager instance.
            callback: Called only after messages were actually removed.
        """
        self.manager = manager
        self.original = manager.process
        self.callback = callback
        self.wrapper = self.process
        manager.process = self.wrapper

    async def process(self, messages, trusted_token_usage=0):
        """Detect actual discarded history and let archival safely replace it.

        Args:
            messages: Full native history before compaction.
            trusted_token_usage: Token usage forwarded unchanged to AstrBot.

        Returns:
            Core result or the archival callback's replacement.
        """
        before = copy.deepcopy(messages)
        result = await self.original(messages, trusted_token_usage=trusted_token_usage)
        old = [m for m in before if m.role != "system"]
        new = [m for m in result if m.role != "system"]
        if [m.model_dump() for m in old] == [m.model_dump() for m in new]:
            return result
        # Built-in compressors preserve exact recent rounds. Only the removed
        # prefix is archived, so retained events cannot be applied twice later.
        shared = 0
        while (
            shared < min(len(old), len(new))
            and old[-1 - shared].model_dump() == new[-1 - shared].model_dump()
        ):
            shared += 1
        if not shared:
            # Unknown compressor semantics: do not risk archiving an unfinished
            # request or dropping its tool-call pairs.
            return before
        discarded = old[:-shared]
        retained = new[-shared:]
        latest_user = next(
            (i for i in range(len(old) - 1, -1, -1) if old[i].role == "user"), None
        )
        if latest_user is None or len(discarded) > latest_user:
            # A legacy turn truncator can split a tool round. Never archive the
            # latest user input or discard it while keeping only its tool tail.
            return before
        if not discarded:
            return result
        return await self.callback(before, result, discarded, retained)

    def close(self):
        """Restore only our own instance wrapper when the run ends."""
        if self.manager.process == self.wrapper:
            self.manager.process = self.original


def replace_summary(processed: list, retained: list, summary: str) -> list:
    """Keep host system instructions, a public bridge, and exact recent rounds.

    Args:
        processed: Core compaction output.
        retained: Exact suffix of complete native messages.
        summary: Public continuity summary after character facts are archived.

    Returns:
        Compact context preserving recent tool-call relationships.
    """
    return (
        [m for m in processed if m.role == "system"]
        + [
            Message(
                role="user",
                content="Our previous history conversation summary: " + summary,
            ),
            Message(
                role="assistant",
                content="Acknowledged the summary of our previous conversation history.",
            ),
        ]
        + retained
    )
