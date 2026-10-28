"""
run_lathe_guard.py — Unified Multi-Process Supervisor & Demo Launcher for LatheGuard (Phase 4: T4.3)
The Crafters Hub | LatheGuard Physical AI — Nebius × NVIDIA Hackathon Submission
Author: Hosam (Co-Founder, The Crafters Hub)

Sequentially launches and supervises all LatheGuard subsystems over Seqlock SHM:
  1. lathe_vision_capture.py  — 60 FPS Camera / Video Replay / Dynamic Red-Zone Producer (Core 1)
  2. lathe_safety_engine.py   — 100Hz Deterministic Safety FSM + Live OpenCV Breach Guard (Core 0)
  3. nebius_hazard_advisor.py — 5Hz Async 3-Tier Nebius (NVIDIA Nemotron) Sidecar (Cores 2-7)
  4. incident_recorder.py     — Optional 8s Black-Box MP4 Flight Recorder (Cores 2-7)
  5. lathe_hud_preview.py     — Optional (--preview) non-blocking OpenCV live video HUD v2 (Cores 2-7)
"""

import argparse
from multiprocessing import shared_memory
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Dict, List, Optional

SERVICE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SERVICE_DIR))

from lathe_shm import AlignedShmHeader, DEFAULT_SHM_NAME, SafetyState  # noqa: E402


def wait_for_shm_ready(shm_name: str, timeout_s: float = 8.0) -> bool:
    """Wait until capture process creates the Seqlock SHM and commits at least 1 even frame."""
    deadline = time.perf_counter() + timeout_s
    while time.perf_counter() < deadline:
        try:
            shm = shared_memory.SharedMemory(name=shm_name, create=False)
            hdr = AlignedShmHeader.from_buffer(shm.buf, 0)
            seq = int(hdr.seqlock_seq)
            del hdr
            shm.close()
            if seq >= 2 and (seq & 1) == 0:
                return True
        except FileNotFoundError:
            pass
        time.sleep(0.05)
    return False


def read_shm_safety_state(shm_name: str) -> Optional[int]:
    """Reads `safety_state` from the SHM header without holding a persistent buffer reference."""
    try:
        shm = shared_memory.SharedMemory(name=shm_name, create=False)
        hdr = AlignedShmHeader.from_buffer(shm.buf, 0)
        st = int(hdr.safety_state)
        del hdr
        shm.close()
        return st
    except Exception:
        return None


def build_commands(args: argparse.Namespace) -> Dict[str, List[str]]:
    py = sys.executable
    source_val = getattr(args, "source", "0")
    if source_val and not str(source_val).isdigit() and not str(source_val).startswith(("rtsp://", "http://", "https://")):
        if os.path.exists(source_val):
            source_val = os.path.abspath(source_val)
        elif (SERVICE_DIR / source_val).exists():
            source_val = str((SERVICE_DIR / source_val).resolve())
        else:
            source_val = os.path.abspath(source_val)
        args.source = source_val

    cap_cmd = [
        py, "-u", str(SERVICE_DIR / "lathe_vision_capture.py"),
        "--shm", args.shm,
        "--source", str(args.source),
    ]
    if args.mock:
        cap_cmd.append("--mock")
    if getattr(args, "loop", False):
        cap_cmd.append("--loop")
    if args.visual_breach_after > 0:
        cap_cmd.extend(["--visual-breach-after", str(args.visual_breach_after)])
    if getattr(args, "profile", None):
        cap_cmd.extend(["--profile", str(args.profile)])

    fsm_cmd = [
        py, "-u", str(SERVICE_DIR / "lathe_safety_engine.py"),
        "--shm", args.shm,
        "--com", args.com,
    ]
    if getattr(args, "profile", None):
        fsm_cmd.extend(["--profile", str(args.profile)])
    if args.mock or args.mock_serial:
        fsm_cmd.append("--mock-serial")
    if (not args.mock) or args.visual_breach_after > 0 or args.cv_detect:
        fsm_cmd.append("--cv-detect")
    if args.simulate_breach_after > 0:
        fsm_cmd.extend(["--simulate-breach-after", str(args.simulate_breach_after)])

    adv_cmd = [
        py, "-u", str(SERVICE_DIR / "nebius_hazard_advisor.py"),
        "--shm", args.shm,
        "--ws-port", str(args.ws_port),
    ]
    rec_cmd = [
        py, "-u", str(SERVICE_DIR / "incident_recorder.py"),
        "--shm", args.shm,
    ]
    hud_cmd = [
        py, "-u", str(SERVICE_DIR / "lathe_hud_preview.py"),
        "--shm", args.shm,
        "--ws-port", str(args.ws_port),
    ]
    return {
        "capture": cap_cmd,
        "safety_fsm": fsm_cmd,
        "nebius_advisor": adv_cmd,
        "recorder": rec_cmd,
        "hud": hud_cmd,
    }


def terminate_all(procs: Dict[str, subprocess.Popen]) -> None:
    for name in ("hud", "recorder", "nebius_advisor", "safety_fsm", "capture"):
        proc = procs.get(name)
        if proc and proc.poll() is None:
            try:
                proc.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGTERM)
            except Exception:
                proc.terminate()
    deadline = time.perf_counter() + 3.0
    for proc in procs.values():
        if proc and proc.poll() is None:
            remaining = max(0.1, deadline - time.perf_counter())
            try:
                proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                proc.kill()


def run_supervisor(args: argparse.Namespace) -> int:
    # T4.3 Safety Gate: --demo-rearm is strictly forbidden when driving a physical COM port
    if getattr(args, "demo_rearm", 0.0) > 0.0 and not (args.mock or args.mock_serial):
        print(
            "[FATAL SAFETY VIOLATION] --demo-rearm is only permitted with --mock or --mock-serial. "
            "Auto-rearming a physical lathe relay is strictly prohibited.",
            file=sys.stderr,
        )
        return 2

    cmds = build_commands(args)
    procs: Dict[str, subprocess.Popen] = {}
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["LATHEGUARD_PROFILE"] = getattr(args, "profile", "crafters_hub_midi_lathe")
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0

    print("=" * 72)
    print("LatheGuard Physical AI Supervisor — Nebius × NVIDIA Edition")
    print(f"SHM Segment : {args.shm} | Profile: {env['LATHEGUARD_PROFILE']} | Mode: {'MOCK BENCH' if args.mock else 'LIVE/REPLAY'}")
    print(f"Camera      : {'synthetic 60fps' if args.mock else args.source} | Watchdog: {'MOCK' if (args.mock or args.mock_serial) else args.com} | WS: :{args.ws_port}")
    print("=" * 72)

    try:
        # 1. Start Vision Capture & wait for Seqlock ring initialization
        print("[1/4] Starting Vision Capture & Dynamic Red-Zone Profiler...")
        procs["capture"] = subprocess.Popen(cmds["capture"], cwd=str(SERVICE_DIR), env=env, creationflags=creationflags)
        if not wait_for_shm_ready(args.shm, timeout_s=8.0):
            print("[FATAL] Seqlock SHM failed to initialize within 8.0s.", file=sys.stderr)
            return 1
        print(f"[OK] Seqlock SHM '{args.shm}' online with verified frame commit.")

        # 2. Start 100Hz Deterministic Safety FSM
        print("[2/4] Starting 100Hz Safety FSM & Live Hand/Chisel Intrusion Guard...")
        procs["safety_fsm"] = subprocess.Popen(cmds["safety_fsm"], cwd=str(SERVICE_DIR), env=env, creationflags=creationflags)
        time.sleep(0.25)

        # 3. Start Nebius × NVIDIA Hazard Advisor Sidecar (optional skip via --no-nebius)
        if not args.no_nebius:
            print("[3/4] Starting Nebius 3-Tier Hazard Advisor (NVIDIA Nemotron)...")
            procs["nebius_advisor"] = subprocess.Popen(cmds["nebius_advisor"], cwd=str(SERVICE_DIR), env=env, creationflags=creationflags)

        # 4. Start Black-Box Incident Flight Recorder (unless disabled or in synthetic unit-test mock without recorder flag)
        if getattr(args, "enable_recorder", False) and not getattr(args, "no_recorder", False):
            print("[4/4] Starting 8s Black-Box Incident Flight Recorder...")
            procs["recorder"] = subprocess.Popen(cmds["recorder"], cwd=str(SERVICE_DIR), env=env, creationflags=creationflags)

        # Optional 5. Start Live OpenCV HUD Preview Window
        if args.preview:
            print("[HUD] Opening non-blocking OpenCV Red-Zone Preview Window v2...")
            procs["hud"] = subprocess.Popen(cmds["hud"], cwd=str(SERVICE_DIR), env=env, creationflags=creationflags)

        t0 = time.perf_counter()
        fault_latched_since: Optional[float] = None

        while True:
            if args.duration > 0 and (time.perf_counter() - t0) >= args.duration:
                print(f"[SUPERVISOR] Reached scheduled duration ({args.duration:.1f}s) — shutting down cleanly.")
                return 0

            # If operator closes the HUD window (presses Q/ESC), shut down cleanly
            hud_p = procs.get("hud")
            if hud_p and hud_p.poll() is not None:
                print("[SUPERVISOR] HUD window closed by operator — shutting down cleanly.")
                return 0

            # If capture or safety_fsm exits unexpectedly, fail-safe immediately
            for critical in ("capture", "safety_fsm"):
                p = procs.get(critical)
                if p and p.poll() is not None:
                    print(f"[CRITICAL] Subsystem '{critical}' exited with code {p.returncode}!", file=sys.stderr)
                    return p.returncode or 1

            # Auto-restart non-blocking Nebius coaching sidecar if transient crash occurs
            adv = procs.get("nebius_advisor")
            if adv and adv.poll() is not None:
                print(f"[WARN] Nebius advisor exited (code={adv.returncode}); restarting sidecar without interrupting 100Hz FSM...")
                procs["nebius_advisor"] = subprocess.Popen(cmds["nebius_advisor"], cwd=str(SERVICE_DIR), env=env, creationflags=creationflags)

            # T4.3 Demo Re-Arm (ONLY in --mock / --mock-serial mode for multi-take video filming)
            demo_rearm_s = float(getattr(args, "demo_rearm", 0.0))
            if demo_rearm_s > 0.0 and (args.mock or args.mock_serial):
                st = read_shm_safety_state(args.shm)
                if st == int(SafetyState.FAULT_TRIPPED):
                    if fault_latched_since is None:
                        fault_latched_since = time.perf_counter()
                    elif (time.perf_counter() - fault_latched_since) >= demo_rearm_s:
                        print(f"[DEMO RE-ARM] Trip latched for {demo_rearm_s:.1f}s in mock mode — restarting capture + FSM for next take...")
                        for k in ("safety_fsm", "capture"):
                            pk = procs.get(k)
                            if pk and pk.poll() is None:
                                pk.terminate()
                                try:
                                    pk.wait(timeout=1.5)
                                except Exception:
                                    pk.kill()
                        procs["capture"] = subprocess.Popen(cmds["capture"], cwd=str(SERVICE_DIR), env=env, creationflags=creationflags)
                        wait_for_shm_ready(args.shm, timeout_s=5.0)
                        procs["safety_fsm"] = subprocess.Popen(cmds["safety_fsm"], cwd=str(SERVICE_DIR), env=env, creationflags=creationflags)
                        fault_latched_since = None
                else:
                    fault_latched_since = None

            time.sleep(0.2)

    except KeyboardInterrupt:
        print("\n[SUPERVISOR] Operator interrupt received — disarming subsystems...")
        return 0
    finally:
        terminate_all(procs)


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Unified Launcher for LatheGuard Physical AI")
    parser.add_argument("--shm", default=DEFAULT_SHM_NAME, help="Seqlock shared memory segment name")
    parser.add_argument("--source", default="0", help="Camera index ('0'), local MP4 path, or RTSP/HTTP stream URL")
    parser.add_argument("--loop", action="store_true", help="Loop local video file when --source is a file")
    parser.add_argument("--profile", default="crafters_hub_midi_lathe", help="Lathe profile name or JSON path")
    parser.add_argument("--com", default="COM3", help="Arduino hardware watchdog serial port")
    parser.add_argument("--ws-port", type=int, default=8766, help="WebSocket port for Osta voice coaching")
    parser.add_argument("--preview", action="store_true", help="Open live OpenCV camera + Red-Zone HUD window")
    parser.add_argument("--mock", action="store_true", help="Zero-hardware bench mode (synthetic camera + mock serial)")
    parser.add_argument("--mock-serial", action="store_true", help="Use real camera/video with mock serial watchdog")
    parser.add_argument("--cv-detect", action="store_true", help="Force-enable live OpenCV hand/chisel detector")
    parser.add_argument("--no-nebius", action="store_true", help="Run local capture + FSM without cloud advisor")
    parser.add_argument("--enable-recorder", action="store_true", help="Spawn 8s black-box incident MP4 flight recorder")
    parser.add_argument("--no-recorder", action="store_true", help="Disable black-box incident flight recorder")
    parser.add_argument("--demo-rearm", type=float, default=0.0, help="Auto-rearm after N seconds ONLY in --mock / --mock-serial mode")
    parser.add_argument("--visual-breach-after", type=float, default=0.0, help="In --mock mode, draw steel gouge + hand entering Red-Zone after N seconds")
    parser.add_argument("--simulate-breach-after", type=float, default=0.0, help="Inject coordinate Red-Zone breach after N seconds")
    parser.add_argument("--duration", type=float, default=0.0, help="Auto-stop after N seconds (0 = run until Ctrl+C)")
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(run_supervisor(parse_args()))
