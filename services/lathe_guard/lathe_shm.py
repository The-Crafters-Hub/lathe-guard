"""
lathe_shm.py — Lock-Free Seqlock Shared Memory IPC for Lathe Safety Guard
The Crafters Hub | LatheGuard Physical AI — Nebius × NVIDIA Hackathon Submission
Author: Hosam (Co-Founder, The Crafters Hub)
"""

import ctypes
from enum import IntEnum, IntFlag
from multiprocessing import shared_memory
import time
from typing import Optional, Tuple
import numpy as np

# --- GEOMETRY & MEMORY CONSTANTS ---
ROI_H, ROI_W, ROI_C = 600, 900, 3
FRAME_BYTES = ROI_H * ROI_W * ROI_C  # 1,620,000 bytes (1.62 MB)
RING_SLOTS = 3
RING_BYTES = FRAME_BYTES * RING_SLOTS  # 4,860,000 bytes (4.86 MB)
DEFAULT_SHM_NAME = "cabinet_lathe_seqlock_v2"


class SafetyState(IntEnum):
    DISARMED = 0
    ARMED_MONITORING = 1
    DEGRADED_PEDAGOGY = 2
    FAULT_TRIPPED = 3


class FaultCode(IntEnum):
    NONE = 0
    COLLAR = 1
    HAND_OR_TOOL_MOAT = 2
    CAMERA_STALE = 3
    SERIAL_FAULT = 4
    SIMULATED = 5
    CAMERA_OBSTRUCTED = 6
    CAMERA_MOVED = 7
    DETECTOR_EXCEPTION = 8


class StatusFlag(IntFlag):
    NONE = 0
    DETECTOR_ARMED = 1 << 0     # Bit 0
    CAMERA_FROZEN = 1 << 1      # Bit 1
    CAMERA_OBSTRUCTED = 1 << 2  # Bit 2
    CAMERA_MOVED = 1 << 3       # Bit 3
    CALIBRATING = 1 << 4        # Bit 4


class AlignedShmHeader(ctypes.Structure):
    """
    Exact 128-byte (2 x 64-byte x86-64 cache lines) Seqlock Control Header.
    Line 0 (Bytes 0..63): Hot Seqlock, state, and telemetry fields.
    Line 1 (Bytes 64..127): Dynamic polygon vertices + explicit padding.
    """
    _pack_ = 1
    _fields_ = [
        # --- Cache Line 0 (64 Bytes) ---
        ("seqlock_seq", ctypes.c_uint64),        # 8B (Offset 0): Odd = writing, Even = valid
        ("active_slot", ctypes.c_uint32),        # 4B (Offset 8): Ring buffer slot (0, 1, 2)
        ("safety_state", ctypes.c_uint32),       # 4B (Offset 12): SafetyState IntEnum
        ("last_capture_ns", ctypes.c_uint64),    # 8B (Offset 16): perf_counter_ns() of capture
        ("last_safety_ns", ctypes.c_uint64),     # 8B (Offset 24): perf_counter_ns() of safety check
        ("p99_latency_us", ctypes.c_uint32),     # 4B (Offset 32): Rolling P99 loop time (us)
        ("poly_vertex_count", ctypes.c_uint32),  # 4B (Offset 36): Number of vertices (max 8)
        ("frame_id", ctypes.c_uint64),           # 8B (Offset 40): Monotonic frame index
        ("intrusion_x", ctypes.c_int32),         # 4B (Offset 48): Detected breach X in ROI (-1 if clear)
        ("intrusion_y", ctypes.c_int32),         # 4B (Offset 52): Detected breach Y in ROI (-1 if clear)
        ("fault_code", ctypes.c_uint32),         # 4B (Offset 56): FaultCode IntEnum
        ("status_flags", ctypes.c_uint32),       # 4B (Offset 60): StatusFlag bitmask -> Line 0 = 64B exact
        # --- Cache Line 1 (64 Bytes) ---
        ("poly_coords", ctypes.c_int32 * 16),    # 64B (Offset 64..127): 8 vertices * (x, y) int32
    ]


HEADER_BYTES = ctypes.sizeof(AlignedShmHeader)
assert HEADER_BYTES == 128, f"Header must be cache-line aligned (128B), got {HEADER_BYTES}B"
TOTAL_SHM_BYTES = HEADER_BYTES + RING_BYTES


def _compiler_and_cpu_fence():
    """Lightweight memory barrier on x86-64."""
    dummy = ctypes.c_uint32(0)
    ctypes.pointer(dummy)[0] = 1


class LatheShmWriter:
    """Single-Producer Seqlock Writer for the Capture Process."""

    def __init__(self, shm_name: str = DEFAULT_SHM_NAME, create: bool = True):
        self.shm_name = shm_name
        self.is_owner = create
        if create:
            try:
                old = shared_memory.SharedMemory(name=shm_name)
                old.close()
                old.unlink()
            except FileNotFoundError:
                pass
            try:
                self.shm = shared_memory.SharedMemory(name=shm_name, create=True, size=TOTAL_SHM_BYTES)
            except FileExistsError:
                # On Windows, if reader processes (advisor/recorder/hud) hold open handles
                # during --demo-rearm restart, unlink() is a no-op and create=True raises
                # WinError 183 (FileExistsError). Re-attach and reset the header cleanly.
                self.shm = shared_memory.SharedMemory(name=shm_name, create=False)
        else:
            self.shm = shared_memory.SharedMemory(name=shm_name, create=False)

        self.header = AlignedShmHeader.from_buffer(self.shm.buf, 0)
        self.ring = np.ndarray(
            (RING_SLOTS, ROI_H, ROI_W, ROI_C),
            dtype=np.uint8,
            buffer=self.shm.buf,
            offset=HEADER_BYTES,
        )
        if create:
            self.header.seqlock_seq = 0
            self.header.active_slot = 0
            self.header.safety_state = int(SafetyState.DISARMED)
            self.header.last_capture_ns = 0
            self.header.last_safety_ns = 0
            self.header.p99_latency_us = 0
            self.header.poly_vertex_count = 0
            self.header.frame_id = 0
            self.header.intrusion_x = -1
            self.header.intrusion_y = -1
            self.header.fault_code = int(FaultCode.NONE)
            self.header.status_flags = int(StatusFlag.NONE)

    def publish_frame(self, roi_bgr: np.ndarray, polygon: np.ndarray) -> int:
        """
        Writes an ROI frame and dynamic polygon using an odd/even Seqlock protocol.
        Returns the committed sequence number (always even).
        """
        if roi_bgr.shape != (ROI_H, ROI_W, ROI_C):
            raise ValueError(f"Expected frame shape {(ROI_H, ROI_W, ROI_C)}, got {roi_bgr.shape}")

        next_slot = (self.header.active_slot + 1) % RING_SLOTS

        # 1. Write payload into the inactive next_slot BEFORE touching seqlock_seq
        np.copyto(self.ring[next_slot], roi_bgr)
        _compiler_and_cpu_fence()

        # 2. SEQLOCK BEGIN: Increment sequence to ODD (signals readers write in progress)
        seq = self.header.seqlock_seq + 1
        self.header.seqlock_seq = seq
        _compiler_and_cpu_fence()

        # 3. Update active slot, timestamps, and polygon coordinates
        self.header.active_slot = next_slot
        self.header.last_capture_ns = time.perf_counter_ns()
        self.header.frame_id += 1

        verts = min(8, len(polygon))
        self.header.poly_vertex_count = verts
        flat = polygon[:verts].flatten()
        for i in range(verts * 2):
            self.header.poly_coords[i] = int(flat[i])

        _compiler_and_cpu_fence()

        # 4. SEQLOCK END: Increment sequence to EVEN (signals readers snapshot is consistent)
        self.header.seqlock_seq = seq + 1
        return seq + 1

    def close(self):
        import gc
        self.ring = None
        self.header = None
        gc.collect()
        try:
            self.shm.close()
        except BufferError:
            pass
        if self.is_owner:
            try:
                self.shm.unlink()
            except FileNotFoundError:
                pass


class LatheShmReader:
    """Lock-Free Seqlock Reader for Fast Safety and Slow Pedagogical Workers."""

    def __init__(self, shm_name: str = DEFAULT_SHM_NAME):
        self.shm = shared_memory.SharedMemory(name=shm_name, create=False)
        self.header = AlignedShmHeader.from_buffer(self.shm.buf, 0)
        self.ring = np.ndarray(
            (RING_SLOTS, ROI_H, ROI_W, ROI_C),
            dtype=np.uint8,
            buffer=self.shm.buf,
            offset=HEADER_BYTES,
        )
        self._local_frame_buf = np.empty((ROI_H, ROI_W, ROI_C), dtype=np.uint8)
        self.last_seq = 0

    def read_latest(self, copy_frame: bool = False) -> Optional[Tuple[np.ndarray, np.ndarray, int, int]]:
        """
        Attempts a lock-free consistent Seqlock read.
        Returns (frame_roi, polygon_nx2, frame_id, capture_ns) or None if no new frame.
        """
        for _ in range(5):  # Retry up to 5 times if writer preempts mid-read
            seq1 = self.header.seqlock_seq
            if (seq1 & 1) != 0:
                continue  # Writer is currently updating metadata
            if seq1 == 0:
                self.last_seq = 0
                return None
            if seq1 < self.last_seq:
                # Writer restarted (e.g. --demo-rearm) and reset seqlock_seq
                self.last_seq = 0
            if seq1 == self.last_seq:
                return None  # No new frame published since last read

            _compiler_and_cpu_fence()
            slot = self.header.active_slot
            frame_id = self.header.frame_id
            cap_ns = self.header.last_capture_ns
            n_verts = self.header.poly_vertex_count
            poly = np.array(self.header.poly_coords[: n_verts * 2], dtype=np.int32).reshape(-1, 2)

            if copy_frame:
                np.copyto(self._local_frame_buf, self.ring[slot])
                frame_out = self._local_frame_buf
            else:
                frame_out = self.ring[slot]

            _compiler_and_cpu_fence()
            seq2 = self.header.seqlock_seq

            if seq1 == seq2:
                self.last_seq = seq1
                return frame_out, poly, frame_id, cap_ns

        return None

    def close(self):
        import gc
        self.ring = None
        self.header = None
        gc.collect()
        try:
            self.shm.close()
        except BufferError:
            pass
