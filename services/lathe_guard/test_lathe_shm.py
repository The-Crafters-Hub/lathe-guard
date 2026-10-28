"""
test_lathe_shm.py — Seqlock IPC Zero-Corruption Benchmark
The Crafters Hub | The Cabinet AI Automation
"""

import ctypes
import multiprocessing as mp
import time
import numpy as np
from lathe_shm import (
    AlignedShmHeader,
    FaultCode,
    HEADER_BYTES,
    LatheShmReader,
    LatheShmWriter,
    ROI_C,
    ROI_H,
    ROI_W,
    StatusFlag,
)

TEST_SHM_NAME = "cabinet_shm_bench_test"
TOTAL_FRAMES = 1000


def test_shm_header_v2_layout_and_roundtrip():
    """T1.1: Verify 128-byte cache-line alignment, offsets 56/60, and FaultCode/StatusFlag round-trip."""
    assert ctypes.sizeof(AlignedShmHeader) == 128
    assert HEADER_BYTES == 128
    assert AlignedShmHeader.fault_code.offset == 56
    assert AlignedShmHeader.status_flags.offset == 60
    assert AlignedShmHeader.poly_coords.offset == 64

    shm_name = "cabinet_shm_v2_unit_test"
    writer = LatheShmWriter(shm_name=shm_name, create=True)
    reader = LatheShmReader(shm_name=shm_name)
    try:
        assert reader.header.fault_code == int(FaultCode.NONE)
        assert reader.header.status_flags == int(StatusFlag.NONE)

        writer.header.fault_code = int(FaultCode.HAND_OR_TOOL_MOAT)
        writer.header.status_flags = int(StatusFlag.DETECTOR_ARMED | StatusFlag.CAMERA_MOVED)
        assert reader.header.fault_code == int(FaultCode.HAND_OR_TOOL_MOAT)
        assert reader.header.status_flags == int(StatusFlag.DETECTOR_ARMED | StatusFlag.CAMERA_MOVED)
    finally:
        reader.close()
        writer.close()


def producer_proc(ready_evt: mp.Event, done_evt: mp.Event):
    writer = LatheShmWriter(shm_name=TEST_SHM_NAME, create=True)
    frame = np.zeros((ROI_H, ROI_W, ROI_C), dtype=np.uint8)
    poly = np.array([[100, 100], [800, 100], [800, 500], [100, 500]], dtype=np.int32)
    ready_evt.set()

    for i in range(1, TOTAL_FRAMES + 1):
        checksum_byte = i % 251  # Prime modulo checksum pattern
        frame.fill(checksum_byte)
        poly[0, 0] = checksum_byte
        writer.publish_frame(frame, poly)
        time.sleep(0.001)  # High-rate publish (~1000 FPS)

    done_evt.wait(timeout=5.0)
    writer.close()


def main():
    mp.set_start_method("spawn", force=True)
    ready_evt = mp.Event()
    done_evt = mp.Event()

    p = mp.Process(target=producer_proc, args=(ready_evt, done_evt))
    p.start()
    ready_evt.wait(timeout=5.0)

    reader = LatheShmReader(shm_name=TEST_SHM_NAME)
    read_count = 0
    corruptions = 0
    t0 = time.perf_counter()

    while read_count < TOTAL_FRAMES:
        res = reader.read_latest(copy_frame=True)
        if res is None:
            if not p.is_alive() and read_count > 0:
                break
            continue
        frame, poly, frame_id, _ = res
        expected_val = frame_id % 251
        # Verify zero frame tearing (first pixel == last pixel == polygon checksum)
        if frame[0, 0, 0] != expected_val or frame[-1, -1, -1] != expected_val or poly[0, 0] != expected_val:
            corruptions += 1
        read_count += 1

    elapsed = time.perf_counter() - t0
    fps = read_count / elapsed
    done_evt.set()
    reader.close()
    p.join()

    print(f"[SHM BENCHMARK] Frames Verified: {read_count} | Throughput: {fps:.1f} FPS | Corruptions: {corruptions}")
    assert corruptions == 0, "FATAL: Seqlock memory tearing detected!"
    assert fps > 120.0, f"Throughput {fps:.1f} FPS below 120 FPS requirement!"
    print("[PASS] Module 1 Seqlock IPC Verified Zero-Corruption.")


def test_shm_recreate_with_active_reader_and_seq_reset():
    """Verify Windows SharedMemory re-attach on writer restart (--demo-rearm) and reader seq reset."""
    shm_name = "cabinet_shm_rearm_unit_test"
    writer1 = LatheShmWriter(shm_name=shm_name, create=True)
    reader = LatheShmReader(shm_name=shm_name)
    frame = np.full((ROI_H, ROI_W, ROI_C), 42, dtype=np.uint8)
    poly = np.array([[100, 100], [800, 100], [800, 500], [100, 500]], dtype=np.int32)
    try:
        for _ in range(5):
            writer1.publish_frame(frame, poly)
        res1 = reader.read_latest(copy_frame=True)
        assert res1 is not None
        assert reader.last_seq == 10

        # Simulate --demo-rearm: writer1 closes, while reader still holds open SHM handle on Windows
        writer1.close()
        writer2 = LatheShmWriter(shm_name=shm_name, create=True)
        try:
            # Header seqlock_seq was reset to 0 by writer2; publish 1 new frame (seq=2 < reader.last_seq=10)
            frame2 = np.full((ROI_H, ROI_W, ROI_C), 99, dtype=np.uint8)
            writer2.publish_frame(frame2, poly)
            res2 = reader.read_latest(copy_frame=True)
            assert res2 is not None, "Reader failed to recover after writer restart reset seqlock_seq!"
            assert res2[0][0, 0, 0] == 99
            assert reader.last_seq == 2
        finally:
            writer2.close()
    finally:
        reader.close()


def test_seqlock_multiprocess_zero_corruption_benchmark():
    """Run the 1000-frame multi-process Seqlock benchmark under pytest."""
    main()


if __name__ == "__main__":
    main()
