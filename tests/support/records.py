"""Synthetic REST sources for integration tests. All documents and keys are generated in memory."""

import json
from collections import Counter
from pathlib import Path
from typing import Any

import yaml
from aiohttp import web

from deltasleuth.connectors.files import SampleWriter
from deltasleuth.core.types import Side

PAIRS = ("group", "record")


def keys(pair: str) -> list[str]:
    size = {"group": 61, "record": 40}[pair]
    return [f"item-{i:03d}" for i in range(1, size + 1)]


def spec(pair: str) -> dict[str, Any]:
    sides = {
        name: {
            "role": role, "target": f"{pair}-{name}",
            "request": {"method": "GET", "path": "/items/{{ key }}"},
            "select": f"$.{name}", "notFound": {"status": [404]}, "truncation": {"flagPaths": ["$.cut"]},
        }
        for name, role in (("a", "legacy"), ("b", "new"))
    }
    sides["b"]["map"] = (
        '{"id": id, "note": note, "count": units, "variant": variant, "state": state, "label": label}'
    )
    return {
        "apiVersion": "deltasleuth/v1", "kind": "ComparisonSpec",
        "metadata": {"name": f"test-{pair}", "version": "1.0.0", "owner": "tests"},
        "normalizerVersion": "1.1.0",
        "key": {"name": "id", "type": "string", "pattern": "^item-[0-9]{3}$",
                "class": "identifier" if pair == "record" else "business_code",
                "source": {"kind": "list", "keys": keys(pair)}},
        "sides": sides, "compare": {"fields": [{"path": "$.count", "severity": "CRITICAL"},
                                                {"path": "$.label", "normalize": ["trim"]}]},
        "knownDiffs": [
            {"id": f"KD-{i:04d}", "when": f"path == '$.state' && b == '{state}'",
             "classification": classification, "reason": "Test rule for a state change", "ticket": f"TT-{i}"}
            for i, (state, classification) in enumerate([("v2", "expected_by_design"), ("v3", "bug")], 1)
        ],
        "recheck": {"delay": "0s", "times": 1, "oneShot": {"delay": "0s", "times": 1},
                    "outcomes": ["MISMATCH", "NOT_FOUND_A", "NOT_FOUND_B"]},
        "execution": {"concurrency": 2, "retries": {"max": 2, "retryOn": ["http_5xx"]}},
        "security": {"unlisted": "tokenize", "clear": ["$.count", "$.variant", "$.state"],
                     "tokenize": [{"path": "$.id", "class": "identifier"},
                                  {"path": "$.note", "class": "name"}]},
    }


def write_spec(directory: Path, pair: str) -> Path:
    path = directory / f"{pair}.yaml"
    path.write_text(yaml.safe_dump(spec(pair)))
    return path


def targets(pair: str, ports: dict[str, int], rate: float | None) -> str:
    return yaml.safe_dump({"targets": {
        f"{pair}-{side}": {"base_url": f"http://127.0.0.1:{port}", "allow_private_network": True,
                           "allow_insecure_http": True, "rate_per_s": rate or 100,
                           "capabilities": {
                               "authoritative_absence": {"status": [404], "shapes": ["missing"]},
                               "absence_shapes": {"missing": 'has(body.error) && body.error == "NOT_FOUND"'},
                           }}
        for side, port in ports.items()
    }})


def reply(pair: str, side: str, key: str, call: int) -> tuple[int, dict[str, Any]]:
    absent = (side, key) == ("b", "item-027") or (pair, key) == ("group", "item-050")
    if key not in keys(pair) or absent:
        return 404, {"error": "NOT_FOUND"}
    i = int(key.removeprefix("item-"))
    item = {"id": key, "note": f"test-note-{i:03d}", "count": i, "variant": "same", "state": "v1"}
    item["label"] = "normal"
    if side == "b":
        if pair == "group" and i == 51:
            return 404, {"error": "unconfirmed"}
        if pair == "group" and i == 15:
            item["label"] = " normal "
        if i == (3 if pair == "record" else 7) or (pair == "group" and i == 29 and call == 1):
            item["count"] = i + 1
        if i == 9:
            del item["variant"]
        if i in {31, 32}:
            item["state"] = "v2" if i == 31 else "v3"
        if pair == "group" and i == 43 and call == 1:
            return 503, {"error": "retry"}
        item["units"] = item.pop("count")
    return 200, {side: item, "cut": (pair, side, i) == ("group", "b", 49), "meta": {side: True}}


def app(pair: str, side: str) -> web.Application:
    calls: Counter[str] = Counter()

    async def read(request: web.Request) -> web.Response:
        key = request.match_info["key"]
        calls[key] += 1
        status, body = reply(pair, side, key, calls[key])
        return web.json_response(body, status=status)

    server = web.Application()
    server.router.add_get("/items/{key}", read, allow_head=False)
    return server


def private_values(pair: str) -> set[str]:
    values = {f"test-note-{i:03d}" for i in range(1, len(keys(pair)) + 1)}
    # The group contract explicitly declares its keys as business codes.
    return values.union(keys(pair)) if pair == "record" else values


def expected(pair: str) -> dict[str, dict[str, dict[str, Any]]]:
    def changed(outcome: str, verdict: str, code: str, **extra: Any) -> dict[str, Any]:
        return {"outcome": outcome, "verdict": verdict, "findings": [{"taxonomy_code": code}], **extra}

    changes = {
        3 if pair == "record" else 7: changed("MISMATCH", "DIFFERENT", "VALUE_MISMATCH", rechecked=True),
        9: changed("MISMATCH", "DIFFERENT", "FIELD_MISSING", rechecked=True),
        27: changed("NOT_FOUND_B", "DIFFERENT", "MISSING_DOCUMENT", rechecked=True),
        31: changed("KNOWN_DIFF", "MATCH", "VALUE_MISMATCH"),
        32: changed("KNOWN_DIFF", "DIFFERENT", "VALUE_MISMATCH"),
    }
    result = {f"item-{i:03d}": {"live": row, "file": row} for i, row in changes.items()}
    if pair == "group":
        for i, why in [(49, "truncated"), (50, "absent_both"), (51, "absence_unconfirmed")]:
            row = {"outcome": "INCONCLUSIVE", "verdict": "INDETERMINATE",
                   "inconclusive_reason": why, "findings": []}
            result[f"item-{i:03d}"] = {"live": row, "file": row}
        noise = changed("MATCH", "MATCH", "NORMALIZATION_ONLY")
        result["item-015"] = {"live": noise, "file": noise}
        result["item-029"] = {"file": changed("MISMATCH", "DIFFERENT", "VALUE_MISMATCH"),
                              "live": {"outcome": "DRIFT", "verdict": "INDETERMINATE", "findings": [],
                                       "first_outcome": "MISMATCH", "rechecked": True}}
        ok = {"outcome": "MATCH", "verdict": "MATCH", "findings": []}
        result["item-043"] = {"file": ok, "live": ok | {"attempts": {"b": 2}}}
    return result


def write_samples(pair: str, directory: Path) -> list[dict[str, Any]]:
    """Write replay files and return the equivalent API sample pairs."""
    writer = SampleWriter(directory)
    pairs = []
    for key in keys(pair):
        row: dict[str, Any] = {"key": key}
        for side in Side:
            status, body = reply(pair, side.value, key, 1)
            if status == 503:
                status, body = reply(pair, side.value, key, 2)
            row[side.value] = body if status == 200 else None
            writer.write(key, side, json.dumps(body).encode(), status=status, latency_ms=1)
        pairs.append(row)
    return pairs
