"""
calibrate_lathe.py — Interactive & CLI Lathe Geometry Calibration Wizard (Phase 3: T3.2)
The Crafters Hub | LatheGuard Physical AI

Allows any workshop to retrofit LatheGuard onto their woodturning lathe in 5 steps:
  1. Spindle Axis (2 clicks along the headstock-to-tailstock centerline)
  2. Headstock & Tailstock Zone Boundaries (2 clicks marking chuck end and live-center end)
  3. Operator Position (`bottom` y > axis_y or `top` y < axis_y)
  4. Physical Scale Calibration (2 clicks across a known ruler length, e.g. 100 mm on a steel square)
  5. Saves `<output>.json` to `profiles/` and writes a visual Red-Zone calibration preview image.
"""

import argparse
import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import cv2
import numpy as np

from lathe_profile import DEFAULT_LATHE_PROFILE, PROFILES_DIR
from lathe_shm import ROI_H, ROI_W
from lathe_vision_capture import compute_dynamic_red_zone


def compute_calibration_profile(
    name: str,
    axis_pts: Tuple[Tuple[int, int], Tuple[int, int]],
    zone_pts: Tuple[Tuple[int, int], Tuple[int, int]],
    ruler_pts: Optional[Tuple[Tuple[int, int], Tuple[int, int]]] = None,
    ruler_mm: float = 100.0,
    operator_side: str = "bottom",
    roi_offset: Tuple[int, int] = (510, 240),
) -> Dict[str, Any]:
    """Computes a standardized LatheGuard profile dictionary from calibration points in 900x600 ROI space."""
    prof = json.loads(json.dumps(DEFAULT_LATHE_PROFILE))
    prof["name"] = name
    prof["roi"] = {"x0": int(roi_offset[0]), "y0": int(roi_offset[1])}

    axis_y = int(round((axis_pts[0][1] + axis_pts[1][1]) / 2.0))
    axis_y = int(np.clip(axis_y, 120, ROI_H - 120))
    prof["axis_y"] = axis_y

    hx = min(int(zone_pts[0][0]), int(zone_pts[1][0]))
    tx = max(int(zone_pts[0][0]), int(zone_pts[1][0]))
    span = max(120, tx - hx)
    prof["zones"] = {
        "headstock_x_max": int(hx + 0.25 * span),
        "tailstock_x_min": int(hx + 0.75 * span),
    }
    prof["x_stations"] = [
        int(hx + 0.05 * span),
        int(hx + 0.35 * span),
        int(hx + 0.65 * span),
        int(hx + 0.95 * span),
    ]
    prof["operator_side"] = "bottom" if str(operator_side).lower() != "top" else "top"

    if ruler_pts is not None and ruler_mm > 0:
        dx = float(ruler_pts[1][0] - ruler_pts[0][0])
        dy = float(ruler_pts[1][1] - ruler_pts[0][1])
        dist_px = math.hypot(dx, dy)
        prof["px_per_mm"] = round(dist_px / float(ruler_mm), 3) if dist_px > 1.0 else None
    else:
        prof["px_per_mm"] = None

    return prof


def render_calibration_preview(roi_bgr: np.ndarray, prof: Dict[str, Any]) -> np.ndarray:
    """Draws the calibrated spindle axis, headstock/tailstock zones, and dynamic Red-Zone polygon."""
    vis = roi_bgr.copy()
    axis_y = int(prof.get("axis_y", 300))
    poly = compute_dynamic_red_zone(
        vis,
        safety_margin_px=int(prof.get("margin_px", 25)),
        radius_clamp=tuple(prof.get("radius_clamp", [60, 160])),
    )
    overlay = vis.copy()
    cv2.fillPoly(overlay, [poly], (0, 0, 180))
    cv2.addWeighted(overlay, 0.28, vis, 0.72, 0, vis)
    cv2.polylines(vis, [poly], True, (0, 215, 255), 2)
    cv2.line(vis, (0, axis_y), (ROI_W, axis_y), (0, 255, 128), 1, cv2.LINE_AA)

    hx = int(prof["zones"]["headstock_x_max"])
    tx = int(prof["zones"]["tailstock_x_min"])
    cv2.line(vis, (hx, 0), (hx, ROI_H), (255, 180, 0), 1)
    cv2.line(vis, (tx, 0), (tx, ROI_H), (255, 180, 0), 1)
    scale_txt = f"{prof['px_per_mm']} px/mm" if prof.get("px_per_mm") else "uncalibrated scale"
    cv2.putText(
        vis,
        f"Profile: {prof['name']} | Axis Y={axis_y} | Op={prof['operator_side']} | {scale_txt}",
        (16, 28),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.58,
        (240, 245, 255),
        2,
        cv2.LINE_AA,
    )
    return vis


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="LatheGuard 5-Step Lathe Calibration Wizard")
    parser.add_argument("--image", type=str, default="", help="Path to workshop lathe photo (1920x1080 or resized)")
    parser.add_argument("--name", type=str, default="custom_lathe", help="Profile name to save")
    parser.add_argument("--operator-side", type=str, default="bottom", choices=["bottom", "top"])
    parser.add_argument("--ruler-mm", type=float, default=100.0, help="Known ruler segment length in mm")
    parser.add_argument("--non-interactive", action="store_true", help="Run automated calibration without GUI clicks")
    parser.add_argument("--preview-out", type=str, default="", help="Optional path to save preview image")
    args = parser.parse_args(argv)

    if args.image and Path(args.image).is_file():
        img = cv2.imread(args.image)
        full = cv2.resize(img, (1920, 1080))
        roi = full[240 : 240 + ROI_H, 510 : 510 + ROI_W].copy()
    else:
        roi = np.full((ROI_H, ROI_W, 3), 28, dtype=np.uint8)
        cv2.rectangle(roi, (100, 210), (760, 390), (70, 142, 212), -1)

    clicks: List[Tuple[int, int]] = []
    if not args.non_interactive:
        win = "LatheGuard Calibration (Click: 2x Axis, 2x Head/Tail, 2x Ruler 100mm, then press Q)"
        disp = roi.copy()

        def _on_mouse(event, x, y, _flags, _param):
            if event == cv2.EVENT_LBUTTONDOWN and len(clicks) < 6:
                clicks.append((int(x), int(y)))
                cv2.circle(disp, (x, y), 5, (0, 255, 255), -1)
                cv2.imshow(win, disp)

        cv2.namedWindow(win, cv2.WINDOW_NORMAL)
        cv2.setMouseCallback(win, _on_mouse)
        cv2.imshow(win, disp)
        while len(clicks) < 6:
            key = cv2.waitKey(30) & 0xFF
            if key in (ord("q"), 27):
                break
        cv2.destroyAllWindows()

    if len(clicks) >= 6:
        axis_pts = (clicks[0], clicks[1])
        zone_pts = (clicks[2], clicks[3])
        ruler_pts = (clicks[4], clicks[5])
    else:
        axis_pts = ((120, 300), (720, 300))
        zone_pts = ((120, 300), (720, 300))
        ruler_pts = ((300, 420), (420, 420))

    prof = compute_calibration_profile(
        name=args.name,
        axis_pts=axis_pts,
        zone_pts=zone_pts,
        ruler_pts=ruler_pts,
        ruler_mm=args.ruler_mm,
        operator_side=args.operator_side,
    )
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    out_json = PROFILES_DIR / f"{args.name}.json"
    out_json.write_text(json.dumps(prof, indent=2), encoding="utf-8")
    print(f"[CALIBRATE] Saved profile -> {out_json}")

    preview = render_calibration_preview(roi, prof)
    if args.preview_out:
        Path(args.preview_out).parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(args.preview_out, preview)
        print(f"[CALIBRATE] Saved preview -> {args.preview_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
