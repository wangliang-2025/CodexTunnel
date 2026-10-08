"""Windows job scope: closing the owner kills only assigned descendants."""
import ctypes
import sys
from ctypes import wintypes


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ('ProcessTime', ctypes.c_longlong), ('JobTime', ctypes.c_longlong),
        ('LimitFlags', wintypes.DWORD), ('MinWorkingSet', ctypes.c_size_t),
        ('MaxWorkingSet', ctypes.c_size_t), ('ActiveLimit', wintypes.DWORD),
        ('Affinity', ctypes.c_size_t), ('Priority', wintypes.DWORD),
        ('Scheduling', wintypes.DWORD),
    ]


class _IOCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in
                ('ReadOps', 'WriteOps', 'OtherOps', 'ReadBytes', 'WriteBytes', 'OtherBytes')]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [('Basic', _BasicLimits), ('IO', _IOCounters),
                ('ProcessMemory', ctypes.c_size_t), ('JobMemory', ctypes.c_size_t),
                ('PeakProcess', ctypes.c_size_t), ('PeakJob', ctypes.c_size_t)]


class ProcessScope:
    def __init__(self, process):
        self.handle = None
        if sys.platform != 'win32':
            return
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        self.kernel = kernel
        handle = kernel.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self.handle = handle
        try:
            info = _ExtendedLimits()
            info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not kernel.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
                raise ctypes.WinError(ctypes.get_last_error())
            # Popen's real process handle avoids PID reuse and never opens an
            # unrelated process by a recycled numeric PID.
            if not kernel.AssignProcessToJobObject(handle, wintypes.HANDLE(int(process._handle))):
                error = ctypes.get_last_error()
                if process.poll() is None:
                    raise ctypes.WinError(error)
        except Exception:
            self.close()
            raise

    def close(self):
        if self.handle is not None:
            self.kernel.CloseHandle(self.handle)
            self.handle = None
