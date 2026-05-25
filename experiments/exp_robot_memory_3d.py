"""Scenario: embodied-AI / robot spatial memory from IoT sensor streams, in 3D,
WITH git-for-data version control.

A robot (or a fleet) perceives the world through IoT sensors (LiDAR / depth cam /
sonar) that emit a continuous stream of 3D points. The canonical "memory" built
from that stream is a VOXEL MAP / occupancy grid: space is diced into cubes
(voxels) and each voxel accumulates how often it was observed as occupied, plus a
semantic label + confidence. This is exactly what OctoMap / a TSDF / an
occupancy grid stores.

The interesting question for THIS repo: a robot's memory is not static — the world
drifts (furniture moves, people pass), multiple robots explore disjoint areas and
must MERGE maps, sensors glitch and inject phantom obstacles that must be ROLLED
BACK, and you often need to ask "what did the robot believe was here LAST TUESDAY"
(time travel). Those are git operations on spatial data. We do all of it in one
MatrixOne database:

  ACT 1  IoT stream -> 3D voxel memory      FLOOR()-voxelization + incremental
                                            UPDATE..JOIN consolidation
  ACT 2  drift detection across time        CREATE SNAPSHOT v1/v2 + DATA BRANCH
                                            DIFF (row-level: which cells changed)
  ACT 3  3D spatial query ON A VERSION      nearest-voxel kNN with {snapshot=}
                                            (time travel + spatial together)
  ACT 4  fleet map merge with conflict      DATA BRANCH CREATE/DIFF/MERGE
                                            (two robots, conflicting cell)
  ACT 5  roll back a bad sensor batch        RESTORE ... FROM SNAPSHOT
  +      DuckDB baseline                     same 3D query is easy analytically,
                                            but there is NO native version control

Then: how this compares to rosbag/MCAP, OctoMap, a TSDB, PostGIS, a vector DB.

Run:  python3 -m experiments.exp_robot_memory_3d
"""
import config
from matrixone.mo_client import MO
from matrixone import git4data as g4d

DB = "mld_robot"
VOX = "voxel_map"
STREAM = "sensor_stream"
VOXEL = 1.0   # voxel edge length (world units); FLOOR(coord/VOXEL) is the cell index


def hr(s):
    print("\n" + "=" * 72 + f"\n  {s}\n" + "=" * 72)


def consolidate(mo, since_id):
    """Fold new sensor points (id > since_id) into the voxel map.

    Existing voxels are UPDATED IN PLACE (occ += observed count) so DATA BRANCH
    DIFF reports them as UPDATED (drift), not delete+insert; brand-new voxels are
    INSERTed. This is the row-level upsert that makes the map diffable/mergeable.
    """
    agg = (f"SELECT FLOOR(x/{VOXEL}) vx, FLOOR(y/{VOXEL}) vy, FLOOR(z/{VOXEL}) vz, "
           f"COUNT(*) c, MAX(ts) ls FROM {DB}.{STREAM} WHERE id > {since_id} "
           f"GROUP BY FLOOR(x/{VOXEL}), FLOOR(y/{VOXEL}), FLOOR(z/{VOXEL})")
    # 1) increment occupancy of voxels we have seen before (in-place UPDATE)
    mo.execute(
        f"UPDATE {DB}.{VOX} m JOIN ({agg}) a "
        f"ON m.vx=a.vx AND m.vy=a.vy AND m.vz=a.vz "
        f"SET m.occ = m.occ + a.c, m.last_seen = a.ls"
    )
    # 2) insert voxels observed for the first time
    mo.execute(
        f"INSERT INTO {DB}.{VOX} (vx,vy,vz,occ,label,confidence,last_seen) "
        f"SELECT a.vx,a.vy,a.vz,a.c,'unknown',0.5,a.ls FROM ({agg}) a "
        f"LEFT JOIN {DB}.{VOX} m ON m.vx=a.vx AND m.vy=a.vy AND m.vz=a.vz "
        f"WHERE m.vx IS NULL"
    )
    return mo.scalar(f"SELECT MAX(id) FROM {DB}.{STREAM}")


def voxel_stats(mo, snap=None):
    s = f" {{snapshot='{snap}'}}" if snap else ""
    n = mo.scalar(f"SELECT COUNT(*) FROM {DB}.{VOX}{s}")
    occ = mo.scalar(f"SELECT SUM(occ) FROM {DB}.{VOX}{s}")
    return int(n), int(occ or 0)


def nearest_voxels(mo, q, k=5, snap=None):
    """3D nearest occupied voxels to query point q=(x,y,z), squared-euclidean.

    MatrixOne has no 3D index type, but the occupancy grid is the index: arithmetic
    distance over INT cell coords, ORDER BY ... LIMIT k. Works at a snapshot too.
    """
    qx, qy, qz = (int(c // VOXEL) for c in q)
    s = f" {{snapshot='{snap}'}}" if snap else ""
    return mo.query(
        f"SELECT vx,vy,vz,occ,label, "
        f"(vx-{qx})*(vx-{qx})+(vy-{qy})*(vy-{qy})+(vz-{qz})*(vz-{qz}) AS d2 "
        f"FROM {DB}.{VOX}{s} ORDER BY d2 ASC, occ DESC LIMIT {k}"
    )


def main():
    acct = config.mo_account_name()
    with MO() as mo:
        for s in ("rob_v1", "rob_v2", "rob_good"):
            mo.execute(f"DROP SNAPSHOT IF EXISTS {s}")
        mo.execute(f"DROP DATABASE IF EXISTS {DB}")
        mo.execute(f"CREATE DATABASE {DB}")
        mo.execute(
            f"CREATE TABLE {DB}.{STREAM} (id BIGINT PRIMARY KEY AUTO_INCREMENT, "
            f"robot_id INT, ts TIMESTAMP, x DOUBLE, y DOUBLE, z DOUBLE)"
        )
        mo.execute(
            f"CREATE TABLE {DB}.{VOX} (vx INT, vy INT, vz INT, occ INT, "
            f"label VARCHAR(16), confidence DOUBLE, last_seen TIMESTAMP, "
            f"PRIMARY KEY (vx, vy, vz))"
        )

        # ============================================================ ACT 1
        hr("ACT 1 — IoT sensor stream -> 3D voxel memory (occupancy grid)")
        # robot 1 first scan: 3000 LiDAR returns over a 10x10x3 region
        mo.execute(
            f"INSERT INTO {DB}.{STREAM} (robot_id, ts, x, y, z) "
            f"SELECT 1, now(), result%10 + 0.3, FLOOR(result/10)%10 + 0.3, "
            f"result%3 + 0.1 FROM generate_series(0,2999) g"
        )
        pts = mo.scalar(f"SELECT COUNT(*) FROM {DB}.{STREAM}")
        wm = consolidate(mo, since_id=0)
        n, occ = voxel_stats(mo)
        print(f"  ingested {pts} sensor points -> voxelized (edge={VOXEL}) into "
              f"{n} occupied voxels, total occ observations={occ}")
        g4d.snapshot(mo, "rob_v1", DB, VOX)
        print("  CREATE SNAPSHOT rob_v1  (the robot's memory, committed as a version)")

        # ============================================================ ACT 2
        hr("ACT 2 — the world drifts; detect WHICH cells changed (row-level DIFF)")
        # robot revisits: re-observes the old region (occupancy drifts up) AND
        # discovers a new alcove at x,y ~ 12..15 (new voxels)
        mo.execute(
            f"INSERT INTO {DB}.{STREAM} (robot_id, ts, x, y, z) "
            f"SELECT 1, now(), result%10 + 0.3, FLOOR(result/10)%10 + 0.3, "
            f"result%3 + 0.1 FROM generate_series(0,999) g"
        )
        mo.execute(
            f"INSERT INTO {DB}.{STREAM} (robot_id, ts, x, y, z) "
            f"SELECT 1, now(), result%5 + 12.3, FLOOR(result/5)%5 + 12.3, result%2 + 0.1 "
            f"FROM generate_series(0,499) g"
        )
        wm = consolidate(mo, since_id=wm)
        g4d.snapshot(mo, "rob_v2", DB, VOX)
        n2, occ2 = voxel_stats(mo)
        print(f"  after revisit: {n2} voxels, total occ={occ2}  (snapshot rob_v2)")
        drift = g4d.branch_diff_summary(mo, DB, VOX, VOX, target_snap="rob_v2", base_snap="rob_v1")
        print(f"  DATA BRANCH DIFF rob_v2 AGAINST rob_v1 -> "
              f"INSERTED={drift.get('INSERTED',0)} (new cells discovered), "
              f"UPDATED={drift.get('UPDATED',0)} (cells whose occupancy drifted), "
              f"DELETED={drift.get('DELETED',0)}")
        print("  => row-level drift map: you know EXACTLY which voxels changed between "
              "two points in time, not just that 'the map changed'.")

        # ============================================================ ACT 3
        hr("ACT 3 — 3D spatial query ON A PAST VERSION (time travel + kNN)")
        dock = (5.0, 5.0, 1.0)   # e.g. the charging dock; what's nearby?
        print(f"  query: 3 nearest occupied voxels to the dock at {dock}")
        for label, snap in (("as of rob_v1", "rob_v1"), ("live (now)", None)):
            rows = nearest_voxels(mo, dock, k=3, snap=snap)
            cells = ", ".join(f"({vx},{vy},{vz})occ={occ}" for vx, vy, vz, occ, lab, d2 in rows)
            print(f"    {label:<14}: {cells}")
        print("  => same spatial query answered against ANY historical version of the "
              "map — 'what did the robot believe was around the dock last Tuesday?'")

        # ============================================================ ACT 4
        hr("ACT 4 — fleet: merge a second robot's map, with conflict handling")
        # robot 2 branches the shared map, explores a far region (20..22) and
        # RE-OBSERVES the dock cell differently; meanwhile the central map also
        # updates the dock cell -> a genuine conflict on that voxel.
        mo.execute(f"DATA BRANCH CREATE TABLE {DB}.{VOX}_b FROM {DB}.{VOX}")
        mo.execute(
            f"INSERT INTO {DB}.{VOX}_b (vx,vy,vz,occ,label,confidence,last_seen) "
            f"VALUES (20,20,0,8,'corridor',0.7,now()),(21,20,0,6,'corridor',0.7,now()),"
            f"(22,21,1,4,'door',0.6,now())"
        )
        mo.execute(f"UPDATE {DB}.{VOX}_b SET occ=occ+50, label='dock_b' WHERE vx=5 AND vy=5 AND vz=1")
        mo.execute(f"UPDATE {DB}.{VOX} SET occ=occ+10, label='dock_a' WHERE vx=5 AND vy=5 AND vz=1")
        fleet = g4d.branch_diff_summary(mo, DB, f"{VOX}_b", VOX)
        print(f"  robot-2 branch vs central: INSERTED={fleet.get('INSERTED',0)} "
              f"(new corridor cells), UPDATED={fleet.get('UPDATED',0)} (the contested dock cell)")
        try:
            g4d.branch_merge(mo, DB, f"{VOX}_b", VOX, conflict="FAIL")
            print("  MERGE WHEN CONFLICT FAIL -> unexpectedly succeeded")
        except Exception:
            print("  MERGE WHEN CONFLICT FAIL -> refused: both sides changed the dock "
                  "cell -> conflict surfaced (not silently overwritten)")
        before = mo.scalar(f"SELECT COUNT(*) FROM {DB}.{VOX} WHERE vx>=20")
        g4d.branch_merge(mo, DB, f"{VOX}_b", VOX, conflict="SKIP")
        after = mo.scalar(f"SELECT COUNT(*) FROM {DB}.{VOX} WHERE vx>=20")
        dock_cell = mo.query_one(f"SELECT occ,label FROM {DB}.{VOX} WHERE vx=5 AND vy=5 AND vz=1")
        print(f"  MERGE WHEN CONFLICT SKIP -> merged robot-2's new region "
              f"(corridor voxels vx>=20: {before} -> {after}); kept central's value on "
              f"the contested dock cell = occ {dock_cell[0]}, label '{dock_cell[1]}'")
        print("  => central authority wins the contested cell (SKIP); non-conflicting "
              "discoveries fold in. WHEN CONFLICT ACCEPT would let the robot override.")
        mo.execute(f"DROP TABLE {DB}.{VOX}_b")

        # ============================================================ ACT 5
        hr("ACT 5 — roll back a glitched sensor batch (phantom obstacles)")
        g4d.snapshot(mo, "rob_good", DB, VOX)
        good_n, _ = voxel_stats(mo)
        # a faulty sensor injects 50 phantom high-occupancy voxels in mid-air
        mo.execute(
            f"INSERT INTO {DB}.{STREAM} (robot_id, ts, x, y, z) "
            f"SELECT 1, now(), 30 + result%5, 30 + result%5, 30 + result%2 "
            f"FROM generate_series(0,499) g"
        )
        consolidate(mo, since_id=wm)
        bad_n, _ = voxel_stats(mo)
        print(f"  glitch batch consolidated: voxels {good_n} -> {bad_n} (phantom "
              f"obstacles at z~30, would make the planner avoid empty air)")
        g4d.restore_table(mo, DB, VOX, "rob_good", account=acct)
        restored_n, _ = voxel_stats(mo)
        print(f"  RESTORE {VOX} FROM SNAPSHOT rob_good -> voxels back to {restored_n} "
              f"(phantoms gone, exactly the trusted state)")

        # ============================================================ baseline
        hr("DuckDB baseline — analytics parity, but NO native version control")
        try:
            import duckdb
            rows = mo.query(f"SELECT vx,vy,vz,occ,label FROM {DB}.{VOX}")
            d = duckdb.connect()
            d.execute("CREATE TABLE voxel_map (vx INT,vy INT,vz INT,occ INT,label VARCHAR)")
            d.executemany("INSERT INTO voxel_map VALUES (?,?,?,?,?)", [list(r) for r in rows])
            qx, qy, qz = 5, 5, 1
            near = d.execute(
                "SELECT vx,vy,vz,occ, (vx-?)*(vx-?)+(vy-?)*(vy-?)+(vz-?)*(vz-?) d2 "
                "FROM voxel_map ORDER BY d2 LIMIT 3",
                [qx, qx, qy, qy, qz, qz]).fetchall()
            print(f"  DuckDB ran the SAME 3D nearest-voxel query fine: "
                  f"{[(r[0],r[1],r[2],r[3]) for r in near]}")
            print("  BUT to 'snapshot' a map version DuckDB only offers a full physical")
            print("  copy:  CREATE TABLE voxel_v1 AS SELECT * FROM voxel_map  (O(n), no CoW);")
            print("  and there is NO time-travel {snapshot=}, NO row-level DATA BRANCH")
            print("  DIFF/MERGE with conflict policy, NO RESTORE, NO microsecond PITR.")
            print("  Drift/merge/rollback would all be hand-rolled SQL + external file mgmt.")
            d.close()
        except ImportError:
            print("  (duckdb not installed; pip install duckdb to see the baseline)")

        # ============================================================ compare
        hr("MatrixOne vs the usual robot-memory stacks (for this workflow)")
        print("  rosbag / MCAP : great at RECORDING the raw sensor stream (append-only log)")
        print("      + replay; but it is a log file, not a queryable/branchable map — no")
        print("      3D query, no row-level diff/merge, no time-travel-as-of-version.")
        print("  OctoMap/TSDF  : the gold standard 3D occupancy data STRUCTURE (octree, ray-")
        print("      casting, probabilistic fusion) — far richer 3D semantics than our grid,")
        print("      but it is an in-process lib with no SQL, no multi-robot merge-with-")
        print("      conflict, no snapshot/rollback/PITR as a data-management layer.")
        print("  TSDB (Influx/Timescale): excellent time-windowed sensor rollups + retention,")
        print("      but no spatial voxel merge, no branch/diff/merge of map versions.")
        print("  PostGIS / pgRouting: real 3D geometry + spatial indexes (richer than FLOOR")
        print("      voxels); but versioning is manual (table copies / audit triggers), no")
        print("      built-in CoW snapshot, branch, row-level merge-with-conflict, or PITR.")
        print("  vector DB     : good for nearest-NEIGHBOR over embeddings, not for an")
        print("      occupancy grid with incremental fusion + versioned diff/merge.")
        print("  MatrixOne     : NOT a 3D engine — no octree/raycasting/true spatial index,")
        print("      voxels are emulated with FLOOR()+arithmetic distance. What it uniquely")
        print("      bundles HERE: SQL voxel fusion + git4data (snapshot/time-travel, row-")
        print("      level DIFF for drift, branch+MERGE-with-conflict for fleet maps,")
        print("      RESTORE/PITR for rollback) over the SAME data, in one ACID database.")

        # ---- cleanup (only ever touches mld_robot + its snapshots) ----
        for s in ("rob_v1", "rob_v2", "rob_good"):
            mo.execute(f"DROP SNAPSHOT IF EXISTS {s}")
        mo.execute(f"DROP DATABASE IF EXISTS {DB}")


if __name__ == "__main__":
    main()
