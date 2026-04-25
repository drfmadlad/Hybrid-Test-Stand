#!/usr/bin/env python3
"""
Polaris CAN Bus Live Dashboard  —  J1939 Decoder
Reads CSV lines from the Teensy sniffer, logs everything to a file,
and shows a stable live table with properly decoded J1939 signals.

Usage:
    python dashboard.py <PORT> [baud]
    python dashboard.py COM3
    python dashboard.py /dev/ttyACM0 2000000

Log file written to the tools/ folder:
    can_log_YYYYMMDD_HHMMSS.csv  (compatible with analyze_can.py)

Requires:  pip install pyserial rich
"""

import sys
import time
import threading
import serial
from collections import deque
from datetime import datetime
from pathlib import Path
from rich.console import Console
from rich.table import Table
from rich.live import Live
from rich.text import Text
from rich.panel import Panel

# ── Config ────────────────────────────────────────────────────────────────────

PORT       = sys.argv[1] if len(sys.argv) > 1 else "COM3"
BAUD       = int(sys.argv[2]) if len(sys.argv) > 2 else 2_000_000
REFRESH_HZ = 4      # lower = less flicker, still feels live
RATE_WINDOW = 20    # recent timestamps for Hz calculation

# ── CSV log ───────────────────────────────────────────────────────────────────

_log_name = f"can_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
_log_path = Path(__file__).parent / _log_name
_log_file = open(_log_path, "w", buffering=1)
_log_file.write("# timestamp_ms,seq,id,ext,len,d0,d1,d2,d3,d4,d5,d6,d7\n")

# ── J1939 ID parser ───────────────────────────────────────────────────────────

def j1939_parse(can_id: int):
    """
    Break a 29-bit J1939 extended ID into (pgn, sa, priority).
    PGN is the lower 16-bit value (ignoring Data Page) so that DP=0 and DP=1
    variants of the same message match the same table entry.
    """
    pf  = (can_id >> 16) & 0xFF
    sa  = can_id & 0xFF
    pri = (can_id >> 26) & 0x7
    if pf >= 0xF0:                          # PDU2: broadcast, GE is part of PGN
        ge  = (can_id >> 8) & 0xFF
        pgn = (pf << 8) | ge
    else:                                   # PDU1: addressed, DA is not part of PGN
        pgn = pf << 8
    return pgn, sa, pri

# ── J1939 signal decoders ─────────────────────────────────────────────────────
# All return a dict of {signal_name: decoded_value_str}.
# 0xFF (byte) and 0xFFFF (word) are J1939 "not available" — returned as None.

def _u16le(d, i):
    v = (d[i + 1] << 8) | d[i]
    return None if v == 0xFFFF else v

def _u8(d, i):
    return None if d[i] == 0xFF else d[i]

def _fmt(val, scale, unit, digits=1):
    if val is None: return "N/A"
    return f"{val * scale:.{digits}f} {unit}".strip()

def decode_eec1(d):
    """PGN 0xF004 — Electronic Engine Controller 1"""
    rpm_raw = _u16le(d, 3)
    return {"RPM": _fmt(rpm_raw, 0.125, "RPM", 0)}

def decode_eec2(d):
    """PGN 0xF005 — Electronic Engine Controller 2"""
    # Standard J1939 TPS (d1) and Load (d2) are FF on this Polaris ECU.
    # d4 and d5 are non-FF but never change — static config, not live signals.
    tps  = _u8(d, 1)
    load = _u8(d, 2)
    return {
        "TPS":  _fmt(tps,  0.4, "%", 0),
        "Load": _fmt(load, 0.4, "%", 0),
    }

def decode_et1(d):
    """PGN 0xFEEE — Engine Temperature 1"""
    raw = _u8(d, 0)
    temp = None if raw is None else raw - 40
    return {"Coolant": "N/A" if temp is None else f"{temp} °C"}

def decode_ccvs(d):
    """PGN 0xFEF1 — Cruise Control / Vehicle Speed"""
    spd_raw = _u16le(d, 1)
    return {"Speed": _fmt(spd_raw, 1.0 / 256.0, "km/h", 1)}

def decode_lfe(d):
    """PGN 0xFEF2 — Fuel Economy / Liquid Fuel Economy"""
    # b0-b1: fuel rate (SPN 183), 0.05 L/h per bit, 16-bit LE
    # b6:    throttle valve 1 position (SPN 51), 0.4 %/bit
    rate_raw = _u16le(d, 0)
    throttle = _u8(d, 6)
    return {
        "FuelRate": _fmt(rate_raw, 0.05, "L/h", 2),
        "ThrottleV": _fmt(throttle, 0.4, "%", 0),
    }

def decode_prop_ff66(d):
    """PGN 0xFF66 — Polaris proprietary (mirrors EEC1 RPM at 1 RPM/bit)"""
    raw = _u16le(d, 0)
    return {"RPM(prop)": _fmt(raw, 1.0, "RPM", 0)}

def decode_eec3(d):
    """PGN 0xF006 — Electronic Engine Controller 3"""
    # Desired operating speed in d0-d1 (0.125 RPM/bit), governor mode in d2
    gov_raw = _u16le(d, 0)
    return {"GovRPM": _fmt(gov_raw, 0.125, "RPM", 0)}

def decode_ambc(d):
    """PGN 0xFEF5 — Ambient Conditions"""
    # b0: barometric pressure (SPN 108), 0.5 kPa/bit
    # b3-b4: air inlet temperature (SPN 172), 16-bit LE, 0.03125°C/bit, -273 offset
    baro = _u8(d, 0)
    iat_raw = _u16le(d, 3)
    iat = None if iat_raw is None else round(iat_raw * 0.03125 - 273.0, 1)
    return {
        "Baro":   _fmt(baro, 0.5, "kPa", 1),
        "IAT":    "N/A" if iat is None or iat < -39 else f"{iat:.1f} °C",
    }

def decode_hours(d):
    """PGN 0xFEE5 — Engine Hours / Revolutions"""
    # 4-byte LE, 0.05 hr/bit
    if d[0] == 0xFF and d[1] == 0xFF and d[2] == 0xFF and d[3] == 0xFF:
        return {"Hours": "N/A"}
    raw = d[0] | (d[1] << 8) | (d[2] << 16) | (d[3] << 24)
    hrs = raw * 0.05
    return {"Hours": f"{hrs:.1f} hr"}

def decode_vep(d):
    """PGN 0xFEF7 — Vehicle Electrical Power"""
    raw = _u16le(d, 4)   # SPN 168 (Electrical Potential) is at bytes 5-6 (0-indexed 4-5)
    return {"BattV": _fmt(raw, 0.05, "V", 2)}

def decode_fefc(d):
    """PGN 0xFEFC — Instrument cluster (SA=0x17); b1 varies slowly, likely fuel level."""
    raw = _u8(d, 1)
    # 0.4%/bit matches the J1939 percent-type SPN convention and gives ~10% in log
    return {"FuelLevel?": _fmt(raw, 0.4, "%", 0)}

def decode_fec1(d):
    """PGN 0xFEC1 — Instrument cluster (SA=0x17); static in log."""
    # b4=3 looks like gear number; other bytes unknown
    gear = _u8(d, 4)
    return {"Gear?": "N/A" if gear is None else str(gear)}

def decode_raw(d):
    """Fallback — show raw bytes, no decode."""
    return {}

# ── PGN table ─────────────────────────────────────────────────────────────────
# Maps lower-16-bit PGN → (short_name, color, decoder_fn)
# Color: rich style string used for the Signal column.

_PGN_TABLE = {
    # ── Confirmed signals ─────────────────────────────────────────────────────
    0xF004: ("EEC1   Engine Speed",   "bright_green",  decode_eec1),
    0xFEEE: ("ET1    Coolant Temp",   "bright_red",    decode_et1),
    0xFEF1: ("CCVS   Speed",          "bright_cyan",   decode_ccvs),
    0xFEF2: ("LFE    Fuel/Throttle",  "yellow",        decode_lfe),
    0xFF66: ("PROP   Polaris RPM",    "bright_green",  decode_prop_ff66),
    # ── Standard J1939 — present but mostly N/A in this log ──────────────────
    0xF005: ("EEC2   Throttle/Load",  "green",         decode_eec2),
    0xF006: ("EEC3   Gov Speed",      "green",         decode_eec3),
    0xFEF5: ("AMBC   Ambient",        "cyan",          decode_ambc),
    0xFEE5: ("EH     Engine Hours",   "white",         decode_hours),
    0xFEF7: ("VEP    Battery",        "yellow",        decode_vep),
    # ── Instrument cluster (SA=0x17) ─────────────────────────────────────────
    0xFEFC: ("INSTR  Fuel Level?",    "bright_yellow", decode_fefc),
    0xFEC1: ("INSTR  Gear?",          "bright_yellow", decode_fec1),
    # ── Transport / network management — hidden ───────────────────────────────
    0xEC00: ("TP.CM  [transport]",    "grey50",        decode_raw),
    0xEB00: ("TP.DT  [transport]",    "grey50",        decode_raw),
    0xEE00: ("AddrClaim [net]",       "grey50",        decode_raw),
    0xEA00: ("PGNReq  [net]",         "grey50",        decode_raw),
    0xE800: ("Ack    [net]",          "grey50",        decode_raw),
}

_HIDE_PGNS = {0xEC00, 0xEB00, 0xEE00, 0xEA00, 0xE800}  # hide transport noise

def pgn_info(pgn: int):
    entry = _PGN_TABLE.get(pgn)
    if entry:
        return entry
    return (f"PGN 0x{pgn:04X}", "grey50", decode_raw)

# ── Frame registry ────────────────────────────────────────────────────────────

class FrameEntry:
    __slots__ = ("can_id", "pgn", "sa", "data", "count", "ts", "_twin", "rate")

    def __init__(self, can_id, pgn, sa, data, count, ts):
        self.can_id = can_id
        self.pgn    = pgn
        self.sa     = sa
        self.data   = data
        self.count  = count
        self.ts     = ts
        self._twin  = deque(maxlen=RATE_WINDOW)
        self._twin.append(ts)
        self.rate   = 0.0

    def update(self, data, count, ts):
        self.data  = data
        self.count = count
        self.ts    = ts
        self._twin.append(ts)
        if len(self._twin) >= 2:
            span = self._twin[-1] - self._twin[0]
            if span > 0:
                self.rate = (len(self._twin) - 1) / span * 1000.0

registry      : dict[int, FrameEntry] = {}
registry_lock = threading.Lock()
status_line   = "Connecting…"
total_frames  = 0
start_wall    = time.time()

# ── Serial reader ─────────────────────────────────────────────────────────────

def _reader():
    global status_line, total_frames
    while True:
        try:
            with serial.Serial(PORT, BAUD, timeout=1) as ser:
                status_line = (
                    f"[green]Connected[/green]  {PORT} @ {BAUD // 1000} kbaud"
                    f"  →  [bold]{_log_name}[/bold]"
                )
                ser.write(b"d\n")   # enable raw CAN dump so we get $C, frames
                while True:
                    try:
                        line = ser.readline().decode("ascii", errors="replace").strip()
                    except Exception:
                        continue

                    if not line:
                        continue

                    _log_file.write(line + "\n")

                    if line.startswith("#") or line.startswith("$T"):
                        continue

                    if line.startswith("$C,"):
                        line = line[3:]  # strip "$C," prefix added by current firmware

                    parts = line.split(",")
                    if len(parts) < 13:
                        continue
                    try:
                        ts    = int(parts[0])
                        seq   = int(parts[1])
                        can_id = int(parts[2], 16)
                        # parts[3]=ext, parts[4]=len
                        data  = []
                        for x in parts[5:13]:
                            x = x.strip()
                            data.append(int(x, 16) if x not in ("--", "") else 0xFF)
                    except (ValueError, IndexError):
                        continue

                    total_frames = seq
                    pgn, sa, _ = j1939_parse(can_id)

                    # Skip transport noise from the live display
                    if pgn in _HIDE_PGNS:
                        continue

                    with registry_lock:
                        if can_id in registry:
                            registry[can_id].update(data, seq, ts)
                        else:
                            registry[can_id] = FrameEntry(can_id, pgn, sa, data, seq, ts)

        except serial.SerialException as exc:
            status_line = f"[red]Disconnected[/red]  {exc}"
            time.sleep(2)

# ── Table builder ─────────────────────────────────────────────────────────────

def _build_table() -> Table:
    tbl = Table(
        show_header=True,
        header_style="bold white on grey19",
        border_style="grey42",
        row_styles=["", "on grey7"],
        expand=True,
        pad_edge=True,
    )
    tbl.add_column("CAN ID",        style="bold white",  width=12, no_wrap=True)
    tbl.add_column("SA",            style="grey70",      width=4,  no_wrap=True)
    tbl.add_column("Signal",                             width=22, no_wrap=True)
    tbl.add_column("Hz",            justify="right",     width=6,  no_wrap=True)
    tbl.add_column("Decoded Values",                     min_width=28)
    tbl.add_column("Raw Bytes",                          width=30, no_wrap=True)

    with registry_lock:
        # Sort by CAN ID — stable order, no jumping rows
        entries = sorted(registry.values(), key=lambda e: e.can_id)

    for e in entries:
        name, color, decoder = pgn_info(e.pgn)
        signals = decoder(e.data)

        all_na = all("N/A" in v for v in signals.values()) if signals else True

        # Skip rows that decode to nothing useful — reduces noise
        if all_na and signals:
            continue

        decoded_parts = [f"{k}={v}" for k, v in signals.items()]
        decoded_str   = "  ".join(decoded_parts) if decoded_parts else "—"
        decoded_style = "grey50" if all_na else "white"

        byte_str = " ".join(f"{b:02X}" for b in e.data)
        hz_str   = f"{e.rate:.1f}" if e.rate >= 0.5 else " —"

        tbl.add_row(
            f"0x{e.can_id:08X}",
            f"{e.sa:02X}",
            Text(name, style=color),
            hz_str,
            Text(decoded_str, style=decoded_style),
            Text(byte_str, style="grey62"),
        )
    return tbl

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    console = Console()
    console.print(f"[dim]Logging all frames to:[/dim] [bold]{_log_path}[/bold]\n")

    threading.Thread(target=_reader, daemon=True).start()

    try:
        with Live(console=console, refresh_per_second=REFRESH_HZ) as live:
            while True:
                elapsed = int(time.time() - start_wall)
                with registry_lock:
                    n_ids = len(registry)

                panel = Panel(
                    _build_table(),
                    title=(
                        "[bold]Polaris CAN Bus  ·  J1939 Live Dashboard[/bold]"
                        f"          {status_line}"
                    ),
                    subtitle=(
                        f"[dim]{n_ids} IDs  ·  {total_frames} frames  ·  "
                        f"{elapsed}s  ·  sorted by CAN ID  ·  Ctrl+C to quit[/dim]"
                    ),
                    border_style="bright_blue",
                )
                live.update(panel)
                time.sleep(1.0 / REFRESH_HZ)
    finally:
        _log_file.close()
        console.print(f"\n[green]Log saved:[/green] {_log_path}")


if __name__ == "__main__":
    main()
