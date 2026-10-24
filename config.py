"""
config.py - ReliefStock AI: every tunable number lives here.

Nothing else in the project hard-codes a weight, limit or threshold, so the
scoring and the 30% floor can be explained (and changed) in one place.
No `from __future__ import annotations` (breaks dataclasses on Agentverse).
"""

import os
import re
import shlex
from pathlib import Path

ASI1_API_KEY_ENV_VAR = "ASI1_API_KEY"


def load_local_environment(env_path=None):
    """Load simple KEY=VALUE entries from the project .env without overriding the process."""
    try:
        if env_path:
            path = Path(env_path)
        else:
            path = Path(".env")
            if not path.is_file():
                try:
                    if "__file__" in globals() and globals()["__file__"]:
                        path = Path(__file__).resolve().parent / ".env"
                except Exception:
                    pass
        if not path.is_file():
            return

        with path.open(encoding="utf-8") as env_file:
            for line_number, line in enumerate(env_file, start=1):
                entry = line.strip()
                if not entry or entry.startswith("#"):
                    continue
                if entry.startswith("export "):
                    entry = entry[len("export "):].lstrip()
                name, separator, value = entry.partition("=")
                name = name.strip()
                if not separator or not name.isidentifier():
                    continue
                value = value.strip()
                if value.startswith(("'", '"')):
                    try:
                        parsed = shlex.split(value, comments=True, posix=True)
                    except ValueError:
                        continue
                    if len(parsed) != 1:
                        continue
                    value = parsed[0]
                else:
                    value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
                os.environ.setdefault(name, value)
    except Exception:
        pass

# ---------------------------------------------------------------- Phase 1
# Validation limits. Values outside these are rejected, never "fixed".
SEVERITY_MIN = 1
SEVERITY_MAX = 5
MAX_PEOPLE_AFFECTED = 500000
# Sanity ceiling per resource (keys are Resource enum values). A typo such as
# an extra zero is rejected with a plain-language reason.
MAX_QUANTITY = {
    "water": 200000,        # litres
    "medical_kits": 5000,   # kits
    "blankets": 20000,      # pieces
    "food_packs": 20000,    # packs
}

# ---------------------------------------------------------------- Phase 2
# Priority score = severity + vulnerability + people + wait - prior allocation.
# Distance is deliberately NOT here: it must never affect priority.
SEVERITY_WEIGHT = 10.0          # severity 1..5 -> 10..50 points

VULNERABILITY_POINTS = {        # per group present (keys: VulnerableGroup values)
    "children": 6.0,
    "elderly": 6.0,
    "pregnant": 8.0,
    "disabled": 6.0,
    "injured": 8.0,
}
VULNERABILITY_CAP = 20.0        # so a long list cannot outweigh severity

PEOPLE_PER_POINT = 100.0        # 1 point per 100 people affected ...
PEOPLE_CAP = 15.0               # ... up to 15 points

WAIT_POINTS_PER_HOUR = 1.0      # 1 point per hour since the request was made ...
WAIT_CAP = 12.0                 # ... up to 12 points

PRIOR_PENALTY_MAX = 15.0        # max points subtracted for a site that already
                                # received a large share in earlier rounds

# ---------------------------------------------------------------- Phase 3
MIN_SHARE_FRACTION = 0.30       # every request that can be served gets >= 30%
                                # of what it asked for (stock permitting)

# ------------------------------------------------------- Phase 4-6 (routing)
AVG_SPEED_KMH = 40.0            # used only by the straight-line fallback
ROAD_FACTOR = 1.3               # straight-line km -> rough road km
DELAY_THRESHOLD_MIN = 20        # traffic delay above this triggers rerouting
HTTP_TIMEOUT_S = 8

OSRM_BASE_URL = "https://router.project-osrm.org"   # public demo server
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
# Nominatim's usage policy requires an identifying User-Agent.
NOMINATIM_USER_AGENT = "ReliefStockAI-hackathon/1.0"

# API keys are read from environment variables (Agentverse: "Agent Secrets").
# Never write a key into the code: the repo is public.
GOOGLE_MAPS_ENV_VAR = "GOOGLE_MAPS_API_KEY"


def google_maps_key():
    """Returns the Google key, or None (routing then falls back to OSRM)."""
    return os.environ.get(GOOGLE_MAPS_ENV_VAR) or None


def validate_config():
    """Raises ValueError if a constant is nonsensical. Returns True if fine."""
    if not (0 < MIN_SHARE_FRACTION <= 1):
        raise ValueError("MIN_SHARE_FRACTION must be in (0, 1]")
    if SEVERITY_MIN >= SEVERITY_MAX:
        raise ValueError("SEVERITY_MIN must be below SEVERITY_MAX")
    for name in ("SEVERITY_WEIGHT", "VULNERABILITY_CAP", "PEOPLE_PER_POINT",
                 "PEOPLE_CAP", "WAIT_POINTS_PER_HOUR", "WAIT_CAP",
                 "PRIOR_PENALTY_MAX", "AVG_SPEED_KMH", "ROAD_FACTOR"):
        if globals()[name] < 0:
            raise ValueError(name + " must not be negative")
    if PEOPLE_PER_POINT == 0:
        raise ValueError("PEOPLE_PER_POINT must not be zero")
    return True


if __name__ == "__main__":
    validate_config()
    print("config.py OK -> floor", MIN_SHARE_FRACTION,
          "| google key set:", google_maps_key() is not None)