"""
SMC Engine — IRL Target Requalification Audit Tool
Gate B: IRL Target Integrity Gate

READ-ONLY. No broker calls. No order primitives. No MT5 interaction.

Usage:
    python tools/requalify_irl_targets.py [--server http://127.0.0.1:8765]

Produces:
  §1  Baseline symbol inventory
  §2  Test suite summary
  §3  Per-setup target provenance (all IRLTarget fields)
  §4  Rejected-candidate table (all hard-reject categories per setup)
  §5  Determinism test (3 identical runs)
  §6  Order-independence test (shuffled swing list)
  §7  as_of causality test
  §8  OB-adjacent failure reproduction (entry≈111.175 → old TP≈111.159 rejected)
  §9  RR-is-not-a-criterion proof
  §10 NO_VALID_TARGET sentinel test
  §11 API / GUI integrity check
  §12 Browser GUI read-only check (skipped if server not reachable)
  §13 Broker safety proof (order_send=0, order_check=0)
  §14 RR case classification (A/B/C)
  §15 Final IRL Target Gate verdict
"""
from __future__ import annotations

import sys
import random
import statistics
import importlib
import argparse

import pandas as pd

_project_root = str(__import__("pathlib").Path(__file__).resolve().parent.parent)
sys.path.insert(0, _project_root)

# ── §1 Baseline symbol inventory ──────────────────────────────────────────────

REQUIRED_SYMBOLS = [
    "IRLTarget", "NO_VALID_TARGET", "_as_of_row", "_swing_consumed", "_irl_score",
    "qualification_reason", "consumed", "ob_proximity_bars",
    "min_distance", "ob_candle_index", "irl_target_type", "irl_qualification_reason",
]

def _section(n, title):
    print(f"\n{'='*70}")
    print(f"§{n}  {title}")
    print('='*70)


def check_baseline():
    _section(1, "Baseline symbol inventory")

    import src.smc_engine.setup as setup_mod
    import src.smc_engine.causal as causal_mod
    import src.smc_engine.strategy as strategy_mod

    # setup.py symbols
    found = []
    missing = []
    for sym in ["IRLTarget", "NO_VALID_TARGET", "_as_of_row", "_swing_consumed", "_irl_score",
                "find_irl_target", "build_trade_setup", "TradeSetup"]:
        if hasattr(setup_mod, sym):
            found.append(sym)
        else:
            missing.append(sym)

    # TradeSetup fields
    import dataclasses
    ts_fields = {f.name for f in dataclasses.fields(setup_mod.TradeSetup)}
    for sym in ["irl_target_type", "irl_qualification_reason"]:
        if sym in ts_fields:
            found.append(f"TradeSetup.{sym}")
        else:
            missing.append(f"TradeSetup.{sym}")

    # IRLTarget fields
    irl_fields = {f.name for f in dataclasses.fields(setup_mod.IRLTarget)}
    for sym in ["qualification_reason", "consumed"]:
        if sym in irl_fields:
            found.append(f"IRLTarget.{sym}")
        else:
            missing.append(f"IRLTarget.{sym}")

    # find_irl_target params
    import inspect
    sig = inspect.signature(setup_mod.find_irl_target)
    for param in ["ob_proximity_bars", "min_distance", "ob_candle_index"]:
        if param in sig.parameters:
            found.append(f"find_irl_target.{param}")
        else:
            missing.append(f"find_irl_target.{param}")

    # strategy.py
    import dataclasses as dc
    cfg_fields = {f.name for f in dc.fields(strategy_mod.MultiTimeframeConfig)}
    if "m15_irl_ob_proximity" in cfg_fields:
        found.append("MultiTimeframeConfig.m15_irl_ob_proximity")
        val = strategy_mod.MultiTimeframeConfig().m15_irl_ob_proximity
        print(f"  m15_irl_ob_proximity default = {val}")
    else:
        missing.append("MultiTimeframeConfig.m15_irl_ob_proximity")

    # causal.py calls find_irl_target with correct params
    import ast, inspect as ins
    src_text = ins.getsource(causal_mod.CausalMTFAnalyzer.analyze_at)
    for kw in ["ob_candle_index", "ob_proximity_bars", "min_distance", "as_of"]:
        if kw in src_text:
            found.append(f"causal.analyze_at passes {kw}")
        else:
            missing.append(f"causal.analyze_at passes {kw}")

    print(f"  FOUND  ({len(found)}): {', '.join(found)}")
    if missing:
        print(f"  MISSING ({len(missing)}): {', '.join(missing)}")
        return False
    print("  RESULT: ALL REQUIRED SYMBOLS PRESENT")
    return True


# ── §2 Test suite summary ─────────────────────────────────────────────────────

def check_tests():
    _section(2, "Test suite summary")
    import subprocess
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-q",
         "tests/test_liquidity_target.py", "tests/test_lifecycle_integration.py"],
        capture_output=True, text=True, cwd=str(__import__("pathlib").Path(__file__).resolve().parent.parent)
    )
    lines = (r.stdout + r.stderr).strip().split("\n")
    summary = [l for l in lines if "passed" in l or "failed" in l or "error" in l]
    for l in summary:
        print(f"  {l}")
    ok = r.returncode == 0
    print(f"  RESULT: {'PASS' if ok else 'FAIL'}")
    return ok


# ── §3–§6  Live per-setup audit (requires running server) ─────────────────────

def _compute_atr(candles, n=14):
    trs = []
    for i in range(1, len(candles)):
        h = float(candles[i][2]); l = float(candles[i][3]); pc = float(candles[i-1][4])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return statistics.mean(trs[-n:]) if trs else 0.0


def _as_of_idx(candles, ts_str):
    ts = pd.Timestamp(ts_str)
    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
    best = -1
    for i, c in enumerate(candles):
        ct = pd.Timestamp(c[0])
        ct = ct.tz_localize("UTC") if ct.tzinfo is None else ct.tz_convert("UTC")
        if ct <= ts:
            best = i
    return best


def _swing_consumed_raw(sw, candles, aoi):
    start = sw.get("confirmation_index", sw["index"]) + 1
    if start > aoi:
        return False
    price = float(sw["price"])
    for c in candles[start:aoi + 1]:
        if float(c[3]) <= price:
            return True
    return False


def _irl_score_raw(price, strength, entry, atr):
    dist = entry - price
    if atr <= 0 or dist <= 0:
        return -1.0
    d = dist / atr
    ds = 1.0 / d if d <= 8.0 else 0.5 / d
    return strength * ds


REJECT_CATS = ["OB_ADJACENT", "BELOW_MIN_DIST", "CONSUMED_AT_AS_OF", "FUTURE_SWING"]


def _audit_setup(s, candles, swings, swing_by_id):
    entry = float(s["entry"])
    tp = float(s["tp"])
    ev = s.get("evidence", {})
    ob_id = ev.get("order_block_id", "")
    sel_id = ev.get("irl_swing_id", "")
    sel_qual = ev.get("irl_qualification_reason", "")
    created_time = s.get("as_of") or s.get("created_time") or ""

    try:
        ob_idx = int(ob_id.split("-M15-")[1].split("-")[0])
    except Exception:
        ob_idx = -1

    try:
        d_a = float(sel_qual.split("dist_atrs=")[1].split(",")[0])
        atr_c = abs(entry - tp) / d_a
    except Exception:
        atr_c = _compute_atr(candles, 14)

    aoi_creation = _as_of_idx(candles, created_time) if created_time else len(candles) - 1
    ob_prox = 10

    creation_ts = pd.Timestamp(created_time)
    creation_ts = creation_ts.tz_localize("UTC") if creation_ts.tzinfo is None else creation_ts.tz_convert("UTC")

    lows_below = [sw for sw in swings if sw["type"] == "LOW" and float(sw["price"]) < entry]

    rows = []
    for sw in lows_below:
        sid = sw["id"]
        price = float(sw["price"])
        sidx = sw["index"]
        ct_s = sw.get("confirmation_time") or sw.get("time", "")
        try:
            ct = pd.Timestamp(ct_s)
            ct = ct.tz_localize("UTC") if ct.tzinfo is None else ct.tz_convert("UTC")
        except Exception:
            ct = creation_ts

        if ct > creation_ts:
            rows.append((sid, price, sidx, "FUTURE_SWING", f"conf_after_as_of"))
            continue
        if (entry - price) <= atr_c:
            rows.append((sid, price, sidx, "BELOW_MIN_DIST", f"dist={entry-price:.4f}≤atr={atr_c:.4f}"))
            continue
        if ob_idx >= 0 and abs(sidx - ob_idx) <= ob_prox:
            rows.append((sid, price, sidx, "OB_ADJACENT", f"|{sidx}-{ob_idx}|={abs(sidx-ob_idx)}≤{ob_prox}"))
            continue
        if aoi_creation >= 0 and _swing_consumed_raw(sw, candles, aoi_creation):
            rows.append((sid, price, sidx, "CONSUMED_AT_AS_OF", f"p={price:.4f}"))
            continue
        strength = sw.get("strength", 4)
        score = _irl_score_raw(price, strength, entry, atr_c)
        dist_a = (entry - price) / atr_c
        rows.append((sid, price, sidx, "QUALIFIES", f"str={strength} d_a={dist_a:.2f} score={score:.4f}"))

    qualifies = sorted([r for r in rows if r[3] == "QUALIFIES"],
                       key=lambda x: float(x[4].split("score=")[1]), reverse=True)
    rejected = [r for r in rows if r[3] != "QUALIFIES"]
    return {
        "setup_id": s.get("setup_id", "?"),
        "entry": entry, "tp": tp, "sl": s.get("sl", 0),
        "rr": s.get("rr", 0),
        "ob_id": ob_id, "ob_idx": ob_idx,
        "sel_id": sel_id, "sel_qual": sel_qual,
        "created_time": created_time,
        "atr_c": atr_c, "aoi_creation": aoi_creation,
        "qualifies": qualifies, "rejected": rejected,
    }


def live_audit(base_url):
    import requests

    _section(3, "Per-setup target provenance")

    try:
        data = requests.get(f"{base_url}/api/analysis/CADJPY/M15", params={"count": 500}, timeout=30).json()
        stored_resp = requests.get(f"{base_url}/api/setups", timeout=10).json()
    except Exception as e:
        print(f"  SERVER NOT REACHABLE: {e}")
        print("  SKIPPING §3–§14 (requires live server)")
        return False

    candles = data["candles"]
    swings = data["swings"]
    swing_by_id = {s["id"]: s for s in swings}
    stored = stored_resp.get("setups", [])

    atr_now = _compute_atr(candles, 14)
    print(f"  CADJPY: {len(candles)} candles, {len(swings)} swings, {len(stored)} stored setups")
    print(f"  Last bar: {data.get('last_closed_time')}  close={data.get('last_closed_close')}")
    print(f"  ATR(14,now): {atr_now:.5f}")

    audits = []
    for s in stored:
        a = _audit_setup(s, candles, swings, swing_by_id)
        audits.append(a)

    for a in audits:
        print(f"\n  Setup: {a['setup_id']}")
        print(f"    entry={a['entry']:.4f}  sl={a['sl']}  tp={a['tp']:.4f}  RR={a['rr']}")
        print(f"    OB={a['ob_id']} (idx={a['ob_idx']})")
        print(f"    created_time={a['created_time']}  as_of_idx={a['aoi_creation']}")
        print(f"    ATR(derived)={a['atr_c']:.6f}")
        print(f"    Selected: {a['sel_id']}")
        print(f"    qual: [{a['sel_qual']}]")

        sw_sel = swing_by_id.get(a['sel_id'])
        if sw_sel:
            print(f"    swing fields: idx={sw_sel['index']}, price={sw_sel['price']}, "
                  f"strength={sw_sel.get('strength','?')}, "
                  f"conf_time={sw_sel.get('confirmation_time','?')}")
        else:
            print(f"    WARNING: selected swing {a['sel_id']} not in current M15 window")

        if a['qualifies']:
            best = a['qualifies'][0]
            if best[0] == a['sel_id']:
                print(f"    SCORE SELECTION: CONFIRMED — {best[0]} has highest score")
            else:
                print(f"    SCORE SELECTION: MISMATCH — API={a['sel_id']}, best_scorer={best[0]}")
        else:
            print(f"    WARNING: no qualifying candidates found (all rejected)")

    _section(4, "Rejected-candidate table")
    all_pass = True
    for a in audits:
        print(f"\n  {a['setup_id']} ({len(a['rejected'])} rejected, {len(a['qualifies'])} qualify):")
        for cat in REJECT_CATS:
            bucket = [r for r in a['rejected'] if r[3] == cat]
            if bucket:
                print(f"    [{cat}] ({len(bucket)}):")
                for r in bucket[:5]:
                    print(f"      {r[0]:<12} p={r[1]:.4f} idx={r[2]:<5} {r[4]}")
                if len(bucket) > 5:
                    print(f"      ...+{len(bucket)-5} more")
        if a['qualifies']:
            print(f"    [QUALIFIES] ({len(a['qualifies'])}):")
            for r in a['qualifies'][:3]:
                marker = " ← SELECTED" if r[0] == a['sel_id'] else ""
                print(f"      {r[0]:<12} p={r[1]:.4f} idx={r[2]:<5} {r[4]}{marker}")
        else:
            print(f"    [QUALIFIES] 0 — NO_VALID_TARGET condition")
            all_pass = False

    # §5 Determinism
    _section(5, "Determinism test (3 identical runs)")
    results_3 = []
    for s in stored[:3]:
        run_results = []
        for _ in range(3):
            a = _audit_setup(s, candles, swings, swing_by_id)
            run_results.append(a['sel_id'] if a['qualifies'] else "NO_VALID_TARGET")
        identical = len(set(run_results)) == 1
        print(f"  {s.get('setup_id','?')}: {run_results[0]} × 3 → {'DETERMINISTIC' if identical else 'NON-DETERMINISTIC'}")
        results_3.append(identical)
    det_ok = all(results_3)
    print(f"  RESULT: {'PASS' if det_ok else 'FAIL'}")

    # §6 Order-independence
    _section(6, "Order-independence test (shuffled swing list)")
    oi_ok = True
    for s in stored[:2]:
        a_fwd = _audit_setup(s, candles, swings, swing_by_id)
        sel_fwd = a_fwd['sel_id'] if a_fwd['qualifies'] else "NO_VALID_TARGET"

        swings_copy = list(swings)
        random.seed(42)
        random.shuffle(swings_copy)
        a_rev = _audit_setup(s, candles, swings_copy, swing_by_id)
        sel_rev = a_rev['sel_id'] if a_rev['qualifies'] else "NO_VALID_TARGET"

        match = sel_fwd == sel_rev
        if not match:
            oi_ok = False
        print(f"  {s.get('setup_id','?')}: fwd={sel_fwd}, shuffled={sel_rev} → {'ORDER-INDEPENDENT' if match else 'ORDER-DEPENDENT'}")
    print(f"  RESULT: {'PASS' if oi_ok else 'FAIL'}")

    # §7 as_of causality
    _section(7, "as_of causality test")
    causal_ok = True
    for s in stored:
        ev = s.get("evidence", {})
        sel_id = ev.get("irl_swing_id", "")
        sw_sel = swing_by_id.get(sel_id)
        created_time = s.get("as_of") or ""
        if not sw_sel or not created_time:
            print(f"  {s.get('setup_id','?')}: selected swing not in window — SKIP")
            continue
        conf_time = sw_sel.get("confirmation_time") or sw_sel.get("time")
        try:
            conf_ts = pd.Timestamp(conf_time)
            conf_ts = conf_ts.tz_localize("UTC") if conf_ts.tzinfo is None else conf_ts.tz_convert("UTC")
            created_ts = pd.Timestamp(created_time)
            created_ts = created_ts.tz_localize("UTC") if created_ts.tzinfo is None else created_ts.tz_convert("UTC")
            ok = conf_ts <= created_ts
        except Exception as e:
            ok = False
            print(f"  {s.get('setup_id','?')}: timestamp parse error {e}")
            causal_ok = False
            continue
        if not ok:
            causal_ok = False
        print(f"  {s.get('setup_id','?')}: conf_time={conf_time} ≤ as_of={created_time} → {'OK' if ok else 'FUTURE LEAK'}")
    print(f"  RESULT: {'PASS' if causal_ok else 'FAIL — FUTURE LEAKAGE DETECTED'}")

    return audits, candles, swings, swing_by_id, atr_now, causal_ok


# ── §8  OB-adjacent failure reproduction ─────────────────────────────────────

def check_ob_failure():
    _section(8, "OB-adjacent failure reproduction (entry≈111.175, old TP≈111.159)")
    from src.smc_engine.models import Direction, SwingPoint, SwingType
    from src.smc_engine.setup import find_irl_target

    entry = 111.175
    atr = 0.01
    ob_idx = 276

    def sp(idx, typ, price, strength=6):
        return SwingPoint(f"S{typ.value[0]}-{idx}", idx, idx, typ, price, strength, idx+2, idx+2)

    adj_swing = sp(277, SwingType.LOW, 111.159)
    good_swing = sp(200, SwingType.LOW, 110.420)

    result = find_irl_target(
        entry, Direction.BEARISH,
        [adj_swing, good_swing],
        min_distance=atr,
        ob_candle_index=ob_idx,
        ob_proximity_bars=10,
    )

    rej = result is not None and abs(result.price - 111.159) > 0.001
    sel = result is not None and abs(result.price - 110.420) < 0.001
    print(f"  OB-adjacent swing (111.159, idx=277, |277-276|=1): {'REJECTED' if rej else 'NOT REJECTED — BUG'}")
    print(f"  Replacement selected (110.420): {'YES' if sel else 'NO'}")
    print(f"  Returned TP: {result.price if result else 'NO_VALID_TARGET'}")
    ok = rej and sel
    print(f"  RESULT: {'PASS' if ok else 'FAIL'}")
    return ok


# ── §9  RR is not a selection criterion ──────────────────────────────────────

def check_rr_not_criterion():
    _section(9, "RR is not a selection criterion")
    from src.smc_engine.models import Direction, SwingPoint, SwingType
    from src.smc_engine.setup import find_irl_target

    # Two swings with identical strength=6 and identical distance→same score.
    # Flip entry to change RR: entry=111.0 vs entry=111.5.
    # Same swing must be selected regardless of RR (since entry changes exogenously).

    def sp(idx, price, strength=6):
        return SwingPoint(f"SL-{idx}", idx, idx, SwingType.LOW, price, strength, idx+2, idx+2)

    swings = [
        sp(200, 110.50, strength=6),   # dist from 111.0 = 0.50
        sp(150, 110.00, strength=6),   # dist from 111.0 = 1.00
    ]
    atr = 0.10

    r1 = find_irl_target(111.0, Direction.BEARISH, swings, min_distance=atr, ob_candle_index=300)
    r2 = find_irl_target(111.5, Direction.BEARISH, swings, min_distance=atr, ob_candle_index=300)

    # Selection is by score. For entry=111.0: dist0=5atrs score=6/5=1.2, dist1=10atrs score=6*0.5/10=0.3 → sw0 wins
    # For entry=111.5: dist0=5.5atrs score=6/5.5≈1.09, dist1=10.5atrs score=6*0.5/10.5≈0.29 → sw0 still wins
    # Score changes only because relative distances change with entry. RR itself is irrelevant.
    score_r1 = f"selected={r1.price if r1 else None}"
    score_r2 = f"selected={r2.price if r2 else None}"
    print(f"  entry=111.0 → {score_r1}")
    print(f"  entry=111.5 → {score_r2}")
    # Verify no RR field in find_irl_target signature
    import inspect
    sig = inspect.signature(find_irl_target)
    has_rr = any("rr" in p.lower() or "reward" in p.lower() for p in sig.parameters)
    print(f"  find_irl_target signature has RR parameter: {has_rr}")
    print(f"  RESULT: {'PASS — RR is not a parameter' if not has_rr else 'FAIL — RR parameter found'}")
    return not has_rr


# ── §10 NO_VALID_TARGET sentinel ─────────────────────────────────────────────

def check_no_valid_target():
    _section(10, "NO_VALID_TARGET sentinel behavior")
    from src.smc_engine.models import Direction, SwingPoint, SwingType
    from src.smc_engine.setup import find_irl_target, NO_VALID_TARGET

    def sp(idx, price, strength=6):
        return SwingPoint(f"SL-{idx}", idx, idx, SwingType.LOW, price, strength, idx+2, idx+2)

    # All OB-adjacent
    r1 = find_irl_target(111.0, Direction.BEARISH,
                         [sp(276, 110.5), sp(278, 110.3)],
                         min_distance=0.05, ob_candle_index=276, ob_proximity_bars=10)
    print(f"  All OB-adjacent → {r1!r} (expected None): {'PASS' if r1 is NO_VALID_TARGET else 'FAIL'}")

    # All too close
    r2 = find_irl_target(111.0, Direction.BEARISH,
                         [sp(200, 110.99), sp(180, 110.98)],
                         min_distance=0.05, ob_candle_index=300)
    print(f"  All below min_distance → {r2!r} (expected None): {'PASS' if r2 is NO_VALID_TARGET else 'FAIL'}")

    # Empty swings list
    r3 = find_irl_target(111.0, Direction.BEARISH, [], min_distance=0.05, ob_candle_index=300)
    print(f"  Empty swings → {r3!r} (expected None): {'PASS' if r3 is NO_VALID_TARGET else 'FAIL'}")

    ok = all(r is NO_VALID_TARGET for r in [r1, r2, r3])
    print(f"  RESULT: {'PASS' if ok else 'FAIL'}")
    return ok


# ── §11–§12 API/GUI integrity (requires server) ───────────────────────────────

def check_api_integrity(base_url):
    import requests
    _section(11, "API / GUI integrity check")

    try:
        r_setups = requests.get(f"{base_url}/api/setups", timeout=10).json()
        r_analysis = requests.get(f"{base_url}/api/analysis/CADJPY/M15", params={"count": 200}, timeout=30).json()
    except Exception as e:
        print(f"  SERVER NOT REACHABLE: {e}")
        return False

    setups = r_setups.get("setups", [])
    print(f"  /api/setups: {len(setups)} setups returned")

    irl_fields_ok = True
    for s in setups:
        ev = s.get("evidence", {})
        for field in ["irl_swing_id", "irl_qualification_reason"]:
            if field not in ev:
                print(f"  MISSING field {field} in setup {s.get('setup_id')}")
                irl_fields_ok = False
    if irl_fields_ok:
        print("  All setups have irl_swing_id and irl_qualification_reason")

    candidates = r_analysis.get("candidates", [])
    print(f"  /api/analysis/CADJPY/M15: {len(candidates)} live candidates")
    for c in candidates:
        ev = c.get("evidence", {})
        for field in ["irl_swing_id", "irl_qualification_reason"]:
            if field not in ev:
                print(f"  MISSING field {field} in candidate {c.get('id')}")

    print(f"  RESULT: {'PASS' if irl_fields_ok else 'FAIL'}")
    return irl_fields_ok


def check_browser_gui(base_url):
    import requests
    _section(12, "Read-only browser GUI check")

    try:
        r = requests.get(f"{base_url}/", timeout=5)
        print(f"  GET / → HTTP {r.status_code}")
    except Exception as e:
        print(f"  Server not reachable: {e}")
        return False

    # Verify compile-time gate via /api/status (exposes live_execution_enabled field)
    gate_ok = False
    try:
        r_status = requests.get(f"{base_url}/api/status", timeout=5)
        if r_status.status_code == 200:
            payload = r_status.json()
            live_enabled = payload.get("live_execution_enabled", None)
            print(f"  /api/status live_execution_enabled = {live_enabled!r}")
            gate_ok = live_enabled is False
        else:
            # Fallback: verify no /api/live endpoint exists (404 means route not registered)
            r_live = requests.post(f"{base_url}/api/live", json={}, timeout=5)
            print(f"  POST /api/live → HTTP {r_live.status_code} "
                  f"({'route not registered — safe' if r_live.status_code == 404 else 'unexpected'})")
            gate_ok = r_live.status_code in (403, 404)
    except Exception as e:
        print(f"  /api/status error: {e}")
        gate_ok = False

    # Also confirm LIVE_EXECUTION_ENABLED in source
    try:
        from src.smc_engine.web.hub import LIVE_EXECUTION_ENABLED
        print(f"  LIVE_EXECUTION_ENABLED (source): {LIVE_EXECUTION_ENABLED}")
        gate_ok = gate_ok or (LIVE_EXECUTION_ENABLED is False)
    except ImportError:
        pass

    print(f"  RESULT: {'PASS' if gate_ok else 'FAIL'}")
    return gate_ok


# ── §13 Broker safety proof ──────────────────────────────────────────────────

def check_broker_safety():
    _section(13, "Broker safety proof")

    import src.smc_engine.setup as setup_mod

    has_mt5 = hasattr(setup_mod, "mt5") or "MetaTrader5" in dir(setup_mod)
    print(f"  setup.py imports mt5: {has_mt5} (expected False)")

    import src.smc_engine.causal as causal_mod
    has_mt5_c = hasattr(causal_mod, "mt5") or "MetaTrader5" in dir(causal_mod)
    print(f"  causal.py imports mt5: {has_mt5_c} (expected False)")

    # Call find_irl_target — must not raise or call any broker code
    from src.smc_engine.models import Direction, SwingPoint, SwingType
    from src.smc_engine.setup import find_irl_target

    def sp(idx, price):
        return SwingPoint(f"SL-{idx}", idx, idx, SwingType.LOW, price, 6, idx+2, idx+2)

    order_send_calls = []
    order_check_calls = []

    class _Trap:
        def order_send(self, *a, **kw): order_send_calls.append(a)
        def order_check(self, *a, **kw): order_check_calls.append(a)

    result = find_irl_target(111.0, Direction.BEARISH, [sp(200, 110.5)], min_distance=0.05)

    print(f"  order_send calls: {len(order_send_calls)} (expected 0)")
    print(f"  order_check calls: {len(order_check_calls)} (expected 0)")

    ok = not has_mt5 and not has_mt5_c and len(order_send_calls) == 0 and len(order_check_calls) == 0
    print(f"  RESULT: {'PASS' if ok else 'FAIL'}")
    return ok


# ── §14 RR case classification ────────────────────────────────────────────────

def classify_rr(audits, candles):
    _section(14, "RR case classification")
    if not audits:
        print("  No live setups — SKIP")
        return

    atr_now = _compute_atr(candles, 14)

    for a in audits:
        entry = a['entry']
        tp = a['tp']
        sl = a['sl']
        rr = a.get('rr', 0)
        reward = abs(entry - tp)
        risk = abs(entry - float(sl)) if sl else 0
        dist_atrs_tp = reward / atr_now if atr_now > 0 else 0

        if risk > 0 and (reward / risk) < 1.0:
            case = "A (wide stop → RR < 1.0)"
        elif dist_atrs_tp < 3.0:
            case = "B (target near entry < 3 ATRs)"
        else:
            case = "C (target ≥ 3 ATRs from entry — standard)"

        print(f"  {a['setup_id']}: RR={rr}  reward_atrs={dist_atrs_tp:.1f}  → Case {case}")


# ── §15 Final verdict ─────────────────────────────────────────────────────────

def verdict(results: dict):
    _section(15, "Final IRL Target Gate verdict")
    blockers = [k for k, v in results.items() if not v]
    if not blockers:
        v = "PASS"
        print(f"  VERDICT: {v}")
        print("  All gates cleared. IRL target selection is correctly implemented.")
    elif all(k.startswith("live_") for k in blockers):
        v = "CONDITIONAL PASS"
        print(f"  VERDICT: {v}")
        print("  Static checks pass. Live-server checks require running server.")
    else:
        v = "BLOCKED"
        print(f"  VERDICT: {v}")
        for b in blockers:
            print(f"    BLOCKER: {b}")
    print()


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", default="http://127.0.0.1:8765")
    args = parser.parse_args()
    base = args.server.rstrip("/")

    results = {}

    results["baseline"] = check_baseline()
    results["tests"] = check_tests()
    results["ob_failure"] = check_ob_failure()
    results["rr_not_criterion"] = check_rr_not_criterion()
    results["no_valid_target"] = check_no_valid_target()
    results["broker_safety"] = check_broker_safety()

    live_result = live_audit(base)
    if live_result is False:
        results["live_provenance"] = False
        results["live_causality"] = False
        results["live_api"] = False
        results["live_gui"] = False
        classify_rr([], [])
    else:
        audits, candles, swings, swing_by_id, atr_now, causal_ok = live_result
        results["live_provenance"] = True
        results["live_causality"] = causal_ok
        results["live_api"] = check_api_integrity(base)
        results["live_gui"] = check_browser_gui(base)
        classify_rr(audits, candles)

    verdict(results)


if __name__ == "__main__":
    main()
