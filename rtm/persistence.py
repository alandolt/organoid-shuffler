"""Persist detections (parquet) and detection images (TIFF)."""
from __future__ import annotations

import os
from pathlib import Path
import numpy as np
import pandas as pd
import tifffile


class DetectionLog:
    """In-memory accumulator that rewrites a single parquet file on each add.

    One row per detection. Small file, so full rewrite is fine.
    """

    def __init__(self, parquet_path: str):
        self.parquet_path = Path(parquet_path)
        self.parquet_path.parent.mkdir(parents=True, exist_ok=True)
        if self.parquet_path.exists():
            self._df = pd.read_parquet(self.parquet_path)
        else:
            self._df = pd.DataFrame()

    def add(self, rows: pd.DataFrame) -> None:
        if rows.empty:
            return
        self._df = pd.concat([self._df, rows], ignore_index=True)
        self._df.to_parquet(self.parquet_path, index=False)

    @property
    def df(self) -> pd.DataFrame:
        return self._df


def save_detection_image(
    image: np.ndarray,
    out_dir: str,
    timestep: int,
    suffix: str = "",
) -> str:
    """Save *image* as a TIFF under *out_dir*/, returning the path."""
    out_dir_p = Path(out_dir)
    out_dir_p.mkdir(parents=True, exist_ok=True)
    name = f"t{timestep:05d}{suffix}.tiff"
    path = out_dir_p / name
    tifffile.imwrite(str(path), image)
    return str(path)
