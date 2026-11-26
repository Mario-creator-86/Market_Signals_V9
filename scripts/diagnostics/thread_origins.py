"""Which code started the threads of a process? Groups its threads by the module of their start address.

Read-only, Windows only. Run: python scripts/diagnostics/thread_origins.py <pid> [out.json]

Take two readings some minutes apart: the module whose count grows is the leak.
On 2026-11-24 the server (run_server.py) had gone from 114 threads to 5,112 in
12 hours; the growing group was libiomp5md.dll, torch's OpenMP runtime, which
starts a team of worker threads for every new thread that runs the sentiment
model (fixed by running the model on one thread, core/news_v9/finbert.py).
py-spy shows Python threads only and saw 24 of them.
"""
import ctypes
import ctypes.wintypes as wt
import json
import sys
from collections import Counter

pid = int(sys.argv[1])
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
ntdll = ctypes.WinDLL("ntdll")
psapi = ctypes.WinDLL("psapi", use_last_error=True)

TH32CS_SNAPTHREAD = 0x4
THREAD_QUERY_INFORMATION = 0x40
PROCESS_QUERY_INFORMATION, PROCESS_VM_READ = 0x400, 0x10


class THREADENTRY32(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ThreadID", wt.DWORD),
                ("th32OwnerProcessID", wt.DWORD), ("tpBasePri", wt.LONG), ("tpDeltaPri", wt.LONG),
                ("dwFlags", wt.DWORD)]


class MODULEINFO(ctypes.Structure):
    _fields_ = [("lpBaseOfDll", ctypes.c_void_p), ("SizeOfImage", wt.DWORD), ("EntryPoint", ctypes.c_void_p)]


kernel32.CreateToolhelp32Snapshot.restype = wt.HANDLE
kernel32.OpenThread.restype = wt.HANDLE
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.CloseHandle.argtypes = [wt.HANDLE]
psapi.EnumProcessModulesEx.argtypes = [wt.HANDLE, ctypes.POINTER(wt.HMODULE), wt.DWORD, ctypes.POINTER(wt.DWORD), wt.DWORD]
psapi.GetModuleInformation.argtypes = [wt.HANDLE, wt.HMODULE, ctypes.POINTER(MODULEINFO), wt.DWORD]
psapi.GetModuleBaseNameW.argtypes = [wt.HANDLE, wt.HMODULE, wt.LPWSTR, wt.DWORD]
ntdll.NtQueryInformationThread.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p]

process = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
if not process:
    raise SystemExit(f"cannot open process {pid}: error {ctypes.get_last_error()}")

modules = []
handles = (wt.HMODULE * 4096)()
needed = wt.DWORD()
if psapi.EnumProcessModulesEx(process, handles, ctypes.sizeof(handles), ctypes.byref(needed), 0x03):
    for index in range(min(4096, needed.value // ctypes.sizeof(wt.HMODULE))):
        info = MODULEINFO()
        name = ctypes.create_unicode_buffer(260)
        module = handles[index]
        if psapi.GetModuleInformation(process, module, ctypes.byref(info), ctypes.sizeof(info)):
            psapi.GetModuleBaseNameW(process, module, name, 260)
            modules.append((info.lpBaseOfDll or 0, (info.lpBaseOfDll or 0) + info.SizeOfImage, name.value))
modules.sort()


def module_of(address):
    for start, end, name in modules:
        if start <= address < end:
            return name, address - start
    return "?", address


snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
entry = THREADENTRY32()
entry.dwSize = ctypes.sizeof(entry)
by_module, by_address, total, unreadable = Counter(), Counter(), 0, 0
more = kernel32.Thread32First(snapshot, ctypes.byref(entry))
while more:
    if entry.th32OwnerProcessID == pid:
        total += 1
        thread = kernel32.OpenThread(THREAD_QUERY_INFORMATION, False, entry.th32ThreadID)
        if thread:
            start = ctypes.c_void_p()
            status = ntdll.NtQueryInformationThread(thread, 9, ctypes.byref(start), ctypes.sizeof(start), None)
            kernel32.CloseHandle(thread)
            if status == 0:
                name, offset = module_of(start.value or 0)
                by_module[name] += 1
                by_address[f"{name}+0x{offset:x}"] += 1
            else:
                unreadable += 1
        else:
            unreadable += 1
    more = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
kernel32.CloseHandle(snapshot)
kernel32.CloseHandle(process)

print(f"pid {pid}: {total} threads, {unreadable} unreadable, {len(modules)} modules")
for name, count in by_module.most_common(8):
    print(f"  {count:5d}  started in {name}")
for name, count in by_address.most_common(8):
    print(f"  {count:5d}  at {name}")
if len(sys.argv) > 2:
    json.dump({"total": total, "by_module": dict(by_module), "by_address": dict(by_address)},
              open(sys.argv[2], "w", encoding="utf-8"), indent=1)
