"""Real CAEN DT5742B backend via ctypes over libCAENDigitizer.

STATUS: driven end to end on serial 53364 - open, identify, configure, arm,
software-trigger, read, decode (8 ch x 1024 DRS4-corrected floats), stop, close.
The call ORDER and the struct layouts are taken from CAENDigitizerType.h and
WaveDump.c. What is NOT verified is anything needing a real signal: waveform
correctness, where the trigger lands in the record, and the absolute 0 V
position of the DC-offset model - its span and sign are measured, the intercept
rests on the nominal spec.

Correction strategy: we use the library's built-in DRS4 correction
(LoadDRS4CorrectionData + EnableDRS4Correction) so DecodeEvent returns already
cell/time/peak-corrected float samples — no software correction port needed.
"""
from __future__ import annotations

import ctypes as ct
import os
import sys
import time

import numpy as np

from . import corrections
from .base import DigitizerBackend, Event, BoardInfo
from .. import constants as C
from .. import logsetup

log = logsetup.get("daq.caen")

MAX_X742_CHANNEL_SIZE = 9   # 8 channels + TR trace
MAX_X742_GROUP_SIZE = 4     # library max; DT5742B populates 2


class _Prof:
    """DAQ_PROFILE=1: where does the time per event actually go, on live
    triggers? Accumulates wall time per readout phase plus the ReadData batch
    sizes - the number no offline bench can see - and logs a breakdown every
    500 events. The overhead is a few perf_counter calls per event."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.t: dict[str, float] = {}
        self.events = 0
        self.reads = 0
        self.batch_max = 0
        self.bytes = 0
        self.empty_calls = 0
        self.empty_s = 0.0

    def add(self, key: str, dt: float) -> float:
        self.t[key] = self.t.get(key, 0.0) + dt
        return time.perf_counter()

    def empty(self, dt: float):
        self.empty_calls += 1
        self.empty_s += dt

    def batch(self, n: int, nbytes: int):
        self.reads += 1
        self.events += n
        self.bytes += nbytes
        self.batch_max = max(self.batch_max, n)
        if self.events >= 500:
            per = " ".join(f"{k}={v / self.events * 1000:.2f}"
                           for k, v in sorted(self.t.items()))
            log.info("profile: %d ev in %d reads (max batch %d, %.0f kB/ev); "
                     "ms/ev: %s; empty ReadData: %d calls, %.2f ms avg",
                     self.events, self.reads, self.batch_max,
                     self.bytes / self.events / 1024, per,
                     self.empty_calls,
                     self.empty_s / max(1, self.empty_calls) * 1000)
            self.reset()


_prof = _Prof() if os.environ.get("DAQ_PROFILE") == "1" else None

# --- CAEN_DGTZ enums we use (from CAENDigitizerType.h) ---
CAEN_DGTZ_Success = 0
# "not allowed for this module" - some getters simply do not exist on the x742
# (GetGroupTriggerThreshold and GetGroupSelfTrigger among them). Those settings
# are write-only here: we keep what we asked for, because nothing can confirm it.
CAEN_DGTZ_FunctionNotAllowed = -17
# A burst of register traffic can make the NEXT call come back CommError; an
# immediate retry succeeds. Reproduced on serial 53364: a 101-step post-trigger
# sweep makes the following SetDRS4SamplingFrequency return -1 whatever value it
# is given, and calling it again straight away returns 0. Retry once rather than
# report a failure the board did not really have.
CAEN_DGTZ_CommError = -1
CAEN_DGTZ_InvalidParam = -3
_ERROR_NAMES = {
    CAEN_DGTZ_CommError: "CommError (transient USB glitch; retried once already)",
    -2: "GenericError",
    CAEN_DGTZ_InvalidParam: "InvalidParam (the value is out of range for this call)",
    -5: "InvalidHandle",
    -9: "Timeout",
    CAEN_DGTZ_FunctionNotAllowed: "FunctionNotAllowed (unsupported on this model)",
}
ConnectionType_USB = 0
# CAEN_DGTZ_ConnectionType, CAENDigitizerType.h (needs CAENDigitizer >= 2.17;
# this machine runs 2.18.0). "a4818" is the A4818 USB 3.0 -> CONET adapter
# talking straight to the digitizer's optical port - the ~80 MB/s path past
# USB 2.0's ~1 kHz event ceiling. Its LinkNum argument is the adapter's PID
# (the number printed on the A4818's label, also its USB serial).
ConnectionType_OpticalLink = 1
ConnectionType_A4818 = 5
_LINK_TYPES = {"usb": ConnectionType_USB,
               "optical": ConnectionType_OpticalLink,
               "a4818": ConnectionType_A4818}


def _link_specs() -> list[tuple[int, int, str]]:
    """The connections to try, in order: (connection_type, link_num, label).

    From DAQ_LINK, a comma list of 'usb', 'optical[:num]' or 'a4818:<pid>' -
    e.g. "a4818:25001,usb" tries the optical adapter first and falls back to
    USB. Unset means plain USB, exactly the old behavior. An a4818 entry
    without its PID cannot be opened and is skipped with a log line saying
    where the PID is printed."""
    specs = []
    for part in os.environ.get("DAQ_LINK", "usb").split(","):
        part = part.strip().lower()
        if not part:
            continue
        kind, _, arg = part.partition(":")
        if kind not in _LINK_TYPES:
            log.warning("DAQ_LINK entry %r not understood - expected usb, "
                        "optical[:num] or a4818:<pid>; skipping it", part)
            continue
        if kind == "a4818" and not arg:
            log.warning("DAQ_LINK entry 'a4818' needs the adapter's PID "
                        "(printed on the A4818 label): a4818:<pid>; skipping it")
            continue
        try:
            num = int(arg) if arg else 0
        except ValueError:
            log.warning("DAQ_LINK entry %r: %r is not a number; skipping it",
                        part, arg)
            continue
        specs.append((_LINK_TYPES[kind], num, part))
    return specs or [(ConnectionType_USB, 0, "usb")]
AcqMode_SW_CONTROLLED = 0
TriggerMode_DISABLED = 0
TriggerMode_ACQ_ONLY = 1
TriggerMode_ACQ_AND_EXTOUT = 3
TriggerMode_EXTOUT_ONLY = 2
# DRS4 frequency enum values (0=5G,1=2.5G,2=1G,3=750M) — match our constants keys.

REG_ACQUISITION_STATUS = 0x8104   # read-only; used as a liveness probe
# Group n Status (0x1n88): bit[2] is the mezzanine DAC/SPI busy flag. UM5698
# warns (sec 1.9) that DAC-backed writes issued while it is set "will not run
# properly" - in practice they are silently dropped while the library still
# answers success. Seen live on serial 53364: a TR threshold write read back
# unchanged, with no error anywhere.
REG_GROUP_STATUS = 0x1088
GROUP_REG_STRIDE = 0x100
BIT_DAC_BUSY = 1 << 2
# Group configuration. GetFastTriggerDigitizing is broken on this board - it
# reports the fast-trigger MODE bit (2 when the mode is on, 0 when off) and
# ignores the digitizing flag entirely, so an "off" always read back as "on".
# The setter works fine; bit 11 here is the truth. Verified on serial 53364:
# mode off/dig off 0x0110, dig on 0x0910; mode on 0x1110 / 0x1910.
REG_GROUP_CONFIG = 0x8000
REG_GROUP_CONFIG_BITSET = 0x8004      # write-1-to-set companion of 0x8000
BIT_TR_DIGITIZE = 1 << 11
# UM5698 sec 1.15 bit[8] "Individual trigger": power-on default 0, and the
# manual says MUST BE 1 for the 742 to work properly. With it clear the board
# triggers happily and delivers header-only events - hundreds counted, all
# empty. Soft resets preserve a previously-set 1 (which is how earlier
# software's leftover state masked this for two days); a POWER cycle clears
# it. Set it at every arm.
BIT_INDIVIDUAL_TRIGGER = 1 << 8

# GPO (TRG-OUT) routing lives in the Front Panel I/O Control register; the
# CAENDigitizer API has no function for it, so this is the one setting done
# with raw register access. UM5698 sec 1.25 (0x811C): bits[17:16] select the
# source (00 trigger, 01 motherboard probes), bits[19:18] pick the probe
# (00 RUN, 11 BUSY), bit[20] picks board-BUSY over PLL-lock-loss on ROC >=
# 4.12 (this board runs 4.29), and bits[15:14] are a test-level override,
# kept off. The field bits [20:14] are owned here wholesale; bit[0] is the
# LEMO level, which SetIOLevel manages, so the two never collide.
REG_FRONT_PANEL_IO = 0x811C
GPO_FIELD_MASK = 0x1FC000                  # bits [20:14]
_GPO_FIELD = {"trigger": 0x000000,         # [17:16]=00: propagate the trigger
              "busy":    0x0D0000,         # [17:16]=01, [19:18]=11: board BUSY
              "run":     0x010000}         # [17:16]=01, [19:18]=00: RUN

# --- codecs between our config vocabulary and CAEN's enums ---
_TRIGMODE = {"disabled": TriggerMode_DISABLED,
             "acquisition_only": TriggerMode_ACQ_ONLY,
             "extout_only": TriggerMode_EXTOUT_ONLY,
             "acq_and_trgout": TriggerMode_ACQ_AND_EXTOUT}
_EDGE = {"rising": 0, "falling": 1}


def _inv(d):
    return {v: k for k, v in d.items()}


def _codec(fwd, fallback):
    """(encode, decode) for a string<->int enum; unknown ints fall back."""
    rev = _inv(fwd)
    return (lambda v: fwd.get(v, fwd[fallback]),
            lambda v: rev.get(int(v), fallback))


_TRIG_ENC, _TRIG_DEC = _codec(_TRIGMODE, "disabled")
_EDGE_ENC, _EDGE_DEC = _codec(_EDGE, "falling")
# CAEN_DGTZ_IOLevel_t: the electrical standard of the front-panel LEMOs.
_IOLEVEL = {"nim": 0, "ttl": 1}
_IO_ENC, _IO_DEC = _codec(_IOLEVEL, "nim")
_BOOL_ENC, _BOOL_DEC = (lambda v: 1 if v else 0), (lambda v: bool(v))
_INT_ENC, _INT_DEC = int, int

# attr, getter, setter, ctype, encode, decode
BOARD_HW = [
    ("max_events_blt", "GetMaxNumEventsBLT", "SetMaxNumEventsBLT", ct.c_uint32, _INT_ENC, _INT_DEC),
    ("drs4_frequency", "GetDRS4SamplingFrequency", "SetDRS4SamplingFrequency", ct.c_int, _INT_ENC, _INT_DEC),
    ("post_trigger", "GetPostTriggerSize", "SetPostTriggerSize", ct.c_uint32, _INT_ENC, _INT_DEC),
    ("external_trigger", "GetExtTriggerInputMode", "SetExtTriggerInputMode", ct.c_int, _TRIG_ENC, _TRIG_DEC),
    ("fast_trigger", "GetFastTriggerMode", "SetFastTriggerMode", ct.c_int, _TRIG_ENC, _TRIG_DEC),
    ("software_trigger", "GetSWTriggerMode", "SetSWTriggerMode", ct.c_int, _TRIG_ENC, _TRIG_DEC),
    ("fast_trigger_digitizing", "GetFastTriggerDigitizing", "SetFastTriggerDigitizing", ct.c_int, _BOOL_ENC, _BOOL_DEC),
    ("io_level", "GetIOLevel", "SetIOLevel", ct.c_int, _IO_ENC, _IO_DEC),
]
GROUP_HW = [
    ("fast_trigger_threshold", "GetGroupFastTriggerThreshold", "SetGroupFastTriggerThreshold", ct.c_uint32, _INT_ENC, _INT_DEC),
    ("fast_trigger_dc_offset", "GetGroupFastTriggerDCOffset", "SetGroupFastTriggerDCOffset", ct.c_uint32, _INT_ENC, _INT_DEC),
]


def _snap_post_trigger(pct: int, drs4_freq: int) -> int:
    """The nearest post-trigger percentage the board can actually reach."""
    steps = C.post_trigger_steps(drs4_freq)
    if not steps:
        return pct
    return min(steps, key=lambda s: (abs(s - pct), s))


def _diff(want, got, skip=()) -> list[str]:
    """Settings the board did not accept as asked. Not fatal — `got` is still
    the state we keep — but the user should see that it disagreed."""
    out = []

    def cmp(label, a, b, key=None):
        if (key or label) in skip:
            return          # write-only on this model; nothing to compare against
        if a != b:
            out.append(f"{label}: requested {a!r}, board reports {b!r}")

    for attr, *_ in BOARD_HW:
        cmp(attr, getattr(want, attr), getattr(got, attr))
    cmp("trigger_edge", want.trigger_edge, got.trigger_edge)
    cmp("gpo_output", want.gpo_output, got.gpo_output)
    for gr in range(C.NUM_GROUPS):
        for attr in [a for a, *_ in GROUP_HW] + ["enabled"]:
            cmp(f"group {gr} {attr}",
                getattr(want.groups[gr], attr), getattr(got.groups[gr], attr), key=attr)
    for ch in range(C.NUM_CHANNELS):
        cmp(f"ch {ch} dc_offset",
            want.channels[ch].dc_offset, got.channels[ch].dc_offset)
    return out


class _X742_GROUP(ct.Structure):
    _fields_ = [
        ("ChSize", ct.c_uint32 * MAX_X742_CHANNEL_SIZE),
        ("DataChannel", ct.POINTER(ct.c_float) * MAX_X742_CHANNEL_SIZE),
        ("TriggerTimeTag", ct.c_uint32),
        ("StartIndexCell", ct.c_uint16),
    ]


class _X742_EVENT(ct.Structure):
    _fields_ = [
        ("GrPresent", ct.c_uint8 * MAX_X742_GROUP_SIZE),
        ("DataGroup", _X742_GROUP * MAX_X742_GROUP_SIZE),
    ]


class _EventInfo(ct.Structure):
    _fields_ = [
        ("EventSize", ct.c_uint32), ("BoardId", ct.c_uint32),
        ("Pattern", ct.c_uint32), ("ChannelMask", ct.c_uint32),
        ("EventCounter", ct.c_uint32), ("TriggerTimeTag", ct.c_uint32),
    ]


class _DRS4CorrectionC(ct.Structure):
    """CAEN_DGTZ_DRS4Correction_t: the calibration stored on the board, read
    back with GetCorrectionTables for the "timing" correction mode. cell is
    indexed by physical DRS4 cell, nsample by readout position, time holds
    the per-cell time stamps the true axis is built from."""
    _fields_ = [
        ("cell", (ct.c_int16 * 1024) * MAX_X742_CHANNEL_SIZE),
        ("nsample", (ct.c_int8 * 1024) * MAX_X742_CHANNEL_SIZE),
        ("time", ct.c_float * 1024),
    ]


class _BoardInfoC(ct.Structure):
    _fields_ = [
        ("ModelName", ct.c_char * 12), ("Model", ct.c_uint32),
        ("Channels", ct.c_uint32), ("FormFactor", ct.c_uint32),
        ("FamilyCode", ct.c_uint32), ("ROC_FirmwareRel", ct.c_char * 20),
        ("AMC_FirmwareRel", ct.c_char * 40), ("SerialNumber", ct.c_uint32),
        ("MezzanineSerNum", (ct.c_char * 8) * 4), ("PCB_Revision", ct.c_uint32),
        ("ADC_NBits", ct.c_uint32), ("SAMCorrectionDataLoaded", ct.c_uint32),
        ("CommHandle", ct.c_int), ("VMEHandle", ct.c_int),
        ("License", ct.c_char * 17),
    ]


def _load_lib():
    """Load libCAENDigitizer / CAENDigitizer.dll.

    On Windows the API is `__stdcall` (`#define CAENDGTZ_API __stdcall` under
    `_WIN32`), so it must be loaded with WinDLL. CDLL is cdecl and would corrupt
    the stack on a 32-bit interpreter; x64 has only one convention so the two
    coincide there, but relying on that would fail bafflingly the day someone
    runs 32-bit Python.
    """
    if sys.platform == "win32":
        loader, names = ct.WinDLL, ("CAENDigitizer.dll", "CAENDigitizer")
        hint = ("Install CAEN's digitizer libraries and make sure their bin "
                "directory is on PATH. Python and the DLLs must both be 64-bit.")
    else:
        loader, names = ct.CDLL, ("libCAENDigitizer.so", "libCAENDigitizer.so.1")
        hint = "Install CAENDigitizer, CAENComm and CAENVMELib."
    for name in names:
        try:
            return loader(name)
        except OSError:
            continue
    raise OSError(f"libCAENDigitizer not found. {hint}")


class CaenBackend(DigitizerBackend):
    def __init__(self, link_num: int = 0, conet_node: int = 0, vme_base: int = 0):
        self._lib = None
        self._h = ct.c_int(-1)
        self._link_num = link_num
        self._conet_node = conet_node
        self._vme_base = vme_base
        self.link_used = "usb"        # which DAQ_LINK entry actually opened
        self._buf = ct.POINTER(ct.c_char)()
        self._buf_size = ct.c_uint32(0)
        self._evtptr = ct.c_void_p()      # decoded Event742
        self._cfg = None
        self._write_only: set[str] = set()    # settable, but not readable back
        self._unsupported: set[str] = set()   # the DT5742B rejects these outright
        self._state = None                    # last known board state, for deltas
        self._timing_tables = None            # set by configure() in timing mode

    def _chk(self, ret, what):
        if ret != CAEN_DGTZ_Success:
            named = _ERROR_NAMES.get(ret)
            detail = f"{ret} - {named}" if named else str(ret)
            raise RuntimeError(f"CAEN_DGTZ error {detail} in {what}")

    def open(self) -> BoardInfo:
        self._lib = _load_lib()
        # Try each configured link in order (DAQ_LINK; plain USB when unset)
        # and keep the first that answers. The failure report names every
        # attempt, so "which cable is it on?" is answered by the log.
        failures = []
        ret = None
        for conn_type, link_num, label in _link_specs():
            ret = self._lib.CAEN_DGTZ_OpenDigitizer(
                conn_type, link_num, self._conet_node,
                self._vme_base, ct.byref(self._h))
            if ret == CAEN_DGTZ_Success:
                self.link_used = label
                if label != "usb":
                    logsetup.did(log, f"Opening over the {label} link", "Ok")
                break
            failures.append(f"{label}: {_ERROR_NAMES.get(ret, ret)}")
        if ret != CAEN_DGTZ_Success:
            self._chk(ret, "OpenDigitizer (" + "; ".join(failures) + ")")
        bi = _BoardInfoC()
        self._chk(self._lib.CAEN_DGTZ_GetInfo(self._h, ct.byref(bi)), "GetInfo")
        # Purely informational - the library version shown in the badge tooltip.
        # Not worth failing an open over, but worth knowing when it is missing.
        sw = ct.create_string_buffer(64)
        try:
            if self._lib.CAEN_DGTZ_SWRelease(sw) != CAEN_DGTZ_Success:
                sw.value = b""
        except (AttributeError, OSError):
            sw.value = b""
        # NO Reset here. Opening must be non-destructive: the unit keeps its
        # settings across our process restarts, and read_settings is about to
        # adopt them. Resetting first wiped them and then faithfully read back
        # our own defaults - post-trigger 0, every DC offset 0x8f00 - which
        # looked like the board had those settings all along. Reset belongs in
        # configure(), where wiping is deliberate and everything is rewritten.
        return BoardInfo(
            model=bi.ModelName.decode(errors="ignore"),
            family_code=str(bi.FamilyCode), serial=bi.SerialNumber,
            roc_firmware=bi.ROC_FirmwareRel.decode(errors="ignore"),
            amc_firmware=bi.AMC_FirmwareRel.decode(errors="ignore"),
            sw_release=sw.value.decode(errors="ignore"),
        )

    def is_alive(self) -> bool:
        """Read the acquisition-status register: a real USB round trip.

        GetInfo is NOT usable here - it answers from state the library cached
        at open time and keeps succeeding after the unit is switched off.
        """
        if not self._lib or self._h.value < 0:
            return False
        try:
            val = ct.c_uint32(0)
            return self._lib.CAEN_DGTZ_ReadRegister(
                self._h, REG_ACQUISITION_STATUS, ct.byref(val)) == CAEN_DGTZ_Success
        except Exception:
            return False

    # ---------- settings: the board is the source of truth ----------
    def _get(self, name, *args, ctype=ct.c_uint32):
        fn = getattr(self._lib, "CAEN_DGTZ_" + name)
        v = ctype(0)
        rc = fn(self._h, *args, ct.byref(v))
        if rc == CAEN_DGTZ_CommError:
            rc = fn(self._h, *args, ct.byref(v))     # transient; see above
        return rc, v.value

    def _set(self, name, *args):
        fn = getattr(self._lib, "CAEN_DGTZ_" + name)
        rc = fn(self._h, *args)
        if rc == CAEN_DGTZ_CommError:
            rc = fn(self._h, *args)                  # transient; see above
        return rc

    # Each _rd_* refreshes one setting on `out`; a getter the module refuses is
    # recorded as write-only rather than reported as a failure.
    def _rd(self, errs, label, getter, *args, ctype=ct.c_uint32, key=None):
        rc, v = self._get(getter, *args, ctype=ctype)
        if rc == CAEN_DGTZ_Success:
            return True, v
        if rc == CAEN_DGTZ_FunctionNotAllowed:
            self._write_only.add(key or label)
        else:
            errs.append(f"{label}: error {rc}")
        return False, None

    def _rd_board(self, out, spec, errs):
        attr, getter, _s, ctype, _e, dec = spec
        if attr == "fast_trigger_digitizing":
            rc, v = self._get("ReadRegister", ct.c_uint32(REG_GROUP_CONFIG))
            if rc == CAEN_DGTZ_Success:
                out.fast_trigger_digitizing = bool(v & BIT_TR_DIGITIZE)
            else:
                errs.append(f"ReadRegister(0x{REG_GROUP_CONFIG:04x}): error {rc}")
            return
        ok, v = self._rd(errs, getter, getter, ctype=ctype, key=attr)
        if ok:
            setattr(out, attr, dec(v))

    def _rd_mask(self, out, errs):
        ok, mask = self._rd(errs, "GetGroupEnableMask", "GetGroupEnableMask")
        if ok:
            for gr in range(C.NUM_GROUPS):
                out.groups[gr].enabled = bool(mask & (1 << gr))

    def _wait_dac_idle(self, gr: int, timeout_s: float = 0.15) -> bool:
        """Give a DAC-backed write a quiet mezzanine to land on."""
        deadline = time.monotonic() + timeout_s
        while True:
            rc, v = self._get("ReadRegister",
                              ct.c_uint32(REG_GROUP_STATUS + GROUP_REG_STRIDE * gr))
            if rc == CAEN_DGTZ_Success and not (v & BIT_DAC_BUSY):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.003)

    def _retry_dropped_dac_writes(self, want, out, errs):
        """Re-issue DAC-backed writes whose readback came back unchanged.

        The channel DC offsets and the TR threshold/offset ride the
        mezzanine's slow SPI; a write landing while that controller is busy -
        e.g. right after configure() queued twenty of them at arm time - is
        silently dropped. Rather than reporting a mismatch the operator can
        only fix by clicking again, wait for the DAC to go idle and write
        once more; only a value the board refuses three times over reaches
        _diff and becomes an error."""
        for gr in range(C.NUM_GROUPS):
            for spec in GROUP_HW:
                attr, _g, setter, _ct2, enc, _d = spec
                if attr in self._write_only or setter in self._unsupported:
                    continue
                wanted = getattr(want.groups[gr], attr)
                for _ in range(3):
                    if getattr(out.groups[gr], attr) == wanted:
                        break
                    self._wait_dac_idle(gr)
                    self._set(setter, ct.c_uint32(gr), enc(wanted))
                    self._wait_dac_idle(gr)     # let it land before judging it
                    self._rd_group(out, gr, spec, errs)
        for ch in range(C.NUM_CHANNELS):
            wanted = want.channels[ch].dc_offset & 0xFFFF
            for _ in range(3):
                if (out.channels[ch].dc_offset & 0xFFFF) == wanted:
                    break
                self._wait_dac_idle(ch // C.GROUP_SIZE)
                self._set("SetChannelDCOffset", ct.c_uint32(ch), wanted)
                self._wait_dac_idle(ch // C.GROUP_SIZE)
                self._rd_channel(out, ch, errs)

    def _rd_gpo(self, out, errs):
        rc, v = self._get("ReadRegister", ct.c_uint32(REG_FRONT_PANEL_IO))
        if rc != CAEN_DGTZ_Success:
            errs.append(f"ReadRegister(0x{REG_FRONT_PANEL_IO:04x}): error {rc}")
            return
        # A field state we never write (clock probes, the test override) decodes
        # as "trigger", matching the codec convention for unknown enum values;
        # the next write of this setting normalizes the register.
        out.gpo_output = _inv(_GPO_FIELD).get(v & GPO_FIELD_MASK, "trigger")

    def _rd_edge(self, out, errs):
        ok, pol = self._rd(errs, "GetTriggerPolarity", "GetTriggerPolarity",
                           ct.c_uint32(0), ctype=ct.c_int, key="trigger_edge")
        if ok:
            out.trigger_edge = _EDGE_DEC(pol)

    def _rd_group(self, out, gr, spec, errs):
        attr, getter, _s, ctype, _e, dec = spec
        ok, v = self._rd(errs, f"{getter}[group {gr}]", getter,
                         ct.c_uint32(gr), ctype=ctype, key=attr)
        if ok:
            setattr(out.groups[gr], attr, dec(v))

    def _rd_channel(self, out, ch, errs):
        ok, v = self._rd(errs, f"GetChannelDCOffset[ch {ch}]", "GetChannelDCOffset",
                         ct.c_uint32(ch), key="dc_offset")
        if ok:
            out.channels[ch].dc_offset = int(v)

    def _blank(self, cfg):
        from ..config import BoardConfig
        return BoardConfig.from_dict(cfg.to_dict())

    def read_settings(self, cfg):
        """Full sweep: everything the board will tell us. Used on open."""
        out, errs = self._blank(cfg), []
        for spec in BOARD_HW:
            self._rd_board(out, spec, errs)
        self._rd_mask(out, errs)
        self._rd_edge(out, errs)
        self._rd_gpo(out, errs)
        for gr in range(C.NUM_GROUPS):
            for spec in GROUP_HW:
                self._rd_group(out, gr, spec, errs)
        for ch in range(C.NUM_CHANNELS):
            self._rd_channel(out, ch, errs)
        self._state = out
        return out, errs

    def write_settings(self, cfg):
        """Write only what changed, then read back only what was written.

        Re-writing every setting on every edit is needless bus traffic, and some
        setters have side effects nobody asked for. Reads are cheap but not free,
        so an untouched register is not re-read either - its cached value is
        still what the board last told us."""
        prev = self._state          # None => we know nothing, so do it all
        errs: list[str] = []
        # Work on our own copy. configure() hands us the engine's live config
        # object, and snapping post-trigger below would otherwise reach back and
        # edit it from under everything else holding a reference.
        cfg = self._blank(cfg)
        # Start from what was asked for, so purely app-side fields (channel
        # names, output options) survive; hardware fields are then overwritten
        # by what the board reports, and anything the board refused is rolled
        # back to its previous value.
        out = self._blank(cfg)
        reads = []                  # refresh exactly what we wrote
        wrote = False

        def put(name, *args):
            nonlocal wrote
            if name in self._unsupported:
                return False        # known-rejected on this model; stop asking
            rc = self._set(name, *args)
            wrote = True
            if rc == CAEN_DGTZ_FunctionNotAllowed:
                self._unsupported.add(name)
                errs.append(f"{name}: not supported on this model - ignored")
                return False
            if rc != CAEN_DGTZ_Success:
                errs.append(f"{name}: error {rc}")
            return True

        # The post-trigger register counts ~8.5 ns steps, so most whole
        # percentages are not reachable and the board silently snaps to the
        # nearest one. Snapping here first means the request and the readback
        # agree, instead of every write reporting a mismatch nobody can act on.
        cfg.post_trigger = _snap_post_trigger(cfg.post_trigger, cfg.drs4_frequency)
        out.post_trigger = cfg.post_trigger

        for spec in BOARD_HW:
            attr, _g, setter, _ct, enc, _d = spec
            if prev is None or getattr(prev, attr) != getattr(cfg, attr):
                if put(setter, enc(getattr(cfg, attr))):
                    reads.append(lambda sp=spec: self._rd_board(out, sp, errs))
                elif prev is not None:
                    setattr(out, attr, getattr(prev, attr))   # refused; keep old

        if prev is None or prev.group_enable_mask != cfg.group_enable_mask:
            if put("SetGroupEnableMask", cfg.group_enable_mask):
                reads.append(lambda: self._rd_mask(out, errs))

        if prev is None or prev.trigger_edge != cfg.trigger_edge:
            # Board-wide despite the per-channel signature: setting ch0 then ch1
            # to different values leaves both reading the last one. One write.
            if put("SetTriggerPolarity", ct.c_uint32(0), _EDGE_ENC(cfg.trigger_edge)):
                reads.append(lambda: self._rd_edge(out, errs))

        if prev is None or prev.gpo_output != cfg.gpo_output:
            # Read-modify-write: only the GPO field is ours; the rest of the
            # register (LEMO level, TRG-IN options) belongs to other settings.
            rc, v = self._get("ReadRegister", ct.c_uint32(REG_FRONT_PANEL_IO))
            if rc == CAEN_DGTZ_Success:
                word = (v & ~GPO_FIELD_MASK) | _GPO_FIELD.get(cfg.gpo_output, 0)
                if put("WriteRegister", ct.c_uint32(REG_FRONT_PANEL_IO),
                       ct.c_uint32(word)):
                    reads.append(lambda: self._rd_gpo(out, errs))
                elif prev is not None:
                    out.gpo_output = prev.gpo_output
            else:
                errs.append(f"gpo_output: ReadRegister(0x{REG_FRONT_PANEL_IO:04x})"
                            f" error {rc}")
                if prev is not None:
                    out.gpo_output = prev.gpo_output

        for gr in range(C.NUM_GROUPS):
            g = cfg.groups[gr]
            pg = prev.groups[gr] if prev is not None else None
            for spec in GROUP_HW:
                attr, _g2, setter, _ct, enc, _d = spec
                if pg is None or getattr(pg, attr) != getattr(g, attr):
                    if put(setter, ct.c_uint32(gr), enc(getattr(g, attr))):
                        reads.append(lambda r=gr, sp=spec: self._rd_group(out, r, sp, errs))
                    elif pg is not None:
                        setattr(out.groups[gr], attr, getattr(pg, attr))

        for ch in range(C.NUM_CHANNELS):
            want = cfg.channels[ch].dc_offset & 0xFFFF
            if prev is None or (prev.channels[ch].dc_offset & 0xFFFF) != want:
                if put("SetChannelDCOffset", ct.c_uint32(ch), want):
                    reads.append(lambda c=ch: self._rd_channel(out, c, errs))
                elif prev is not None:
                    out.channels[ch].dc_offset = prev.channels[ch].dc_offset

        if not wrote:
            self._state = out       # no hardware change, but names etc. may differ
            return out, errs

        for r in reads:
            r()
        self._retry_dropped_dac_writes(cfg, out, errs)
        self._state = out
        errs += _diff(cfg, out, skip=self._write_only)
        return out, errs

    def configure(self, cfg):
        """Arm-time setup. Reset wipes the board, so every setting is rewritten
        here; returns (actual config, errors) like write_settings."""
        self._cfg = cfg
        L, h = self._lib, self._h
        self._chk(L.CAEN_DGTZ_Reset(h), "Reset")
        self._state = None      # Reset invalidated everything we knew
        self._chk(L.CAEN_DGTZ_SetAcquisitionMode(h, AcqMode_SW_CONTROLLED), "SetAcquisitionMode")
        # See BIT_INDIVIDUAL_TRIGGER: without this the board counts triggers
        # and delivers no waveforms.
        self._chk(self._set("WriteRegister", ct.c_uint32(REG_GROUP_CONFIG_BITSET),
                            ct.c_uint32(BIT_INDIVIDUAL_TRIGGER)),
                  "WriteRegister(individual trigger bit)")
        actual, errs = self.write_settings(cfg)
        # DRS4 corrections. Three regimes:
        #   auto/manual - the library corrects inside DecodeEvent, including
        #     its time step, which RESAMPLES onto a uniform grid;
        #   timing      - we read the same tables off the board and apply only
        #     the amplitude part ourselves in read_events, keeping the samples
        #     untouched in time and carrying the true axis alongside;
        #   disabled    - raw cells.
        self._timing_tables = None
        if cfg.correction_level == "timing":
            tables = (_DRS4CorrectionC * MAX_X742_GROUP_SIZE)()
            self._chk(L.CAEN_DGTZ_GetCorrectionTables(
                h, cfg.drs4_frequency, ct.byref(tables)), "GetCorrectionTables")
            self._timing_tables = [
                {"cell": np.ctypeslib.as_array(t.cell).astype(np.float32),
                 "nsample": np.ctypeslib.as_array(t.nsample).astype(np.float32),
                 "time": np.ctypeslib.as_array(t.time).copy()}
                for t in tables]
        elif cfg.correction_level != "disabled":
            self._chk(L.CAEN_DGTZ_LoadDRS4CorrectionData(h, cfg.drs4_frequency),
                      "LoadDRS4CorrectionData")
            self._chk(L.CAEN_DGTZ_EnableDRS4Correction(h), "EnableDRS4Correction")
        # Readout buffers. BOTH have to be released first: configure() runs on
        # every arm, and an AllocateEvent without the matching FreeEvent leaked
        # a decoded-event buffer per start/stop cycle.
        self._free_buffers()
        self._chk(L.CAEN_DGTZ_MallocReadoutBuffer(h, ct.byref(self._buf), ct.byref(self._buf_size)),
                  "MallocReadoutBuffer")
        self._chk(L.CAEN_DGTZ_AllocateEvent(h, ct.byref(self._evtptr)), "AllocateEvent")
        return actual, errs

    def _free_buffers(self) -> None:
        """Release the readout and decoded-event buffers, if we hold any."""
        if self._evtptr:
            self._lib.CAEN_DGTZ_FreeEvent(self._h, ct.byref(self._evtptr))
            self._evtptr = ct.c_void_p()
        if self._buf:
            self._lib.CAEN_DGTZ_FreeReadoutBuffer(ct.byref(self._buf))
            self._buf = ct.POINTER(ct.c_char)()
            self._buf_size = ct.c_uint32(0)

    def start(self) -> None:
        self._chk(self._lib.CAEN_DGTZ_SWStartAcquisition(self._h), "SWStartAcquisition")

    def stop(self) -> None:
        self._chk(self._lib.CAEN_DGTZ_SWStopAcquisition(self._h), "SWStopAcquisition")

    def trigger(self) -> None:
        # One register write, so it shares the sporadic -1 every other call can
        # answer with; _set gives it the same single retry.
        self._chk(self._set("SendSWtrigger"), "SendSWtrigger")

    def read_events(self) -> list[Event]:
        L, h = self._lib, self._h
        read = ct.c_uint32(0)
        t0 = time.perf_counter() if _prof else 0.0
        ret = L.CAEN_DGTZ_ReadData(h, 0, self._buf, ct.byref(read))  # 0 = SLAVE_TERMINATED
        self._chk(ret, "ReadData")
        if read.value == 0:
            if _prof:
                _prof.empty(time.perf_counter() - t0)
            return []
        n = ct.c_uint32(0)
        self._chk(L.CAEN_DGTZ_GetNumEvents(h, self._buf, read, ct.byref(n)), "GetNumEvents")
        if _prof:
            _prof.add("readdata", time.perf_counter() - t0)
            _prof.batch(n.value, read.value)
        out: list[Event] = []
        info = _EventInfo()
        evtdata = ct.c_char_p()
        for i in range(n.value):
            t0 = time.perf_counter() if _prof else 0.0
            self._chk(L.CAEN_DGTZ_GetEventInfo(h, self._buf, read, i,
                      ct.byref(info), ct.byref(evtdata)), "GetEventInfo")
            self._chk(L.CAEN_DGTZ_DecodeEvent(h, evtdata, ct.byref(self._evtptr)), "DecodeEvent")
            if _prof:
                t0 = _prof.add("cdecode", time.perf_counter() - t0)
            ev742 = ct.cast(self._evtptr, ct.POINTER(_X742_EVENT)).contents
            samples: dict[int, np.ndarray] = {}
            trigger_cells: dict[int, int] = {}
            times_ns: dict[int, np.ndarray] = {}
            for gr in range(C.NUM_GROUPS):
                if not ev742.GrPresent[gr]:
                    continue
                group = ev742.DataGroup[gr]
                tc = int(group.StartIndexCell)
                trigger_cells[gr] = tc
                chans: dict[int, np.ndarray] = {}
                # 9 slots per group: 8 signal channels plus the digitized TR
                # trace at index 8 (present only when TR digitizing is on -
                # its ChSize is 0 otherwise). On the DT5742B both groups
                # digitize the same TR0 input.
                for ci in range(MAX_X742_CHANNEL_SIZE):
                    size = group.ChSize[ci]
                    if size == 0:
                        continue
                    ptr = group.DataChannel[ci]
                    chans[ci] = np.ctypeslib.as_array(
                        ptr, shape=(size,)).astype(np.float32).copy()
                if _prof:
                    t0 = _prof.add("copy", time.perf_counter() - t0)
                if self._timing_tables is not None and chans:
                    # Amplitude corrections only, on the whole group at once -
                    # peak removal votes across the group's 8 channels - and
                    # the true time axis instead of the library's resampling.
                    t = self._timing_tables[gr]
                    rows = sorted(chans)
                    stack = np.stack([chans[ci] for ci in rows])
                    corrections.amplitude_correct(
                        stack, t["cell"][rows], t["nsample"][rows], tc)
                    for k, ci in enumerate(rows):
                        chans[ci] = stack[k]
                    times_ns[gr] = corrections.true_times(
                        t["time"], tc, C.sample_period_ns(self._cfg.drs4_frequency),
                        stack.shape[1])
                    if _prof:
                        t0 = _prof.add("correct", time.perf_counter() - t0)
                for ci, arr in chans.items():
                    # TR traces land at 16+group - the RADiCAL channel[18]
                    # layout's last two slots.
                    ch = (16 + gr) if ci == C.GROUP_SIZE else gr * C.GROUP_SIZE + ci
                    samples[ch] = arr
            out.append(Event(index=info.EventCounter, timestamp_s=0.0,
                             trigger_time_tag=info.TriggerTimeTag, samples=samples,
                             trigger_cells=trigger_cells,
                             times_ns=times_ns or None))
        return out

    def close(self) -> None:
        """Release everything. Raises if the library refused to close.

        The handle is invalidated whatever happens, so nothing - is_alive() in
        particular, which runs on every status poll - can go on making calls
        against a device that has been closed.
        """
        if not self._lib or self._h.value < 0:
            return
        try:
            self._free_buffers()
            self._chk(self._lib.CAEN_DGTZ_CloseDigitizer(self._h), "CloseDigitizer")
        finally:
            self._h = ct.c_int(-1)
            self._state = None
