#!/usr/bin/env python3
from __future__ import annotations

import copy
import json
import math
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import build_indoor_thermal_model as thermal

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


def passive_fit_contract_ok(fit):
    try:
        coupling = float(fit.get("coupling_per_h"))
        p25 = float(fit.get("coupling_p25_per_h"))
        p75 = float(fit.get("coupling_p75_per_h"))
        tau = float(fit.get("time_constant_h"))
    except Exception:
        return False
    if not (0 < p25 <= coupling <= p75):
        return False
    expected_tau = 1.0 / coupling
    return abs(tau - expected_tau) <= max(1.0, expected_tau * 0.02)


def run_oracle_negative_controls(now):
    """Exercise production gates with known-good and deliberately bad synthetic cases."""
    start = 1200
    indoor = {
        start + i: {
            "temperature_c": 15.0 - 0.05 * i,
            "setpoint_c": 7.0,
        }
        for i in range(thermal.WINDOW_BINS + 1)
    }
    weather = {
        start + i: {"temperature_c": 5.0}
        for i in range(thermal.WINDOW_BINS + 1)
    }

    results = []

    baseline, _, _ = thermal.build_windows(indoor, weather, [])
    results.append({
        "name": "known_good_passive_window",
        "expected": "accepted",
        "passed": len(baseline) == 1,
    })

    stale_latest = {
        "latest_indoor_c": 12.0,
        "last_timestamp": (now - timedelta(minutes=31)).isoformat().replace("+00:00", "Z"),
    }
    stale_state = thermal.resolve_current_indoor_state(
        stale_latest,
        {"stage": "completed", "ok": True},
        now,
    )
    results.append({
        "name": "stale_indoor_sensor",
        "expected": "UNKNOWN/not current",
        "passed": stale_state.get("ok") is False,
    })

    gapped = copy.deepcopy(indoor)
    gapped.pop(start + 5)
    gapped.pop(start + 6)
    accepted, rejected, _ = thermal.build_windows(gapped, weather, [])
    results.append({
        "name": "large_indoor_gap",
        "expected": "rejected",
        "passed": not accepted and rejected.get("indoor_gap", 0) > 0,
    })

    swapped_indoor = {
        key: {"temperature_c": weather[key]["temperature_c"], "setpoint_c": 0.0}
        for key in weather
    }
    swapped_weather = {
        key: {"temperature_c": indoor[key]["temperature_c"]}
        for key in indoor
    }
    accepted, rejected, _ = thermal.build_windows(swapped_indoor, swapped_weather, [])
    results.append({
        "name": "swapped_indoor_outdoor_series",
        "expected": "rejected",
        "passed": not accepted and rejected.get("gap", 0) > 0,
    })

    shifted_weather = {
        key + thermal.WINDOW_BINS * 2: value
        for key, value in weather.items()
    }
    accepted, _, _ = thermal.build_windows(indoor, shifted_weather, [])
    results.append({
        "name": "shifted_weather_timestamps",
        "expected": "rejected/no aligned window",
        "passed": not accepted,
    })

    jumped = copy.deepcopy(indoor)
    jumped[start + 6]["temperature_c"] += 8.0
    accepted, rejected, _ = thermal.build_windows(jumped, weather, [])
    results.append({
        "name": "unrealistic_temperature_jump",
        "expected": "rejected as heat gain/anomaly",
        "passed": not accepted and rejected.get("heat_gain", 0) > 0,
    })

    one_branch = thermal.forecast_branch_coverage(1)
    results.append({
        "name": "forecast_insufficient_model_coverage",
        "expected": "withheld",
        "passed": one_branch.get("coverage_sufficient") is False,
    })

    bad_fit = {
        "coupling_per_h": 0.025,
        "coupling_p25_per_h": 0.003,
        "coupling_p75_per_h": 0.006,
        "time_constant_h": 40.0,
    }
    results.append({
        "name": "intentionally_wrong_thermal_constant",
        "expected": "rejected",
        "passed": passive_fit_contract_ok(bad_fit) is False,
    })

    return results


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
    generic_prices = pricing.get("variable_price_czk_per_kwh_vat") or {}
    check(
        "generic_price_alias_is_current",
        close(generic_prices.get("low_tariff"), nt_price, 1e-5)
        and close(generic_prices.get("high_tariff"), vt_price, 1e-5),
        "Generic variable-price alias must point to the current tariff, not a historical invoice average.",
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
    )
    if monitor.get("exact_customer_schedule_auto_refresh") is False:
        warnings.append(
            "Exact customer-specific HDO export is not auto-refreshed because the ČEZ DIP endpoint is CAPTCHA-protected; the current verified export is combined with a daily public-source change monitor."
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

    filters = model.get("filters") or {}
    check(
        "passive_window_indoor_coverage_guard",
        int(filters.get("minimum_indoor_points_per_window") or 0) >= 10
        and int(filters.get("maximum_indoor_gap_minutes") or 9999) <= 60
        and filters.get("reject_unknown_endpoint_setpoint") is True,
        "Passive fitting must reject sparse indoor windows, gaps over 60 minutes, and windows whose endpoint thermostat setpoint is unknown.",
    )
    check(
        "forecast_gap_fail_closed",
        0 < float(filters.get("maximum_forecast_step_hours") or 0) <= 3.1,
        "Passive forecast must fail closed across weather gaps larger than the documented maximum step.",
    )

    data_coverage = model.get("data_coverage") or {}
    coverage_streams = [
        data_coverage.get("indoor_30m") or {},
        data_coverage.get("outdoor_30m") or {},
        data_coverage.get("paired_training_30m") or {},
    ]
    check(
        "thermal_data_coverage_contract",
        len(coverage_streams) == 3
        and all(item.get("observed_count") is not None for item in coverage_streams)
        and all(item.get("expected_count") is not None for item in coverage_streams)
        and all(item.get("coverage") is not None for item in coverage_streams)
        and all(item.get("largest_gap_minutes") is not None for item in coverage_streams),
        "Thermal model inputs must expose machine-readable observed/expected counts, coverage and largest gap.",
    )

    passive_forecast = model.get("passive_forecast") or {}
    forecast_contract = passive_forecast.get("forecast_coverage_contract") or {}
    forecast_horizons = passive_forecast.get("horizons") or []
    if model.get("prediction_ready") is True:
        check(
            "forecast_model_coverage_guard",
            int(forecast_contract.get("minimum_model_branches") or 0) >= 2
            and bool(forecast_horizons)
            and all(h.get("coverage_sufficient") is True for h in forecast_horizons)
            and all(int(h.get("observed_model_branches") or 0) >= 2 for h in forecast_horizons),
            "Every published passive forecast horizon must have sufficient model-branch coverage; a single model branch is not enough.",
        )

    heat_diag = model.get("heat_gain_diagnostics") or {}
    rejected_windows = filters.get("rejected") or {}
    end_rule = heat_diag.get("end_detection_rule") or {}
    check(
        "post_heating_quarantine_guard",
        float(filters.get("post_heating_quarantine_hours") or 0) >= 12.0
        and float(filters.get("post_heating_quarantine_trigger_rise_c") or 999) <= 0.2
        and float(filters.get("heating_end_search_hours") or 0) >= 4.0
        and float(filters.get("heating_end_confirmation_hours") or 0) >= 2.0
        and float(filters.get("heating_end_minimum_confirmed_drop_c") or 0) >= 0.3
        and float(filters.get("heating_end_maximum_rebound_c") or 999) <= 0.1
        and heat_diag.get("status") == "diagnostic_only"
        and heat_diag.get("source_attribution") == "not independently measured"
        and float(end_rule.get("confirmation_hours") or 0) >= 2.0
        and isinstance(heat_diag.get("episodes"), list)
        and all(
            episode.get("inferred_heating_end_local")
            and episode.get("end_detection_status") in {
                "confirmed_plateau_to_cooling",
                "awaiting_confirmation",
                "fallback_last_trigger",
            }
            for episode in heat_diag.get("episodes") or []
        )
        and "post_heating_quarantine" in rejected_windows,
        "Passive fitting must infer heating end from plateau-to-cooling evidence before the 12 h quarantine, fail conservatively while confirmation is incomplete, and keep source attribution explicitly unproven.",
    )

    current_indoor = model.get("current_indoor_state") or {}
    current_outdoor = ((model.get("weather_sources") or {}).get("current_temperature_selection") or {})
    current_estimate = model.get("current_passive_estimate")

    check(
        "current_indoor_freshness_and_connectivity",
        float(current_indoor.get("max_age_minutes") or 9999) <= 30.0
        and not (
            current_indoor.get("device_offline") is True
            and current_indoor.get("ok") is True
        ),
        "Current indoor state must be freshness-gated to at most 30 minutes, and an explicitly offline EMOS device must never be considered live.",
    )

    if current_estimate is not None:
        check(
            "current_state_freshness_and_provenance",
            current_indoor.get("ok") is True
            and current_outdoor.get("ok") is True
            and current_estimate.get("outside_source") in {"DWD Sohland", "ČHMÚ Varnsdorf"}
            and float(current_estimate.get("outside_age_minutes") or 9999)
                <= float(current_outdoor.get("max_age_minutes") or 0),
            "Current passive-rate estimate requires both a fresh connected indoor observation and a freshness-gated DWD/ČHMÚ outdoor observation.",
        )
    else:
        check(
            "current_state_freshness_and_provenance",
            not (current_indoor.get("ok") is True and current_outdoor.get("ok") is True),
            "Current passive-rate estimate is missing even though both indoor and outdoor current-state inputs are fresh.",
            severity="warn",
        )

    check(
        "dp2_not_used_as_boiler_proof",
        model.get("dp2_used_as_heating_state") is False,
        "DP2 must not be used as proof of actual boiler/relay heating state.",
    )
    validated_control = model.get("validated_for_heating_control") is True
    check(
        "heating_control_not_overclaimed",
        (not validated_control)
        or (
            model.get("prediction_ready") is True
            and (model.get("heating_energy_model") or {}).get("absolute_energy_scale_calibrated") is True
        ),
        "Thermal model is marked validated for heating control before both passive prediction and absolute energy scale are calibrated.",
    )

    fit = model.get("fit") or {}
    check(
        "passive_fit_positive",
        float(fit.get("coupling_per_h") or 0) > 0 and float(fit.get("time_constant_h") or 0) > 0,
        "Passive thermal fit is missing or non-physical.",
    )
    check(
        "passive_fit_internal_consistency",
        passive_fit_contract_ok(fit),
        "Production coupling must remain inside its empirical p25-p75 support and tau must be consistent with 1/coupling.",
    )

    oracle_controls = run_oracle_negative_controls(now)
    check(
        "oracle_negative_controls",
        all(item.get("passed") is True for item in oracle_controls),
        "One or more known-bad data-quality controls passed as normal data; fix the checker/gate before trusting PASS results.",
    )

    shadow = model.get("shadow_validation") or {}
    weighted_shadow = shadow.get("weighted_fit") or {}
    rolling_shadow = shadow.get("rolling_origin_vs_persistence") or {}
    check(
        "shadow_estimator_isolation",
        shadow.get("production_fit_unchanged") is True
        and (not weighted_shadow or weighted_shadow.get("shadow_only") is True)
        and rolling_shadow.get("allowed_to_change_production_fit") is False,
        "Alternative passive estimators must remain shadow-only and must not silently alter the production forecast.",
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
    check(
        "energy_model_current_price",
        close(price, pricing.get("tempering_incremental_price_czk_per_kwh"), 1e-5),
        "Thermal model is using a stale electricity price.",
    )
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
        current_setpoint = actual.get("current_setpoint_c")
        configured_setpoint = actual.get("configured_tempering_setpoint_c")
        check(
            "actual_zero_need_proof",
            current_indoor.get("ok") is True
            and inside > lower
            and outside_min >= lower
            and ((actual.get("forecast_continuity") or {}).get("ok") is True)
            and actual.get("setpoint_matches_configured_target") is True
            and close(current_setpoint, configured_setpoint, 0.15)
            and close(actual.get("electricity_kwh"), 0.0)
            and close(actual.get("incremental_cost_czk"), 0.0),
            "Zero next-24h tempering estimate requires the temperature-bound proof and a live thermostat setpoint matching the configured tempering target.",
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
        "schema": "2026-10-18.heating-audit-v6",
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "overall_status": overall,
        "critical_failures": failures,
        "warnings": list(dict.fromkeys(warnings)),
        "checks": checks,
        "oracle_negative_controls": oracle_controls,
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
