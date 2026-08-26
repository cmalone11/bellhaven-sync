"""
Small local review app.

    python review_app.py
    open http://127.0.0.1:5050

Shows every pending proposal with its supporting evidence (the scraped
website data and/or the CRM account(s) involved) side by side, and lets a
reviewer Approve or Reject. Approving is the ONLY code path in this whole
project that calls a write endpoint on the CRM API.
"""
import json

from flask import Flask, redirect, render_template_string, request, url_for

from crm_client import CrmClient
from store import decide_proposal, get_proposal, init_db, list_proposals

app = Flask(__name__)
init_db()

REVIEWER_NAME = "local-reviewer"  # swap for real auth/username if this ever goes multi-user

PAGE = """
<!doctype html>
<html>
<head>
<title>Bellhaven CRM Sync — Review Queue</title>
<style>
  body { font-family: -apple-system, Helvetica, Arial, sans-serif; max-width: 980px; margin: 30px auto; color: #1c1c1e; }
  h1 { font-size: 22px; }
  .tabs a { margin-right: 14px; text-decoration: none; color: #555; }
  .tabs a.active { color: #111; font-weight: 600; border-bottom: 2px solid #111; }
  .card { border: 1px solid #ddd; border-radius: 10px; padding: 16px 18px; margin-bottom: 16px; }
  .kind { display:inline-block; font-size: 11px; text-transform: uppercase; letter-spacing: .04em;
          background:#eee; border-radius: 5px; padding: 2px 8px; margin-bottom: 8px; }
  .kind.needs_fix, .kind.chow { background:#fff3cd; }
  .kind.new_location { background:#d4edda; }
  .kind.orphaned_crm_account { background:#f8d7da; }
  .kind.duplicate { background:#f8d7da; }
  .kind.confident_match { background:#e2e3e5; }
  .summary { font-size: 15px; font-weight: 600; margin: 4px 0 10px; }
  pre { background:#f6f6f7; padding: 10px 12px; border-radius: 6px; overflow-x:auto; font-size: 12px; }
  .actions button { padding: 7px 16px; border-radius: 6px; border: 1px solid #ccc; cursor:pointer; margin-right: 8px; }
  .approve { background:#111; color:#fff; border-color:#111 !important; }
  .reject { background:#fff; }
  .empty { color:#888; padding: 40px 0; text-align:center; }
  .decided { font-size: 12px; color: #888; margin-top: 8px;}
</style>
</head>
<body>
<h1>Bellhaven CRM Sync — Review Queue</h1>
<div class="tabs">
  <a href="{{ url_for('index', status='pending') }}" class="{{ 'active' if status=='pending' else '' }}">Pending ({{ counts.pending }})</a>
  <a href="{{ url_for('index', status='approved') }}" class="{{ 'active' if status=='approved' else '' }}">Approved ({{ counts.approved }})</a>
  <a href="{{ url_for('index', status='rejected') }}" class="{{ 'active' if status=='rejected' else '' }}">Rejected ({{ counts.rejected }})</a>
</div>
<hr>
{% if not proposals %}
  <div class="empty">Nothing here. Run <code>python pipeline.py</code> to generate proposals.</div>
{% endif %}
{% for p in proposals %}
  <div class="card">
    <span class="kind {{ p.kind }}">{{ p.kind.replace('_',' ') }}</span>
    <div class="summary">{{ p.summary }}</div>
    <details>
      <summary>Evidence</summary>
      <pre>{{ p.evidence_pretty }}</pre>
    </details>
    <details>
      <summary>Proposed API action(s)</summary>
      <pre>{{ p.actions_pretty }}</pre>
    </details>
    {% if status == 'pending' %}
    <div class="actions" style="margin-top:10px;">
      <form style="display:inline" method="post" action="{{ url_for('approve', proposal_id=p.id) }}">
        <button class="approve" type="submit">Approve &amp; apply</button>
      </form>
      <form style="display:inline" method="post" action="{{ url_for('reject', proposal_id=p.id) }}">
        <button class="reject" type="submit">Reject</button>
      </form>
    </div>
    {% else %}
      <div class="decided">{{ p.status }} by {{ p.decided_by }} — {{ p.decided_at_fmt }}</div>
      {% if p.result_json %}<details><summary>API result</summary><pre>{{ p.result_pretty }}</pre></details>{% endif %}
    {% endif %}
  </div>
{% endfor %}
</body>
</html>
"""


def _decorate(rows):
    import time
    out = []
    for r in rows:
        r = dict(r)
        r["evidence_pretty"] = json.dumps(json.loads(r["evidence_json"]), indent=2)
        r["actions_pretty"] = json.dumps(json.loads(r["actions_json"]), indent=2)
        r["result_pretty"] = json.dumps(json.loads(r["result_json"]), indent=2) if r.get("result_json") else ""
        r["decided_at_fmt"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["decided_at"])) if r.get("decided_at") else ""
        out.append(r)
    return out


@app.route("/")
def index():
    status = request.args.get("status", "pending")
    all_rows = list_proposals()
    counts = {
        "pending": sum(1 for r in all_rows if r["status"] == "pending"),
        "approved": sum(1 for r in all_rows if r["status"] == "approved"),
        "rejected": sum(1 for r in all_rows if r["status"] == "rejected"),
    }
    rows = [r for r in all_rows if r["status"] == status]
    return render_template_string(PAGE, proposals=_decorate(rows), status=status, counts=counts)


def _apply_actions(actions: list) -> dict:
    """Executes the proposal's action list against the live CRM. Supports a
    tiny templating convention ("$new_account.id") so a CHOW's second step
    can reference the account just created in its first step."""
    crm = CrmClient()
    bound = {}
    results = []
    for action in actions:
        op = action["op"]
        if op == "create_account":
            created = crm.create_account(action["fields"])
            results.append(created)
            if "bind_result_as" in action:
                bound[action["bind_result_as"]] = created
        elif op == "patch_account":
            fields = {}
            for k, v in action["fields"].items():
                if isinstance(v, str) and v.startswith("$"):
                    ref, _, attr = v[1:].partition(".")
                    fields[k] = bound[ref][attr]
                else:
                    fields[k] = v
            if action.get("note_append"):
                fields["note"] = action["note_append"]
            results.append(crm.patch_account(action["account_id"], fields))
        else:
            raise ValueError(f"Unknown op {op}")
    return {"results": results}


@app.route("/approve/<int:proposal_id>", methods=["POST"])
def approve(proposal_id):
    proposal = get_proposal(proposal_id)
    if not proposal:
        return "not found", 404
    actions = json.loads(proposal["actions_json"])
    result = _apply_actions(actions) if actions else {"note": "informational, no write performed"}
    decide_proposal(proposal_id, "approved", REVIEWER_NAME, result)
    return redirect(url_for("index", status="pending"))


@app.route("/reject/<int:proposal_id>", methods=["POST"])
def reject(proposal_id):
    decide_proposal(proposal_id, "rejected", REVIEWER_NAME)
    return redirect(url_for("index", status="pending"))


if __name__ == "__main__":
    app.run(port=5050, debug=True)
