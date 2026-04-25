"""Thin pymmcore-plus wrapper for synchronous single-frame acquisition.

Inspired by faro's MMDemo but stripped down: no MDA, no frameReady callbacks —
just load a config and snap one image at a time.
"""
from __future__ import annotations

import os
import numpy as np
import pymmcore_plus


class Microscope:
    def __init__(
        self,
        config_path: str,
        micromanager_path: str = r"C:\Program Files\Micro-Manager-2.0",
        channel_group: str | None = None,
    ):
        pymmcore_plus.use_micromanager(micromanager_path)
        self.micromanager_path = micromanager_path
        self.config_path = config_path
        self.mmc = pymmcore_plus.CMMCorePlus()
        self.mmc.loadSystemConfiguration(config_path)
        if channel_group is not None:
            self.mmc.setChannelGroup(channelGroup=channel_group)

    @classmethod
    def demo(cls, micromanager_path: str = r"C:\Program Files\Micro-Manager-2.0") -> "Microscope":
        """Load the bundled Micro-Manager demo configuration."""
        cfg = os.path.join(micromanager_path, "MMConfig_demo.cfg")
        return cls(config_path=cfg, micromanager_path=micromanager_path, channel_group="Channel")

    def snap(self) -> np.ndarray:
        """Acquire a single frame. Returns the raw image as a 2D numpy array."""
        self.mmc.snapImage()
        return self.mmc.getImage()

    def set_channel(self, channel: str) -> None:
        group = self.mmc.getChannelGroup()
        if group:
            self.mmc.setConfig(group, channel)

    def set_exposure(self, exposure_ms: float) -> None:
        self.mmc.setExposure(exposure_ms)
