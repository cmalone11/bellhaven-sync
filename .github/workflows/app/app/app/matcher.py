"""
Core matching logic. Pure functions that take (scraped locations, CRM
accounts) and return a list of proposals. No network calls, no writes --
this module is unit-testable in isolation.

Classifications produced (the `kind` field on each proposal):

  confident_match       Website location maps cleanly to one CRM account
                         under the Bellhaven parent, and the CRM record
                         already looks correct (name/city/state/care match).
                         We still emit a proposal (e.g. to refresh
                         care_offerings or phone) but it's low-risk.

  needs_fix              Matched, but something on the CRM account is wrong:
                         stale name, wrong/missing parent_id, wrong
                         city/state. Proposed action fixes the fields (or
                         performs a CHOW if billing history requires it --
                         see chow logic below).

  new_location            Website location has no corresponding CRM account
                         at all. Proposed action creates one under the
                         Bellhaven parent, status Active.

  orphaned_crm_account   A CRM account lives under the Bellhaven parent but
                         no matching website location exists anymore.
                         Proposed action sets status to "Needs Review" with
                         a note -- we never auto-delete or auto-deactivate,
                         since a facility can be temporarily off the website
                         (renovation, re-brand) without having actually
                         closed. A human makes the final call.

  duplicate               Two CRM accounts under Bellhaven that clearly
                         describe the same physical facility (near-identical
                         name + same city/state). Proposed action marks the
                         newer/thinner record as the loser: duplicate_of ->
                         survivor id, status Inactive.
"""
import re
from rapidfuzz import fuzz

from config import NAME_MATCH_CONFIDENT, NAME_MATCH_LIKELY, DUPLICATE_NAME_THRESHOLD

STATE_ABBR = {
    "ohio": "OH", "michigan": "MI", "indiana": "IN", "pennsylvania": "PA",
}


def normalize_name(name: str) -> str:
    if not name:
        return ""
    n = name.lower()
    n = re.sub(r"[^a-z0-9 ]", " ", n)
    # strip generic corporate/care-type suffixes that add noise to matching
    for noise in [
        "bellhaven", "healthcare centre", "rehabilitation & nursing",
        "rehabilitation and nursing", "of", "at", "the", "arbors", "-",
    ]:
        n = n.replace(noise, " ")
    return re.sub(r"\s+", " ", n).strip()


def normalize_state(state: str) -> str:
    if not state:
        return ""
    s = state.strip()
    return STATE_ABBR.get(s.lower(), s.upper())


def name_score(a: str, b: str) -> float:
    return fuzz.token_sort_ratio(normalize_name(a), normalize_name(b))


def find_best_crm_match(location: dict, crm_accounts: list[dict]) -> tuple[dict | None, float]:
    """Best-scoring CRM account for a scraped location, using name similarity
    with a bonus/penalty for city agreement (city is a strong disambiguator
    since Bellhaven reuses naming patterns like "Bellhaven of <Town>")."""
    best, best_score = None, 0.0
    for acct in crm_accounts:
        score = name_score(location.get("name", ""), acct.get("name", ""))
        same_city = (
            location.get("city")
            and acct.get("city")
            and location["city"].strip().lower() == acct["city"].strip().lower()
        )
        if same_city:
            score += 8  # nudge, doesn't let a bad name match win outright
        else:
            score -= 5
        if score > best_score:
            best, best_score = acct, score
    return best, min(best_score, 100)


def fields_to_fix(location: dict, acct: dict, bellhaven_parent_id: str) -> dict:
    """Diff a matched pair and return only the fields that actually differ."""
    fixes = {}
    if acct.get("name") != location.get("name"):
        fixes["name"] = location["name"]
    if acct.get("parent_id") != bellhaven_parent_id:
        fixes["parent_id"] = bellhaven_parent_id
    if location.get("address") and acct.get("address") != location.get("address"):
        fixes["address"] = location["address"]
    if location.get("city") and acct.get("city") != location.get("city"):
        fixes["city"] = location["city"]
    if location.get("state") and normalize_state(acct.get("state", "")) != normalize_state(
        location.get("state", "")
    ):
        fixes["state"] = normalize_state(location["state"])
    if location.get("zip") and acct.get("zip") != location.get("zip"):
        fixes["zip"] = location["zip"]
    website_care = sorted(location.get("care_offerings") or [])
    crm_care = sorted(acct.get("care_offerings") or acct.get("services") or [])
    if website_care and website_care != crm_care:
        fixes["care_offerings"] = website_care
    return fixes


def needs_chow(acct: dict) -> bool:
    """Billing SOP: preserve the old account instead of re-parenting it when
    it has real revenue history AND currently-outstanding AR."""
    revenue = acct.get("lifetime_revenue") or 0
    ar = acct.get("outstanding_ar") or 0
    return revenue > 0 and ar > 0


def build_proposals(locations: list[dict], crm_accounts: list[dict], bellhaven_parent: dict):
    """Returns a list of proposal dicts: {dedupe_key, kind, summary, evidence, actions}."""
    parent_id = bellhaven_parent["id"]
    bellhaven_accounts = [a for a in crm_accounts if a.get("parent_id") == parent_id]
    proposals = []
    matched_account_ids = set()

    # ---- 1. website -> CRM: confident_match / needs_fix / new_location -----
    for loc in locations:
        if loc.get("error"):
            continue  # scrape failure, nothing to propose -- surfaced separately in run log
        candidates = bellhaven_accounts + [
            a for a in crm_accounts if a["id"] not in {x["id"] for x in bellhaven_accounts}
        ]
        best, score = find_best_crm_match(loc, candidates)

        if best is None or score < NAME_MATCH_LIKELY:
            # ---- new_location ----
            dedupe_key = f"new:{loc['slug']}"
            proposals.append(
                {
                    "dedupe_key": dedupe_key,
                    "kind": "new_location",
                    "summary": f"Create CRM account for '{loc['name']}' ({loc.get('city')}, {loc.get('state')})",
                    "evidence": {"website_location": loc, "best_candidate": best, "score": score},
                    "actions": [
                        {
                            "op": "create_account",
                            "fields": {
                                "name": loc["name"],
                                "parent_id": parent_id,
                                "address": loc.get("address"),
                                "city": loc.get("city"),
                                "state": normalize_state(loc.get("state", "")),
                                "zip": loc.get("zip"),
                                "care_offerings": loc.get("care_offerings"),
                                "status": "Active",
                                "note": f"Auto-created from website scrape {loc['url']}",
                            },
                        }
                    ],
                }
            )
            continue

        matched_account_ids.add(best["id"])
        fixes = fields_to_fix(loc, best, parent_id)
        parent_changing = "parent_id" in fixes

        if parent_changing and needs_chow(best):
            # ---- CHOW: don't touch the old account's parent. Create a new
            # account under the correct parent and link back via
            # chow_current_account, leaving the old one exactly as-is aside
            # from that link (and a note for the audit trail). ----
            dedupe_key = f"chow:{best['id']}:{loc['slug']}"
            new_fields = {
                "name": loc["name"],
                "parent_id": parent_id,
                "address": loc.get("address"),
                "city": loc.get("city"),
                "state": normalize_state(loc.get("state", "")),
                "zip": loc.get("zip"),
                "care_offerings": loc.get("care_offerings"),
                "status": "Active",
                "note": f"Created via CHOW from account {best['id']} ({best.get('name')}); "
                        f"old account preserved for billing history.",
            }
            proposals.append(
                {
                    "dedupe_key": dedupe_key,
                    "kind": "chow",
                    "summary": (
                        f"CHOW: '{best.get('name')}' has revenue history + outstanding AR "
                        f"(${best.get('lifetime_revenue')} / ${best.get('outstanding_ar')} AR). "
                        f"Preserve old account, create new account under correct parent."
                    ),
                    "evidence": {
                        "website_location": loc,
                        "old_account": best,
                        "reason": "lifetime_revenue > 0 AND outstanding_ar > 0",
                    },
                    "actions": [
                        {"op": "create_account", "fields": new_fields, "bind_result_as": "new_account"},
                        {
                            "op": "patch_account",
                            "account_id": best["id"],
                            "fields": {"chow_current_account": "$new_account.id"},
                            "note_append": "CHOW performed by sync pipeline; new account created "
                                           "under correct parent, old account left otherwise unchanged.",
                        },
                    ],
                }
            )
            continue

        if fixes:
            # ---- needs_fix (safe re-parent / field corrections) ----
            dedupe_key = f"fix:{best['id']}:" + ",".join(sorted(fixes.keys()))
            proposals.append(
                {
                    "dedupe_key": dedupe_key,
                    "kind": "needs_fix",
                    "summary": f"Update {', '.join(fixes.keys())} on '{best.get('name')}' -> matches website '{loc['name']}'",
                    "evidence": {
                        "website_location": loc,
                        "crm_account": best,
                        "match_score": score,
                        "proposed_fields": fixes,
                    },
                    "actions": [{"op": "patch_account", "account_id": best["id"], "fields": fixes}],
                }
            )
        else:
            # ---- confident_match, nothing to change; still logged so a
            # reviewer can see coverage, but low priority. We still write a
            # no-op-ish proposal only if there's *something* worth a note;
            # otherwise skip to avoid queue noise. ----
            if score < 100:
                dedupe_key = f"confirm:{best['id']}"
                proposals.append(
                    {
                        "dedupe_key": dedupe_key,
                        "kind": "confident_match",
                        "summary": f"Confirm match: '{loc['name']}' == CRM '{best.get('name')}' (score {score:.0f})",
                        "evidence": {"website_location": loc, "crm_account": best, "match_score": score},
                        "actions": [],  # informational only, approving just marks it reviewed
                    }
                )

    # ---- 2. orphaned CRM accounts (under Bellhaven, no website match) -----
    matched_website_slugs = {p["evidence"]["website_location"]["slug"] for p in proposals if "website_location" in p["evidence"]}
    for acct in bellhaven_accounts:
        if acct["id"] in matched_account_ids:
            continue
        if acct.get("status") == "Inactive" and acct.get("duplicate_of_account"):
            continue  # already resolved as a duplicate previously
        dedupe_key = f"orphan:{acct['id']}"
        proposals.append(
            {
                "dedupe_key": dedupe_key,
                "kind": "orphaned_crm_account",
                "summary": f"'{acct.get('name')}' is under Bellhaven in the CRM but not on the website anymore",
                "evidence": {"crm_account": acct},
                "actions": [
                    {
                        "op": "patch_account",
                        "account_id": acct["id"],
                        "fields": {"status": "Needs Review"},
                        "note_append": "No matching community found on bellhaven site during automated sync; "
                                       "confirm whether this facility closed, rebranded, or was temporarily delisted.",
                    }
                ],
            }
        )

    # ---- 3. duplicate detection among Bellhaven accounts -------------------
    for i, a in enumerate(bellhaven_accounts):
        for b in bellhaven_accounts[i + 1:]:
            if a.get("status") == "Inactive" or b.get("status") == "Inactive":
                continue
            same_city = (a.get("city") or "").strip().lower() == (b.get("city") or "").strip().lower()
            score = name_score(a.get("name", ""), b.get("name", ""))
            if same_city and score >= DUPLICATE_NAME_THRESHOLD:
                # keep the one with more history/completeness as survivor
                def completeness(x):
                    return (
                        (x.get("lifetime_revenue") or 0) + (x.get("outstanding_ar") or 0),
                        len([v for v in x.values() if v]),
                    )
                survivor, loser = (a, b) if completeness(a) >= completeness(b) else (b, a)
                dedupe_key = f"dup:{loser['id']}:{survivor['id']}"
                proposals.append(
                    {
                        "dedupe_key": dedupe_key,
                        "kind": "duplicate",
                        "summary": f"'{loser.get('name')}' looks like a duplicate of '{survivor.get('name')}' (name score {score:.0f})",
                        "evidence": {
                            "loser_account": loser,
                            "survivor_account": survivor,
                            "name_score": score,
                        },
                        "actions": [
                            {
                                "op": "patch_account",
                                "account_id": loser["id"],
                                "fields": {
                                    "duplicate_of_account": survivor["id"],
                                    "status": "Inactive",
                                },
                                "note_append": f"Marked duplicate of {survivor['id']} by automated sync (name+city match).",
                            }
                        ],
                    }
                )

    return proposals
