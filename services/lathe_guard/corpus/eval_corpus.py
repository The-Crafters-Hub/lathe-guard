"""
eval_corpus.py — Corpus Benchmark & Validation Gate Harness (Phase 3: T3.4)
The Crafters Hub | LatheGuard Physical AI

Evaluates `RedZoneVisionDetector` across the synthetic + real Crafters Hub workshop corpus,
enforces the 5 P0 safety/latency gates, and generates `docs/lathe_guard_validation.md`.
"""

import argparse
import contextlib
import io
import os
import sys
import time
from datetime import datetime, UTC
from pathlib import Path
from typing import Any, Dict, List, Optional
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from lathe_safety_engine import RedZoneVisionDetector
from lathe_shm import FaultCode
from synthetic_corpus import ClipScenario, build_corpus_scenarios, iter_scenario_frames


def evaluate_corpus(quick: bool = False) -> Dict[str, Any]:
    scenarios = build_corpus_scenarios(quick=quick)
    results: List[Dict[str, Any]] = []
    frame_latencies_ms: List[float] = []

    for sc in scenarios:
        det = RedZoneVisionDetector(warmup_frames=16, confirm_frames=2)
        tripped = False
        trip_frame: Optional[int] = None
        moat_frame: Optional[int] = None
        trip_penetration_px: float = 0.0
        fault_name = "NONE"

        for f_idx, roi, poly, gt_moat_f, pen_px in iter_scenario_frames(sc):
            if gt_moat_f is not None and moat_frame is None:
                moat_frame = gt_moat_f
            with contextlib.redirect_stdout(io.StringIO()):
                t0 = time.perf_counter_ns()
                pts = det.detect(roi, poly)
                dt_ms = (time.perf_counter_ns() - t0) / 1e6
            if det.is_armed:
                frame_latencies_ms.append(dt_ms)

            if pts and not tripped:
                tripped = True
                trip_frame = f_idx
                trip_penetration_px = pen_px
                try:
                    fault_name = FaultCode(det.last_fault_code).name
                except ValueError:
                    fault_name = str(det.last_fault_code)
                if sc.is_positive:
                    break

        frames_after_moat: Optional[int] = None
        if tripped and trip_frame is not None:
            frames_after_moat = max(1, int(trip_frame - moat_frame + 1)) if moat_frame is not None else 1

        passed = (not tripped) if (not sc.is_positive) else tripped
        results.append(
            {
                "clip_id": sc.clip_id,
                "is_positive": sc.is_positive,
                "category": sc.category,
                "bg_source": sc.bg_source,
                "armed": det.is_armed,
                "tripped": tripped,
                "passed": passed,
                "trip_frame": trip_frame,
                "moat_frame": moat_frame,
                "frames_after_moat": frames_after_moat,
                "penetration_px": round(trip_penetration_px, 1),
                "fault_name": fault_name,
                "speed_px_per_f": sc.speed_px_per_f,
            }
        )

    neg_res = [r for r in results if not r["is_positive"]]
    pos_res = [r for r in results if r["is_positive"]]

    false_trips = sum(1 for r in neg_res if r["tripped"])
    unarmed_negs = sum(1 for r in neg_res if not r["armed"])
    pos_hits = sum(1 for r in pos_res if r["tripped"])
    recall_pct = 100.0 * pos_hits / max(1, len(pos_res))

    moat_delays = [r["frames_after_moat"] for r in pos_res if r["frames_after_moat"] is not None]
    p95_frames_after_moat = float(np.percentile(moat_delays, 95)) if moat_delays else 99.0

    fast_30_pens = [r["penetration_px"] for r in pos_res if r["speed_px_per_f"] >= 30.0 and r["tripped"]]
    max_pen_30px = max(fast_30_pens) if fast_30_pens else 0.0

    p50_lat_ms = float(np.percentile(frame_latencies_ms, 50)) if frame_latencies_ms else 0.0
    p95_lat_ms = float(np.percentile(frame_latencies_ms, 95)) if frame_latencies_ms else 0.0
    p99_lat_ms = float(np.percentile(frame_latencies_ms, 99)) if frame_latencies_ms else 0.0

    gates = {
        "zero_false_trips": (false_trips == 0 and unarmed_negs == 0),
        "hundred_pct_recall": (pos_hits == len(pos_res)),
        "p95_trip_le_3_frames": (p95_frames_after_moat <= 3.0),
        "penetration_30pxf_le_40px": (max_pen_30px <= 40.0),
        "vision_p95_lt_3_5ms": (p95_lat_ms < float(os.environ.get("LATHEGUARD_VISION_P95_MS", "3.5"))),
    }
    all_passed = all(gates.values())

    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "total_clips": len(results),
        "neg_clips": len(neg_res),
        "pos_clips": len(pos_res),
        "false_trips": false_trips,
        "unarmed_negs": unarmed_negs,
        "pos_hits": pos_hits,
        "recall_pct": round(recall_pct, 2),
        "p95_frames_after_moat": round(p95_frames_after_moat, 2),
        "max_pen_30px": round(max_pen_30px, 1),
        "p50_lat_ms": round(p50_lat_ms, 3),
        "p95_lat_ms": round(p95_lat_ms, 3),
        "p99_lat_ms": round(p99_lat_ms, 3),
        "gates": gates,
        "all_passed": all_passed,
        "results": results,
    }


def write_validation_markdown(summary: Dict[str, Any], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    g = summary["gates"]
    lines = [
        "# LatheGuard Physical AI — Validation & Corpus Benchmark Report",
        "",
        f"**Generated (UTC):** `{summary['timestamp']}`  ",
        f"**Total Evaluated Clips:** `{summary['total_clips']}` (`{summary['neg_clips']}` Negative Hazard-Free Clips + `{summary['pos_clips']}` Positive Intrusion Clips)  ",
        "**Real Workshop Hardware Backgrounds Included:**",
        "- `fox_f46_717_grey` — FOX F46-717 Heavy Floor-Standing Lathe (`300 mm` swing, `1000 mm` bed, `M33×3.5`, `MT2`)",
        "- `mc1018_yellow` — Master Craft MC1018 Midi Lathe (`250 mm` swing, `450 mm` bed, `1\"×8 TPI`, `MT2`)",
        "- `wl300_blue` — Wood Lathe WL300 Mini Precision Lathe (`200 mm` swing, `305 mm` bed, `3/4\"×16 TPI`, `MT1`)",
        "",
        "---",
        "",
        "## 1. P0 Safety & Determinism Gate Summary",
        "",
        "| Gate ID | Safety / Real-Time Criterion | Target Threshold | Measured Value | Status |",
        "| :--- | :--- | :--- | :--- | :--- |",
        f"| **G-1** | False Trips on Negative Clips (Shadows, Square Blank, Sawdust, Flicker, Real Photos) | `0 / {summary['neg_clips']}` | **`{summary['false_trips']} / {summary['neg_clips']}`** | {'PASS' if g['zero_false_trips'] else 'FAIL'} |",
        f"| **G-2** | Intrusion Recall on Positive Clips (Hands, Gloves, 6mm Gouge, Collar, Real Photos) | `100.0%` (`{summary['pos_clips']}/{summary['pos_clips']}`) | **`{summary['recall_pct']:.1f}%` (`{summary['pos_hits']}/{summary['pos_clips']}`)** | {'PASS' if g['hundred_pct_recall'] else 'FAIL'} |",
        f"| **G-3** | P95 Detection Delay After Moat Contact (2-Frame Spatial Debounce) | `<= 3.0 frames` | **`{summary['p95_frames_after_moat']:.1f} frames`** | {'PASS' if g['p95_trip_le_3_frames'] else 'FAIL'} |",
        f"| **G-4** | Max Penetration Depth at Fast Hand Speed (`30 px/frame` = `1,800 px/s`) | `<= 40.0 px` | **`{summary['max_pen_30px']:.1f} px`** | {'PASS' if g['penetration_30pxf_le_40px'] else 'FAIL'} |",
        f"| **G-5** | CPU Vision Detector Latency (`225x150` Zero-Copy Strided Pass) | `P95 < 3.50 ms` | **`P50 = {summary['p50_lat_ms']:.2f} ms`, `P95 = {summary['p95_lat_ms']:.2f} ms`, `P99 = {summary['p99_lat_ms']:.2f} ms`** | {'PASS' if g['vision_p95_lt_3_5ms'] else 'FAIL'} |",
        "",
        "---",
        "",
        "## 2. Representative Clip Breakdown",
        "",
        "| Clip ID | Type | Background | Armed | Tripped | Delay (Frames) | Penetration (px) | Fault Code | Result |",
        "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
    ]

    for r in summary["results"]:
        if r["category"] != "clean_spin_variation" and not r["clip_id"].startswith("pos_matrix_"):
            tp_str = "POSITIVE" if r["is_positive"] else "NEGATIVE"
            d_str = str(r["frames_after_moat"]) if r["frames_after_moat"] is not None else "-"
            lines.append(
                f"| `{r['clip_id']}` | {tp_str} | `{r['bg_source']}` | {r['armed']} | {r['tripped']} | {d_str} | {r['penetration_px']} | `{r['fault_name']}` | **{'PASS' if r['passed'] else 'FAIL'}** |"
            )

    lines.extend(
        [
            "",
            "---",
            "",
            "## 3. Reproduce This Benchmark",
            "",
            "```powershell",
            "python services/lathe_guard/corpus/eval_corpus.py",
            "```",
            "",
        ]
    )
    out_path.write_text("\n".join(lines), encoding="utf-8")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="LatheGuard Corpus Evaluation Harness")
    parser.add_argument("--quick", action="store_true", help="Run 20-clip quick gate instead of 80-clip full benchmark")
    parser.add_argument(
        "--out-md",
        type=str,
        default=str(Path(__file__).resolve().parent.parent.parent.parent / "docs" / "lathe_guard_validation.md"),
        help="Path to write markdown validation report",
    )
    args = parser.parse_args(argv)

    summary = evaluate_corpus(quick=args.quick)
    write_validation_markdown(summary, Path(args.out_md))

    print(
        f"[EVAL CORPUS] Clips={summary['total_clips']} | "
        f"FalseTrips={summary['false_trips']}/{summary['neg_clips']} | "
        f"Recall={summary['pos_hits']}/{summary['pos_clips']} ({summary['recall_pct']}%) | "
        f"P95_Delay={summary['p95_frames_after_moat']}f | "
        f"MaxPen30px={summary['max_pen_30px']}px | "
        f"Vision_P95={summary['p95_lat_ms']}ms | "
        f"ALL_GATES={'PASS' if summary['all_passed'] else 'FAIL'}"
    )
    return 0 if summary["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
