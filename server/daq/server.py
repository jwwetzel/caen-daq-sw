"""FastAPI app: REST control plane + WebSocket telemetry, plus static frontend.
Telemetry pushes server-side aggregates (decimated averaged waveforms for all
enabled channels + a rolling rate window) at a fixed cadence."""
from __future__ import annotations

import asyncio
import logging
import os
import time

from fastapi import (BackgroundTasks, FastAPI, HTTPException, Request, Response,
                     WebSocket, WebSocketDisconnect)
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from . import logsetup
from .acquisition import AcquisitionEngine
from .config import BoardConfig, default_config
from .catalog import catalog
from . import configfile
from . import runs
from . import sessions
from . import constants as C

log = logsetup.get("daq.api")

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


def create_app(engine: AcquisitionEngine) -> FastAPI:
    app = FastAPI(title="DT5742B DAQ")

    @app.get("/api/status")
    def status():
        engine.probe()          # keeps `opened` honest between polls
        # `app` identifies us to the launcher, which must not mistake some other
        # program holding the port for a DAQ server it can attach to. `pid` lets
        # `daq stop` confirm the pid in the runtime file is really this server's
        # before it signals it - that record outlives crashes, and pids are
        # recycled, so acting on it unchecked can signal an unrelated process.
        return {**engine.status(), "app": "dt5742b-daq", "version": __version__,
                "pid": os.getpid(), "log_file": logsetup.active_log_path()}

    @app.post("/api/board/reconnect")
    def reconnect():
        return engine.reconnect()

    @app.get("/api/catalog")
    def get_catalog():
        return catalog()

    @app.get("/api/config")
    def get_config():
        return engine.get_config().to_dict()

    @app.post("/api/config")
    def set_config(payload: dict):
        try:
            wanted = BoardConfig.from_dict(payload)
        except (TypeError, ValueError) as e:
            raise HTTPException(400, f"not a usable config: {e}")
        # The errors come back from the call itself. Diffing engine.status()
        # before and after cannot work: that list is a capped ring, so once it
        # is full the diff is empty and a refused write reports success.
        cfg, errors = engine.set_config(wanted)
        return {"ok": not errors, "config": cfg.to_dict(), "errors": errors,
                "connected": engine.status()["opened"]}

    @app.get("/api/config/file")
    def save_config_file(names: bool = True):
        body = configfile.to_json(engine.get_config(), include_names=names)
        # Content-Disposition wins over the link's download attribute for a
        # same-origin response, so the date has to be applied here or the file
        # arrives with the same name every time and quietly overwrites.
        name = f"daq-config-{time.strftime('%Y-%m-%d')}.json"
        return Response(
            body, media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.post("/api/config/file")
    async def load_config_file(request: Request):
        """Accepts our JSON or a CAEN WaveDumpConfig.txt."""
        text = (await request.body()).decode("utf-8", errors="replace")
        with logsetup.step(log, "Loading a settings file") as loading:
            try:
                loaded, notes = configfile.from_text(text)
            except Exception as e:
                loading.done(f"Could not parse the file: {e}")
                # `parsed` tells the two failures apart. Without it a file that
                # would not even read looked identical to one the unit refused,
                # and the UI announced "loaded" for a file it had never read.
                return {"ok": False, "parsed": False, "connected": engine.status()["opened"],
                        "errors": [f"could not parse: {e}"], "notes": [],
                        "config": engine.get_config().to_dict(), "restart": []}
            loading.done(f"Read with {len(notes)} notes" if notes else "Read")
        restart = configfile.needs_restart(engine.get_config(), loaded)
        cfg, errors = engine.set_config(loaded)
        st = engine.status()
        return {"ok": not errors, "parsed": True, "config": cfg.to_dict(),
                "errors": errors, "notes": notes, "restart": restart,
                "connected": st["opened"], "running": st["running"]}

    @app.post("/api/config/default")
    def reset_default():
        # Same shape as /api/config: a reset the unit refused must be reported
        # as one, not returned as a bare config the UI then calls a success.
        cfg, errors = engine.set_config(default_config())
        return {"ok": not errors, "config": cfg.to_dict(), "errors": errors,
                "connected": engine.status()["opened"]}

    # ---- display preferences + named sessions ----

    @app.get("/api/display")
    def get_display():
        return sessions.get_display()

    @app.post("/api/display")
    def set_display(payload: dict):
        """Autosaved UI state (waveform Y ranges). Never touches the board."""
        sessions.set_display(payload or {})
        return {"ok": True}

    @app.get("/api/sessions")
    def list_sessions():
        return {"sessions": sessions.listing()}

    @app.post("/api/sessions/{name}")
    def save_session(name: str):
        saved = sessions.save(name, engine.get_config().to_dict(),
                              sessions.get_display())
        if saved is None:
            raise HTTPException(400, "session name is empty once sanitized")
        logsetup.did(log, f"Saving session {saved['name']!r}", "Ok")
        return {"ok": True, **saved}

    @app.post("/api/sessions/{name}/apply")
    def apply_session(name: str):
        """Explicitly write a session to the unit. Refused while recording:
        rewriting offsets under a run silently corrupts the data it is
        collecting, and no one applying a saved state means to do that."""
        if engine.status()["recording"]:
            raise HTTPException(409, "a run is recording - stop it first")
        s = sessions.load(name)
        if s is None:
            raise HTTPException(404, "no such session")
        with logsetup.step(log, f"Applying session {name!r}") as applying:
            cfg, errs = engine.set_config(BoardConfig.from_dict(s["config"]))
            if isinstance(s.get("display"), dict):
                sessions.set_display(s["display"])
            st = engine.status()
            applying.done("Applied with errors" if errs else
                          ("Applied and read back" if st["opened"]
                           else "Stored for the display; no unit connected"))
        return {"ok": not errs, "config": cfg.to_dict(),
                "display": s.get("display") or {}, "errors": errs,
                "connected": st["opened"]}

    @app.delete("/api/sessions/{name}")
    def delete_session(name: str):
        if not sessions.delete(name):
            raise HTTPException(404, "no such session")
        logsetup.did(log, f"Deleting session {name!r}", "Ok")
        return {"ok": True}

    @app.post("/api/rec/start")
    def rec_start(payload: dict | None = None):
        p = payload or {}
        rn = p.get("run_number")
        try:
            rn = int(rn) if rn not in (None, "") else None
        except (TypeError, ValueError):
            rn = None                # a mistyped number falls back to inferred
        if rn is not None and rn < 1:
            rn = None
        me = p.get("max_events")
        try:
            me = int(me) if me not in (None, "") else None
        except (TypeError, ValueError):
            me = None
        if me is not None and me < 1:
            me = None
        r = engine.start_recording((p.get("name") or "").strip(),
                                   bool(p.get("timestamp", True)), rn, me)
        return {**r, "status": engine.status()}

    @app.post("/api/rec/stop")
    def rec_stop():
        r = engine.stop_recording()
        return {**r, "status": engine.status()}

    @app.get("/api/runs")
    def list_runs():
        return {"data_dir": runs.DATA_ROOT, "runs": runs.listing()}

    @app.get("/api/runs/{run_id}/download")
    def download_run(run_id: str, background: BackgroundTasks):
        if engine.status()["run_id"] == run_id:
            raise HTTPException(409, "that run is still recording")
        try:
            tmp = runs.zip_to_temp(run_id)
        except OSError as e:
            log.error("Could not zip run %r: %s", run_id, e)
            raise HTTPException(500, f"could not build the zip: {e}")
        if tmp is None:
            raise HTTPException(404, "no such run")
        # The task is attached to the response, not to the route: FastAPI only
        # adopts its own BackgroundTasks when the response carries none, so
        # setting both here would be two owners of one unlink.
        background.add_task(os.unlink, tmp)
        return FileResponse(tmp, media_type="application/zip",
                            filename=f"{run_id}.zip", background=background)

    @app.delete("/api/runs/{run_id}")
    def delete_run(run_id: str):
        if engine.status()["run_id"] == run_id:
            logsetup.did(log, f"Deleting run {run_id!r}", "Refused: still recording",
                         level=logging.WARNING)
            raise HTTPException(409, "that run is still recording")
        try:
            gone = runs.delete(run_id)
        except OSError as e:
            # Windows refuses to remove a directory something still has open,
            # and the bare 500 that produced said nothing about which run or why.
            logsetup.did(log, f"Deleting run {run_id!r}", f"Refused by the filesystem: {e}",
                         level=logging.ERROR)
            raise HTTPException(500, f"could not delete it: {e}")
        if not gone:
            logsetup.did(log, f"Deleting run {run_id!r}", "No such run",
                         level=logging.WARNING)
            raise HTTPException(404, "no such run")
        logsetup.did(log, f"Deleting run {run_id!r}", "Ok")
        return {"ok": True, "deleted": run_id}

    @app.post("/api/acq/start")
    def start():
        # Start FIRST, then snapshot. A dict literal evaluates `**status()`
        # before the `started` value, so the status would be the one from before
        # the attempt - reporting no errors for the failure it is describing.
        started = engine.start()
        return {**engine.status(), "started": started}

    # Registered before the {mode} route, which would otherwise swallow it.
    @app.post("/api/calibrate/cancel")
    def calibrate_cancel():
        r = engine.calibrator.cancel()
        if not r["ok"]:
            raise HTTPException(409, r["error"])
        return r

    @app.post("/api/calibrate/{mode}")
    def calibrate(mode: str, payload: dict | None = None):
        """Start a closed-loop calibration: 'baseline' (software triggers,
        centre everything) or 'fit' (real triggers, fit the pulse in the
        window). {"events": N} sets the per-measurement event count.
        Progress is polled at GET /api/calibrate."""
        p = payload or {}
        try:
            ev = int(p.get("events")) if p.get("events") not in (None, "") else None
        except (TypeError, ValueError):
            ev = None
        r = engine.calibrator.start(mode, ev)
        if not r["ok"]:
            raise HTTPException(409, r["error"])
        return {**r, "status": engine.status()}

    @app.get("/api/calibrate")
    def calibrate_status():
        return engine.calibrator.status()

    @app.post("/api/trigger")
    def trigger(payload: dict | None = None):
        """Queue software triggers - the bench check when nothing external
        can trigger the board. {"count": 100, "rate_hz": 10} both optional."""
        p = payload or {}
        r = engine.fire_software_triggers(int(p.get("count", 1)),
                                          float(p.get("rate_hz", 10.0)))
        return {**r, "status": engine.status()}

    @app.post("/api/scope")
    def scope(payload: dict | None = None):
        """Scope mode: free-running software triggers at a steady rate, with
        full-resolution single traces in telemetry - for studying the noise
        on a line. {"on": true, "rate_hz": 2} or {"on": false}."""
        p = payload or {}
        rate = float(p.get("rate_hz", 2.0)) if p.get("on") else None
        r = engine.set_scope(rate)
        return {**r, "status": engine.status()}

    @app.post("/api/acq/stop")
    def stop():
        engine.stop()
        return engine.status()

    @app.websocket("/ws/telemetry")
    async def telemetry(ws: WebSocket):
        await ws.accept()
        try:
            while True:
                await ws.send_json(engine.telemetry())
                await asyncio.sleep(1.0 / C.TELEMETRY_HZ)
        except (WebSocketDisconnect, RuntimeError):
            return                       # the browser went away; not an error
        except Exception:
            # Anything else is a bug in telemetry(), and the symptom is a UI
            # that simply stops updating. Swallowing it left nothing at all to
            # go on; the client reconnects a second later either way.
            log.exception("The telemetry feed failed and the socket was dropped")
            return

    if os.path.isdir(STATIC_DIR):
        app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    else:
        # The UI is built into the package; without it every page is a 404 and
        # the browser shows a blank window with nothing to explain it.
        log.error("No web UI at %s - the API works but there is no page to serve. "
                  "Rebuild it with 'cd web && npm run build'.", STATIC_DIR)

    return app
