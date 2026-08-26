"""
Thin wrapper around the Bellhaven CRM sandbox API.

Every write (PATCH/POST) in this module is called from exactly one place in
the whole project: apply_proposal() in review_app.py, which only runs after a
human clicks Approve. The pipeline and matcher never write -- they only read
and propose. That separation is what guarantees "nothing writes without
approval."
"""
import requests

from config import CRM_BASE_URL, API_TOKEN, REQUEST_TIMEOUT


class CrmClient:
    def __init__(self, base_url: str = CRM_BASE_URL, token: str = API_TOKEN):
        if not token:
            raise RuntimeError(
                "Set BELLHAVEN_API_TOKEN before running (export BELLHAVEN_API_TOKEN=...)."
            )
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update(
            {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        )

    # ---- reads ----------------------------------------------------------
    def list_accounts(self, **params) -> list[dict]:
        """Paginated GET /accounts, follows pagination until exhausted."""
        accounts = []
        page = 1
        while True:
            resp = self.session.get(
                f"{self.base_url}/accounts",
                params={**params, "page": page, "page_size": 100},
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            body = resp.json()
            batch = body.get("results", body if isinstance(body, list) else [])
            accounts.extend(batch)
            if not batch or not body.get("next"):
                break
            page += 1
        return accounts

    def get_account(self, account_id: str) -> dict:
        resp = self.session.get(f"{self.base_url}/accounts/{account_id}", timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json()

    def find_bellhaven_parent(self) -> dict | None:
        """Locate the Bellhaven corporate parent account by name search."""
        for acct in self.list_accounts(q="bellhaven"):
            name = (acct.get("name") or "").lower()
            if name.strip() in {"bellhaven senior living", "bellhaven"} and not acct.get(
                "parent_id"
            ):
                return acct
        return None

    # ---- writes (approval-gated) ----------------------------------------
    def patch_account(self, account_id: str, fields: dict) -> dict:
        resp = self.session.patch(
            f"{self.base_url}/accounts/{account_id}", json=fields, timeout=REQUEST_TIMEOUT
        )
        resp.raise_for_status()
        return resp.json()

    def create_account(self, fields: dict) -> dict:
        resp = self.session.post(
            f"{self.base_url}/accounts", json=fields, timeout=REQUEST_TIMEOUT
        )
        resp.raise_for_status()
        return resp.json()
