"""Generate original, dry PCM UI clicks and verify their silence boundaries."""

from __future__ import annotations

import math
import random
import struct
import wave
from pathlib import Path


SAMPLE_RATE = 48_000
OUTPUT_DIR = Path(__file__).resolve().parents[1] / "assets" / "ui_sounds"


SOUNDS = {
    # start, length, resonant-body Hz, noise amount, body amount
    "navigate": [(0.000, 0.036, 2_150, .92, .16)],
    "confirm": [(0.000, 0.058, 1_280, .86, .24)],
    "back": [(0.000, 0.046, 820, .74, .21)],
    "task_start": [(0.000, 0.068, 1_050, .80, .20), (.061, .048, 1_750, .68, .18)],
    "success": [(0.000, 0.066, 1_250, .72, .18), (.070, .072, 1_900, .70, .20)],
    "warning": [(0.000, 0.047, 700, .74, .22), (.044, .045, 700, .62, .18)],
    "error": [(0.000, 0.057, 430, .72, .27), (.047, .058, 360, .64, .24)],
}


def synthesize(name: str, parts: list[tuple[float, float, int, float, float]]) -> list[float]:
    randomizer = random.Random(name)
    duration = max(start + tail for start, tail, *_rest in parts)
    samples = [0.0] * math.ceil(duration * SAMPLE_RATE)
    previous_noise = 0.0
    for start, tail, frequency, noise_amount, body_amount in parts:
        first = round(start * SAMPLE_RATE)
        count = round(tail * SAMPLE_RATE)
        for offset in range(count):
            frame = first + offset
            if frame >= len(samples):
                break
            progress = offset / max(1, count - 1)
            envelope = math.exp(-progress * 5.2)
            raw_noise = randomizer.uniform(-1.0, 1.0)
            high_noise = raw_noise - previous_noise * .76
            previous_noise = raw_noise
            phase = math.tau * frequency * (offset / SAMPLE_RATE)
            body = math.sin(phase) * math.exp(-progress * 11.0)
            samples[frame] += (high_noise * noise_amount + body * body_amount) * envelope
    # Ensure the very first sample contains the transient, then taper only the end.
    samples[0] = max(samples[0], .5)
    peak = max(abs(sample) for sample in samples) or 1.0
    samples = [sample * (0.78 / peak) for sample in samples]
    threshold = max(abs(sample) for sample in samples) * 0.012
    first = next(index for index, sample in enumerate(samples) if abs(sample) >= threshold)
    last = len(samples) - 1 - next(index for index, sample in enumerate(reversed(samples)) if abs(sample) >= threshold)
    # Keep a 1 ms anti-click taper after the final audible sample, then stop.
    tail = min(round(SAMPLE_RATE * 0.0008), len(samples) - last - 1)
    trimmed = samples[first : last + 1 + tail]
    if tail:
        for offset in range(tail):
            trimmed[-tail + offset] *= (tail - offset - 1) / max(1, tail)
    return trimmed


def write_wav(path: Path, samples: list[float]) -> None:
    pcm = b"".join(struct.pack("<h", max(-32767, min(32767, round(sample * 32767)))) for sample in samples)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(SAMPLE_RATE)
        output.writeframes(pcm)


def analyse(path: Path) -> tuple[float, int, int, float]:
    with wave.open(str(path), "rb") as source:
        values = struct.unpack(f"<{source.getnframes()}h", source.readframes(source.getnframes()))
        sample_rate = source.getframerate()
    peak = max(abs(value) for value in values) or 1
    threshold = peak * 0.012
    leading = next((index for index, value in enumerate(values) if abs(value) >= threshold), len(values))
    trailing = next((index for index, value in enumerate(reversed(values)) if abs(value) >= threshold), len(values))
    return len(values) / sample_rate * 1000, leading, trailing, peak / 32767


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print("name       duration_ms  leading_samples  trailing_samples  peak")
    for name, parts in SOUNDS.items():
        path = OUTPUT_DIR / f"{name}.wav"
        write_wav(path, synthesize(name, parts))
        duration, leading, trailing, peak = analyse(path)
        if leading > 1 or trailing > 40:
            raise RuntimeError(f"{name} has {leading} leading-silence samples")
        print(f"{name:<10} {duration:>10.1f} {leading:>16} {trailing:>17} {peak:>5.2f}")


if __name__ == "__main__":
    main()
