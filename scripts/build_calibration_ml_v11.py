#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

import build_calibration_ml_shadow as holdout
import build_calibration_ml_walkforward as walk

ROOT = Path(__file__).resolve().parents[1]
HOLDOUT_OUT = ROOT / "data" / "calibration" / "ml-v1.1-shadow.json"
WALK_OUT = ROOT / "data" / "calibration" / "ml-v1.1-walkforward.json"


def annotate(path: Path, kind: str):
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["challenger"] = {
        "name": "ML v1.1 squared-loss challenger",
        "kind": kind,
        "reason": "Strict validation found model-specific warm bias in ML v1 despite lower MAE. Squared-error loss passed the independent 5-fold bias diagnostic while improving MAE, RMSE and P95.",
        "shadow_only": True,
        "selected_from_diagnostic": "data/calibration/bias-diagnostic.json",
        "may_replace_ml_v1": False,
        "replacement_requires": "continued strict walk-forward stability and later on-site Nové Hraběcí truth",
    }
    if isinstance(payload.get("model"), dict):
        payload["model"]["variant"] = "v1.1_squared_error"
    if isinstance(payload.get("method"), dict):
        payload["method"]["variant"] = "v1.1_squared_error"
    payload["production_eligible"] = False
    payload["allowed_to_affect_public_forecast"] = False
    payload["allowed_to_affect_alerts"] = False
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    # Both builders import the same mutable MODEL_PARAMS object. Change only the
    # loss for this challenger; every other feature and hyperparameter remains
    # identical to ML v1 so the comparison isolates the loss function.
    holdout.MODEL_PARAMS["loss"] = "squared_error"
    walk.MODEL_PARAMS["loss"] = "squared_error"

    holdout.OUT = HOLDOUT_OUT
    holdout.main()
    annotate(HOLDOUT_OUT, "chronological_holdout")

    walk.OUT = WALK_OUT
    walk.main()
    annotate(WALK_OUT, "strict_as_of_time_walk_forward")

    h = json.loads(HOLDOUT_OUT.read_text(encoding="utf-8"))
    w = json.loads(WALK_OUT.read_text(encoding="utf-8"))
    print(json.dumps({
        "holdout_ok": h.get("ok"),
        "holdout_metrics": (h.get("metrics") or {}).get("overall"),
        "walk_ok": w.get("ok"),
        "walk_metrics": w.get("aggregate_metrics"),
        "walk_gate": w.get("gate"),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
