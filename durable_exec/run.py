"""Demo: MatrixOne as a durable execution engine (DBOS/Temporal-style).

An order-processing workflow (reserve inventory -> charge payment -> ship ->
send receipt). We crash the worker mid-workflow (after the payment commits), then
re-invoke the SAME workflow id: completed steps are skipped and execution resumes
— proving side effects (charge, inventory decrement) run EXACTLY ONCE despite the
workflow being invoked twice. Plus a retry demo and SQL observability.

Run:  python3 -m durable_exec.run
"""
from durable_exec.engine import DB, DurableEngine, WorkflowFailed


def hr(s):
    print("\n" + "=" * 72 + f"\n  {s}\n" + "=" * 72)


def order_workflow(eng, wf_id, order, crash_before=None):
    """Ordinary code; each `eng.step(...)` is durable + exactly-once."""
    eng.start_workflow(wf_id, "order", order)

    def reserve(cur):
        cur.execute(f"UPDATE {DB}.inventory SET qty = qty - %s WHERE sku = %s",
                    (order["qty"], order["sku"]))
        return {"reserved": order["qty"], "sku": order["sku"]}
    _, a = eng.step(wf_id, "reserve_inventory", reserve)
    print(f"    reserve_inventory: {a}")

    if crash_before == "charge":
        raise RuntimeError("worker crashed before charge")

    def charge(cur):
        cur.execute(f"INSERT INTO {DB}.payments (wf_id, amount) VALUES (%s, %s)",
                    (wf_id, order["amount"]))
        return {"charged": order["amount"]}
    _, a = eng.step(wf_id, "charge_payment", charge)
    print(f"    charge_payment:    {a}")

    if crash_before == "ship":
        raise RuntimeError("worker crashed after charge, before ship")

    def ship(cur):
        cur.execute(f"INSERT INTO {DB}.shipments (wf_id, sku) VALUES (%s, %s)", (wf_id, order["sku"]))
        return {"shipped": True}
    _, a = eng.step(wf_id, "ship_order", ship)
    print(f"    ship_order:        {a}")

    def receipt(cur):
        cur.execute(f"INSERT INTO {DB}.receipts (wf_id) VALUES (%s)", (wf_id,))
        return {"receipt": True}
    _, a = eng.step(wf_id, "send_receipt", receipt)
    print(f"    send_receipt:      {a}")

    eng.complete_workflow(wf_id, {"status": "done"})


def main():
    eng = DurableEngine()
    eng.reset()
    eng._exec(f"INSERT INTO {DB}.inventory VALUES ('WIDGET', 10)")
    eng.conn.commit()
    order = {"sku": "WIDGET", "qty": 1, "amount": 49.99}
    W1 = "order-0001"

    # ---- run 1: crash after the payment is charged ----
    hr("Run 1 — worker crashes mid-workflow (after charge_payment commits)")
    try:
        order_workflow(eng, W1, order, crash_before="ship")
    except RuntimeError as e:
        print(f"    >>> CRASH: {e}")
    print(f"  after crash: inventory.qty={eng.scalar(f'SELECT qty FROM {DB}.inventory WHERE sku=%s', ('WIDGET',))}, "
          f"payments={eng.scalar(f'SELECT COUNT(*) FROM {DB}.payments WHERE wf_id=%s', (W1,))}, "
          f"shipments={eng.scalar(f'SELECT COUNT(*) FROM {DB}.shipments WHERE wf_id=%s', (W1,))}")
    print(f"  recoverable workflows (status=RUNNING): {eng.recoverable()}")

    # ---- run 2: re-invoke the SAME workflow id -> resume from checkpoint ----
    hr("Run 2 — re-invoke same workflow id: completed steps skipped, resume")
    order_workflow(eng, W1, order)   # no crash this time
    inv = eng.scalar(f"SELECT qty FROM {DB}.inventory WHERE sku=%s", ("WIDGET",))
    pays = eng.scalar(f"SELECT COUNT(*) FROM {DB}.payments WHERE wf_id=%s", (W1,))
    ships = eng.scalar(f"SELECT COUNT(*) FROM {DB}.shipments WHERE wf_id=%s", (W1,))
    status = eng.scalar(f"SELECT status FROM {DB}.wf_exec WHERE wf_id=%s", (W1,))
    print(f"  after resume: inventory.qty={inv}, payments={pays}, shipments={ships}, wf_status={status}")
    ok = (inv == 9 and pays == 1 and ships == 1 and status == "COMPLETED")
    print(f"  EXACTLY-ONCE side effects (charge & reserve ran once despite 2 invocations): {ok}")

    # ---- retry demo: a transient step that fails once then succeeds ----
    hr("Retry — a flaky step fails attempt 1, succeeds attempt 2 (attempts tracked)")
    W2 = "order-0002"
    eng.start_workflow(W2, "order", order)

    def flaky(cur, attempt):
        if attempt == 1:
            raise RuntimeError("transient downstream error")
        cur.execute(f"INSERT INTO {DB}.payments (wf_id, amount) VALUES (%s, %s)", (W2, 9.99))
        return {"charged": 9.99, "on_attempt": attempt}
    res, info = eng.step_with_retry(W2, "charge_payment", flaky, max_attempts=3)
    eng.complete_workflow(W2, {"status": "done"})
    print(f"    charge_payment: {info} -> {res}")
    print(f"    wf_step.attempts recorded = "
          f"{eng.scalar(f'SELECT attempts FROM {DB}.wf_step WHERE wf_id=%s AND step=%s', (W2, 'charge_payment'))}")

    # ---- observability ----
    hr("Observability (SQL over the durable execution log)")
    print(f"  W1 step history:")
    for step, st, att, out in eng.history(W1):
        print(f"    {step:<18} {st:<10} attempts={att}  out={out}")
    print(f"  workflow status summary:")
    for st, c in eng._q(f"SELECT status, COUNT(*) FROM {DB}.wf_exec GROUP BY status"):
        print(f"    {st}: {c}")

    eng.drop()
    eng.close()
    hr("Done — MatrixOne as a durable execution engine: crash-resumable, exactly-once, queryable")
    print("  Why it fits: step result + business side effect committed in ONE ACID transaction;")
    print("  re-invocation skips committed steps (idempotent via PK); in-flight workflows are a")
    print("  simple SQL query. This is the OPPOSITE workload from trace monitoring — here")
    print("  transactional correctness is the requirement, which is MatrixOne's strength.")
    print("  (git4data bonus: snapshot wf_exec/wf_step for an auditable, time-travelable run log.)")


if __name__ == "__main__":
    main()
