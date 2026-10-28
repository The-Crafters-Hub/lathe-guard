"""
test_e2e_lathe_guard.py — End-to-End Simulation of Capture + Safety Guard Pipeline
The Crafters Hub | The Cabinet AI Automation
"""

import multiprocessing as mp
import time
from lathe_vision_capture import run_capture_loop
from lathe_safety_engine import LatheSafetyEngine, SafetyState
from lathe_scheduler import MASK_SAFETY_CORES, win32_realtime_context
from lathe_shm import DEFAULT_SHM_NAME


def run_e2e():
    mp.set_start_method("spawn", force=True)
    shm_name = "cabinet_e2e_test_shm"

    print("==================================================================")
    print("  THE CABINET: LATHE GUARD END-TO-END PIPELINE SIMULATION")
    print("  Host: Windows 11 | i9-11950H | RTX A2000 | Zero-Cost Brain")
    print("==================================================================")

    # 1. Start Capture Worker in separate process (simulating 60 FPS Android feed)
    capture_proc = mp.Process(
        target=run_capture_loop,
        args=("0", shm_name),
        kwargs={"mock_mode": True, "max_frames": 300}
    )
    capture_proc.start()

    # Allow capture process to create SHM and start publishing
    time.sleep(0.5)

    # 2. Run Safety Engine in current process
    engine = LatheSafetyEngine(shm_name=shm_name, com_port="MOCK", mock_serial=True)

    heartbeat_count = 0
    start_time = time.perf_counter()

    with win32_realtime_context(affinity_mask=MASK_SAFETY_CORES, time_critical=True):
        print("[+] Safety Engine Monitoring Active... (Running for 2.5 seconds)")
        while time.perf_counter() - start_time < 2.5:
            state = engine.step()
            if engine.serial.tx_history and engine.serial.tx_history[-1:] == b"\x55":
                heartbeat_count += 1
            time.sleep(0.008)  # ~125 Hz loop cadence

        print(f"[+] Heartbeats emitted: {heartbeat_count} (Target: ~100Hz)")
        print(f"[+] P99 Loop Latency recorded: {engine.reader.header.p99_latency_us} microseconds")
        print(f"[+] Current FSM State: {SafetyState(engine.reader.header.safety_state).name}")

        # 3. Simulate Red-Zone Intrusion!
        print("\n[!] SIMULATING STUDENT HAND BREACHING ROTATING BLANK ZONE (450, 300)...")
        engine.simulated_fingertips = [(450, 300)]
        time.sleep(0.02)
        state = engine.step()
        print(f"[!] FSM State after breach: {SafetyState(state).name}")
        print(f"[!] Last Byte sent to Hardware Watchdog: {hex(engine.serial.tx_history[-1])}")
        assert state == int(SafetyState.FAULT_TRIPPED), "Failed to trip!"
        assert engine.serial.tx_history[-1:] == b"\xFF", "Failed to transmit emergency stop byte!"

    engine.close()
    capture_proc.join(timeout=3.0)
    if capture_proc.is_alive():
        capture_proc.terminate()

    print("\n[PASS] End-to-End Lathe Guard Pipeline Successfully Verified!")


def test_e2e_capture_and_safety_fsm_pipeline():
    """Run the end-to-end multi-process capture + safety FSM pipeline test under pytest."""
    run_e2e()


if __name__ == "__main__":
    run_e2e()
