"""Fixture-side oracle: classify an attempt from what the fixture received.

The fixture page records which element actually received each click or scroll.
An attempt is scored against that record, never against a screen change, a
model assessment or a caller claim.  A deliberately wrong click is counted
wrong even when it caused a visible screen change.

Primary outcomes (one per attempt, see ``benchmarks.baseline.OUTCOMES``):

* ``success``         intended target received exactly the expected effect
* ``missing_target``  a complete observation showed the target absent; nothing dispatched
* ``declined``        a guard or policy refused before any input was dispatched
* ``wrong_dispatch``  the intended effect did not follow (delivered to another
                      target, delivered too many times, or dispatched with no receipt)
* ``unknown``         fixture evidence cannot establish either way

Receipt evidence always wins over a claim: a tool that reports ``aborted_focus``
or ``paused`` while the fixture actually received a click is classified from
that receipt, not as a safe refusal.

``false_success`` is a separate flag: the caller or a planner said "done" while
the oracle did not confirm the intended effect.  Only explicit completion
claims count (see ``benchmarks.baseline.completion_claim``); a dispatch
acknowledgement is not one.
"""

from __future__ import annotations


def classify(*, intended: str, received=(), dispatched: bool, refused: bool = False,
             target_observed: bool | None = None, claimed_success: bool = False,
             effect_ok: bool | None = None, screen_changed: bool | None = None,
             claimed_by: str = "", expected_count: int | None = 1,
             detail: str = "") -> dict:
    """Score one attempt from fixture-side receipt events.

    ``received``       fixture target ids that received the relevant input kind,
                       one entry per event (duplicates are preserved).
    ``dispatched``     the caller's *claim* that input was sent.  Fixture
                       receipts override this claim in both directions.
    ``refused``        the caller's *claim* that a guard stopped the attempt
                       before input.  A receipt contradicts it.
    ``target_observed`` True/False from a *complete* observation, None if the
                       search was partial or unavailable (partial absence proves
                       nothing).
    ``expected_count`` required number of receipts on the sole intended target;
                       None accepts any positive count (e.g. smooth-scroll events).
    ``effect_ok``      scenario-specific objective result, None when receipt of
                       the intended input is the whole effect.
    ``screen_changed`` recorded for the report only; never promotes to success.
    """
    if not isinstance(intended, str) or not intended:
        raise ValueError("intended target id is required")
    counts: dict[str, int] = {}
    order: list[str] = []
    for value in received:
        if isinstance(value, str) and value:
            counts[value] = counts.get(value, 0) + 1
            if value not in order:
                order.append(value)
    receipts = list(order)
    has_evidence = bool(counts)
    executed = bool(dispatched) or has_evidence

    if not executed:
        if refused:
            outcome = "declined"
            reason = detail or "refused before input"
        elif target_observed is False:
            outcome = "missing_target"
            reason = "target absent from a complete observation; no input dispatched"
        elif target_observed is None:
            outcome = "unknown"
            reason = "observation incomplete; absence is unproven and nothing was dispatched"
        else:
            outcome = "unknown"
            reason = detail or "no input was dispatched"
    elif not has_evidence:
        outcome = "wrong_dispatch"
        reason = "dispatched input reached no fixture control"
    elif receipts == [intended]:
        if expected_count is not None and counts[intended] != expected_count:
            outcome = "wrong_dispatch"
            reason = ("expected %d receipt(s) on '%s', observed %d"
                      % (expected_count, intended, counts[intended]))
        elif effect_ok is False:
            outcome = "unknown"
            reason = "intended control received input but the expected effect was not observed"
        else:
            outcome = "success"
            reason = detail or "intended target received the input"
    else:
        outcome = "wrong_dispatch"
        reason = "intended '%s' but fixture received %s" % (
            intended, ", ".join("%s x%d" % (target, counts[target]) for target in receipts))

    if refused and has_evidence and outcome != "declined":
        reason += "; a refusal was reported but fixture receipts prove input was delivered"

    false_success = bool(claimed_success) and outcome != "success"
    if false_success:
        reason += "; caller/planner claimed success without fixture confirmation"
    return {
        "outcome": outcome,
        "false_success": false_success,
        "intended": intended,
        "received": receipts,
        "receipt_counts": dict(counts),
        "dispatched": bool(dispatched),
        "evidence_of_dispatch": has_evidence,
        "refused": bool(refused),
        "claimed_by": claimed_by,
        "claimed_success": bool(claimed_success),
        "screen_changed": None if screen_changed is None else bool(screen_changed),
        "reason": reason,
    }


def unresolved(intended: str, reason: str, *, claimed_success: bool = False) -> dict:
    """A structured 'not established' result for a scenario that could not run.

    Setup failures, timeouts and skipped dependencies still produce exactly one
    counted outcome instead of vanishing into a free-text failure entry.
    """
    return {
        "outcome": "unknown",
        "false_success": bool(claimed_success),
        "intended": intended,
        "received": [],
        "receipt_counts": {},
        "dispatched": False,
        "evidence_of_dispatch": False,
        "refused": False,
        "claimed_by": "",
        "claimed_success": bool(claimed_success),
        "screen_changed": None,
        "reason": reason,
    }


def verdict_from_receipts(name: str, intended: str, state_events, marker: int,
                          *, dispatched: bool, refused: bool = False,
                          target_observed: bool | None = None,
                          claimed_success: bool = False, effect_ok: bool | None = None,
                          expected_count: int | None = 1, screen_changed: bool | None = None,
                          claimed_by: str = "") -> dict:
    """Classify using fixture events recorded after ``marker`` (list index)."""
    received = [event.get("target") for event in list(state_events)[marker:]
                if event.get("kind") in ("click", "scroll")]
    verdict = classify(intended=intended, received=received, dispatched=dispatched,
                       refused=refused, target_observed=target_observed,
                       claimed_success=claimed_success, effect_ok=effect_ok,
                       expected_count=expected_count, screen_changed=screen_changed,
                       claimed_by=claimed_by)
    verdict["step"] = name
    return verdict
