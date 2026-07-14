import argparse
import hashlib
import json
import sqlite3


def stable_legacy_id(rowid: int, expression: str) -> str:
    digest = hashlib.sha256(expression.encode("utf-8")).hexdigest()[:20]
    return f"legacy:{rowid}:{digest}"


def migrate(path: str) -> dict[str, int]:
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    used = {
        row[0]
        for row in conn.execute("SELECT id FROM alphas WHERE id IS NOT NULL")
    }
    rows = conn.execute(
        "SELECT rowid, expression, raw_data FROM alphas WHERE id IS NULL ORDER BY rowid"
    ).fetchall()
    counts = {"scanned": len(rows), "recovered": 0, "synthetic": 0}

    try:
        conn.execute("BEGIN IMMEDIATE")
        for rowid, expression, raw_data in rows:
            try:
                data = json.loads(raw_data) if raw_data else {}
            except (TypeError, json.JSONDecodeError):
                data = {}

            candidate = data.get("alpha_id") if isinstance(data, dict) else None
            if candidate and str(candidate) not in used:
                new_id = str(candidate)
                counts["recovered"] += 1
            else:
                new_id = stable_legacy_id(rowid, expression or "")
                counts["synthetic"] += 1

            while new_id in used:
                new_id = f"{new_id}:{rowid}"
            conn.execute("UPDATE alphas SET id = ? WHERE rowid = ?", (new_id, rowid))
            used.add(new_id)

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return counts


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="wq_miner.db")
    args = parser.parse_args()
    print(json.dumps(migrate(args.db), ensure_ascii=False, sort_keys=True))
