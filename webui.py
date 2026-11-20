"""Authenticated native plugin view APIs for inspecting and editing archives."""

import asyncio
import copy
import json

from astrbot.api.web import error_response, json_response, request

from .engine import checked_text, new_archive
from .sessions import InitializationError

PLUGIN_NAME = "astrbot_plugin_living_characters"


class ArchiveWebAPI:
    """Expose plugin-owned archives to authenticated Dashboard administrators."""

    def __init__(self, plugin):
        """Register only plugin-scoped routes behind AstrBot authentication.

        Args:
            plugin: LivingCharacters instance sharing storage and edit locks.
        """
        self.plugin = plugin
        self.routes = [
            (
                f"/{PLUGIN_NAME}/archives",
                self.list_archives,
                ["GET"],
                "List story archives",
            ),
            (
                f"/{PLUGIN_NAME}/archives/<archive_id>",
                self.get_archive,
                ["GET"],
                "Read a story archive",
            ),
            (
                f"/{PLUGIN_NAME}/archives/<archive_id>/save",
                self.save_archive,
                ["POST"],
                "Save a story archive",
            ),
            (
                f"/{PLUGIN_NAME}/archives/<archive_id>/initialize",
                self.initialize_archive,
                ["POST"],
                "Initialize a native conversation archive",
            ),
        ]
        for route in self.routes:
            plugin.context.register_web_api(*route)

    async def list_archives(self):
        """List plugin story files without exposing internal paths or history.

        Returns:
            Summary records visible to the authenticated Dashboard operator.
        """
        if not request.username:
            return error_response("请先登录 AstrBot WebUI", status_code=401)
        references = dict(await self.plugin.sessions.refresh())
        items = {
            archive_id: {
                "id": archive_id,
                "title": reference["title"],
                "conversation": reference["conversation"],
                "origin": reference["origin"],
                "status": "failed"
                if archive_id in self.plugin.sessions.errors
                else reference["status"],
                "error": self.plugin.sessions.errors.get(archive_id, ""),
                "characters": 0,
                "revision": 0,
            }
            for archive_id, reference in references.items()
        }
        for path in self.plugin.store.directory.glob("*.json"):
            try:
                archive = self.plugin.store.load_id(path.stem)
                if archive is None:
                    continue
                label = archive.get("label", {})
                names = [r["name"] for r in archive["characters"].values()]
                items[path.stem] = {
                    "id": path.stem,
                    "title": label.get("title") or "、".join(names[:3]) or "剧情档案",
                    "conversation": label.get("conversation", ""),
                    "origin": label.get("origin", ""),
                    "characters": len(names),
                    "revision": archive["revision"],
                    "status": "ready",
                }
            except (ValueError, OSError, KeyError, TypeError):
                continue
        items = sorted(items.values(), key=lambda item: (item["title"], item["id"]))
        return json_response({"archives": items})

    async def get_archive(self, archive_id):
        """Read an editable author document, excluding transient runtime data.

        Args:
            archive_id: Opaque file identifier supplied by the native view.

        Returns:
            Validated editable document and revision, or an HTTP error.
        """
        if not request.username:
            return error_response("请先登录 AstrBot WebUI", status_code=401)
        try:
            archive = self.plugin.store.load_id(archive_id)
            if archive is None:
                await self.plugin.sessions.refresh()
                reference = self.plugin.sessions.references.get(archive_id)
                if reference is None:
                    return error_response("档案不存在", status_code=404)
                return json_response(
                    {
                        "id": archive_id,
                        "title": reference["title"],
                        "status": reference["status"],
                        "error": self.plugin.sessions.errors.get(archive_id, ""),
                        "document": None,
                    }
                )
            document = {
                "world": archive["world"],
                "worldbook": archive["worldbook"],
                "characters": [
                    {"id": key, **role} for key, role in archive["characters"].items()
                ],
            }
            if archive.get("world_base"):
                document["world_base"] = archive["world_base"]
            # Stored files are validated before they can enter an edit session.
            new_archive(document)
            return json_response(
                {
                    "id": archive_id,
                    "revision": archive["revision"],
                    "title": archive.get("label", {}).get("title", "剧情档案"),
                    "document": document,
                }
            )
        except ValueError:
            return error_response("档案标识或档案结构无效", status_code=400)
        except (OSError, KeyError, TypeError):
            return error_response("无法读取档案，请检查文件是否完整", status_code=400)

    async def initialize_archive(self, archive_id):
        """Initialize the selected native session from its effective persona.

        Args:
            archive_id: Server-generated native catalog identifier.

        Returns:
            Initialization acknowledgement or a safe explanation.
        """
        if not request.username:
            return error_response("请先登录 AstrBot WebUI", status_code=401)
        try:
            await self.plugin.sessions.initialize_id(archive_id)
            return json_response({"initialized": True})
        except InitializationError as exc:
            return error_response(str(exc), status_code=422)
        except ValueError:
            return error_response("档案标识无效", status_code=400)

    async def save_archive(self, archive_id):
        """Apply explicit author edits while preventing stale compaction writes.

        Args:
            archive_id: Opaque file identifier.

        Returns:
            Saved revision or a validation/conflict response.
        """
        if not request.username:
            return error_response("请先登录 AstrBot WebUI", status_code=401)
        try:
            self.plugin.store.id_path(archive_id)
            body = await request.body()
            if len(body) > 2_000_000:
                return error_response("档案内容过大", status_code=413)
            payload = json.loads(body)
            if (
                not isinstance(payload, dict)
                or set(payload) != {"revision", "title", "document"}
                or type(payload["revision"]) is not int
            ):
                raise ValueError("Invalid edit payload")
            title = checked_text(payload["title"], 120, False)
            validated = new_archive(payload["document"])
            lock = self.plugin.locks.setdefault(archive_id, asyncio.Lock())
            async with lock:
                current = self.plugin.store.load_id(archive_id)
                if current is None:
                    return error_response("档案不存在", status_code=404)
                if current["revision"] != payload["revision"]:
                    return error_response(
                        "档案已被剧情压缩或其他页面更新，请刷新后再编辑",
                        status_code=409,
                    )
                if list(current["characters"]) != list(validated["characters"]):
                    return error_response(
                        "角色标识不可更改；当前界面仅编辑已有角色", status_code=400
                    )
                updated = copy.deepcopy(current)
                for key in ("world", "worldbook", "characters"):
                    updated[key] = validated[key]
                if "world_base" in validated:
                    updated["world_base"] = validated["world_base"]
                else:
                    updated.pop("world_base", None)
                updated.setdefault("label", {})["title"] = title
                updated["revision"] += 1
                updated.pop("compaction_id", None)
                self.plugin.store.save_id(archive_id, updated)
                for scope in list(self.plugin.scenes):
                    if self.plugin.store.path(scope).stem == archive_id:
                        self.plugin.scenes.pop(scope, None)
                return json_response({"saved": True, "revision": updated["revision"]})
        except (ValueError, KeyError, TypeError):
            return error_response(
                "保存失败：请检查名称、角色字段及势力关系端点，不能留空或重复",
                status_code=400,
            )
        except OSError:
            return error_response(
                "保存失败：文件未能写入，原档案已保留", status_code=500
            )

    def close(self):
        """Remove only handlers owned by this plugin instance on unload."""
        registered = self.plugin.context.registered_web_apis
        handlers = {route[1] for route in self.routes}
        registered[:] = [route for route in registered if route[1] not in handlers]
