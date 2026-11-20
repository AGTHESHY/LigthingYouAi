"""Native plugin page APIs: access checks, validation and concurrent edits."""

import asyncio
import copy
import json
from pathlib import Path

import pytest
from astrbot.api.web import PluginRequest, bind_request_context
from astrbot_plugin_living_characters.engine import new_archive
from conftest import SEED
from starlette.requests import Request


def bound_request(payload=None, username="admin"):
    body = json.dumps(payload).encode() if payload is not None else b""
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    req = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/plugins/extensions/test/archives",
            "headers": [(b"content-type", b"application/json")],
            "query_string": b"",
            "scheme": "http",
            "server": ("test", 80),
            "client": ("test", 1),
        },
        receive,
    )
    return bind_request_context(PluginRequest(req, username=username))


def decoded(response):
    return json.loads(response.body)


def editable(plugin, archive):
    scope = "webui-test"
    archive = copy.deepcopy(archive)
    archive["label"] = {
        "title": "医馆故事",
        "origin": "test:group:one",
        "conversation": "story-one",
    }
    plugin.store.save(scope, archive)
    return plugin.store.path(scope).stem, {
        "revision": archive["revision"],
        "title": "医馆故事",
        "document": {
            "world": archive["world"],
            "worldbook": archive["worldbook"],
            "characters": [
                {"id": key, **role} for key, role in archive["characters"].items()
            ],
        },
    }


def test_api_routes_are_registered_under_plugin_namespace(plugin):
    assert plugin.context.register_web_api.call_count == 3
    assert all(
        c.args[0].startswith("/astrbot_plugin_living_characters/")
        for c in plugin.context.register_web_api.call_args_list
    )


def test_list_and_read_never_return_transient_raw_history(plugin, archive):
    async def run():
        archive["pending"] = [{"role": "assistant", "content": "不应返回的历史"}]
        archive["compaction_id"] = "internal"
        archive_id, _ = editable(plugin, archive)
        with bound_request():
            listing = decoded(await plugin.webui.list_archives())
            response = await plugin.webui.get_archive(archive_id)
        assert listing["archives"][0]["title"] == "医馆故事"
        assert response.status_code == 200
        data = decoded(response)
        assert set(data) == {"id", "title", "revision", "document"}
        assert "pending" not in data and "不应返回的历史" not in response.body.decode()
        new_archive(data["document"])

    asyncio.run(run())


@pytest.mark.parametrize("operation", ["list", "get", "save"])
def test_api_requires_authenticated_dashboard_user(plugin, archive, operation):
    async def run():
        archive_id, payload = editable(plugin, archive)
        with bound_request(payload, username=None):
            response = await (
                plugin.webui.list_archives()
                if operation == "list"
                else plugin.webui.get_archive(archive_id)
                if operation == "get"
                else plugin.webui.save_archive(archive_id)
            )
        assert response.status_code == 401

    asyncio.run(run())


def test_manual_edit_can_change_fixed_rules_role_and_macro_world(plugin, archive):
    async def run():
        archive_id, payload = editable(plugin, archive)
        document = payload["document"]
        document["world"] = "作者手动修订的世界规则，保留用户控制权。"
        document["characters"][0]["core"] = "作者手动修订的性格底线。"
        document["characters"][0]["knowledge"]["救命者"] = "已经知道是B"
        document["characters"][0]["state"]["腿伤"] = "作者确认已康复"
        document["worldbook"]["relations"][0]["value"] = "合作"
        payload["title"] = "新的故事名"
        plugin.scenes["webui-test"] = {"active": ["r1"]}
        plugin.context.llm_generate.reset_mock()
        with bound_request(payload):
            response = await plugin.webui.save_archive(archive_id)
        assert response.status_code == 200
        assert decoded(response)["revision"] == 1
        saved = plugin.store.load("webui-test")
        assert saved["world"] == document["world"]
        assert saved["characters"]["r1"]["core"] == document["characters"][0]["core"]
        assert saved["characters"]["r1"]["knowledge"]["救命者"] == "已经知道是B"
        assert saved["worldbook"]["relations"][0]["value"] == "合作"
        assert saved["label"]["title"] == "新的故事名"
        assert "webui-test" not in plugin.scenes
        plugin.context.llm_generate.assert_not_called()

    asyncio.run(run())


def test_save_conflict_keeps_both_new_archive_and_unsaved_client_data(plugin, archive):
    async def run():
        archive_id, payload = editable(plugin, archive)
        archive["revision"] += 1
        plugin.store.save("webui-test", archive)
        payload["document"]["world"] = "过期页面的修改"
        before = plugin.store.path("webui-test").read_bytes()
        with bound_request(payload):
            response = await plugin.webui.save_archive(archive_id)
        assert response.status_code == 409
        assert plugin.store.path("webui-test").read_bytes() == before
        assert payload["document"]["world"] == "过期页面的修改"

    asyncio.run(run())


@pytest.mark.parametrize(
    "invalid",
    [
        "unknown-role",
        "bad-faction",
        "blank-name",
        "duplicate-name",
        "changed-id",
        "bad-known",
    ],
)
def test_invalid_author_edits_cannot_damage_archive(plugin, archive, invalid):
    async def run():
        archive_id, payload = editable(plugin, archive)
        document = payload["document"]
        if invalid == "unknown-role":
            document["characters"].append(
                {**document["characters"][0], "id": "r3", "name": "C"}
            )
        elif invalid == "bad-faction":
            document["worldbook"]["relations"][0]["source"] = "不存在的势力"
        elif invalid == "blank-name":
            document["characters"][0]["name"] = ""
        elif invalid == "duplicate-name":
            document["characters"][0]["name"] = document["characters"][1]["name"]
        elif invalid == "changed-id":
            document["characters"][0]["id"] = "../../escape"
        else:
            document["characters"][0]["memories"][0]["known"] = "true"
        before = plugin.store.path("webui-test").read_bytes()
        with bound_request(payload):
            response = await plugin.webui.save_archive(archive_id)
        assert response.status_code == 400
        assert plugin.store.path("webui-test").read_bytes() == before

    asyncio.run(run())


@pytest.mark.parametrize("archive_id", ["../escape", "x" * 64, "a/b", "", "A" * 64])
def test_archive_identifier_is_not_a_filesystem_path(plugin, archive_id):
    async def run():
        with bound_request(
            {"revision": 0, "title": "test", "document": copy.deepcopy(SEED)}
        ):
            assert (await plugin.webui.get_archive(archive_id)).status_code == 400
            assert (await plugin.webui.save_archive(archive_id)).status_code == 400

    asyncio.run(run())


def test_symlinked_archive_is_rejected(plugin, tmp_path, archive):
    outside = tmp_path / "outside.txt"
    outside.write_text("private")
    linked = plugin.store.directory / ("a" * 64 + ".json")
    linked.symlink_to(outside)
    with pytest.raises(ValueError):
        plugin.store.load_id("a" * 64)
    assert outside.read_text() == "private"


def test_web_save_and_compaction_share_the_same_lock(plugin, archive):
    async def run():
        archive_id, payload = editable(plugin, archive)
        lock = plugin.locks.setdefault(archive_id, asyncio.Lock())
        await lock.acquire()

        async def save():
            with bound_request(payload):
                return await plugin.webui.save_archive(archive_id)

        task = asyncio.create_task(save())
        await asyncio.sleep(0)
        assert not task.done()
        archive["revision"] = 1
        plugin.store.save("webui-test", archive)
        lock.release()
        assert (await task).status_code == 409

    asyncio.run(run())


def test_page_metadata_is_accepted_and_assets_exist():
    from astrbot.core.star.star_manager import PluginManager

    root = Path(__file__).parents[1]
    metadata = PluginManager._load_plugin_metadata(str(root))
    view = metadata.views[0]
    name = view["name"] if isinstance(view, dict) else view.name
    assert name == "archives"
    assert all(
        (root / "views" / "archives" / name).is_file()
        for name in ("index.html", "app.js", "style.css")
    )


def test_unload_removes_only_own_routes(plugin):
    registered = plugin.context.registered_web_apis
    registered[:] = plugin.webui.routes + [
        ("/another/items", object(), ["GET"], "another")
    ]
    plugin.webui.close()
    assert len(registered) == 1 and registered[0][0] == "/another/items"


def test_first_chat_populates_ui_automatically_without_manual_setup(plugin):
    from astrbot_plugin_living_characters.prompts import IMPORT
    from conftest import Event, request

    async def run():
        with bound_request():
            assert decoded(await plugin.webui.list_archives())["archives"] == []
        event = Event()
        await plugin.prepare_scene(event, request())
        with bound_request():
            listing = decoded(await plugin.webui.list_archives())["archives"]
            assert len(listing) == 1
            populated = decoded(await plugin.webui.get_archive(listing[0]["id"]))
        assert (
            populated["document"]["worldbook"]["factions"]
            == SEED["worldbook"]["factions"]
        )
        assert [r["name"] for r in populated["document"]["characters"]] == ["A", "B"]
        assert populated["title"] == "inherited-story"
        first_imports = sum(
            call.kwargs["system_prompt"] == IMPORT
            for call in plugin.context.llm_generate.call_args_list
        )
        assert first_imports == 1
        await plugin.finish_run(event, None, None)
        await plugin.prepare_scene(Event(), request("继续"))
        assert (
            sum(
                call.kwargs["system_prompt"] == IMPORT
                for call in plugin.context.llm_generate.call_args_list
            )
            == 1
        )

    asyncio.run(run())


def test_saved_edits_are_used_by_next_chat_without_reimport(plugin):
    from astrbot_plugin_living_characters.prompts import IMPORT
    from conftest import Event, request

    async def run():
        event = Event()
        await plugin.prepare_scene(event, request())
        archive_id = plugin.store.path(
            event.get_extra("living_characters_turn")["scope"]
        ).stem
        with bound_request():
            loaded = decoded(await plugin.webui.get_archive(archive_id))
        payload = {key: loaded[key] for key in ("revision", "title", "document")}
        payload["document"]["characters"][0]["core"] = "作者确认的新角色核心"
        payload["document"]["worldbook"]["facts"]["时代"] = "新纪元"
        with bound_request(payload):
            assert (await plugin.webui.save_archive(archive_id)).status_code == 200
        plugin.context.llm_generate.reset_mock()
        next_req = request()
        await plugin.prepare_scene(Event(), next_req)
        assert "作者确认的新角色核心" in next_req.system_prompt
        assert "新纪元" in next_req.system_prompt
        assert not any(
            c.kwargs["system_prompt"] == IMPORT
            for c in plugin.context.llm_generate.call_args_list
        )

    asyncio.run(run())
