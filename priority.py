"""
priority.py - ReliefStock AI, Phase 2: transparent, decomposable priority.

score = severity + vulnerability + people + wait - prior-allocation penalty

Every component is stored on the PriorityScore, so the chat can show exactly
why a request ranked where it did. Ties are broken deterministically.

FAIRNESS RULE: distance NEVER affects priority. This module does not import
routing or registry and its functions take no coordinates, so it cannot.

`prior` maps (site_id, resource_value) -> (allocated_total, requested_total)
over EARLIER rounds. store.py builds it from the ledger. The penalty is
applied per site AND resource (a site that got its water is not penalised
for blankets). No `from __future__ import annotations`.
"""

from datetime import datetime, timezone

import config
from models import PriorityScore


def _hours_waiting(created_at, now):
    try:
        t = datetime.fromisoformat(created_at)
    except (TypeError, ValueError):
        return 0.0
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return max(0.0, (now - t).total_seconds() / 3600.0)


def score_request(req, prior=None, now=None):
    """Return the PriorityScore for one request (rank is filled in later)."""
    now = now or datetime.now(timezone.utc)

    severity_pts = req.severity * config.SEVERITY_WEIGHT

    raw_vuln = sum(config.VULNERABILITY_POINTS.get(g.value, 0.0) for g in set(req.vulnerable_groups))
    vulnerability_pts = min(config.VULNERABILITY_CAP, raw_vuln)

    people_pts = min(config.PEOPLE_CAP, req.people_affected / config.PEOPLE_PER_POINT)

    wait_pts = min(config.WAIT_CAP, _hours_waiting(req.created_at, now) * config.WAIT_POINTS_PER_HOUR)

    penalty = 0.0
    if prior:
        allocated, requested = prior.get((req.site_id, req.resource.value), (0, 0))
        if requested > 0:
            ratio = max(0.0, min(1.0, float(allocated) / float(requested)))
            penalty = config.PRIOR_PENALTY_MAX * ratio

    total = severity_pts + vulnerability_pts + people_pts + wait_pts - penalty
    return PriorityScore(
        request_id=req.request_id,
        severity_pts=round(severity_pts, 4),
        vulnerability_pts=round(vulnerability_pts, 4),
        people_pts=round(people_pts, 4),
        wait_pts=round(wait_pts, 4),
        prior_allocation_penalty=round(penalty, 4),
        total=round(total, 4),
    )


def rank_requests(requests, prior=None, now=None):
    """
    Score and rank requests. Returns a list of (request, PriorityScore),
    best first, with `rank` set (1 = highest priority).

    Tie-break order (fully deterministic, independent of input order):
      higher total -> higher severity -> more people affected
      -> earlier created_at -> request_id (alphabetical).
    """
    now = now or datetime.now(timezone.utc)
    scored = [(r, score_request(r, prior, now)) for r in requests]
    scored.sort(key=lambda rs: (-rs[1].total, -rs[0].severity, -rs[0].people_affected,
                                rs[0].created_at, rs[0].request_id))
    for position, (_, score) in enumerate(scored, start=1):
        score.rank = position
    return scored


if __name__ == "__main__":
    from datetime import timedelta
    from models import Resource, ResourceRequest, VulnerableGroup as VG

    NOW = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
    T0 = NOW.isoformat(timespec="seconds")

    def make(rid, site, sev, people, groups=(), created=T0, res=Resource.MEDICAL_KITS, qty=10):
        r = ResourceRequest(site_id=site, resource=res, quantity=qty, people_affected=people,
                            severity=sev, vulnerable_groups=list(groups), created_at=created)
        r.request_id = rid
        return r

    # hospital vs hamlet: 50 + 14 + 4 = 68  vs  30 + 6 + 0.6 = 36.6
    hosp = make("REQ-H", "S2", 5, 400, [VG.INJURED, VG.ELDERLY])
    hamlet = make("REQ-M", "S3", 3, 60, [VG.CHILDREN])
    ranked = rank_requests([hamlet, hosp], now=NOW)
    assert [r.request_id for r, _ in ranked] == ["REQ-H", "REQ-M"]
    assert ranked[0][1].total == 68.0 and ranked[1][1].total == 36.6, [s.total for _, s in ranked]
    assert ranked[0][1].rank == 1 and ranked[1][1].rank == 2

    # caps: huge crowd and many groups cannot exceed the caps
    big = score_request(make("REQ-B", "S1", 1, 10**6, [VG.CHILDREN, VG.ELDERLY, VG.PREGNANT, VG.DISABLED, VG.INJURED]), now=NOW)
    assert big.people_pts == config.PEOPLE_CAP and big.vulnerability_pts == config.VULNERABILITY_CAP

    # waiting time: 5 hours -> +5 points
    old = make("REQ-O", "S1", 3, 100, created=(NOW - timedelta(hours=5)).isoformat(timespec="seconds"))
    assert score_request(old, now=NOW).wait_pts == 5.0

    # prior-allocation penalty: full prior fulfilment -> -15, half -> -7.5, none -> 0
    base = make("REQ-P", "S4", 3, 100)
    plain = score_request(base, now=NOW).total
    full = score_request(base, {("S4", "medical_kits"): (100, 100)}, NOW)
    half = score_request(base, {("S4", "medical_kits"): (50, 100)}, NOW)
    other = score_request(base, {("S4", "water"): (100, 100)}, NOW)   # different resource: no penalty
    assert full.total == round(plain - 15.0, 4) and half.total == round(plain - 7.5, 4)
    assert full.prior_allocation_penalty == 15.0 and other.total == plain

    # deterministic ties, whatever the input order
    a = make("REQ-A", "S5", 3, 100)
    b = make("REQ-B", "S6", 3, 100)
    c = make("REQ-C", "S7", 3, 100)
    orders = [[a, b, c], [c, b, a], [b, c, a]]
    results = [[r.request_id for r, _ in rank_requests(o, now=NOW)] for o in orders]
    assert results[0] == results[1] == results[2] == ["REQ-A", "REQ-B", "REQ-C"], results

    # the score is explainable in one line
    print(ranked[0][1].explain())
    print("priority.py OK")