"""
lathe_hud_preview.py — Non-Blocking Seqlock Live Camera & Red-Zone HUD Viewer v2 (Phase 4: T4.2)
The Crafters Hub | LatheGuard Physical AI — Nebius × NVIDIA Hackathon
Runs strictly on MASK_PEDAGOGY_CORES so GUI rendering never perturbs Core 0 100Hz FSM.

Features (HUD v2):
  - Bilingual Arabic + English subtitle rendering via PIL + `arabic_reshaper` + `python-bidi`
    with graceful ASCII fallback if fonts/libraries are unavailable (never crashes or draws `???`).
  - 2-line word-wrapped coaching overlay + Tier badge (`TIER-0 LOCAL`, `TIER-1 NEMOTRON`, `TIER-2 REPORT`).
  - Visual Hardware Relay Indicator (`H-3`: `RELAY: 0x55 CLOSED [RUN]` vs `RELAY: 0xFF OPEN [E-STOP CUT]`).
  - Warm-up Arming Gate Banner (`CLEAR THE ZONE — HANDS OFF · arming...`) while `StatusFlag.CALIBRATING`.
  - Strictly read-only (no keyboard re-arm key; F-7 invariant).
"""

import argparse
import json
import textwrap
import threading
import time
from typing import Any, Dict, List, Optional
import cv2
import numpy as np

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    Image = None  # type: ignore
    ImageDraw = None  # type: ignore
    ImageFont = None  # type: ignore

try:
    import arabic_reshaper
    from bidi.algorithm import get_display as bidi_get_display
except ImportError:
    arabic_reshaper = None  # type: ignore
    bidi_get_display = None  # type: ignore

from lathe_scheduler import MASK_PEDAGOGY_CORES, win32_realtime_context
from lathe_shm import (
    DEFAULT_SHM_NAME,
    FaultCode,
    LatheShmReader,
    ROI_H,
    ROI_W,
    SafetyState,
    StatusFlag,
)

_latest_coaching: Dict[str, Any] = {
    "tier": 0,
    "advice": "",
    "advice_ar": "",
    "advice_en": "",
    "latency_ms": 0.0,
    "model": "LOCAL",
    "zone": "CENTER",
    "side": "OPERATOR",
    "hazard_code": "H-02",
}

_FONT_CACHE: Dict[int, Any] = {}


def _get_pil_font(size: int = 18) -> Optional[Any]:
    if ImageFont is None:
        return None
    if size in _FONT_CACHE:
        return _FONT_CACHE[size]
    for candidate in (
        r"C:\Windows\Fonts\segoeui.ttf",
        r"C:\Windows\Fonts\tahoma.ttf",
        r"C:\Windows\Fonts\arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        try:
            f = ImageFont.truetype(candidate, size)
            _FONT_CACHE[size] = f
            return f
        except Exception:
            continue
    return None


def shape_bilingual_text(text: str, allow_unicode: bool = True) -> str:
    """Shapes Arabic glyphs for right-to-left PIL rendering, or strips non-ASCII if fonts are absent."""
    if not text:
        return ""
    if not allow_unicode:
        return text.encode("ascii", errors="ignore").decode("ascii").strip()
    if arabic_reshaper is not None and bidi_get_display is not None:
        try:
            return str(bidi_get_display(arabic_reshaper.reshape(text)))
        except Exception:
            pass
    return text


def draw_unicode_lines(
    vis_bgr: np.ndarray,
    lines: List[str],
    xy: tuple,
    color_bgr: tuple = (240, 240, 240),
    font_size: int = 17,
) -> np.ndarray:
    """Draws 1-2 lines of Unicode (Arabic + English) text onto `vis_bgr` using PIL or OpenCV ASCII fallback."""
    font = _get_pil_font(font_size)
    if Image is not None and ImageDraw is not None and font is not None:
        try:
            rgb = cv2.cvtColor(vis_bgr, cv2.COLOR_BGR2RGB)
            pil_img = Image.fromarray(rgb)
            draw = ImageDraw.Draw(pil_img)
            color_rgb = (int(color_bgr[2]), int(color_bgr[1]), int(color_bgr[0]))
            x0, y0 = int(xy[0]), int(xy[1])
            for idx, ln in enumerate(lines[:2]):
                shaped = shape_bilingual_text(ln, allow_unicode=True)
                draw.text((x0, y0 + idx * (font_size + 6)), shaped, font=font, fill=color_rgb)
            return cv2.cvtColor(np.asarray(pil_img), cv2.COLOR_RGB2BGR)
        except Exception:
            pass

    # Fallback: OpenCV Hershey ASCII-only (never draws '???')
    x0, y0 = int(xy[0]), int(xy[1])
    for idx, ln in enumerate(lines[:2]):
        clean = shape_bilingual_text(ln, allow_unicode=False) or "Safety coaching active"
        cv2.putText(
            vis_bgr,
            clean[:110],
            (x0, y0 + 16 + idx * 22),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.50,
            color_bgr,
            1,
            cv2.LINE_AA,
        )
    return vis_bgr


def render_hud_frame(
    frame_roi: np.ndarray,
    poly: np.ndarray,
    state: SafetyState,
    fault_code: int = 0,
    status_flags: int = 0,
    p99_us: int = 0,
    fid: int = 0,
    bx: int = -1,
    by: int = -1,
    coaching: Optional[Dict[str, Any]] = None,
) -> np.ndarray:
    """Pure rendering function for HUD v2 (testable headlessly without opening a GUI window)."""
    vis = frame_roi.copy()
    is_fault = state == SafetyState.FAULT_TRIPPED
    is_calibrating = bool(status_flags & int(StatusFlag.CALIBRATING)) and not is_fault
    zone_color = (0, 0, 255) if is_fault else ((0, 190, 255) if is_calibrating else (0, 230, 90))

    if len(poly) >= 3:
        overlay = vis.copy()
        cv2.fillPoly(overlay, [poly], zone_color)
        cv2.addWeighted(overlay, 0.25 if is_fault else 0.12, vis, 0.75 if is_fault else 0.88, 0, vis)
        cv2.polylines(vis, [poly], isClosed=True, color=zone_color, thickness=3)

    if bx >= 0 and by >= 0:
        cv2.circle(vis, (bx, by), 22, (0, 0, 255), 3)
        cv2.drawMarker(vis, (bx, by), (0, 255, 255), cv2.MARKER_CROSS, 28, 2)
        cv2.putText(
            vis,
            f"BREACH ({bx},{by})",
            (max(10, bx - 75), max(55, by - 28)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.58,
            (0, 255, 255),
            2,
        )

    # 1. Top Telemetry + H-3 Visual Relay Indicator Banner
    cv2.rectangle(vis, (0, 0), (ROI_W, 46), (15, 15, 18), thickness=-1)
    try:
        fault_name = FaultCode(fault_code).name if fault_code > 0 else "NONE"
    except ValueError:
        fault_name = str(fault_code)

    banner = f"STATE: {state.name} | FAULT: {fault_name} | FSM P99: {p99_us}us | #{fid}"
    cv2.putText(vis, banner, (12, 29), cv2.FONT_HERSHEY_SIMPLEX, 0.54, zone_color, 2, cv2.LINE_AA)

    # H-3 Visual Relay Status Pill (Top-Right)
    relay_bg = (0, 0, 190) if is_fault else ((30, 130, 190) if is_calibrating else (0, 135, 45))
    relay_txt = "RELAY: 0xFF OPEN [CUT!]" if is_fault else ("RELAY: 0x00 WAIT [CAL]" if is_calibrating else "RELAY: 0x55 CLOSED [ON]")
    cv2.rectangle(vis, (ROI_W - 245, 7), (ROI_W - 10, 39), relay_bg, thickness=-1)
    cv2.putText(vis, relay_txt, (ROI_W - 237, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.50, (255, 255, 255), 2, cv2.LINE_AA)

    # 2. Warm-up Arming Gate Banner (while CALIBRATING)
    if is_calibrating:
        cv2.rectangle(vis, (90, 54), (ROI_W - 90, 108), (15, 45, 75), thickness=-1)
        cv2.rectangle(vis, (90, 54), (ROI_W - 90, 108), (0, 200, 255), thickness=2)
        vis = draw_unicode_lines(
            vis,
            [
                "CLEAR THE ZONE - HANDS OFF - Calibrating baseline...",
                "ابعد يدك عن منطقة الدوران - جاري معايرة الأمان...",
            ],
            (108, 58),
            color_bgr=(80, 235, 255),
            font_size=16,
        )

    # 3. Bottom 2-Line Word-Wrapped Bilingual Coaching Overlay
    c = coaching or _latest_coaching
    advice_text = str(c.get("advice") or "").strip()
    if advice_text:
        cv2.rectangle(vis, (0, 505), (ROI_W, ROI_H), (18, 18, 22), thickness=-1)
        tier_num = int(c.get("tier", 0))
        tier_label = (
            "TIER-0 LOCAL (<5ms)"
            if tier_num == 0
            else f"TIER-{tier_num} NEMOTRON ({c.get('model', 'Nemotron')} - {float(c.get('latency_ms', 0)):.0f}ms)"
        )
        zone_badge = f"ZONE: {c.get('zone', 'CENTER')} ({c.get('side', 'OPERATOR')}) | {c.get('hazard_code', 'H-02')}"
        hdr = f"[{tier_label}] {zone_badge}"
        cv2.putText(vis, hdr, (14, 526), cv2.FONT_HERSHEY_SIMPLEX, 0.48, (80, 220, 255), 1, cv2.LINE_AA)

        wrapped = textwrap.wrap(advice_text.replace("\n", " "), width=92)[:2]
        vis = draw_unicode_lines(vis, wrapped, (14, 534), color_bgr=(242, 242, 245), font_size=17)

    return vis


def _ws_listener_thread(ws_port: int):
    """Background listener receiving Tier-0/1/2 events from :8766."""
    import asyncio
    import websockets

    async def _listen():
        uri = f"ws://127.0.0.1:{ws_port}"
        while True:
            try:
                async with websockets.connect(uri) as ws:
                    async for msg in ws:
                        data = json.loads(msg)
                        msg_type = data.get("type")
                        if msg_type == "breach_event":
                            _latest_coaching.update(
                                {
                                    "tier": 0,
                                    "advice": f"{data.get('advice_en', '')} | {data.get('advice_ar', '')}",
                                    "advice_ar": data.get("advice_ar", ""),
                                    "advice_en": data.get("advice_en", ""),
                                    "latency_ms": 1.0,
                                    "model": "LOCAL",
                                    "zone": data.get("zone", "CENTER"),
                                    "side": data.get("side", "OPERATOR"),
                                    "hazard_code": data.get("hazard_code", "H-02"),
                                }
                            )
                        elif msg_type == "nebius_coaching":
                            _latest_coaching.update(
                                {
                                    "tier": data.get("tier", 1),
                                    "advice": data.get("advice", ""),
                                    "latency_ms": data.get("latency_ms", 0.0),
                                    "model": data.get("model", "Nemotron"),
                                }
                            )
            except Exception:
                await asyncio.sleep(0.5)

    asyncio.run(_listen())


def run_hud(shm_name: str, ws_port: int):
    t = threading.Thread(target=_ws_listener_thread, args=(ws_port,), daemon=True)
    t.start()

    with win32_realtime_context(affinity_mask=MASK_PEDAGOGY_CORES, time_critical=False):
        reader = LatheShmReader(shm_name=shm_name)
        gui_available = True
        win_title = "LatheGuard Physical AI — 100Hz Safety HUD v2 (Press Q/ESC to close)"
        try:
            cv2.namedWindow(win_title, cv2.WINDOW_NORMAL)
            cv2.resizeWindow(win_title, 960, 640)
        except cv2.error as e:
            gui_available = False
            print(f"[HUD] Note: OpenCV GUI highgui not available ({e}); running headless HUD telemetry...", file=sys.stderr)

        last_frame = np.zeros((ROI_H, ROI_W, 3), dtype=np.uint8)
        last_poly = np.zeros((0, 2), dtype=np.int32)

        try:
            while True:
                res = reader.read_latest(copy_frame=True)
                if res is not None:
                    frame_roi, poly, _, _ = res
                    last_frame = frame_roi.copy()
                    last_poly = poly

                state = SafetyState(reader.header.safety_state)
                vis = render_hud_frame(
                    frame_roi=last_frame,
                    poly=last_poly,
                    state=state,
                    fault_code=int(reader.header.fault_code),
                    status_flags=int(reader.header.status_flags),
                    p99_us=int(reader.header.p99_latency_us),
                    fid=int(reader.header.frame_id),
                    bx=int(reader.header.intrusion_x),
                    by=int(reader.header.intrusion_y),
                    coaching=_latest_coaching,
                )

                if gui_available:
                    cv2.imshow(win_title, vis)
                    key = cv2.waitKey(16) & 0xFF
                    if key in (27, ord("q"), ord("Q")):
                        break
                else:
                    time.sleep(0.016)
        finally:
            if gui_available:
                try:
                    cv2.destroyAllWindows()
                except Exception:
                    pass
            reader.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LatheGuard Live OpenCV HUD Viewer v2")
    parser.add_argument("--shm", default=DEFAULT_SHM_NAME)
    parser.add_argument("--ws-port", type=int, default=8766)
    args = parser.parse_args()
    run_hud(args.shm, args.ws_port)
