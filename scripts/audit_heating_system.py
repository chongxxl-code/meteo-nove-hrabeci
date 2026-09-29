#!/usr/bin/env python3
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
TZ = ZoneInfo("Europe/Prague")


def load(name):
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def parse_dt(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def close(a, b, tol=1e-6):
    try:
        return abs(float(a) - float(b)) <= tol
    except Exception:
        return False


def main():
    now = datetime.now(timezone.utc)
    profile = load("house-profile.json")
    state = load("house-state.json")
    hdo = load("hdo-schedule.json")
    latest = load("indoor-latest.json")
    history = load("indoor-history.json")
    model = load("indoor-thermal-model.json")
    status = load("status.json")

    checks = []
    warnings = []
    failures = []

    def check(name, ok, detail, severity="fail"):
        item = {"name": name, "ok": bool(ok), "detail": detail}
        checks.append(item)
        if not ok:
            (warnings if severity == "warn" else failures).append(detail)

    boiler = ((profile.get("heating_system") or {}).get("primary_electric_boiler") or {})
    check(
        "boiler_identity",
        boiler.get("model") == "Ray 12 K" and close(boiler.get("rated_heat_output_kw"), 12.1, 0.01),
        "Boiler profile must remain Protherm Ray 12 K with rated heat output 12.1 kW.",
    )
    check(
        "boiler_efficiency",
        close(boiler.get("label_efficiency_percent"), 99.5, 0.01),
        "Boiler label efficiency must remain 99.5%.",
    )

    sensor = ((profile.get("heating_energy_model") or {}).get("sensor_model") or {})
    check(
        "single_reference_sensor",
        sensor.get("reference_temperature") == "EMOS thermostat downstairs"
        and sensor.get("second_sensor_planned") is False,
        "Downstairs EMOS must remain the sole modelling/control temperature reference.",
    )

    pricing = state.get("energy_pricing") or {}
    current_prices = pricing.get("current_variable_price_czk_per_kwh_vat") or {}
    nt_price = current_prices.get("low_tariff")
    vt_price = current_prices.get("high_tariff")
    check(
        "current_nt_price_binding",
        close(pricing.get("tempering_incremental_price_czk_per_kwh"), nt_price, 1e-5)
        and float(nt_price or 0) > 0,
        "Tempering price must bind to the current 2026 D45d NT variable price, not the historical invoice average.",
    )
    check(
        "current_price_pair",
        float(vt_price or 0) > float(nt_price or 0) > 0,
        "Current D45d VT/NT price pair is missing or internally inconsistent.",
    )

    week = hdo.get("week_ahead") or []
    check("hdo_week_length", len(week) == 7, "HDO schedule must contain seven forward days.")
    for day in week:
        date = day.get("date")
        nt = int(day.get("nt_minutes") or 0)
        vt = int(day.get("vt_minutes") or 0)
        timeline = day.get("timeline") or []
        max_vt = max((int(seg.get("minutes") or 0) for seg in timeline if seg.get("tariff") == "VT"), default=0)
        check(
            f"hdo_24h_{date}",
            nt + vt == 1440,
            f"HDO day {date} does not cover exactly 24 hours.",
        )
        check(
            f"hdo_nt_min_{date}",
            nt >= 1200,
            f"HDO day {date} has less than 20 hours of NT.",
        )
        check(
            f"hdo_vt_block_{date}",
            max_vt <= 60,
            f"HDO day {date} contains a VT block longer than 60 minutes.",
        )

    monitor = hdo.get("daily_monitor") or {}
    check(
        "hdo_zero_paid_provider",
        hdo.get("api_cost_czk") == 0 and hdo.get("paid_provider_calls") == 0,
        "HDO monitoring unexpectedly uses a paid provider.",
    )
    check(
        "hdo_exact_refresh_limitation_recorded",
        monitor.get("exact_customer_schedule_auto_refresh") is False,
        "Customer-specific HDO CAPTCHA limitation must be recorded explicitly.",
        severity="warn",
    )

    latest_dt = parse_dt(latest.get("last_timestamp") or latest.get("generated_at"))
    age_min = None if latest_dt is None else (now - latest_dt).total_seconds() / 60
    check(
        "indoor_latest_freshness",
        age_min is not None and age_min <= 45,
        f"Indoor latest sample is stale or missing (age {None if age_min is None else round(age_min, 1)} min).",
        severity="warn",
    )
    points = history.get("points") or []
    check(
        "indoor_history_present",
        len(points) >= 100,
        f"Indoor history has only {len(points)} points.",
    )

    check(
        "dp2_not_used_as_boiler_proof",
        model.get("dp2_used_as_heating_state") is False,
        "DP2 must not be used as proof of actual boiler/relay heating state.",
    )
    check(
        "heating_control_not_overclaimed",
        model.get("validated_for_heating_control") is False,
        "Thermal model is unexpectedly marked validated for heating control.",
    )

    fit = model.get("fit") or {}
    check(
        "passive_fit_positive",
        float(fit.get("coupling_per_h") or 0) > 0 and float(fit.get("time_constant_h") or 0) > 0,
        "Passive thermal fit is missing or non-physical.",
    )

    energy = model.get("heating_energy_model") or {}
    prior = energy.get("heat_loss_prior") or {}
    check(
        "energy_scale_not_overclaimed",
        energy.get("absolute_energy_scale_calibrated") is False
        and prior.get("absolute_scale_calibrated") is False,
        "Absolute kWh scale must remain explicitly uncalibrated until active-heating evidence exists.",
    )

    refs = energy.get("reference_daily_costs") or []
    price = float(energy.get("price_czk_per_kwh") or 0)
    efficiency = float(energy.get("boiler_efficiency_fraction") or 0)
    central_wk = float(((prior.get("w_per_k") or {}).get("central")) or 0)
    target = float(energy.get("tempering_setpoint_c") or 0)
    refs_ok = bool(refs and price > 0 and efficiency > 0 and central_wk > 0)
    if refs_ok:
        for row in refs:
            outside = float(row.get("outside_c"))
            dh = max(0.0, target - outside) * 24.0
            expected_kwh = central_wk / 1000.0 * dh / efficiency
            got_kwh = float(((row.get("electricity_kwh_per_day") or {}).get("central")) or 0)
            expected_cost = expected_kwh * price
            got_cost = float(((row.get("cost_czk_per_day") or {}).get("central")) or 0)
            if abs(expected_kwh - got_kwh) > 0.11 or abs(round(expected_cost) - got_cost) > 1:
                refs_ok = False
                break
    check(
        "reference_energy_math",
        refs_ok,
        "Reference kWh/cost table does not match H × degree-hours / efficiency × current price.",
    )

    actual = energy.get("actual_tempering_need_next_24h") or {}
    if actual.get("status") == "no_heating_call_possible_from_forecast_bound":
        lower = float(actual.get("lower_switch_c"))
        inside = float(actual.get("inside_now_c"))
        outside_min = float(actual.get("forecast_min_outside_c"))
        check(
            "actual_zero_need_proof",
            inside > lower and outside_min >= lower
            and close(actual.get("electricity_kwh"), 0.0)
            and close(actual.get("incremental_cost_czk"), 0.0),
            "Zero next-24h tempering estimate is not supported by the temperature-bound proof.",
        )
    elif actual.get("actual_cost_estimate_available") is not True:
        warnings.append(
            "Actual next-24h heating cost is intentionally withheld because active-cycle energy or threshold timing is not calibrated."
        )

    if model.get("prediction_ready") is not True:
        warnings.append(
            f"Passive forecast is still learning: {model.get('prediction_blocker') or 'prediction-ready gate not met'}."
        )

    if pricing.get("hdo_boiler_blocking", {}).get("installation_status") != "physically_verified":
        warnings.append(
            "D45d requires HDO blocking of electric heating, but the actual boiler wiring has not been physically verified."
        )

    hysteresis_status = ((profile.get("heating_energy_model") or {}).get("thermostat_hysteresis_status") or {})
    if hysteresis_status.get("current_device_setting_machine_read") is not True:
        warnings.append(
            "The 1.0 °C EMOS DIFF setting is used as the working setting but is not machine-read from the thermostat."
        )

    forecast_dt = parse_dt(status.get("collected_at_local"))
    forecast_age_min = None if forecast_dt is None else (now - forecast_dt).total_seconds() / 60
    check(
        "weather_snapshot_freshness",
        forecast_age_min is not None and forecast_age_min <= 240,
        f"Weather snapshot is stale or missing (age {None if forecast_age_min is None else round(forecast_age_min, 1)} min).",
        severity="warn",
    )

    overall = "fail" if failures else ("pass_with_warnings" if warnings else "pass")
    out = {
        "schema": "2026-09-29.heating-audit-v1",
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "overall_status": overall,
        "critical_failures": failures,
        "warnings": list(dict.fromkeys(warnings)),
        "checks": checks,
        "summary": {
            "checks_total": len(checks),
            "checks_failed": sum(1 for item in checks if not item["ok"]),
            "critical_failures": len(failures),
            "warnings": len(list(dict.fromkeys(warnings))),
        },
    }
    (DATA / "heating-audit.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(out["summary"] | {"overall_status": overall}, ensure_ascii=False))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
