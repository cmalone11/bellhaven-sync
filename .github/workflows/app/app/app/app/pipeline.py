"""
Daily entrypoint. Read-only against the CRM (list_accounts only) -- it never
writes. Writing only ever happens from review_app.py's approve action.

Run:
    python pipeline.py

Safe to run any number of times per day: every proposal is keyed by a
deterministic dedupe_key (see matcher.py), and store.upsert_proposal() no-ops
on anything already pending or already decided.
"""
import sys
import time

from config import DB_PATH
from crm_client import CrmClient
from matcher import build_proposals
from scraper import scrape_all
from store import init_db, upsert_proposal


def run():
    init_db()
    print("[1/4] Scraping bellhavenseniorliving website...")
    locations = scrape_all()
    print(f"      -> {len(locations)} communities found on site")

    print("[2/4] Pulling CRM accounts...")
    crm = CrmClient()
    accounts = crm.list_accounts()
    print(f"      -> {len(accounts)} accounts in CRM")

    parent = crm.find_bellhaven_parent()
    if not parent:
        print("ERROR: could not find the Bellhaven parent account via name search 'bellhaven'. "
              "Check /api/docs for the correct filter, or hardcode its id in config.py.")
        sys.exit(1)
    print(f"      -> Bellhaven parent account id: {parent['id']}")

    print("[3/4] Matching + generating proposals...")
    proposals = build_proposals(locations, accounts, parent)
    print(f"      -> {len(proposals)} proposals generated")

    print("[4/4] Writing new proposals to local review queue (idempotent)...")
    counts = {"inserted": 0, "already_pending": 0, "already_decided": 0}
    for p in proposals:
        result = upsert_proposal(
            p["dedupe_key"], p["kind"], p["summary"], p["evidence"], p["actions"]
        )
        counts[result] += 1

    print(f"      -> inserted: {counts['inserted']}, "
          f"already pending: {counts['already_pending']}, "
          f"already decided (skipped): {counts['already_decided']}")
    print(f"\nRun complete at {time.strftime('%Y-%m-%d %H:%M:%S')}. "
          f"Open the review app to approve/reject: python review_app.py")


if __name__ == "__main__":
    run()
