"""Windows WASAPI capture, including per-application loopback.

PortAudio (and therefore sounddevice) cannot do loopback capture on Windows, so
this module talks to WASAPI directly through ctypes. Two modes:

**Endpoint loopback** -- the classic trick: activate ``IAudioClient`` on a
*render* endpoint with ``AUDCLNT_STREAMFLAGS_LOOPBACK`` and you get everything
that device is playing.

**Process loopback** -- Windows 10 2004 added
``ActivateAudioInterfaceAsync`` with ``AUDIOCLIENT_ACTIVATION_PARAMS``, which
grabs the audio of one process tree and nothing else. That is what makes
"capture only the game" work without routing the rest of the desktop through
the mic. It needs a COM completion handler, so there is a hand-rolled vtable
further down -- ctypes cannot implement a COM interface on its own.

If any of this is unavailable (older Windows, odd driver), the caller falls back
to endpoint loopback and then to nothing, and the app keeps running.
"""
from __future__ import annotations

import ctypes
import threading
from ctypes import POINTER, byref, c_void_p, wintypes

import numpy as np

from .. import log

_log = log.get("wasapi")

ole32 = ctypes.windll.ole32
kernel32 = ctypes.windll.kernel32

# --------------------------------------------------------------------------- constants
S_OK = 0
E_NOINTERFACE = 0x80004002
COINIT_MULTITHREADED = 0x0
COINIT_APARTMENTTHREADED = 0x2
RPC_E_CHANGED_MODE = 0x80010106

CLSCTX_ALL = 23

eRender, eCapture, eAll = 0, 1, 2
eConsole, eMultimedia, eCommunications = 0, 1, 2
DEVICE_STATE_ACTIVE = 0x1

AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000
AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM = 0x80000000
AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY = 0x08000000
AUDCLNT_BUFFERFLAGS_SILENT = 0x2

WAVE_FORMAT_PCM = 1
WAVE_FORMAT_IEEE_FLOAT = 3
WAVE_FORMAT_EXTENSIBLE = 0xFFFE

VT_LPWSTR = 31
VT_BLOB = 0x41

REFTIMES_PER_SEC = 10_000_000
REFTIMES_PER_MS = 10_000

VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK = "VAD\\Process_Loopback"

AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE = 0
PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE = 1


# --------------------------------------------------------------------------- structs
class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    def __init__(self, text: str | None = None):
        super().__init__()
        if text:
            hr = ole32.CLSIDFromString(ctypes.c_wchar_p(text), byref(self))
            if hr != S_OK:
                raise ValueError(f"bad GUID {text!r}")

    def __eq__(self, other) -> bool:
        if not isinstance(other, GUID):
            return NotImplemented
        return bytes(memoryview(self)) == bytes(memoryview(other))  # type: ignore[arg-type]

    def __hash__(self):
        return hash(bytes(memoryview(self)))  # type: ignore[arg-type]


class PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", GUID), ("pid", wintypes.DWORD)]


class _PropBlob(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.ULONG), ("pBlobData", c_void_p)]


class _PropValue(ctypes.Union):
    """The PROPVARIANT payload.

    Only the two members we use are declared. Note the offsets differ: a string
    pointer sits directly at the start of the union, while a BLOB puts its size
    first and the pointer eight bytes in. Getting this wrong silently reads the
    wrong field, which is how device names came back empty the first time.
    """

    _fields_ = [
        ("pwszVal", c_void_p),
        ("blob", _PropBlob),
        ("_raw", ctypes.c_byte * 16),
    ]


class PROPVARIANT(ctypes.Structure):
    _fields_ = [
        ("vt", wintypes.WORD),
        ("wReserved1", wintypes.WORD),
        ("wReserved2", wintypes.WORD),
        ("wReserved3", wintypes.WORD),
        ("value", _PropValue),
    ]


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


class WAVEFORMATEXTENSIBLE(ctypes.Structure):
    _fields_ = [
        ("Format", WAVEFORMATEX),
        ("wValidBitsPerSample", wintypes.WORD),
        ("dwChannelMask", wintypes.DWORD),
        ("SubFormat", GUID),
    ]


class AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS(ctypes.Structure):
    _fields_ = [
        ("TargetProcessId", wintypes.DWORD),
        ("ProcessLoopbackMode", wintypes.DWORD),
    ]


class AUDIOCLIENT_ACTIVATION_PARAMS(ctypes.Structure):
    _fields_ = [
        ("ActivationType", wintypes.DWORD),
        ("ProcessLoopbackParams", AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS),
    ]


# --------------------------------------------------------------------------- ids
CLSID_MMDeviceEnumerator = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
IID_IMMDeviceEnumerator = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
IID_IAudioClient = GUID("{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}")
IID_IAudioCaptureClient = GUID("{C8ADBD64-E71E-48A0-A4DE-185C395CD317}")
IID_IUnknown = GUID("{00000000-0000-0000-C000-000000000046}")
IID_IAgileObject = GUID("{94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90}")
IID_IActivateAudioInterfaceCompletionHandler = GUID(
    "{41D949AB-9862-444A-80F6-C261334DA5EB}")
KSDATAFORMAT_SUBTYPE_PCM = GUID("{00000001-0000-0010-8000-00AA00389B71}")
KSDATAFORMAT_SUBTYPE_IEEE_FLOAT = GUID("{00000003-0000-0010-8000-00AA00389B71}")

PKEY_Device_FriendlyName = PROPERTYKEY(
    GUID("{A45C254E-DF1C-4EFD-8020-67D146A850E0}"), 14)


# --------------------------------------------------------------------------- com glue
def hresult(hr) -> int:
    """Normalise an HRESULT to unsigned.

    ctypes hands back a signed 32-bit int, so 0x80010106 arrives as
    -2147155706 and every comparison against a hex constant silently fails.
    """
    return int(hr or 0) & 0xFFFFFFFF


class ComError(RuntimeError):
    def __init__(self, hr: int, what: str = ""):
        self.hr = hresult(hr)
        super().__init__(f"{what} failed: 0x{self.hr:08X}")


def check(hr: int, what: str = "call") -> None:
    if hresult(hr) != S_OK:
        raise ComError(hr, what)


def _method(ptr: c_void_p, index: int, restype, *argtypes):
    """Bind vtable slot ``index`` on a raw COM pointer."""
    vtable = ctypes.cast(ptr, POINTER(POINTER(c_void_p)))[0]
    proto = ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)
    return proto(vtable[index])


def _release(ptr: c_void_p | None) -> None:
    if ptr:
        try:
            _method(ptr, 2, wintypes.ULONG)(ptr)
        except Exception:
            pass


class _ComInit:
    """Scoped CoInitializeEx that tolerates an already-initialised thread."""

    def __init__(self, mta: bool = True):
        self.mode = COINIT_MULTITHREADED if mta else COINIT_APARTMENTTHREADED
        self._did = False

    def __enter__(self):
        hr = hresult(ole32.CoInitializeEx(None, self.mode))
        # S_FALSE means this thread was already initialised in the mode we
        # asked for; RPC_E_CHANGED_MODE means it is in the *other* apartment
        # already (PortAudio does this on the main thread). Both are fine to
        # keep working in - we just must not call CoUninitialize afterwards.
        self._did = hr == S_OK
        if hr not in (S_OK, 1, RPC_E_CHANGED_MODE):
            raise ComError(hr, "CoInitializeEx")
        return self

    def __exit__(self, *exc):
        if self._did:
            try:
                ole32.CoUninitialize()
            except Exception:
                pass
        return False


# --------------------------------------------------------------------------- formats
def _describe_format(pwfx) -> tuple[int, int, str]:
    """(samplerate, channels, dtype-tag) for a WAVEFORMATEX pointer."""
    wfx = ctypes.cast(pwfx, POINTER(WAVEFORMATEX))[0]
    sr = int(wfx.nSamplesPerSec)
    ch = int(wfx.nChannels)
    bits = int(wfx.wBitsPerSample)
    tag = wfx.wFormatTag

    if tag == WAVE_FORMAT_EXTENSIBLE:
        ext = ctypes.cast(pwfx, POINTER(WAVEFORMATEXTENSIBLE))[0]
        if ext.SubFormat == KSDATAFORMAT_SUBTYPE_IEEE_FLOAT:
            tag = WAVE_FORMAT_IEEE_FLOAT
        else:
            tag = WAVE_FORMAT_PCM

    if tag == WAVE_FORMAT_IEEE_FLOAT:
        kind = "f32" if bits == 32 else "f64"
    elif bits == 16:
        kind = "i16"
    elif bits == 32:
        kind = "i32"
    elif bits == 24:
        kind = "i24"
    elif bits == 8:
        kind = "u8"
    else:
        kind = "i16"
    return sr, ch, kind


def _decode(raw: bytes, kind: str, channels: int) -> np.ndarray:
    """Interleaved device bytes -> (frames, channels) float32 in [-1, 1]."""
    if kind == "f32":
        data = np.frombuffer(raw, dtype=np.float32)
    elif kind == "f64":
        data = np.frombuffer(raw, dtype=np.float64).astype(np.float32)
    elif kind == "i16":
        data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif kind == "i32":
        data = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
    elif kind == "u8":
        data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif kind == "i24":
        b = np.frombuffer(raw, dtype=np.uint8)
        usable = (b.size // 3) * 3
        b = b[:usable].reshape(-1, 3)
        vals = (b[:, 0].astype(np.int32)
                | (b[:, 1].astype(np.int32) << 8)
                | (b[:, 2].astype(np.int32) << 16))
        vals = np.where(vals & 0x800000, vals - 0x1000000, vals)
        data = vals.astype(np.float32) / 8388608.0
    else:
        data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

    if channels > 1:
        usable = (data.size // channels) * channels
        return data[:usable].reshape(-1, channels)
    return data.reshape(-1, 1)


# --------------------------------------------------------------------------- devices
class _Enumerator:
    def __init__(self):
        self.ptr = c_void_p()
        check(ole32.CoCreateInstance(byref(CLSID_MMDeviceEnumerator), None,
                                     CLSCTX_ALL, byref(IID_IMMDeviceEnumerator),
                                     byref(self.ptr)), "CoCreateInstance(MMDeviceEnumerator)")

    def close(self):
        _release(self.ptr)
        self.ptr = c_void_p()

    def default_endpoint(self, flow: int = eRender, role: int = eConsole) -> c_void_p:
        dev = c_void_p()
        fn = _method(self.ptr, 4, ctypes.HRESULT, wintypes.DWORD, wintypes.DWORD,
                     POINTER(c_void_p))
        check(fn(self.ptr, flow, role, byref(dev)), "GetDefaultAudioEndpoint")
        return dev

    def device_by_id(self, dev_id: str) -> c_void_p:
        dev = c_void_p()
        fn = _method(self.ptr, 5, ctypes.HRESULT, ctypes.c_wchar_p, POINTER(c_void_p))
        check(fn(self.ptr, dev_id, byref(dev)), "GetDevice")
        return dev

    def endpoints(self, flow: int = eRender) -> list[tuple[str, str]]:
        """[(device_id, friendly_name)] for active endpoints."""
        coll = c_void_p()
        fn = _method(self.ptr, 3, ctypes.HRESULT, wintypes.DWORD, wintypes.DWORD,
                     POINTER(c_void_p))
        check(fn(self.ptr, flow, DEVICE_STATE_ACTIVE, byref(coll)), "EnumAudioEndpoints")
        out: list[tuple[str, str]] = []
        try:
            count = wintypes.UINT()
            check(_method(coll, 3, ctypes.HRESULT, POINTER(wintypes.UINT))(
                coll, byref(count)), "GetCount")
            for i in range(count.value):
                dev = c_void_p()
                if hresult(_method(coll, 4, ctypes.HRESULT, wintypes.UINT,
                                   POINTER(c_void_p))(
                        coll, i, byref(dev))) != S_OK:
                    continue
                try:
                    out.append((device_id(dev), friendly_name(dev)))
                finally:
                    _release(dev)
        finally:
            _release(coll)
        return out


def device_id(dev: c_void_p) -> str:
    pid = ctypes.c_wchar_p()
    if hresult(_method(dev, 5, ctypes.HRESULT, POINTER(ctypes.c_wchar_p))(
            dev, byref(pid))) != S_OK:
        return ""
    try:
        return pid.value or ""
    finally:
        ole32.CoTaskMemFree(pid)


def friendly_name(dev: c_void_p) -> str:
    store = c_void_p()
    if hresult(_method(dev, 4, ctypes.HRESULT, wintypes.DWORD, POINTER(c_void_p))(
            dev, 0, byref(store))) != S_OK:  # STGM_READ
        return ""
    try:
        pv = PROPVARIANT()
        if hresult(_method(store, 5, ctypes.HRESULT, POINTER(PROPERTYKEY),
                            POINTER(PROPVARIANT))(
                store, byref(PKEY_Device_FriendlyName), byref(pv))) != S_OK:
            return ""
        try:
            if pv.vt == VT_LPWSTR and pv.value.pwszVal:
                return ctypes.cast(pv.value.pwszVal, ctypes.c_wchar_p).value or ""
            return ""
        finally:
            try:
                ctypes.windll.ole32.PropVariantClear(byref(pv))
            except Exception:
                pass
    finally:
        _release(store)


def list_render_endpoints() -> list[tuple[str, str]]:
    """Playback devices available for desktop loopback capture."""
    try:
        with _ComInit():
            en = _Enumerator()
            try:
                return en.endpoints(eRender)
            finally:
                en.close()
    except Exception as exc:
        _log.warning("cannot enumerate render endpoints: %s", exc)
        return []


def default_render_name() -> str:
    try:
        with _ComInit():
            en = _Enumerator()
            try:
                dev = en.default_endpoint(eRender, eConsole)
                try:
                    return friendly_name(dev)
                finally:
                    _release(dev)
            finally:
                en.close()
    except Exception:
        return ""


# --------------------------------------------------------------------------- async activation
_QI = ctypes.WINFUNCTYPE(ctypes.HRESULT, c_void_p, POINTER(GUID), POINTER(c_void_p))
_ADDREF = ctypes.WINFUNCTYPE(wintypes.ULONG, c_void_p)
_ACTIVATE_COMPLETED = ctypes.WINFUNCTYPE(ctypes.HRESULT, c_void_p, c_void_p)


class _HandlerVtbl(ctypes.Structure):
    _fields_ = [
        ("QueryInterface", _QI),
        ("AddRef", _ADDREF),
        ("Release", _ADDREF),
        ("ActivateCompleted", _ACTIVATE_COMPLETED),
    ]


class _HandlerObj(ctypes.Structure):
    _fields_ = [("lpVtbl", POINTER(_HandlerVtbl))]


class _CompletionHandler:
    """A COM object implemented in Python, for ActivateAudioInterfaceAsync.

    ctypes has no way to implement a COM interface, so the vtable is built by
    hand. Every callback and structure must stay referenced from Python for as
    long as WASAPI might call it, hence all the attributes.
    """

    def __init__(self):
        self.done = threading.Event()
        self._refs = 1

        self._qi = _QI(self._query_interface)
        self._addref = _ADDREF(self._add_ref)
        self._rel = _ADDREF(self._release_ref)
        self._completed = _ACTIVATE_COMPLETED(self._activate_completed)

        self._vtbl = _HandlerVtbl(self._qi, self._addref, self._rel, self._completed)
        self._obj = _HandlerObj(ctypes.pointer(self._vtbl))
        self.ptr = ctypes.cast(ctypes.pointer(self._obj), c_void_p)

    # --- IUnknown
    def _query_interface(self, this, riid, ppv):
        if not ppv:
            return -2147467261  # E_POINTER
        want = riid[0]
        if want in (IID_IUnknown, IID_IActivateAudioInterfaceCompletionHandler,
                    IID_IAgileObject):
            ppv[0] = this
            self._refs += 1
            return S_OK
        ppv[0] = None
        return E_NOINTERFACE

    def _add_ref(self, this):
        self._refs += 1
        return self._refs

    def _release_ref(self, this):
        self._refs -= 1
        return max(self._refs, 0)

    def _activate_completed(self, this, operation):
        self.done.set()
        return S_OK


def _activate_process_loopback(pid: int, exclude: bool, wfx: WAVEFORMATEX,
                               timeout_s: float = 5.0) -> c_void_p:
    """IAudioClient bound to one process tree. Raises on failure."""
    try:
        mmdevapi = ctypes.windll.mmdevapi
        activate = mmdevapi.ActivateAudioInterfaceAsync
    except (AttributeError, OSError) as exc:
        raise RuntimeError("ActivateAudioInterfaceAsync unavailable "
                           "(needs Windows 10 build 20348 / 2004+)") from exc

    activate.restype = ctypes.HRESULT
    activate.argtypes = [ctypes.c_wchar_p, POINTER(GUID), POINTER(PROPVARIANT),
                         c_void_p, POINTER(c_void_p)]

    params = AUDIOCLIENT_ACTIVATION_PARAMS()
    params.ActivationType = AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK
    params.ProcessLoopbackParams.TargetProcessId = int(pid)
    params.ProcessLoopbackParams.ProcessLoopbackMode = (
        PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE if exclude
        else PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE)

    pv = PROPVARIANT()
    pv.vt = VT_BLOB
    pv.value.blob.cbSize = ctypes.sizeof(params)
    pv.value.blob.pBlobData = ctypes.cast(ctypes.pointer(params), c_void_p)

    handler = _CompletionHandler()
    op = c_void_p()
    hr = hresult(activate(VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK,
                          byref(IID_IAudioClient), byref(pv), handler.ptr,
                          byref(op)))
    if hr != S_OK:
        raise ComError(hr, "ActivateAudioInterfaceAsync")

    try:
        if not handler.done.wait(timeout_s):
            raise RuntimeError("process loopback activation timed out")
        activate_hr = ctypes.HRESULT()
        iface = c_void_p()
        get_result = _method(op, 3, ctypes.HRESULT, POINTER(ctypes.HRESULT),
                             POINTER(c_void_p))
        check(get_result(op, byref(activate_hr), byref(iface)), "GetActivateResult")
        hr_value = hresult(activate_hr.value)
        if hr_value != S_OK:
            raise ComError(hr_value, "process loopback activation")
        if not iface:
            raise RuntimeError("process loopback returned a null interface")
        return iface
    finally:
        _release(op)
        # Keep the handler alive until WASAPI is certainly done with it.
        _HANDLER_KEEPALIVE.append(handler)
        del _HANDLER_KEEPALIVE[:-8]


_HANDLER_KEEPALIVE: list[_CompletionHandler] = []


def _make_format(samplerate: int, channels: int, float32: bool) -> WAVEFORMATEX:
    wfx = WAVEFORMATEX()
    wfx.wFormatTag = WAVE_FORMAT_IEEE_FLOAT if float32 else WAVE_FORMAT_PCM
    wfx.nChannels = channels
    wfx.nSamplesPerSec = samplerate
    wfx.wBitsPerSample = 32 if float32 else 16
    wfx.nBlockAlign = channels * wfx.wBitsPerSample // 8
    wfx.nAvgBytesPerSec = samplerate * wfx.nBlockAlign
    wfx.cbSize = 0
    return wfx


# --------------------------------------------------------------------------- capture
class WasapiCapture:
    """Background WASAPI capture that pushes float32 frames to a callback.

    ``on_audio(frames, samplerate)`` is called from the capture thread with a
    ``(n, channels)`` float32 array. Keep it quick -- push into a ring buffer
    and return.
    """

    def __init__(self, on_audio, *, mode: str = "desktop", pid: int = 0,
                 exclude: bool = False, device_id: str = "",
                 samplerate: int = 48000, channels: int = 2,
                 buffer_ms: float = 20.0):
        self.on_audio = on_audio
        self.mode = mode
        self.pid = int(pid)
        self.exclude = bool(exclude)
        self.device_id = device_id
        self.samplerate = int(samplerate)
        self.channels = int(channels)
        self.buffer_ms = float(buffer_ms)

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._started = threading.Event()
        self.error: str = ""
        self.actual_samplerate = self.samplerate
        self.actual_channels = self.channels
        self.frames_captured = 0
        self.running = False

    # ------------------------------------------------------------- lifecycle
    def start(self, timeout_s: float = 6.0) -> bool:
        if self.running:
            return True
        self._stop.clear()
        self._started.clear()
        self.error = ""
        self._thread = threading.Thread(target=self._run, name="wasapi-capture",
                                        daemon=True)
        self._thread.start()
        ok = self._started.wait(timeout_s)
        if not ok and not self.error:
            self.error = "capture did not start in time"
        return self.running

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=2.0)
        self._thread = None
        self.running = False

    # ---------------------------------------------------------------- worker
    def _run(self) -> None:
        client = c_void_p()
        capture = c_void_p()
        event_handle = None
        enumerator = None
        device = None
        mix_format_ptr = None
        try:
            with _ComInit(mta=True):
                if self.mode in ("process", "exclude"):
                    client, kind, sr, ch = self._open_process_client()
                else:
                    (client, kind, sr, ch, enumerator, device,
                     mix_format_ptr) = self._open_endpoint_client()

                self.actual_samplerate = sr
                self.actual_channels = ch

                event_handle = kernel32.CreateEventW(None, False, False, None)
                set_event = _method(client, 13, ctypes.HRESULT, wintypes.HANDLE)
                if hresult(set_event(client, event_handle)) != S_OK:
                    kernel32.CloseHandle(event_handle)
                    event_handle = None

                svc = _method(client, 14, ctypes.HRESULT, POINTER(GUID),
                              POINTER(c_void_p))
                check(svc(client, byref(IID_IAudioCaptureClient), byref(capture)),
                      "GetService(IAudioCaptureClient)")
                check(_method(client, 10, ctypes.HRESULT)(client), "Start")

                self.running = True
                self._started.set()
                _log.info("capture running: mode=%s pid=%s %d Hz %d ch %s",
                          self.mode, self.pid or "-", sr, ch, kind)
                self._pump(capture, kind, ch, sr, event_handle)

                try:
                    _method(client, 11, ctypes.HRESULT)(client)  # Stop
                except Exception:
                    pass
        except Exception as exc:
            self.error = str(exc)
            _log.error("capture failed (%s): %s", self.mode, exc)
        finally:
            self.running = False
            self._started.set()
            if event_handle:
                kernel32.CloseHandle(event_handle)
            _release(capture)
            _release(client)
            if mix_format_ptr:
                ole32.CoTaskMemFree(mix_format_ptr)
            if device:
                _release(device)
            if enumerator:
                enumerator.close()

    # --------------------------------------------------------------- opening
    def _open_process_client(self):
        # mode is what the caller actually asked for; the flag is a convenience
        # alias. Letting the two disagree silently captured the whole desktop.
        exclude = self.exclude or self.mode == "exclude"
        attempts = [
            (48000, 2, True), (48000, 2, False),
            (44100, 2, True), (44100, 2, False),
            (48000, 1, False),
        ]
        last: Exception | None = None
        for sr, ch, use_float in attempts:
            wfx = _make_format(sr, ch, use_float)
            try:
                client = _activate_process_loopback(self.pid, exclude, wfx)
            except Exception as exc:
                last = exc
                break  # activation itself failed; a different format will not help
            flags = AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK
            duration = int(self.buffer_ms * REFTIMES_PER_MS)
            init = _method(client, 3, ctypes.HRESULT, wintypes.DWORD, wintypes.DWORD,
                           ctypes.c_longlong, ctypes.c_longlong,
                           POINTER(WAVEFORMATEX), POINTER(GUID))
            ok = False
            for periodicity in (0, AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM):
                hr = hresult(init(client, AUDCLNT_SHAREMODE_SHARED, flags,
                                  duration, periodicity, byref(wfx), None))
                if hr == S_OK:
                    ok = True
                    break
                last = ComError(hr, f"Initialize({sr}/{ch}/"
                                    f"{'f32' if use_float else 'i16'})")
            if ok:
                return client, ("f32" if use_float else "i16"), sr, ch
            _release(client)
        raise last or RuntimeError("process loopback could not be initialised")

    def _open_endpoint_client(self):
        enumerator = _Enumerator()
        if self.device_id:
            device = enumerator.device_by_id(self.device_id)
        else:
            device = enumerator.default_endpoint(eRender, eConsole)

        client = c_void_p()
        act = _method(device, 3, ctypes.HRESULT, POINTER(GUID), wintypes.DWORD,
                      POINTER(PROPVARIANT), POINTER(c_void_p))
        check(act(device, byref(IID_IAudioClient), CLSCTX_ALL, None, byref(client)),
              "IMMDevice::Activate")

        mix = c_void_p()
        check(_method(client, 8, ctypes.HRESULT, POINTER(c_void_p))(client, byref(mix)),
              "GetMixFormat")
        sr, ch, kind = _describe_format(mix)

        flags = AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK
        duration = int(self.buffer_ms * REFTIMES_PER_MS)
        init = _method(client, 3, ctypes.HRESULT, wintypes.DWORD, wintypes.DWORD,
                       ctypes.c_longlong, ctypes.c_longlong,
                       c_void_p, POINTER(GUID))
        hr = hresult(init(client, AUDCLNT_SHAREMODE_SHARED, flags, duration, 0,
                          mix, None))
        if hr != S_OK:
            # Some drivers reject event-driven loopback; polling works everywhere.
            hr = hresult(init(client, AUDCLNT_SHAREMODE_SHARED,
                              AUDCLNT_STREAMFLAGS_LOOPBACK, duration, 0, mix, None))
            check(hr, "Initialize(loopback)")
        return client, kind, sr, ch, enumerator, device, mix

    # ----------------------------------------------------------------- pump
    def _pump(self, capture: c_void_p, kind: str, channels: int, sr: int,
              event_handle) -> None:
        get_next = _method(capture, 5, ctypes.HRESULT, POINTER(wintypes.UINT))
        get_buffer = _method(capture, 3, ctypes.HRESULT, POINTER(c_void_p),
                             POINTER(wintypes.UINT), POINTER(wintypes.DWORD),
                             POINTER(ctypes.c_ulonglong), POINTER(ctypes.c_ulonglong))
        release_buffer = _method(capture, 4, ctypes.HRESULT, wintypes.UINT)

        bytes_per_frame = {"f32": 4, "f64": 8, "i16": 2, "i32": 4,
                           "i24": 3, "u8": 1}[kind] * channels
        wait_ms = max(2, int(self.buffer_ms / 2))
        idle_sleep = wait_ms / 1000.0

        while not self._stop.is_set():
            if event_handle:
                kernel32.WaitForSingleObject(event_handle, wait_ms)
            else:
                self._stop.wait(idle_sleep)

            while not self._stop.is_set():
                packet = wintypes.UINT()
                if hresult(get_next(capture, byref(packet))) != S_OK:
                    break
                if packet.value == 0:
                    break

                data_ptr = c_void_p()
                frames = wintypes.UINT()
                flags = wintypes.DWORD()
                hr = hresult(get_buffer(capture, byref(data_ptr), byref(frames),
                                        byref(flags), None, None))
                if hr != S_OK:
                    break
                try:
                    n = int(frames.value)
                    if n <= 0:
                        continue
                    if flags.value & AUDCLNT_BUFFERFLAGS_SILENT or not data_ptr:
                        block = np.zeros((n, channels), dtype=np.float32)
                    else:
                        raw = ctypes.string_at(data_ptr, n * bytes_per_frame)
                        block = _decode(raw, kind, channels)
                    self.frames_captured += n
                    try:
                        self.on_audio(block, sr)
                    except Exception:
                        _log.exception("capture consumer raised")
                finally:
                    release_buffer(capture, frames)


def is_process_loopback_supported() -> bool:
    """True when this Windows build exposes the per-application capture API."""
    try:
        return hasattr(ctypes.windll.mmdevapi, "ActivateAudioInterfaceAsync")
    except Exception:
        return False
