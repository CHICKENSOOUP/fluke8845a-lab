import struct

from PySide6.QtGui import QImage

from scripts.create_app_icon_ico import SIZES, build_icon


def test_app_icon_builder_writes_all_png_sizes(tmp_path):
    source = QImage("assets/fluke8845a_app_icon_v3.png")
    assert not source.isNull()
    assert source.width() == source.height()
    assert source.hasAlphaChannel()
    assert source.pixelColor(0, 0).alpha() == 0
    assert source.pixelColor(source.width() // 2, source.height() // 2).alpha() > 0

    destination = tmp_path / "app.ico"
    build_icon(destination=destination)
    data = destination.read_bytes()

    reserved, icon_type, count = struct.unpack_from("<HHH", data)
    assert (reserved, icon_type, count) == (0, 1, len(SIZES))

    found_sizes = []
    for index, expected_size in enumerate(SIZES):
        offset = 6 + index * 16
        width, height, color_count, entry_reserved, planes, bpp, length, image_offset = (
            struct.unpack_from("<BBBBHHII", data, offset)
        )
        width = width or 256
        height = height or 256
        assert (width, height) == (expected_size, expected_size)
        assert (color_count, entry_reserved, planes, bpp) == (0, 0, 1, 32)
        assert image_offset + length <= len(data)

        image = QImage()
        assert image.loadFromData(data[image_offset : image_offset + length], "PNG")
        assert (image.width(), image.height()) == (expected_size, expected_size)
        assert image.hasAlphaChannel()
        found_sizes.append((width, height))

    assert found_sizes == [(size, size) for size in SIZES]
