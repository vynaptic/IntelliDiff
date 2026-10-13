"""Fail-closed egress, checked before every call; a denied call sends nothing.

A request needs declared data classes, all listed by the profile, in an environment the profile
lists. An external profile (by host trust) is also confined to dev/test/eval and to the external
baseline classes plus those this environment's policy approves.
"""

from __future__ import annotations

from dataclasses import dataclass

from deltasleuth.llm.model import DataClass, EgressDenied
from deltasleuth.llm.profiles import ENVIRONMENTS, LLMProfile, Trust

EXTERNAL_BASELINE = frozenset({DataClass.SYNTHETIC, DataClass.GOLDEN, DataClass.SCHEMA_SUMMARY})
EXTERNAL_ENVIRONMENTS = frozenset({"dev", "test", "eval"})


@dataclass(frozen=True)
class EgressPolicy:
    """Where this process runs, and which classes beyond the baseline it approves externally."""

    environment: str
    external_extra_classes: frozenset[DataClass] = frozenset()

    def __post_init__(self) -> None:
        if self.environment not in ENVIRONMENTS:
            raise ValueError(f"environment must be one of {ENVIRONMENTS}")
        object.__setattr__(
            self, "external_extra_classes", frozenset(DataClass(c) for c in self.external_extra_classes)
        )


def check_egress(profile: LLMProfile, data_classes: frozenset[DataClass], policy: EgressPolicy) -> None:
    """Raise ``EgressDenied`` unless ``profile`` may receive ``data_classes`` here."""
    external = profile.trust is Trust.EXTERNAL
    denials = {
        "no_data_classes": not data_classes,
        "environment_not_allowed": policy.environment not in profile.environments,
        "class_not_allowed": not data_classes <= profile.data_classes,
        "external_environment": external and policy.environment not in EXTERNAL_ENVIRONMENTS,
        "external_class": external and not data_classes <= EXTERNAL_BASELINE | policy.external_extra_classes,
    }
    reason = next((reason for reason, denied in denials.items() if denied), None)
    if reason is not None:
        classes = ",".join(sorted(data_classes)) or "none"
        raise EgressDenied(
            f"profile {profile.profile_id} ({profile.trust}, host {profile.host}) may not receive "
            f"[{classes}] in {policy.environment}: {reason}",
            reason=reason,
        )
