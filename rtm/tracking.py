"""Online particle tracking with trackpy over a sliding window.

Re-links all observations within a rolling window of the last ``window_frames``
frames. Assigns stable global particle IDs across re-links so tracks can be
identified across calls, and emits "completed" tracks once a particle has not
been observed for more than ``memory`` frames.

Hackable surface:

- ``search_range`` / ``memory`` — forwarded to :func:`trackpy.link`.
- ``window_frames`` — how far back to keep observations; also sets the horizon
  after which a missing particle is considered to have left the FOV.
- ``flow_speed(..., aggregator=np.median)`` — swap the aggregator for ``max``,
  ``mean``, or a custom callable.
"""
from __future__ import annotations

from collections import Counter
from typing import Callable

import numpy as np
import pandas as pd
import trackpy as tp

# Silence trackpy's per-call "Frame X: N trajectories present" prints.
tp.quiet()


_OBS_COLS = [
    "obs_id",
    "frame",
    "x",
    "y",
    "area",
    "bbox_min_row",
    "bbox_min_col",
    "bbox_max_row",
    "bbox_max_col",
]


def _empty_obs_df() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype="float64") for c in _OBS_COLS})


class ParticleTracker:
    def __init__(
        self,
        *,
        search_range: float = 15.0,
        memory: int = 1,
        window_frames: int = 50,
    ):
        self.search_range = search_range
        self.memory = memory
        self.window_frames = window_frames

        self._obs: pd.DataFrame = _empty_obs_df()
        self._obs_to_particle: dict[int, int] = {}
        self._emitted: set[int] = set()
        self._next_obs_id: int = 0
        self._next_particle_id: int = 0

    # ------------------------------------------------------------------
    # Ingest
    # ------------------------------------------------------------------

    def update(self, regions: pd.DataFrame, timestep: int) -> pd.DataFrame:
        """Ingest this frame's regions and return linked observations in the window.

        *regions* is the DataFrame produced by :func:`rtm.segmentation.measure_regions`.
        Returns a DataFrame with columns ``obs_id, frame, x, y, area, ...,
        particle`` (the ``particle`` column holds stable global IDs).
        """
        if not regions.empty:
            new = regions.rename(
                columns={"centroid_x": "x", "centroid_y": "y"}
            )[[
                "x", "y", "area",
                "bbox_min_row", "bbox_min_col", "bbox_max_row", "bbox_max_col",
            ]].copy()
            new["frame"] = timestep
            n = len(new)
            new["obs_id"] = np.arange(self._next_obs_id, self._next_obs_id + n)
            self._next_obs_id += n
            self._obs = pd.concat([self._obs, new[_OBS_COLS]], ignore_index=True)

        # Drop observations that fell out of the window.
        horizon = timestep - self.window_frames
        self._obs = self._obs[self._obs["frame"] > horizon].reset_index(drop=True)
        keep_ids = set(self._obs["obs_id"].astype(int).tolist())
        self._obs_to_particle = {
            oid: pid for oid, pid in self._obs_to_particle.items() if oid in keep_ids
        }

        if self._obs.empty:
            return self._obs.assign(particle=pd.Series(dtype="int64"))

        linked = tp.link(
            self._obs.copy(),
            search_range=self.search_range,
            memory=self.memory,
        )
        linked["particle"] = self._assign_stable_ids(linked)
        return linked

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def pop_completed_tracks(
        self, tracks: pd.DataFrame, timestep: int
    ) -> pd.DataFrame:
        """Return (and mark emitted) tracks whose last obs is older than ``memory``.

        A track is "completed" when no observation has been seen for more than
        ``memory`` frames. Once returned, it will not be returned again.
        """
        if tracks.empty:
            return tracks

        last_frame = tracks.groupby("particle")["frame"].max()
        done_mask = (last_frame < timestep - self.memory) & (
            ~last_frame.index.isin(self._emitted)
        )
        done_ids = last_frame[done_mask].index.tolist()
        if not done_ids:
            return tracks.iloc[0:0].copy()

        self._emitted.update(done_ids)
        return tracks[tracks["particle"].isin(done_ids)].copy()

    def flow_speed(
        self,
        tracks: pd.DataFrame,
        *,
        aggregator: Callable[[list[float]], float] = np.median,
    ) -> float | None:
        """Aggregate per-particle instantaneous speed (px/frame) across active tracks.

        Speed for a particle uses its last two observations. Returns ``None``
        when no particle has at least two observations.
        """
        if tracks.empty:
            return None

        speeds: list[float] = []
        for _, grp in tracks.groupby("particle", sort=False):
            if len(grp) < 2:
                continue
            g = grp.sort_values("frame")
            dx = g["x"].iloc[-1] - g["x"].iloc[-2]
            dy = g["y"].iloc[-1] - g["y"].iloc[-2]
            dt = g["frame"].iloc[-1] - g["frame"].iloc[-2]
            if dt > 0:
                speeds.append(float(np.hypot(dx, dy) / dt))
        if not speeds:
            return None
        return float(aggregator(speeds))

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _assign_stable_ids(self, linked: pd.DataFrame) -> np.ndarray:
        """Map trackpy's per-call ``particle`` IDs to stable global IDs.

        For each trackpy group, the most common existing global ID among its
        observations wins; if the group has no known observations, a fresh
        global ID is minted.
        """
        stable = np.empty(len(linked), dtype=np.int64)
        for _, grp in linked.groupby("particle", sort=False):
            existing = [
                self._obs_to_particle[int(oid)]
                for oid in grp["obs_id"]
                if int(oid) in self._obs_to_particle
            ]
            if existing:
                pid = Counter(existing).most_common(1)[0][0]
            else:
                pid = self._next_particle_id
                self._next_particle_id += 1
            for oid in grp["obs_id"]:
                self._obs_to_particle[int(oid)] = pid
            stable[grp.index.to_numpy()] = pid
        return stable
