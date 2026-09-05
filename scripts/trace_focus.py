#!/usr/bin/env python3
"""Drive a fixed-focus sweep so the FTDI trace shows what `set_focus` emits.

Answers the question left open by `captures/20260903T1827_medium_plate_focus_sweep/
FINDING.md`: the focus command is *issued* with the requested value, but the
image never changes. This script does not look at images at all — it exists so
that `[instrument].ftdi_trace = true` records the bytes for each focal height,
including the instrument's reply, which PyLabRobot's `set_focus` discards.

Two ranges are swept deliberately:

* 4.50-9.00 mm, where PyLabRobot's `F{mode}0{n:05d}` encoding is well-formed;
* 9.40-13.50 mm, where ``n`` needs six digits and the command goes out one
  byte long (see the `focus_overflow` note in the module docstring below).

Usage (from anywhere with HTTP reach to the service):

    python3 scripts/trace_focus.py --plate <plate_id> --well H12

Nothing is captured unless a plate is loaded and the carrier is in; the script
refuses rather than loading a plate for you, because `plate.load` without a
`wells` array blanks the sample map (see HANDOFF).
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
import uuid

BASE = "http://sdl2-pc-03-cytation:8040"

# `int(f + 1.0243 + 10.638 * f * 1000)` crosses 100000 here, so every focal
# height at or above it overflows PyLabRobot's five-digit field.
OVERFLOW_MM = 9.3993

WELL_FORMED = [4.5, 5.5, 6.5, 7.5, 8.5, 9.0]
OVERFLOWING = [9.4, 10.5, 12.0, 13.5]


def call(method: str, path: str, body: dict | None = None, token: str | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-Claim-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, raw.decode(errors="replace")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--well", default="H12")
    ap.add_argument("--channel", default="brightfield")
    ap.add_argument("--objective", default="O_4X_PL_FL_Phase")
    ap.add_argument("--exposure-ms", type=float, default=8.0)
    ap.add_argument("--owner", default="bench:focus-trace")
    ap.add_argument(
        "--only",
        choices=["well-formed", "overflowing", "both"],
        default="both",
        help="Which half of the range to sweep.",
    )
    args = ap.parse_args()

    status, st = call("GET", "/status")
    if status != 200:
        print(f"/status returned {status}", file=sys.stderr)
        return 1
    details = st["details"]
    if details["drawer"] == "out":
        print(
            "Carrier reads 'out'. If a plate is physically in, POST "
            "/control/drawer/close first to resync — the service dead-reckons "
            "this and cannot see the front-panel button.",
            file=sys.stderr,
        )
        return 2
    if not details["plate_in_reader"]:
        print("No plate assigned to the reader; load one first.", file=sys.stderr)
        return 2

    code, claim = call(
        "POST",
        "/control/claim",
        {"owner": args.owner, "session_id": str(uuid.uuid4()), "ttl_s": 300.0},
    )
    if code != 200:
        print(f"claim refused ({code}): {claim}", file=sys.stderr)
        return 3
    token = claim["claim_token"]

    heights: list[float] = []
    if args.only in ("well-formed", "both"):
        heights += WELL_FORMED
    if args.only in ("overflowing", "both"):
        heights += OVERFLOWING

    try:
        for f in heights:
            n = int(f + 1.0243013203461762 + 10.637991436186072 * f * 1000)
            expected = f"F50{str(n).zfill(5)}"
            code, body = call(
                "POST",
                "/control/imaging/capture",
                {
                    "well": args.well,
                    "channel": args.channel,
                    "objective": args.objective,
                    "focal_height_mm": f,
                    "exposure_ms": args.exposure_ms,
                    "autofocus": False,
                    "auto_exposure": False,
                },
                token=token,
            )
            flag = "OVERFLOW" if f >= OVERFLOW_MM else "ok"
            path = body.get("path") if isinstance(body, dict) else body
            print(f"f={f:5.2f} {flag:8s} expect={expected:<10s} http={code} {path}")
    finally:
        call("POST", "/control/release", token=token)

    print("\nNow grep the trace out of the service log, e.g.")
    print(r"  grep -n 'F50' /mnt/c/SDL_Logs/cytation.out.log | tail -60")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
