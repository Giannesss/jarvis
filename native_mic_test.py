import ctypes
from ctypes import wintypes
import time

winmm = ctypes.windll.winmm

class WAVEFORMATEX(ctypes.Structure):
    _fields_ = [
        ("wFormatTag", wintypes.WORD),
        ("nChannels", wintypes.WORD),
        ("nSamplesPerSec", wintypes.DWORD),
        ("nAvgBytesPerSec", wintypes.DWORD),
        ("nBlockAlign", wintypes.WORD),
        ("wBitsPerSample", wintypes.WORD),
        ("cbSize", wintypes.WORD),
    ]

class WAVEHDR(ctypes.Structure):
    _fields_ = [
        ("lpData", ctypes.c_void_p),
        ("dwBufferLength", wintypes.DWORD),
        ("dwBytesRecorded", wintypes.DWORD),
        ("dwUser", ctypes.c_void_p),
        ("dwFlags", wintypes.DWORD),
        ("dwLoops", wintypes.DWORD),
        ("lpNext", ctypes.c_void_p),
        ("reserved", ctypes.c_void_p),
    ]

fmt = WAVEFORMATEX(
    1,
    1,
    44100,
    44100 * 2,
    2,
    16,
    0
)

handle = wintypes.HANDLE()

result = winmm.waveInOpen(
    ctypes.byref(handle),
    0,
    ctypes.byref(fmt),
    0,
    0,
    0
)

print("waveInOpen result:", result)

if result == 0:
    print("SUCCESS: Windows native microphone opened.")
    winmm.waveInClose(handle)
else:
    print("FAILED")