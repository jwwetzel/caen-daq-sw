"""Connection chirps: the operator hears the unit come and go without
watching the badge - which matters exactly when nobody is watching it.

Windows-only by stdlib (winsound), which is also the deployment target;
anywhere else, and on any audio failure, silence. A missing sound file or a
missing audio device must never touch the DAQ.
"""
from __future__ import annotations

import os

from . import logsetup

log = logsetup.get("daq.sounds")

_DIR = os.path.join(os.path.dirname(__file__), "sounds")


def play(event: str) -> None:
    """Fire-and-forget playback of daq_<event>.wav ('connected' or
    'disconnected'). Asynchronous, so the readout thread never waits on the
    sound card."""
    path = os.path.join(_DIR, f"daq_{event}.wav")
    try:
        import winsound
        if os.path.exists(path):
            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC
                               | winsound.SND_NODEFAULT)
    except Exception as e:
        log.debug("could not play the %s sound: %s", event, e)
