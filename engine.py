"""Validated character archives independent of AstrBot's runtime."""

import copy
import hashlib
import json
from pathlib import Path

from .prompts import INJECTION


def parse_json(text: str) -> dict:
    """Parse one bounded JSON object, allowing a single Markdown fence.

    Args:
        text: Model completion without reasoning content.

    Returns:
        Decoded JSON object.

    Raises:
        ValueError: The completion is oversized or is not a JSON object.
    """
    if len(text) > 500_000:
        raise ValueError("Model output exceeds archive limits")
    text = text.strip()
    if text.startswith("```json\n") and text.endswith("```"):
        text = text[8:-3].strip()
    elif text.startswith("```\n") and text.endswith("```"):
        text = text[4:-3].strip()
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("Expected an object")
    return data


def checked_text(value, limit: int = 8000, allow_empty: bool = True) -> str:
    """Validate text without silently truncating authored facts.

    Args:
        value: Candidate text.
        limit: Maximum character count.
        allow_empty: Whether blank text is valid.

    Returns:
        Original text.

    Raises:
        ValueError: The value violates the text contract.
    """
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError("Invalid text field")
    if not allow_empty and not value.strip():
        raise ValueError("Empty required text")
    return value


def new_archive(data: dict) -> dict:
    """Validate a complete initial decomposition before replacing any prompt.

    Args:
        data: Import model result.

    Returns:
        Independent validated archive.

    Raises:
        ValueError: Required fields or character IDs are invalid.
    """
    world = checked_text(data.get("world"), 24000)
    characters = data.get("characters")
    if not isinstance(characters, list) or not 1 <= len(characters) <= 100:
        raise ValueError("Persona must contain between 1 and 100 characters")
    result = {"world": world, "characters": {}, "active": [], "revision": 0}
    names = set()
    for index, item in enumerate(characters, 1):
        if not isinstance(item, dict) or item.get("id") != f"r{index}":
            raise ValueError("Character IDs must be sequential and unique")
        name = checked_text(item.get("name"), 100, False)
        if name in names:
            raise ValueError("Duplicate character name")
        names.add(name)
        role = {
            "name": name,
            "core": checked_text(item.get("core"), allow_empty=False),
            "state": checked_text(item.get("state")),
            "growth": checked_text(item.get("growth")),
            "memories": [],
        }
        memories = item.get("memories")
        if not isinstance(memories, list) or len(memories) > 300:
            raise ValueError("Invalid initial memories")
        for memory in memories:
            if not isinstance(memory, dict) or type(memory.get("known")) is not bool:
                raise ValueError("Memory knowledge must be explicit")
            role["memories"].append(
                {
                    "text": checked_text(memory.get("text"), 2000, False),
                    "known": memory["known"],
                }
            )
        result["characters"][item["id"]] = role
    return result


def apply_update(archive: dict, data: dict, evidence_source: str) -> dict:
    """Apply only fully validated changes with verbatim story evidence.

    Args:
        archive: Previously validated archive.
        data: Analysis model output.
        evidence_source: Actual narrative supplied for this analysis.

    Returns:
        New archive; the input is never mutated.

    Raises:
        ValueError: Any change lacks evidence or violates the schema.
    """
    result = copy.deepcopy(archive)
    active = data.get("active")
    changes = data.get("changes")
    if (
        not isinstance(active, list)
        or not all(
            isinstance(key, str) and key in result["characters"] for key in active
        )
        or len(active) != len(set(active))
    ):
        raise ValueError("Invalid active character IDs")
    if not isinstance(changes, list):
        raise ValueError("Invalid character changes")
    seen = set()
    for change in changes:
        if not isinstance(change, dict) or set(change) - {
            "id",
            "state",
            "growth",
            "memories",
        }:
            raise ValueError("Unsupported character mutation")
        key = change.get("id")
        if not isinstance(key, str) or key not in result["characters"] or key in seen:
            raise ValueError("Invalid or duplicate changed character")
        seen.add(key)
        role = result["characters"][key]
        for field in ("state", "growth"):
            if field in change:
                patch = change[field]
                if not isinstance(patch, dict) or set(patch) != {"value", "evidence"}:
                    raise ValueError("Invalid state patch")
                quote = checked_text(patch["evidence"], 2000, False)
                if quote not in evidence_source:
                    raise ValueError("State evidence not found in narrative")
                role[field] = checked_text(patch["value"])
        memories = change.get("memories", [])
        if not isinstance(memories, list) or len(memories) > 30:
            raise ValueError("Invalid memory additions")
        for memory in memories:
            if not isinstance(memory, dict) or set(memory) != {
                "text",
                "known",
                "evidence",
            }:
                raise ValueError("Invalid memory patch")
            quote = checked_text(memory["evidence"], 2000, False)
            if quote not in evidence_source or type(memory["known"]) is not bool:
                raise ValueError("Invalid memory evidence or knowledge")
            text = checked_text(memory["text"], 2000, False)
            # Knowledge may become confirmed, but must never regress to unknown.
            previous = next((m for m in role["memories"] if m["text"] == text), None)
            if previous:
                previous["known"] = previous["known"] or memory["known"]
            else:
                role["memories"].append({"text": text, "known": memory["known"]})
    result["active"] = active
    recall = data.get("recall", {})
    if not isinstance(recall, dict) or set(recall) - set(result["characters"]):
        raise ValueError("Invalid memory recall roles")
    for key, indices in recall.items():
        if (
            not isinstance(indices, list)
            or len(indices) > 12
            or any(
                type(index) is not int
                or not 0 <= index < len(result["characters"][key]["memories"])
                for index in indices
            )
        ):
            raise ValueError("Invalid memory recall indices")
    result["recall"] = recall
    return result


def render(archive: dict) -> str:
    """Render the public setting and only active character dossiers.

    Args:
        archive: Validated scene archive.

    Returns:
        System prompt replacement.
    """
    scene = {"world": archive["world"], "characters": {}}
    for key in archive["active"]:
        role = copy.deepcopy(archive["characters"][key])
        memories = role["memories"]
        indices = archive.get("recall", {}).get(
            key, list(range(max(0, len(memories) - 8), len(memories)))
        )
        role["memories"] = [memories[index] for index in indices]
        scene["characters"][key] = role
    return INJECTION + json.dumps(scene, ensure_ascii=False)


class ArchiveStore:
    """Keep archives outside the plugin directory using atomic replacement."""

    def __init__(self, directory: Path):
        """Bind a plugin-owned data directory.

        Args:
            directory: Persistent AstrBot plugin data path.
        """
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)

    def path(self, scope: str) -> Path:
        """Resolve an opaque filename without exposing session identifiers.

        Args:
            scope: Session, conversation, and persona source identity.

        Returns:
            JSON archive path.
        """
        return self.directory / (hashlib.sha256(scope.encode()).hexdigest() + ".json")

    def load(self, scope: str) -> dict | None:
        """Read an archive without treating corruption as an empty archive.

        Args:
            scope: Archive identity.

        Returns:
            Stored archive, or None when it has never been created.

        Raises:
            ValueError: Archive format is invalid.
            OSError: Reading fails for a reason other than a missing file.
        """
        path = self.path(scope)
        if not path.exists():
            return None
        data = parse_json(path.read_text(encoding="utf-8"))
        if data.get("version") != 1 or not isinstance(data.get("characters"), dict):
            raise ValueError("Unsupported archive format")
        return data

    def save(self, scope: str, archive: dict) -> None:
        """Atomically persist a whole archive after validation.

        Args:
            scope: Archive identity.
            archive: Validated character archive.
        """
        path = self.path(scope)
        temporary = path.with_suffix(".tmp")
        try:
            temporary.write_text(
                json.dumps({**archive, "version": 1}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.chmod(0o600)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
