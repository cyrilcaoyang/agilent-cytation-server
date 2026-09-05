#!/usr/bin/env python3
"""Reassemble the D2XX trace into readable frames.

`[instrument].ftdi_trace = true` logs one line per read byte and one per
write call, which is the right granularity for the shim and the wrong one
for reading a protocol exchange. Note the asymmetry: a command letter and
its parameter string are two separate writes, so a naive byte-only parse
silently drops every parameter.
This collapses runs of same-direction bytes into a single frame, stamped with
the time of its first byte and the gap since the previous frame.

    python3 scripts/trace_frames.py [--since 15:09:00] [--gap 0.25] [LOGFILE]

The gap threshold splits a direction run into separate frames when the
instrument pauses, which is what distinguishes a solicited reply from bytes
the instrument volunteered on its own.
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys

LINE = re.compile(
    r"^(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3}) INFO\s+"
    r"agilent_cytation_server\.ftd2xx_shim: D2XX (?P<dir>tx|rx) (?P<hex>(?:[0-9a-f]{2})+)\b"
)
DEFAULT_LOG = "/mnt/c/SDL_Logs/cytation.err.log"


def printable(b: bytes) -> str:
    out = []
    for x in b:
        if 32 <= x < 127:
            out.append(chr(x))
        elif x == 0x06:
            out.append("<ACK>")
        elif x == 0x03:
            out.append("<ETX>")
        elif x == 0x02:
            out.append("<STX>")
        elif x == 0x15:
            out.append("<NAK>")
        else:
            out.append(f"<{x:02x}>")
    return "".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("log", nargs="?", default=DEFAULT_LOG)
    ap.add_argument("--since", help="HH:MM:SS — ignore frames before this")
    ap.add_argument(
        "--gap",
        type=float,
        default=0.25,
        help="Seconds of silence that ends a frame (default 0.25).",
    )
    ap.add_argument("--grep", help="Only print frames whose text contains this")
    args = ap.parse_args()

    frames: list[tuple[dt.datetime, str, bytearray]] = []
    with open(args.log, errors="replace") as fh:
        for line in fh:
            m = LINE.match(line)
            if not m:
                continue
            ts = dt.datetime.strptime(m["ts"], "%Y-%m-%d %H:%M:%S,%f")
            chunk = bytes.fromhex(m["hex"])
            if (
                frames
                and frames[-1][1] == m["dir"]
                and (ts - frames[-1][0]).total_seconds() < args.gap
            ):
                frames[-1][2].extend(chunk)
                frames[-1] = (frames[-1][0], frames[-1][1], frames[-1][2])
            else:
                frames.append((ts, m["dir"], bytearray(chunk)))

    prev: dt.datetime | None = None
    for ts, direction, buf in frames:
        if args.since and ts.strftime("%H:%M:%S") < args.since:
            prev = ts
            continue
        text = printable(bytes(buf))
        if args.grep and args.grep not in text:
            prev = ts
            continue
        gap = f"+{(ts - prev).total_seconds():6.2f}" if prev else "      "
        print(f"{ts.strftime('%H:%M:%S.%f')[:-3]} {gap} {direction.upper()} {len(buf):3d}  {text}")
        prev = ts
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
