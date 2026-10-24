"""
validation.py - ReliefStock AI, Phase 1: collect and validate requests.

Takes the raw fields extracted from chat (everything may be a string, or
missing) and returns either a clean ResourceRequest or ALL the problems in
plain language. It never invents or "repairs" a value:
  * a missing field is reported as missing;
  * "12.5 litres" is rejected, not rounded;
  * an unknown group or resource is rejected, not dropped;
  * an unknown site is rejected unless the coordinator supplies lat/lng.

Returns (ValidationResult, new_site). `new_site` is a Site only when the
request was accepted AND registered a new location; the caller persists it.
Pure logic: no storage, no network. No `from __future__ import annotations`.
"""

import re

import config
from models import Resource, ResourceRequest, ValidationResult, VulnerableGroup, RequestStatus
from registry import lookup_site, make_user_site

# ---------------------------------------------------------------- vocabulary
RESOURCE_ALIASES = {
    "water": Resource.WATER, "drinking water": Resource.WATER,
    "clean water": Resource.WATER, "potable water": Resource.WATER,
    "medical kits": Resource.MEDICAL_KITS, "medical kit": Resource.MEDICAL_KITS,
    "medical": Resource.MEDICAL_KITS, "med kits": Resource.MEDICAL_KITS,
    "first aid": Resource.MEDICAL_KITS, "first aid kits": Resource.MEDICAL_KITS,
    "blankets": Resource.BLANKETS, "blanket": Resource.BLANKETS,
    "food": Resource.FOOD_PACKS, "food packs": Resource.FOOD_PACKS,
    "food pack": Resource.FOOD_PACKS, "rations": Resource.FOOD_PACKS,
    "ration packs": Resource.FOOD_PACKS,
}

GROUP_ALIASES = {
    "child": VulnerableGroup.CHILDREN, "children": VulnerableGroup.CHILDREN,
    "kids": VulnerableGroup.CHILDREN, "infants": VulnerableGroup.CHILDREN,
    "elderly": VulnerableGroup.ELDERLY, "old": VulnerableGroup.ELDERLY,
    "senior": VulnerableGroup.ELDERLY, "seniors": VulnerableGroup.ELDERLY,
    "aged": VulnerableGroup.ELDERLY, "old people": VulnerableGroup.ELDERLY,
    "pregnant": VulnerableGroup.PREGNANT, "pregnancy": VulnerableGroup.PREGNANT,
    "pregnant women": VulnerableGroup.PREGNANT,
    "disabled": VulnerableGroup.DISABLED, "disability": VulnerableGroup.DISABLED,
    "disabilities": VulnerableGroup.DISABLED,
    "injured": VulnerableGroup.INJURED, "wounded": VulnerableGroup.INJURED,
}

_NONE_WORDS = {"none", "no", "nil", "n a", "na", "not applicable", "nobody"}


def _norm(text):
    return " ".join(str(text).lower().replace("_", " ").replace("-", " ").split())


def _is_missing(value):
    return value is None or (isinstance(value, str) and not value.strip())


# ------------------------------------------------------------------- parsers
def parse_whole_number(raw, label):
    """Returns (int, None) or (None, plain-language error). Accepts '1,200'."""
    if isinstance(raw, bool):
        return None, "%s must be a whole number." % label
    if isinstance(raw, int):
        return raw, None
    if isinstance(raw, float):
        if raw.is_integer():
            return int(raw), None
        return None, "%s must be a whole number; I got %s. I won't round it for you." % (label, raw)
    text = str(raw).strip().replace(",", "").replace(" ", "")
    if re.fullmatch(r"-?\d+(\.0+)?", text):
        return int(float(text)), None
    return None, "%s must be a whole number; I got '%s'." % (label, str(raw).strip())


def parse_resource(raw):
    key = _norm(raw)
    if key in RESOURCE_ALIASES:
        return RESOURCE_ALIASES[key], None
    supported = ", ".join(r.value.replace("_", " ") for r in Resource)
    return None, "I don't recognise the resource '%s'. Supported: %s." % (str(raw).strip(), supported)


def parse_groups(raw):
    """Returns (list of VulnerableGroup, list of errors). Empty input = none reported."""
    if _is_missing(raw):
        return [], []
    if isinstance(raw, (list, tuple, set)):
        tokens = [str(t) for t in raw]
    else:
        tokens = re.split(r"[,;/&]|\band\b", str(raw), flags=re.IGNORECASE)
    groups, errors = [], []
    tokens = [_norm(t) for t in tokens if _norm(t)]
    if len(tokens) == 1 and tokens[0] in _NONE_WORDS:
        return [], []
    for t in tokens:
        if t in GROUP_ALIASES:
            g = GROUP_ALIASES[t]
            if g not in groups:
                groups.append(g)
        else:
            valid = ", ".join(g.value for g in VulnerableGroup)
            errors.append("I don't recognise the vulnerable group '%s'. Use any of: %s, or 'none'." % (t, valid))
    return groups, errors


# ---------------------------------------------------------------- main entry
def validate_request(raw, extra_sites=None, open_requests=None, created_at=None):
    """
    raw keys: site, resource, quantity, people_affected, severity,
              vulnerable_groups (optional), lat/lng/site_name/contact_name/
              contact_phone (only for registering an unknown site).
    open_requests: PENDING requests, used to reject duplicates (same site+resource).
    created_at: ISO string override (used by the demo seed to back-date waiting time).
    """
    if not isinstance(raw, dict):
        return ValidationResult(False, None, ["I could not read that request."]), None

    errors = []

    # 1. required fields: report everything missing in one go
    required = [
        ("site", "the site (name or id)"),
        ("resource", "the resource (water, medical kits, blankets or food packs)"),
        ("quantity", "the quantity"),
        ("people_affected", "the number of people affected"),
        ("severity", "the severity (1 = low ... 5 = critical)"),
    ]
    missing = [label for key, label in required if _is_missing(raw.get(key))]
    if missing:
        errors.append("I'm missing: " + "; ".join(missing) + ". I won't guess these.")

    # 2. site
    site, new_site = None, None
    if not _is_missing(raw.get("site")):
        site, candidates = lookup_site(raw["site"], extra_sites)
        if site is None and candidates:
            errors.append("'%s' matches several sites (%s). Which one do you mean?"
                          % (str(raw["site"]).strip(), ", ".join(candidates)))
        elif site is None:
            if _is_missing(raw.get("lat")) or _is_missing(raw.get("lng")):
                errors.append(
                    "I don't know the site '%s'. Use a registered site name or id, or send its "
                    "latitude and longitude so I can register it." % str(raw["site"]).strip())
            else:
                name = raw.get("site_name") if not _is_missing(raw.get("site_name")) else raw["site"]
                site, err = make_user_site(name, raw.get("lat"), raw.get("lng"),
                                           raw.get("contact_name", ""), raw.get("contact_phone", ""))
                if err:
                    errors.append(err)
                else:
                    new_site = site

    # 3. resource
    resource = None
    if not _is_missing(raw.get("resource")):
        resource, err = parse_resource(raw["resource"])
        if err:
            errors.append(err)

    # 4. quantity
    quantity = None
    if not _is_missing(raw.get("quantity")):
        quantity, err = parse_whole_number(raw["quantity"], "Quantity")
        if err:
            errors.append(err)
        elif quantity < 1:
            errors.append("Quantity must be at least 1.")
            quantity = None
        elif resource is not None and quantity > config.MAX_QUANTITY[resource.value]:
            errors.append("A request for %s %s looks like a typo (the limit per request is %s). "
                          "Please check and resend." % (format(quantity, ","), resource.unit,
                                                        format(config.MAX_QUANTITY[resource.value], ",")))
            quantity = None

    # 5. people affected
    people = None
    if not _is_missing(raw.get("people_affected")):
        people, err = parse_whole_number(raw["people_affected"], "People affected")
        if err:
            errors.append(err)
        elif people < 1:
            errors.append("People affected must be at least 1.")
            people = None
        elif people > config.MAX_PEOPLE_AFFECTED:
            errors.append("People affected (%s) is above the limit of %s. Please check the number."
                          % (format(people, ","), format(config.MAX_PEOPLE_AFFECTED, ",")))
            people = None

    # 6. severity
    severity = None
    if not _is_missing(raw.get("severity")):
        severity, err = parse_whole_number(raw["severity"], "Severity")
        if err:
            errors.append(err)
        elif not (config.SEVERITY_MIN <= severity <= config.SEVERITY_MAX):
            errors.append("Severity must be from %d (low) to %d (critical); I got %d."
                          % (config.SEVERITY_MIN, config.SEVERITY_MAX, severity))
            severity = None

    # 7. vulnerable groups (optional; empty means none were reported)
    groups, group_errors = parse_groups(raw.get("vulnerable_groups"))
    errors.extend(group_errors)

    # 8. duplicate open request for the same site + resource
    if site is not None and resource is not None and open_requests:
        for r in open_requests:
            if (r.site_id == site.site_id and r.resource == resource
                    and r.status == RequestStatus.PENDING):
                errors.append("%s already has an open %s request (%s). Wait for the next "
                              "allocation round or ask me to update that request."
                              % (site.name, resource.value.replace("_", " "), r.request_id))
                break

    if errors:
        return ValidationResult(False, None, errors), None

    kwargs = dict(site_id=site.site_id, resource=resource, quantity=quantity,
                  people_affected=people, severity=severity, vulnerable_groups=groups)
    if created_at:
        kwargs["created_at"] = created_at
    return ValidationResult(True, ResourceRequest(**kwargs), []), new_site


if __name__ == "__main__":
    good = {"site": "Hill Hamlet", "resource": "medical kits", "quantity": "60",
            "people_affected": "1,200", "severity": 3, "vulnerable_groups": "children and elderly"}
    res, ns = validate_request(good)
    assert res.ok and ns is None
    r = res.request
    assert (r.site_id, r.resource, r.quantity, r.people_affected) == ("S3", Resource.MEDICAL_KITS, 60, 1200)
    assert r.vulnerable_groups == [VulnerableGroup.CHILDREN, VulnerableGroup.ELDERLY]

    # missing fields are listed together, nothing invented
    res, _ = validate_request({"site": "S1", "resource": "water"})
    assert not res.ok and "quantity" in res.errors[0] and "severity" in res.errors[0]

    # bad values give plain-language reasons
    for patch, needle in [
        ({"severity": 9}, "from 1"), ({"quantity": 12.5}, "whole number"),
        ({"quantity": 0}, "at least 1"), ({"resource": "gold"}, "don't recognise"),
        ({"quantity": 999999}, "typo"), ({"people_affected": True}, "whole number"),
        ({"vulnerable_groups": "aliens"}, "vulnerable group"), ({"site": "Atlantis"}, "latitude"),
        ({"site": "shelter"}, "several sites"),
    ]:
        bad = {"site": "S1", "resource": "water", "quantity": 100, "people_affected": 50, "severity": 3}
        bad.update(patch)
        res, _ = validate_request(bad)
        assert not res.ok, patch
        assert any(needle in e for e in res.errors), (patch, res.errors)

    # unknown site accepted only with lat/lng, and returned for persistence
    res, ns = validate_request({"site": "New Camp", "lat": 26.5, "lng": 91.5, "resource": "water",
                                "quantity": 100, "people_affected": 50, "severity": 2})
    assert res.ok and ns is not None and ns.user_supplied_location and res.request.site_id == ns.site_id

    # duplicates rejected
    first, _ = validate_request({"site": "S1", "resource": "water", "quantity": 100,
                                 "people_affected": 50, "severity": 3})
    again, _ = validate_request({"site": "S1", "resource": "water", "quantity": 50,
                                 "people_affected": 50, "severity": 3}, open_requests=[first.request])
    assert not again.ok and "already has an open" in again.errors[0]

    # 'none' means no groups, not an error
    assert parse_groups("none") == ([], [])
    print("validation.py OK")