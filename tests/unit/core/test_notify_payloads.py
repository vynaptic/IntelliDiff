"""Payloads, channel shapes and wording (docs/mvp/ALERTS.md §6): the Standard Webhooks signature, Slack's
escaping, Teams under 28 KB, e-mail headers stored without addresses, the footer and its labels, withheld
names and links, closed texts that never claim a verdict, and no root-cause id."""

from __future__ import annotations

import base64
import email
import json
import re
from email.policy import SMTP
from typing import Any

import pytest
from multidict import CIMultiDict

from deltasleuth.connectors.notify import signature
from deltasleuth.core import notify
from deltasleuth.core.notify import Event, Fired
from deltasleuth.core.spec import compile_spec
from deltasleuth.core.spec.documents import parse_yaml_text
from deltasleuth.runner.notify import _unlabelled
from deltasleuth.security import rawvalues
from mocks.notify import verify
from tests.unit.runner.conftest import SPEC

from .notified import (
    APPROVAL,
    DELIVERY,
    DRAW,
    INSTALLATION,
    RUN,
    block,
    ended,
    missed,
    paused,
    readiness,
    rendered,
)

BANNED = re.compile(r"\b(resolved|fixed|parity|all clear|no discrepancies|new)\b", re.IGNORECASE)


def test_the_standard_webhooks_vector_and_an_independent_hmac() -> None:
    key = base64.b64decode("MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw")
    body = b'{"test": 2432232314}'
    assert signature([key], "msg_p5jXN8AQM9LWM0D4loKWxJek", 1614265330, body) == (
        "v1,g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE="
    )
    headers = CIMultiDict(
        {
            "webhook-id": DELIVERY,
            "webhook-timestamp": "1700000000",
            "webhook-signature": signature([b"k" * 32, b"j" * 32], DELIVERY, 1700000000, body),
        }
    )
    assert verify([b"j" * 32], headers, body) and not verify([b"x" * 32], headers, body)
    assert not verify([b"j" * 32], headers, body + b" ")


def test_the_webhook_document() -> None:
    doc, body = rendered(ended(), fired={"new-gaps": Fired(held=("new_root_causes_at_least",))})
    assert json.loads(body) == doc
    assert {k: doc[k] for k in ("format", "id", "event", "event_id", "severity", "occurred_at", "claim")} == {
        "format": "deltasleuth-notify/1",
        "id": DELIVERY,
        "event": "cycle.ended",
        "event_id": "seq:81234:cycle.ended",
        "severity": "warning",
        "occurred_at": "2026-10-03T04:00:12Z",
        "claim": "observed",
    }
    assert doc["installation"] == {"id": INSTALLATION, "label": "prod"} and doc["rules"] == ["new-gaps"]
    assert doc["link"] == f"https://ds.bank.example/#/runs/{RUN}?cycle=12" and doc["withheld"] == {}
    assert doc["run"]["spec_name"] == "customer-profile"
    assert rendered(ended(), public_url=None)[0]["link"] is None  # developer mode


def test_the_cycle_text() -> None:
    bounds = {"mismatch_rate_above": "0.001"}
    doc, _ = rendered(ended(), fired={"r": Fired(crossed=("mismatch_rate_above",))}, bounds=bounds)
    assert doc["text"] == (
        f"[prod] customer-profile: cycle 12 of run {RUN} ended: 12 of 1,000,000 keys different,"
        " 3 indeterminate, 0 not read (observed). 2 root causes observed for the first time since cycle 4;"
        " 1 no longer observed. Of the keys read in both cycles 11 and 12, 41 were observed discrepant in 12"
        " and not in 11; 17 no longer."
        " Mismatch rate 0.0021 (was 0.0009 in cycle 11; above 0.001)."
        f" Observed by DeltaSleuth (installation {INSTALLATION}); not evidence and not a sign-off."
        " Audit row 81234."
    )
    first = rendered(ended(cycle=1, baseline_from=None, gone_root_causes=0))[0]["text"]
    assert "2 root causes and 41 discrepant keys observed in the first cycle." in first
    sampled = rendered(ended(between_samples=True, newly_discrepant=None, no_longer_discrepant=None))[0][
        "text"
    ]
    assert "(between samples); 1 no longer observed; key changes are not counted between samples." in sampled
    assert "Of the keys" not in sampled
    timed = rendered(ended(new_root_causes=None, newly_discrepant=None, unavailable=["new_root_causes"]))[0]
    assert "Root causes observed for the first time since cycle 4: not counted; 1 no longer" in timed["text"]
    assert "Of the keys read in both cycles 11 and 12, changes were not counted." in timed["text"]


def test_the_footer_says_what_it_is_not() -> None:
    unapproved = notify.checked(
        Event(
            "run.paused",
            {
                "cycle": 2,
                "reason": "run_paused_auth_error",
                "approval_id": None,
                "draw_id": None,
                "licence_refusal": None,
            },
            block(approval="unapproved", licence="evaluation"),
        )
    )
    text = rendered(unapproved)[0]["text"]
    assert text.startswith(
        f"[unlicensed evaluation] [prod] customer-profile: run {RUN} paused: a side refused"
    )
    assert text.endswith("Audit row 81234. Not sign-off evidence.")
    assert "A person fixes it and resumes the run." in text


def test_an_open_build_s_state_asks_for_no_licence() -> None:
    """docs/mvp/LICENCE.md §19: the one licence.state row of a build without a vendor key."""
    facts = {"installation_id": INSTALLATION, "from": "evaluation", "to": "open", "licence_id": None}
    doc, _ = rendered(notify.checked(Event("licence.state", facts | {"ends": None})))
    assert (doc["severity"], doc["title"]) == ("info", "no licence is needed")
    assert doc["text"] == (
        f"[prod] Installation {INSTALLATION}: no licence is needed (was evaluation); this build enforces"
        f" none. Observed by DeltaSleuth (installation {INSTALLATION}); not evidence and not a sign-off."
        " Audit row 81234."
    )


def test_names_and_links_withheld() -> None:
    event = ended()
    assert rendered(event, labels=False)[0]["withheld"] == {"spec_name": "labels_off"}
    doc, _ = rendered(event, "slack")
    assert doc["withheld"] == {"spec_name": "labels_off", "link": "links_off"} and doc["link"] is None
    assert doc["run"]["spec_name"] is None and f"spec 7: cycle 12 of run {RUN}" in doc["text"]
    assert rendered(event, "teams")[0]["withheld"] == {"spec_name": "labels_off", "link": "links_off"}
    for why in ("no_key_pattern", "key_shaped"):
        doc, body = rendered(Event(**{**vars(event), "unlabelled": why}))
        assert doc["withheld"] == {"spec_name": why} and "customer-profile" not in body


@pytest.mark.parametrize(
    ("name", "key", "expected"),
    [
        ("customer-profile", 'pattern: "^C[0-9]{6}$"\n  class: identifier', None),
        ("customer-profile", 'pattern: ".*"\n  class: identifier', "no_key_pattern"),  # matches ""
        ("c123456", 'pattern: "^C[0-9]{6}$"\n  class: identifier', "key_shaped"),  # normalised: upper
        ("c123456", 'pattern: "^C[0-9]{6}$"\n  class: business_code', None),
    ],
)
def test_a_spec_name_is_sent_only_when_it_cannot_be_a_key(name: str, key: str, expected: str | None) -> None:
    text = SPEC.replace("name: unit-runner", f"name: {name}").replace("[K001, K002, K003]", "[C000001]")
    spec = compile_spec(parse_yaml_text(text.replace('pattern: "^K[0-9]{3}$"\n  class: business_code', key)))
    assert _unlabelled(spec) == expected


def test_slack_escapes_and_teams_stays_small() -> None:
    escaped = notify.checked(
        Event(
            "approval.waiting",
            {
                "approval_id": APPROVAL,
                "kind": "spec",
                "spec_id": 7,
                "run_id": None,
                "cycle": None,
                "approvals_left": 1,
            },
        )
    )
    _, body = rendered(escaped, "slack")
    slack = json.loads(body)
    assert slack["unfurl_links"] is False and slack["unfurl_media"] is False
    assert [b["type"] for b in slack["blocks"]] == ["section", "context"] and len(
        slack["blocks"][0]["fields"]
    ) <= 8
    assert notify._slack("a & <b> c") == "a &amp; &lt;b&gt; c"
    assert slack["blocks"][1]["elements"][0]["text"] == f"audit row 81234 · {DELIVERY} · observed"
    daily = notify.checked(
        Event(
            "notify.daily",
            {
                "runs": {"running": 3},
                "paused": {"manual": 1, "run_paused_schema_drift": 2},  # a named reason, as each pause's
                "cycles_ended_24h": 4,
                "runs_finished_24h": 1,
                "approvals_waiting": 2,
                "licence": "valid",
                "feed_lag_s": 0,
                "mutes": 0,
                "channels": {
                    f"channel-{i}": {
                        "pending": 1,
                        "sent_24h": 2,
                        "dead_24h": 0,
                        "suppressed_24h": 0,
                        "head_age_s": 3,
                    }
                    for i in range(20)
                },
            },
        )
    )
    assert daily.facts["paused"] == {"manual": 1, "run_paused_schema_drift": 2}
    _, body = rendered(daily, "teams", links=True)
    teams = json.loads(body)
    card = teams["attachments"][0]
    assert teams["type"] == "message" and card["contentType"] == "application/vnd.microsoft.card.adaptive"
    assert card["contentUrl"] is None and card["content"]["version"] == "1.4"
    assert (
        len(card["content"]["body"][2]["facts"]) <= 10
        and card["content"]["actions"][0]["type"] == "Action.OpenUrl"
    )
    assert len(body.encode()) < 28 * 1024 and len(rendered(ended(), "teams")[1].encode()) < 28 * 1024


def test_email_headers_and_no_address_stored() -> None:
    doc, body = rendered(ended(), "email")
    message = email.message_from_string(body, policy=SMTP)
    assert message["Subject"] == f"[DeltaSleuth prod] Warning: customer-profile: cycle 12 of run {RUN} ended"
    assert message["Message-ID"] == f"<{DELIVERY}@{INSTALLATION}.deltasleuth.invalid>"
    assert message["Auto-Submitted"] == "auto-generated" and message["X-DeltaSleuth-Event"] == "cycle.ended"
    assert message["Date"] == "Sat, 03 Oct 2026 04:00:12 +0000"
    assert message["From"] is None and message["To"] is None and message["Reply-To"] is None
    assert "\r\n" in body and doc["link"] in message.get_content()
    with pytest.raises(ValueError):  # CR or LF in a header is refused
        message["X-Test"] = "a\r\nBcc: x@example.com"


def _all(**options: Any) -> list[str]:
    """Every kind's text over its cases, for every channel kind."""
    cases = [
        ended(),
        ended(cycle=1, baseline_from=None, gone_root_causes=0),
        ended(between_samples=True, newly_discrepant=None, no_longer_discrepant=None),
        ended(new_root_causes=None, gone_root_causes=None),
    ]
    for reason in notify.PAUSES:
        cases.append(
            Event(
                "run.paused",
                {
                    "cycle": 2,
                    "reason": reason,
                    "approval_id": None,
                    "draw_id": None,
                    "licence_refusal": "sso",
                },
                block(),
            )
        )
    for status, reason in (
        ("completed", None),
        ("completed_with_issues", "stop_condition:maxCycles"),
        ("cancelled", "cancelled"),
    ):
        cases.append(Event("run.finished", {"status": status, "reason": reason, "cycle": 3}, block()))
    for via in ("cancelling", "run.finished"):
        cases.append(Event("run.stopped", {"cycle": 3, "condition": "until"}, block(), via=via))
    cases += [
        Event("run.failed", {"cycle": 3, "error_type": "SecretError"}, block()),
        Event("sample.draw_failed", {"cycle": 3}, block()),
        Event(
            "sample.judged",
            {
                "cycle": 3,
                "draw_id": "D-" + "0" * 26,
                "method": "rate",
                "status": "unjudged",
                "strata": {"passed": 0, "failed": 0, "unfinished": 0},
            },
            block(),
        ),
        Event("approval.revoked", {"approval_id": APPROVAL, "kind": "signoff", "paused": 0, "stopped": 1}),
        Event(
            "licence.state",
            {
                "installation_id": INSTALLATION,
                "from": None,
                "to": "expired",
                "licence_id": "LI-" + "0" * 26,
                "ends": "2026-11-01",
            },
        ),
        Event(
            "notify.dead",
            {
                "channel": "ops-hook",
                "delivery_id": DELIVERY,
                "event": "run.failed",
                "attempts": 8,
                "error": "retries_exhausted",
                "dead_since_last": 0,
            },
        ),
        Event("notify.skipped", {"reason": "stale", "from_seq": 1, "to_seq": 9}),
        Event("notify.skipped", {"reason": "unsafe", "rows": 2, "seqs": [3, 4]}),
        Event("notify.test", {"channel": "ops-hook"}),
        readiness(),
        readiness(**{"from": None, "causes": ["baseline"], "changed": []}),
        readiness(to="not_ready", regressed=True),
    ]
    for reason, (codes, _) in notify.MISSED.items():
        typed = "ValueError" if reason in ("not_started", "error") else None
        cases += [missed(reason=reason, code=code, draw_id=DRAW, error_type=typed) for code in codes]
    cases.append(missed(reason="late", code="down", to="2026-10-09T01:00Z", count=3))
    cases.append(missed(reason="approval", to="2026-10-21T01:00Z", count=14))
    cases += [*paused(), *paused(code="cron")]  # the worker's pause before a time is due
    texts = []
    for event in cases:
        for kind in ("webhook", "slack", "teams", "email"):
            doc, body = rendered(notify.checked(event), kind, **options)
            texts += [doc["title"], doc["text"], body]
    return texts


def test_banned_words() -> None:
    """No text claims a verdict; ``parity`` is said only in the names of readiness's state and its code
    (parity ready, docs/mvp/READINESS.md §5), never of what was observed."""
    unwrapped = [t.replace("=\r\n", "") for t in _all()]  # an e-mail's soft line breaks
    texts = [re.sub(r"parity[_ ]ready", "", t) for t in unwrapped]
    assert [m.group(0) for t in texts for m in BANNED.finditer(t)] == []


def test_no_root_cause_id() -> None:
    assert not any(re.search(r"RC-[0-9a-f]{12}", text) for text in _all())


def test_delivery_and_mute_ids_are_words_deltasleuth_writes() -> None:
    """A pack's raw-value gate reads N- and M- ids as generated, as it reads R-, A- and D- ids (§12)."""
    assert rawvalues._generated(DELIVERY) and rawvalues._generated("M-" + "7" * 26)
    assert rawvalues._generated(f"user:{DELIVERY}") and not rawvalues._generated("X-" + "7" * 26)
    assert rawvalues._generated("SC-" + "7" * 26) and not rawvalues._generated("SC-" + "7" * 25)
