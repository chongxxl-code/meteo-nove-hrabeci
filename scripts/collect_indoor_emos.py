#!/usr/bin/env python3
# Validation trigger: secrets configured.
# GitHub Actions: this collector is intentionally triggered by the shared 3-hour weather workflow.
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import unicodedata
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
TZ = ZoneInfo("Europe/Prague")
TARGET = os.getenv("EMOS_TARGET_NAME", "Nové hraběcí")
ENDPOINT = "https://a1-eu.emosgosmart.net/api.json"
PACKAGE = "com.emos.eu"

APP_VERSION = "3.0.2"
SDK_VERSION = "6.8.0"
APP_RN_VERSION = "5.97"
CH_KEY = "1fef4bd9"
TTID_SUFFIX = "sdk_international@"
COUNTRY = os.getenv("EMOS_COUNTRY", "420")

SIGN_KEYS = {
    "a", "v", "lat", "lon", "lang", "deviceId", "appVersion", "ttid",
    "h5", "h5Token", "os", "clientId", "postData", "time", "requestId",
    "et", "n4h5", "sid", "chKey", "sp",
}
FIXED_RSA_SEED = bytes([
    0xAA, 0xFD, 0x12, 0xF6, 0x59, 0xCA, 0xE6, 0x34, 0x89, 0xB4,
    0x79, 0xE5, 0x07, 0x6D, 0xDE, 0xC2, 0xF0, 0x6C, 0xB5, 0x8F,
])


def env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def md5_hex(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def post_data_md5(post_data: str) -> str:
    value = md5_hex(post_data)
    return value[8:16] + value[0:8] + value[24:32] + value[16:24]


def normalize_cert_sha256(value: str) -> str:
    raw = value.replace(":", "").replace(" ", "").upper()
    if len(raw) != 64 or any(ch not in "0123456789ABCDEF" for ch in raw):
        raise ValueError("EMOS_CERT_SHA256 must contain 64 hexadecimal characters")
    return ":".join(raw[i:i + 2] for i in range(0, 64, 2))


def native_signing_key(app_secret: str, bmp_key: str, cert_sha256: str) -> bytes:
    cert = normalize_cert_sha256(cert_sha256)
    return f"{PACKAGE}_{cert}_{bmp_key}_{app_secret}".encode("utf-8")


def sign_input(params: dict) -> str:
    normalized = dict(params)
    if normalized.get("postData"):
        normalized["postData"] = post_data_md5(normalized["postData"])
    return "||".join(
        f"{key}={normalized[key]}"
        for key in sorted(normalized)
        if key in SIGN_KEYS and normalized.get(key) not in (None, "")
    )


def stable_device_id(username: str, app_id: str) -> str:
    material = f"{PACKAGE}|{app_id}|{username}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:44]


def rsa_pkcs1_v15_encrypt_hex(message: str, modulus_dec: str, exponent_dec: str) -> str:
    modulus = int(modulus_dec)
    exponent = int(exponent_dec)
    key_len = (modulus.bit_length() + 7) // 8
    payload = message.encode("utf-8")
    padding_len = key_len - len(payload) - 3
    if padding_len < 8:
        raise ValueError("RSA payload too long")
    padding = (FIXED_RSA_SEED * (padding_len // len(FIXED_RSA_SEED) + 1))[:padding_len]
    encoded = b"\x00\x02" + padding + b"\x00" + payload
    cipher = pow(int.from_bytes(encoded, "big"), exponent, modulus)
    return cipher.to_bytes(key_len, "big").hex()


def normalize_name(value: str) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(ch for ch in text if not unicodedata.combining(ch)).casefold()


class EmosMobileApi:
    def __init__(self, username: str, password: str, app_id: str, app_secret: str, bmp_key: str, cert_sha256: str):
        self.username = username
        self.password = password
        self.app_id = app_id
        self.device_id = stable_device_id(username, app_id)
        self.ttid = TTID_SUFFIX + app_id
        self.native_key = native_signing_key(app_secret, bmp_key, cert_sha256)
        self.sid = None

    def request(self, api: str, version: str, payload=None, *, sid=None, extra=None):
        params = {
            "a": api,
            "v": version,
            "clientId": self.app_id,
            "deviceId": self.device_id,
            "appVersion": APP_VERSION,
            "chKey": CH_KEY,
            "ttid": self.ttid,
            "lang": "en_US",
            "os": "Android",
            "et": "0",
            "time": str(int(time.time())),
            "requestId": str(uuid.uuid4()),
            "sdkVersion": SDK_VERSION,
            "deviceCoreVersion": SDK_VERSION,
            "osSystem": "11",
            "platform": "sdk_gphone_x86_64",
            "channel": "oem",
            "appRnVersion": APP_RN_VERSION,
            "bizData": "",
            "cp": "",
            "nd": "",
            "timeZoneId": "GMT",
        }
        if payload is not None:
            params["postData"] = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if sid:
            params["sid"] = sid
        if extra:
            params.update(extra)
        params["sign"] = hmac.new(
            self.native_key,
            sign_input(params).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        body = urllib.parse.urlencode(params).encode("utf-8")
        req = urllib.request.Request(
            ENDPOINT,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": f"ThingSmart/{APP_VERSION} Android",
                "Accept-Encoding": "identity",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))

    @staticmethod
    def checked(response: dict, label: str) -> dict:
        if not response.get("success"):
            code = response.get("errorCode") or response.get("code") or "unknown"
            msg = response.get("errorMsg") or response.get("msg") or ""
            raise RuntimeError(f"{label} failed: {code} {msg}".strip())
        return response

    def login(self):
        token = self.checked(
            self.request(
                "thing.m.user.username.token.get",
                "2.0",
                {"countryCode": COUNTRY, "username": self.username, "isUid": False},
            ),
            "token",
        )["result"]
        encrypted_password = rsa_pkcs1_v15_encrypt_hex(
            md5_hex(self.password),
            token["publicKey"],
            token["exponent"],
        )
        result = self.checked(
            self.request(
                "thing.m.user.email.password.login",
                "3.0",
                {
                    "countryCode": COUNTRY,
                    "email": self.username,
                    "passwd": encrypted_password,
                    "options": '{"group": 1,"mfaCode": ""}',
                    "token": token["token"],
                    "ifencrypt": 1,
                },
            ),
            "login",
        )["result"]
        self.sid = result["sid"]

    def homes(self):
        return self.checked(
            self.request("m.life.home.space.list", "1.0", sid=self.sid),
            "home list",
        ).get("result") or []

    def devices(self, home_id):
        return self.checked(
            self.request(
                "m.life.my.group.device.list",
                "2.2",
                {"gid": home_id},
                sid=self.sid,
                extra={"gid": home_id},
            ),
            "device list",
        ).get("result") or []


def get_dp(dps, key):
    if not isinstance(dps, dict):
        return None
    return dps.get(str(key), dps.get(key))


def temp_c(raw):
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return round(float(raw) / 10.0, 1)
    except (TypeError, ValueError):
        return None


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def main():
    required = {
        "EMOS_USERNAME": env("EMOS_USERNAME"),
        "EMOS_PASSWORD": env("EMOS_PASSWORD"),
        "EMOS_APP_ID": env("EMOS_APP_ID"),
        "EMOS_APP_SECRET": env("EMOS_APP_SECRET"),
        "EMOS_BMP_KEY": env("EMOS_BMP_KEY"),
        "EMOS_CERT_SHA256": env("EMOS_CERT_SHA256"),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        print(json.dumps({"ok": False, "skipped": "missing_secrets", "missing": missing}))
        return 0

    api = EmosMobileApi(
        required["EMOS_USERNAME"],
        required["EMOS_PASSWORD"],
        required["EMOS_APP_ID"],
        required["EMOS_APP_SECRET"],
        required["EMOS_BMP_KEY"],
        required["EMOS_CERT_SHA256"],
    )
    api.login()

    target = None
    target_norm = normalize_name(TARGET)
    for home in api.homes():
        home_id = home.get("homeId") or home.get("gid") or home.get("id")
        if home_id is None:
            continue
        devices = api.devices(home_id)
        if isinstance(devices, dict):
            devices = devices.get("list") or devices.get("deviceList") or []
        for device in devices if isinstance(devices, list) else []:
            if target_norm in normalize_name(device.get("name")):
                target = device
                break
        if target:
            break

    if not target:
        raise RuntimeError("EMOS target device not found")

    dps = target.get("dps") or {}
    indoor = temp_c(get_dp(dps, 24))
    setpoint = temp_c(get_dp(dps, 3))
    if indoor is None:
        raise RuntimeError("DP24 current temperature missing from EMOS cloud device record")

    now = datetime.now(TZ).replace(microsecond=0)
    timestamp = now.isoformat()
    point = {
        "timestamp_local": timestamp,
        "indoor_c": indoor,
        "setpoint_c": setpoint,
        "dp2_raw": get_dp(dps, 2),
        "source": "EMOS/Tuya cloud snapshot",
    }

    DATA.mkdir(exist_ok=True)
    archive_dir = DATA / "indoor-cloud"
    archive_dir.mkdir(exist_ok=True)
    archive_file = archive_dir / f"{now:%Y-%m}.jsonl"
    with archive_file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(point, ensure_ascii=False, separators=(",", ":")) + "\n")

    history_path = DATA / "indoor-history.json"
    history = load_json(history_path, {"points": []})
    points = history.get("points") if isinstance(history.get("points"), list) else []
    by_ts = {
        str(item.get("timestamp_local")): item
        for item in points
        if isinstance(item, dict) and item.get("timestamp_local")
    }
    by_ts[timestamp] = point
    merged = [by_ts[key] for key in sorted(by_ts)]

    history.update({
        "generated_at": now.isoformat(),
        "resolution_minutes": None,
        "source": "EMOS GoSmart / Tuya cloud + display backfill",
        "source_quality": "mixed",
        "points": merged,
    })
    history_path.write_text(
        json.dumps(history, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    latest = {
        "generated_at": now.isoformat(),
        "points": len(merged),
        "first_timestamp": merged[0]["timestamp_local"] if merged else None,
        "last_timestamp": timestamp,
        "latest_indoor_c": indoor,
        "latest_setpoint_c": setpoint,
        "dp2_raw": get_dp(dps, 2),
        "source": "EMOS/Tuya cloud snapshot",
    }
    (DATA / "indoor-latest.json").write_text(
        json.dumps(latest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(json.dumps({
        "ok": True,
        "indoor_c": indoor,
        "setpoint_c": setpoint,
        "points": len(merged),
        "archive": str(archive_file.relative_to(ROOT)),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
