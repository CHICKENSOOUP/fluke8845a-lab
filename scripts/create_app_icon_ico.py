"""Create a multi-resolution Windows ICO from the app's PNG icon."""

from __future__ import annotations

import struct
from pathlib import Path

from PySide6.QtCore import QBuffer, QIODevice, Qt
from PySide6.QtGui import QImage


ROOT = Path(__file__).resolve().parents[1]
PNG_PATH = ROOT / "assets" / "fluke8845a_app_icon_v3.png"
ICO_PATH = ROOT / "assets" / "fluke8845a_app_icon_v3.ico"
SIZES = (256, 128, 64, 48, 32, 24, 16)


def png_bytes(image: QImage) -> bytes:
    buffer = QBuffer()
    if not buffer.open(QIODevice.OpenModeFlag.WriteOnly):
        raise OSError("Could not open an in-memory image buffer")
    if not image.save(buffer, "PNG"):
        raise OSError("Qt could not encode an icon image as PNG")
    return bytes(buffer.data())


def build_icon(source: Path = PNG_PATH, destination: Path = ICO_PATH) -> None:
    image = QImage(str(source))
    if image.isNull() or image.width() != image.height():
        raise ValueError(f"Expected a readable square PNG icon: {source}")

    payloads: list[tuple[int, bytes]] = []
    for size in SIZES:
        scaled = image.scaled(
            size,
            size,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        payloads.append((size, png_bytes(scaled)))

    directory = bytearray(struct.pack("<HHH", 0, 1, len(payloads)))
    offset = 6 + 16 * len(payloads)
    for size, payload in payloads:
        dimension = 0 if size == 256 else size
        directory.extend(
            struct.pack(
                "<BBBBHHII",
                dimension,
                dimension,
                0,
                0,
                1,
                32,
                len(payload),
                offset,
            )
        )
        offset += len(payload)

    destination.write_bytes(directory + b"".join(payload for _, payload in payloads))


if __name__ == "__main__":
    build_icon()
    print(f"Created {ICO_PATH.relative_to(ROOT)} with sizes: {', '.join(map(str, SIZES))}")
