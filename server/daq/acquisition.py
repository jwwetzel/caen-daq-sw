"""Acquisition engine: owns the backend, runs readout on its own thread fully
decoupled from the web server, and exposes cheap telemetry snapshots (decimated
averaged waveforms for all enabled channels + a rolling trigger-rate window)."""
from __future__ import annotations

import logging
import threading
import time

import numpy as np

from .backend.base import make_backend, DigitizerBackend, BoardInfo
from .config import BoardConfig, default_config
from .stats import RollingAverage, TriggerRateMeter, decimate
from .writer import make_writer
from . import runs
from . import constants as C
from . import logsetup


log = logsetup.get("daq.acq")


class _StatsCollector:
    """Accumulates per-channel (baseline, min, max) over the next N events -
    the calibrator's measuring instrument. Baseline is the median of each
    event's median, so a pulse in the record does not drag it."""

    def __init__(self, events: int):
        self.want = events
        self.seen = 0
        self.done = threading.Event()
        self.last_add = time.monotonic()   # for the stall detector
        self._by_ch: dict[int, dict] = {}
        self._lock = threading.Lock()

    def add(self, ev) -> None:
        with self._lock:
            self.last_add = time.monotonic()
            if self.seen >= self.want:
                return
            for ch, wave in ev.samples.items():
                e = self._by_ch.setdefault(
                    ch, {"medians": [], "min": float("inf"), "max": float("-inf")})
                e["medians"].append(float(np.median(wave)))
                e["min"] = min(e["min"], float(wave.min()))
                e["max"] = max(e["max"], float(wave.max()))
            self.seen += 1
            if self.seen >= self.want:
                self.done.set()

    def summary(self) -> dict[int, dict]:
        with self._lock:
            return {ch: {"baseline": float(np.median(e["medians"])),
                         "min": e["min"], "max": e["max"],
                         "n": len(e["medians"])}
                    for ch, e in self._by_ch.items() if e["medians"]}


class AcquisitionEngine:
    def __init__(self, backend_factory=make_backend):
        # Injectable so tests can refuse hardware outright. The default factory
        # loads the real libCAENDigitizer, so on a machine with a unit attached
        # a "hardware-free" test would open — or hang on — the actual board.
        self._backend_factory = backend_factory
        self._backend: DigitizerBackend | None = None
        self._board_info = BoardInfo()
        self._cfg = default_config()   # only a seed; the board wins once open
        self._avg = RollingAverage()
        self._rate = TriggerRateMeter()
        # Latest single event per channel, as (event_index, wave). Telemetry
        # ships it decimated so the UI's overlay mode can accumulate a
        # density picture client-side - one trace per tick, so the stream
        # stays the same size class as the averages and can never throttle
        # data-taking. (index, wave) as one tuple: assignment is atomic, so
        # the telemetry thread never sees a wave paired with the wrong id.
        self._last: dict[int, tuple[int, np.ndarray]] = {}
        # Recording is independent of acquiring: you watch first, then record.
        self._writer = None
        self._run_id: str | None = None
        self._run_started: float | None = None
        self._recorded = 0
        self._rec_limit: int | None = None    # auto-close the run at N events

        self._thread: threading.Thread | None = None
        self._running = threading.Event()
        self._lock = threading.Lock()
        # Serialises opening the board. Startup now opens on a worker thread, so
        # a status poll arriving mid-open would otherwise call _try_open and put
        # a second thread inside libCAENDigitizer on the same handle.
        self._open_lock = threading.RLock()
        self._events_seen = 0
        self._errors: list[str] = []
        self._opened = False
        self._last_open_attempt = 0.0
        # Software triggers are queued here and fired by the readout loop, one
        # per pass at the requested pace. Firing from the request thread that
        # asked for them would put a second thread inside libCAENDigitizer
        # while the loop is in ReadData on the same handle.
        self._sw_pending = 0
        self._sw_interval_s = 0.0
        self._sw_next_fire = 0.0
        # Scope mode: free-running software triggers at a steady pace, with
        # full-resolution single traces in telemetry (see telemetry()). The
        # line-noise debugging tool - one trace, replaced by the next.
        self._scope_hz: float | None = None
        self._scope_next_fire = 0.0
        # Optional software channel-trigger for the scope: only events where
        # this channel's trace crosses a level (relative to its own median
        # baseline) refresh the single-trace display. {"channel", "level_mv",
        # "edge"} or None for every event. The x742 cannot hardware-trigger
        # on a signal channel - every channel-trigger call answers -17 - so
        # this is a display trigger over the randomly-sampled windows, not an
        # acquisition trigger; rare pulses still need the signal on TR0.
        self._scope_trigger: dict | None = None
        # Per-event stats tap for the calibrator: set for the duration of one
        # measurement, fed by the readout loop, then cleared.
        self._stats_col: _StatsCollector | None = None
        from .calibration import Calibrator
        self.calibrator = Calibrator(self)

    # ---------- lifecycle ----------
    def open(self, level: int = logging.INFO):
        with self._open_lock:
            with logsetup.step(log, "Opening the digitizer", level=level) as opening:
                backend = self._backend_factory()
                board_info = backend.open()
                self._backend = backend
                self._board_info = board_info
                opening.done(f"Found {board_info.model} S/N {board_info.serial}, "
                             f"ROC {board_info.roc_firmware}, "
                             f"AMC {board_info.amc_firmware}")

            # The board, not our last-used file, is the source of truth.
            with logsetup.step(log, "Reading settings off the unit",
                               level=level) as reading:
                cfg, errs = backend.read_settings(self._cfg)
                for e in errs:
                    log.warning("%sCould not read: %s", "  ", e)
                    self._record_error(f"read settings: {e}")
                reading.done(f"{len(errs)} settings could not be read" if errs
                             else "All settings read")
            with self._lock:
                self._cfg = cfg
            # Last, not first: while this is False every other path treats the
            # unit as absent and keeps off the wire, so nothing talks to a board
            # that is still being set up.
            self._opened = True
            return self._board_info

    def get_config(self) -> BoardConfig:
        """A COPY, deliberately: callers mutate what they get (the calibrator,
        the tests), and handing out the live object let a mutation slip past
        set_config's changed-DAC comparison - it compared the object with
        itself and saw nothing to re-arm for."""
        with self._lock:
            return BoardConfig.from_dict(self._cfg.to_dict())

    def _dac_backed_changed(self, cfg: BoardConfig) -> bool:
        """Did this write touch a setting that lives on a mezzanine DAC?

        Those (channel DC offsets, TR threshold/offset) only take ANALOG
        effect at an arm - written mid-acquisition they update the register
        and change nothing, measured on serial 53364."""
        with self._lock:
            cur = self._cfg
        if any(c.dc_offset != o.dc_offset
               for c, o in zip(cfg.channels, cur.channels)):
            return True
        return any(g.fast_trigger_dc_offset != o.fast_trigger_dc_offset
                   or g.fast_trigger_threshold != o.fast_trigger_threshold
                   for g, o in zip(cfg.groups, cur.groups))

    def set_config(self, cfg: BoardConfig) -> tuple[BoardConfig, list[str]]:
        """Push to the board and adopt what it reports back.

        Returns (actual config, errors from this call). The errors are returned
        rather than left for the caller to recover from `status()`: that list is
        a capped ring, so once it is full a diff of it reports no errors at all
        and a refused write reads as a success.

        A DAC-backed change while acquiring re-arms automatically (stop,
        write, start) - the only way it takes analog effect - and is refused
        outright while recording, where a mid-run baseline shift would
        corrupt the data with a straight face.
        """
        if not self._opened or self._backend is None:
            # Nothing was sent anywhere. Returning the requested config here
            # would have the UI show - and confirm - a value the unit never
            # received, and it would be discarded anyway the moment we reopen
            # and read the unit's own settings.
            err = "no unit connected: settings were not applied"
            log.warning("settings not applied: no unit connected")
            self._record_error(err)
            with self._lock:
                return self._cfg, [err]

        rearm = False
        if self._running.is_set() and self._dac_backed_changed(cfg):
            if self._writer is not None:
                err = ("DC offsets and TR levels cannot change "
                       "during a recording - stop it first")
                self._record_error(err)
                with self._lock:
                    return self._cfg, [err]
            rearm = True
            self.stop()


        with logsetup.step(log, "Writing settings to the unit") as writing:
            try:
                actual, errors = self._backend.write_settings(cfg)
            except Exception as e:
                # The write blew up part-way, so what the board now holds is
                # unknown. Keeping the requested config would show - and
                # confirm - settings that may never have landed.
                with self._lock:
                    actual = self._cfg
                errors = [f"write settings: {e}; showing the last confirmed settings"]
            for e in errors:
                log.warning("%s%s", "  ", e)
                self._record_error(e)
            writing.done(f"{len(errors)} settings refused or read back wrong"
                         if errors else "All settings accepted and read back")
        with self._lock:
            self._cfg = actual
        if rearm:
            self.start()               # the arm is what loads the DACs
        return actual, errors

    def start(self) -> bool:
        """Arm the board and begin reading out. True if acquisition is running.

        Every failure here is refused, never raised: an exception through the
        API produces a full ASGI traceback in the log and a 500 in the UI,
        neither of which says anything the error list does not.
        """
        if self._running.is_set():
            return True
        with logsetup.step(log, "Starting acquisition") as starting:
            if not self._opened:
                try:
                    self.open()
                except Exception as e:
                    starting.done("Not started: no unit connected")
                    self._record_error(f"start: {e}")
                    return False
            with self._lock:
                cfg = self._cfg
            with logsetup.step(log, "Applying settings to the unit") as applying:
                try:
                    actual, cfg_errs = self._backend.configure(cfg)
                except Exception as e:
                    # Reset() has already wiped the board by the time most of
                    # configure() can fail, so say that rather than let the
                    # caller assume the unit is untouched.
                    applying.done(f"Could not apply them: {e}")
                    self._record_error(f"configure: {e}")
                    starting.done("Not started: the unit would not take its settings")
                    return False
                for e in cfg_errs:
                    log.warning("%sRefused: %s", "  ", e)
                    self._record_error(e)
                applying.done(f"{len(cfg_errs)} settings refused" if cfg_errs
                              else "All settings accepted")
            with self._lock:
                self._cfg = actual
            self._events_seen = 0      # Count reflects this acquisition run
            self._rate.reset()
            try:
                self._backend.start()
            except Exception as e:
                logsetup.did(log, "Arming the board", f"Refused: {e}",
                             level=logging.ERROR)
                self._record_error(f"arm: {e}")
                starting.done("Not started: the board would not arm")
                return False
            logsetup.did(log, "Arming the board", "Ok")
            self._running.set()
            self._thread = threading.Thread(target=self._loop, name="acq", daemon=True)
            self._thread.start()
            starting.done("Acquisition running")
            return True

    def collect_stats(self, events: int, fire_sw: bool,
                      stall_s: float = 30.0,
                      abort: threading.Event | None = None,
                      progress=None) -> tuple[dict[int, dict], int]:
        """Per-channel {baseline, min, max} over the next `events` events.

        Event-count-driven, not time-driven: the wait lasts as long as events
        keep arriving, however slowly. The only clock is the STALL detector -
        `stall_s` with no events at all means nothing is triggering, and the
        caller gets whatever arrived plus the honest count to judge it by.
        With fire_sw the engine supplies its own software triggers (the
        no-signal measurement). `abort` ends the wait early; `progress` is
        told the running event count for a live status line."""
        col = _StatsCollector(events)
        with self._lock:
            self._stats_col = col
        try:
            if fire_sw:
                r = self.fire_software_triggers(events, rate_hz=100.0)
                if not r.get("ok"):
                    raise RuntimeError(r.get("error") or "could not fire triggers")
            elif not self._running.is_set():
                self.start()
                if not self._running.is_set():
                    raise RuntimeError("no unit connected")
            last_reported = -1
            while not col.done.wait(0.2):
                if abort is not None and abort.is_set():
                    break
                if time.monotonic() - col.last_add > stall_s:
                    break
                if progress is not None and col.seen != last_reported:
                    last_reported = col.seen
                    progress(col.seen)
        finally:
            with self._lock:
                self._stats_col = None
        return col.summary(), col.seen

    def fire_software_triggers(self, count: int = 1, rate_hz: float = 10.0) -> dict:
        """Queue `count` software triggers for the readout loop to fire.

        The bench check with no signal source: the x742 cannot self-trigger, so
        the board is poked from software instead. Starts acquisition if the
        operator has not already, the same courtesy start_recording extends.
        """
        if not self._running.is_set():
            self.start()
        if not self._running.is_set():           # start() refused: no unit
            return {"ok": False, "error": "no unit connected"}
        with self._lock:
            mode = self._cfg.software_trigger
        if mode == "disabled":
            # The board would swallow every SendSWtrigger without a trace;
            # say so now instead of reporting 100 triggers that did nothing.
            return {"ok": False, "error": "the software trigger is disabled "
                                          "in the unit settings"}
        count = max(1, min(int(count), 100_000))
        rate_hz = min(max(float(rate_hz), 0.1), 1000.0)
        with self._lock:
            self._sw_pending += count
            self._sw_interval_s = 1.0 / rate_hz
        logsetup.did(log, f"Queueing {count} software triggers at {rate_hz:g} Hz", "Ok")
        return {"ok": True, "queued": count, "rate_hz": rate_hz}

    @staticmethod
    def _valid_scope_trigger(trigger) -> dict | None:
        """The scope trigger spec, normalised, or None for trigger-on-anything.
        A malformed spec becomes None rather than an error: the scope keeps
        showing traces, which is what a scope is for."""
        if not isinstance(trigger, dict):
            return None
        try:
            ch = int(trigger["channel"])
            level = float(trigger["level_mv"])
        except (KeyError, TypeError, ValueError):
            return None
        if not 0 <= ch < C.NUM_CHANNELS + C.NUM_GROUPS:   # 0-15 + TR copies
            return None
        edge = trigger.get("edge")
        return {"channel": ch,
                "level_mv": min(500.0, max(1.0, level)),
                "edge": edge if edge in ("rising", "falling") else "falling"}

    def set_scope(self, rate_hz: float | None, trigger: dict | None = None) -> dict:
        """Scope mode on (at `rate_hz`) or off (None).

        On: the readout loop free-runs software triggers at a steady pace and
        telemetry ships each single trace at FULL resolution - the line-noise
        debugging tool: one trace, replaced by the next, nothing averaged
        away. The pace is deliberately capped low: full-resolution traces are
        heavy on the wire, and a noise study needs eyes-on time per trace,
        not throughput."""
        if rate_hz is None:
            with self._lock:
                already_off = self._scope_hz is None
                self._scope_hz = None
                self._scope_trigger = None
            if not already_off:
                logsetup.did(log, "Leaving scope mode", "Ok")
            return {"ok": True, "scope_hz": None, "scope_trigger": None}
        if not self._running.is_set():
            self.start()
        if not self._running.is_set():           # start() refused: no unit
            return {"ok": False, "error": "no unit connected"}
        with self._lock:
            mode = self._cfg.software_trigger
        if mode == "disabled":
            return {"ok": False, "error": "the software trigger is disabled "
                                          "in the unit settings"}
        rate_hz = min(max(float(rate_hz), 0.1), 20.0)
        trig = self._valid_scope_trigger(trigger)
        with self._lock:
            self._scope_hz = rate_hz
            self._scope_next_fire = 0.0          # first trace immediately
            self._scope_trigger = trig
        on = (f" (showing only events where CH {trig['channel']} crosses "
              f"{trig['level_mv']:g} mV {trig['edge']})" if trig else "")
        logsetup.did(log, f"Scope mode: software triggers at {rate_hz:g} Hz{on}",
                     "Ok")
        return {"ok": True, "scope_hz": rate_hz, "scope_trigger": trig}

    def _scope_gate(self, ev) -> bool:
        """Should this event refresh the single-trace display?

        True always, except when the scope's channel-trigger is set: then only
        events where that channel's trace crosses the level - measured against
        the trace's own median baseline, so a DC-offset move never changes the
        condition - hold the display, and everything else leaves the last
        triggering trace on screen, the way a scope's display holds."""
        with self._lock:
            trig = self._scope_trigger if self._scope_hz is not None else None
        if not trig:
            return True
        wave = ev.samples.get(trig["channel"])
        if wave is None:
            return True         # the judged channel is not in the event
        base = float(np.median(wave))
        level = trig["level_mv"] * (C.ADC_MAX + 1) / 1000.0
        if trig["edge"] == "rising":
            return float(wave.max()) - base >= level
        return base - float(wave.min()) >= level

    def _fire_due_software_trigger(self):
        """One trigger per loop pass, no sooner than the requested pace.
        Queued test triggers and the scope's free-running pace share this
        single firing point, so only the readout thread ever touches the
        handle."""
        now = time.monotonic()
        with self._lock:
            due = self._sw_pending > 0 and now >= self._sw_next_fire
            if due:
                self._sw_pending -= 1
                self._sw_next_fire = now + self._sw_interval_s
            elif self._scope_hz is not None and now >= self._scope_next_fire:
                due = True
                self._scope_next_fire = now + 1.0 / self._scope_hz
        if not due:
            return
        try:
            self._backend.trigger()
        except Exception as e:
            with self._lock:
                self._sw_pending = 0    # one report, not one per queued trigger
                self._scope_hz = None
            self._record_error(f"software trigger: {e}")

    def stop(self):
        if not self._running.is_set():
            return
        with self._lock:
            self._sw_pending = 0        # owed triggers die with the acquisition
        with logsetup.step(log, "Stopping acquisition") as stopping:
            self._running.clear()
            # Before the join, not after: this clears the writer, so the loop
            # stops writing at once and its own end-of-run cleanup (which is
            # written for a LOST board) finds nothing left to report.
            self.stop_recording()
            if self._thread:
                self._thread.join(timeout=2.0)
            halted = True
            if self._backend is not None:
                try:
                    self._backend.stop()
                except Exception as e:
                    halted = False
                    log.error("%sThe board would not stop: %s", "  ", e)
                    self._record_error(f"stop: {e}")
            stopping.done(
                f"Readout stopped after {self._events_seen} events"
                + ("" if halted else "; the board is still armed"))

    def close(self):
        self.stop()
        if self._backend and self._opened:
            try:
                self._backend.close()
                logsetup.did(log, "Closing the digitizer", "Ok")
            except Exception as e:
                logsetup.did(log, "Closing the digitizer", f"Failed: {e}",
                             level=logging.ERROR)
                self._record_error(f"close: {e}")
            finally:
                self._opened = False

    # ---------- connection health ----------
    def probe(self) -> bool:
        """Liveness for the UI, safe to call on every status poll.

        While acquiring, the readout loop is the authority — a concurrent board
        call would race it. While idle, poke the board, and retry a lost one at
        a slow cadence so the app recovers once the unit is switched back on.
        """
        if self._running.is_set():
            return self._opened
        if self._opened and self._backend is not None:
            alive = False
            try:
                alive = bool(self._backend.is_alive())
            except Exception:
                alive = False
            if alive:
                return True
            self._opened = False
            self._board_info = BoardInfo()
            self._record_error("board stopped responding")
        self._try_open(force=False)
        return self._opened

    def reconnect(self) -> dict:
        """Explicit user-driven reconnect: drop what we have and open again now."""
        with logsetup.step(log, "Reconnecting to the unit") as reconnecting:
            self.stop()
            self._opened = False
            self._try_open(force=True)
            reconnecting.done("Reconnected" if self._opened else "No unit found")
        return self.status()

    def _try_open(self, force: bool):
        if self._opened:
            return
        now = time.monotonic()
        if not force and now - self._last_open_attempt < C.RECONNECT_RETRY_S:
            return
        # An open is already running: leave it alone rather than starting a
        # second one on the same hardware.
        if not self._open_lock.acquire(blocking=False):
            return
        try:
            self._open_locked(force, now)
        finally:
            self._open_lock.release()

    def _open_locked(self, force: bool, now: float):
        self._last_open_attempt = now
        if self._backend is not None:
            closed = "Ok"
            try:
                self._backend.close()
            except Exception as e:
                closed = f"would not close ({e})"
            logsetup.did(log, "Closing the previous connection", closed,
                         level=logging.INFO if force else logging.DEBUG)
            self._backend = None
        try:
            # An automatic retry every few seconds must not fill the log; a
            # reconnect the operator asked for must always say what happened.
            self.open(level=logging.INFO if force else logging.DEBUG)
        except Exception as e:
            if force:
                self._record_error(f"reconnect: {e}")

    # ---------- recording ----------
    def start_recording(self, name: str, timestamp: bool = True,
                        run_number: int | None = None,
                        max_events: int | None = None) -> dict:
        """Begin writing to a new run directory, starting acquisition if the
        operator has not already. Watching and recording are separate actions.

        The run number is the analysis-facing identity (run_<N>.root): given
        explicitly it is taken as-is; otherwise it is one past the highest
        number already in the data directory. With `max_events` the recording
        closes itself after exactly that many events - the bounded capture for
        "give me N triggers to look at" - while acquisition keeps running."""
        if self._writer is not None:
            return {"ok": False, "error": "already recording"}
        if not self._opened:
            return {"ok": False, "error": "no unit connected"}
        if self.calibrator.is_active():
            # A run recorded while the servo is moving baselines is garbage
            # with a straight face; refuse rather than let it happen.
            return {"ok": False, "error": "calibration in progress - wait for it"}
        if run_number is None:
            run_number = runs.next_run_number()
        self._rec_limit = max(1, int(max_events)) if max_events else None
        with logsetup.step(log, f"Starting a recording named {name!r} "
                                f"(run {run_number})") as rec:
            # Acquisition must actually be running, or this opens a run that can
            # never receive an event and reports it as a success.
            if not self._running.is_set() and not self.start():
                rec.done("Not started: acquisition would not start")
                return {"ok": False,
                        "error": "acquisition would not start - see the errors below"}
            try:
                run_id, path = runs.create(name, timestamp)
            except FileExistsError as e:
                rec.done(f"Not started: a run named {e.args[0]!r} already exists")
                return {"ok": False,
                        "error": f"a run named {e.args[0]!r} already exists - "
                                 f"rename it or switch the timestamp on"}
            except OSError as e:
                rec.done(f"Not started: could not create the run directory: {e}")
                self._record_error(f"record: {e}")
                return {"ok": False, "error": f"could not create the run directory: {e}"}
            with self._lock:
                cfg = self._cfg
            writer = make_writer(path, run_id, cfg.output_format, run_number)
            try:
                writer.open(cfg)
                logsetup.did(log, "Creating the run directory", path)
            except Exception as e:
                # The directory exists but holds nothing; leaving it behind puts
                # an empty run in the listing that was never recorded.
                runs.discard_empty(run_id)
                rec.done(f"Not started: could not open the run files: {e}")
                self._record_error(f"record: {e}")
                return {"ok": False, "error": str(e)}
            rec.done(f"Recording to {run_id}")
        self._recorded = 0
        self._run_started = time.time()
        self._run_id = run_id
        self._writer = writer          # last: the loop starts writing here
        return {"ok": True, "run": run_id}

    def stop_recording(self) -> dict:
        w, run_id = self._writer, self._run_id
        self._writer = None            # first: the loop stops writing
        if w is None:
            return {"ok": False, "error": "not recording"}
        with logsetup.step(log, f"Closing the recording {run_id!r}") as closing:
            try:
                w.close()
            except Exception as e:
                log.error("%sThe writer would not close: %s", "  ", e)
                self._record_error(f"record close: {e}")
            closing.done(f"Wrote {self._recorded} events")
        self._run_id = None
        self._run_started = None
        return {"ok": True, "run": run_id}

    # ---------- readout loop ----------
    def _loop(self):
        try:
            self._read_loop()
        except Exception as e:
            # This thread has no owner to raise into. Left unhandled, it died in
            # silence: events stopped arriving while the UI went on saying
            # "acquiring", and nothing anywhere said why.
            log.exception("The readout thread stopped unexpectedly")
            self._record_error(f"readout stopped: {e}")
            self._running.clear()
        finally:
            if self._writer is not None:
                self._end_recording_from_loop(
                    "board stopped responding" if not self._opened
                    else "readout stopped")

    def _read_loop(self):
        fails = 0
        while self._running.is_set():
            self._fire_due_software_trigger()
            try:
                events = self._backend.read_events()
                fails = 0
            except Exception as e:
                fails += 1
                self._record_error(f"read: {e}")
                if fails >= C.READ_FAIL_LIMIT:
                    self._record_error("board stopped responding - acquisition halted")
                    self._opened = False
                    self._board_info = BoardInfo()
                    self._running.clear()
                    break
                time.sleep(0.05)
                continue
            if not events:
                time.sleep(0.002)
                continue
            t = time.monotonic()
            for ev in events:
                self._events_seen += 1
                refresh_last = self._scope_gate(ev)
                for ch, wave in ev.samples.items():
                    self._avg.add(ch, wave, t)
                    if refresh_last:
                        self._last[ch] = (ev.index, wave)
                col = self._stats_col
                if col is not None:
                    col.add(ev)
                # A write failure is a DISK failure. Reporting it as a read
                # error blamed the board for a full or unwritable filesystem,
                # and ten of them halted a perfectly healthy acquisition.
                writer = self._writer
                if writer is not None:
                    try:
                        writer.write(ev)
                        self._recorded += 1
                    except Exception as e:
                        self._end_recording_from_loop(f"could not write: {e}")
                    else:
                        if self._rec_limit and self._recorded >= self._rec_limit:
                            # The bounded capture is complete: close the run and
                            # keep acquiring, so the operator can keep watching.
                            self.stop_recording()
            self._rate.add(len(events))

    def _end_recording_from_loop(self, why: str):
        """Close a recording from the readout thread and say why it stopped."""
        w, run_id = self._writer, self._run_id
        self._writer = None            # first: nothing else tries to write
        if w is None:
            return
        try:
            w.close()
        except Exception as e:
            self._record_error(f"record close: {e}")
        self._record_error(f"recording {run_id!r} cut short: {why}")
        log.error("Recording %r stopped after %d events: %s",
                  run_id, self._recorded, why)
        self._run_id = None
        self._run_started = None

    def _record_error(self, msg: str):
        with self._lock:
            self._errors.append(f"{time.strftime('%H:%M:%S')} {msg}")
            self._errors = self._errors[-50:]

    # ---------- telemetry ----------
    def telemetry(self, _channels=None) -> dict:
        with self._lock:
            cfg = self._cfg
            scope = self._scope_hz is not None
        chans = cfg.enabled_channels()
        dt = C.sample_period_ns(cfg.drs4_frequency)
        # The digitized TR trace rides along as 16+group when enabled.
        shown = list(chans)
        if cfg.fast_trigger_digitizing:
            shown += [16 + gr for gr, g in enumerate(cfg.groups) if g.enabled]
        channels = {}
        for ch in shown:
            mean, count = self._avg.snapshot(ch)
            if mean is None:
                channels[str(ch)] = {"count": 0}
                continue
            vpp = float(mean.max() - mean.min())
            entry = {
                "wave": decimate(mean, C.OVERVIEW_POINTS),
                "count": count,
                "vpp": vpp,
                "min": float(mean.min()),
                "max": float(mean.max()),
                "baseline": float(np.median(mean)),
            }
            last = self._last.get(ch)
            if last is not None:
                # One single-event trace per tick for the overlay display; the
                # id lets the client add each event once, not once per render.
                # Scope mode ships the trace at FULL resolution: the block-mean
                # decimation that keeps the wire light also averages away the
                # very noise a scope exists to show.
                entry["last"] = (last[1].astype(float).tolist() if scope
                                 else decimate(last[1], C.OVERVIEW_POINTS))
                entry["last_index"] = last[0]
                # Peak-to-peak of the FULL single event, before decimation:
                # the liveness discriminator. A live channel always shows its
                # noise floor here; averaging erases dark pulses and noise
                # alike, so the averaged vpp cannot tell quiet from dead.
                entry["last_vpp"] = float(last[1].max() - last[1].min())
            channels[str(ch)] = entry
        return {
            "running": self._running.is_set(),
            "sample_period_ns": dt,
            "record_length": cfg.record_length,
            "overview_points": C.OVERVIEW_POINTS,
            "avg_window_s": self._avg.window_s,
            "events_seen": self._events_seen,
            "recording": self._writer is not None,
            "run_id": self._run_id,
            "run_started": self._run_started,
            "recorded": self._recorded,
            "enabled_channels": chans,
            "channels": channels,
            "rate": self._rate.snapshot(),
        }

    def status(self) -> dict:
        bi = self._board_info
        return {
            "opened": self._opened,
            "running": self._running.is_set(),
            "backend": "caen",
            "board": {
                "model": bi.model, "family": bi.family_code, "serial": bi.serial,
                "roc_firmware": bi.roc_firmware, "amc_firmware": bi.amc_firmware,
                "sw_release": bi.sw_release,
            },
            "events_seen": self._events_seen,
            "sw_triggers_pending": self._sw_pending,
            "scope_hz": self._scope_hz,
            "scope_trigger": self._scope_trigger,
            "recording": self._writer is not None,
            "run_id": self._run_id,
            "run_started": self._run_started,
            "recorded": self._recorded,
            "data_dir": runs.DATA_ROOT,
            "next_run_number": runs.next_run_number(),
            "errors": list(self._errors),
        }
