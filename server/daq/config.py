"""Configuration model with the 742's real setting tiers: board / bank(group) /
channel. Defaults mirror CAEN's WaveDump and are only a seed: once a board is
open, its own settings are the truth, so nothing is persisted between runs.

Tiers (verified against WaveDump.c x742 branch):
  board   : sampling freq, post-trigger, correction, trigger modes, output
  bank    : enable, self-trigger threshold, fast-trigger (TR) threshold + DC
            offset, self-trigger mode  -> per DRS4 group of 8 channels
  channel : DC-offset trim only (SetChannelDCOffset). No per-channel enable
            (whole group digitizes together) and no per-channel gain on the 742.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict

from . import constants as C


def _clamp_int(value, lo: int, hi: int, fallback: int) -> int:
    """An integer inside [lo, hi], or `fallback` if it is not a number at all.

    Config values reach here from a hand-edited file, a WaveDump file and the
    browser, so none of them can be trusted to be in range - and an out-of-range
    one does not stop at the hardware. A bad drs4_frequency used to reach
    C.sample_period_ns() and raise KeyError inside the telemetry feed, which
    closed the websocket and left the UI simply not updating.
    """
    try:
        v = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(lo, min(hi, v))


def _one_of(value, allowed: tuple[str, ...], fallback: str) -> str:
    v = str(value).lower() if value is not None else ""
    return v if v in allowed else fallback


# These allow-lists must cover every choice the catalog offers, or the UI
# shows a selection silently reverting: _one_of coerces an unknown value to
# the fallback with no error anywhere. A merge once brought this validation
# layer in without "timing" and "root", and both features switched
# themselves off - test_smoke pins the catalog's choices to these lists.
CORRECTION_LEVELS = ("auto", "disabled", "manual", "timing")
TRIGGER_EDGES = ("rising", "falling")
TRIGGER_MODES = ("disabled", "acquisition_only", "extout_only", "acq_and_trgout")
OUTPUT_FORMATS = ("ascii", "binary", "root")

@dataclass
class ChannelConfig:
    # Unsigned 16-bit DAC word, matching CAEN_DGTZ_SetChannelDCOffset's uint32_t.
    dc_offset: int = C.DC_OFFSET_MID
    # Operator label, e.g. "Upstream". The UI shows it as "CH 3 - Upstream", but
    # that prefix is presentation only and never reaches the recording.
    name: str = ""

    def __post_init__(self):
        self.dc_offset = _clamp_int(self.dc_offset, 0, C.DC_OFFSET_MAX,
                                    C.DC_OFFSET_MID)
        self.name = "" if self.name is None else str(self.name)[:64]


@dataclass
class GroupConfig:
    enabled: bool = False
    fast_trigger_threshold: int = 20000     # SetGroupFastTriggerThreshold (TR0/TR1)
    fast_trigger_dc_offset: int = 32768     # SetGroupFastTriggerDCOffset

    def __post_init__(self):
        self.enabled = bool(self.enabled)
        self.fast_trigger_threshold = _clamp_int(
            self.fast_trigger_threshold, 0, C.DC_OFFSET_MAX, 20000)
        self.fast_trigger_dc_offset = _clamp_int(
            self.fast_trigger_dc_offset, 0, C.DC_OFFSET_MAX, C.DC_OFFSET_MID)


@dataclass
class BoardConfig:
    # board-level
    drs4_frequency: int = C.DEFAULT_DRS4_FREQUENCY
    record_length: int = C.RECORD_LENGTH
    post_trigger: int = 20
    # "timing" by default: amplitude corrections plus each event's true
    # non-uniform time axis. The library's "auto" path resamples onto a
    # uniform grid, which costs ps-level timing precision - and this group's
    # program is a timing program.
    correction_level: str = "timing"
    trigger_edge: str = "falling"
    external_trigger: str = "acquisition_only"
    fast_trigger: str = "acquisition_only"
    # What a software trigger (POST /api/trigger) does: acquire, pulse the
    # GPO/TRG-OUT connector, or both. Same mode vocabulary as the other two.
    software_trigger: str = "acquisition_only"
    fast_trigger_digitizing: bool = True
    # Electrical standard of the front-panel LEMOs (GPO/TRG-OUT and TRG-IN).
    io_level: str = "nim"
    # What the GPO connector emits: the trigger, the board's BUSY (dead-time
    # veto for downstream electronics), or RUN (for daisy-chained start/stop).
    gpo_output: str = "trigger"
    max_events_blt: int = 1023   # 1024 is silently clamped to this; see CLAUDE.md
    test_pattern: bool = False
    # output. ROOT by default: the analysis
    # (gitlab.cern.ch/ledovsk/tb_fnal_radical) reads it directly, so the
    # conversion step disappears. ASCII/binary remain for WaveDump parity.
    output_format: str = "root"
    output_header: bool = False
    # tiers
    groups: list[GroupConfig] = field(
        default_factory=lambda: [GroupConfig() for _ in range(C.NUM_GROUPS)])
    channels: list[ChannelConfig] = field(
        default_factory=lambda: [ChannelConfig() for _ in range(C.NUM_CHANNELS)])

    def __post_init__(self):
        # Board-level values are range-checked here rather than at each use: the
        # rest of the app indexes tables with them, so an out-of-range one is a
        # crash somewhere far from where it was introduced.
        freq = _clamp_int(self.drs4_frequency, 0, max(C.DRS4_FREQUENCIES),
                          C.DEFAULT_DRS4_FREQUENCY)
        self.drs4_frequency = freq if freq in C.DRS4_FREQUENCIES \
            else C.DEFAULT_DRS4_FREQUENCY
        self.record_length = C.RECORD_LENGTH        # fixed by the DRS4
        self.post_trigger = _clamp_int(self.post_trigger, 0, 100, 0)
        self.max_events_blt = _clamp_int(self.max_events_blt, 1, 1023, 1023)
        self.correction_level = _one_of(self.correction_level, CORRECTION_LEVELS, "auto")
        self.trigger_edge = _one_of(self.trigger_edge, TRIGGER_EDGES, "falling")
        self.external_trigger = _one_of(self.external_trigger, TRIGGER_MODES,
                                        "acquisition_only")
        self.fast_trigger = _one_of(self.fast_trigger, TRIGGER_MODES, "acquisition_only")
        self.fast_trigger_digitizing = bool(self.fast_trigger_digitizing)
        self.output_format = _one_of(self.output_format, OUTPUT_FORMATS, "ascii")
        self.output_header = bool(self.output_header)

        self.groups = [g if isinstance(g, GroupConfig) else GroupConfig(**g)
                       for g in self.groups][:C.NUM_GROUPS]
        while len(self.groups) < C.NUM_GROUPS:
            self.groups.append(GroupConfig())
        chs = []
        for i in range(C.NUM_CHANNELS):
            c = self.channels[i] if i < len(self.channels) else ChannelConfig()
            chs.append(c if isinstance(c, ChannelConfig) else ChannelConfig(**c))
        self.channels = chs

    # ---- derived ----
    @property
    def group_enable_mask(self) -> int:
        return sum((1 << g) for g, gc in enumerate(self.groups) if gc.enabled)

    def channel_enabled(self, ch: int) -> bool:
        return self.groups[C.channel_group(ch)].enabled

    def enabled_channels(self) -> list[int]:
        return [ch for ch in range(C.NUM_CHANNELS) if self.channel_enabled(ch)]

    def bank_channels(self, group: int) -> list[int]:
        base = group * C.GROUP_SIZE
        return list(range(base, base + C.GROUP_SIZE))

    # ---- serialization ----
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "BoardConfig":
        d = dict(d)
        groups = d.pop("groups", None)
        chs = d.pop("channels", None)
        # tolerate unknown keys from older configs
        known = cls().__dict__
        d = {k: v for k, v in d.items() if k in known}
        cfg = cls(**d)
        if groups is not None:
            cfg.groups = [GroupConfig(**g) if isinstance(g, dict) else g for g in groups]
        if chs is not None:
            cfg.channels = [ChannelConfig(**c) if isinstance(c, dict) else c for c in chs]
        cfg.__post_init__()
        return cfg


def default_config() -> BoardConfig:
    """WaveDump-equivalent: group 0 enabled (ch0-7), 5 GS/s, corrections AUTO."""
    cfg = BoardConfig()
    cfg.groups[0].enabled = True
    return cfg
