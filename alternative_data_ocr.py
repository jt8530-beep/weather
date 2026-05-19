#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Alternative OCR data collector for the weather paper scanner.

Read-only module:
- Opens a public dashboard with Playwright.
- Screenshots a configured CSS selector.
- Runs local Tesseract OCR.
- Extracts one temperature reading with a confidence score.
- Never connects to Polymarket, never signs, never trades.
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
    raise ValueError(f"Unsupported temperature unit: {unit}")


def _load_playwright():
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
        return sync_playwright
    except Exception as exc:
        raise RuntimeError("Missing Playwright. Install: pip install playwright && playwright install chromium") from exc


def _load_ocr_deps():
    try:
        from PIL import Image, ImageFilter, ImageOps  # type: ignore
        import pytesseract  # type: ignore
        return Image, ImageFilter, ImageOps, pytesseract
    except Exception as exc:
        raise RuntimeError("Missing OCR deps. Install tesseract-ocr, Pillow and pytesseract.") from exc


def preprocess_image(src: str, dst: str, threshold: int = 170, scale: int = 3) -> str:
    Image, ImageFilter, ImageOps, _ = _load_ocr_deps()
    img = Image.open(src)
    img = ImageOps.grayscale(img)
    if scale and scale > 1:
        img = img.resize((img.width * scale, img.height * scale))
    img = img.filter(ImageFilter.SHARPEN)
    img = img.point(lambda p: 255 if p >= threshold else 0)
    img.save(dst)
    return dst


def extract_temperature(text: str) -> Optional[float]:
    patterns = [
        r"(-?\d{1,3}(?:\.\d+)?)\s*°?\s*[FfCc]",
        r"temp(?:erature)?[^-\d]{0,12}(-?\d{1,3}(?:\.\d+)?)",
        r"\b(-?\d{1,3}(?:\.\d+)?)\b",
    ]
    for pat in patterns:
        match = re.search(pat, text or "")
        if match:
            value = float(match.group(1))
            if -80 <= value <= 140:
                return value
    return None


def ocr_confidence(pytesseract: Any, img_path: str) -> float:
    try:
        data = pytesseract.image_to_data(img_path, output_type=pytesseract.Output.DICT, config="--psm 6")
        confs = []
        for c in data.get("conf", []):
            try:
                v = float(c)
                if v >= 0:
                    confs.append(v)
            except Exception:
                pass
        return sum(confs) / len(confs) if confs else 0.0
    except Exception:
        return 0.0


class OcrDataCollector:
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
            sync_playwright = _load_playwright()
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=bool(self.config.get("ocr_headless", True)))
                page = browser.new_page(viewport={"width": 1280, "height": 900})
                page.goto(url, wait_until="networkidle", timeout=int(source.get("timeout_ms", 20000)))
                page.locator(selector).first.screenshot(path=raw_path)
                browser.close()

            preprocess_image(
                raw_path,
                processed_path,
                threshold=int(source.get("threshold", 170)),
                scale=int(source.get("scale", 3)),
            )
            _, _, _, pytesseract = _load_ocr_deps()
            raw_text = pytesseract.image_to_string(
                processed_path,
                config="--psm 6 -c tessedit_char_whitelist=0123456789.-°FfCcTemperaturetemperatureTEMPtemp ",
            )
            conf = ocr_confidence(pytesseract, processed_path)
            temp_raw = extract_temperature(raw_text)
            if temp_raw is None:
                raise ValueError(f"No temperature found in OCR text: {raw_text!r}")
            temp_f = temp_to_fahrenheit(temp_raw, unit)
            ok = conf >= float(self.config.get("ocr_min_confidence", 70.0))
            return OcrReading(city, name, url, selector, temp_raw, unit, temp_f, conf, raw_text, raw_path, processed_path, datetime.now(timezone.utc).isoformat(), ok)
        except Exception as exc:
            return OcrReading(city, name, url, selector, None, unit, None, 0.0, "", raw_path, processed_path, datetime.now(timezone.utc).isoformat(), False, str(exc))

    def collect_all(self) -> List[OcrReading]:
        readings: List[OcrReading] = []
        for src in self.config.get("ocr_sources", []):
            if src.get("enabled"):
                readings.append(self.collect_one(src))
                time.sleep(0.5)
        return readings


def load_config(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="weather_scanner_config_v2_ocr.json")
    args = parser.parse_args()
    cfg = load_config(args.config)
    collector = OcrDataCollector(cfg)
    for reading in collector.collect_all():
        print(json.dumps(asdict(reading), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
