"""One-off: purge palindrome / promo_banner / day-0 dry-run tasks from the
prod task queue. Keeps the 5 genuine ops tasks. Run:  uv run python clean_tasks.py
"""
from google.cloud import firestore

col = firestore.Client(project="operation-sunrise").collection("stagenator_tasks")

deleted = 0
for d in col.stream():
    t = d.to_dict()
    junk = (
        t.get("game") == "palindrome"
        or t.get("type") in ("promo_banner", "faultdrill")
        or str(t.get("created") or "")[:10] == "2026-08-24"   # day-0 dry runs
    )
    if junk:
        col.document(d.id).delete()
        deleted += 1

print("deleted junk tasks:", deleted)
print("remaining:", [f"{x.to_dict().get('type')}/{x.to_dict().get('game')}"
                     for x in col.stream()])
