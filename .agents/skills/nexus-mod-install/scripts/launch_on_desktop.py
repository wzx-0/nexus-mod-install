import ctypes
import sys
from ctypes import wintypes

class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(wintypes.BYTE)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]

class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]

def launch_on_user_desktop(cmd_line: str, cwd: str = None):
    si = STARTUPINFOW()
    si.cb = ctypes.sizeof(si)
    si.lpDesktop = "WinSta0\\Default"
    si.dwFlags = 0x00000001  # STARTF_USESHOWWINDOW
    si.wShowWindow = 3       # SW_SHOWMAXIMIZED
    pi = PROCESS_INFORMATION()

    buf = ctypes.create_unicode_buffer(cmd_line)
    ok = ctypes.windll.kernel32.CreateProcessW(
        None,
        buf,
        None,
        None,
        False,
        0x00000010,  # CREATE_NEW_CONSOLE / normal window
        None,
        cwd,
        ctypes.byref(si),
        ctypes.byref(pi)
    )
    if not ok:
        raise ctypes.WinError(ctypes.GetLastError())
    ctypes.windll.kernel32.CloseHandle(pi.hThread)
    ctypes.windll.kernel32.CloseHandle(pi.hProcess)
    return pi.dwProcessId

if __name__ == "__main__":
    cmd = " ".join(sys.argv[1:])
    pid = launch_on_user_desktop(cmd)
    print(f"Launched PID={pid} on WinSta0\\Default")
