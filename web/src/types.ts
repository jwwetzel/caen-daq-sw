export interface Choice { value: string | number; label: string; }

export interface SettingDef {
  key: string;
  label: string;
  type: "enum" | "int" | "bool" | "volts" | "steps";
  choices?: Choice[];
  min?: number;
  max?: number;
  unit?: string;
  caen?: string;
  help?: string;
  depends_on?: string;
  values_by_freq?: Record<string, { pct: number; ns: number }[]>;
  record_ns_by_freq?: Record<string, number>;
  /** For type "volts": a linear DAC<->volts calibration of the setting's own
   *  path (volts per DAC LSB, signed, and the DAC word that reads 0 V).
   *  Absent means the channel-input model. */
  lsb_v?: number;
  zero_dac?: number;
  /** Must be deliberately chosen for every run; listed before the optional
   *  settings, which sit behind a per-setting checkbox. */
  required?: boolean;
  /** What an optional setting is pinned to while its checkbox is off.
   *  Injected server-side from default_config(), so it cannot drift. */
  default?: any;
}

export interface Catalog {
  unit: SettingDef[];
  bank: SettingDef[];
  channel: SettingDef[];
  geometry: {
    num_channels: number; group_size: number; num_groups: number; record_length: number;
    adc_max: number; input_range_vpp: number;
    dc_offset_max: number; dc_offset_mid: number; dc_offset_range_v: number;
  };
}

export interface ChannelConfig { dc_offset: number; name: string; }

export interface GroupConfig {
  enabled: boolean;
  fast_trigger_threshold: number;
  fast_trigger_dc_offset: number;
}

export interface BoardConfig {
  drs4_frequency: number;
  record_length: number;
  post_trigger: number;
  correction_level: string;
  trigger_edge: string;
  external_trigger: string;
  fast_trigger: string;
  fast_trigger_digitizing: boolean;
  max_events_blt: number;
  output_format: string;
  output_header: boolean;
  groups: GroupConfig[];
  channels: ChannelConfig[];
  [key: string]: any;
}

export interface ChannelTelemetry {
  wave?: number[];
  count: number;
  vpp?: number;
  min?: number;
  max?: number;
  baseline?: number;
  /** Latest single event, decimated - one per tick, for the overlay mode. */
  last?: number[];
  /** Its event counter, so the client adds each event exactly once. */
  last_index?: number;
  /** Peak-to-peak of the full (undecimated) latest event: the liveness
   *  discriminator - a live channel always shows its noise floor. */
  last_vpp?: number;
}

export interface Telemetry {
  running: boolean;
  sample_period_ns: number;
  record_length: number;
  overview_points: number;
  avg_window_s: number;
  events_seen: number;
  recording: boolean;
  run_id: string | null;
  run_started: number | null;
  recorded: number;
  enabled_channels: number[];
  channels: Record<string, ChannelTelemetry>;
  rate: { bin_seconds: number; window_seconds: number; t: number[]; rate: number[]; instant: number; total: number };
}

export interface Status {
  opened: boolean;
  /** The server's own pid, so `daq stop` can tell it from a stale record. */
  pid?: number;
  running: boolean;
  recording: boolean;
  run_id: string | null;
  run_started: number | null;
  recorded: number;
  data_dir: string;
  next_run_number?: number;
  sw_triggers_pending?: number;
  /** Scope mode's free-running software-trigger rate; null/absent = off. */
  scope_hz?: number | null;
  /** The scope's software channel-trigger, when one is set. */
  scope_trigger?: { channel: number; level_mv: number; edge: string } | null;
  /** Bumped on every accepted config; tabs refetch when it moves. */
  config_rev?: number;
  backend: string;
  board: { model: string; family: string; serial: number; roc_firmware: string; amc_firmware: string; sw_release: string };
  events_seen: number;
  errors: string[];
}

/** How often the header re-checks the board. */
export const STATUS_POLL_MS = 1500;
