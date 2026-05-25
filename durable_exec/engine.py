"""A tiny DBOS-style durable execution engine backed by MatrixOne.

Idea (same as Temporal/DBOS): a workflow is ordinary code made of `steps`. After
each step we **checkpoint** its result to MatrixOne *in the same transaction as
the step's business writes*. If the worker crashes mid-workflow and the workflow
is re-invoked, completed steps are **skipped** (their results read back from the
DB) and execution resumes — so side effects run **exactly once** despite retries
and crashes.

Why MatrixOne fits: this needs ACID transactions + durable, queryable state +
idempotency via primary keys — exactly what a database gives you (the opposite of
the trace-monitoring workload). The engine uses its own connection with
autocommit OFF so each step commit is atomic.
"""
import json

import pymysql

import config

DB = "mld_durable"


class WorkflowFailed(Exception):
    pass


class DurableEngine:
    def __init__(self):
        p = config.mo_conn_params()
        self.conn = pymysql.connect(host=p["host"], port=p["port"], user=p["user"],
                                    password=p["password"], charset="utf8mb4",
                                    autocommit=False)

    # ---- schema ----
    def reset(self):
        self._exec(f"DROP DATABASE IF EXISTS {DB}")
        self._exec(f"CREATE DATABASE {DB}")
        self._exec(f"CREATE TABLE {DB}.wf_exec (wf_id VARCHAR(64) PRIMARY KEY, name VARCHAR(64), "
                   f"status VARCHAR(16), input JSON, output JSON, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        self._exec(f"CREATE TABLE {DB}.wf_step (wf_id VARCHAR(64), step VARCHAR(64), status VARCHAR(16), "
                   f"output JSON, attempts INT, error VARCHAR(256), PRIMARY KEY (wf_id, step))")
        # business tables (real side effects)
        self._exec(f"CREATE TABLE {DB}.inventory (sku VARCHAR(32) PRIMARY KEY, qty INT)")
        self._exec(f"CREATE TABLE {DB}.payments (wf_id VARCHAR(64) PRIMARY KEY, amount DOUBLE)")
        self._exec(f"CREATE TABLE {DB}.shipments (wf_id VARCHAR(64) PRIMARY KEY, sku VARCHAR(32))")
        self._exec(f"CREATE TABLE {DB}.receipts (wf_id VARCHAR(64) PRIMARY KEY)")
        self.conn.commit()

    def _exec(self, sql, args=None):
        with self.conn.cursor() as cur:
            cur.execute(sql, args)

    def _one(self, sql, args=None):
        with self.conn.cursor() as cur:
            cur.execute(sql, args)
            r = cur.fetchone()
        self.conn.commit()   # end the read transaction so later reads see fresh commits
        return r

    # ---- workflow lifecycle ----
    def start_workflow(self, wf_id, name, inp):
        row = self._one(f"SELECT status FROM {DB}.wf_exec WHERE wf_id=%s", (wf_id,))
        if row is None:
            self._exec(f"INSERT INTO {DB}.wf_exec (wf_id,name,status,input) VALUES (%s,%s,'RUNNING',%s)",
                       (wf_id, name, json.dumps(inp)))
            self.conn.commit()
            return "started"
        return f"resumed (was {row[0]})"

    def step(self, wf_id, name, fn):
        """Durable step: skip if already COMPLETED, else run fn(cursor) + checkpoint
        in ONE transaction (exactly-once for DB side effects)."""
        row = self._one(f"SELECT status, output FROM {DB}.wf_step WHERE wf_id=%s AND step=%s", (wf_id, name))
        if row and row[0] == "COMPLETED":
            return json.loads(row[1]) if row[1] else None, "skipped"
        try:
            with self.conn.cursor() as cur:
                result = fn(cur)                       # business writes happen on this cursor/txn
                cur.execute(
                    f"INSERT INTO {DB}.wf_step (wf_id,step,status,output,attempts) "
                    f"VALUES (%s,%s,'COMPLETED',%s,1)", (wf_id, name, json.dumps(result)))
            self.conn.commit()                         # atomic: side effect + checkpoint
            return result, "executed"
        except Exception:
            self.conn.rollback()
            raise

    def step_with_retry(self, wf_id, name, fn, max_attempts=3):
        row = self._one(f"SELECT status, output FROM {DB}.wf_step WHERE wf_id=%s AND step=%s", (wf_id, name))
        if row and row[0] == "COMPLETED":
            return json.loads(row[1]) if row[1] else None, "skipped"
        attempt = 0
        while attempt < max_attempts:
            attempt += 1
            try:
                with self.conn.cursor() as cur:
                    # clear any prior RETRYING row, then run + checkpoint in one txn
                    cur.execute(f"DELETE FROM {DB}.wf_step WHERE wf_id=%s AND step=%s", (wf_id, name))
                    result = fn(cur, attempt)
                    cur.execute(
                        f"INSERT INTO {DB}.wf_step (wf_id,step,status,output,attempts) "
                        f"VALUES (%s,%s,'COMPLETED',%s,%s)", (wf_id, name, json.dumps(result), attempt))
                self.conn.commit()
                return result, f"executed (attempt {attempt})"
            except Exception as e:
                self.conn.rollback()
                self._exec(f"DELETE FROM {DB}.wf_step WHERE wf_id=%s AND step=%s", (wf_id, name))
                self._exec(f"INSERT INTO {DB}.wf_step (wf_id,step,status,attempts,error) "
                           f"VALUES (%s,%s,'RETRYING',%s,%s)", (wf_id, name, attempt, str(e)[:256]))
                self.conn.commit()
                if attempt >= max_attempts:
                    self._exec(f"UPDATE {DB}.wf_step SET status='FAILED' WHERE wf_id=%s AND step=%s", (wf_id, name))
                    self.conn.commit()
                    raise WorkflowFailed(f"step {name} failed after {attempt} attempts: {e}")

    def complete_workflow(self, wf_id, output):
        self._exec(f"UPDATE {DB}.wf_exec SET status='COMPLETED', output=%s WHERE wf_id=%s",
                   (json.dumps(output), wf_id))
        self.conn.commit()

    # ---- observability / recovery ----
    def recoverable(self):
        return [r[0] for r in self._q(f"SELECT wf_id FROM {DB}.wf_exec WHERE status='RUNNING' ORDER BY wf_id")]

    def history(self, wf_id):
        return self._q(f"SELECT step, status, attempts, output FROM {DB}.wf_step WHERE wf_id=%s "
                       f"ORDER BY step", (wf_id,))

    def scalar(self, sql, args=None):
        r = self._one(sql, args)
        return r[0] if r else None

    def _q(self, sql, args=None):
        with self.conn.cursor() as cur:
            cur.execute(sql, args)
            rows = cur.fetchall()
        self.conn.commit()
        return rows

    def drop(self):
        self._exec(f"DROP DATABASE IF EXISTS {DB}")
        self.conn.commit()

    def close(self):
        self.conn.close()
