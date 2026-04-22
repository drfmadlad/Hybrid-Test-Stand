#!/usr/bin/env python3
"""
Polaris CAN Log Analyzer

Analyzes CAN sniffer logs to help identify message patterns and decode signals.
Known J1939 signals (RPM, coolant temp, battery voltage, fuel level, etc.) are
decoded automatically from the dashboard.py findings.

Usage:
    python analyze_can.py <logfile.csv> [options]

Options:
    --summary           Show ID summary with J1939 decode (default)
    --id <hex_id>       Deep-dive a specific CAN ID
    --scan              Show all changing bytes across ALL IDs — use to hunt unknown signals (AFR etc.)
    --unknown           In summary/scan, show only unrecognized PGNs
    --compare <f1> <f2> Compare two log files
    --rate              Show message rates per ID over time
"""

import sys
import re
import pandas as pd
import argparse
from collections import defaultdict
import os

_DATA_COL_RE = re.compile(r'^d[0-7]$')


# ── J1939 parsing (ported from dashboard.py) ─────────────────────────────────

def j1939_parse(can_id: int):
    """Break a 29-bit J1939 extended ID into (pgn, sa, priority)."""
    pf  = (can_id >> 16) & 0xFF
    sa  = can_id & 0xFF
    pri = (can_id >> 26) & 0x7
    if pf >= 0xF0:                    # PDU2: broadcast
        ge  = (can_id >> 8) & 0xFF
        pgn = (pf << 8) | ge
    else:                             # PDU1: addressed, DA not part of PGN
        pgn = pf << 8
    return pgn, sa, pri


def _try_j1939(id_str: str):
    """Return (pgn, sa, pri) if id_str looks like a 29-bit J1939 ID, else None."""
    try:
        v = int(id_str, 16)
        if v > 0x7FF:                 # 11-bit standard IDs stay < 0x800
            return j1939_parse(v)
    except ValueError:
        pass
    return None


# ── J1939 signal decoders (confirmed from dashboard.py) ──────────────────────

def _u16le(d, i):
    v = (d[i + 1] << 8) | d[i]
    return None if v == 0xFFFF else v

def _u8(d, i):
    return None if d[i] == 0xFF else d[i]

def _fmt(val, scale, unit, digits=1):
    if val is None:
        return "N/A"
    return f"{val * scale:.{digits}f} {unit}".strip()


def decode_eec1(d):
    """PGN 0xF004 — Engine Speed (confirmed)"""
    rpm_raw = _u16le(d, 3)
    return {"RPM": _fmt(rpm_raw, 0.125, "RPM", 0)}

def decode_eec2(d):
    """PGN 0xF005 — Throttle / Engine Load (N/A on this ECU)"""
    tps  = _u8(d, 1)
    load = _u8(d, 2)
    return {"TPS": _fmt(tps, 0.4, "%", 0), "Load": _fmt(load, 0.4, "%", 0)}

def decode_eec3(d):
    """PGN 0xF006 — Governor Speed"""
    gov_raw = _u16le(d, 0)
    return {"GovRPM": _fmt(gov_raw, 0.125, "RPM", 0)}

def decode_et1(d):
    """PGN 0xFEEE — Coolant Temperature (confirmed)"""
    raw  = _u8(d, 0)
    temp = None if raw is None else raw - 40
    return {"Coolant": "N/A" if temp is None else f"{temp} °C"}

def decode_ccvs(d):
    """PGN 0xFEF1 — Vehicle Speed"""
    spd_raw = _u16le(d, 1)
    return {"Speed": _fmt(spd_raw, 1.0 / 256.0, "km/h", 1)}

def decode_lfe(d):
    """PGN 0xFEF2 — Fuel Rate / Throttle"""
    rate_raw = _u16le(d, 0)
    throttle = _u8(d, 6)
    return {"FuelRate": _fmt(rate_raw, 0.05, "L/h", 2),
            "Throttle": _fmt(throttle, 0.4, "%", 0)}

def decode_ambc(d):
    """PGN 0xFEF5 — Ambient Conditions"""
    baro    = _u8(d, 0)
    iat_raw = _u16le(d, 3)
    iat     = None if iat_raw is None else round(iat_raw * 0.03125 - 273.0, 1)
    return {"Baro": _fmt(baro, 0.5, "kPa", 1),
            "IAT":  "N/A" if iat is None or iat < -39 else f"{iat:.1f} °C"}

def decode_hours(d):
    """PGN 0xFEE5 — Engine Hours"""
    if d[0] == 0xFF and d[1] == 0xFF and d[2] == 0xFF and d[3] == 0xFF:
        return {"Hours": "N/A"}
    raw = d[0] | (d[1] << 8) | (d[2] << 16) | (d[3] << 24)
    return {"Hours": f"{raw * 0.05:.1f} hr"}

def decode_vep(d):
    """PGN 0xFEF7 — Battery Voltage (confirmed)"""
    raw = _u16le(d, 2)
    return {"BattV": _fmt(raw, 0.05, "V", 2)}

def decode_fefc(d):
    """PGN 0xFEFC — Instrument cluster, fuel level (confirmed)"""
    raw = _u8(d, 1)
    return {"FuelLevel": _fmt(raw, 0.4, "%", 0)}

def decode_fec1(d):
    """PGN 0xFEC1 — Instrument cluster, gear?"""
    gear = _u8(d, 4)
    return {"Gear?": "N/A" if gear is None else str(gear)}

def decode_prop_ff66(d):
    """PGN 0xFF66 — Polaris proprietary RPM mirror"""
    raw = _u16le(d, 0)
    return {"RPM(prop)": _fmt(raw, 1.0, "RPM", 0)}

def decode_raw(d):
    return {}


# ── PGN table ─────────────────────────────────────────────────────────────────
# (pgn_int) -> (label, decoder_fn, confirmed?)

_PGN_TABLE = {
    # Confirmed ----------------------------------------------------------------
    0xF004: ("EEC1   Engine Speed",      decode_eec1,       True),
    0xFEEE: ("ET1    Coolant Temp",      decode_et1,        True),
    0xFEF7: ("VEP    Battery Voltage",   decode_vep,        True),
    0xFEFC: ("INSTR  Fuel Level",        decode_fefc,       True),
    0xFF66: ("PROP   Polaris RPM",       decode_prop_ff66,  True),
    # Standard J1939 present but N/A on this ECU ------------------------------
    0xF005: ("EEC2   Throttle/Load",     decode_eec2,       False),
    0xF006: ("EEC3   Gov Speed",         decode_eec3,       False),
    0xFEF1: ("CCVS   Vehicle Speed",     decode_ccvs,       False),
    0xFEF2: ("LFE    Fuel Rate",         decode_lfe,        False),
    0xFEF5: ("AMBC   Ambient",           decode_ambc,       False),
    0xFEE5: ("EH     Engine Hours",      decode_hours,      False),
    0xFEC1: ("INSTR  Gear?",             decode_fec1,       False),
    # Transport / network management ------------------------------------------
    0xEC00: ("TP.CM  [transport]",       decode_raw,        False),
    0xEB00: ("TP.DT  [transport]",       decode_raw,        False),
    0xEE00: ("AddrClaim [net]",          decode_raw,        False),
    0xEA00: ("PGNReq  [net]",            decode_raw,        False),
    0xE800: ("Ack    [net]",             decode_raw,        False),
}

_KNOWN_PGNS = set(_PGN_TABLE.keys())

def pgn_label(pgn: int):
    entry = _PGN_TABLE.get(pgn)
    if entry:
        return entry[0], entry[1], entry[2]
    return f"PGN 0x{pgn:04X}  [UNKNOWN]", decode_raw, False


# ── CSV loader ────────────────────────────────────────────────────────────────

def load_log(filepath):
    """Load CAN log file, handling common encodings and UTF-16 NUL bytes."""
    with open(filepath, "rb") as f:
        head = f.read(4096)

    if head.startswith(b"\xff\xfe") or head.startswith(b"\xfe\xff"):
        enc = "utf-16"
    elif head.count(b"\x00") > 100:
        enc = "utf-16"
    else:
        enc = "utf-8"

    df = pd.read_csv(filepath, comment="#", header=None,
                     encoding=enc, sep=",", engine="python")

    expected = ["timestamp", "seq", "id", "ext", "dlc",
                "d0", "d1", "d2", "d3", "d4", "d5", "d6", "d7"]

    if df.shape[1] < len(expected):
        raise ValueError(
            f"Expected at least {len(expected)} columns, got {df.shape[1]}. "
            f"First row: {df.iloc[0].tolist()}"
        )

    df = df.iloc[:, :len(expected)]
    df.columns = expected

    for col in df.columns:
        if df[col].dtype == object:
            df[col] = df[col].astype(str).str.replace("\x00", "", regex=False).str.strip()

    df["timestamp"] = pd.to_numeric(df["timestamp"], errors="coerce")
    df["seq"]       = pd.to_numeric(df["seq"],       errors="coerce")
    df["ext"]       = pd.to_numeric(df["ext"],       errors="coerce")
    df["dlc"]       = pd.to_numeric(df["dlc"],       errors="coerce")
    df["id"]        = df["id"].astype(str).str.strip()

    return df


def _parse_data_cols(row, data_cols):
    """Return list of int byte values (0xFF for missing/invalid)."""
    result = []
    for c in data_cols:
        x = str(row[c]).strip()
        if x in ("--", "", "nan"):
            result.append(0xFF)
        else:
            try:
                result.append(int(x, 16))
            except ValueError:
                result.append(0xFF)
    return result


# ── Summary ───────────────────────────────────────────────────────────────────

def summarize_ids(df, unknown_only=False):
    """Print summary of all CAN IDs with J1939 decode hints."""
    print("\n" + "=" * 70)
    print("CAN ID Summary")
    print("=" * 70)

    id_counts    = df["id"].value_counts().sort_index()
    total_frames = len(df)
    duration_ms  = df["timestamp"].max() - df["timestamp"].min()
    duration_s   = duration_ms / 1000

    print(f"\nCapture Duration : {duration_s:.2f} s")
    print(f"Total Frames     : {total_frames}")
    print(f"Overall Rate     : {total_frames / duration_s:.1f} frames/sec")
    print(f"Unique IDs       : {len(id_counts)}")

    data_cols = [c for c in df.columns if _DATA_COL_RE.match(c)]

    hdr = "{:<14} {:>8} {:>9}  {:<26}  {}"
    print("\n" + hdr.format("CAN ID", "Count", "Rate(Hz)", "J1939 Signal", "Last Decoded Values"))
    print("-" * 90)

    for can_id, count in id_counts.items():
        rate = count / duration_s if duration_s > 0 else 0

        j = _try_j1939(can_id)
        if j:
            pgn, sa, _ = j
            label, decoder, confirmed = pgn_label(pgn)
            is_unknown = pgn not in _KNOWN_PGNS
            if unknown_only and not is_unknown:
                continue
            marker = "  [confirmed]" if confirmed else ("  [?]" if not is_unknown else "  [UNKNOWN]")
            signal_col = f"{label}{marker}"
        else:
            if unknown_only:
                continue
            signal_col = "(11-bit std frame)"
            decoder    = decode_raw

        # Grab last message and decode it
        last_row = df[df["id"] == can_id].iloc[-1]
        d = _parse_data_cols(last_row, data_cols)
        try:
            sigs = decoder(d)
        except Exception:
            sigs = {}
        decoded_str = "  ".join(f"{k}={v}" for k, v in sigs.items()) if sigs else "—"

        print(hdr.format(can_id, count, f"{rate:.1f}", signal_col[:26], decoded_str))

    return id_counts


# ── Per-ID deep dive ──────────────────────────────────────────────────────────

def analyze_id(df, can_id):
    """Detailed analysis of a specific CAN ID."""
    if not can_id.startswith("0x"):
        can_id = "0x" + can_id.upper()

    id_df = df[df["id"] == can_id].copy()
    if len(id_df) == 0:
        print(f"No messages found for ID {can_id}")
        return

    print("\n" + "=" * 70)
    print(f"Analysis for CAN ID: {can_id}")
    print("=" * 70)

    # J1939 info
    j = _try_j1939(can_id)
    if j:
        pgn, sa, pri = j
        label, decoder, confirmed = pgn_label(pgn)
        status = "CONFIRMED" if confirmed else "standard J1939 (unconfirmed on this ECU)"
        print(f"\nJ1939  PGN=0x{pgn:04X}  SA=0x{sa:02X}  Priority={pri}")
        print(f"       {label}")
        print(f"       Status: {status}")
    else:
        decoder = decode_raw

    print(f"\nMessage count: {len(id_df)}")

    if len(id_df) > 1:
        intervals = id_df["timestamp"].diff().dropna()
        print(f"Average interval : {intervals.mean():.1f} ms  ({1000 / intervals.mean():.1f} Hz)")
        print(f"Min / Max interval: {intervals.min():.1f} / {intervals.max():.1f} ms")

    data_cols = [c for c in id_df.columns if _DATA_COL_RE.match(c)]

    # Parse all byte values once
    parsed = []
    for _, row in id_df.iterrows():
        parsed.append(_parse_data_cols(row, data_cols))

    import numpy as np
    arr = []
    for row_bytes in parsed:
        arr.append(row_bytes[:len(data_cols)])
    arr = [r for r in arr if len(r) == len(data_cols)]
    if not arr:
        print("No valid data rows.")
        return

    # Per-byte statistics
    print("\n--- Byte Analysis (decimal) ---")
    print("{:<6} {:>6} {:>6} {:>7} {:>7} {:>8}  {}".format(
        "Byte", "Min", "Max", "Avg", "Unique", "Changes", "Notes"))
    print("-" * 65)

    byte_series = {}
    for i, col in enumerate(data_cols):
        vals = [r[i] for r in arr if r[i] != 0xFF]
        if not vals:
            print(f"{col:<6} {'(all FF)':>6}")
            continue
        mn, mx, avg = min(vals), max(vals), sum(vals) / len(vals)
        unique = len(set(vals))
        changes = sum(1 for a, b in zip(vals, vals[1:]) if a != b)
        byte_series[col] = vals

        notes = []
        if unique == 1:
            notes.append("STATIC")
        elif unique > 1:
            notes.append("CHANGING")
            # Heuristic hints for reverse engineering
            if mx <= 250 and mn >= 0:
                span = mx - mn
                if 40 <= mx <= 120 and mn >= 0:
                    notes.append("temp-like? (raw-40 = °C)")
                if 0 <= mn and mx <= 250 and span > 10:
                    # Could be 0-100% at 0.4/bit
                    notes.append(f"0.4%/bit → {mn * 0.4:.0f}–{mx * 0.4:.0f}%")
            if unique == 2:
                notes.append("binary flag?")

        flag = " <<<" if unique > 2 else (" <" if unique == 2 else "")
        print(f"{col:<6} {mn:>6} {mx:>6} {avg:>7.1f} {unique:>7} {changes:>8}{flag}  {', '.join(notes)}")

    # Show decoded values (known decoder)
    print("\n--- J1939 Decoded Values (sample) ---")
    print("First 5:")
    for row_bytes in parsed[:5]:
        try:
            sigs = decoder(row_bytes)
            sig_str = "  ".join(f"{k}={v}" for k, v in sigs.items()) if sigs else "(no decoder)"
        except Exception as e:
            sig_str = f"decode error: {e}"
        print(f"  {sig_str}")

    print("\nLast 5:")
    for row_bytes in parsed[-5:]:
        try:
            sigs = decoder(row_bytes)
            sig_str = "  ".join(f"{k}={v}" for k, v in sigs.items()) if sigs else "(no decoder)"
        except Exception as e:
            sig_str = f"decode error: {e}"
        print(f"  {sig_str}")

    # Raw hex samples
    print("\n--- Raw Hex Samples ---")
    print("First 5:")
    for _, row in id_df.head(5).iterrows():
        data = " ".join(str(row[c]) for c in data_cols)
        print(f"  [{row['timestamp']:>10}]  {data}")

    print("\nLast 5:")
    for _, row in id_df.tail(5).iterrows():
        data = " ".join(str(row[c]) for c in data_cols)
        print(f"  [{row['timestamp']:>10}]  {data}")

    # 16-bit word scan (little-endian) — helpful for finding multi-byte signals
    print("\n--- 16-bit LE Word Scan (to help spot scaled signals) ---")
    print("{:<10} {:>8} {:>8} {:>8}  {}".format(
        "Bytes", "Min", "Max", "Unique", "Hints"))
    print("-" * 60)
    n_bytes = len(data_cols)
    for i in range(n_bytes - 1):
        col_a, col_b = data_cols[i], data_cols[i + 1]
        if col_a not in byte_series or col_b not in byte_series:
            continue
        a_vals = [r[i] for r in arr]
        b_vals = [r[i + 1] for r in arr]
        words  = [(b << 8) | a for a, b in zip(a_vals, b_vals)
                  if a != 0xFF and b != 0xFF]
        if not words:
            continue
        mn, mx = min(words), max(words)
        unique = len(set(words))
        hints  = []
        if unique > 1:
            # RPM: 0.125 RPM/bit typical range 0–8000 RPM → 0–64000 raw
            if mx < 70000:
                hints.append(f"RPM@0.125 → {mn * 0.125:.0f}–{mx * 0.125:.0f}")
            # Voltage: 0.05 V/bit typical 200–350 → 10–17.5 V
            if 100 < mx < 500:
                hints.append(f"Volts@0.05 → {mn * 0.05:.2f}–{mx * 0.05:.2f} V")
            # Speed: 1/256 km/h per bit
            if mx < 30000:
                hints.append(f"Speed@1/256 → {mn / 256:.1f}–{mx / 256:.1f} km/h")
            # Lambda/AFR: J1939 SPN 5765 lambda = raw * 0.0000305, range 0.7–1.4
            #   → raw 22950–45900; AFR = lambda * 14.7
            if 20000 < mx < 50000:
                hints.append(f"Lambda@3.05e-5 → {mn * 0.0000305:.3f}–{mx * 0.0000305:.3f} λ"
                              f"  ({mn * 0.0000305 * 14.7:.1f}–{mx * 0.0000305 * 14.7:.1f} AFR)")
        label = f"d{i}-d{i+1}"
        print(f"{label:<10} {mn:>8} {mx:>8} {unique:>8}  {'; '.join(hints) or '—'}")


# ── Scan: hunt for unknown signals across all IDs ─────────────────────────────

def scan_changing_bytes(df, unknown_only=False):
    """
    Show every byte position that changes, across all CAN IDs.
    Useful for hunting unknown signals like AFR.
    """
    print("\n" + "=" * 70)
    print("Byte-Change Scan  (all changing bytes across all IDs)")
    if unknown_only:
        print("  -- showing UNKNOWN PGNs only --")
    print("=" * 70)

    data_cols    = [c for c in df.columns if _DATA_COL_RE.match(c)]
    duration_ms  = df["timestamp"].max() - df["timestamp"].min()
    duration_s   = duration_ms / 1000

    print(f"\n{'CAN ID':<14} {'PGN/Label':<30} {'Byte':<6} "
          f"{'Min':>5} {'Max':>5} {'Avg':>6} {'Uniq':>5}  Notes")
    print("-" * 90)

    for can_id in sorted(df["id"].unique()):
        j = _try_j1939(can_id)
        if j:
            pgn, sa, _ = j
            is_unknown = pgn not in _KNOWN_PGNS
            if unknown_only and not is_unknown:
                continue
            label, decoder, confirmed = pgn_label(pgn)
            pgn_str = f"PGN 0x{pgn:04X} SA={sa:02X}"
        else:
            if unknown_only:
                continue
            pgn_str = "(11-bit)"
            confirmed = False

        id_df = df[df["id"] == can_id]
        rows  = [_parse_data_cols(r, data_cols) for _, r in id_df.iterrows()]

        printed_header = False
        for i, col in enumerate(data_cols):
            vals = [r[i] for r in rows if r[i] != 0xFF]
            if len(vals) < 2:
                continue
            unique = len(set(vals))
            if unique < 2:
                continue  # static byte — skip

            mn, mx, avg = min(vals), max(vals), sum(vals) / len(vals)
            notes = []
            if 40 <= mx <= 125 and mn >= 0:
                notes.append(f"temp? raw-40={mn-40}–{mx-40}°C")
            if 0 <= mn and mx <= 250:
                notes.append(f"0.4%/bit={mn*0.4:.0f}–{mx*0.4:.0f}%")
            # AFR byte hint: stoich ~14.7:1; if 1 byte scaled 0.1 AFR/bit, stoich=147
            if 100 < mn and mx < 200:
                notes.append(f"AFR? @0.1/bit={mn*0.1:.1f}–{mx*0.1:.1f}")

            id_col = can_id if not printed_header else ""
            pgn_col = pgn_str if not printed_header else ""
            printed_header = True

            print(f"{id_col:<14} {pgn_col:<30} {col:<6} "
                  f"{mn:>5} {mx:>5} {avg:>6.1f} {unique:>5}  {'; '.join(notes)}")

        if printed_header:
            print()  # blank line between IDs


# ── Compare two logs ──────────────────────────────────────────────────────────

def compare_logs(file1, file2):
    print("\n" + "=" * 70)
    print("Comparing Log Files")
    print("=" * 70)

    df1 = load_log(file1)
    df2 = load_log(file2)

    ids1 = set(df1["id"].unique())
    ids2 = set(df2["id"].unique())

    print(f"\nFile 1: {os.path.basename(file1)}  — {len(df1)} frames, {len(ids1)} IDs")
    print(f"File 2: {os.path.basename(file2)}  — {len(df2)} frames, {len(ids2)} IDs")

    only_in_1 = ids1 - ids2
    if only_in_1:
        print(f"\nIDs only in {os.path.basename(file1)}:")
        for can_id in sorted(only_in_1):
            print(f"  {can_id}: {len(df1[df1['id'] == can_id])} messages")

    only_in_2 = ids2 - ids1
    if only_in_2:
        print(f"\nIDs only in {os.path.basename(file2)}:")
        for can_id in sorted(only_in_2):
            print(f"  {can_id}: {len(df2[df2['id'] == can_id])} messages")

    common    = ids1 & ids2
    data_cols = [c for c in df1.columns if _DATA_COL_RE.match(c)]

    print("\n--- Data Differences in Common IDs ---")
    for can_id in sorted(common):
        data1 = df1[df1["id"] == can_id][data_cols].values
        data2 = df2[df2["id"] == can_id][data_cols].values

        patterns1 = set(tuple(row) for row in data1)
        patterns2 = set(tuple(row) for row in data2)

        only1 = patterns1 - patterns2
        only2 = patterns2 - patterns1

        if only1 or only2:
            j = _try_j1939(can_id)
            label = ""
            if j:
                pgn, sa, _ = j
                label, _, _ = pgn_label(pgn)
            print(f"\n{can_id}  {label}")
            if only1:
                print(f"  Patterns only in file 1: {len(only1)}")
                for p in list(only1)[:3]:
                    print(f"    {' '.join(p)}")
            if only2:
                print(f"  Patterns only in file 2: {len(only2)}")
                for p in list(only2)[:3]:
                    print(f"    {' '.join(p)}")


# ── Message rates ─────────────────────────────────────────────────────────────

def show_rates(df):
    print("\n" + "=" * 70)
    print("Message Rates Over Time")
    print("=" * 70)

    df = df.copy()
    df["time_bin"] = (df["timestamp"] / 1000).astype(int)
    time_bins      = sorted(df["time_bin"].unique())

    header = "{:<14}".format("ID")
    for t in time_bins[:20]:
        header += f" {t:>4}"
    print(header)
    print("-" * len(header))

    for can_id in sorted(df["id"].unique()):
        id_df = df[df["id"] == can_id]
        row   = f"{can_id:<14}"
        for t in time_bins[:20]:
            count = len(id_df[id_df["time_bin"] == t])
            row  += f" {count:>4}"
        print(row)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Analyze Polaris CAN logs with J1939 decode",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("logfile", nargs="?", help="CAN log CSV file")
    parser.add_argument("--summary",  action="store_true", help="Show ID summary (default)")
    parser.add_argument("--id",       type=str,  help="Deep-dive a specific CAN ID (hex)")
    parser.add_argument("--scan",     action="store_true",
                        help="Show all changing bytes — use to hunt unknown signals (AFR etc.)")
    parser.add_argument("--unknown",  action="store_true",
                        help="Limit summary/scan to unrecognized PGNs only")
    parser.add_argument("--compare",  nargs=2, metavar=("FILE1", "FILE2"),
                        help="Compare two log files")
    parser.add_argument("--rate",     action="store_true", help="Show message rates over time")

    args = parser.parse_args()

    if args.compare:
        compare_logs(args.compare[0], args.compare[1])
        return

    if not args.logfile:
        parser.print_help()
        return

    print(f"Loading {args.logfile}...")
    df = load_log(args.logfile)
    print(f"Loaded {len(df)} frames")

    # Default to summary when no specific mode given
    show_sum = args.summary or not (args.id or args.scan or args.rate)

    if show_sum:
        summarize_ids(df, unknown_only=args.unknown)

    if args.id:
        analyze_id(df, args.id)

    if args.scan:
        scan_changing_bytes(df, unknown_only=args.unknown)

    if args.rate:
        show_rates(df)


if __name__ == "__main__":
    main()
