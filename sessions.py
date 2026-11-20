"""Read native session metadata and initialize only the selected story."""

import hashlib
import inspect
import json
from types import SimpleNamespace

from .engine import new_archive
from .importer import parse_marked_persona
from .prompts import IMPORT


class InitializationError(ValueError):
    """An initialization error safe to show without provider payloads or secrets."""


class ArchiveSessions:
    """Share native conversation identity between request hooks and the view."""

    def __init__(self, plugin):
        """Bind plugin-owned stores and errors.

        Args:
            plugin: Active LivingCharacters instance.
        """
        self.plugin = plugin
        self.references = {}
        self.errors = {}

    @staticmethod
    def scope(origin, conversation_id, persona_id, source):
        """Derive the same archive identity for requests and imported sessions.

        Args:
            origin: Host message origin.
            conversation_id: Native conversation identifier.
            persona_id: Resolved persona identifier.
            source: Effective persona text.

        Returns:
            Stable scoped identity string.
        """
        return json.dumps(
            [
                origin,
                conversation_id,
                persona_id,
                hashlib.sha256(source.encode()).hexdigest(),
            ]
        )

    async def refresh(self):
        """Discover native sessions using host APIs without reading dialogue.

        Returns:
            Current native session references, including uninitialized ones.
        """
        context = self.plugin.context
        manager = getattr(context, "conversation_manager", None)
        if manager is None:
            return []
        resolver = context.persona_manager.resolve_selected_persona
        legacy = "conversation_persona_id" in inspect.signature(resolver).parameters
        references = {}
        page = 1
        while True:
            conversations, count = await manager.get_filtered_conversations(
                page=page, page_size=100, include_history=False
            )
            for conversation in conversations:
                origin = conversation.user_id
                kwargs = {"umo": origin, "platform_name": conversation.platform_id}
                if legacy:
                    kwargs["conversation_persona_id"] = conversation.persona_id
                    kwargs["provider_settings"] = context.get_config(origin).get(
                        "provider_settings", {}
                    )
                resolved = await resolver(**kwargs)
                persona_id, persona = resolved[:2]
                source = persona.get("prompt", "") if persona else ""
                scope = self.scope(origin, conversation.cid, persona_id, source)
                archive_id = self.plugin.store.path(scope).stem
                references[archive_id] = {
                    "scope": scope,
                    "source": source,
                    "persona_id": persona_id,
                    "conversation": conversation.cid,
                    "origin": origin,
                    "title": conversation.title or str(persona_id or "未选择人设"),
                    "status": "pending" if source else "no_persona",
                }
            if page * 100 >= count or not conversations:
                break
            page += 1
        self.references = references
        return list(references.items())

    async def initialize(
        self, event, source, persona_id, conversation_id, model=None, title=None
    ):
        """Build a base archive once, without replaying or modifying past chat.

        Args:
            event: Event or lightweight origin wrapper used to select the model.
            source: Resolved source persona.
            persona_id: Effective persona identifier.
            conversation_id: Native conversation identifier.
            model: Optional model override from the active request.
            title: Friendly existing conversation title.

        Returns:
            Archive scope and validated base archive.

        Raises:
            InitializationError: Initialization failed with a safe user message.
        """
        import asyncio

        scope = self.scope(
            event.unified_msg_origin, conversation_id, persona_id, source
        )
        archive_id = self.plugin.store.path(scope).stem
        lock = self.plugin.locks.setdefault(archive_id, asyncio.Lock())
        async with lock:
            try:
                archive = self.plugin.store.load(scope)
                if archive is not None:
                    self.errors.pop(archive_id, None)
                    return scope, archive
                if not source.strip():
                    raise InitializationError(
                        "此会话没有可读取的角色人设，请先在 AstrBot 中选择人设。"
                    )
                archive = parse_marked_persona(source)
                if archive is None:
                    archive = new_archive(
                        await self.plugin.analyze(
                            event, IMPORT, {"persona": source}, model
                        )
                    )
                archive["label"] = {
                    "title": title or str(persona_id or "剧情档案"),
                    "conversation": conversation_id,
                    "origin": event.unified_msg_origin,
                }
                self.plugin.store.save(scope, archive)
                self.errors.pop(archive_id, None)
                return scope, archive
            except Exception as exc:
                if isinstance(exc, InitializationError):
                    message = str(exc)
                elif isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
                    message = "初始化模型调用超时，原会话与人设未改动，可以重试。"
                elif isinstance(exc, json.JSONDecodeError):
                    message = "模型未返回完整 JSON；请检查模型输出预算或使用明确的角色标记后重试。"
                elif isinstance(exc, (ValueError, KeyError, TypeError)):
                    message = "初始化结果不符合档案结构；模型可能返回空正文或输出被截断，请检查模型并重试。"
                elif isinstance(exc, OSError):
                    message = "初始化档案无法写入，原会话与人设未改动。"
                else:
                    message = "初始化模型暂不可用，原会话与人设未改动，可以重试。"
                self.errors[archive_id] = message
                raise InitializationError(message) from exc

    async def initialize_id(self, archive_id):
        """Initialize a server-resolved native session, never caller text or IDs.

        Args:
            archive_id: Opaque catalog identifier.

        Returns:
            Existing or newly initialized base archive.

        Raises:
            InitializationError: Native session or persona has changed.
        """
        self.plugin.store.id_path(archive_id)
        await self.refresh()
        reference = self.references.get(archive_id)
        if reference is None:
            raise InitializationError("会话或人设已变化，请刷新会话列表。")
        event = SimpleNamespace(unified_msg_origin=reference["origin"])
        _, archive = await self.initialize(
            event,
            reference["source"],
            reference["persona_id"],
            reference["conversation"],
            title=reference["title"],
        )
        return archive
