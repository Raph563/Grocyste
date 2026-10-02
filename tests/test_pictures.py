import base64
import struct
import zlib

import pytest

from grocyste.pictures import PictureError, validate_picture


def filename(value):
    return base64.b64encode(value.encode()).decode()


def chunk(kind, content):
    return struct.pack(">I", len(content)) + kind + content + struct.pack(">I", zlib.crc32(kind + content) & 0xffffffff)


def png(width=1, height=1):
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(b"\0\xff\0\0")) + chunk(b"IEND", b"")


GIF = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")
WEBP = base64.b64decode("UklGRiIAAABXRUJQVlA4IBYAAAAwAQCdASoBAAEADsD+JaQAA3AAAAAA")


@pytest.mark.parametrize("data,mime,extension", [(png(), "image/png", "png"), (GIF, "image/gif", "gif"), (WEBP, "image/webp", "webp")])
def test_real_small_raster_containers_and_safe_names_are_accepted(data, mime, extension):
    validate_picture(data, mime, filename("synthetic-picture." + extension))


@pytest.mark.parametrize("data,mime,extension", [(png(), "image/png", "png"), (GIF, "image/gif", "gif"), (WEBP, "image/webp", "webp")])
def test_truncation_and_trailing_active_markup_are_refused(data, mime, extension):
    with pytest.raises(PictureError):
        validate_picture(data[:-1], mime, filename("synthetic." + extension))
    with pytest.raises(PictureError):
        validate_picture(data + b"<svg><script>alert(1)</script></svg>", mime, filename("synthetic." + extension))


@pytest.mark.parametrize("width,height", [(0, 1), (1, 0), (16385, 1), (10000, 10000)])
def test_oversized_raster_dimensions_are_refused_before_forwarding(width, height):
    with pytest.raises(PictureError, match="dimensions"):
        validate_picture(png(width, height), "image/png", filename("synthetic.png"))


def test_png_checksum_and_declared_mime_mismatch_are_refused():
    damaged = bytearray(png())
    damaged[-17] ^= 1
    with pytest.raises(PictureError):
        validate_picture(bytes(damaged), "image/png", filename("synthetic.png"))
    with pytest.raises(PictureError):
        validate_picture(png(), "image/jpeg", filename("synthetic.jpg"))
