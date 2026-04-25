"""Convpaint segmentation + mask post-processing + region measurement.

All image-analysis primitives live here (one module, three stages):

- :class:`SegmentationModel` — load a pre-trained Convpaint ``.pkl`` and run
  pixel classification. ``.segment(img)`` returns a per-pixel class map;
  ``.segment_and_label(img, ...)`` chains ``segment`` → :func:`clean_and_label`.
- :func:`clean_and_label` — from the class map, extract the binary mask for
  one class, morphologically close gaps, fill holes, drop small objects, and
  assign a unique integer label per connected component.
- :func:`measure_regions` — run :func:`skimage.measure.regionprops_table` on
  a CC-labeled image; returns a DataFrame with area/centroid/bbox per object.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from napari_convpaint.convpaint_model import ConvpaintModel
from scipy import ndimage as ndi
from skimage import measure, morphology


# ----------------------------------------------------------------------
# Segmentation
# ----------------------------------------------------------------------

class SegmentationModel:
    def __init__(self, model_path: str, fe_use_device: str = "auto"):
        """
        Args:
            model_path: path to a .pkl Convpaint model.
            fe_use_device: "auto", "gpu", or "cpu".
        """
        self.model_path = model_path
        self.fe_use_device = fe_use_device
        self._model = ConvpaintModel(model_path=model_path)

    def segment(self, image: np.ndarray) -> np.ndarray:
        """Return a label map with pixel values equal to class ids (0 = background)."""
        return self._model.segment(image, fe_use_device=self.fe_use_device)

    def segment_and_label(
        self,
        image: np.ndarray,
        *,
        class_id: int = 2,
        min_pixel_size: int = 50,
        closing_radius: int = 3,
    ) -> np.ndarray:
        """Segment with Convpaint and return an individually-labeled image.

        Convenience wrapper: ``segment`` → :func:`clean_and_label`.
        Pair with :func:`measure_regions` to get the DataFrame.
        """
        return clean_and_label(
            self.segment(image),
            class_id=class_id,
            min_pixel_size=min_pixel_size,
            closing_radius=closing_radius,
        )


# ----------------------------------------------------------------------
# Mask post-processing
# ----------------------------------------------------------------------

def clean_and_label(
    class_labels: np.ndarray,
    *,
    class_id: int = 2,
    min_pixel_size: int = 50,
    closing_radius: int = 3,
) -> np.ndarray:
    """Extract the class-*class_id* mask, clean it, and connected-component label.

    Designed for a speckled pixel classifier: closes gaps within each blob, then
    fills any enclosed holes (no size cap) so a fragmented detection becomes one
    solid region.

    Steps:

    1. ``mask = class_labels == class_id``
    2. ``binary_closing(disk(closing_radius))`` — bridges gaps up to ~2R pixels wide
    3. ``binary_fill_holes`` — fills every enclosed hole, any size
    4. ``remove_small_objects`` (objects with area ≤ ``min_pixel_size``)
    5. ``measure.label`` (8-connectivity)

    Set ``closing_radius=0`` to skip the closing step.

    Returns an int32 image the same shape as *class_labels*, with background 0
    and each surviving object assigned a unique integer ≥ 1. Returns an all-zero
    array when the class is absent or everything was filtered out.
    """
    mask = class_labels == class_id
    if not mask.any():
        return np.zeros(class_labels.shape, dtype=np.int32)

    if closing_radius > 0:
        mask = morphology.closing(mask, morphology.disk(closing_radius))
    mask = ndi.binary_fill_holes(mask)
    mask = morphology.remove_small_objects(mask, max_size=min_pixel_size)
    if not mask.any():
        return np.zeros(class_labels.shape, dtype=np.int32)

    return measure.label(mask, connectivity=2).astype(np.int32)


# ----------------------------------------------------------------------
# Region measurement
# ----------------------------------------------------------------------

def measure_regions(cc_labels: np.ndarray) -> pd.DataFrame:
    """Measure each connected component in a CC-labeled image.

    Columns: ``region_label, area, centroid_x, centroid_y, bbox_min_row,
    bbox_min_col, bbox_max_row, bbox_max_col``. Returns an empty (but
    correctly typed) DataFrame when there are no regions.
    """
    if not np.any(cc_labels):
        return _empty_df()

    props = measure.regionprops_table(
        cc_labels,
        properties=("label", "area", "centroid", "bbox"),
    )
    df = pd.DataFrame(props).rename(
        columns={
            "label": "region_label",
            "centroid-0": "centroid_y",
            "centroid-1": "centroid_x",
            "bbox-0": "bbox_min_row",
            "bbox-1": "bbox_min_col",
            "bbox-2": "bbox_max_row",
            "bbox-3": "bbox_max_col",
        }
    )
    return df[
        [
            "region_label",
            "area",
            "centroid_x",
            "centroid_y",
            "bbox_min_row",
            "bbox_min_col",
            "bbox_max_row",
            "bbox_max_col",
        ]
    ]


def _empty_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "region_label": pd.Series(dtype="int64"),
            "area": pd.Series(dtype="float64"),
            "centroid_x": pd.Series(dtype="float64"),
            "centroid_y": pd.Series(dtype="float64"),
            "bbox_min_row": pd.Series(dtype="int64"),
            "bbox_min_col": pd.Series(dtype="int64"),
            "bbox_max_row": pd.Series(dtype="int64"),
            "bbox_max_col": pd.Series(dtype="int64"),
        }
    )
