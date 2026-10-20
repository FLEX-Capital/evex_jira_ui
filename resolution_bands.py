"""Resolution-time bands for the Länder tab.

Pure classification: business hours in, band label out. It lives outside
data_transformation.py so the labels, colours and thresholds can be imported
by app.py and backfill_country.py without dragging in that module's Jira and
Assets machinery.

BAND_ORDER is the order the bands must be read in - it drives the stacking and
legend order of the Länder chart, where the colours are a ramp and only mean
anything in sequence.
"""

from typing import Any, cast

import numpy as np
import pandas as pd
from pandas._libs.missing import NAType

from reporting_policy import WORKING_DAY_HOURS

BAND_UNDER_1H = "≤ 1 Stunde"
BAND_UNDER_1D = "≤ 1 Arbeitstag"
BAND_OVER_1D = "> 1 Arbeitstag"
BAND_NOT_DONE = "Nicht abgeschlossen"
BAND_UNKNOWN_DURATION = "Unbekannte Dauer"
BAND_UNKNOWN_COMPLETION = "Unbekannter Abschlussstatus"

BAND_ORDER = [
    BAND_UNDER_1H,
    BAND_UNDER_1D,
    BAND_OVER_1D,
    BAND_UNKNOWN_DURATION,
    BAND_NOT_DONE,
    BAND_UNKNOWN_COMPLETION,
]

BAND_COLORS = {
    BAND_UNDER_1H: "green",
    BAND_UNDER_1D: "#FFD700",
    BAND_OVER_1D: "#D62728",
    BAND_NOT_DONE: "#9E9E9E",
    BAND_UNKNOWN_DURATION: "#9467BD",
    BAND_UNKNOWN_COMPLETION: "#607D8B",
}

ONE_HOUR = 1.0

# The dashboard's business window is 08:00-18:00 (BUS_START/BUS_END in
# data_transformation.py), so one working day is ten business hours.
HOURS_PER_WORKING_DAY = WORKING_DAY_HOURS


def classify_band(
    biz_hours: float | np.floating | NAType | None,
    is_done: bool | np.bool_ | NAType | None,
    hours_per_working_day: float = HOURS_PER_WORKING_DAY,
) -> str:
    """Return the resolution band label for a single ticket."""
    if pd.isna(cast(Any, is_done)):
        return BAND_UNKNOWN_COMPLETION
    if not is_done:
        return BAND_NOT_DONE
    if biz_hours is None or pd.isna(biz_hours):
        return BAND_UNKNOWN_DURATION
    if biz_hours < 0:
        return BAND_UNKNOWN_DURATION
    if biz_hours <= ONE_HOUR:
        return BAND_UNDER_1H
    if biz_hours <= hours_per_working_day:
        return BAND_UNDER_1D
    return BAND_OVER_1D


def classify_bands(
    biz_hours: pd.Series, is_done: pd.Series, hours_per_working_day: float = HOURS_PER_WORKING_DAY
) -> pd.Series:
    """Vectorised classify_band: two Series in, a Series of labels out."""
    hours = pd.Series(biz_hours)
    done = pd.Series(is_done, index=hours.index, dtype="boolean")

    # Widest band first, then narrow - each assignment overwrites the last.
    bands = pd.Series(BAND_OVER_1D, index=hours.index, dtype="object")
    bands[hours <= hours_per_working_day] = BAND_UNDER_1D
    bands[hours <= ONE_HOUR] = BAND_UNDER_1H
    # NaN comparisons above are False, so unresolved rows land here.
    bands[hours.isna() | (hours < 0)] = BAND_UNKNOWN_DURATION
    bands[done.eq(False).fillna(False)] = BAND_NOT_DONE
    bands[done.isna()] = BAND_UNKNOWN_COMPLETION
    return bands
