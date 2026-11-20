# ruff: noqa: E402
"""Bind this checkout as a package and provide real-SDK test fixtures."""

import copy
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

PACKAGE = "astrbot_plugin_living_characters"
root = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location(
    PACKAGE, root / "__init__.py", submodule_search_locations=[str(root)]
)
module = importlib.util.module_from_spec(spec)
sys.modules[PACKAGE] = module
spec.loader.exec_module(module)

from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot_plugin_living_characters.engine import new_archive
from astrbot_plugin_living_characters.main import LivingCharacters
from astrbot_plugin_living_characters.prompts import COMPACT, DECISIONS, IMPORT

SEED = {
    "world": "用户独占旅人的语言、行动和心理。A的性格底线不可被一次道歉改变。秘密不共享。",
    "worldbook": {
        "facts": {"时代": "旧历元年"},
        "factions": {"北盟": "控制北方", "南盟": "控制南方"},
        "relations": [{"source": "北盟", "target": "南盟", "value": "敌对"}],
        "events": [],
    },
    "characters": [
        {
            "id": "r1",
            "name": "A",
            "controller": "ai",
            "core": "谨慎剑客，永久失去嗅觉且无法恢复；腿伤是可治疗的临时伤。",
            "state": {"腿伤": "未愈", "位置": "医馆"},
            "growth": {},
            "knowledge": {"救命者": "不知道是谁"},
            "memories": [{"text": "曾在山路受伤", "known": True}],
        },
        {
            "id": "r2",
            "name": "B",
            "controller": "ai",
            "core": "温和医师。",
            "state": {"位置": "外出出诊"},
            "growth": {},
            "knowledge": {"救命者": "知道自己曾暗中救A"},
            "memories": [{"text": "曾暗中救A，A不知情", "known": True}],
        },
    ],
}

PROPOSALS = {
    "candidates": [
        {
            "id": "r1",
            "kind": "state",
            "key": "腿伤",
            "value": "已痊愈",
            "evidence": "A的腿伤已经痊愈",
        },
        {
            "id": "r1",
            "kind": "memory",
            "value": "腿伤经过治疗后康复",
            "known": True,
            "evidence": "A的腿伤已经痊愈",
        },
    ],
    "shared_summary": "A留在医馆，双方约定明日出发。",
}


class Event:
    unified_msg_origin = "test:group:one"

    def __init__(self):
        self.extras = {"_trace_persona_id": "inherited-story"}

    def get_extra(self, key):
        return self.extras.get(key)

    def set_extra(self, key, value):
        self.extras[key] = value


def request(prompt="A独自在医馆", cid="story-one"):
    return ProviderRequest(
        prompt=prompt,
        contexts=[],
        model="session-model",
        conversation=SimpleNamespace(cid=cid),
        system_prompt="HOST SAFETY\n<persona_instructions>\n原始完整人设\n</persona_instructions>\nHOST TOOLS",
    )


@pytest.fixture
def archive():
    return new_archive(copy.deepcopy(SEED))


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "astrbot_plugin_living_characters.main.StarTools.get_data_dir",
        lambda *_: tmp_path,
    )
    ctx = SimpleNamespace(
        persona_manager=SimpleNamespace(
            get_persona_v3_by_id=lambda _: {"prompt": "原始完整人设"}
        ),
        get_current_chat_provider_id=AsyncMock(return_value="session-provider"),
        get_provider_by_id=lambda _: None,
        llm_generate=AsyncMock(),
    )
    p = LivingCharacters(ctx, {"decision_provider_id": ""})
    p.test_proposals = copy.deepcopy(PROPOSALS)

    async def generate(**kwargs):
        instruction = kwargs["system_prompt"]
        payload = json.loads(kwargs["prompt"])
        if instruction == IMPORT:
            data = copy.deepcopy(SEED)
        elif instruction == COMPACT:
            data = copy.deepcopy(p.test_proposals)
        elif instruction == DECISIONS:
            data = {
                "answers": {
                    key: (
                        key == "r1:present"
                        if key.endswith(":present")
                        else False
                        if key.endswith((":referenced", ":violates"))
                        else True
                    )
                    for key in payload["questions"]
                }
            }
        else:
            raise AssertionError("Unexpected instruction")
        return LLMResponse(
            role="assistant", completion_text=json.dumps(data, ensure_ascii=False)
        )

    ctx.llm_generate.side_effect = generate
    return p
