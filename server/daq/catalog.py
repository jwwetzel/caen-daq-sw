"""Browsable command/setting catalog, organized by tier (unit / bank / channel).

Drives the UI. Every entry carries a `help` written for the operator - what the
setting does and what it costs - not a restatement of the CAEN function name.
The function name rides along separately for anyone reading the code.

`type: "volts"` means the wire value is a 16-bit DAC word but the UI must show
volts; nothing human-facing should present a raw DAC integer.

`required: True` marks the settings every run must have deliberately chosen -
the UI lists those first, and gates the rest behind a per-setting checkbox
that pins an untouched setting to its default. The defaults themselves are
injected in catalog() from default_config(), never written here, so the
catalog can not drift from the config it describes.
"""
from __future__ import annotations

from . import constants as C
from .config import default_config

FREQ_CHOICES = [{"value": k, "label": v[0]} for k, v in C.DRS4_FREQUENCIES.items()]
TRIG_MODES = [{"value": "disabled", "label": "Disabled"},
              {"value": "acquisition_only", "label": "Acquisition only"},
              {"value": "acq_and_trgout", "label": "Acq + TRG-OUT"}]

UNIT_SETTINGS = [
    {"key": "drs4_frequency", "required": True, "label": "Sampling frequency", "type": "enum",
     "choices": FREQ_CHOICES, "caen": "CAEN_DGTZ_SetDRS4SamplingFrequency",
     "help": "How fast the DRS4 samples.\n\n"
             "The record is always 1024 cells, so this sets the time "
             "resolution and the window length together - one cell is one "
             "sample period, and 1024 of them is the whole record.\n\n"
             "5 GS/s - 0.2 ns per cell, 204.8 ns window\n"
             "2.5 GS/s - 0.4 ns per cell, 409.6 ns window\n"
             "1 GS/s - 1.0 ns per cell, 1.02 us window\n"
             "750 MS/s - 1.33 ns per cell, 1.37 us window\n\n"
             "Changing it reloads the correction tables and changes which "
             "post-trigger settings are reachable."},
    {"key": "post_trigger", "required": True, "label": "Post-trigger duration", "type": "steps",
     "depends_on": "drs4_frequency",
     "values_by_freq": {
         str(f): [{"pct": p, "ns": round(p / 100.0 * C.record_ns(f), 2)}
                  for p in C.post_trigger_steps(f)]
         for f in C.DRS4_FREQUENCIES
     },
     "record_ns_by_freq": {str(f): round(C.record_ns(f), 2) for f in C.DRS4_FREQUENCIES},
     "caen": "CAEN_DGTZ_SetPostTriggerSize",
     "help": "How much of the record is captured AFTER the trigger fires.\n\n"
             "0 ns - trigger at the very end, nothing after it\n"
             "half - trigger centred in the record\n"
             "full - trigger at the very start, nothing before it\n\n"
             "The register moves in ~8.5 ns steps and the API takes a whole "
             "percent, so the smallest increment depends on the sampling "
             "frequency:\n\n"
             "5 GS/s - 8.5 ns, 25 settings\n"
             "2.5 GS/s - 8.5 ns, 49 settings\n"
             "1 GS/s - 10.24 ns, every 1%\n"
             "750 MS/s - 13.65 ns, every 1%\n\n"
             "The arrows walk exactly those settings."},
    {"key": "correction_level", "label": "DRS4 correction", "type": "enum",
     "choices": [{"value": "timing", "label": "Amplitude + true times"},
                 {"value": "auto", "label": "Auto"},
                 {"value": "disabled", "label": "Disabled"},
                 {"value": "manual", "label": "Manual tables"}],
     "caen": "CAEN_DGTZ_LoadDRS4CorrectionData / GetCorrectionTables",
     "help": "The DRS4 needs cell-by-cell correction before its samples mean "
             "anything - each capacitor has its own offset, timing and peak "
             "error.\n\n"
             "Amplitude + true times (default) - amplitude corrections with "
             "the samples untouched in time; each event's true non-uniform "
             "time axis is recorded alongside. Full ps-level timing "
             "precision\n"
             "Auto - CAEN's full correction during decode. Its time step "
             "RESAMPLES onto a uniform grid, smoothing pulse edges slightly; "
             "use it when downstream code assumes uniform sampling\n"
             "Disabled - raw cells, for diagnosing the chip itself\n"
             "Manual tables - supply your own"},
    {"key": "trigger_edge", "required": True, "label": "Trigger edge", "type": "enum",
     "choices": [{"value": "rising", "label": "Rising"}, {"value": "falling", "label": "Falling"}],
     "caen": "CAEN_DGTZ_SetTriggerPolarity",
     "help": "Which way the signal must cross the threshold to fire.\n\n"
             "Rising - for positive-going pulses\n"
             "Falling - for negative-going pulses (PMTs, NIM)\n\n"
             "Despite the per-channel API, this unit applies one edge to "
             "every channel."},
    {"key": "external_trigger", "required": True, "label": "External trigger (TRG-IN)", "type": "enum",
     "choices": TRIG_MODES, "caen": "CAEN_DGTZ_SetExtTriggerInputMode",
     "help": "Accept triggers on the front-panel TRG-IN connector.\n\n"
             "Disabled - ignore TRG-IN\n"
             "Acquisition only - trigger on it\n"
             "Acq + TRG-OUT - trigger, and pass it out for chaining\n\n"
             "Carries about 115 ns of delay, against ~42 ns on the TR inputs."},
    {"key": "fast_trigger", "required": True, "label": "Fast trigger (TR0/TR1)", "type": "enum",
     "choices": TRIG_MODES[:2], "caen": "CAEN_DGTZ_SetFastTriggerMode",
     "help": "Trigger from the dedicated TR inputs - the low-latency path, and "
             "the usual choice for timing work.\n\n"
             "Disabled - ignore the TR inputs\n"
             "Acquisition only - trigger on them\n\n"
             "TR0 serves bank 0, TR1 serves bank 1. Each has its own "
             "threshold and DC offset under Bank Settings. Carries about "
             "42 ns of delay."},
    {"key": "software_trigger", "label": "Software trigger", "type": "enum",
     "choices": TRIG_MODES + [{"value": "extout_only", "label": "TRG-OUT only"}],
     "caen": "CAEN_DGTZ_SetSWTriggerMode",
     "help": "What a trigger sent from this app does. The bench source when "
             "nothing external can trigger the board - the 742 has no "
             "channel self-trigger.\n\n"
             "Disabled - software triggers are ignored\n"
             "Acquisition only - capture an event\n"
             "Acq + TRG-OUT - capture, and pulse it out on GPO/TRG-OUT\n"
             "TRG-OUT only - pulse the connector without capturing, for "
             "checking cabling downstream."},
    {"key": "fast_trigger_digitizing", "label": "Digitize TR traces", "type": "bool",
     "caen": "CAEN_DGTZ_SetFastTriggerDigitizing",
     "help": "Record the TR inputs alongside the channels, giving a timing "
             "reference in the data.\n\n"
             "Costs conversion time: dead time per event rises from 110 us "
             "to 181 us."},
    {"key": "io_level", "label": "Front-panel level (GPO, TRG-IN)", "type": "enum",
     "choices": [{"value": "nim", "label": "NIM"}, {"value": "ttl", "label": "TTL"}],
     "caen": "CAEN_DGTZ_SetIOLevel",
     "help": "Electrical standard of the front-panel LEMO connectors - the "
             "GPO/TRG-OUT output and the TRG-IN input switch together.\n\n"
             "NIM - negative logic, the usual choice with NIM crates and PMTs\n"
             "TTL - positive logic\n\n"
             "Match what the cabling expects, especially before trusting the "
             "GPO pulse downstream."},
    {"key": "gpo_output", "label": "GPO output", "type": "enum",
     "choices": [{"value": "trigger", "label": "Trigger out"},
                 {"value": "busy", "label": "Busy"},
                 {"value": "run", "label": "Run"}],
     "caen": "register 0x811C bits[20:14], UM5698 sec 1.25 (docs/)",
     "help": "What the GPO connector emits.\n\n"
             "Trigger out - the trigger, for chaining boards or timing\n"
             "Busy - high while the board cannot take another event: the "
             "dead-time veto for downstream electronics\n"
             "Run - high while acquisition runs, for daisy-chained "
             "start/stop\n\n"
             "The electrical standard follows the front-panel level setting "
             "above."},
    {"key": "max_events_blt", "label": "Events per readout", "type": "int",
     "min": 1, "max": 1023, "caen": "CAEN_DGTZ_SetMaxNumEventsBLT",
     "help": "Upper bound on how many events one readout may return - a cap, "
             "not a fixed batch, so a read gives you whatever is waiting up "
             "to this.\n\n"
             "Higher means fewer, larger transfers and better throughput at "
             "high rates; lower means the display updates sooner at low "
             "rates. Not the same thing as the unit's 1024-event buffer."},
    {"key": "output_format", "label": "Dump format", "type": "enum",
     "choices": [{"value": "ascii", "label": "ASCII"},
                 {"value": "binary", "label": "Binary"},
                 {"value": "root", "label": "ROOT"}],
     "help": "How samples are written to disk.\n\n"
             "ASCII - one decimal per line; readable, ~6x larger, slower\n"
             "Binary - WaveDump .dat, compact raw; feeds the drs2root "
             "converter\n"
             "ROOT - one waveforms.root per run, TTree 'pulse' in the "
             "RADiCAL testbeam layout (event, channel[18][1024], times) - "
             "straight into the analysis, no conversion step"},
    {"key": "output_header", "label": "Dump header", "type": "bool",
     "help": "Prepend WaveDump's per-event header (event size, channel, "
             "counter, trigger time tag).\n\n"
             "Needed to tell events apart in a binary file; leave it off for "
             "a bare column of samples."},
]

# Per DRS4 group of 8 channels.
BANK_SETTINGS = [
    {"key": "enabled", "label": "Bank enabled", "type": "bool",
     "caen": "CAEN_DGTZ_SetGroupEnableMask",
     "help": "The DRS4 digitizes all 8 channels of a bank together, so there "
             "is no per-channel enable.\n\n"
             "Disabling a bank you are not using cuts readout time and file "
             "size."},
    # The TR input has its own DAC calibrations, distinct from the channels'.
    # lsb_v/zero_dac drive the UI's volts conversion. The THRESHOLD uses the
    # calibration MEASURED on serial 53364 (2026-08-28, spectrum-edge method:
    # the sharp lower edge of the accepted MCP pulse-depth distribution vs
    # the DAC): 0.00494 mV/LSB, rel = 0 extrapolating to DAC 78670. The
    # RADiCAL bench constant (0.0329 mV/LSB, zero 25448) is SEVEN times off
    # for this comparator - typing "-140 mV" through it once armed a -235 mV
    # cut. The offset's bench numbers, by contrast, agree with the digitized
    # data and stand.
    {"key": "fast_trigger_threshold", "label": "TR threshold", "type": "volts",
     "lsb_v": 4.94e-6, "zero_dac": 78670,
     "caen": "CAEN_DGTZ_SetGroupFastTriggerThreshold",
     "help": "How far below its baseline the TR input must dip to fire the "
             "fast trigger (falling edge) - measured calibration for this "
             "unit, so what you set is where the pulse-depth spectrum will "
             "cut.\n\n"
             "Set it well inside your pulse amplitude but clear of the "
             "baseline noise.\n\n"
             "On the DT5742B both banks configure the same TR0 input - keep "
             "them equal. Shallower than about -65 mV is beyond the DAC "
             "range on this unit."},
    {"key": "fast_trigger_dc_offset", "label": "TR DC offset", "type": "volts",
     "lsb_v": -4.66e-5, "zero_dac": 33540,
     "caen": "CAEN_DGTZ_SetGroupFastTriggerDCOffset",
     "help": "Shifts the TR input's own baseline so the threshold has room "
             "to sit.\n\n"
             "Leave it near midscale for NIM and other negative pulses; "
             "raise it for positive signals. Volts use the TR path's "
             "measured calibration (-0.0466 mV per DAC step, zero at "
             "33540)."},
]

CHANNEL_SETTINGS = [
    {"key": "dc_offset", "label": "DC offset", "type": "volts",
     "caen": "CAEN_DGTZ_SetChannelDCOffset",
     "help": "Moves this channel's baseline within the 1 Vpp window so the "
             "pulse fits without clipping.\n\n"
             "The DAC covers +/-1 V - twice the window - so only about half "
             "its travel keeps the channel in view."},
]


def catalog() -> dict:
    defaults = default_config().to_dict()
    return {
        "unit": [{**s, "default": defaults.get(s["key"])} for s in UNIT_SETTINGS],
        "bank": BANK_SETTINGS,
        "channel": CHANNEL_SETTINGS,
        "geometry": {
            "num_channels": C.NUM_CHANNELS,
            "group_size": C.GROUP_SIZE,
            "num_groups": C.NUM_GROUPS,
            "record_length": C.RECORD_LENGTH,
            "adc_max": C.ADC_MAX,
            "input_range_vpp": C.INPUT_RANGE_VPP,
            "dc_offset_max": C.DC_OFFSET_MAX,
            "dc_offset_range_v": C.DC_OFFSET_RANGE_V,
            "dc_offset_mid": C.DC_OFFSET_MID,
        },
    }
