# -*- coding: utf-8 -*-
"""Rendered-slide measurements for reference quality arbitration."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from statistics import mean
from typing import Iterable

from PIL import Image, ImageFilter, ImageStat

from .models import ReferenceVisualMeasurement, VisualSlideMeasurement


def _hex(rgb: tuple[int, int, int]) -> str:
    return "".join(f"{channel:02X}" for channel in rgb)


def _dominant_colors(image: Image.Image, count: int = 6) -> list[str]:
    quantized = image.convert("RGB").resize((160, 90)).quantize(
        colors=max(8, count * 3),
        method=Image.Quantize.MEDIANCUT,
    )
    palette = quantized.getpalette() or []
    colors: list[str] = []
    for _frequency, index in sorted(quantized.getcolors() or [], reverse=True):
        base = index * 3
        rgb = tuple(palette[base : base + 3])
        if len(rgb) == 3:
            colors.append(_hex(rgb))
        if len(colors) == count:
            break
    return colors


def _background_rgb(image: Image.Image) -> tuple[int, int, int]:
    rgb = image.convert("RGB")
    width, height = rgb.size
    samples = [
        rgb.getpixel((0, 0)),
        rgb.getpixel((width - 1, 0)),
        rgb.getpixel((0, height - 1)),
        rgb.getpixel((width - 1, height - 1)),
        rgb.getpixel((width // 2, 0)),
        rgb.getpixel((width // 2, height - 1)),
    ]
    return tuple(int(mean(channel)) for channel in zip(*samples))


def _foreground_occupancy(image: Image.Image) -> float:
    rgb = image.convert("RGB").resize((256, 144))
    background = _background_rgb(rgb)
    foreground = 0
    total = rgb.width * rgb.height
    for pixel in rgb.getdata():
        distance = sum(abs(int(pixel[index]) - background[index]) for index in range(3))
        if distance > 42:
            foreground += 1
    return round(foreground / max(total, 1), 4)


def _edge_density(image: Image.Image) -> float:
    edges = image.convert("L").resize((256, 144)).filter(ImageFilter.FIND_EDGES)
    histogram = edges.histogram()
    active = sum(histogram[28:])
    return round(active / max(edges.width * edges.height, 1), 4)


def _mean_saturation(image: Image.Image) -> float:
    hsv = image.convert("RGB").resize((160, 90)).convert("HSV")
    saturation = ImageStat.Stat(hsv.getchannel("S")).mean[0] / 255.0
    return round(float(saturation), 4)


def _longest_density_run(values: list[str]) -> int:
    longest = current = 0
    previous = None
    for value in values:
        if value == previous:
            current += 1
        else:
            current = 1
            previous = value
        longest = max(longest, current)
    return longest


class RenderedSlideAnalyzer:
    """Analyze the actual rendered appearance, not only the source objects."""

    @staticmethod
    def _resolve_paths(
        image_paths: str | Path | Iterable[str | Path],
    ) -> list[Path]:
        if isinstance(image_paths, (str, Path)):
            path = Path(image_paths)
            if path.is_dir():
                supported = {".png", ".jpg", ".jpeg", ".webp"}
                return sorted(
                    (item for item in path.iterdir() if item.suffix.casefold() in supported),
                    key=lambda item: item.name.casefold(),
                )
            return [path]
        return [Path(item) for item in image_paths]

    def analyze(
        self,
        image_paths: str | Path | Iterable[str | Path],
    ) -> ReferenceVisualMeasurement:
        slides: list[VisualSlideMeasurement] = []
        palette_votes: Counter[str] = Counter()
        for slide_number, path in enumerate(self._resolve_paths(image_paths), start=1):
            with Image.open(path) as source:
                image = source.convert("RGB")
                luminance = ImageStat.Stat(image.convert("L"))
                occupancy = _foreground_occupancy(image)
                edge_density = _edge_density(image)
                density_signal = occupancy * 0.7 + min(1.0, edge_density * 4.0) * 0.3
                density = "high" if density_signal >= 0.34 else "low" if density_signal <= 0.15 else "medium"
                colors = _dominant_colors(image)
                palette_votes.update(colors[:4])
                slides.append(
                    VisualSlideMeasurement(
                        slide_number=slide_number,
                        pixel_width=image.width,
                        pixel_height=image.height,
                        mean_luminance=round(float(luminance.mean[0]), 3),
                        luminance_stddev=round(float(luminance.stddev[0]), 3),
                        foreground_occupancy=occupancy,
                        edge_density=edge_density,
                        mean_saturation=_mean_saturation(image),
                        dominant_colors=colors,
                        visual_density=density,
                    ),
                )
        if not slides:
            raise ValueError("reference visual analysis requires at least one slide image")
        sequence = [slide.visual_density for slide in slides]
        return ReferenceVisualMeasurement(
            slides=slides,
            density_sequence=sequence,
            dominant_palette=[color for color, _count in palette_votes.most_common(8)],
            repeated_density_run=_longest_density_run(sequence),
        )
