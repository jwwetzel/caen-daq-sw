"""Hardware-free smoke tests. Run: `python -m pytest` or
`python tests/test_smoke.py` from the server/ dir.

These cover the config tiers, the aggregation math, the HTTP surface, and the
runtime record the launcher uses to find a running server.
The acquisition loop itself needs the board and is not covered here."""
import json
import os
import tempfile
import time
import numpy as np
from fastapi.testclient import TestClient

from daq.acquisition import AcquisitionEngine
from daq.config import default_config, BoardConfig
from daq.stats import RollingAverage, TriggerRateMeter, decimate
from daq.server import create_app
from daq import constants as C
from daq import runtime


def _refuse_hardware():
    raise RuntimeError("no unit: this test must never touch hardware")


def _engine_without_a_unit() -> AcquisitionEngine:
    """An engine whose every open fails, exactly like a machine with no board.

    The default factory loads the real libCAENDigitizer, so on a machine with a
    unit attached these "hardware-free" tests would open — or hang on — actual
    hardware. Discovered the hard way: a wedged CAEN USB driver left this suite
    blocked inside OpenDigitizer with no output at all.
    """
    return AcquisitionEngine(backend_factory=_refuse_hardware)


def test_tiers_and_enable_is_per_group():
    cfg = default_config()
    assert cfg.groups[0].enabled and not cfg.groups[1].enabled
    assert cfg.enabled_channels() == list(range(0, 8))
    assert cfg.group_enable_mask == 0b01
    cfg.channels[0].dc_offset = 1234
    cfg.channels[3].name = "Upstream"
    cfg2 = BoardConfig.from_dict(cfg.to_dict())          # survives a round trip
    assert cfg2.channels[0].dc_offset == 1234
    assert cfg2.channels[3].name == "Upstream"
    assert cfg2.groups[0].enabled


def test_rolling_average_matches_numpy():
    avg = RollingAverage(window_s=10.0)
    now = time.monotonic()
    waves = [np.full(8, k, dtype=np.float32) for k in (10, 20, 30)]
    for w in waves:
        avg.add(0, w, t=now)  # all within the (real-clock) window
    mean, count = avg.snapshot(0)
    assert count == 3 and np.allclose(mean, np.mean(waves, axis=0))


def test_decimate():
    w = np.arange(1024, dtype=np.float32)
    assert len(decimate(w, 256)) == 256
    assert len(decimate(np.arange(100.0), 256)) == 100  # shorter than target


def test_http_api():
    from fastapi.testclient import TestClient
    # constructing the engine does not touch hardware; opening it would
    c = TestClient(create_app(_engine_without_a_unit()))
    cat = c.get("/api/catalog").json()
    assert cat["bank"]  # bank tier present
    unit = {d["key"]: d for d in cat["unit"]}
    assert {"software_trigger", "io_level", "gpo_output"} <= unit.keys()
    # The UI's required/optional split and its pin-to-default checkboxes hang
    # off these fields; a catalog without them renders every setting required.
    assert unit["drs4_frequency"].get("required") is True
    assert unit["max_events_blt"]["default"] == 1023
    assert unit["io_level"]["default"] == "nim"
    st = c.get("/api/status").json()
    assert st["backend"] == "caen" and st["opened"] is False


def test_config_write_is_refused_with_no_unit():
    """With nothing attached the write goes nowhere, so it must be reported as
    a failure and must not change the stored config. Claiming success here once
    produced a green 'applied and read back from unit' toast with no unit."""
    from fastapi.testclient import TestClient
    c = TestClient(create_app(_engine_without_a_unit()))
    cfg = c.get("/api/config").json()
    was = cfg["channels"][0]["dc_offset"]

    cfg["channels"][0]["dc_offset"] = 555
    r = c.post("/api/config", json=cfg).json()
    assert r["connected"] is False
    assert r["ok"] is False and r["errors"]
    assert r["config"]["channels"][0]["dc_offset"] == was       # reverts
    assert c.get("/api/config").json()["channels"][0]["dc_offset"] == was


def test_a_refused_write_is_reported_even_with_a_full_error_log():
    """The error list is a capped ring. The API used to report a write's errors
    by diffing it before and after, so once it was full the diff came back empty
    and a refused write was answered `ok: true` with no errors at all."""
    eng = AcquisitionEngine()
    for i in range(80):                       # overflow the 50-entry ring
        eng._record_error(f"filler {i}")
    c = TestClient(create_app(eng))
    cfg = c.get("/api/config").json()
    cfg["channels"][0]["dc_offset"] = 555
    r = c.post("/api/config", json=cfg).json()
    assert r["ok"] is False and r["errors"], "a refused write reported success"


def test_config_values_are_range_checked():
    """Out-of-range values reach here from hand-edited files and the browser. An
    impossible drs4_frequency used to survive into telemetry and raise KeyError
    inside the websocket, which just stopped the display with no explanation."""
    cfg = BoardConfig.from_dict({"drs4_frequency": 9, "post_trigger": 900,
                                 "max_events_blt": 99999,
                                 "channels": [{"dc_offset": -5},
                                              {"dc_offset": "not a number"}],
                                 "correction_level": "nonsense"})
    assert cfg.drs4_frequency in C.DRS4_FREQUENCIES
    assert 0 <= cfg.post_trigger <= 100
    assert 1 <= cfg.max_events_blt <= 1023
    assert cfg.channels[0].dc_offset == 0                  # clamped into range
    assert cfg.channels[1].dc_offset == C.DC_OFFSET_MID    # not a number at all
    assert cfg.correction_level == "auto"
    C.sample_period_ns(cfg.drs4_frequency)    # must not raise


def test_runs_are_listed_newest_first_whatever_they_are_called():
    """The listing sorted by directory name under a heading that says newest
    first, so a run recorded without a timestamp landed wherever its letters
    fell."""
    from daq import runs
    with tempfile.TemporaryDirectory() as d:
        old_root, runs.DATA_ROOT = runs.DATA_ROOT, d
        try:
            for name, started in (("aaa-old", 1000), ("zzz-newest", 3000),
                                  ("mmm-middle", 2000)):
                os.makedirs(os.path.join(d, name))
                with open(os.path.join(d, name, "run_metadata.json"), "w") as f:
                    json.dump({"started": started, "channels": {}}, f)
            assert [r["id"] for r in runs.listing()] == \
                ["zzz-newest", "mmm-middle", "aaa-old"]
        finally:
            runs.DATA_ROOT = old_root


def test_wavedump_file_can_turn_a_bank_off():
    """WaveDump enables per channel. Only the "on" half was honoured, so a file
    that disables every channel of a bank left that bank running."""
    from daq import configfile
    cfg, _ = configfile.from_text(
        "[COMMON]\nENABLE_INPUT YES\n"
        + "".join(f"[{ch}]\nENABLE_INPUT NO\n" for ch in range(8))
        + "".join(f"[{ch}]\nENABLE_INPUT YES\n" for ch in range(8, 16)))
    assert cfg.groups[0].enabled is False, "a bank the file disables must be off"
    assert cfg.groups[1].enabled is True


def test_rate_meter_total_and_last_bucket():
    """Count is a per-run total, and the headline rate is the last COMPLETE
    bucket — the still-filling one always reads low."""
    m = TriggerRateMeter(bin_s=0.05, window_s=0.5)
    for _ in range(3):
        m.add(4)
        time.sleep(0.06)
    snap = m.snapshot()
    assert snap["total"] == 12                       # cumulative, not windowed
    assert snap["instant"] == snap["rate"][-1]       # matches the last bar drawn
    assert len(snap["rate"]) == m.nbins - 1          # partial bin excluded
    m.reset()
    snap = m.snapshot()
    assert snap["total"] == 0 and all(v == 0 for v in snap["rate"])
    m.add(7)
    assert m.snapshot()["total"] == 7                # counts up again


def test_probe_and_reconnect_without_hardware():
    """No board attached: probing and reconnecting must report disconnected
    rather than raising, so the UI can render a red badge."""
    eng = _engine_without_a_unit()
    assert eng.probe() is False
    assert eng.status()["opened"] is False
    assert eng.reconnect()["opened"] is False
    c = TestClient(create_app(eng))
    assert c.get("/api/status").json()["opened"] is False
    assert c.post("/api/board/reconnect").json()["opened"] is False


def test_software_trigger_is_refused_with_no_unit():
    """No unit means nothing can fire: the request must be refused, not queued
    for an acquisition that can never start."""
    c = TestClient(create_app(_engine_without_a_unit()))
    r = c.post("/api/trigger", json={"count": 100, "rate_hz": 50}).json()
    assert r["ok"] is False
    assert r["status"]["sw_triggers_pending"] == 0


def test_legacy_config_format_imports():
    """The group's previous DAQ format ("Configuration B") must load: raw DAC
    offsets addressed by channel-in-group + group, register-convention flags,
    and GPO_BUSY mapping onto the gpo_output setting."""
    from daq import configfile
    text = """
Module 125
DRS4FREQ 0
CHNOFFSE 47000 0 0
CHNOFFSE 18536 4 0
CHNOFFSE 32768 5 0
CHNOFFSE 47000 0 1
CHNOFFSE 18536 7 1
TR0OFFSE 32768
TRG__TR0 20934
TRGPOLAR 1
POSTTRIG 0
LEMO_LEV 0
GPO_BUSY 1
"""
    cfg, notes = configfile.from_text(text)
    assert cfg.drs4_frequency == 0
    assert cfg.channels[0].dc_offset == 47000
    assert cfg.channels[4].dc_offset == 18536
    assert cfg.channels[5].dc_offset == 32768
    assert cfg.channels[8].dc_offset == 47000      # group 1 starts at ch 8
    assert cfg.channels[15].dc_offset == 18536
    assert cfg.groups[0].enabled and cfg.groups[1].enabled
    # One TR0 split to both banks: the TR0 keys land in both register sets.
    for g in cfg.groups:
        assert g.fast_trigger_dc_offset == 32768
        assert g.fast_trigger_threshold == 20934
    assert cfg.trigger_edge == "falling"
    assert cfg.post_trigger == 0
    assert cfg.io_level == "nim"
    assert cfg.gpo_output == "busy"
    assert any("module number 125" in n for n in notes)

    # A legacy file mentioning only bank 1 must turn the default bank 0 OFF -
    # the file's channel list is the whole statement of what is in use.
    only_b1, _ = configfile.from_text("CHNOFFSE 40000 0 1")
    assert not only_b1.groups[0].enabled and only_b1.groups[1].enabled


def test_run_numbers_are_inferred_and_never_reused():
    """Next number = one past the highest anywhere in the data dir, read from
    both metadata and run_<N>.root filenames - so files copied in from another
    machine still count, and deleting mid-range runs reuses nothing."""
    from daq import runs
    old_root = runs.DATA_ROOT
    with tempfile.TemporaryDirectory() as d:
        runs.DATA_ROOT = d
        try:
            assert runs.next_run_number() == 1
            os.makedirs(os.path.join(d, "copied-in"))
            open(os.path.join(d, "copied-in", "run_7.root"), "wb").close()
            assert runs.next_run_number() == 8
            os.makedirs(os.path.join(d, "metadata-only"))
            with open(os.path.join(d, "metadata-only", "run_metadata.json"), "w") as f:
                json.dump({"run_number": 12}, f)
            assert runs.next_run_number() == 13
        finally:
            runs.DATA_ROOT = old_root


def test_amplitude_corrections_and_true_times():
    """The numpy port of X742CorrectionRoutines' amplitude path, checked by
    hand: cell offsets rotate with the trigger cell, nsample offsets do not,
    the ring-buffer wrap in the time axis gains one full revolution, and the
    spike fix fires only when all 8 channels vote."""
    from daq.backend import corrections

    # Cell rotation + nsample: n=8 record, start cell 1022 wraps after 2.
    cell = np.zeros((1, 1024), dtype=np.float32)
    cell[0][1022], cell[0][0] = 10.0, 7.0     # hit via rotation at i=0 and i=2
    nsample = np.zeros((1, 1024), dtype=np.float32)
    nsample[0][3] = 5.0                        # hit at readout position 3
    waves = np.full((1, 8), 100.0, dtype=np.float32)
    corrections.amplitude_correct(waves, cell, nsample, start_cell=1022)
    # The reference pins sample 0 to sample 1 unconditionally, so the cell
    # offset applied at i=0 is immediately overwritten - as in the original.
    assert waves[0][0] == 100.0 and waves[0][1] == 100.0
    assert waves[0][2] == 93.0 and waves[0][3] == 95.0
    assert waves[0][4] == 100.0

    # True times: stamps 0,2,4,... ns; start cell 1023 wraps immediately.
    table = (np.arange(1024, dtype=np.float32) * 2.0)
    t = corrections.true_times(table, start_cell=1023, tsamp_ns=2.0, n=4)
    # 1023 -> 0 is a wrap: diff -2046 + 2*1024 = 2; then uniform 2 ns steps.
    assert t.tolist() == [0.0, 2.0, 4.0, 6.0]

    # Spike removal: an upward spike in all 8 channels is repaired to the
    # neighbour average; the same spike in only 3 channels is left alone.
    base = np.full((8, 16), 1000.0, dtype=np.float32)
    base[:, 7] -= 50.0                        # dip: w[i-1]-w[i] > 30 all round
    fixed = base.copy()
    corrections.peak_correct(fixed)
    assert (fixed[:, 7] == 1000.0).all()
    partial = np.full((8, 16), 1000.0, dtype=np.float32)
    partial[:3, 7] -= 50.0
    kept = partial.copy()
    corrections.peak_correct(kept)
    assert (kept[:3, 7] == 950.0).all()


def test_root_writer_matches_the_radical_layout():
    """waveforms.root must read back with the structure the group's testbeam
    analysis expects (tb_fnal_radical drs2root/maketree.cc): TTree 'pulse',
    channel[18][1024]/F in maketree's INTERLEAVED slot order (group*9 + ch,
    TR/MCP copies at slots 8 and 17) and its mV amplitude convention. An
    earlier writer put the TR copies at 16/17 - an analysis reading slot 8
    as the MCP would have gotten a signal channel instead."""
    import uproot
    from daq.writer import make_writer
    from daq.backend.base import Event
    from daq.config import default_config

    mv = lambda counts: (counts / 4095.0 - 0.5) * 1000.0
    cfg = default_config()
    cfg.output_format = "root"
    for g in cfg.groups:
        g.enabled = True          # both banks: the slot map spans all 18
    with tempfile.TemporaryDirectory() as d:
        w = make_writer(d, "root-test", cfg.output_format)
        w.open(cfg)
        true_t = np.arange(C.RECORD_LENGTH, dtype=np.float32) * 0.21
        for i in range(3):
            samples = {ch: np.full(C.RECORD_LENGTH, 100.0 * i + ch,
                                   dtype=np.float32)
                       for ch in cfg.enabled_channels()}
            if i == 2:                       # the decoder's TR copies, 16/17
                samples[16] = np.full(C.RECORD_LENGTH, 60.0, dtype=np.float32)
                samples[17] = np.full(C.RECORD_LENGTH, 61.0, dtype=np.float32)
            w.write(Event(index=i, timestamp_s=0.0, trigger_time_tag=7 * i,
                          samples=samples, trigger_cells={0: 100 + i},
                          times_ns={0: true_t} if i == 2 else None))
        w.close()

        with uproot.open(os.path.join(d, "waveforms.root")) as f:
            t = f["pulse"]
            assert t.num_entries == 3
            a = t.arrays(library="np")
            assert a["event"].tolist() == [0, 1, 2]
            assert a["channel"].shape == (3, 18, C.RECORD_LENGTH)
            assert a["times"].shape == (3, 2, C.RECORD_LENGTH)
            # Group 0 signal channels land at slots 0-7 unchanged...
            assert abs(a["channel"][2][5][0] - mv(205.0)) < 1e-3
            # ...group 1's shift by one: decoder ch 8 is slot 9...
            assert abs(a["channel"][1][9][0] - mv(108.0)) < 1e-3
            assert abs(a["channel"][1][16][0] - mv(115.0)) < 1e-3
            # ...and the TR/MCP copies sit at maketree's slots 8 and 17.
            assert abs(a["channel"][2][8][0] - mv(60.0)) < 1e-3
            assert abs(a["channel"][2][17][0] - mv(61.0)) < 1e-3
            assert a["channel"][0][8].max() == 0.0    # no TR in that event
            # 5 GS/s: 0.2 ns per sample, so sample 10 sits at 2 ns.
            assert abs(a["times"][0][0][10] - 2.0) < 1e-6
            assert a["tc"][1][0] == 101                  # trigger cell recorded
            # Event 2 carried a true (non-uniform-capable) axis for group 0;
            # group 1 keeps the uniform default.
            assert abs(a["times"][2][0][10] - 2.1) < 1e-5
            assert abs(a["times"][2][1][10] - 2.0) < 1e-6

        meta = json.load(open(os.path.join(d, "run_metadata.json")))
        # The per-run record of the mapping, so no analysis ever guesses.
        assert meta["channels"]["8"]["root_slot"] == 9
        assert "slot = group*9" in meta["root_channel_layout"]

        meta = json.load(open(os.path.join(d, "run_metadata.json")))
        assert meta["events"] == 3 and meta["output_format"] == "root"


def test_fake_backend_behaves_like_a_board():
    """The Playwright suite runs the server with DAQ_BACKEND=fake; this guards
    the contract it relies on: the fake opens, settings stick exactly, and
    software triggers become events the readout loop counts."""
    from daq.backend.base import make_backend
    eng = AcquisitionEngine(lambda: make_backend("fake"))
    try:
        assert eng.probe() is True and eng.status()["opened"]
        cfg = eng.get_config()
        cfg.channels[0].dc_offset = 41000
        assert eng.set_config(cfg)[0].channels[0].dc_offset == 41000
        assert eng.fire_software_triggers(3, rate_hz=1000)["ok"]
        deadline = time.time() + 3
        while time.time() < deadline and eng.status()["events_seen"] < 3:
            time.sleep(0.05)
        assert eng.status()["events_seen"] >= 3
        # The overlay display feeds on one single-event trace per tick.
        tele = eng.telemetry()
        ch0 = tele["channels"]["0"]
        assert len(ch0["last"]) == C.OVERVIEW_POINTS
        assert isinstance(ch0["last_index"], int)
        # TR digitizing is on by default, so the trace rides along as 16+group.
        assert "16" in tele["channels"]
        # Single-event vpp is the DEAD discriminator: the fake's noise floor
        # plus its pulse must comfortably exceed a live channel's threshold.
        assert ch0["last_vpp"] > 100
    finally:
        eng.close()


def test_every_catalog_choice_survives_config_validation():
    """The catalog is what the UI offers; __post_init__ is what the config
    keeps. A choice the validator's allow-list does not know is coerced to a
    fallback with no error anywhere, so in the UI the selection just reverts.
    That happened for real: a merge brought the validation layer in without
    "root" and "timing", and ROOT output and the timing correction switched
    themselves off silently."""
    from daq.catalog import UNIT_SETTINGS
    base = default_config().to_dict()
    for s in UNIT_SETTINGS:
        key = s.get("key")
        if not s.get("choices") or key not in base:
            continue
        for choice in s["choices"]:
            value = choice["value"]
            got = getattr(BoardConfig.from_dict({**base, key: value}), key)
            assert got == value, (
                f"{key}={value!r} was coerced to {got!r}: the config "
                f"allow-list lags the catalog")


def test_stale_config_push_is_refused_and_returns_the_truth():
    """A tab pushing a whole config based on an older revision must be
    refused: accepting one once reverted every calibrated offset and name
    to the tab's stale pre-restart snapshot. The refusal returns the
    current config and revision so the tab catches up instead."""
    from fastapi.testclient import TestClient
    from daq.backend.base import make_backend
    eng = AcquisitionEngine(lambda: make_backend("fake"))
    try:
        assert eng.probe() is True
        c = TestClient(create_app(eng))
        cfg = c.get("/api/config").json()
        rev = c.get("/api/status").json()["config_rev"]
        # A current-revision push lands and bumps the revision.
        cfg["channels"][0]["dc_offset"] = 40123
        r = c.post("/api/config", json={**cfg, "base_rev": rev}).json()
        assert r["ok"] and r["config"]["channels"][0]["dc_offset"] == 40123
        assert r["config_rev"] == rev + 1
        # The same, now stale, revision is refused and nothing changes.
        cfg["channels"][0]["dc_offset"] = 30001
        r2 = c.post("/api/config", json={**cfg, "base_rev": rev}).json()
        assert r2.get("stale") is True and not r2["ok"]
        assert r2["config"]["channels"][0]["dc_offset"] == 40123
        # A push naming no base (scripts, curl) still works as before.
        r3 = c.post("/api/config", json=cfg).json()
        assert r3["ok"] and r3["config"]["channels"][0]["dc_offset"] == 30001
    finally:
        eng.close()


def test_link_specs_parse_from_the_environment():
    """DAQ_LINK picks the wire: plain USB when unset (the old behavior,
    exactly), or an ordered fallback list like "a4818:25001,usb" for the
    optical adapter with USB as the safety net. Malformed entries are
    skipped with a log line, never fatal - a typo must not strand the DAQ."""
    from daq.backend.caen import _link_specs, ConnectionType_USB, ConnectionType_A4818
    old = os.environ.pop("DAQ_LINK", None)
    try:
        assert _link_specs() == [(ConnectionType_USB, 0, "usb")]
        os.environ["DAQ_LINK"] = "a4818:25001, usb"
        assert _link_specs() == [(ConnectionType_A4818, 25001, "a4818:25001"),
                                 (ConnectionType_USB, 0, "usb")]
        # a4818 without its PID, junk entries, junk numbers: skipped, and the
        # list never comes back empty.
        os.environ["DAQ_LINK"] = "a4818, wombat, optical:x"
        assert _link_specs() == [(ConnectionType_USB, 0, "usb")]
    finally:
        if old is None:
            os.environ.pop("DAQ_LINK", None)
        else:
            os.environ["DAQ_LINK"] = old


def test_connection_sounds_never_raise():
    """The chirps are a courtesy. No audio device, no sound files, not on
    Windows - none of it may ever raise into the readout path."""
    from daq import sounds
    sounds.play("connected")
    sounds.play("disconnected")
    sounds.play("no-such-event")


def test_run_note_lands_in_the_metadata_sidecar():
    """The record dialog's note - what was tested, beam energy - is stored
    verbatim in run_metadata.json, where the listing and the analysis read
    it. The one fact about a run no register readback can supply."""
    from daq.writer import write_run_metadata, stamp_run_end
    with tempfile.TemporaryDirectory() as d:
        write_run_metadata(d, default_config(), "beam", 7, "LuAG, 3 GeV e-")
        with open(os.path.join(d, "run_metadata.json")) as f:
            meta = json.load(f)
        assert meta["note"] == "LuAG, 3 GeV e-"
        assert meta["run_number"] == 7
        # A campaign folder: a second recording into the same directory keeps
        # BOTH runs' notes and event counts, while the top level tracks the
        # latest - which is also what single-run folders always showed.
        write_run_metadata(d, default_config(), "beam", 8, "same setup, 5 GeV")
        stamp_run_end(d, 250, 8)
        with open(os.path.join(d, "run_metadata.json")) as f:
            meta = json.load(f)
        assert meta["runs"]["7"]["note"] == "LuAG, 3 GeV e-"
        assert meta["runs"]["8"]["note"] == "same setup, 5 GeV"
        assert meta["runs"]["8"]["events"] == 250
        assert meta["note"] == "same setup, 5 GeV" and meta["events"] == 250


def test_scope_mode_free_runs_and_ships_full_resolution_traces():
    """Scope mode fires software triggers on its own pace and telemetry ships
    the single trace at FULL resolution - the block-mean decimation that keeps
    the wire light would average away the noise a scope exists to show."""
    from daq.backend.base import make_backend
    eng = AcquisitionEngine(lambda: make_backend("fake"))
    try:
        assert eng.probe() is True
        r = eng.set_scope(10.0)
        assert r["ok"] and r["scope_hz"] == 10.0
        assert eng.status()["scope_hz"] == 10.0
        seen0 = eng.status()["events_seen"]
        deadline = time.time() + 3
        full = None
        while time.time() < deadline:
            e = eng.telemetry()["channels"].get("0", {})
            if e.get("last") and len(e["last"]) == C.RECORD_LENGTH:
                full = e["last"]
                break
            time.sleep(0.05)
        assert full is not None, "no full-resolution trace arrived"
        assert eng.status()["events_seen"] > seen0     # the scope fed itself
        r = eng.set_scope(None)
        assert r["ok"] and eng.status()["scope_hz"] is None
        # Off again: the wire returns to the light decimated form.
        deadline = time.time() + 2
        n = None
        while time.time() < deadline:
            e = eng.telemetry()["channels"].get("0", {})
            if e.get("last") and len(e["last"]) == C.OVERVIEW_POINTS:
                n = len(e["last"])
                break
            time.sleep(0.05)
        assert n == C.OVERVIEW_POINTS
    finally:
        eng.close()


def test_scope_channel_trigger_gates_the_single_trace_display():
    """The scope's software channel-trigger: with a condition the fake's
    pulse cannot meet (rising, on a negative pulse) the single-trace display
    holds while events keep flowing; with one it meets easily, the display
    refreshes again. Events are never dropped - only the display is gated."""
    from daq.backend.base import make_backend
    eng = AcquisitionEngine(lambda: make_backend("fake"))
    try:
        assert eng.probe() is True
        assert eng.set_scope(20.0)["ok"]
        deadline = time.time() + 3
        while time.time() < deadline \
                and not eng.telemetry()["channels"].get("0", {}).get("last_index"):
            time.sleep(0.05)
        # Rising condition on the fake's NEGATIVE ~195 mV pulse: nothing passes.
        r = eng.set_scope(20.0, {"channel": 0, "level_mv": 100, "edge": "rising"})
        assert r["ok"] and r["scope_trigger"] == {"channel": 0, "level_mv": 100.0,
                                                 "edge": "rising"}
        time.sleep(0.3)                          # in-flight events drain
        held = eng.telemetry()["channels"]["0"]["last_index"]
        seen = eng.status()["events_seen"]
        time.sleep(0.7)
        assert eng.status()["events_seen"] > seen              # still acquiring
        assert eng.telemetry()["channels"]["0"]["last_index"] == held  # display held
        # Falling at 50 mV, well under the pulse: the display refreshes.
        assert eng.set_scope(20.0, {"channel": 0, "level_mv": 50,
                                    "edge": "falling"})["ok"]
        deadline = time.time() + 3
        while time.time() < deadline \
                and eng.telemetry()["channels"]["0"]["last_index"] == held:
            time.sleep(0.05)
        assert eng.telemetry()["channels"]["0"]["last_index"] > held
        # A malformed spec falls back to trigger-on-anything, never an error.
        assert eng.set_scope(20.0, {"channel": "junk"})["scope_trigger"] is None
    finally:
        eng.close()


def _wait_calibration(eng, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline and eng.calibrator.is_active():
        time.sleep(0.1)
    st = eng.calibrator.status()
    assert not st["active"], "calibration did not finish"
    return st


def test_auto_baseline_centers_every_channel():
    """Phase 1 servo: a channel parked far off centre comes back to the
    window middle, and TR0 is steered through its own (different) DAC."""
    from daq.backend.base import make_backend
    eng = AcquisitionEngine(lambda: make_backend("fake"))
    try:
        assert eng.probe() is True
        cfg = eng.get_config()
        cfg.channels[0].dc_offset = 42598           # ~ -0.3 V: far off centre
        eng.set_config(cfg)
        eng.calibrator.baseline_events = 6
        eng.calibrator.stall_s = 5.0
        eng.calibrator.settle_s = 0.05      # the fake has no SPI to drain
        assert eng.calibrator.start("baseline")["ok"]
        st = _wait_calibration(eng)
        assert st["error"] is None
        assert st["report"] and all(r["status"] == "ok" for r in st["report"])
        assert any(r["channel"] == "TR0" for r in st["report"])
        got = eng.get_config()
        assert abs(got.channels[0].dc_offset - 32768) <= 200
    finally:
        eng.close()


def test_fit_calibration_makes_room_for_the_pulse():
    """Phase 2, polarity-agnostic: the fake's pulses are negative-going, so
    fitting them must RAISE the baselines above centre - inferred from the
    data, with no polarity setting anywhere."""
    from daq.backend.base import make_backend
    eng = AcquisitionEngine(lambda: make_backend("fake"))
    try:
        assert eng.probe() is True
        eng.calibrator.baseline_events = 6
        eng.calibrator.fit_events = 6
        eng.calibrator.stall_s = 5.0
        eng.calibrator.settle_s = 0.05      # the fake has no SPI to drain
        assert eng.calibrator.start("fit")["ok"]
        st = _wait_calibration(eng, timeout=90)
        assert st["error"] is None
        assert st["report"] and all(r["status"] == "ok" for r in st["report"])
        got = eng.get_config()
        # ~800-count pulse below an initially centred baseline: the servo must
        # shift the baseline up (a smaller DAC word raises it).
        assert got.channels[0].dc_offset < 31500
        ch0 = next(r for r in st["report"] if r["channel"] == "CH 0")
        assert ch0["below_mv"] > 150            # the pulse extent was seen
    finally:
        eng.close()


def test_dac_changes_rearm_while_acquiring_and_refuse_while_recording():
    """A DC-offset change only takes analog effect at an arm (measured on the
    real unit), so writing one mid-acquisition must re-arm - and mid-recording
    must be refused, not smuggled into the run."""
    from daq import runs
    from daq.backend.base import make_backend
    old_root = runs.DATA_ROOT
    with tempfile.TemporaryDirectory() as d:
        runs.DATA_ROOT = d
        eng = AcquisitionEngine(lambda: make_backend("fake"))
        try:
            assert eng.probe() is True
            eng.start()
            assert eng.status()["running"]
            cfg = eng.get_config()
            cfg.channels[0].dc_offset = 30000
            got, _ = eng.set_config(cfg)
            assert got.channels[0].dc_offset == 30000
            assert eng.status()["running"]           # re-armed, still going

            assert eng.start_recording("guard", timestamp=False)["ok"]
            before = len(eng.status()["errors"])
            cfg2 = eng.get_config()
            cfg2.channels[0].dc_offset = 29000
            got2, refused = eng.set_config(cfg2)
            assert got2.channels[0].dc_offset == 30000   # refused, unchanged
            assert refused                               # and says so in the return
            assert len(eng.status()["errors"]) > before
            assert eng.status()["recording"]             # the run survived
            eng.stop_recording()
        finally:
            eng.close()
            runs.DATA_ROOT = old_root


def test_calibration_cancel_stops_a_patient_wait():
    """Measurements are event-count-driven with no overall timeout, so Cancel
    must end a long wait promptly - and read as cancelled, not failed."""
    from daq.backend.base import make_backend
    eng = AcquisitionEngine(lambda: make_backend("fake"))
    try:
        assert eng.probe() is True
        eng.calibrator.settle_s = 0.05
        # The operator's event count rides in via start(); hours at 5 Hz.
        assert eng.calibrator.start("fit", events=100000)["ok"]
        time.sleep(0.8)                          # let it settle into the wait
        assert eng.calibrator.cancel()["ok"]
        deadline = time.time() + 5
        while time.time() < deadline and eng.calibrator.is_active():
            time.sleep(0.1)
        st = eng.calibrator.status()
        assert not st["active"]
        assert st["message"] == "cancelled" and st["error"] is None
    finally:
        eng.close()


def test_recording_stops_itself_at_max_events():
    """A bounded capture: the run closes at exactly N events while acquisition
    keeps running - "give me N triggers to look at" without a stopwatch."""
    from daq import runs
    from daq.backend.base import make_backend
    old_root = runs.DATA_ROOT
    with tempfile.TemporaryDirectory() as d:
        runs.DATA_ROOT = d
        eng = AcquisitionEngine(lambda: make_backend("fake"))
        try:
            assert eng.probe() is True
            r = eng.start_recording("bounded", timestamp=False, max_events=3)
            assert r["ok"]
            eng.fire_software_triggers(10, rate_hz=1000)
            deadline = time.time() + 5
            while time.time() < deadline and eng.status()["recording"]:
                time.sleep(0.05)
            st = eng.status()
            assert st["recording"] is False and st["running"] is True
            meta = json.load(open(os.path.join(d, "bounded", "run_metadata.json")))
            assert meta["events"] == 3
        finally:
            eng.close()
            runs.DATA_ROOT = old_root


def test_sessions_and_display_roundtrip():
    """Display prefs autosave and reload; sessions save, list, apply and
    delete. Applying with no unit must still restore the display state while
    reporting the hardware write was refused - and never invent success."""
    with tempfile.TemporaryDirectory() as d:
        key = "LOCALAPPDATA" if os.name == "nt" else "XDG_STATE_HOME"
        old = os.environ.get(key)
        os.environ[key] = d
        try:
            c = TestClient(create_app(_engine_without_a_unit()))
            assert c.get("/api/sessions").json()["sessions"] == []

            c.post("/api/display", json={"y_ranges": {"3": [-0.5, 0.25]}})
            assert c.get("/api/display").json()["y_ranges"]["3"] == [-0.5, 0.25]

            r = c.post("/api/sessions/cosmics nov").json()
            assert r["ok"] and r["name"] == "cosmics nov"
            names = [s["name"] for s in c.get("/api/sessions").json()["sessions"]]
            assert names == ["cosmics nov"]

            a = c.post("/api/sessions/cosmics nov/apply").json()
            assert a["connected"] is False and a["ok"] is False   # no unit
            assert a["display"]["y_ranges"]["3"] == [-0.5, 0.25]  # still lands

            assert c.delete("/api/sessions/cosmics nov").json()["ok"]
            assert c.post("/api/sessions/cosmics nov/apply").status_code == 404
        finally:
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


def test_runtime_url_is_always_loopback_for_a_local_window():
    """A server bound to every interface is still opened at 127.0.0.1 locally."""
    assert runtime.url_for("0.0.0.0", 8000) == "http://127.0.0.1:8000/"
    assert runtime.url_for("", 8000) == "http://127.0.0.1:8000/"
    assert runtime.url_for("127.0.0.1", 8800) == "http://127.0.0.1:8800/"
    assert runtime.url_for("10.0.0.5", 8000) == "http://10.0.0.5:8000/"


def test_runtime_record_roundtrips_and_clears():
    with tempfile.TemporaryDirectory() as d:
        os.environ["XDG_STATE_HOME"] = d
        os.environ["LOCALAPPDATA"] = d
        runtime.write("0.0.0.0", 8123)
        rec = runtime.read()
        assert rec["port"] == 8123 and rec["pid"] == os.getpid()
        assert rec["url"] == "http://127.0.0.1:8123/"
        runtime.clear()
        assert runtime.read() is None


def test_stale_and_foreign_servers_are_not_attached_to():
    """The runtime file is a hint, not an authority. A dead port, and a port held
    by some other program, must both read as 'no server' — never as one to
    attach to and drive."""
    import http.server
    import threading

    with tempfile.TemporaryDirectory() as d:
        os.environ["XDG_STATE_HOME"] = d
        os.environ["LOCALAPPDATA"] = d

        # 1. Nothing listening: the record is stale and must be cleared.
        os.makedirs(runtime.state_dir(), exist_ok=True)
        with open(runtime.runtime_path(), "w") as f:
            json.dump({"app": runtime.APP_ID, "pid": 1, "host": "127.0.0.1",
                       "port": 9, "url": "http://127.0.0.1:9/"}, f)
        assert runtime.find_server() is None
        assert runtime.read() is None

        # 2. Something answers on the port, but it is not us.
        class Impostor(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({"app": "something-else", "opened": True}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), Impostor)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            assert runtime.probe(port, timeout=2.0) is None
            with open(runtime.runtime_path(), "w") as f:
                json.dump({"app": runtime.APP_ID, "pid": 1, "host": "127.0.0.1",
                           "port": port, "url": f"http://127.0.0.1:{port}/"}, f)
            assert runtime.find_server() is None
        finally:
            srv.shutdown()


def test_status_endpoint_identifies_the_app():
    """The launcher keys off these two fields; losing them would make every
    running server invisible to `daq`."""
    c = TestClient(create_app(_engine_without_a_unit()))
    body = c.get("/api/status").json()
    assert body["app"] == "dt5742b-daq"
    assert body["version"]


def test_bind_probe_matches_uvicorn_so_a_restart_can_reuse_its_port():
    """Closing a server leaves its connections in TIME_WAIT, and a bind without
    SO_REUSEADDR fails there — so a plain probe reports "port already in use" for
    a port uvicorn (which sets SO_REUSEADDR) would take happily. That refuses the
    server a port it could have had, for minutes after every restart."""
    import socket

    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.listen(1)
    client = socket.create_connection(("127.0.0.1", port))
    accepted, _ = srv.accept()
    client.close()
    accepted.close()
    srv.close()

    assert runtime.bind_probe("127.0.0.1", port) is None, \
        "the probe must mirror uvicorn's SO_REUSEADDR or restarts are refused"
    assert runtime.port_is_free("127.0.0.1", port)


def test_bind_probe_still_reports_a_port_that_is_really_taken():
    import socket

    held = socket.socket()
    held.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    held.bind(("127.0.0.1", 0))
    port = held.getsockname()[1]
    held.listen(1)
    try:
        if os.name != "nt":
            # On Windows SO_REUSEADDR permits binding over a live socket, so this
            # only holds on POSIX; there the real conflict surfaces from uvicorn.
            assert runtime.bind_probe("127.0.0.1", port) is not None
    finally:
        held.close()


def _collecting_logger(name):
    import logging
    seen = []

    class Collect(logging.Handler):
        def emit(self, record):
            seen.append(record.getMessage())

    log = logging.getLogger(name)
    log.handlers = [Collect()]
    log.setLevel(logging.DEBUG)
    log.propagate = False
    return log, seen


def test_log_steps_nest_outside_in_then_close_inside_out():
    """The ordering rule: entries appear when the work happened. A recursion
    three deep must log three opening lines outside-in, then three closing lines
    inside-out - never interleaved, never reordered."""
    from daq import logsetup

    log, seen = _collecting_logger("daq.test.steps")

    def recurse(depth):
        with logsetup.step(log, f"Level {depth}") as s:
            if depth < 3:
                recurse(depth + 1)
            s.done(f"Finished {depth}")

    recurse(1)

    opens = [m for m in seen if m.endswith("...")]
    closes = [m for m in seen if m.strip().startswith("Finished")]
    assert len(opens) == 3 and len(closes) == 3
    assert [m.strip() for m in opens] == ["Level 1...", "Level 2...", "Level 3..."]
    assert [m.strip() for m in closes] == ["Finished 3", "Finished 2", "Finished 1"]
    # The deepest open precedes the first close: starts, then ends.
    assert seen.index(opens[2]) < seen.index(closes[0])
    # Indentation reflects nesting.
    assert not opens[0].startswith(" ")
    assert opens[1].startswith("  ") and not opens[1].startswith("    ")
    assert opens[2].startswith("    ")


def test_log_conclusion_never_repeats_the_opening_line():
    """A closing line that echoes its opening reads as a new operation starting.
    The conclusion is stated in its own words."""
    from daq import logsetup

    log, seen = _collecting_logger("daq.test.wording")
    with logsetup.step(log, "Looking for a running server") as s:
        s.done("No server found")

    assert seen[0] == "Looking for a running server..."
    assert seen[1] == "No server found"
    assert "Looking for" not in seen[1]


def test_log_atomic_operations_are_one_line():
    from daq import logsetup

    log, seen = _collecting_logger("daq.test.atomic")
    logsetup.did(log, "Checking for a config file", "Ok")
    assert seen == ["Checking for a config file... Ok"]


def test_log_lines_carry_no_durations():
    """Every line is timestamped, so elapsed time is a subtraction away; printed
    durations were noise, and mostly read 0.0s."""
    import re
    from daq import logsetup

    log, seen = _collecting_logger("daq.test.timing")
    with logsetup.step(log, "Doing something") as s:
        s.done("Did it")
    logsetup.did(log, "Something atomic", "Ok")
    assert not any(re.search(r"\d+\.\d+s", m) for m in seen), seen


if __name__ == "__main__":
    for fn in [test_tiers_and_enable_is_per_group,
               test_rolling_average_matches_numpy, test_decimate,
               test_http_api, test_config_write_is_refused_with_no_unit,
               test_a_refused_write_is_reported_even_with_a_full_error_log,
               test_config_values_are_range_checked,
               test_runs_are_listed_newest_first_whatever_they_are_called,
               test_wavedump_file_can_turn_a_bank_off,
               test_rate_meter_total_and_last_bucket,
               test_probe_and_reconnect_without_hardware,
               test_software_trigger_is_refused_with_no_unit,
               test_legacy_config_format_imports,
               test_run_numbers_are_inferred_and_never_reused,
               test_amplitude_corrections_and_true_times,
               test_root_writer_matches_the_radical_layout,
               test_fake_backend_behaves_like_a_board,
               test_auto_baseline_centers_every_channel,
               test_fit_calibration_makes_room_for_the_pulse,
               test_dac_changes_rearm_while_acquiring_and_refuse_while_recording,
               test_calibration_cancel_stops_a_patient_wait,
               test_recording_stops_itself_at_max_events,
               test_sessions_and_display_roundtrip,
               test_runtime_url_is_always_loopback_for_a_local_window,
               test_runtime_record_roundtrips_and_clears,
               test_stale_and_foreign_servers_are_not_attached_to,
               test_status_endpoint_identifies_the_app,
               test_bind_probe_matches_uvicorn_so_a_restart_can_reuse_its_port,
               test_bind_probe_still_reports_a_port_that_is_really_taken,
               test_log_steps_nest_outside_in_then_close_inside_out,
               test_log_conclusion_never_repeats_the_opening_line,
               test_log_atomic_operations_are_one_line,
               test_log_lines_carry_no_durations]:
        fn()
        print("ok:", fn.__name__)
    print("ALL SMOKE TESTS PASSED")
