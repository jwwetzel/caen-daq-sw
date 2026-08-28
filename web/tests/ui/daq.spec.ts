import { test, expect, Page } from "@playwright/test";

/** The UI against the fake board: what a person does with a mouse, verified
 * against what the server then holds. Every hardware-facing assertion checks
 * /api/config - the board (here, the fake) is the source of truth, and a test
 * that only reads the DOM would pass while the write silently failed. */

const cfg = async (page: Page) =>
  (await page.request.get("/api/config")).json();

test.beforeEach(async ({ page }) => {
  await page.goto("/");
  // The board opens on a background thread; controls unlock when it is up.
  await expect(page.locator(".hw-lock")).toBeEnabled({ timeout: 15_000 });
});

test("loads with the fake unit connected and all 16 channels", async ({ page }) => {
  await expect(page.getByText("DT5742B-SIM")).toBeVisible();
  // Default config: bank 0 enabled and open (8 tiles); bank 1 disabled and
  // collapsed. Expanding it shows the other 8.
  await expect(page.locator(".tile")).toHaveCount(8);
  await page.locator(".bank-head", { hasText: "Bank 1" }).click();
  await expect(page.locator(".tile")).toHaveCount(16);
  await expect(page.locator(".pill.state")).toHaveText("idle");
  // The event counter is always drawn (n=0 when idle), so tiles never
  // resize when events start arriving.
  await expect(page.locator(".tile-foot .n").first()).toHaveText(/^n=\d+$/);
});

test("unit settings: required first, optional gated behind checkboxes", async ({ page }) => {
  const grid = page.locator(".settings-grid").first();
  const rows = grid.locator("> *");
  // Five required rows, then the divider, then the optional rows.
  await expect(rows.nth(0)).toContainText("Sampling frequency");
  await expect(rows.nth(4)).toContainText("Fast trigger");
  await expect(rows.nth(5)).toHaveClass(/settings-divider/);
  await expect(grid.locator(".setting-row.optional").first()).toBeVisible();
});

test("TR threshold is shown in TR-calibrated volts", async ({ page }) => {
  // Fake board default threshold 20000 through the MANUAL's arithmetic
  // (UM4270 9.8.3): (20000 - 26214) / 13.2 mV = -471 mV vs the TR zero.
  const row = page.locator(".setting-row", { hasText: "TR threshold" }).first();
  const input = row.locator('input[type="number"]');
  await expect(input).toHaveValue("-0.471");
  // The change toast must quote the SAME calibration as the field - it once
  // translated the DAC word back through the channel model and announced a
  // nonsense positive voltage for a negative threshold.
  await input.fill("-0.049");
  await input.press("Enter");
  await expect(page.getByText(/tr threshold: -0\.049 V/).first()).toBeVisible();
});

test("moving the TR offset leaves the raw threshold untouched", async ({ page }) => {
  // RAW semantics: threshold and offset are independent absolute levels.
  // An offset move must never rewrite the threshold DAC behind the
  // operator's back - the card's "vs offset" readout shows the new depth.
  const before = (await cfg(page)).groups[0].fast_trigger_threshold;

  const offRow = page.locator(".setting-row", { hasText: "TR DC offset" }).first();
  const input = offRow.locator('input[type="number"]');
  await input.fill("0.1");
  await input.press("Enter");
  await expect.poll(async () => (await cfg(page)).groups[0].fast_trigger_dc_offset)
    .not.toBe(32768);
  expect((await cfg(page)).groups[0].fast_trigger_threshold).toBe(before);
});

test("unchecking an optional setting writes its default to the unit", async ({ page }) => {
  // Customize "Dump header" (default off), then uncheck the row: the value
  // must return to the default ON THE SERVER, not merely in the form.
  const row = page.locator(".setting-row.optional", { hasText: "Dump header" });
  const box = row.locator('input[type="checkbox"]').first();
  await box.check();                                  // engage
  await row.locator('input[type="checkbox"]').nth(1).check();  // the value itself
  await expect.poll(async () => (await cfg(page)).output_header).toBe(true);
  await box.uncheck();                                // pin back to default
  await expect.poll(async () => (await cfg(page)).output_header).toBe(false);
});

test("a typed out-of-range value is clamped before it reaches the unit", async ({ page }) => {
  const row = page.locator(".setting-row.optional", { hasText: "Events per readout" });
  await row.locator('input[type="checkbox"]').first().check();
  const input = row.locator('input[type="number"]');
  await input.fill("5000");
  await input.press("Enter");
  await expect.poll(async () => (await cfg(page)).max_events_blt).toBe(1023);
  await expect(input).toHaveValue("1023");
});

test("the baseline guide is drawn when idle and follows the offset", async ({ page }) => {
  // Idle (no data): the ground marker shows where the offset will put the
  // baseline, in the channel colour, at the window centre for a centred DAC.
  const rowOf = async () => page.evaluate(() => {
    const cv = document.querySelector(".tile canvas") as HTMLCanvasElement;
    const ctx = cv.getContext("2d")!;
    const d = ctx.getImageData(0, 0, cv.width, cv.height).data;
    // Row with the most green-dominant pixels = the guide line.
    let best = -1, bestN = 0;
    for (let yy = 0; yy < cv.height; yy++) {
      let n = 0;
      for (let x = 0; x < cv.width; x++) {
        const p = (yy * cv.width + x) * 4;
        if (d[p + 3] > 60 && d[p + 1] > 140 && d[p] < 130) n++;
      }
      if (n > bestN) { bestN = n; best = yy; }
    }
    return { row: best, lit: bestN, height: cv.height };
  });
  const centred = await rowOf();
  expect(centred.lit).toBeGreaterThan(50);
  expect(Math.abs(centred.row - centred.height / 2)).toBeLessThan(centred.height * 0.1);

  // Typing a new offset moves the guide (auto re-arm is not needed for the
  // marker - it predicts).
  const field = page.locator(".tile-dc input[type=number]").first();
  await field.fill("0.2");
  await field.press("Enter");
  await expect.poll(async () => (await rowOf()).row).toBeLessThan(centred.row - 10);
  await field.fill("0");
  await field.press("Enter");
});

test("the DC-offset slider commits one write on release", async ({ page }) => {
  const before = (await cfg(page)).channels[0].dc_offset;
  const slider = page.locator(".dc-slider").first();
  await slider.focus();
  await slider.press("ArrowLeft");        // one 0.01 V step down; keyup commits
  const want = Math.round(32768 * (1 - (-0.01)));   // voltsToDac(-0.01)
  await expect.poll(async () => (await cfg(page)).channels[0].dc_offset).toBe(want);
  expect(before).not.toBe(want);
  // The typed field agrees with what the unit reports.
  await expect(page.locator(".tile-dc input[type=number]").first()).toHaveValue("-0.010");
});

test("typing a DC offset lands on the unit exactly", async ({ page }) => {
  const field = page.locator(".tile-dc input[type=number]").first();
  await field.fill("0.1");
  await field.press("Enter");
  const want = Math.round(32768 * (1 - 0.1));       // voltsToDac(+0.1)
  await expect.poll(async () => (await cfg(page)).channels[0].dc_offset).toBe(want);
});

test("clicking a Y label edits the display range, and it persists", async ({ page }) => {
  const tile = page.locator(".tile").first();
  await tile.locator("button.ax.y.max").click();
  const editor = tile.locator(".yedit input[type=number]");
  await editor.fill("0.25");
  await editor.press("Enter");
  await expect(tile.locator("button.ax.y.max")).toHaveText("+0.250 V");
  await expect
    .poll(async () => (await (await page.request.get("/api/display")).json())?.y_ranges?.["0"])
    .toEqual([-0.5, 0.25]);   // window frame: min stays at the window bottom
  // Survives a full reload: the display prefs live on the server.
  await page.reload();
  await expect(page.locator(".tile").first().locator("button.ax.y.max"))
    .toHaveText("+0.250 V");
});

test("the 'full' button resets a channel's range to the full window", async ({ page }) => {
  const tile = page.locator(".tile").first();
  await tile.locator("button.ax.y.max").click();
  await tile.locator(".yedit button", { hasText: "full" }).click();
  // Window-referenced frame: full range IS the 1 Vpp window, +/-0.5 V.
  await expect(tile.locator("button.ax.y.max")).toHaveText("+0.500 V");
});

test("sessions: save, perturb, apply restores the unit, delete", async ({ page }) => {
  const mark = (await cfg(page)).channels[0].dc_offset;

  await page.locator(".session-save input").fill("pw-test");
  await page.locator(".session-save button").click();
  const row = page.locator(".session-row", { hasText: "pw-test" });
  await expect(row).toBeVisible();

  // Perturb through the UI, then apply the session: the unit must go back.
  const field = page.locator(".tile-dc input[type=number]").first();
  await field.fill("-0.3");
  await field.press("Enter");
  await expect.poll(async () => (await cfg(page)).channels[0].dc_offset).not.toBe(mark);

  await row.locator("button", { hasText: "Apply" }).click();
  await expect.poll(async () => (await cfg(page)).channels[0].dc_offset).toBe(mark);
  await expect(page.getByText(/applied and read back/)).toBeVisible();

  page.on("dialog", (d) => d.accept());
  await row.locator("button.danger").click();
  await expect(row).toHaveCount(0);
});

test("auto-baseline centers a mis-set channel from the UI", async ({ page }) => {
  // Park ch0 far off centre, then let the servo bring it back.
  const field = page.locator(".tile-dc input[type=number]").first();
  await field.fill("-0.3");
  await field.press("Enter");
  await expect.poll(async () => (await cfg(page)).channels[0].dc_offset)
    .toBeGreaterThan(40000);

  await page.locator(".calib-btns button", { hasText: "Center baselines" }).click();
  await expect(page.getByText(/Calibration done/)).toBeVisible({ timeout: 60_000 });
  const c = await cfg(page);
  expect(Math.abs(c.channels[0].dc_offset - 32768)).toBeLessThanOrEqual(300);
  // The UI re-adopted the board's new state: the field shows ~0 V again.
  await expect(field).not.toHaveValue("-0.300");
});

test("a calibration's persistence profile is there to review afterwards", async ({ page }) => {
  // The pile accumulates in every mode, so the events a calibration collected
  // are already stacked when the operator flips to Overlay to look.
  await page.locator(".calib-btns button", { hasText: "Center baselines" }).click();
  await expect(page.getByText(/Calibration done/)).toBeVisible({ timeout: 60_000 });
  await page.locator(".wave-mode button", { hasText: "Overlay" }).click();
  const lit = await page.evaluate(() => {
    const cv = document.querySelector(".tile canvas") as HTMLCanvasElement;
    const d = cv.getContext("2d")!.getImageData(0, 0, cv.width, cv.height).data;
    let n = 0;
    for (let i = 3; i < d.length; i += 4) if (d[i] > 40) n++;
    return n;
  });
  // The fake's baseline run is quick, so the pile is thin - but present.
  expect(lit).toBeGreaterThan(150);
  await page.locator(".wave-mode button", { hasText: "Avg" }).click();
});

test("the Fire button queues test triggers and events arrive", async ({ page }) => {
  const before = (await (await page.request.get("/api/status")).json()).events_seen;
  await page.locator(".test-trigger input").fill("5");
  await page.locator(".test-trigger button", { hasText: "Fire" }).click();
  await expect(page.getByText(/Firing 5 test triggers/)).toBeVisible();
  // Firing auto-starts acquisition; the queued triggers become events.
  await expect
    .poll(async () => (await (await page.request.get("/api/status")).json()).events_seen,
          { timeout: 10_000 })
    .toBeGreaterThanOrEqual(before + 5);
  await page.getByRole("button", { name: /Disable Acquisition/ }).click();
});

test("overlay mode paints a density pile and the choice persists", async ({ page }) => {
  // Acquire so single-event traces flow (the fake board emits ~5/s).
  await page.getByRole("button", { name: /Enable Acquisition/ }).click();
  await page.locator(".wave-mode button", { hasText: "Overlay" }).click();
  await expect(page.locator(".wave-mode button.on")).toHaveText("Overlay");

  // The density canvas actually paints: non-transparent pixels appear in the
  // first tile once a few events have arrived.
  await expect.poll(async () => page.evaluate(() => {
    const cv = document.querySelector(".tile canvas") as HTMLCanvasElement;
    const ctx = cv.getContext("2d")!;
    const d = ctx.getImageData(0, 0, cv.width, cv.height).data;
    let lit = 0;
    for (let i = 3; i < d.length; i += 4) if (d[i] > 0) lit++;
    return lit;
  }), { timeout: 10_000 }).toBeGreaterThan(500);

  // The choice is display state: it survives a reload via the server.
  await expect
    .poll(async () => (await (await page.request.get("/api/display")).json()).wave_mode)
    .toBe("overlay");
  await page.reload();
  await expect(page.locator(".wave-mode button.on")).toHaveText("Overlay");

  // Back to Avg for the tests that follow.
  await page.locator(".wave-mode button", { hasText: "Avg" }).click();
  await page.getByRole("button", { name: /Disable Acquisition/ }).click();
});

test("scope mode free-runs triggers and stops when left", async ({ page }) => {
  const status = async () => (await page.request.get("/api/status")).json();

  await page.locator(".wave-mode button", { hasText: "Scope" }).click();
  // Entering scope starts the free-running software triggers server-side...
  await expect.poll(async () => (await status()).scope_hz).toBe(2);
  // ...at the rate shown in the field beside the toggle.
  await expect(page.locator(".scope-rate input")).toHaveValue("2");
  // Events arrive with nothing queued: the scope feeds itself.
  const seen = (await status()).events_seen;
  await expect.poll(async () => (await status()).events_seen,
                    { timeout: 10_000 }).toBeGreaterThan(seen);
  // A single full-resolution trace paints in the first tile.
  await expect.poll(async () => page.evaluate(() => {
    const cv = document.querySelector(".tile canvas") as HTMLCanvasElement;
    const ctx = cv.getContext("2d")!;
    const d = ctx.getImageData(0, 0, cv.width, cv.height).data;
    let lit = 0;
    for (let i = 3; i < d.length; i += 4) if (d[i] > 0) lit++;
    return lit;
  }), { timeout: 10_000 }).toBeGreaterThan(200);

  // The rate is adjustable in place.
  await page.locator(".scope-rate input").fill("5");
  await page.locator(".scope-rate input").press("Enter");
  await expect.poll(async () => (await status()).scope_hz).toBe(5);

  // The software channel-trigger travels to the server with its level.
  await page.locator(".scope-trig select").selectOption("0");
  await expect.poll(async () => (await status()).scope_trigger?.channel).toBe(0);
  await page.locator(".scope-trig input").fill("35");
  await page.locator(".scope-trig input").press("Enter");
  await expect.poll(async () => (await status()).scope_trigger?.level_mv).toBe(35);
  // Back to trigger-on-anything.
  await page.locator(".scope-trig select").selectOption("");
  await expect.poll(async () => (await status()).scope_trigger ?? null).toBe(null);

  // Leaving scope stops the firing - no orphaned trigger source.
  await page.locator(".wave-mode button", { hasText: "Avg" }).click();
  await expect.poll(async () => (await status()).scope_hz).toBe(null);
  await page.getByRole("button", { name: /Disable Acquisition/ }).click();
});

test("the TR0 card appears when the fast trigger is digitized", async ({ page }) => {
  await page.getByRole("button", { name: /Enable Acquisition/ }).click();
  // "fast trigger" is unique to the TR0 waveform card's subtitle (the TR0
  // Trigger settings panel is a different heading).
  const trCard = page.locator(".card", { has: page.locator("h2", { hasText: "fast trigger" }) });
  await expect(trCard).toBeVisible({ timeout: 10_000 });
  // The labelled trigger line: red-dominant pixels drawn across the plot.
  await expect.poll(async () => trCard.evaluate((card) => {
    const cv = card.querySelector("canvas") as HTMLCanvasElement;
    const d = cv.getContext("2d")!.getImageData(0, 0, cv.width, cv.height).data;
    let red = 0;
    for (let i = 0; i < d.length; i += 4) {
      if (d[i + 3] > 100 && d[i] > 180 && d[i + 1] < 130) red++;
    }
    return red;
  }), { timeout: 10_000 }).toBeGreaterThan(50);
  await page.getByRole("button", { name: /Disable Acquisition/ }).click();
});

test("recording a run writes run_N.root and the number advances", async ({ page }) => {
  // Fresh test state starts at run 1; the placeholder shows the inference.
  await expect(page.locator("#runno")).toHaveAttribute("placeholder", "1");

  await page.locator("#runname").fill("ui-suite");
  // Record first opens the run-notes dialog; the note lands in the metadata.
  await page.locator(".rec-group button.record").click();
  await page.locator(".rec-modal textarea").fill("LuAG crystal, 3 GeV electrons");
  await page.locator(".rec-modal button.record").click();
  await expect(page.locator(".rec-group.on")).toBeVisible();
  // The fake board emits ~5 events/s; a moment later there is data to keep.
  await expect
    .poll(async () => (await (await page.request.get("/api/status")).json()).recorded)
    .toBeGreaterThan(0);
  await page.locator("button.danger", { hasText: "Stop recording" }).click();
  // The click resolves before the server has closed the writer; the metadata
  // event count exists only once recording has actually ended.
  await expect
    .poll(async () => (await (await page.request.get("/api/status")).json()).recording)
    .toBe(false);

  const runs = await (await page.request.get("/api/runs")).json();
  expect(runs.runs.length).toBe(1);
  expect(runs.runs[0].events).toBeGreaterThan(0);
  expect(runs.runs[0].note).toBe("LuAG crystal, 3 GeV electrons");
  // ...and the listing shows it.
  await expect(page.locator(".run-note").first())
    .toHaveText("LuAG crystal, 3 GeV electrons");
  // The number advanced, and an explicit override is respected next.
  await expect(page.locator("#runno")).toHaveAttribute("placeholder", "2");
  await page.locator("#runno").fill("42");
  await page.locator("#runname").fill("ui-suite-2");
  // An empty note is fine - the dialog never blocks a shift in a hurry.
  await page.locator(".rec-group button.record").click();
  await page.locator(".rec-modal button.record").click();
  await expect(page.locator(".rec-group.on")).toBeVisible();
  await page.locator("button.danger", { hasText: "Stop recording" }).click();
  await expect
    .poll(async () => (await (await page.request.get("/api/status")).json()).recording)
    .toBe(false);
  await expect
    .poll(async () => (await (await page.request.get("/api/status")).json()).next_run_number)
    .toBe(43);
});

test("a bounded recording closes itself at N events", async ({ page }) => {
  await page.locator("#runname").fill("bounded");
  await page.locator("#recmax").fill("3");
  await page.locator(".rec-group button.record").click();
  await page.locator(".rec-modal button.record").click();
  await expect(page.locator(".rec-group.on")).toBeVisible();
  // The run ends on its own; acquisition keeps going.
  await expect(page.locator(".rec-group.on")).toHaveCount(0, { timeout: 15_000 });
  const st = await (await page.request.get("/api/status")).json();
  expect(st.running).toBe(true);
  await page.getByRole("button", { name: /Disable Acquisition/ }).click();
  const runs = await (await page.request.get("/api/runs")).json();
  expect(runs.runs.find((r: any) => r.id.startsWith("bounded")).events).toBe(3);
});

test("a second run joins an existing folder when picked without timestamp", async ({ page }) => {
  const status = async () => (await page.request.get("/api/status")).json();
  // Timestamp off, so the folder carries the bare campaign name.
  await page.locator(".rec-stamp input").uncheck();
  await page.locator("#runname").fill("campaign");
  await page.locator(".rec-group button.record").click();
  await expect(page.locator(".rec-dest")).toContainText("new run folder");
  await page.locator(".rec-modal button.record").click();
  await expect(page.locator(".rec-group.on")).toBeVisible();
  await page.locator("button.danger", { hasText: "Stop recording" }).click();
  await expect.poll(async () => (await status()).recording).toBe(false);
  const dirs0 = (await (await page.request.get("/api/runs")).json()).runs.length;

  // Same name, timestamp still off: the dialog announces it JOINS the
  // folder, and no new directory appears - the campaign stays together.
  await page.locator(".rec-group button.record").click();
  await expect(page.locator(".rec-dest")).toContainText("existing folder");
  await page.locator(".rec-modal button.record").click();
  await expect(page.locator(".rec-group.on")).toBeVisible();
  await page.locator("button.danger", { hasText: "Stop recording" }).click();
  await expect.poll(async () => (await status()).recording).toBe(false);

  const runs = (await (await page.request.get("/api/runs")).json()).runs;
  expect(runs.length).toBe(dirs0);
  const camp = runs.find((r: { id: string }) => r.id === "campaign");
  expect(camp.files).toBeGreaterThanOrEqual(3);   // 2 x run_N.root + metadata
  await page.locator(".rec-stamp input").check(); // leave it as found
});

test("a legacy Configuration B file loads through the Load button", async ({ page }) => {
  const legacy = [
    "Module 125", "DRS4FREQ 0",
    "CHNOFFSE 47000 0 0", "CHNOFFSE 18536 4 1",
    "TR0OFFSE 32768", "TRG__TR0 20934",
    "TRGPOLAR 1", "POSTTRIG 0", "LEMO_LEV 0", "GPO_BUSY 1",
  ].join("\n");
  // Straight onto the hidden input - clicking Load would open the native
  // chooser, which is the browser's UI, not ours to test.
  await page.locator('input[type="file"]').setInputFiles({
    name: "configB.txt", mimeType: "text/plain",
    buffer: Buffer.from(legacy),
  });
  await expect(page.getByText(/Config loaded and read back/)).toBeVisible();
  const c = await cfg(page);
  expect(c.gpo_output).toBe("busy");
  expect(c.trigger_edge).toBe("falling");
  expect(c.channels[0].dc_offset).toBe(47000);
  expect(c.channels[12].dc_offset).toBe(18536);
});
