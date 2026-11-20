"""Rule-first import must be complete, lossless, and conservative."""

import asyncio
import json

import pytest
from astrbot_plugin_living_characters.engine import (
    apply_candidates,
    render,
    validate_candidates,
)
from astrbot_plugin_living_characters.importer import parse_marked_persona
from astrbot_plugin_living_characters.prompts import IMPORT
from conftest import SEED, Event, request

SIMPLE = """【世界背景】
山中医馆，旧历元年。
【世界规则】
秘密不能自动共享。
【角色名】A
【角色设定】谨慎的剑客。腿伤未愈，正在医馆休养。
【当前状态】
腿伤：未愈
位置：医馆
【角色名】B
【角色设定】温和的医师，外出出诊。
"""


def test_named_settings_are_parsed_verbatim_without_semantic_guessing():
    archive = parse_marked_persona(SIMPLE)
    assert archive is not None
    assert archive["world"] == "秘密不能自动共享。"
    assert archive["world_base"] == "山中医馆，旧历元年。"
    assert archive["characters"]["r1"]["base"] == "谨慎的剑客。腿伤未愈，正在医馆休养。"
    assert archive["characters"]["r1"]["core"] == ""
    assert archive["characters"]["r1"]["state"] == {"腿伤": "未愈", "位置": "医馆"}
    assert archive["characters"]["r2"]["knowledge"] == {}


def test_inline_markers_and_colons_are_supported():
    archive = parse_marked_persona(
        "【角色名】：A【角色设定】：谨慎的剑客【角色名】B【角色设定】温和的医师"
    )
    assert [r["name"] for r in archive["characters"].values()] == ["A", "B"]
    assert archive["characters"]["r2"]["base"] == "温和的医师"


def test_structured_json_is_imported_directly():
    archive = parse_marked_persona(json.dumps(SEED, ensure_ascii=False))
    assert archive["characters"]["r1"]["core"] == SEED["characters"][0]["core"]
    assert archive["worldbook"] == SEED["worldbook"]
    fenced = "```json\n" + json.dumps(SEED, ensure_ascii=False) + "\n```"
    assert parse_marked_persona(fenced)["worldbook"] == SEED["worldbook"]


def test_explicit_player_controller_and_fixed_core_are_preserved():
    archive = parse_marked_persona(
        "【世界规则】用户独占A的控制权。【角色名】A【控制方式】用户控制【角色设定】用户的角色。【角色名】B【控制方式】AI演绎【核心设定】温和医师。"
    )
    assert archive["characters"]["r1"]["controller"] == "user"
    assert archive["characters"]["r2"]["core"] == "温和医师。"


def test_explicit_worldbook_and_knowledge_are_parsed_without_sharing_secrets():
    source = (
        "【世界背景】医馆【世界书】"
        + json.dumps(SEED["worldbook"], ensure_ascii=False)
        + '\n【角色名】A【角色设定】剑客【角色认知】救命者：不知道是谁\n【重要经历】[{"text":"曾在山路受伤","known":true}]\n【角色名】B【角色设定】医师【角色认知】救命者：知道是自己'
    )
    archive = parse_marked_persona(source)
    assert archive["worldbook"]["relations"][0]["source"] == "北盟"
    assert archive["characters"]["r1"]["knowledge"]["救命者"] == "不知道是谁"
    assert archive["characters"]["r2"]["knowledge"]["救命者"] == "知道是自己"
    assert "知道是自己" not in render(archive, active=["r1"])


@pytest.mark.parametrize(
    "text",
    [
        "普通自然语言：A是剑客，B是医师。",
        "【角色名】A",
        "【角色设定】剑客【角色名】A",
        "【角色名】A【角色设定】剑客【未支持字段】不能丢失的内容",
        "【角色名】A【角色设定】剑客【角色名】A【角色设定】医师",
        "【角色名】A【角色设定】剑客【当前状态】受伤了，但是并没有键值结构",
        "【角色名】A【角色设定】剑客【重要经历】他曾经受伤",
        "【角色名】A【角色设定】剑客【控制方式】不确定谁来控制",
        "【世界规则】A由用户控制。【角色名】A【角色设定】剑客",
        "【角色名】A【角色设定】剑客【当前状态】{}【当前状态】腿伤：受伤",
        "【角色名】A【角色设定】剑客【当前状态】腿伤：未愈\n腿伤：已愈",
        "【角色名】A【角色设定】剑客【角色设定】重复资料",
    ],
)
def test_partial_ambiguous_or_unknown_formats_fall_back_instead_of_dropping_text(text):
    assert parse_marked_persona(text) is None


def test_new_state_can_supersede_unclassified_initial_prose():
    archive = parse_marked_persona(SIMPLE)
    proposals = {
        "candidates": [
            {
                "id": "r1",
                "kind": "state",
                "key": "腿伤",
                "value": "已痊愈",
                "evidence": "A的腿伤已经痊愈",
            }
        ],
        "shared_summary": "医馆剧情继续。",
    }
    candidates, _ = validate_candidates(archive, proposals, "A的腿伤已经痊愈")
    updated = apply_candidates(archive, candidates, {0})
    assert updated["characters"]["r1"]["state"]["腿伤"] == "已痊愈"
    assert updated["characters"]["r1"]["base"] == archive["characters"]["r1"]["base"]
    assert '"base"' in render(updated, active=["r1"])
    assert '"core": ""' in render(updated, active=["r1"])


@pytest.mark.parametrize(
    "source,expected_imports",
    [(SIMPLE, 0), ("【角色名】A【角色设定】剑客【未知字段】保留这段", 1)],
)
def test_first_chat_uses_rules_first_and_llm_only_on_import_failure(
    plugin, source, expected_imports
):
    async def run():
        plugin.context.persona_manager.get_persona_v3_by_id = lambda _: {
            "prompt": source
        }
        req = request()
        req.system_prompt = (
            "SAFETY\n<persona_instructions>\n"
            + source
            + "\n</persona_instructions>\nTOOLS"
        )
        event = Event()
        await plugin.prepare_scene(event, req)
        assert event.get_extra("living_characters_turn") is not None
        imports = [
            c
            for c in plugin.context.llm_generate.call_args_list
            if c.kwargs["system_prompt"] == IMPORT
        ]
        assert len(imports) == expected_imports
        assert req.system_prompt.startswith("SAFETY") and req.system_prompt.endswith(
            "TOOLS"
        )
        if expected_imports:
            assert json.loads(imports[0].kwargs["prompt"])["persona"] == source

    asyncio.run(run())
