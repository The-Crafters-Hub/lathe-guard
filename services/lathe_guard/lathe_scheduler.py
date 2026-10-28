"""
lathe_scheduler.py — Win32 Real-Time Scheduler & Core Affinity Manager
The Crafters Hub | LatheGuard Physical AI — Nebius × NVIDIA Hackathon Submission
Author: Hosam (Co-Founder, The Crafters Hub)
Host: Windows 11 | Intel Core i9-11950H (8 Cores / 16 Threads) — clean POSIX no-op fallback
"""

from contextlib import contextmanager
import ctypes
import os
from typing import Callable

IS_WINDOWS = os.name == "nt"

# Logical Core Bitmasks for Intel Core i9-11950H (8 Cores / 16 Threads)
MASK_SAFETY_CORES = 0b0000_0000_0000_0011     # Logical Processors 0-1 (Physical Core 0)
MASK_CAPTURE_CORES = 0b0000_0000_0000_1100    # Logical Processors 2-3 (Physical Core 1)
MASK_PEDAGOGY_CORES = 0b1111_1111_1111_0000   # Logical Processors 4-15 (Physical Cores 2-7)

if IS_WINDOWS:
    kernel32 = ctypes.windll.kernel32
    winmm = ctypes.windll.winmm

    NORMAL_PRIORITY_CLASS = 0x00000020
    HIGH_PRIORITY_CLASS = 0x00000080
    REALTIME_PRIORITY_CLASS = 0x00000100

    THREAD_PRIORITY_NORMAL = 0
    THREAD_PRIORITY_HIGHEST = 2
    THREAD_PRIORITY_TIME_CRITICAL = 15

    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.GetCurrentThread.restype = ctypes.c_void_p
    kernel32.SetProcessAffinityMask.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    kernel32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.SetThreadPriority.argtypes = [ctypes.c_void_p, ctypes.c_int]


@contextmanager
def win32_realtime_context(affinity_mask: int, time_critical: bool = False):
    """
    Context manager that lowers Windows scheduler timer resolution to 1.0ms,
    pins the current process to `affinity_mask`, and elevates process/thread priority.
    Automatically restores timer period on exit.
    """
    if IS_WINDOWS:
        winmm.timeBeginPeriod(1)
        proc = kernel32.GetCurrentProcess()
        thread = kernel32.GetCurrentThread()

        kernel32.SetProcessAffinityMask(proc, ctypes.c_size_t(affinity_mask))
        prio_class = REALTIME_PRIORITY_CLASS if time_critical else HIGH_PRIORITY_CLASS
        kernel32.SetPriorityClass(proc, prio_class)

        t_prio = THREAD_PRIORITY_TIME_CRITICAL if time_critical else THREAD_PRIORITY_HIGHEST
        kernel32.SetThreadPriority(thread, t_prio)

    try:
        yield
    finally:
        if IS_WINDOWS:
            proc = kernel32.GetCurrentProcess()
            thread = kernel32.GetCurrentThread()
            kernel32.SetThreadPriority(thread, THREAD_PRIORITY_NORMAL)
            kernel32.SetPriorityClass(proc, NORMAL_PRIORITY_CLASS)
            winmm.timeEndPeriod(1)


def pin_worker(affinity_mask: int, time_critical: bool = False):
    """Decorator version of `win32_realtime_context` for worker entrypoints."""
    def decorator(func: Callable):
        def wrapper(*args, **kwargs):
            with win32_realtime_context(affinity_mask=affinity_mask, time_critical=time_critical):
                return func(*args, **kwargs)
        return wrapper
    return decorator
