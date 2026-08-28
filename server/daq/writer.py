"""Data writers behind a small interface so new formats (ROOT, HDF5, ...) drop
in later without touching acquisition.

v1 target: WaveDump-compatible output. WaveDump writes one file per channel
(wave_<ch>.txt / .dat). ASCII = optional 7-line header then one sample per line.
Binary = optional 6x uint32 header then samples. For the 742, corrected samples
are floats.

NOTE (validation pending): byte-exactness vs a real WaveDump dump has not been
checked against hardware output yet - a sample .dat from the board will let us
confirm/lock the layout. The structure below follows WaveDump.c/WriteOutputFiles.
"""
from __future__ import annotations

import abc
import json
import os
import struct
import time

import numpy as np

from .backend.base import Event
from . import constants as C
from . import logsetup

log = logsetup.get("daq.writer")


class Writer(abc.ABC):
    @abc.abstractmethod
    def open(self, cfg) -> None: ...
    @abc.abstractmethod
    def write(self, ev: Event) -> None: ...
    @abc.abstractmethod
    def close(self) -> None: ...


def write_run_metadata(directory: str, cfg, run_name: str,
                       run_number: int | None = None,
                       note: str = "") -> None:
    """Channel names and settings go in a sidecar next to the data, whatever
    the format - the data files' own layouts are fixed by compatibility.
    Names are stored bare, without the UI's "CH n - " prefix. The note is the
    operator's own words from record time - what was tested, beam energy -
    the context no register readback can supply.

    A CAMPAIGN folder holds several run_<N>.root files, so the sidecar keeps
    a per-run entry under "runs" (note, started, events) while the top-level
    fields always describe the LATEST recording - which is what the listing
    shows, and exactly right for the single-run folders too."""
    path = os.path.join(directory, "run_metadata.json")
    try:
        with open(path) as f:
            existing = json.load(f)
        per_run = existing.get("runs") if isinstance(existing, dict) else None
        per_run = dict(per_run) if isinstance(per_run, dict) else {}
    except (OSError, ValueError):
        per_run = {}
    if run_number is not None:
        per_run[str(run_number)] = {"note": note, "started": time.time()}
    meta = {
        "runs": per_run,
        "run_name": run_name,
        "run_number": run_number,
        "note": note,
        "started": time.time(),
        # Where each channel lands in the ROOT file's channel[18] array -
        # maketree's interleaved order, TR/MCP copies at slots 8 and 17.
        # Recorded per run so an analysis never has to guess the mapping.
        "root_channel_layout": "slot = group*9 + ch_in_group; slots 8 and 17 "
                               "are the TR0 (MCP) copies; amplitudes in mV = "
                               "1000*(counts/4095 - 0.5)",
        "channels": {
            str(ch): {"name": cfg.channels[ch].name,
                      "dc_offset": cfg.channels[ch].dc_offset,
                      "root_slot": (ch // 8) * 9 + (ch % 8)}
            for ch in cfg.enabled_channels()
        },
        "drs4_frequency": cfg.drs4_frequency,
        "record_length": cfg.record_length,
        "post_trigger": cfg.post_trigger,
        "output_format": cfg.output_format,
    }
    with open(os.path.join(directory, "run_metadata.json"), "w") as f:
        json.dump(meta, f, indent=2)


def stamp_run_end(directory: str, events: int,
                  run_number: int | None = None) -> None:
    """Final event count into the sidecar, so a listing can show it without
    opening every data file. Losing it only costs the listing an event count,
    so it must never take a close down - but it is worth a line in the log,
    because a run that will not stamp usually cannot be written to either."""
    path = os.path.join(directory, "run_metadata.json")
    try:
        with open(path) as f:
            meta = json.load(f)
        meta["events"] = events
        meta["ended"] = time.time()
        entry = (meta.get("runs") or {}).get(str(run_number))
        if entry is not None:            # the campaign folder's per-run record
            entry["events"] = events
        with open(path, "w") as f:
            json.dump(meta, f, indent=2)
    except (OSError, ValueError) as e:
        log.warning("Could not stamp the event count into %s: %s", path, e)


class NullWriter(Writer):
    def open(self, cfg): pass
    def write(self, ev): pass
    def close(self): pass


class WaveDumpWriter(Writer):
    def __init__(self, directory: str, run_name: str = "",
                 run_number: int | None = None, note: str = ""):
        self._note = note
        self._files = {}
        self._cfg = None
        self._ascii = True
        self._header = False
        self._dir = directory
        self._run_name = run_name
        self._run_number = run_number
        self._events = 0

    def open(self, cfg) -> None:
        self._ascii = (cfg.output_format.lower() == "ascii")
        self._header = bool(cfg.output_header)
        os.makedirs(self._dir, exist_ok=True)
        ext = "txt" if self._ascii else "dat"
        mode = "w" if self._ascii else "wb"
        self._files = {}
        try:
            for ch in cfg.enabled_channels():
                path = os.path.join(self._dir, f"wave_{ch}.{ext}")
                self._files[ch] = open(path, mode)
            write_run_metadata(self._dir, cfg, self._run_name, self._run_number,
                               self._note)
            self._cfg = cfg         # last: close() takes this as "there is a run"
        except OSError:
            # Do not leave half a run open: the caller discards the directory,
            # and on Windows it cannot remove files we still hold. With _cfg
            # still unset, close() knows there is no metadata to stamp.
            self.close()
            raise

    def write(self, ev: Event) -> None:
        self._events += 1
        for ch, wave in ev.samples.items():
            f = self._files.get(ch)
            if f is None:
                continue
            if self._ascii:
                if self._header:
                    self._write_ascii_header(f, ch, ev, len(wave))
                f.write("\n".join(f"{v:.6f}" for v in wave))
                f.write("\n")
            else:
                payload = wave.astype("<f4").tobytes()
                if self._header:
                    # WaveDump binary header: 6 x uint32. The size must describe
                    # the bytes that follow it, which are always 4 per sample -
                    # not wave.nbytes, which is whatever dtype arrived.
                    f.write(struct.pack(
                        "<6I", 24 + len(payload), self._cfg_board_id(), 0, ch,
                        ev.index & 0xFFFFFFFF, ev.trigger_time_tag & 0xFFFFFFFF,
                    ))
                f.write(payload)

    def _write_ascii_header(self, f, ch, ev, n):
        f.write(f"Record Length: {n}\n")
        f.write("BoardID: 0\n")
        f.write(f"Channel: {ch}\n")
        f.write(f"Event Number: {ev.index}\n")
        f.write("Pattern: 0x0000\n")
        f.write(f"Trigger Time Stamp: {ev.trigger_time_tag}\n")
        f.write(f"DC offset (DAC): {self._cfg.channels[ch].dc_offset:04x}\n")

    def _cfg_board_id(self) -> int:
        return 0

    def close(self) -> None:
        for ch, f in self._files.items():
            try:
                f.close()
            except OSError as e:
                # A failed close means buffered samples never reached the disk.
                # Keep closing the rest, but do not lose the fact that this
                # channel's file is short.
                log.error("wave file for channel %d did not close cleanly, so its "
                          "last events may be missing: %s", ch, e)
        self._files = {}
        if self._cfg is not None:
            stamp_run_end(self._dir, self._events, self._run_number)


class RootWriter(Writer):
    """One waveforms.root per run: TTree "pulse", readable by CERN ROOT and
    uproot alike, written with no ROOT install on the DAQ machine.

    The branch layout follows the group's RADiCAL testbeam converter
    (gitlab.cern.ch/ledovsk/tb_fnal_radical, drs2root/maketree.cc) so files
    drop straight into that analysis:

      event/I               event number
      channel[18][1024]/F   slot = group*9 + ch, amplitudes in mV (below)
      times[2][1024]/F      per-group sample times, ns
      tc[2]/s               per-group DRS4 start cell (trigger cell)

    The channel slots are maketree's INTERLEAVED 9-per-group order
    (`totalIndex = realGroup*9 + i`, i = 0..8 with the TR copy at 8):

      0-7   group 0 signal channels          9-16  group 1 signal channels
      8     group 0's TR0 copy (the MCP)     17    group 1's TR0 copy

    NOT 16 signal channels then two TR traces - an earlier version wrote
    that, and it would have sent an analysis reading slot 8 as the MCP to a
    signal channel instead. Amplitudes are maketree's convention too:
    window-referenced mV, 1000*(counts/4095 - 0.5), so -500..+500 mV with
    the window centre at 0.

    In "timing" correction mode the times branch carries each event's TRUE
    non-uniform axis, exactly as maketree produces - full timing precision,
    no converter. In "auto" mode times are uniform steps, which is what the
    library's resampling time correction leaves behind. tc is recorded in
    every mode so the two paths can be cross-checked. trigger_time_tag
    rides along as an extra branch.

    Events are buffered and written in batches so baskets stay a sane size;
    a stream of one-event extends would bloat the file and the read path.
    """
    BATCH = 64
    N_CHANNELS_OUT = 18            # matches maketree's channel[18][1024]

    def __init__(self, directory: str, run_name: str = "",
                 run_number: int | None = None, note: str = ""):
        self._dir = directory
        self._run_name = run_name
        self._run_number = run_number
        self._note = note
        self._file = None
        self._tree = None
        self._cfg = None
        self._events = 0
        self._buf: list[dict] = []
        self._times = None

    def open(self, cfg) -> None:
        import uproot          # deferred: only a ROOT run pays the import
        from . import constants as C

        self._cfg = cfg
        os.makedirs(self._dir, exist_ok=True)
        n = cfg.record_length
        # run_<N>.root: the number is the analysis-facing identity of the file
        # (test-beam convention), inferred or set at record time.
        fname = (f"run_{self._run_number}.root" if self._run_number
                 else "waveforms.root")
        self._file = uproot.recreate(os.path.join(self._dir, fname))
        self._tree = self._file.mktree(
            "pulse",
            {"event": np.int32, "trigger_time_tag": np.uint32,
             "channel": (np.float32, (self.N_CHANNELS_OUT, n)),
             "times": (np.float32, (2, n)),
             "tc": (np.uint16, (2,))},
            title="Digitized waveforms")
        dt = C.sample_period_ns(cfg.drs4_frequency)
        self._times = np.tile(np.arange(n, dtype=np.float32) * dt, (2, 1))
        write_run_metadata(self._dir, cfg, self._run_name, self._run_number,
                           self._note)

    @staticmethod
    def root_slot(ch: int) -> int:
        """maketree's flat slot for the decoder's channel numbering.

        The decoder counts signal channels 0-15 and the TR copies 16/17;
        maketree interleaves per group with the TR copy at in-group index 8.
        This is the one place the two numberings meet - everything else in
        the app speaks decoder numbers."""
        if ch >= C.NUM_CHANNELS:               # decoder TR indices 16/17
            return (ch - C.NUM_CHANNELS) * 9 + 8
        return (ch // 8) * 9 + (ch % 8)

    @staticmethod
    def to_mv(wave) -> np.ndarray:
        """maketree's amplitude convention: window-referenced millivolts."""
        return ((np.asarray(wave, dtype=np.float32) / 4095.0) - 0.5) * 1000.0

    def write(self, ev: Event) -> None:
        n = self._cfg.record_length
        chans = np.zeros((self.N_CHANNELS_OUT, n), dtype=np.float32)
        for ch, wave in ev.samples.items():
            slot = self.root_slot(ch)
            if 0 <= slot < self.N_CHANNELS_OUT:
                chans[slot, :len(wave)] = self.to_mv(wave)
        # True per-event times when the correction mode produced them
        # ("timing"); the uniform axis otherwise.
        times = self._times
        if ev.times_ns:
            times = self._times.copy()
            for gr, t in ev.times_ns.items():
                if 0 <= gr < 2:
                    times[gr, :len(t)] = t
        tc = np.zeros(2, dtype=np.uint16)
        for gr, cell in (ev.trigger_cells or {}).items():
            if 0 <= gr < 2:
                tc[gr] = cell
        self._buf.append({"event": ev.index,
                          "trigger_time_tag": ev.trigger_time_tag & 0xFFFFFFFF,
                          "channel": chans, "times": times, "tc": tc})
        self._events += 1
        if len(self._buf) >= self.BATCH:
            self._flush()

    def _flush(self) -> None:
        if not self._buf:
            return
        b = self._buf
        self._buf = []
        self._tree.extend({
            "event": np.array([e["event"] for e in b], dtype=np.int32),
            "trigger_time_tag": np.array([e["trigger_time_tag"] for e in b],
                                         dtype=np.uint32),
            "channel": np.stack([e["channel"] for e in b]),
            "times": np.stack([e["times"] for e in b]),
            "tc": np.stack([e["tc"] for e in b]),
        })

    def close(self) -> None:
        try:
            self._flush()
        finally:
            if self._file is not None:
                self._file.close()
                self._file = None
        if self._cfg is not None:
            stamp_run_end(self._dir, self._events, self._run_number)


def make_writer(directory: str, run_name: str = "",
                output_format: str = "ascii",
                run_number: int | None = None, note: str = "") -> Writer:
    if (output_format or "").lower() == "root":
        return RootWriter(directory, run_name, run_number, note)
    return WaveDumpWriter(directory, run_name, run_number, note)
