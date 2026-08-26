"""
Shared configuration.

Everything here is overridable with environment variables so the same code
runs in CI (GitHub Actions) and locally without editing source.
"""
import os

CRM_BASE_URL = os.environ.get(
    "CRM_BASE_URL", "https://analyst-assessment-production.up.railway.app/api/v1"
)
SITE_BASE_URL = os.environ.get(
    "SITE_BASE_URL", "https://analyst-assessment-production.up.railway.app"
)
API_TOKEN = os.environ.get("BELLHAVEN_API_TOKEN", "")  # never hardcode the token

# Local state DB. This is what makes reruns idempotent -- it remembers every
# proposal we've ever generated (by a stable dedupe key) and every decision a
# human reviewer made on it.
DB_PATH = os.environ.get("BELLHAVEN_DB_PATH", "data/state.db")
SCRAPE_CACHE_PATH = os.environ.get("SCRAPE_CACHE_PATH", "data/scraped_locations.json")

# Fuzzy-match thresholds (0-100, via rapidfuzz token_sort_ratio on the
# normalized facility name), tuned against the 120-account sandbox.
NAME_MATCH_CONFIDENT = 90   # >= this + same city/state -> confident match
NAME_MATCH_LIKELY = 70      # >= this -> candidate, but flagged for review as needs_fix
DUPLICATE_NAME_THRESHOLD = 92  # two CRM accounts this similar in name+city are treated as dupes

REQUEST_TIMEOUT = 20
