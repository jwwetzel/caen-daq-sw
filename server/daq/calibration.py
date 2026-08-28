"""Closed-loop channel calibration: the board's own response is the truth.

Hand-setting DC offsets leans on the nominal DAC arithmetic, and the nominal
arithmetic is measurably wrong on this unit - midscale sits at +145 mV, the
slope is 10% off, and both vary channel to channel (Configuration B's
hand-computed offsets railed six channels). A servo does not care: measure
the actual baseline, step the DAC, measure again.

Two phases, both polarity-agnostic by design - some positive pulses carry a
negative afterpulse, so no polarity flag can be trusted; the data says where
the pulse goes, not a setting:

- "baseline": no signal knowledge. Software triggers, and every enabled
  channel's baseline (TR0's too) is servoed to the window centre - the
  neutral start with the most symmetric headroom.
- "fit": real triggers flowing. Measure each channel's actual excursions in
  both directions, then place the baseline so the WHOLE pulse - afterpulses
  included - sits inside the window with margin on both sides. A pulse
  bigger than the window is reported as no_fit: no offset can fix that, only
  attenuation.

The servo slope starts from the nominal counts-per-LSB and switches to the
measured secant after the first step, so each channel calibrates its own
response - which is also what lets the TR path, with its different DAC,
share the same loop.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from . import constants as C
from . import logsetup

log = logsetup.get("daq.calib")

ADC_TOP = C.ADC_MAX                  # 4095
CENTER = (C.ADC_MAX + 1) / 2         # 2048
TOL_COUNTS = 20                      # ~5 mV: within the noise, out of the way
MAX_ITER = 6
MARGIN_FRAC = 0.05                   # spare window kept clear on each side
RAIL_LO, RAIL_HI = 2, ADC_TOP - 2    # excursions here mean "clipped, or worse"
# Nominal servo slopes in ADC counts per DAC LSB (negative: a larger DAC word
# lowers the baseline). Replaced by the measured secant after one step.
SLOPE_CH = -0.125
SLOPE_TR = -0.19
# TR threshold coupling, MEASURED on serial 53364 (2026-08-28, spectrum-edge
# calibration): the comparator's true response is ~0.00494 mV per threshold
# LSB - seven times more compressed than the RADiCAL bench constant - while
# the offset moves the input by -0.0464 mV/LSB. Keeping the operator's margin
# across an offset move therefore takes ~9.4 threshold LSBs per offset LSB.
TR_THR_TRUE_MV_PER_LSB = 0.00494
TR_COUPLING_LSB_PER_OFF_LSB = -0.0464 / TR_THR_TRUE_MV_PER_LSB

TR_KEY = "TR0"


class _Cancelled(Exception):
    """The operator asked the run to stop; not an error."""


def _counts_to_mv(counts: float) -> float:
    return (counts - CENTER) / (C.ADC_MAX + 1) * 1000.0


@dataclass
class _Servo:
    """One DAC being steered: a signal channel or the shared TR0 offset."""
    key: str                     # "0".."15" or TR_KEY
    stat_ch: int                 # where its baseline is measured in events
    target: float                # counts
    slope: float
    dac: int = 0
    baseline: float | None = None
    prev: tuple[int, float] | None = None    # (dac, baseline) for the secant
    status: str = "adjusting"
    below: float = 0.0           # measured excursions, fit phase
    above: float = 0.0

    def report(self) -> dict:
        return {"channel": f"CH {self.key}" if self.key != TR_KEY else TR_KEY,
                "dac": self.dac,
                "baseline_mv": round(_counts_to_mv(self.baseline), 1)
                if self.baseline is not None else None,
                "below_mv": round(self.below / (C.ADC_MAX + 1) * 1000.0, 1),
                "above_mv": round(self.above / (C.ADC_MAX + 1) * 1000.0, 1),
                "status": self.status}


class Calibrator:
    """Owns one calibration run at a time; state is what the UI polls."""

    # Overridable per instance - the tests shrink them to keep suites fast,
    # and the UI passes the operator's count for a fit run.
    baseline_events = 24
    fit_events = 100
    # Event-count-driven, not time-driven: a measurement waits for its events
    # however long they take. The only clock is the stall detector - this
    # long with NO events means nothing is triggering, and the run stops with
    # an honest count instead of fitting on scraps.
    stall_s = 30.0
    # After an adjustment pass, 17 DAC writes sit on the mezzanine's slow SPI
    # and the baselines SLEW through the next moments. Measuring during the
    # slew poisoned every number on the first live run - quiet channels
    # reported 250 mV "excursions" that were the baseline in flight, and the
    # secant learned garbage slopes from them. Let the board settle first.
    settle_s = 0.6

    def __init__(self, engine):
        self._engine = engine
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._abort = threading.Event()
        self._state = {"active": False, "phase": None, "message": "",
                       "iteration": 0, "report": [], "error": None}

    def status(self) -> dict:
        with self._lock:
            return dict(self._state, report=list(self._state["report"]))

    def is_active(self) -> bool:
        with self._lock:
            return self._state["active"]

    def start(self, mode: str, events: int | None = None) -> dict:
        if mode not in ("baseline", "fit"):
            return {"ok": False, "error": f"unknown calibration mode {mode!r}"}
        if events:
            events = max(4, min(100_000, int(events)))
            if mode == "fit":
                self.fit_events = events
            else:
                self.baseline_events = events
        if self._engine.status()["recording"]:
            return {"ok": False, "error": "a run is recording - stop it first"}
        with self._lock:
            if self._state["active"]:
                return {"ok": False, "error": "a calibration is already running"}
            self._state = {"active": True, "phase": mode, "message": "starting",
                           "iteration": 0, "report": [], "error": None}
        self._abort.clear()
        self._thread = threading.Thread(target=self._run, args=(mode,),
                                        name="calib", daemon=True)
        self._thread.start()
        return {"ok": True}

    def cancel(self) -> dict:
        """End a run at the next safe point; the board keeps whatever the
        last completed pass wrote - never a half-applied one."""
        if not self.is_active():
            return {"ok": False, "error": "no calibration is running"}
        self._abort.set()
        return {"ok": True}

    # ---------- the run ----------
    def _run(self, mode: str) -> None:
        try:
            with logsetup.step(log, f"Calibrating ({mode})") as step:
                servos = self._make_servos()
                if mode == "baseline":
                    for s in servos:
                        s.target = CENTER
                    self._servo(servos, fire_sw=True)
                else:
                    self._fit(servos)
                bad = [s.key for s in servos if s.status != "ok"]
                step.done(f"{len(servos) - len(bad)} of {len(servos)} channels ok"
                          + (f"; needs attention: {', '.join(bad)}" if bad else ""))
        except _Cancelled:
            log.info("calibration cancelled")
            with self._lock:
                self._state["message"] = "cancelled"
                self._state["active"] = False
            return
        except Exception as e:
            log.error("calibration failed: %s", e)
            with self._lock:
                self._state["error"] = str(e)
        finally:
            with self._lock:
                self._state["active"] = False
                if self._state["message"] != "cancelled":
                    self._state["message"] = "done"

    def _say(self, msg: str, iteration: int | None = None) -> None:
        with self._lock:
            self._state["message"] = msg
            if iteration is not None:
                self._state["iteration"] = iteration

    def _publish(self, servos: list[_Servo]) -> None:
        with self._lock:
            self._state["report"] = [s.report() for s in servos]

    def _make_servos(self) -> list[_Servo]:
        cfg = self._engine.get_config()
        servos = [
            _Servo(key=str(ch), stat_ch=ch, target=CENTER, slope=SLOPE_CH,
                   dac=cfg.channels[ch].dc_offset)
            for ch in cfg.enabled_channels()
        ]
        if cfg.fast_trigger_digitizing:
            gr = next((g for g, gc in enumerate(cfg.groups) if gc.enabled), None)
            if gr is not None:
                servos.append(_Servo(key=TR_KEY, stat_ch=16 + gr, target=CENTER,
                                     slope=SLOPE_TR,
                                     dac=cfg.groups[gr].fast_trigger_dc_offset))
        if not servos:
            raise RuntimeError("no enabled channels to calibrate")
        return servos

    def _measure(self, servos: list[_Servo], events: int, fire_sw: bool) -> None:
        base_msg = self.status()["message"]
        stats, seen = self._engine.collect_stats(
            events, fire_sw, stall_s=self.stall_s, abort=self._abort,
            progress=lambda n: self._say(f"{base_msg} - {n}/{events} events"))
        if self._abort.is_set():
            raise _Cancelled()
        if seen < events:
            # The stall detector fired: nothing has triggered for stall_s.
            # Better an honest stop than a fit built on scraps.
            raise RuntimeError(
                f"only {seen} of {events} events, then nothing for "
                f"{self.stall_s:.0f}s - is anything triggering?")
        missing = [s.key for s in servos if s.stat_ch not in stats]
        if missing:
            raise RuntimeError("no events carried data for " + ", ".join(missing))
        for s in servos:
            st = stats[s.stat_ch]
            s.baseline = st["baseline"]
            s.below = max(0.0, st["baseline"] - st["min"])
            s.above = max(0.0, st["max"] - st["baseline"])

    def _apply(self, servos: list[_Servo]) -> None:
        """Write the DACs while acquisition is STOPPED, then re-arm.

        Measured on serial 53364: a DC-offset write during acquisition
        updates the register - readback agrees, no error anywhere - but the
        analog output NEVER moves until the next arm. Two servo runs chased
        frozen baselines for six passes each before this was understood. So:
        stop, write, start (arming rewrites every setting while stopped,
        which is when the DACs actually load), and only then measure."""
        eng = self._engine
        eng.stop()
        cfg = eng.get_config()
        for s in servos:
            if s.key == TR_KEY:
                # The trigger margin is the operator's; moving the offset
                # must not change it. Shift the threshold by the same
                # predicted delta the baseline moves - this is exactly the
                # mistake that silenced the trigger the first time
                # auto-baseline ran.
                old = cfg.groups[0].fast_trigger_dc_offset
                d_thr = round(TR_COUPLING_LSB_PER_OFF_LSB * (s.dac - old))
                for g in cfg.groups:      # one TR0, both banks' registers
                    g.fast_trigger_dc_offset = s.dac
                    g.fast_trigger_threshold = int(min(0xFFFF, max(
                        0, g.fast_trigger_threshold + d_thr)))
            else:
                cfg.channels[int(s.key)].dc_offset = s.dac
        got, _ = eng.set_config(cfg)   # write errors already land in status
        for s in servos:                  # the board's answer is the truth
            s.dac = (got.groups[0].fast_trigger_dc_offset if s.key == TR_KEY
                     else got.channels[int(s.key)].dc_offset)
        eng.start()                       # the arm loads the DACs
        time.sleep(self.settle_s)         # and the SPI drains before we judge

    def _servo(self, servos: list[_Servo], fire_sw: bool) -> None:
        """Steer every servo to its target; the measured secant replaces the
        nominal slope as soon as a step's response has been observed."""
        for it in range(1, MAX_ITER + 1):
            self._say(f"measuring baselines (pass {it})", it)
            self._measure(servos, self.baseline_events, fire_sw)
            moving = []
            for s in servos:
                err = s.target - s.baseline
                if s.prev is not None:
                    d_dac, d_base = s.dac - s.prev[0], s.baseline - s.prev[1]
                    if abs(d_dac) >= 8 and abs(d_base) >= 4:
                        cand = d_base / d_dac
                        # A physical slope on this hardware is negative and of
                        # order -0.1 counts/LSB; anything else is a poisoned
                        # measurement and must not steer the loop.
                        if -0.6 <= cand <= -0.03:
                            s.slope = cand
                if abs(err) <= TOL_COUNTS:
                    s.status = "ok"
                    continue
                s.prev = (s.dac, s.baseline)
                want = s.dac + err / s.slope
                s.dac = int(min(C.DC_OFFSET_MAX, max(0, round(want))))
                s.status = ("unreachable"
                            if s.dac in (0, C.DC_OFFSET_MAX) and want != s.dac
                            else "adjusting")
                moving.append(s)
            self._publish(servos)
            if not moving:
                return
            self._say(f"adjusting {len(moving)} channels (pass {it})", it)
            self._apply(moving)
        for s in servos:
            if s.status == "adjusting":
                s.status = "unreachable"
        self._publish(servos)

    def _fit(self, servos: list[_Servo]) -> None:
        """Place each baseline so the measured pulse fits with margin.

        Two rounds: excursions measured against real triggers, targets
        computed, servo run; then re-measured, because a channel that was
        clipping under-reports its extent until the first move un-clips it.
        """
        margin = (C.ADC_MAX + 1) * MARGIN_FRAC
        for round_no in (1, 2):
            self._say(f"measuring pulse extents (round {round_no})")
            self._measure(servos, self.fit_events, fire_sw=False)
            for s in servos:
                lo_rail = (s.baseline - s.below) <= RAIL_LO
                hi_rail = (s.baseline + s.above) >= RAIL_HI
                lo = margin + s.below
                hi = ADC_TOP - margin - s.above
                if lo_rail and not hi_rail:
                    s.target = hi          # clipped below: as high as allowed
                elif hi_rail and not lo_rail:
                    s.target = lo          # clipped above: as low as allowed
                elif lo > hi:
                    s.status = "no_fit"    # bigger than the window: attenuate
                    s.target = CENTER + (s.below - s.above) / 2
                else:
                    s.target = (lo + hi) / 2
            self._servo(servos, fire_sw=False)
        self._say("verifying")
        self._measure(servos, self.fit_events, fire_sw=False)
        for s in servos:
            span = s.below + s.above + 2 * margin
            if span > C.ADC_MAX + 1:
                s.status = "no_fit"
            elif ((s.baseline - s.below) <= RAIL_LO
                  or (s.baseline + s.above) >= RAIL_HI):
                s.status = "clipped"
            elif s.status != "unreachable":
                s.status = "ok"
        self._publish(servos)
