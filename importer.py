"""Lossless rule-first import for explicitly marked persona documents."""

import json
import re

from .engine import new_archive, parse_json

LABELS = {
    "角色名": "name",
    "人物名称": "name",
    "姓名": "name",
    "角色设定": "base",
    "人物设定": "base",
    "核心设定": "core",
    "控制方式": "controller",
    "当前状态": "state",
    "角色认知": "knowledge",
    "性格成长": "growth",
    "重要经历": "memories",
    "世界背景": "world_base",
    "世界规则": "world",
    "演绎规则": "world",
    "全局规则": "world",
    "世界书": "worldbook",
}


def parse_marked_persona(text):
    """Import complete marked records without interpreting prose or dropping it.

    Args:
        text: Effective author persona text.

    Returns:
        Validated archive, or None when a complete reliable parse is impossible.
    """
    if not isinstance(text, str) or not text.strip() or len(text) > 500_000:
        return None
    stripped = text.strip()
    if stripped.startswith(("{", "```json\n", "```\n")):
        try:
            return new_archive(parse_json(stripped))
        except (ValueError, KeyError, TypeError):
            return None
    markers = list(re.finditer(r"【([^【】\r\n]+)】[ \t]*[：:]?[ \t]*", text))
    if not markers:
        return None
    document = {
        "world": "",
        "world_base": text[: markers[0].start()].strip(),
        "worldbook": {"facts": {}, "factions": {}, "relations": [], "events": []},
        "characters": [],
    }
    current = None
    explicit_controllers = set()
    seen_fields = set()
    try:
        for index, marker in enumerate(markers):
            label = LABELS.get(marker.group(1).strip())
            value = text[
                marker.end() : markers[index + 1].start()
                if index + 1 < len(markers)
                else len(text)
            ].strip()
            if label is None or not value:
                return None
            if label == "name":
                if current is not None and not (current["core"] or current.get("base")):
                    return None
                current = {
                    "id": f"r{len(document['characters']) + 1}",
                    "name": value,
                    "controller": "ai",
                    "core": "",
                    "state": {},
                    "knowledge": {},
                    "growth": {},
                    "memories": [],
                }
                if "\n" in value or "\r" in value:
                    return None
                document["characters"].append(current)
            elif label in ("world", "world_base"):
                document[label] = "\n\n".join(
                    part for part in (document[label], value) if part
                )
            elif label == "worldbook":
                if document["worldbook"] != {
                    "facts": {},
                    "factions": {},
                    "relations": [],
                    "events": [],
                }:
                    return None
                document["worldbook"] = json.loads(value)
            else:
                if current is None:
                    return None
                identity = (current["id"], label)
                if identity in seen_fields:
                    return None
                seen_fields.add(identity)
                if label in ("base", "core"):
                    if current.get(label):
                        return None
                    current[label] = value
                elif label == "controller":
                    controllers = {
                        "ai": "ai",
                        "AI": "ai",
                        "AI演绎": "ai",
                        "AI 演绎": "ai",
                        "user": "user",
                        "用户": "user",
                        "用户控制": "user",
                        "玩家控制": "user",
                    }
                    if (
                        value not in controllers
                        or current["id"] in explicit_controllers
                    ):
                        return None
                    current[label] = controllers[value]
                    explicit_controllers.add(current["id"])
                elif label == "memories":
                    if current[label]:
                        return None
                    current[label] = json.loads(value)
                else:
                    if current[label]:
                        return None
                    if value.startswith("{"):
                        current[label] = json.loads(value)
                    else:
                        facts = {}
                        for line in value.splitlines():
                            match = re.fullmatch(
                                r"\s*([^：:]+?)\s*[：:]\s*(.+?)\s*", line
                            )
                            if not match or match[1] in facts:
                                return None
                            facts[match[1]] = match[2]
                        current[label] = facts
        if not current or not (current["core"] or current.get("base")):
            return None
        # Never guess player ownership from unstructured prose.
        if re.search(
            r"(?:用户|玩家).{0,15}(?:扮演|控制|独占)|(?:由用户|由玩家)|(?:AI|模型).{0,8}不得代写",
            text,
        ) and len(explicit_controllers) != len(document["characters"]):
            return None
        if not document["world_base"]:
            document.pop("world_base")
        return new_archive(document)
    except (ValueError, KeyError, TypeError):
        return None
