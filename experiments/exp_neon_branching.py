"""MatrixOne as a "branchable database" — the Neon dev/preview-branch workflow.

Neon's headline is instant copy-on-write database branches (branch prod for a PR /
CI / dev sandbox, work in isolation, then discard or keep) + PITR. MatrixOne offers
the same shape via zero-copy CLONE (whole-DB branch) / DATA BRANCH + snapshots/PITR.
This exercises that workflow and times it, then notes where Neon/Supabase differ.

Run:  python3 -m experiments.exp_neon_branching
"""
import time

import config
from matrixone.mo_client import MO

PROD = "mld_prod"
DEV = "mld_prod_dev"


def hr(s):
    print("\n" + "=" * 72 + f"\n  {s}\n" + "=" * 72)


def main():
    with MO() as mo:
        for db in (PROD, DEV):
            mo.execute(f"DROP DATABASE IF EXISTS {db}")
        mo.execute(f"CREATE DATABASE {PROD}")
        mo.execute(f"CREATE TABLE {PROD}.users (id INT PRIMARY KEY, name VARCHAR(32))")
        mo.execute(f"CREATE TABLE {PROD}.orders (id INT PRIMARY KEY, user_id INT, amount DOUBLE)")
        mo.execute(f"INSERT INTO {PROD}.users SELECT result, concat('u', result) FROM generate_series(0,999) g")
        mo.execute(f"INSERT INTO {PROD}.orders SELECT result, result%1000, result*1.5 FROM generate_series(0,4999) g")
        print(f"prod: users={mo.scalar(f'SELECT COUNT(*) FROM {PROD}.users')}, "
              f"orders={mo.scalar(f'SELECT COUNT(*) FROM {PROD}.orders')}")

        # ---- instant CoW branch of the whole database (Neon-style) ----
        hr("Create an instant dev/preview branch of prod (zero-copy CLONE)")
        t = time.perf_counter()
        mo.execute(f"CREATE DATABASE {DEV} CLONE {PROD}")
        ms = (time.perf_counter() - t) * 1000
        print(f"  CREATE DATABASE {DEV} CLONE {PROD}  ->  {ms:.0f} ms (copy-on-write, "
              f"branched {mo.scalar(f'SELECT COUNT(*) FROM {DEV}.orders')} orders + "
              f"{mo.scalar(f'SELECT COUNT(*) FROM {DEV}.users')} users)")

        # ---- migrate + change data on the branch, in isolation ----
        hr("Run a schema migration + data changes on the branch only")
        mo.execute(f"ALTER TABLE {DEV}.users ADD COLUMN tier VARCHAR(8) DEFAULT 'free'")
        mo.execute(f"UPDATE {DEV}.users SET tier='pro' WHERE id < 100")
        mo.execute(f"INSERT INTO {DEV}.orders SELECT result, result%1000, 9.9 FROM generate_series(5000,5099) g")
        print(f"  branch now: users have new `tier` col; orders={mo.scalar(f'SELECT COUNT(*) FROM {DEV}.orders')}")

        # ---- prod is untouched (branch isolation) ----
        prod_orders = mo.scalar(f"SELECT COUNT(*) FROM {PROD}.orders")
        prod_has_tier = mo.query(
            f"SELECT COUNT(*) FROM information_schema.columns WHERE table_schema='{PROD}' "
            f"AND table_name='users' AND column_name='tier'")[0][0]
        print(f"  PROD unchanged: orders={prod_orders} (still 5000), users.tier exists in prod? "
              f"{'yes' if prod_has_tier else 'no'}  -> branch is isolated")

        # ---- row-level diff branch vs prod (Neon has no native row diff) ----
        hr("Diff the branch against prod (row-level) — extra over Neon")
        d = {r[0]: int(r[1]) for r in mo.query(
            f"DATA BRANCH DIFF {DEV}.orders AGAINST {PROD}.orders OUTPUT SUMMARY")}
        print(f"  orders: DATA BRANCH DIFF dev vs prod = INSERTED={d.get('INSERTED',0)} "
              f"DELETED={d.get('DELETED',0)} UPDATED={d.get('UPDATED',0)}  (100 new orders added on branch)")
        print("  notes: (1) `users` changed schema -> not row-diffable (DIFF needs identical schema);")
        print("         (2) zero-copy CLONE has no branch lineage, so DIFF here detects inserts/updates")
        print("             but NOT deletes — for full row-level diff/merge incl. deletes use")
        print("             `DATA BRANCH CREATE TABLE … FROM …` (which carries the lineage/LCA).")

        # ---- discard the ephemeral branch (like deleting a Neon preview branch) ----
        hr("Discard the branch")
        t = time.perf_counter()
        mo.execute(f"DROP DATABASE {DEV}")
        print(f"  DROP DATABASE {DEV} -> {(time.perf_counter()-t)*1000:.0f} ms; prod intact "
              f"(orders={mo.scalar(f'SELECT COUNT(*) FROM {PROD}.orders')})")

        mo.execute(f"DROP DATABASE IF EXISTS {PROD}")

        hr("MatrixOne vs Neon vs Supabase (this workflow)")
        print("  Same as Neon: instant CoW DB branches + PITR + a real SQL DB + vector.")
        print("  MatrixOne extra: ROW-LEVEL DATA BRANCH DIFF/MERGE/PICK between branches.")
        print("  Neon extra: each branch is a separate SERVERLESS Postgres endpoint (autoscale +")
        print("    scale-to-zero, branch-per-PR CI), true Postgres ecosystem; MatrixOne branches")
        print("    live inside one instance (db/table scope).")
        print("  Supabase (full BaaS) extra — and what MatrixOne LACKS for the BaaS scenario:")
        print("    auto REST/GraphQL API (PostgREST), Auth/JWT/OAuth, Realtime push, object")
        print("    Storage with policies, Edge Functions, Row-Level Security, Studio UI, client")
        print("    SDKs. MatrixOne is the DATABASE layer; it has pub/sub + SQL UDF + STAGE(OSS")
        print("    file refs) + git4data, but the BaaS app layer would have to be built/added.")


if __name__ == "__main__":
    main()
