#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ASCII-safe OCR temperature reader.

Use this module on servers where locale or terminal encoding may mishandle
special symbols. It does not rely on the degree symbol.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class OcrReading:
    city: str
    source_name: str
    url: str
    selector: str
    temp_raw: Optional[float]
    unit: str
    temp_f: Optional[float]
    confidence: float
    raw_text: str
    screenshot_path: str
    processed_path: str
    timestamp_utc: str
    ok: bool
    error: str = ""


def temp_to_fahrenheit(value: float, unit: str) -> float:
    unit = (unit or "F").upper()
    if unit == "F":
        return float(value)
    if unit == "C":
        return float(value) * 9.0 / 5.0 + 32.0
    raise ValueError(f"Unsupported unit: {unit}")


def extract_temperature(text: str) -> Optional[float]:
    patterns = [
        r"(-?\d{1,3}(?:\.\d+)?)\s*(?:deg|degree|degrees)?\s*[FfCc]\b",
        r"temp(?:erature)?[^-\d]{0,16}(-?\d{1,3}(?:\.\d+)?)",
        r"\b(-?\d{1,3}(?:\.\d+)?)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text or "")
        if match:
            value = float(match.group(1))
            if -80 <= value <= 140:
                return value
    return None


def load_playwright():
    from playwright.sync_api import sync_playwright  # type: ignore
    return sync_playwright


def load_image_tools():
    from PIL import Image, ImageFilter, ImageOps  # type: ignore
    import pytesseract  # type: ignore
    return Image, ImageFilter, ImageOps, pytesseract


def preprocess_image(src: str, dst: str, threshold: int, scale: int) -> None:
    Image, ImageFilter, ImageOps, _ = load_image_tools()
    img = Image.open(src)
    img = ImageOps.grayscale(img)
    if scale > 1:
        img = img.resize((img.width * scale, img.height * scale))
    img = img.filter(ImageFilter.SHARPEN)
    img = img.point(lambda p: 255 if p >= threshold else 0)
    img.save(dst)


def confidence_score(pytesseract: Any, img_path: str) -> float:
    try:
        data = pytesseract.image_to_data(img_path, output_type=pytesseract.Output.DICT, config="--psm 6")
        values = []
        for raw in data.get("conf", []):
            try:
                val = float(raw)
                if val >= 0:
                    values.append(val)
            except Exception:
                pass
        return sum(values) / len(values) if values else 0.0
    except Exception:
        return 0.0


class OcrDataCollectorAscii:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.out_dir = Path(config.get("ocr_out_dir", "paper_logs/ocr"))
        self.out_dir.mkdir(parents=True, exist_ok=True)

    def collect_one(self, source: Dict[str, Any]) -> OcrReading:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        name = source.get("name", "ocr_source")
        city = source.get("city", "")
        url = source.get("url", "")
        selector = source.get("selector", "")
        unit = source.get("unit", "F")
        raw_path = str(self.out_dir / f"{ts}_{name}_raw.png")
        processed_path = str(self.out_dir / f"{ts}_{name}_processed.png")
        try:
            if not url or not selector:
                raise ValueError("Missing url or selector")
            sync_playwright = load_playwright()
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=bool(self.config.get("ocr_headless", True)))
                page = browser.new_page(viewport={"width": 1280, "height": 900})
                page.goto(url, wait_until="networkidle", timeout=int(source.get("timeout_ms", 20000)))
                page.locator(selector).first.screenshot(path=raw_path)
                browser.close()
            preprocess_image(raw_path, processed_path, int(source.get("threshold", 170)), int(source.get("scale", 3)))
            _, _, _, pytesseract = load_image_tools()
            raw_text = pytesseract.image_to_string(
                processed_path,
                config="--psm 6 -c tessedit_char_whitelist=0123456789.-FfCcDEGdegTemperaturetemperatureTEMPtemp ",
            )
            conf = confidence_score(pytesseract, processed_path)
            temp_raw = extract_temperature(raw_text)
            if temp_raw is None:
                raise ValueError(f"No temperature found: {raw_text!r}")
            temp_f = temp_to_fahrenheit(temp_raw, unit)
            ok = conf >= float(self.config.get("ocr_min_confidence", 70.0))
            return OcrReading(city, name, url, selector, temp_raw, unit, temp_f, conf, raw_text, raw_path, processed_path, datetime.now(timezone.utc).isoformat(), ok)
        except Exception as exc:
            return OcrReading(city, name, url, selector, None, unit, None, 0.0, "", raw_path, processed_path, datetime.now(timezone.utc).isoformat(), False, str(exc))

    def collect_all(self) -> List[OcrReading]:
        readings = []
        for source in self.config.get("ocr_sources", []):
            if source.get("enabled"):
                readings.append(self.collect_one(source))
                time.sleep(0.5)
        return readings


def load_config(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="weather_scanner_config_v2_ocr.json")
    args = parser.parse_args()
    collector = OcrDataCollectorAscii(load_config(args.config))
    for item in collector.collect_all():
        print(json.dumps(asdict(item), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
