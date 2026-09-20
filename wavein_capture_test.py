import ctypes
import threading
import wave

from ctypes import (
    c_void_p, c_uint, c_uint32, c_uint16, c_size_t,
    POINTER, Structure, byref, WINFUNCTYPE
)

winmm = ctypes.WinDLL("winmm")

class WAVEFORMATEX(Structure):
    _fields_ = [
        ("wFormatTag", c_uint16),
        ("nChannels", c_uint16),
        ("nSamplesPerSec", c_uint32),
        ("nAvgBytesPerSec", c_uint32),
        ("nBlockAlign", c_uint16),
        ("wBitsPerSample", c_uint16),
        ("cbSize", c_uint16),
    ]

class WAVEHDR(Structure):
    _fields_ = [
        ("lpData", c_void_p),
        ("dwBufferLength", c_uint32),
        ("dwBytesRecorded", c_uint32),
        ("dwUser", c_size_t),
        ("dwFlags", c_uint32),
        ("dwLoops", c_uint32),
        ("lpNext", c_void_p),
        ("reserved", c_void_p),
    ]

class WAVEINCAPSW(Structure):
    _fields_ = [
        ("wMid", c_uint16),
        ("wPid", c_uint16),
        ("vDriverVersion", c_uint32),
        ("szPname", ctypes.c_wchar * 32),
        ("dwFormats", c_uint32),
        ("wChannels", c_uint16),
        ("wReserved1", c_uint16),
        ("dwSupport", c_uint32),
    ]

winmm.waveInGetNumDevs.restype = c_uint

winmm.waveInGetDevCapsW.argtypes = [
    c_uint, POINTER(WAVEINCAPSW), c_uint
]
winmm.waveInGetDevCapsW.restype = c_uint

winmm.waveInOpen.argtypes = [
    POINTER(c_void_p), c_uint, POINTER(WAVEFORMATEX),
    c_void_p, c_size_t, c_uint
]
winmm.waveInOpen.restype = c_uint

winmm.waveInPrepareHeader.argtypes = [
    c_void_p, POINTER(WAVEHDR), c_uint32
]
winmm.waveInPrepareHeader.restype = c_uint

winmm.waveInAddBuffer.argtypes = [
    c_void_p, POINTER(WAVEHDR), c_uint32
]
winmm.waveInAddBuffer.restype = c_uint

winmm.waveInStart.argtypes = [c_void_p]
winmm.waveInStart.restype = c_uint

winmm.waveInStop.argtypes = [c_void_p]
winmm.waveInStop.restype = c_uint

winmm.waveInReset.argtypes = [c_void_p]
winmm.waveInReset.restype = c_uint

winmm.waveInUnprepareHeader.argtypes = [
    c_void_p, POINTER(WAVEHDR), c_uint32
]
winmm.waveInUnprepareHeader.restype = c_uint

winmm.waveInClose.argtypes = [c_void_p]
winmm.waveInClose.restype = c_uint

CALLBACK_FUNCTION = 0x00030000
WIM_DATA = 0x3C0

done = threading.Event()

@WINFUNCTYPE(None, c_void_p, c_uint, c_size_t, c_size_t, c_size_t)
def callback(hwi, msg, instance, param1, param2):
    if msg == WIM_DATA:
        done.set()

def try_capture(device_index, sample_rate):
    print(f"\\nTRYING device={device_index}, rate={sample_rate}")

    fmt = WAVEFORMATEX(
        1,
        1,
        sample_rate,
        sample_rate * 2,
        2,
        16,
        0
    )

    handle = c_void_p()

    result = winmm.waveInOpen(
        byref(handle),
        device_index,
        byref(fmt),
        ctypes.cast(callback, c_void_p),
        0,
        CALLBACK_FUNCTION
    )

    print("waveInOpen result:", result)

    if result != 0:
        return False

    print("WAVEIN OPEN: OK")

    seconds = 3
    buffer_size = sample_rate * 2 * seconds
    buffer = ctypes.create_string_buffer(buffer_size)

    header = WAVEHDR()
    header.lpData = ctypes.cast(buffer, c_void_p)
    header.dwBufferLength = buffer_size

    result = winmm.waveInPrepareHeader(
        handle, byref(header), ctypes.sizeof(WAVEHDR)
    )
    print("Prepare result:", result)

    if result != 0:
        winmm.waveInClose(handle)
        return False

    result = winmm.waveInAddBuffer(
        handle, byref(header), ctypes.sizeof(WAVEHDR)
    )
    print("AddBuffer result:", result)

    if result != 0:
        winmm.waveInUnprepareHeader(
            handle, byref(header), ctypes.sizeof(WAVEHDR)
        )
        winmm.waveInClose(handle)
        return False

    result = winmm.waveInStart(handle)
    print("Start result:", result)

    if result != 0:
        winmm.waveInUnprepareHeader(
            handle, byref(header), ctypes.sizeof(WAVEHDR)
        )
        winmm.waveInClose(handle)
        return False

    print("RECORDING FOR 3 SECONDS...")
    done.wait(5)

    winmm.waveInStop(handle)
    winmm.waveInReset(handle)

    recorded = header.dwBytesRecorded
    print("BYTES RECORDED:", recorded)

    data = ctypes.string_at(buffer, recorded)

    winmm.waveInUnprepareHeader(
        handle, byref(header), ctypes.sizeof(WAVEHDR)
    )
    winmm.waveInClose(handle)

    if recorded > 0:
        with wave.open("wavein_test.wav", "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(data)

        samples = memoryview(data).cast("h")
        peak = max((abs(x) for x in samples), default=0)

        print("CAPTURE SUCCESS")
        print("PEAK:", peak)
        print("FILE: wavein_test.wav")
        return True

    print("NO AUDIO DATA")
    return False

num_devices = winmm.waveInGetNumDevs()
print("WAVE INPUT DEVICES:", num_devices)

for i in range(num_devices):
    caps = WAVEINCAPSW()
    result = winmm.waveInGetDevCapsW(
        i, byref(caps), ctypes.sizeof(WAVEINCAPSW)
    )
    print(f"DEVICE {i}: result={result}, name={caps.szPname}, channels={caps.wChannels}, formats=0x{caps.dwFormats:08X}")

for rate in (48000, 44100, 32000, 16000):
    if try_capture(0, rate):
        break
