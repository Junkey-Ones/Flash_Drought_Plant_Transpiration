"""M2 UTC aggregation, storage and area math for the frozen existing-data profile.

Input: decoded ERA5-Land arrays or independently verified accumulation intervals.
Output: day/pentad arrays in mm or degrees Celsius, with NaN for missing.
Used by preprocess.py. This module has no CLI and performs no file I/O.
"""

from calendar import isleap
from datetime import date, timedelta

import numpy as np


DAY = timedelta(days=1)
STATE = ("sm_0_100_mm", "sm_deep_mm", "t2m_c")
FLUX = ("p_mm", "et_loss_mm", "pet_loss_mm", "runoff_surface_mm",
        "runoff_subsurface_mm", "runoff_mm", "balance_mm")
VALUES = STATE + FLUX


def calendar_rows(start, end):
    """Return complete UTC pentads intersecting the inclusive output dates."""
    rows = []
    for year in range(start.year, end.year + 1):
        for pentad in range(1, 74):
            first = date(year, 1, 1) + (pentad - 1) * 5 * DAY
            last = date(year, 12, 31) if pentad == 73 else first + 4 * DAY
            if last < start or first > end:
                continue
            index = (year - 2000) * 73 + pentad
            rows.append(dict(t_abs=index, year=year, pentad=pentad, start_date=first,
                             end_date=last, n_days=(last - first).days + 1,
                             is_leap_year=isleap(year), is_pentad73=pentad == 73,
                             previous_t_abs=index - 1, next_t_abs=index + 1))
    return rows


def decoded(values, low=-np.inf, high=np.inf):
    """Convert native masks, infinities and out-of-range values to NaN."""
    clean = np.ma.asarray(values, dtype=np.float64).filled(np.nan)
    clean[~np.isfinite(clean) | (clean < low) | (clean > high)] = np.nan
    return clean


def daily_state(values):
    """Mean of 24 complete UTC hourly samples per cell. Any missing hour propagates."""
    return values.reshape(-1, 24, *values.shape[1:]).mean(axis=1)


def era_storage(layers):
    """Combine four complete hourly (or complete daily mean) theta layers into mm."""
    first, second, third, deep = layers
    return 1000 * (0.07 * first + 0.21 * second + 0.72 * third), 1890 * deep


def interval_total(values, kind):
    """Reduce step 1..24, 24 increments or one preaggregated interval in native units.

    For a verified forecast-step accumulator, step 24 is the direct daily total.
    A missing endpoint remains NaN even when earlier hours are present.
    """
    if kind == "processing_step_accumulation" and len(values) == 24:
        return values[-1]
    if kind == "increment" and len(values) == 24:
        return values.sum(axis=0)
    if kind == "interval_total" and len(values) == 1:
        return values[0]
    raise ValueError("unverified accumulation type or incomplete interval")


def water_mm(total, scale):
    """Apply a verified signed unit factor once, retaining net condensation."""
    return total * scale


def pentad_state(values, min_days, max_gap):
    """Return per-cell mean, count and longest missing run for one full pentad."""
    valid = np.isfinite(values)
    count = valid.sum(axis=0)
    run = np.zeros_like(count)
    longest = np.zeros_like(count)
    for day in valid:
        run = np.where(day, 0, run + 1)
        longest = np.maximum(longest, run)
    accepted = (count >= min_days) & (longest <= max_gap)
    mean = np.divide(np.nansum(values, axis=0), count,
                     out=np.full(count.shape, np.nan), where=accepted)
    return mean, count, longest


def pentad_flux(values):
    """Sum every day, propagating missingness rather than treating it as zero."""
    return values.sum(axis=0)


def cell_area_km2(latitudes, resolution):
    """Spherical full cell footprint areas, one value per latitude row."""
    north = np.radians(latitudes + resolution / 2)
    south = np.radians(latitudes - resolution / 2)
    return 6371.0088**2 * np.radians(resolution) * (np.sin(north) - np.sin(south))


def identity_support(values, threshold):
    """Apply the SOP valid-area rule on the already aligned ERA5-Land grid."""
    fractions = np.isfinite(values).astype(float)
    return np.where(fractions >= threshold, values, np.nan), fractions
