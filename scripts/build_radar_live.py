#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

import build_radar_nowcast as radar


def main():
    radar.OUT = Path(__file__).resolve().parents[1] / "data" / "radar-live.json"
    radar.main()


if __name__ == "__main__":
    main()
