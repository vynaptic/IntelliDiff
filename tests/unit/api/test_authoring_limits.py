"""The production authoring routes return actionable refusals for bounded-resource inputs."""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from deltasleuth.api import deps
from deltasleuth.api.app import _HANDLERS
from deltasleuth.api.routes import specs
from deltasleuth.auth.principal import dev
from deltasleuth.core.spec.documents import parse_yaml_text

from .conftest import SPEC_YAML

NAMES = {"spec_name": "drafted", "target_a": "left", "target_b": "right", "key_name": "item_id"}


@pytest.fixture
def authoring() -> Iterator[TestClient]:
    # These routes need targets, not a database. Keep production parsing, rules and error handlers;
    # the role was granted before the untrusted authoring input reaches them.
    app = FastAPI(exception_handlers=_HANDLERS)
    app.include_router(specs.router, prefix="/v1")
    app.dependency_overrides[deps.context] = lambda: SimpleNamespace(targets=None)
    app.dependency_overrides[deps.principal] = dev
    with TestClient(app) as client:
        yield client


def test_profile_alignment_limit_is_an_actionable_422(authoring: TestClient) -> None:
    doc = {f"field_{i}": str(i) if i % 2 else i for i in range(600)}
    answer = authoring.post("/v1/profile", json=NAMES | {"pairs": [{"key": "K1", "a": doc, "b": doc}]})
    assert answer.status_code == 422, answer.text
    assert "alignment" in answer.json()["detail"] and "narrower samples" in answer.json()["detail"]


def test_profile_and_validation_bound_hostile_patterns(authoring: TestClient) -> None:
    hostile, pattern = "a" * 100 + "b", "^(a|aa)+$"
    payload = NAMES | {"key_pattern": pattern, "pairs": [{"key": hostile, "a": {"v": 1}, "b": {"v": 1}}]}
    answer = authoring.post("/v1/profile", json=payload)
    assert answer.status_code == 422 and "time limit" in answer.json()["detail"]
    assert hostile not in answer.text
    doc = parse_yaml_text(SPEC_YAML)
    doc["key"].update(pattern=pattern, source={"kind": "list", "keys": [hostile]})
    validation = authoring.post("/v1/specs/validate", content=yaml.safe_dump(doc))
    assert validation.status_code == 200 and validation.json()["valid"] is False
    assert ("/key/pattern", "key_pattern_limit") in {
        (issue["pointer"], issue["code"]) for issue in validation.json()["issues"]
    }
    assert hostile not in validation.text
    uploaded = authoring.post("/v1/specs", content=yaml.safe_dump(doc))
    assert uploaded.status_code == 422 and hostile not in uploaded.text


def test_profile_yaml_keeps_its_draft_policy_and_valid_control(authoring: TestClient) -> None:
    wrapper = "\u2028security: {unlisted: clear}\u2028#"
    pair = {"key": "K1", "a": {"id": "K1", "v": 1}, "b": {wrapper: {"id": "K1", "v": 1}}}
    answer = authoring.post("/v1/profile", json=NAMES | {"pairs": [pair]})
    assert answer.status_code == 200, answer.text
    doc = parse_yaml_text(answer.json()["yaml"])
    assert "security" not in doc and doc["key"]["source"]["keys"] == ["K1"]
    assert wrapper in doc["sides"]["b"]["select"]
