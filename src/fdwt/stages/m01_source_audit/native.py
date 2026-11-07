"""M1 native metadata and representative data checks.

Read NetCDF, HDF4, raster and archive inputs without creating sidecars.
Used by audit.py. Units and time conventions follow the frozen native SOP.
"""

from calendar import monthrange
from datetime import date, datetime, timedelta
import hashlib
from pathlib import Path
import re
from zipfile import ZipFile

from netCDF4 import Dataset, num2date
import numpy as np
from pyhdf.SD import SD, SDC
import rasterio


ERA = {
    "instant": {**dict.fromkeys(("swvl1", "swvl2", "swvl3", "swvl4"), "m**3 m**-3"),
                "t2m": "K"},
    "accumulated": {"tp": "m", "e": "m of water equivalent", "pev": "m"},
    "surface_runoff": {"sro": "m"},
}
MODIS = {"MCD15A2H": ("Lai_500m", "FparLai_QC", "FparExtra_QC"),
         "MCD12Q1": ("LC_Type1", "QC", "LW")}
TILES = ("h25v04", "h25v05", "h26v04", "h26v05", "h27v05")
SOP = "FD-WT v1.3.1 分产品原生变量与预处理SOP附录"


def json_value(value):
    """Convert native attribute scalars/arrays to finite JSON values."""
    if isinstance(value, (np.generic, np.ndarray)):
        return json_value(value.tolist())
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    if isinstance(value, float) and not np.isfinite(value):
        return str(value)
    return value


def fingerprint(path):
    """Stream file bytes and reject a file modified or replaced during reading."""
    before = path.stat()
    with path.open("rb") as source:
        sha = hashlib.file_digest(source, "sha256").hexdigest()
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (
            after.st_size, after.st_mtime_ns, after.st_ino):
        raise ValueError(f"input changed during hash: {path}")
    return {"bytes": after.st_size, "sha256": sha}


def expected_hours(start, stop):
    """Return inclusive start / exclusive stop UTC epoch seconds."""
    return np.arange(np.datetime64(start, "s"), np.datetime64(stop, "s"),
                     np.timedelta64(1, "h")).astype("int64")


def month_bounds(year, month):
    start = date(year, month, 1)
    return start, start + timedelta(days=monthrange(year, month)[1])


def utc_times(variable):
    """Decode a complete CF time axis to UTC epoch seconds without rounding."""
    values = variable[:]
    if np.ma.getmaskarray(values).any():
        raise ValueError("masked time coordinate")
    decoded = num2date(values, variable.units, getattr(variable, "calendar", "standard"),
                       only_use_cftime_datetimes=False)
    stamps = np.array([item.isoformat() for item in decoded], dtype="datetime64[us]")
    if not len(stamps) or np.any(np.diff(stamps) <= np.timedelta64(0, "us")):
        raise ValueError("empty, reversed or duplicated time coordinate")
    seconds = stamps.astype("datetime64[s]")
    if np.any(stamps != seconds):
        raise ValueError("subsecond timestamps")
    return seconds.astype("int64")


def iso(seconds):
    return str(np.datetime64(int(seconds), "s")) + "Z"


def axis(variable):
    values = np.ma.asarray(variable[:], dtype=float).filled(np.nan)
    if values.ndim != 1 or len(values) < 2 or not np.isfinite(values).all():
        raise ValueError(f"invalid axis {variable.name}")
    delta = np.diff(values)
    if not (np.all(delta > 0) or np.all(delta < 0)):
        raise ValueError(f"nonmonotonic axis {variable.name}")
    return values, "ascending" if delta[0] > 0 else "descending"


def sampled(variable):
    """Read bounded slabs at start, middle and end, preserving masks."""
    selection = tuple(slice(length // 2 - 2, length // 2 + 3) if length >= 5
                      else slice(None) for length in variable.shape[1:])
    return np.ma.concatenate([
        np.ma.asarray(variable[(index, *selection)], dtype=float).ravel()
        for index in sorted({0, variable.shape[0] // 2, variable.shape[0] - 1})
    ]).compressed()


def check_month_hours(stamps, start, stop, terminal_boundary=None):
    """Require a complete month, or a contiguous prefix for terminal context."""
    expected = expected_hours(start, stop)
    if start == terminal_boundary:
        expected = expected[:len(stamps)]
    if not len(stamps) or not np.array_equal(stamps, expected):
        raise ValueError("hourly coordinate differs from named month or terminal prefix")


def nc_metadata(path, product, stream, domain, terminal_boundary=None):
    with Dataset(path) as ds:
        variables = ds.variables
        lat, lat_direction = axis(variables.get("latitude", variables.get("lat")))
        lon, lon_direction = axis(variables.get("longitude", variables.get("lon")))
        time = variables.get("valid_time", variables.get("time"))
        stamps = utc_times(time)
        fields = {}
        warnings = []
        for name, variable in variables.items():
            if variable.ndim < 3:
                continue
            attrs = {key: json_value(variable.getncattr(key)) for key in
                     ("units", "scale_factor", "add_offset", "_FillValue", "missing_value")
                     if key in variable.ncattrs()}
            fields[name] = attrs
            if product == "era5_land" and name in ERA[stream]:
                if variable.dimensions != (time.name, "latitude", "longitude"):
                    raise ValueError(f"{name}: unexpected dimensions {variable.dimensions}")
                values = sampled(variable)
                if not len(values) or not np.isfinite(values).all():
                    raise ValueError(f"{name}: empty or nonfinite representative block")
                if np.ptp(values) == 0:
                    warnings.append(f"{name}: constant representative block")
        if product == "era5_land":
            bounds = domain["cell_center_bounds"]
            x = np.linspace(bounds[0], bounds[2], domain["longitude_count"])
            y = np.linspace(bounds[3], bounds[1], domain["latitude_count"])
            if (lat.shape != y.shape or lon.shape != x.shape or
                    not np.allclose(lat, y, atol=1e-5, rtol=0) or
                    not np.allclose(lon, x, atol=1e-5, rtol=0)):
                raise ValueError("ERA5-Land axes differ from frozen grid or expected orientation")
            match = re.fullmatch(r"ERA5-Land_([a-z]+)_(\d{4})_(\d{2})\.nc", path.name)
            if not match or match[1] != ("instant" if stream == "instant" else "accumulated"):
                raise ValueError("unexpected ERA5-Land filename")
            start, stop = month_bounds(int(match[2]), int(match[3]))
            check_month_hours(stamps, start, stop,
                              None if stream == "instant" else terminal_boundary)
            for name, unit in ERA[stream].items():
                if fields.get(name, {}).get("units") != unit:
                    raise ValueError(f"{name}: missing variable or unexpected unit")
        elif product == "era5_single_levels":
            match = re.search(r"_(\d{4})_(\d{2})\.nc$", path.name)
            start, stop = month_bounds(int(match[1]), int(match[2]))
            if not np.array_equal(stamps, expected_hours(start, stop)):
                raise ValueError("ERA5 hourly coordinate differs from named month")
        elif product in ("esa_cci_sm", "gleam"):
            if product == "esa_cci_sm":
                day = datetime.strptime(re.search(r"-(\d{8})000000-", path.name)[1], "%Y%m%d").date()
                expected = expected_hours(day, day + timedelta(days=1))[::24]
                if "sm" not in fields or "flag" not in fields:
                    raise ValueError("CCI missing sm or flag")
            else:
                year = int(re.search(r"_(\d{4})_", path.name)[1])
                expected = expected_hours(date(year, 1, 1), date(year + 1, 1, 1))[::24]
                if stream not in fields:
                    raise ValueError(f"GLEAM missing {stream}")
            # Daily products may timestamp within their nominal day.
            if not np.array_equal(stamps // 86400, expected // 86400):
                raise ValueError("daily coordinate differs from named coverage")
        return dict(time_start=iso(stamps[0]), time_end=iso(stamps[-1]),
                    spatial_extent=[float(lon.min()), float(lat.min()),
                                    float(lon.max()), float(lat.max())],
                    native_resolution=float(abs(np.median(np.diff(lon)))), crs="EPSG:4326",
                    lat_direction=lat_direction, lon_direction=lon_direction,
                    time_zone="UTC", fields=fields, warnings=warnings)


def modis_metadata(path, stream):
    match = re.fullmatch(r"(MCD\w+)\.A(\d{4})(\d{3})\.(h\d\dv\d\d)\.061\.\d+\.hdf", path.name)
    if not match or match[1] != stream:
        raise ValueError("unexpected MODIS collection or filename")
    year, doy = int(match[2]), int(match[3])
    start = date(year, 1, 1) + timedelta(days=doy - 1)
    period = 8 if stream == "MCD15A2H" else 366
    if start.year != year or doy < 1 or (doy - 1) % period:
        raise ValueError("unexpected MODIS compositing date")
    end = min(date(year, 12, 31), start + timedelta(days=period - 1))
    hdf = SD(str(path), SDC.READ)
    fields = {}
    try:
        attrs = hdf.attributes()
        geo = attrs.get("StructMetadata.0", "")
        if "GCTP_SNSOID" not in geo:
            raise ValueError("MODIS sinusoidal projection absent")
        coords = [float(value) for pair in re.findall(
            r"(?:UpperLeftPointMtrs|LowerRightMtrs)=\(([^,]+),([^\)]+)\)", geo) for value in pair]
        h, v = int(match[4][1:3]), int(match[4][4:6])
        tile_width = 1111950.519667
        expected = [(h - 18) * tile_width, (9 - v) * tile_width,
                    (h - 17) * tile_width, (8 - v) * tile_width]
        if len(coords) != 4 or not np.allclose(coords, expected, atol=0.02, rtol=0):
            raise ValueError("MODIS tile coordinates disagree with filename")
        if not set(MODIS[stream]) <= set(hdf.datasets()):
            raise ValueError("MODIS required SDS absent")
        for name in MODIS[stream]:
            layer = hdf.select(name)
            try:
                if layer.info()[2] != [2400, 2400]:
                    raise ValueError(f"{name}: unexpected dimensions")
                layer_attrs = layer.attributes()
                fields[name] = {key: json_value(layer_attrs[key]) for key in
                                ("units", "scale_factor", "add_offset", "_FillValue", "valid_range")
                                if key in layer_attrs}
                if np.asarray(layer[1200:1205, 1200:1205]).shape != (5, 5):
                    raise ValueError(f"{name}: cannot read representative pixels")
            finally:
                layer.endaccess()
        if stream == "MCD15A2H":
            lai = fields["Lai_500m"]
            if (lai.get("scale_factor"), lai.get("add_offset"), lai.get("_FillValue"),
                    lai.get("valid_range")) != (0.1, 0.0, 255, [0, 100]):
                raise ValueError("LAI scaling, fill or range differs from documented encoding")
            if not all(key in attrs for key in
                       ("MOD15A2_FparLai_QC_DOC", "MOD15A2_FparExtra_QC_DOC")):
                raise ValueError("MODIS embedded QA definitions absent")
    finally:
        hdf.end()
    return dict(time_start=start.isoformat(), time_end=end.isoformat(), tile=match[4],
                spatial_extent=[coords[0], coords[3], coords[2], coords[1]],
                native_resolution=tile_width / 2400, crs="MODIS sinusoidal (m)",
                lat_direction="descending", lon_direction="ascending", time_zone="UTC",
                fields=fields, warnings=[],
                source_url="https://doi.org/10.5067/MODIS/" + stream + ".061")


def raster_metadata(path):
    if path.suffix == ".zip":
        with ZipFile(path) as archive:
            members = [item.filename for item in archive.infolist() if not item.is_dir()]
            if not members:
                raise ValueError("empty archive")
        return dict(fields={}, warnings=["archive inventory only"], members=members)
    uri = f"/vsigzip/{path.as_posix()}" if path.suffix == ".gz" else str(path)
    with rasterio.open(uri) as ds:
        # Read an early strip for gzip streams, which are sequential on disk.
        row = 0 if path.suffix == ".gz" else ds.height // 2
        ds.read(1, window=((row, row + 2), (ds.width // 2, ds.width // 2 + 2)))
        meta = dict(fields={"band_1": {"_FillValue": json_value(ds.nodata),
                                      "scale_factor": ds.scales[0], "add_offset": ds.offsets[0]}},
                    spatial_extent=list(ds.bounds), native_resolution=ds.res[0],
                    crs=str(ds.crs) if ds.crs else "unknown",
                    lat_direction="descending" if ds.transform.e < 0 else "ascending",
                    lon_direction="ascending" if ds.transform.a > 0 else "descending",
                    warnings=[])
    if path.suffix == ".gz":
        match = re.fullmatch(r"GOSIF_(\d{4})(\d{3})\.tif\.gz", path.name)
        start = date(int(match[1]), 1, 1) + timedelta(days=int(match[2]) - 1)
        end = min(date(start.year, 12, 31), start + timedelta(days=7))
        meta.update(time_start=start.isoformat(), time_end=end.isoformat())
    return meta


def interval_amount(values, kind):
    """Convert a 24-step day in native units to a total; no unit/sign conversion."""
    if kind == "processing_step_accumulation":
        if len(values) != 24:
            raise ValueError("need 24 processing steps including next midnight")
        return np.diff(values, axis=0, prepend=np.zeros_like(values[:1])).sum(axis=0)
    if kind == "increment" and len(values) == 24:
        return np.sum(values, axis=0)
    if kind == "interval_total" and len(values) == 1:
        return values[0]
    raise ValueError("unknown or incomplete accumulation")


def accumulation_sample(paths, year, month, stream):
    """Check a three-day cross-month sample at multiple interior grid cells."""
    _, boundary = month_bounds(year, month)
    first = boundary - timedelta(days=2)
    expected = expected_hours(first, boundary + timedelta(days=2))[:73]
    slices = {}
    for path in paths:
        with Dataset(path) as ds:
            stamps = utc_times(ds["valid_time"])
            take = (stamps >= expected[0]) & (stamps <= expected[-1])
            indices = np.flatnonzero(take)
            if not len(indices):
                continue
            for name in ERA[stream]:
                block = ds[name][indices[0]:indices[-1] + 1, 12:81:11, 16:136:13]
                slices.setdefault(name, []).append((stamps[take], block))
    evidence = {}
    for name in ERA[stream]:
        parts = slices.get(name, [])
        if not parts or not np.array_equal(np.concatenate([item[0] for item in parts]), expected):
            raise ValueError(f"{name}: missing or duplicated hours in cross-month evidence")
        raw = np.ma.concatenate([item[1] for item in parts], axis=0)
        values = np.ma.asarray(raw, dtype=float).filled(np.nan).reshape(73, -1)
        values = values[:, np.isfinite(values).all(axis=0)]
        if not values.shape[1]:
            raise ValueError(f"{name}: no complete representative grid cell")
        daily = values[1:].reshape(3, 24, -1)
        ends = daily[:, -1, :]
        totals = np.array([interval_amount(day, "processing_step_accumulation") for day in daily])
        if not np.allclose(totals, ends, rtol=1e-7, atol=1e-10):
            raise ValueError(f"{name}: step totals differ from midnight")
        if name in ("tp", "sro"):
            if (np.any(daily < -1e-7) or np.any(np.diff(daily, axis=1) < -1e-7)
                    or not np.any(ends[:2] - daily[1:, 0, :] > 1e-7)):
                raise ValueError(f"{name}: precipitation/runoff series lacks cumulative-reset evidence")
        elif not np.any(ends < -1e-7):
            raise ValueError(f"{name}: sample contains no negative evaporative loss")
        evidence[name] = dict(start=iso(expected[0]), end=iso(expected[-1]),
                              cells=values.shape[1], days=3,
                              midnight_min=float(ends.min()), midnight_max=float(ends.max()),
                              max_total_error=float(np.max(np.abs(totals - ends))))
    return evidence
