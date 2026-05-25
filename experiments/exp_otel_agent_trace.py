"""Verify: ingest OpenTelemetry agent traces into MatrixOne (real OTel SDK +
custom SpanExporter), query them in SQL, and version them with git4data.

How agent/LLM traces flow in practice (OpenTelemetry GenAI semantic conventions):
  agent SDK is instrumented -> emits spans (root `invoke_agent`, child `chat
  {model}` LLM calls, `execute_tool {name}` calls) with `gen_ai.*` attributes ->
  a SpanExporter ships them (OTLP -> Collector -> backend, often ClickHouse).

This PoC plugs MatrixOne in as that backend: a real `opentelemetry.sdk`
`SpanExporter` writes each ReadableSpan into a MatrixOne `spans` table (a
ClickHouse-OTel-like schema). Then we (1) reconstruct a trace tree and roll up
tokens/latency/errors in SQL, and (2) snapshot the trace store, ingest a second
agent *version*'s traces, and use git4data to diff/compare versions — i.e. the
trace store doubles as a versioned substrate for agent iteration.

Run:  python3 -m experiments.exp_otel_agent_trace
"""
import json
import random

from opentelemetry import trace as otel_trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.trace import SpanKind, Status, StatusCode

import config
from matrixone.mo_client import MO

DB = "mld_otel"


# ---------- a real OpenTelemetry SpanExporter that writes to MatrixOne ----------
class MatrixOneSpanExporter(SpanExporter):
    """Maps OTel ReadableSpan -> a MatrixOne row (this is the integration point;
    in production the same logic lives in an OTel Collector exporter)."""

    def __init__(self, mo):
        self.mo = mo
        self.run_tag = 1

    def export(self, spans):
        rows = []
        for s in spans:
            a = dict(s.attributes or {})
            rows.append((
                format(s.context.trace_id, "032x"),
                format(s.context.span_id, "016x"),
                format(s.parent.span_id, "016x") if s.parent else "",
                s.name,
                s.kind.name,
                int(s.start_time), int(s.end_time),
                round((s.end_time - s.start_time) / 1e6, 3),
                s.status.status_code.name,
                (s.resource.attributes.get("service.name") if s.resource else None),
                a.get("gen_ai.operation.name"),
                a.get("gen_ai.system"),
                a.get("gen_ai.request.model"),
                int(a.get("gen_ai.usage.input_tokens", 0)),
                int(a.get("gen_ai.usage.output_tokens", 0)),
                json.dumps(a, ensure_ascii=False),
                self.run_tag,
            ))
        self.mo.executemany(
            f"INSERT INTO {DB}.spans (trace_id,span_id,parent_span_id,name,kind,"
            f"start_ns,end_ns,duration_ms,status,service_name,gen_ai_operation,"
            f"gen_ai_system,gen_ai_request_model,input_tokens,output_tokens,"
            f"attributes,ingest_run) VALUES "
            f"(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", rows)
        return SpanExportResult.SUCCESS

    def shutdown(self):
        pass


def simulate_agent_run(tracer, idx, model, fail_tool=False):
    """One agent invocation -> a trace: invoke_agent -> chat + execute_tool + chat."""
    rng = random.Random(1000 + idx)
    with tracer.start_as_current_span(
        "invoke_agent research-bot", kind=SpanKind.INTERNAL,
        attributes={"gen_ai.operation.name": "invoke_agent",
                    "gen_ai.agent.name": "research-bot"}) as root:
        # step 1: LLM plans
        with tracer.start_as_current_span(
            f"chat {model}", kind=SpanKind.CLIENT,
            attributes={"gen_ai.operation.name": "chat", "gen_ai.system": "openai",
                        "gen_ai.request.model": model,
                        "gen_ai.usage.input_tokens": rng.randint(200, 400),
                        "gen_ai.usage.output_tokens": rng.randint(50, 150),
                        "gen_ai.response.finish_reasons": "tool_calls"}):
            pass
        # step 2: tool call (may fail)
        with tracer.start_as_current_span(
            "execute_tool web_search", kind=SpanKind.INTERNAL,
            attributes={"gen_ai.operation.name": "execute_tool",
                        "tool.name": "web_search"}) as tool:
            if fail_tool:
                tool.set_status(Status(StatusCode.ERROR, "search API timeout"))
        # step 3: LLM answers
        with tracer.start_as_current_span(
            f"chat {model}", kind=SpanKind.CLIENT,
            attributes={"gen_ai.operation.name": "chat", "gen_ai.system": "openai",
                        "gen_ai.request.model": model,
                        "gen_ai.usage.input_tokens": rng.randint(300, 600),
                        "gen_ai.usage.output_tokens": rng.randint(80, 200),
                        "gen_ai.response.finish_reasons": "stop"}):
            pass
        if fail_tool:
            root.set_status(Status(StatusCode.ERROR, "tool failed"))


def hr(t):
    print("\n" + "=" * 72 + f"\n  {t}\n" + "=" * 72)


def main():
    acct = config.mo_account_name()
    with MO() as mo:
        mo.execute(f"DROP DATABASE IF EXISTS {DB}")
        mo.execute(f"CREATE DATABASE {DB}")
        mo.execute(
            f"CREATE TABLE {DB}.spans (trace_id VARCHAR(32), span_id VARCHAR(16), "
            f"parent_span_id VARCHAR(16), name VARCHAR(128), kind VARCHAR(16), "
            f"start_ns BIGINT, end_ns BIGINT, duration_ms DOUBLE, status VARCHAR(16), "
            f"service_name VARCHAR(64), gen_ai_operation VARCHAR(32), gen_ai_system VARCHAR(32), "
            f"gen_ai_request_model VARCHAR(64), input_tokens INT, output_tokens INT, "
            f"attributes JSON, ingest_run INT, PRIMARY KEY (trace_id, span_id))")
        for s in ("otel_v1", "otel_v2"):
            mo.execute(f"DROP SNAPSHOT IF EXISTS mld_{s}")

        # ---- wire a real OTel TracerProvider to the MatrixOne exporter ----
        exporter = MatrixOneSpanExporter(mo)
        provider = TracerProvider(resource=Resource.create({"service.name": "research-bot"}))
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        tracer = provider.get_tracer("agent.poc")

        # ---- ACT 1: ingest agent v1 traces via OTel ----
        hr("ACT 1  Ingest OpenTelemetry agent traces into MatrixOne (real OTel SDK)")
        exporter.run_tag = 1
        for i in range(8):
            simulate_agent_run(tracer, i, model="gpt-4o", fail_tool=(i % 4 == 0))
        n_spans = mo.scalar(f"SELECT COUNT(*) FROM {DB}.spans")
        n_traces = mo.scalar(f"SELECT COUNT(DISTINCT trace_id) FROM {DB}.spans")
        print(f"  exported {n_traces} agent traces ({n_spans} spans) through a real "
              f"opentelemetry SpanExporter -> MatrixOne `spans` table")

        # ---- ACT 2: reconstruct a trace tree in SQL ----
        hr("ACT 2  Reconstruct a trace tree + roll up tokens/latency/errors in SQL")
        tid = mo.scalar(f"SELECT trace_id FROM {DB}.spans WHERE parent_span_id='' LIMIT 1")
        spans = mo.query(
            f"SELECT span_id, parent_span_id, name, status, input_tokens, output_tokens "
            f"FROM {DB}.spans WHERE trace_id=%s", (tid,))
        children = {}
        for sid, pid, name, st, it, ot in spans:
            children.setdefault(pid, []).append((sid, name, st, it, ot))

        def walk(pid, depth):
            for sid, name, st, it, ot in children.get(pid, []):
                tok = f"  tok={it}+{ot}" if (it or ot) else ""
                err = "  [ERROR]" if st == "ERROR" else ""
                print(f"    {'  ' * depth}└─ {name}{tok}{err}")
                walk(sid, depth + 1)

        print(f"  trace {tid[:16]}…:")
        walk("", 0)

        print("\n  per-trace rollup (SQL over the span tree):")
        for tr, ns, tok, dur, errs in mo.query(
            f"SELECT trace_id, COUNT(*), SUM(input_tokens+output_tokens), "
            f"ROUND((MAX(end_ns)-MIN(start_ns))/1e6,3), "
            f"SUM(CASE WHEN status='ERROR' THEN 1 ELSE 0 END) "
            f"FROM {DB}.spans GROUP BY trace_id ORDER BY trace_id LIMIT 4"):
            print(f"    {tr[:16]}…  spans={ns}  tokens={tok}  span_ms={dur}  errors={errs}")
        bad = mo.scalar(f"SELECT COUNT(*) FROM {DB}.spans WHERE status='ERROR'")
        print(f"  error spans across all traces: {bad}  "
              f"(SELECT … WHERE status='ERROR' — find every failing tool/LLM call)")

        # snapshot the trace store = "agent v1 traces"
        mo.execute(f"CREATE SNAPSHOT mld_otel_v1 FOR TABLE {DB} spans")

        # ---- ACT 3: ingest agent v2 (cheaper model) -> git4data version & compare ----
        hr("ACT 3  Version the trace store: ingest agent v2, diff & compare with git4data")
        exporter.run_tag = 2
        for i in range(8, 16):
            simulate_agent_run(tracer, i, model="gpt-4o-mini", fail_tool=(i % 8 == 0))
        mo.execute(f"CREATE SNAPSHOT mld_otel_v2 FOR TABLE {DB} spans")
        d = {r[0]: int(r[1]) for r in mo.query(
            f"DATA BRANCH DIFF {DB}.spans {{snapshot='mld_otel_v2'}} "
            f"AGAINST {DB}.spans {{snapshot='mld_otel_v1'}} OUTPUT SUMMARY")}
        print(f"  DATA BRANCH DIFF v2 vs v1: INSERTED={d.get('INSERTED',0)} spans (agent v2's traces)")
        print("\n  agent version comparison (SQL on the versioned trace store):")
        print(f"    {'run':<6}{'model':<14}{'traces':>7}{'avg_tokens/trace':>18}{'err_traces':>12}")
        for run, model, in [(1, "gpt-4o"), (2, "gpt-4o-mini")]:  # noqa
            row = mo.query_one(
                f"SELECT COUNT(DISTINCT trace_id), "
                f"ROUND(SUM(input_tokens+output_tokens)/COUNT(DISTINCT trace_id),1), "
                f"COUNT(DISTINCT CASE WHEN status='ERROR' THEN trace_id END) "
                f"FROM {DB}.spans WHERE ingest_run=%s", (run,))
            print(f"    v{run:<5}{model:<14}{row[0]:>7}{row[1]:>18}{row[2]:>12}")
        print("  => the OTel trace store is also a *versioned* substrate: snapshot per agent")
        print("     version, row-level DIFF of new spans, and SQL A/B on cost/error across versions.")

        for s in ("otel_v1", "otel_v2"):
            mo.execute(f"DROP SNAPSHOT IF EXISTS mld_{s}")
        mo.execute(f"DROP DATABASE IF EXISTS {DB}")
        provider.shutdown()

        hr("Verdict")
        print("  MatrixOne CAN back OpenTelemetry agent traces: a standard OTel SpanExporter")
        print("  maps gen_ai.* spans into a SQL table; trace trees/roll-ups/error search are")
        print("  plain SQL; and git4data adds snapshot/DIFF/branch on top — so the same store")
        print("  serves observability AND versioned agent iteration. (In prod: agent SDK -> OTLP")
        print("  -> OTel Collector with a MatrixOne exporter; this PoC is that exporter inline.)")


if __name__ == "__main__":
    main()
