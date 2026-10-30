#!/usr/bin/env python3
"""Generate Quorum's deterministic, mutation-seeded code-review corpus."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


PACK_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Seed:
    id: str
    category: str
    split: str
    clean: str
    before: str | None = None
    after: str | None = None
    anchor: str | None = None


SEEDS = [
    Seed(
        "cal-sql-01", "sql_injection", "calibration",
        '''def find_customer(db, email):
    query = "SELECT * FROM customers WHERE email = ?"
    return db.execute(query, (email,)).fetchone()
''',
        'query = "SELECT * FROM customers WHERE email = ?"',
        'query = "SELECT * FROM customers WHERE email = \'" + email + "\'"',
        'query = "SELECT * FROM customers WHERE email = \'" + email + "\'"',
    ),
    Seed(
        "cal-off-by-one-01", "off_by_one", "calibration",
        '''def take_prefix(records, count):
    return records[:count]
''',
        "records[:count]", "records[:count - 1]", "records[:count - 1]",
    ),
    Seed(
        "cal-missing-await-01", "missing_await", "calibration",
        '''async def send_alert(client, payload):
    await client.publish("alerts", payload)
    return True
''',
        'await client.publish("alerts", payload)',
        'client.publish("alerts", payload)',
        'client.publish("alerts", payload)',
    ),
    Seed(
        "cal-unbounded-01", "unbounded_query", "calibration",
        '''def load_recent_events(db, owner_id, limit):
    cursor = db.execute("SELECT * FROM events WHERE owner_id = ?", (owner_id,))
    return cursor.fetchmany(limit)
''',
        "cursor.fetchmany(limit)", "cursor.fetchall()", "cursor.fetchall()",
    ),
    Seed(
        "cal-auth-01", "missing_authorization", "calibration",
        '''def get_invoice(user, invoice, policy):
    if not policy.can_read(user, invoice):
        raise PermissionError("invoice access denied")
    return invoice
''',
        'if not policy.can_read(user, invoice):\n        raise PermissionError("invoice access denied")\n',
        "",
        "return invoice",
    ),
    Seed(
        "cal-path-01", "path_traversal", "calibration",
        '''import os

def read_attachment(root, name):
    safe_name = os.path.basename(name)
    path = os.path.join(root, safe_name)
    with open(path, "rb") as handle:
        return handle.read()
''',
        "safe_name = os.path.basename(name)", "safe_name = name", "path = os.path.join(root, safe_name)",
    ),
    Seed(
        "heldout-sql-02", "sql_injection", "heldout",
        '''def find_order(db, reference):
    query = "SELECT * FROM orders WHERE reference = ?"
    return db.execute(query, (reference,)).fetchone()
''',
        'query = "SELECT * FROM orders WHERE reference = ?"',
        'query = "SELECT * FROM orders WHERE reference = \'" + reference + "\'"',
        'query = "SELECT * FROM orders WHERE reference = \'" + reference + "\'"',
    ),
    Seed(
        "heldout-sql-01", "sql_injection", "heldout",
        '''def lookup_member(db, handle):
    statement = "SELECT id FROM members WHERE handle = ?"
    return db.execute(statement, (handle,)).fetchone()
''',
        'statement = "SELECT id FROM members WHERE handle = ?"',
        'statement = "SELECT id FROM members WHERE handle = \'" + handle + "\'"',
        'statement = "SELECT id FROM members WHERE handle = \'" + handle + "\'"',
    ),
    Seed(
        "heldout-off-by-one-01", "off_by_one", "heldout",
        '''def first_available(values):
    for index in range(0, len(values)):
        if values[index] is not None:
            return index
    return None
''',
        "range(0, len(values))", "range(1, len(values))", "range(1, len(values))",
    ),
    Seed(
        "heldout-missing-await-01", "missing_await", "heldout",
        '''async def save_session(store, session):
    await store.commit(session)
    return session.id
''',
        "await store.commit(session)", "store.commit(session)", "store.commit(session)",
    ),
    Seed(
        "heldout-unbounded-01", "unbounded_query", "heldout",
        '''def search_messages(db, conversation_id, limit):
    cursor = db.execute("SELECT * FROM messages WHERE conversation_id = ?", (conversation_id,))
    return cursor.fetchmany(limit)
''',
        "cursor.fetchmany(limit)", "cursor.fetchall()", "cursor.fetchall()",
    ),
    Seed(
        "heldout-auth-01", "missing_authorization", "heldout",
        '''def update_profile(user, profile, policy, changes):
    if not policy.can_edit(user, profile):
        raise PermissionError("profile update denied")
    profile.update(changes)
    return profile
''',
        'if not policy.can_edit(user, profile):\n        raise PermissionError("profile update denied")\n',
        "",
        "profile.update(changes)",
    ),
    Seed(
        "heldout-path-01", "path_traversal", "heldout",
        '''import os

def write_report(root, name, contents):
    safe_name = os.path.basename(name)
    path = os.path.join(root, safe_name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(contents)
''',
        "safe_name = os.path.basename(name)", "safe_name = name", "path = os.path.join(root, safe_name)",
    ),
]


def make_item(seed: Seed) -> dict[str, object]:
    if seed.before is None:
        return {
            "id": seed.id,
            "category": seed.category,
            "input": seed.clean,
            "label": {"has_issue": False, "issue_category": None, "lines": None},
        }

    mutated = seed.clean.replace(seed.before, seed.after or "", 1)
    if mutated == seed.clean:
        raise ValueError(f"Mutation did not change seed {seed.id}")
    assert seed.anchor is not None
    line_number = next(
        (number for number, line in enumerate(mutated.splitlines(), start=1) if seed.anchor in line),
        None,
    )
    if line_number is None:
        raise ValueError(f"Mutation anchor is absent for seed {seed.id}")
    return {
        "id": seed.id,
        "category": seed.category,
        "input": mutated,
        "label": {
            "has_issue": True,
            "issue_category": seed.category,
            "lines": [line_number, line_number],
        },
    }


def write_corpus() -> None:
    groups: dict[str, list[dict[str, object]]] = {"calibration": [], "heldout": []}
    for seed in SEEDS:
        issue_item = make_item(seed)
        clean_item = {
            "id": f"{seed.id}-clean",
            "category": seed.category,
            "input": seed.clean,
            "label": {"has_issue": False, "issue_category": None, "lines": None},
        }
        groups[seed.split].extend((issue_item, clean_item))
    ids = [{item["id"] for item in group} for group in groups.values()]
    inputs = [{item["input"] for item in group} for group in groups.values()]
    all_ids = [item["id"] for group in groups.values() for item in group]
    expected_categories = {seed.category for seed in SEEDS}
    if ids[0] & ids[1]:
        raise ValueError("Calibration and held-out ids must not overlap")
    if inputs[0] & inputs[1]:
        raise ValueError("Calibration and held-out inputs must not overlap")
    if len(all_ids) != len(set(all_ids)):
        raise ValueError("Corpus item ids must be unique")
    for split, items in groups.items():
        missing = expected_categories - {item["category"] for item in items}
        if missing:
            raise ValueError(f"{split} is missing categories: {', '.join(sorted(missing))}")
    for split, items in groups.items():
        output = PACK_DIR / f"{split}.jsonl"
        output.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in items), encoding="utf-8")
        print(f"wrote {len(items)} items to {output.name}")


if __name__ == "__main__":
    write_corpus()
