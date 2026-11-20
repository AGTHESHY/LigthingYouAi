"""Regression cases for story continuity, evidence, and persistence."""

import copy
import json

import pytest
from astrbot_plugin_living_characters.engine import (
    ArchiveStore,
    apply_update,
    new_archive,
    parse_json,
    render,
)


@pytest.fixture
def archive():
    return new_archive(
        {
            "world": "山中医馆",
            "characters": [
                {
                    "id": "r1",
                    "name": "A",
                    "core": "谨慎的剑客",
                    "state": "腿伤未愈，身在医馆",
                    "growth": "",
                    "memories": [{"text": "曾在山路受伤", "known": True}],
                },
                {
                    "id": "r2",
                    "name": "B",
                    "core": "温和的医师",
                    "state": "正在出诊",
                    "growth": "",
                    "memories": [{"text": "暗中救过A，A尚不知情", "known": True}],
                },
            ],
        }
    )


def test_healed_state_keeps_injury_memory_and_excludes_absent_role(archive):
    result = apply_update(
        archive,
        {
            "active": ["r1"],
            "changes": [
                {
                    "id": "r1",
                    "state": {
                        "value": "腿伤痊愈，身在医馆",
                        "evidence": "A的腿伤已经痊愈",
                    },
                    "memories": [
                        {
                            "text": "腿伤经治疗痊愈",
                            "known": True,
                            "evidence": "A的腿伤已经痊愈",
                        }
                    ],
                }
            ],
        },
        "A的腿伤已经痊愈",
    )
    assert result["characters"]["r1"]["state"] == "腿伤痊愈，身在医馆"
    assert result["characters"]["r1"]["memories"][0]["text"] == "曾在山路受伤"
    assert "温和的医师" not in render(result)
    assert "腿伤未愈" not in render(result)
    assert archive["characters"]["r1"]["state"] == "腿伤未愈，身在医馆"


@pytest.mark.parametrize(
    "bad",
    [
        {"active": ["invented"], "changes": []},
        {"active": ["r1"], "changes": [{"id": "r1", "core": "另一身份"}]},
        {
            "active": ["r1"],
            "changes": [
                {"id": "r1", "state": {"value": "痊愈", "evidence": "编造的事件"}}
            ],
        },
        {
            "active": ["r1"],
            "changes": [
                {
                    "id": "r1",
                    "memories": [{"text": "秘密", "known": "true", "evidence": "剧情"}],
                }
            ],
        },
        {"active": ["r1"], "changes": [], "recall": {"r1": [99]}},
    ],
)
def test_invalid_changes_are_atomic(archive, bad):
    original = copy.deepcopy(archive)
    with pytest.raises(ValueError):
        apply_update(archive, bad, "剧情")
    assert archive == original


def test_duplicate_memory_upgrades_knowledge_without_duplicate(archive):
    archive["characters"]["r1"]["memories"] = [{"text": "B暗中救了A", "known": False}]
    change = {
        "active": ["r1"],
        "changes": [
            {
                "id": "r1",
                "memories": [
                    {"text": "B暗中救了A", "known": True, "evidence": "B向A承认救过他"}
                ],
            }
        ],
    }
    result = apply_update(archive, change, "B向A承认救过他")
    assert result["characters"]["r1"]["memories"] == [
        {"text": "B暗中救了A", "known": True}
    ]


def test_selected_old_memory_survives_bounded_injection(archive):
    archive["active"] = ["r1"]
    archive["characters"]["r1"]["memories"] += [
        {"text": f"普通事件{i}", "known": True} for i in range(30)
    ]
    archive["recall"] = {"r1": [0, 30]}
    prompt = render(archive)
    assert "曾在山路受伤" in prompt
    assert "普通事件29" in prompt
    assert "普通事件1" not in prompt
    assert len(archive["characters"]["r1"]["memories"]) == 31


def test_restart_isolation_and_pending_retry_data(tmp_path, archive):
    store = ArchiveStore(tmp_path)
    archive["pending"] = [{"role": "assistant", "content": "A的腿伤已经痊愈"}]
    store.save("group1:story1", archive)
    restarted = ArchiveStore(tmp_path)
    assert restarted.load("group1:story1")["pending"] == archive["pending"]
    assert restarted.load("group2:story1") is None
    assert restarted.load("group1:story2") is None
    assert store.path("../../escape").parent == tmp_path
    assert not list(tmp_path.glob("*.tmp"))
    assert store.path("group1:story1").stat().st_mode & 0o777 == 0o600


def test_corruption_does_not_overwrite_archive(tmp_path):
    store = ArchiveStore(tmp_path)
    path = store.path("story")
    path.write_text("broken", encoding="utf-8")
    with pytest.raises(ValueError):
        store.load("story")
    assert path.read_text() == "broken"


def test_json_rejects_prose_and_accepts_single_fence():
    assert parse_json('```json\n{"ok":true}\n```') == {"ok": True}
    with pytest.raises((ValueError, json.JSONDecodeError)):
        parse_json('I think {"ok":true}')
