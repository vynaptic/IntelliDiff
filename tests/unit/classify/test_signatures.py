"""Finding signatures."""

from __future__ import annotations

import hashlib
from dataclasses import replace

from deltasleuth.core.classify.signatures import signature_hash
from deltasleuth.core.types import ChangeType


def test_signature_is_16_hex_of_sha256_over_the_four_fields(make_finding) -> None:
    f = make_finding("$.segment", a="A", b="B", value_class="other", taxonomy_code="VALUE_MISMATCH")
    assert (
        signature_hash(f)
        == hashlib.sha256(b"$.segment\x1fvalue_changed\x1fother\x1fVALUE_MISMATCH").hexdigest()[:16]
    )
    unset = hashlib.sha256(b"$.segment\x1fvalue_changed\x1f\x1f").hexdigest()[:16]
    assert signature_hash(replace(f, value_class=None, taxonomy_code=None)) == unset


def test_signature_depends_on_the_four_fields_only(make_finding) -> None:
    f = make_finding("$.segment", a="A", b="B", value_class="other", taxonomy_code="VALUE_MISMATCH")
    same = replace(f, key="C99999999", a_value="X", b_value="Y", json_pointer="/other", member_key="m")
    assert signature_hash(same) == signature_hash(f)
    variants = [
        replace(f, path_pattern="$.status"),
        replace(f, change_type=ChangeType.TYPE_CHANGED),
        replace(f, value_class="case_only"),
        replace(f, taxonomy_code="ATTRIBUTE_MISMATCH"),
    ]
    assert len({signature_hash(v) for v in [f, *variants]}) == 5
