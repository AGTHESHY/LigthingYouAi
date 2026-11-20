"""Atomic state, cognition, macro politics, and archive safety regressions."""

import copy
import json

import pytest
from astrbot_plugin_living_characters.engine import (
    ArchiveStore,
    apply_candidates,
    new_archive,
    parse_json,
    render,
    validate_candidates,
)
from conftest import PROPOSALS, SEED


def test_recovery_preserves_old_injury_and_unrelated_state(archive):
    candidates, _ = validate_candidates(archive, PROPOSALS, "A的腿伤已经痊愈")
    updated = apply_candidates(archive, candidates, {0, 1})
    assert updated["characters"]["r1"]["state"] == {"腿伤": "已痊愈", "位置": "医馆"}
    assert updated["characters"]["r1"]["memories"][0]["text"] == "曾在山路受伤"
    assert archive["characters"]["r1"]["state"]["腿伤"] == "未愈"
    assert "温和医师" not in render(updated, active=["r1"])


@pytest.mark.parametrize(
    "change",
    [
        {"id": "r1", "kind": "core", "value": "洗白", "evidence": "剧情"},
        {
            "id": "invented",
            "kind": "state",
            "key": "伤势",
            "value": "已愈",
            "evidence": "剧情",
        },
        {
            "id": "r1",
            "kind": "state",
            "key": "伤势",
            "value": "已愈",
            "evidence": "未发生的事件",
        },
        {
            "id": "r1",
            "kind": "memory",
            "value": "秘密",
            "known": "true",
            "evidence": "剧情",
        },
        {
            "kind": "faction_relation",
            "source": "A",
            "target": "B",
            "value": "朋友",
            "evidence": "剧情",
        },
    ],
)
def test_invalid_mutations_rejected_without_writing(archive, change):
    original = copy.deepcopy(archive)
    with pytest.raises(ValueError):
        validate_candidates(
            archive, {"candidates": [change], "shared_summary": "摘要"}, "剧情"
        )
    assert archive == original


def test_macro_factions_do_not_replace_personal_relations(archive):
    changes = [
        {
            "kind": "faction_relation",
            "source": "北盟",
            "target": "南盟",
            "value": "结盟",
            "evidence": "两盟签订盟约",
        },
        {
            "id": "r1",
            "kind": "state",
            "key": "对B关系",
            "value": "仍不信任",
            "evidence": "A仍不信任B",
        },
        {
            "kind": "world_fact",
            "key": "时代",
            "value": "旧历二年",
            "evidence": "进入旧历二年",
        },
        {"kind": "world_event", "value": "南北两盟签约", "evidence": "两盟签订盟约"},
    ]
    validated, _ = validate_candidates(
        archive,
        {"candidates": changes, "shared_summary": "两盟已签约"},
        "两盟签订盟约，A仍不信任B，进入旧历二年",
    )
    updated = apply_candidates(archive, validated, set(range(4)))
    assert updated["worldbook"]["relations"][0]["value"] == "结盟"
    assert updated["characters"]["r1"]["state"]["对B关系"] == "仍不信任"
    assert updated["worldbook"]["facts"]["时代"] == "旧历二年"
    assert updated["world"] == archive["world"]


def test_false_belief_is_separate_from_objective_state_and_secrets(archive):
    archive["characters"]["r1"]["knowledge"]["失踪者"] = "误以为已死亡"
    archive["characters"]["r2"]["knowledge"]["失踪者"] = "知道仍被秘密囚禁"
    archive["characters"]["r2"]["state"]["囚笼位置"] = "隐藏之地"
    prompt = render(archive, active=["r1"])
    assert "误以为已死亡" in prompt
    assert "隐藏之地" not in prompt
    assert "知道仍被秘密囚禁" not in prompt


def test_worldbook_and_role_memory_bounded_retrieval(archive):
    archive["characters"]["r1"]["memories"] += [
        {"text": f"普通事件{i}", "known": True} for i in range(40)
    ]
    prompt = render(archive, active=["r1"], query="山路受伤")
    assert "曾在山路受伤" in prompt
    assert prompt.count('"known":') <= 12
    assert len(archive["characters"]["r1"]["memories"]) == 41


def test_referenced_role_is_marked_absent(archive):
    prompt = render(archive, active=["r1"], referenced=["r2"])
    data = parse_json(prompt[prompt.index("{") :])
    assert "r2" not in data["characters"] and "r2" in data["referenced"]


def test_player_control_metadata_is_retained():
    seed = copy.deepcopy(SEED)
    seed["characters"][0]["controller"] = "user"
    assert new_archive(seed)["characters"]["r1"]["controller"] == "user"


def test_archive_restart_scope_and_corruption(tmp_path, archive):
    store = ArchiveStore(tmp_path)
    store.save("group1:story1", archive)
    assert store.load("group1:story1") == archive
    assert store.load("group2:story1") is None
    assert store.path("../../escape").parent == tmp_path
    assert store.path("group1:story1").stat().st_mode & 0o777 == 0o600
    store.path("broken").write_text("broken")
    with pytest.raises(ValueError):
        store.load("broken")
    assert store.path("broken").read_text() == "broken"


def test_legacy_archives_migrate_without_deleting_memories(tmp_path):
    store = ArchiveStore(tmp_path)
    old = {
        "version": 1,
        "world": "规则",
        "characters": {
            "r1": {
                "name": "A",
                "core": "设定",
                "state": "受伤",
                "growth": "",
                "memories": [{"text": "往事", "known": True}],
            }
        },
        "active": ["r1"],
        "revision": 3,
    }
    store.path("old").write_text(json.dumps(old))
    migrated = store.load("old")
    assert migrated["version"] == 2
    assert migrated["characters"]["r1"]["state"] == {"既有记录": "受伤"}
    assert (
        migrated["characters"]["r1"]["memories"] == old["characters"]["r1"]["memories"]
    )
    assert migrated["worldbook"]["relations"] == []


def test_json_rejects_prose():
    assert parse_json('```json\n{"ok":true}\n```') == {"ok": True}
    with pytest.raises(ValueError):
        parse_json('Explanation {"ok":true}')


def test_faction_relationship_cannot_hide_in_generic_world_fact(archive):
    bad = {
        "kind": "world_fact",
        "key": "北盟与南盟关系",
        "value": "结盟",
        "evidence": "两盟结盟",
    }
    with pytest.raises(ValueError, match="faction_relation"):
        validate_candidates(
            archive, {"candidates": [bad], "shared_summary": "两盟结盟"}, "两盟结盟"
        )
