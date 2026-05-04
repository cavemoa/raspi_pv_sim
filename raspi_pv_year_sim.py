"""Simulate a solar-powered Raspberry Pi 5 acoustic monitor for one year.

Settings are read from YAML by default. Weather data comes from the Open-Meteo
Historical Weather API and can be cached locally between modelling runs.
"""

from __future__ import annotations

import argparse
import datetime as dt
from dataclasses import dataclass
from copy import deepcopy
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pvlib
import requests
import yaml
from matplotlib.patches import Patch
from pvlib.location import Location


DEFAULT_CONFIG_PATH = Path("raspi_pv_config.yaml")


def default_config() -> dict:
    year = previous_calendar_year()
    return {
        "simulation": {
            "year": year,
        },
        "location": {
            "name": "New Plymouth, New Zealand",
            "latitude": -39.0556,
            "longitude": 174.0752,
            "timezone": "Pacific/Auckland",
        },
        "pv_panel": {
            "rated_power_w": 25.0,
            "tilt_deg": 35.0,
            "azimuth_deg": 0.0,
            "gamma_pdc_per_c": -0.004,
        },
        "battery": {
            "capacity_ah": 20.0,
            "nominal_voltage_v": 12.8,
            "min_soc": 0.10,
            "max_soc": 0.80,
        },
        "load_profile": {
            "idle": {
                "fraction": 0.50,
                "power_w": 3.5,
            },
            "moderate": {
                "fraction": 0.40,
                "power_w": 6.0,
            },
            "heavy": {
                "fraction": 0.10,
                "power_w": 11.5,
            },
        },
        "losses": {
            "charge_efficiency": 0.90,
        },
        "weather_cache": {
            "enabled": True,
            "directory": "weather_cache",
            "refresh": False,
        },
        "output": {
            "save_plot": f"outputs/raspi_pv_{year}.png",
            "show_plot": True,
        },
    }


@dataclass(frozen=True)
class LoadProfile:
    idle_pct: float
    moderate_pct: float
    heavy_pct: float
    idle_w: float = 3.5
    moderate_w: float = 6.0
    heavy_w: float = 11.5

    @property
    def average_w(self) -> float:
        return (
            self.idle_pct * self.idle_w
            + self.moderate_pct * self.moderate_w
            + self.heavy_pct * self.heavy_w
        )


@dataclass(frozen=True)
class SystemConfig:
    year: int
    location_name: str = "New Plymouth, New Zealand"
    latitude: float = -39.0556
    longitude: float = 174.0752
    timezone: str = "Pacific/Auckland"
    panel_w: float = 50.0
    panel_tilt_deg: float = 35.0
    panel_azimuth_deg: float = 0.0
    battery_ah: float = 20.0
    battery_voltage: float = 12.8
    min_soc_fraction: float = 0.10
    max_soc_fraction: float = 0.80
    charge_efficiency: float = 0.90
    gamma_pdc: float = -0.004
    weather_cache_enabled: bool = True
    weather_cache_dir: Path = Path("weather_cache")
    refresh_weather_cache: bool = False

    @property
    def nominal_battery_wh(self) -> float:
        return self.battery_ah * self.battery_voltage

    @property
    def min_battery_wh(self) -> float:
        return self.nominal_battery_wh * self.min_soc_fraction

    @property
    def max_battery_wh(self) -> float:
        return self.nominal_battery_wh * self.max_soc_fraction

    @property
    def operating_battery_wh(self) -> float:
        return self.max_battery_wh - self.min_battery_wh


def parse_pct(value: str) -> float:
    pct = float(value)
    if pct > 1:
        pct /= 100.0
    return pct


def previous_calendar_year(today: dt.date | None = None) -> int:
    today = today or dt.date.today()
    return today.year - 1


def deep_merge(base: dict, override: dict) -> dict:
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_yaml_config(config_path: Path | None) -> dict:
    config = default_config()
    if config_path is None:
        if not DEFAULT_CONFIG_PATH.exists():
            return config
        config_path = DEFAULT_CONFIG_PATH

    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"Config file must contain a YAML mapping at the top level: {config_path}")
    return deep_merge(config, loaded)


def get_nested(config: dict, path: tuple[str, ...]):
    current = config
    for key in path:
        current = current[key]
    return current


def set_nested(config: dict, path: tuple[str, ...], value) -> None:
    current = config
    for key in path[:-1]:
        current = current[key]
    current[path[-1]] = value


def apply_cli_overrides(config: dict, args: argparse.Namespace) -> dict:
    cli_map = {
        "year": ("simulation", "year"),
        "idle_pct": ("load_profile", "idle", "fraction"),
        "moderate_pct": ("load_profile", "moderate", "fraction"),
        "heavy_pct": ("load_profile", "heavy", "fraction"),
        "idle_w": ("load_profile", "idle", "power_w"),
        "moderate_w": ("load_profile", "moderate", "power_w"),
        "heavy_w": ("load_profile", "heavy", "power_w"),
        "panel_w": ("pv_panel", "rated_power_w"),
        "battery_ah": ("battery", "capacity_ah"),
        "battery_voltage": ("battery", "nominal_voltage_v"),
        "min_soc": ("battery", "min_soc"),
        "max_soc": ("battery", "max_soc"),
        "charge_efficiency": ("losses", "charge_efficiency"),
        "latitude": ("location", "latitude"),
        "longitude": ("location", "longitude"),
        "timezone": ("location", "timezone"),
        "tilt": ("pv_panel", "tilt_deg"),
        "azimuth": ("pv_panel", "azimuth_deg"),
        "gamma_pdc": ("pv_panel", "gamma_pdc_per_c"),
        "weather_cache_dir": ("weather_cache", "directory"),
        "save_plot": ("output", "save_plot"),
    }
    merged = deepcopy(config)
    for arg_name, path in cli_map.items():
        value = getattr(args, arg_name)
        if value is not None:
            set_nested(merged, path, value)
    if args.show_plot is not None:
        set_nested(merged, ("output", "show_plot"), args.show_plot)
    if args.weather_cache_enabled is not None:
        set_nested(merged, ("weather_cache", "enabled"), args.weather_cache_enabled)
    if args.refresh_weather_cache is not None:
        set_nested(merged, ("weather_cache", "refresh"), args.refresh_weather_cache)
    return merged


def build_models_from_config(config: dict) -> tuple[SystemConfig, LoadProfile, Path | None, bool]:
    load = LoadProfile(
        idle_pct=parse_pct(str(get_nested(config, ("load_profile", "idle", "fraction")))),
        moderate_pct=parse_pct(str(get_nested(config, ("load_profile", "moderate", "fraction")))),
        heavy_pct=parse_pct(str(get_nested(config, ("load_profile", "heavy", "fraction")))),
        idle_w=float(get_nested(config, ("load_profile", "idle", "power_w"))),
        moderate_w=float(get_nested(config, ("load_profile", "moderate", "power_w"))),
        heavy_w=float(get_nested(config, ("load_profile", "heavy", "power_w"))),
    )
    config_model = SystemConfig(
        year=int(get_nested(config, ("simulation", "year"))),
        location_name=str(get_nested(config, ("location", "name"))),
        latitude=float(get_nested(config, ("location", "latitude"))),
        longitude=float(get_nested(config, ("location", "longitude"))),
        timezone=str(get_nested(config, ("location", "timezone"))),
        panel_w=float(get_nested(config, ("pv_panel", "rated_power_w"))),
        panel_tilt_deg=float(get_nested(config, ("pv_panel", "tilt_deg"))),
        panel_azimuth_deg=float(get_nested(config, ("pv_panel", "azimuth_deg"))),
        battery_ah=float(get_nested(config, ("battery", "capacity_ah"))),
        battery_voltage=float(get_nested(config, ("battery", "nominal_voltage_v"))),
        min_soc_fraction=parse_pct(str(get_nested(config, ("battery", "min_soc")))),
        max_soc_fraction=parse_pct(str(get_nested(config, ("battery", "max_soc")))),
        charge_efficiency=float(get_nested(config, ("losses", "charge_efficiency"))),
        gamma_pdc=float(get_nested(config, ("pv_panel", "gamma_pdc_per_c"))),
        weather_cache_enabled=bool(get_nested(config, ("weather_cache", "enabled"))),
        weather_cache_dir=Path(get_nested(config, ("weather_cache", "directory"))),
        refresh_weather_cache=bool(get_nested(config, ("weather_cache", "refresh"))),
    )
    save_plot = get_nested(config, ("output", "save_plot"))
    output_path = Path(save_plot) if save_plot else None
    show_plot = bool(get_nested(config, ("output", "show_plot")))
    return config_model, load, output_path, show_plot


def weather_cache_path(config: SystemConfig) -> Path:
    lat = f"{config.latitude:.4f}".replace("-", "S").replace(".", "p")
    lon = f"{config.longitude:.4f}".replace("-", "W").replace(".", "p")
    return config.weather_cache_dir / f"open_meteo_{config.year}_{lat}_{lon}.csv"


def read_cached_weather(cache_path: Path, timezone: str) -> pd.DataFrame:
    weather = pd.read_csv(cache_path)
    if "time" not in weather.columns:
        raise ValueError(f"Cached weather file has no 'time' column: {cache_path}")
    weather["time"] = pd.to_datetime(weather["time"], utc=True)
    return weather.set_index("time").tz_convert(timezone)


def write_cached_weather(cache_path: Path, weather_utc: pd.DataFrame) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    weather_utc.to_csv(cache_path, index_label="time")


def fetch_open_meteo_weather(config: SystemConfig) -> pd.DataFrame:
    cache_path = weather_cache_path(config)
    if config.weather_cache_enabled and not config.refresh_weather_cache and cache_path.exists():
        try:
            print(f"Loading cached Open-Meteo weather from {cache_path}...")
            return read_cached_weather(cache_path, config.timezone)
        except Exception as exc:
            print(f"Could not read cached weather ({exc}); downloading a fresh copy.")

    start_date = f"{config.year}-01-01"
    # One extra local date gives enough timeline to simulate 31 Dec sunset
    # through 1 Jan sunrise without needing PV production for the new year.
    end_date = f"{config.year + 1}-01-01"
    url = "https://archive-api.open-meteo.com/v1/archive"
    params = {
        "latitude": config.latitude,
        "longitude": config.longitude,
        "start_date": start_date,
        "end_date": end_date,
        "hourly": ",".join(
            [
                "shortwave_radiation",
                "direct_normal_irradiance",
                "diffuse_radiation",
                "temperature_2m",
                "wind_speed_10m",
            ]
        ),
        "timezone": "UTC",
    }

    print(f"Downloading Open-Meteo historical weather for {config.year}...")
    response = requests.get(url, params=params, timeout=60)
    response.raise_for_status()
    payload = response.json()

    hourly = payload.get("hourly")
    if not hourly:
        raise RuntimeError(f"Open-Meteo response did not contain hourly data: {payload}")

    weather_utc = pd.DataFrame(hourly)
    weather_utc["time"] = pd.to_datetime(weather_utc["time"], utc=True)
    weather_utc = weather_utc.set_index("time")

    if config.weather_cache_enabled:
        write_cached_weather(cache_path, weather_utc)
        print(f"Cached Open-Meteo weather at {cache_path}.")

    return weather_utc.tz_convert(config.timezone)


def sun_times_for_year(config: SystemConfig) -> pd.DataFrame:
    loc = Location(config.latitude, config.longitude, tz=config.timezone)
    dates = pd.date_range(
        f"{config.year}-01-01",
        f"{config.year + 1}-01-01",
        freq="D",
    )
    # pvlib's sunrise/sunset date selection is UTC based. Noon UTC maps cleanly
    # to the matching civil date in New Zealand for this use case.
    utc_noons = pd.DatetimeIndex(
        [pd.Timestamp(date.date()).tz_localize("UTC") + pd.Timedelta(hours=12) for date in dates]
    )
    sun = loc.get_sun_rise_set_transit(utc_noons)
    for column in ["sunrise", "sunset", "transit"]:
        sun[column] = sun[column].dt.tz_convert(config.timezone)
    sun.index = dates.date
    return sun


def add_pv_generation(weather: pd.DataFrame, config: SystemConfig) -> pd.DataFrame:
    loc = Location(config.latitude, config.longitude, tz=config.timezone)
    solar_position = loc.get_solarposition(weather.index)

    dni = weather["direct_normal_irradiance"].fillna(0).clip(lower=0)
    ghi = weather["shortwave_radiation"].fillna(0).clip(lower=0)
    dhi = weather["diffuse_radiation"].fillna(0).clip(lower=0)

    tilted = pvlib.irradiance.get_total_irradiance(
        surface_tilt=config.panel_tilt_deg,
        surface_azimuth=config.panel_azimuth_deg,
        solar_zenith=solar_position["apparent_zenith"],
        solar_azimuth=solar_position["azimuth"],
        dni=dni,
        ghi=ghi,
        dhi=dhi,
    )

    wind_m_s = weather["wind_speed_10m"].fillna(0).clip(lower=0) / 3.6
    cell_temp = pvlib.temperature.pvsyst_cell(
        poa_global=tilted["poa_global"].fillna(0).clip(lower=0),
        temp_air=weather["temperature_2m"].ffill().bfill(),
        wind_speed=wind_m_s,
    )
    pv_power_w = pvlib.pvsystem.pvwatts_dc(
        effective_irradiance=tilted["poa_global"].fillna(0).clip(lower=0),
        temp_cell=cell_temp,
        pdc0=config.panel_w,
        gamma_pdc=config.gamma_pdc,
    )

    result = weather.copy()
    result["pv_power_w"] = pv_power_w.fillna(0).clip(lower=0)
    result["usable_pv_energy_wh"] = result["pv_power_w"] * config.charge_efficiency
    return result


def build_night_windows(config: SystemConfig, load: LoadProfile) -> pd.DataFrame:
    sun = sun_times_for_year(config)
    records = []
    start = dt.date(config.year, 1, 1)
    end = dt.date(config.year, 12, 31)

    for night_date in pd.date_range(start, end, freq="D").date:
        next_date = night_date + dt.timedelta(days=1)
        sunset = sun.loc[night_date, "sunset"]
        sunrise = sun.loc[next_date, "sunrise"]
        night_hours = (sunrise - sunset).total_seconds() / 3600.0
        records.append(
            {
                "night_date": pd.Timestamp(night_date),
                "sunset": sunset,
                "sunrise": sunrise,
                "night_hours": night_hours,
                "required_wh": night_hours * load.average_w,
            }
        )

    return pd.DataFrame(records).set_index("night_date")


def overlap_hours(
    interval_start: pd.Timestamp,
    interval_end: pd.Timestamp,
    window_start: pd.Timestamp,
    window_end: pd.Timestamp,
) -> float:
    latest_start = max(interval_start, window_start)
    earliest_end = min(interval_end, window_end)
    seconds = (earliest_end - latest_start).total_seconds()
    return max(0.0, seconds / 3600.0)


def assign_night_load(
    hourly: pd.DataFrame,
    night_windows: pd.DataFrame,
    load: LoadProfile,
) -> pd.DataFrame:
    result = hourly.copy()
    result["night_date"] = pd.NaT
    result["load_energy_wh"] = 0.0

    for night_date, row in night_windows.iterrows():
        mask = (result.index < row["sunrise"]) & (result.index + pd.Timedelta(hours=1) > row["sunset"])
        for timestamp in result.index[mask]:
            hours = overlap_hours(
                timestamp,
                timestamp + pd.Timedelta(hours=1),
                row["sunset"],
                row["sunrise"],
            )
            if hours:
                result.loc[timestamp, "night_date"] = night_date
                result.loc[timestamp, "load_energy_wh"] += hours * load.average_w

    return result


def simulate_battery(hourly: pd.DataFrame, config: SystemConfig) -> pd.DataFrame:
    result = hourly.copy()
    soc_wh = config.max_battery_wh
    shutdown_nights = set()
    soc_values = []
    unmet_values = []
    running_values = []

    for _, row in result.iterrows():
        soc_wh = min(config.max_battery_wh, soc_wh + row["usable_pv_energy_wh"])
        unmet = 0.0
        running = True
        if row["load_energy_wh"] > 0:
            night_date = row["night_date"]
            if pd.notna(night_date) and night_date in shutdown_nights:
                running = False
                unmet = row["load_energy_wh"]
            else:
                available_wh = max(0.0, soc_wh - config.min_battery_wh)
                supplied = min(available_wh, row["load_energy_wh"])
                soc_wh -= supplied
                unmet = row["load_energy_wh"] - supplied
                if unmet > 0.01 and pd.notna(night_date):
                    shutdown_nights.add(night_date)
                    running = False

        soc_values.append(soc_wh)
        unmet_values.append(unmet)
        running_values.append(running)

    result["soc_wh"] = soc_values
    result["soc_pct"] = np.array(soc_values) / config.nominal_battery_wh * 100.0
    result["unmet_load_wh"] = unmet_values
    result["system_running"] = running_values
    return result


def daily_summary(
    hourly: pd.DataFrame,
    night_windows: pd.DataFrame,
    config: SystemConfig,
) -> pd.DataFrame:
    local_dates = pd.Series(hourly.index.date, index=hourly.index)
    daily_pv = hourly.groupby(local_dates)["usable_pv_energy_wh"].sum()
    daily_pv.index = pd.to_datetime(daily_pv.index)

    nightly = night_windows.copy()
    nightly["usable_pv_wh"] = daily_pv.reindex(nightly.index).fillna(0)
    nightly["energy_margin_wh"] = nightly["usable_pv_wh"] - nightly["required_wh"]

    unmet_by_night = hourly.dropna(subset=["night_date"]).groupby("night_date")["unmet_load_wh"].sum()
    min_soc_by_night = hourly.dropna(subset=["night_date"]).groupby("night_date")["soc_pct"].min()
    end_soc_by_night = hourly.dropna(subset=["night_date"]).groupby("night_date")["soc_pct"].last()

    nightly["unmet_load_wh"] = unmet_by_night.reindex(nightly.index).fillna(0)
    nightly["min_soc_pct"] = min_soc_by_night.reindex(nightly.index).fillna(100)
    nightly["end_soc_pct"] = end_soc_by_night.reindex(nightly.index).fillna(100)
    nightly["early_shutdown"] = nightly["unmet_load_wh"] > 0.01
    nightly["solar_excess_for_night"] = nightly["energy_margin_wh"] >= 0
    nightly["battery_reached_full"] = (
        hourly.groupby(local_dates)["soc_wh"].max().reindex(nightly.index).fillna(0)
        >= config.max_battery_wh - 0.01
    )
    return nightly


def print_monthly_report(daily: pd.DataFrame, config: SystemConfig, load: LoadProfile) -> None:
    monthly = daily.groupby(daily.index.month).agg(
        total_days=("early_shutdown", "count"),
        complete_nights=("early_shutdown", lambda values: int((~values).sum())),
        early_shutdowns=("early_shutdown", "sum"),
        solar_excess_days=("solar_excess_for_night", "sum"),
        full_battery_days=("battery_reached_full", "sum"),
        mean_daily_pv_wh=("usable_pv_wh", "mean"),
        mean_required_wh=("required_wh", "mean"),
        mean_margin_wh=("energy_margin_wh", "mean"),
        mean_night_hours=("night_hours", "mean"),
        total_night_hours=("night_hours", "sum"),
        max_unmet_wh=("unmet_load_wh", "max"),
        lowest_soc_pct=("min_soc_pct", "min"),
        unmet_wh=("unmet_load_wh", "sum"),
    )

    print()
    print("=" * 72)
    print(f"Raspberry Pi PV simulation: {config.location_name}, {config.year}")
    print("=" * 72)
    print(f"Panel: {config.panel_w:.0f} W, tilt {config.panel_tilt_deg:.0f} deg, azimuth {config.panel_azimuth_deg:.0f} deg true north")
    print(
        f"Battery: {config.battery_ah:.0f} Ah @ {config.battery_voltage:.1f} V "
        f"({config.nominal_battery_wh:.0f} Wh nominal, "
        f"{config.operating_battery_wh:.0f} Wh between "
        f"{config.min_soc_fraction:.0%}-{config.max_soc_fraction:.0%} SOC)"
    )
    print(
        "Load: "
        f"{load.average_w:.2f} W average "
        f"(idle {load.idle_pct:.0%}, moderate {load.moderate_pct:.0%}, heavy {load.heavy_pct:.0%})"
    )
    print(f"Charge efficiency: {config.charge_efficiency:.0%}")
    print("-" * 72)

    for month, row in monthly.iterrows():
        month_name = dt.date(config.year, int(month), 1).strftime("%B").upper()
        print(month_name)
        print(f"  Sufficient sunset-to-sunrise nights : {int(row['complete_nights']):2d} / {int(row['total_days']):2d}")
        print(f"  Early shutdown nights               : {int(row['early_shutdowns']):2d}")
        print(f"  Days with solar energy surplus      : {int(row['solar_excess_days']):2d}")
        print(f"  Days battery reached max SOC        : {int(row['full_battery_days']):2d}")
        print(f"  Mean usable PV generation           : {row['mean_daily_pv_wh']:6.0f} Wh/day")
        print(f"  Mean overnight requirement          : {row['mean_required_wh']:6.0f} Wh/night")
        print(f"  Mean daily energy margin            : {row['mean_margin_wh']:6.0f} Wh")
        print(f"  Mean night duration                 : {row['mean_night_hours']:6.2f} h")
        print(f"  Lowest overnight battery SOC        : {row['lowest_soc_pct']:6.1f} %")
        print(f"  Total unmet load                    : {row['unmet_wh']:6.1f} Wh")
        if row["early_shutdowns"] > 0:
            lost_hours = row["unmet_wh"] / load.average_w
            lost_pct = lost_hours / row["total_night_hours"] * 100.0
            max_lost_hours = row["max_unmet_wh"] / load.average_w
            print(f"  Operating time lost to shutdowns    : {lost_pct:6.1f} %")
            print(f"  Max time lost on one night          : {max_lost_hours:6.2f} h")
        print("-" * 72)


def make_plot(
    hourly: pd.DataFrame,
    daily: pd.DataFrame,
    config: SystemConfig,
    output_path: Path | None,
    show_plot: bool,
) -> None:
    fig, axes = plt.subplots(
        3,
        1,
        figsize=(15, 11),
        sharex=False,
        gridspec_kw={"height_ratios": [2.0, 1.8, 1.0]},
    )
    fig.suptitle(f"Solar Powered Raspberry Pi Acoustic Monitor - {config.location_name} ({config.year})")

    axes[0].plot(
        daily.index,
        daily["usable_pv_wh"],
        color="steelblue",
        alpha=0.35,
        linewidth=1.0,
        label="Daily usable PV energy",
    )
    axes[0].plot(
        daily.index,
        daily["usable_pv_wh"].rolling(window=7, center=True, min_periods=1).mean(),
        color="navy",
        linewidth=2.0,
        label="7-day mean usable PV energy",
    )
    axes[0].plot(
        daily.index,
        daily["required_wh"],
        color="black",
        linestyle="--",
        linewidth=1.5,
        label="Daily required overnight energy",
    )
    axes[0].set_ylabel("Energy (Wh)")
    axes[0].set_title("Daily PV Energy Production and Overnight Requirement")
    axes[0].grid(True, linestyle=":", alpha=0.55)
    axes[0].legend(loc="upper right")

    axes[1].plot(hourly.index, hourly["soc_pct"], color="darkorange", linewidth=1.1)
    axes[1].axhline(
        config.max_soc_fraction * 100.0,
        color="forestgreen",
        linestyle="--",
        linewidth=1.1,
        label=f"Max SOC ({config.max_soc_fraction:.0%})",
    )
    axes[1].axhline(
        config.min_soc_fraction * 100.0,
        color="firebrick",
        linestyle="--",
        linewidth=1.1,
        label=f"Shutdown SOC ({config.min_soc_fraction:.0%})",
    )
    axes[1].set_ylim(-2, 102)
    axes[1].set_ylabel("SOC (%)")
    axes[1].set_title("Battery State of Charge")
    axes[1].grid(True, linestyle=":", alpha=0.55)
    axes[1].legend(loc="upper right")

    colors = np.where(daily["energy_margin_wh"] >= 0, "forestgreen", "firebrick")
    axes[2].bar(daily.index, daily["energy_margin_wh"], color=colors, width=1.0)
    axes[2].axhline(0, color="black", linewidth=0.8)
    axes[2].set_ylabel("Margin (Wh)")
    axes[2].set_title("Daily Solar Energy Margin for the Following Overnight Run")
    axes[2].legend(
        handles=[
            Patch(facecolor="forestgreen", label="Solar surplus for overnight run"),
            Patch(facecolor="firebrick", label="Solar shortfall for overnight run"),
        ],
        loc="upper right",
    )
    axes[2].grid(True, axis="y", linestyle=":", alpha=0.55)

    for axis in axes:
        axis.margins(x=0)

    fig.tight_layout()

    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(output_path, dpi=160)
        print(f"Saved plot to {output_path}")

    if show_plot:
        plt.show()
    else:
        plt.close(fig)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Model PV and battery performance for a Raspberry Pi 5 overnight acoustic monitor. "
            "Settings are read from raspi_pv_config.yaml by default when it exists; command-line "
            "arguments override YAML values."
        )
    )
    parser.add_argument("--config", type=Path, default=None, help="YAML config file to load.")
    parser.add_argument(
        "--no-config",
        action="store_true",
        help=f"Ignore {DEFAULT_CONFIG_PATH} and use built-in defaults plus command-line overrides.",
    )
    parser.add_argument("--year", type=int, default=None, help="Calendar year to simulate.")
    parser.add_argument("--idle-pct", type=parse_pct, default=None, help="Idle percentage or fraction.")
    parser.add_argument("--moderate-pct", type=parse_pct, default=None, help="Moderate-load percentage or fraction.")
    parser.add_argument("--heavy-pct", type=parse_pct, default=None, help="Heavy-load percentage or fraction.")
    parser.add_argument("--idle-w", type=float, default=None, help="Idle load in watts.")
    parser.add_argument("--moderate-w", type=float, default=None, help="Moderate load in watts.")
    parser.add_argument("--heavy-w", type=float, default=None, help="Heavy load in watts.")
    parser.add_argument("--panel-w", type=float, default=None, help="PV panel rating in watts.")
    parser.add_argument("--battery-ah", type=float, default=None, help="Battery capacity in amp-hours.")
    parser.add_argument("--battery-voltage", type=float, default=None, help="Battery nominal voltage.")
    parser.add_argument("--min-soc", type=parse_pct, default=None, help="Minimum battery SOC before shutdown.")
    parser.add_argument("--max-soc", type=parse_pct, default=None, help="Maximum battery SOC allowed while charging.")
    parser.add_argument("--charge-efficiency", type=float, default=None, help="PV-to-battery efficiency.")
    parser.add_argument("--latitude", type=float, default=None)
    parser.add_argument("--longitude", type=float, default=None)
    parser.add_argument("--timezone", default=None)
    parser.add_argument("--tilt", type=float, default=None, help="Panel tilt in degrees.")
    parser.add_argument("--azimuth", type=float, default=None, help="Panel azimuth in pvlib degrees, 0=true north.")
    parser.add_argument("--gamma-pdc", type=float, default=None, help="PVWatts temperature coefficient per deg C.")
    cache = parser.add_mutually_exclusive_group()
    cache.add_argument("--weather-cache", dest="weather_cache_enabled", action="store_true", help="Use local weather cache.")
    cache.add_argument("--no-weather-cache", dest="weather_cache_enabled", action="store_false", help="Always download weather data.")
    parser.add_argument("--weather-cache-dir", type=Path, default=None, help="Directory for cached Open-Meteo CSV files.")
    parser.add_argument(
        "--refresh-weather-cache",
        action="store_true",
        default=None,
        help="Download weather again and replace the cached CSV.",
    )
    parser.add_argument("--save-plot", type=Path, default=None, help="Optional path to save the matplotlib figure.")
    display = parser.add_mutually_exclusive_group()
    display.add_argument("--show", dest="show_plot", action="store_true", help="Open the matplotlib plot window.")
    display.add_argument("--no-show", dest="show_plot", action="store_false", help="Do not open the matplotlib plot window.")
    parser.set_defaults(show_plot=None)
    parser.set_defaults(weather_cache_enabled=None)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.no_config and args.config is not None:
        raise ValueError("Use either --config or --no-config, not both.")

    raw_config = default_config() if args.no_config else load_yaml_config(args.config)
    raw_config = apply_cli_overrides(raw_config, args)
    config, load, output_path, show_plot = build_models_from_config(raw_config)

    total_load_fraction = load.idle_pct + load.moderate_pct + load.heavy_pct
    if not np.isclose(total_load_fraction, 1.0, atol=0.001):
        raise ValueError(
            "Load percentages must sum to 100% "
            f"(received {total_load_fraction:.2%})."
        )
    if not 0 <= config.min_soc_fraction < config.max_soc_fraction <= 1:
        raise ValueError(
            "Battery SOC limits must satisfy 0 <= min_soc < max_soc <= 100% "
            f"(received min={config.min_soc_fraction:.1%}, max={config.max_soc_fraction:.1%})."
        )

    print("Calculating sunset-to-sunrise load windows...")
    night_windows = build_night_windows(config, load)

    print("Loading Open-Meteo historical weather...")
    weather = fetch_open_meteo_weather(config)
    sim_start = pd.Timestamp(f"{config.year}-01-01 00:00", tz=config.timezone)
    sim_end = night_windows["sunrise"].max().ceil("h")
    weather = weather.loc[(weather.index >= sim_start) & (weather.index < sim_end)]

    print("Calculating PV generation...")
    hourly = add_pv_generation(weather, config)
    hourly = assign_night_load(hourly, night_windows, load)
    print("Simulating battery state of charge...")
    hourly = simulate_battery(hourly, config)
    daily = daily_summary(hourly, night_windows, config)

    print_monthly_report(daily, config, load)
    make_plot(hourly, daily, config, output_path, show_plot=show_plot)


if __name__ == "__main__":
    main()
