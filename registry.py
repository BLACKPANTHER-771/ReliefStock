"""
registry.py - ReliefStock AI: the site and depot registry (fictional demo data).

Hosted agents cannot rely on reading sites.json / depots.json, so the registry
is plain Python data here. The same data can be exported to JSON for the repo.

All coordinates and names below are FICTIONAL demo data (region: a made-up
flood-hit river valley). Real deployments must replace them.

Rules (from the project brief):
  * a known site is found by id or by name;
  * an unknown site is rejected UNLESS the coordinator supplies lat/lng;
  * nothing is invented: no guessed coordinates, no guessed matches.

Pure functions, no storage and no network. Sites added by coordinators at
runtime are passed in as `extra_sites` (store.py will persist them).
"""

from models import Depot, Site, new_id

# ------------------------------------------------------------------ DEMO DATA
_SITES = [
    # site_id, name, lat, lng, contact_name, contact_phone
    ("S1", "Riverside Relief Camp",    26.2000, 91.7000, "Anita Borah",   "+91 90000 00001"),
    ("S2", "District Hospital",        26.1700, 91.7600, "Dr. R. Sen",    "+91 90000 00002"),
    ("S3", "Hill Hamlet",              26.0200, 91.9800, "Mohan Das",     "+91 90000 00003"),
    ("S4", "Primary School Shelter",   26.3000, 91.6300, "Pooja Kalita",  "+91 90000 00004"),
    ("S5", "Old Age Home",             26.1400, 91.8200, "Sister Mary",   "+91 90000 00005"),
    ("S6", "Market Town Shelter",      26.4600, 91.4600, "Imran Ali",     "+91 90000 00006"),
    ("S7", "Char Island Village",      26.2300, 91.9500, "Lakhi Devi",    "+91 90000 00007"),
    ("S8", "Tea Garden Settlement",    26.3800, 92.0500, "Rupam Gogoi",   "+91 90000 00008"),
    ("S9", "Railway Colony Camp",      26.1600, 91.6900, "Deepak Nath",   "+91 90000 00009"),
]

_DEPOTS = [
    # depot_id, name, lat, lng, contact_name, contact_phone, resources held
    ("D1", "Central Warehouse", 26.1800, 91.7500, "Warehouse Desk A", "+91 91111 00001",
     ["water", "medical_kits", "blankets", "food_packs"]),
    ("D2", "North Depot",       26.4400, 91.4400, "Warehouse Desk B", "+91 91111 00002",
     ["water", "blankets", "food_packs"]),
    ("D3", "East Depot",        26.2500, 92.3400, "Warehouse Desk C", "+91 91111 00003",
     ["water", "medical_kits", "food_packs"]),
]

SITES = {row[0]: Site(row[0], row[1], row[2], row[3], row[4], row[5]) for row in _SITES}
DEPOTS = {row[0]: Depot(row[0], row[1], row[2], row[3], row[4], row[5], list(row[6]))
          for row in _DEPOTS}


# ------------------------------------------------------------------- helpers
def _norm(text):
    """Lower-case and collapse whitespace so 'hill  HAMLET ' == 'hill hamlet'."""
    return " ".join(str(text).lower().split())


def all_sites(extra_sites=None):
    """Registry sites plus runtime-added ones (extra wins on id clash)."""
    merged = dict(SITES)
    if extra_sites:
        merged.update(extra_sites)
    return merged


def lookup_site(query, extra_sites=None):
    """
    Find a site by id or name.
    Returns (site, candidates):
      (Site, [])        exactly one match
      (None, [names])   ambiguous: several names contain the query
      (None, [])        unknown site
    Never guesses: a partial name must match exactly one site.
    """
    if query is None or not str(query).strip():
        return None, []
    sites = all_sites(extra_sites)
    q = _norm(query)

    # 1. exact id
    for s in sites.values():
        if _norm(s.site_id) == q:
            return s, []
    # 2. exact name
    for s in sites.values():
        if _norm(s.name) == q:
            return s, []
    # 3. unique partial name (need >= 3 chars so "a" cannot match half the registry)
    if len(q) >= 3:
        hits = [s for s in sites.values() if q in _norm(s.name)]
        if len(hits) == 1:
            return hits[0], []
        if len(hits) > 1:
            return None, sorted(s.name for s in hits)
    return None, []


def make_user_site(name, lat, lng, contact_name="", contact_phone=""):
    """
    Build a Site from coordinator-supplied details (unknown-site rule).
    Returns (site, error). Exactly one of them is None.
    """
    name = " ".join(str(name or "").split())
    if len(name) < 2:
        return None, "I need a name for the new site."
    try:
        lat_f = float(lat)
        lng_f = float(lng)
    except (TypeError, ValueError):
        return None, "Latitude and longitude must be numbers, for example 26.14, 91.73."
    if not (-90.0 <= lat_f <= 90.0):
        return None, "Latitude must be between -90 and 90."
    if not (-180.0 <= lng_f <= 180.0):
        return None, "Longitude must be between -180 and 180."
    if lat_f == 0.0 and lng_f == 0.0:
        return None, "0, 0 is not a real location. Please send the site's actual coordinates."
    site = Site(
        site_id=new_id("SITE"), name=name, lat=lat_f, lng=lng_f,
        contact_name=str(contact_name or "").strip(),
        contact_phone=str(contact_phone or "").strip(),
        user_supplied_location=True,
    )
    return site, None


def depots_holding(resource_value):
    """Depots that physically hold this resource (resource_value is a string)."""
    return [d for d in DEPOTS.values() if resource_value in d.resources]


if __name__ == "__main__":
    assert len(SITES) == 9 and len(DEPOTS) == 3
    assert lookup_site("s3")[0].name == "Hill Hamlet"
    assert lookup_site("  hill   HAMLET ")[0].site_id == "S3"
    assert lookup_site("hospital")[0].site_id == "S2"            # unique partial
    s, cands = lookup_site("shelter")                             # two names contain it
    assert s is None and len(cands) == 2, cands
    assert lookup_site("Atlantis") == (None, [])
    assert lookup_site("a") == (None, [])                         # too short to guess
    ok, err = make_user_site("New Camp", "26.5", "91.5")
    assert ok and err is None and ok.user_supplied_location
    assert lookup_site("new camp", {ok.site_id: ok})[0].site_id == ok.site_id
    assert make_user_site("X", "abc", "1")[1] is not None
    assert make_user_site("New Camp", "95", "10")[1] is not None
    assert make_user_site("New Camp", "0", "0")[1] is not None
    assert [d.depot_id for d in depots_holding("medical_kits")] == ["D1", "D3"]
    print("registry.py OK ->", len(SITES), "sites,", len(DEPOTS), "depots")