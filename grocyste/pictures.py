"""Bounded raster envelopes before forwarding a picture to Grocy's raw store.

This checks container structure and dimensions; it is not a full codec decoder.
Grocy serves its raw files inline using sniffed MIME, so HTML/SVG or ambiguous
filename/type combinations must never be written through the picture facade.
"""
import base64
import re
import struct
import zlib


class PictureError(ValueError):
    pass


def bounds(width, height):
    if not 1 <= width <= 16384 or not 1 <= height <= 16384 or width * height > 40_000_000:
        raise PictureError("Picture dimensions exceed limit")


def png(data):
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise PictureError("Picture content does not match its declared type")
    offset, header, content, ended = 8, False, False, False
    while offset + 12 <= len(data):
        size = int.from_bytes(data[offset:offset + 4], "big")
        kind = data[offset + 4:offset + 8]
        end = offset + 12 + size
        if end > len(data) or not re.fullmatch(rb"[A-Za-z]{4}", kind):
            break
        chunk = data[offset + 8:end - 4]
        if zlib.crc32(kind + chunk) & 0xffffffff != int.from_bytes(data[end - 4:end], "big"):
            break
        if not header:
            if kind != b"IHDR" or size != 13:
                break
            width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", chunk)
            bounds(width, height)
            if (color not in {0, 2, 3, 4, 6} or depth not in {1, 2, 4, 8, 16}
                    or compression or filtering or interlace not in {0, 1}):
                break
            header = True
        elif kind == b"IHDR":
            break
        if kind == b"IDAT":
            content = True
        if kind == b"IEND":
            ended = size == 0 and content and end == len(data)
            break
        offset = end
    if not ended:
        raise PictureError("Invalid raster picture envelope")


def jpeg(data):
    if not data.startswith(b"\xff\xd8\xff") or not data.endswith(b"\xff\xd9"):
        raise PictureError("Picture content does not match its declared type")
    offset, frame = 2, False
    while offset < len(data) - 2:
        if data[offset] != 0xff:
            break
        while offset < len(data) and data[offset] == 0xff:
            offset += 1
        if offset >= len(data):
            break
        marker = data[offset]
        offset += 1
        if marker in {0x00, 0xd8, 0xd9} or 0xd0 <= marker <= 0xd7:
            break
        if offset + 2 > len(data):
            break
        length = int.from_bytes(data[offset:offset + 2], "big")
        if length < 2 or offset + length > len(data) - 2:
            break
        if marker in {0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf}:
            if length < 8:
                break
            bounds(int.from_bytes(data[offset + 5:offset + 7], "big"),
                   int.from_bytes(data[offset + 3:offset + 5], "big"))
            frame = True
        if marker == 0xda:
            if not frame:
                break
            return
        offset += length
    raise PictureError("Invalid raster picture envelope")


def gif(data):
    if not data.startswith((b"GIF87a", b"GIF89a")) or len(data) < 14:
        raise PictureError("Picture content does not match its declared type")
    width, height = struct.unpack("<HH", data[6:10])
    bounds(width, height)
    offset = 13 + (3 * (2 ** ((data[10] & 7) + 1)) if data[10] & 0x80 else 0)
    frames = 0
    while offset < len(data):
        marker = data[offset]
        offset += 1
        if marker == 0x3b:
            if frames and offset == len(data):
                return
            break
        if marker == 0x21:
            offset += 1  # extension label; its body is a sequence of blocks
        elif marker == 0x2c:
            if offset + 9 > len(data):
                break
            left, top, frame_width, frame_height = struct.unpack("<HHHH", data[offset:offset + 8])
            bounds(frame_width, frame_height)
            if left + frame_width > width or top + frame_height > height:
                break
            flags = data[offset + 8]
            offset += 9 + (3 * (2 ** ((flags & 7) + 1)) if flags & 0x80 else 0)
            if offset >= len(data) or not 2 <= data[offset] <= 8:
                break
            offset += 1  # LZW code size
            frames += 1
            if frames > 500:
                raise PictureError("Picture dimensions exceed limit")
        else:
            break
        while offset < len(data):
            length = data[offset]
            offset += 1
            if not length:
                break
            offset += length
        else:
            break
    raise PictureError("Invalid raster picture envelope")


def webp(data):
    if not data.startswith(b"RIFF") or len(data) < 20 or data[8:12] != b"WEBP":
        raise PictureError("Picture content does not match its declared type")
    if int.from_bytes(data[4:8], "little") + 8 != len(data):
        raise PictureError("Invalid raster picture envelope")
    offset, frame = 12, False
    while offset + 8 <= len(data):
        kind, length = data[offset:offset + 4], int.from_bytes(data[offset + 4:offset + 8], "little")
        end = offset + 8 + length
        if end > len(data):
            break
        chunk = data[offset + 8:end]
        if kind == b"VP8X" and length == 10:
            bounds(1 + int.from_bytes(chunk[4:7], "little"), 1 + int.from_bytes(chunk[7:10], "little"))
        elif kind == b"VP8 " and length >= 10 and chunk[3:6] == b"\x9d\x01\x2a":
            bounds(int.from_bytes(chunk[6:8], "little") & 0x3fff, int.from_bytes(chunk[8:10], "little") & 0x3fff)
            frame = True
        elif kind == b"VP8L" and length >= 5 and chunk[0] == 0x2f:
            bits = int.from_bytes(chunk[1:5], "little")
            bounds(1 + (bits & 0x3fff), 1 + ((bits >> 14) & 0x3fff))
            frame = True
        elif kind == b"ANMF" and length >= 16:
            bounds(1 + int.from_bytes(chunk[6:9], "little"), 1 + int.from_bytes(chunk[9:12], "little"))
            frame = True
        offset = end + (length % 2)
    if not frame or offset != len(data):
        raise PictureError("Invalid raster picture envelope")


def validate_picture(data, content_type, encoded_filename):
    try:
        filename = base64.b64decode(encoded_filename, validate=True).decode("utf-8")
    except (ValueError, UnicodeError):
        raise PictureError("Invalid picture filename") from None
    if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,179}", filename)
            or ".." in filename):
        raise PictureError("Invalid picture filename")
    validators = {"image/png": (png, {"png"}), "image/jpeg": (jpeg, {"jpg", "jpeg"}),
                  "image/gif": (gif, {"gif"}), "image/webp": (webp, {"webp"})}
    if content_type not in validators or filename.rsplit(".", 1)[-1].lower() not in validators[content_type][1]:
        raise PictureError("Picture filename does not match its declared type")
    validators[content_type][0](data)
