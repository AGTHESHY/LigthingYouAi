"""Validated atomic archives for characters and a growing macro worldbook."""

import copy
import hashlib
import json
import re
from pathlib import Path

from .prompts import INJECTION


def parse_json(text: str) -> dict:
    """Parse a bounded JSON object, permitting one surrounding code fence.

    Args:
        text: Model completion.

    Returns:
        Parsed object.

    Raises:
        ValueError: Invalid JSON or an oversized result.
    """
    if not isinstance(text, str) or len(text) > 500_000:
        raise ValueError("Invalid model output size")
    text = text.strip()
    if text.startswith("```json\n") and text.endswith("```"):
        text = text[8:-3].strip()
    elif text.startswith("```\n") and text.endswith("```"):
        text = text[4:-3].strip()
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("Expected JSON object")
    return data


def checked_text(value, limit=8000, allow_empty=True):
    """Check text without truncating authored facts.

    Args:
        value: Candidate text.
        limit: Maximum length.
        allow_empty: Whether blank text is allowed.

    Returns:
        Original text.

    Raises:
        ValueError: Text violates the contract.
    """
    if (
        not isinstance(value, str)
        or len(value) > limit
        or (not allow_empty and not value.strip())
    ):
        raise ValueError("Invalid text")
    return value


def text_map(value):
    """Validate a map of single named facts.

    Args:
        value: Model-provided object.

    Returns:
        Independent validated map.

    Raises:
        ValueError: A key or value is invalid.
    """
    if not isinstance(value, dict) or len(value) > 200:
        raise ValueError("Invalid fact map")
    return {
        checked_text(k, 100, False): checked_text(v, 4000, False)
        for k, v in value.items()
    }


def new_archive(data):
    """Separate immutable author rules, macro world state, and character facts.

    Args:
        data: Import JSON.

    Returns:
        Validated version-two archive.

    Raises:
        ValueError: Import fields are invalid.
    """
    if set(data) != {"world", "worldbook", "characters"}:
        raise ValueError("Invalid import fields")
    world = checked_text(data["world"], 32000, False)
    book = data["worldbook"]
    if not isinstance(book, dict) or set(book) != {
        "facts",
        "factions",
        "relations",
        "events",
    }:
        raise ValueError("Invalid worldbook")
    factions = text_map(book["factions"])
    relations = book["relations"]
    events = book["events"]
    if (
        not isinstance(relations, list)
        or not isinstance(events, list)
        or len(events) > 300
    ):
        raise ValueError("Invalid world history")
    seen_pairs = set()
    for rel in relations:
        if not isinstance(rel, dict) or set(rel) != {"source", "target", "value"}:
            raise ValueError("Invalid faction relation")
        if (
            rel["source"] not in factions
            or rel["target"] not in factions
            or rel["source"] == rel["target"]
        ):
            raise ValueError("Faction endpoints must be distinct factions")
        pair = (rel["source"], rel["target"])
        if pair in seen_pairs:
            raise ValueError("Duplicate faction relation")
        seen_pairs.add(pair)
        checked_text(rel["value"], 4000, False)
    result = {
        "version": 2,
        "world": world,
        "worldbook": {
            "facts": text_map(book["facts"]),
            "factions": factions,
            "relations": copy.deepcopy(relations),
            "events": [checked_text(e, 2000, False) for e in events],
        },
        "characters": {},
        "active": [],
        "revision": 0,
    }
    roles = data["characters"]
    if not isinstance(roles, list) or not 1 <= len(roles) <= 100:
        raise ValueError("Expected named non-player characters")
    names = set()
    for index, role in enumerate(roles, 1):
        if (
            not isinstance(role, dict)
            or set(role)
            != {
                "id",
                "name",
                "controller",
                "core",
                "state",
                "growth",
                "knowledge",
                "memories",
            }
            or role["id"] != f"r{index}"
        ):
            raise ValueError("Invalid character schema")
        if role["controller"] not in ("ai", "user"):
            raise ValueError("Invalid role controller")
        name = checked_text(role["name"], 100, False)
        if name in names:
            raise ValueError("Duplicate role name")
        names.add(name)
        memories = role["memories"]
        if not isinstance(memories, list) or len(memories) > 300:
            raise ValueError("Invalid memories")
        for mem in memories:
            if (
                not isinstance(mem, dict)
                or set(mem) != {"text", "known"}
                or type(mem["known"]) is not bool
            ):
                raise ValueError("Invalid memory knowledge")
            checked_text(mem["text"], 2000, False)
        result["characters"][role["id"]] = {
            "name": name,
            "controller": role["controller"],
            "core": checked_text(role["core"], 16000, False),
            "state": text_map(role["state"]),
            "growth": text_map(role["growth"]),
            "knowledge": text_map(role["knowledge"]),
            "memories": copy.deepcopy(memories),
        }
    return result


def validate_candidates(archive, data, source):
    """Validate each atomic proposal before any model decision or write.

    Args:
        archive: Existing archive.
        data: Compaction extraction result.
        source: Discarded narrative, the only allowed evidence source.

    Returns:
        Independent candidates and public summary.

    Raises:
        ValueError: Any proposal is invalid or unsupported by quoted evidence.
    """
    if (
        set(data) != {"candidates", "shared_summary"}
        or not isinstance(data["candidates"], list)
        or len(data["candidates"]) > 200
    ):
        raise ValueError("Invalid compaction schema")
    summary = checked_text(data["shared_summary"], 8000, False)
    candidates = copy.deepcopy(data["candidates"])
    seen = set()
    factions = set(archive["worldbook"]["factions"])
    for c in candidates:
        if (
            isinstance(c, dict)
            and c.get("kind") == "faction"
            and isinstance(c.get("key"), str)
        ):
            factions.add(c["key"])
    for c in candidates:
        if not isinstance(c, dict):
            raise ValueError("Invalid candidate")
        kind = c.get("kind")
        required = {"kind", "value", "evidence"}
        if kind in ("state", "growth", "knowledge"):
            required |= {"id", "key"}
        elif kind == "memory":
            required |= {"id", "known"}
        elif kind in ("world_fact", "faction"):
            required |= {"key"}
        elif kind == "faction_relation":
            required |= {"source", "target"}
        elif kind != "world_event":
            raise ValueError("Unsupported mutation kind")
        if set(c) != required:
            raise ValueError("Invalid candidate fields")
        if "id" in c and (
            not isinstance(c["id"], str) or c["id"] not in archive["characters"]
        ):
            raise ValueError("Unknown role")
        if "key" in c:
            checked_text(c["key"], 100, False)
            if kind == "world_fact" and sum(name in c["key"] for name in factions) >= 2:
                raise ValueError("Faction-pair facts must use faction_relation")
        checked_text(c["value"], 4000, False)
        quote = checked_text(c["evidence"], 4000, False)
        if quote not in source:
            raise ValueError("Evidence is absent from discarded history")
        if kind == "memory" and type(c["known"]) is not bool:
            raise ValueError("Knowledge must be boolean")
        if kind == "faction_relation" and (
            c["source"] not in factions
            or c["target"] not in factions
            or c["source"] == c["target"]
        ):
            raise ValueError("Macro relations require faction endpoints")
        identity = (
            kind,
            c.get("id"),
            c.get("key"),
            c.get("source"),
            c.get("target"),
            c["value"] if kind in ("memory", "world_event") else None,
        )
        if identity in seen:
            raise ValueError("Multiple proposals for the same fact")
        seen.add(identity)
    return candidates, summary


def apply_candidates(archive, candidates, accepted):
    """Apply approved atomic facts without rewriting any author's constraints.

    Args:
        archive: Existing archive.
        candidates: Structurally validated proposals.
        accepted: Approved proposal indices.

    Returns:
        Independent updated archive.
    """
    result = copy.deepcopy(archive)
    # Add accepted faction definitions before their accepted relationships.
    selected = [c for i, c in enumerate(candidates) if i in accepted]
    selected.sort(key=lambda c: c["kind"] != "faction")
    for c in selected:
        kind = c["kind"]
        if kind in ("state", "growth", "knowledge"):
            result["characters"][c["id"]][kind][c["key"]] = c["value"]
        elif kind == "memory":
            memories = result["characters"][c["id"]]["memories"]
            previous = next((m for m in memories if m["text"] == c["value"]), None)
            if previous:
                previous["known"] |= c["known"]
            else:
                memories.append({"text": c["value"], "known": c["known"]})
        elif kind in ("world_fact", "faction"):
            result["worldbook"]["facts" if kind == "world_fact" else "factions"][
                c["key"]
            ] = c["value"]
        elif kind == "world_event":
            if c["value"] not in result["worldbook"]["events"]:
                result["worldbook"]["events"].append(c["value"])
        elif kind == "faction_relation":
            book = result["worldbook"]
            if (
                c["source"] not in book["factions"]
                or c["target"] not in book["factions"]
            ):
                continue
            previous = next(
                (
                    r
                    for r in book["relations"]
                    if (r["source"], r["target"]) == (c["source"], c["target"])
                ),
                None,
            )
            if previous:
                previous["value"] = c["value"]
            else:
                book["relations"].append(
                    {k: c[k] for k in ("source", "target", "value")}
                )
    return result


def relevant_indices(texts, query, limit):
    """Retrieve relevant older facts with a bounded recent fallback, locally.

    Args:
        texts: Stored fact strings.
        query: Current scene text.
        limit: Maximum records to inject.

    Returns:
        Selected indices in historical order.
    """
    words = set(re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]{2}", query.lower()[:4000]))
    ranked = sorted(
        range(len(texts)),
        key=lambda i: (sum(w in texts[i].lower() for w in words), i),
        reverse=True,
    )
    return sorted(ranked[:limit])


def render(archive, active=None, referenced=None, query=""):
    """Inject fixed rules, relevant macro context, and selected role cards.

    Args:
        archive: Stored archive.
        active: In-scene role IDs.
        referenced: Relevant off-scene role IDs.
        query: Scene text used for local memory retrieval.

    Returns:
        Persona replacement text.
    """
    active = archive.get("active", []) if active is None else active
    referenced = [] if referenced is None else referenced
    book = copy.deepcopy(archive["worldbook"])
    names = list(book["factions"])
    chosen = {
        names[i]
        for i in relevant_indices([n + book["factions"][n] for n in names], query, 6)
    }
    book["factions"] = {n: book["factions"][n] for n in names if n in chosen}
    book["relations"] = [
        r for r in book["relations"] if r["source"] in chosen or r["target"] in chosen
    ]
    book["events"] = [
        book["events"][i] for i in relevant_indices(book["events"], query, 8)
    ]
    scene = {
        "world": archive["world"],
        "worldbook": book,
        "characters": {},
        "referenced": {},
    }
    for key in dict.fromkeys(active + referenced):
        if key not in archive["characters"]:
            continue
        role = copy.deepcopy(archive["characters"][key])
        memories = role["memories"]
        role["memories"] = [
            memories[i]
            for i in relevant_indices([m["text"] for m in memories], query, 12)
        ]
        scene["characters" if key in active else "referenced"][key] = role
    return INJECTION + json.dumps(scene, ensure_ascii=False)


class ArchiveStore:
    """Store isolated character and world archives atomically."""

    def __init__(self, directory: Path):
        """Bind persistent storage.

        Args:
            directory: Plugin-owned data path.
        """
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)

    def path(self, scope):
        """Resolve a safe opaque archive filename.

        Args:
            scope: Conversation and persona identity.

        Returns:
            Path to a JSON file.
        """
        return self.directory / (hashlib.sha256(scope.encode()).hexdigest() + ".json")

    def id_path(self, archive_id):
        """Resolve a validated opaque archive identifier inside this store.

        Args:
            archive_id: SHA-256 filename stem, never a filesystem path.

        Returns:
            Confined archive path.

        Raises:
            ValueError: Identifier is invalid.
        """
        if not isinstance(archive_id, str) or not re.fullmatch(
            r"[a-f0-9]{64}", archive_id
        ):
            raise ValueError("Invalid archive identifier")
        path = self.directory / (archive_id + ".json")
        if path.is_symlink():
            raise ValueError("Archive symlinks are not allowed")
        return path

    def load(self, scope):
        """Load an archive and migrate old text fields without a network call.

        Args:
            scope: Archive identity.

        Returns:
            Archive, or None if it does not exist.

        Raises:
            ValueError: Corrupt or unsupported archive.
        """
        return self.load_id(self.path(scope).stem)

    def load_id(self, archive_id):
        """Read an archive using its validated opaque identifier.

        Args:
            archive_id: SHA-256 filename stem.

        Returns:
            Archive or None when absent.

        Raises:
            ValueError: Identifier or archive is invalid.
        """
        path = self.id_path(archive_id)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(data, dict)
            or data.get("version") not in (1, 2)
            or not isinstance(data.get("characters"), dict)
        ):
            raise ValueError("Unsupported archive")
        if data["version"] == 1:
            for role in data["characters"].values():
                for field in ("state", "growth"):
                    value = role.get(field, "")
                    role[field] = {"既有记录": value} if value else {}
                role["knowledge"] = {}
                role["controller"] = "ai"
            data["worldbook"] = {
                "facts": {},
                "factions": {},
                "relations": [],
                "events": [],
            }
            data["version"] = 2
            data.pop("recall", None)
        return data

    def save(self, scope, archive):
        """Replace the archive atomically, keeping private file permissions.

        Args:
            scope: Archive identity.
            archive: Validated content.
        """
        self.save_id(self.path(scope).stem, archive)

    def save_id(self, archive_id, archive):
        """Atomically write a validated archive identifier.

        Args:
            archive_id: SHA-256 filename stem.
            archive: Validated archive content.
        """
        path = self.id_path(archive_id)
        temporary = path.with_suffix(".tmp")
        try:
            temporary.write_text(
                json.dumps({**archive, "version": 2}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.chmod(0o600)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
