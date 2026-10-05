"""Text encoding (GSM 03.38 / Latin-1 / UCS2 ...) and long-message segmentation."""
import random

GSM7_BASIC = ("@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ\x1bÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
              "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà")
GSM7_EXT = {"\f": 0x0A, "^": 0x14, "{": 0x28, "}": 0x29, "\\": 0x2F, "[": 0x3C, "~": 0x3D,
            "]": 0x3E, "|": 0x40, "€": 0x65}
_BASIC_MAP = {c: i for i, c in enumerate(GSM7_BASIC) if c != "\x1b"}
_BASIC_REV = {i: c for i, c in enumerate(GSM7_BASIC)}
_EXT_REV = {v: k for k, v in GSM7_EXT.items()}

# name -> (data_coding, codec)
ENCODINGS = {
    "gsm7": (0x00, "gsm7"),          # SMSC default alphabet, one septet per octet (unpacked)
    "gsm7_packed": (0x00, "gsm7p"),  # SMSC default alphabet, packed septets
    "ascii": (0x01, "ascii"),        # IA5
    "latin1": (0x03, "latin1"),
    "binary": (0x04, "binary"),      # message text is hex
    "ucs2": (0x08, "ucs2"),
}


def is_gsm7(text):
    return all(c in _BASIC_MAP or c in GSM7_EXT for c in text)


def gsm7_septets(text):
    """Encode to a list of septets; extension chars become [0x1B, x]."""
    out = []
    for c in text:
        if c in _BASIC_MAP:
            out.append(_BASIC_MAP[c])
        elif c in GSM7_EXT:
            out.extend((0x1B, GSM7_EXT[c]))
        else:
            out.append(_BASIC_MAP["?"])
    return out


def pack_septets(septets, fill_bits=0):
    out = bytearray()
    acc = 0
    nbits = fill_bits
    for s in septets:
        acc |= (s & 0x7F) << nbits
        nbits += 7
        while nbits >= 8:
            out.append(acc & 0xFF)
            acc >>= 8
            nbits -= 8
    if nbits > 0:
        out.append(acc & 0xFF)
    return bytes(out)


def decode_gsm7(data):
    out = []
    esc = False
    for b in data:
        if esc:
            out.append(_EXT_REV.get(b, "?"))
            esc = False
        elif b == 0x1B:
            esc = True
        else:
            out.append(_BASIC_REV.get(b & 0x7F, "?"))
    return "".join(out)


def decode_text(data, data_coding):
    try:
        if data_coding == 0x08:
            return data.decode("utf-16-be", "replace")
        if data_coding in (0x03, 0x01):
            return data.decode("latin-1")
        if data_coding == 0x00:
            # many SMSCs send DLR text as plain ASCII; GSM7 unpacked is ASCII-compatible for these
            return decode_gsm7(data) if any(b == 0x1B or b < 0x20 and b not in (10, 13) for b in data) \
                else data.decode("latin-1")
        if data_coding in (0x04, 0x02):
            return data.hex()
        return data.decode("latin-1")
    except Exception:
        return data.hex()


def resolve_encoding(text, name):
    """'auto' picks gsm7 when possible, otherwise ucs2."""
    if name == "auto":
        return "gsm7" if is_gsm7(text) else "ucs2"
    if name not in ENCODINGS:
        raise ValueError("unknown encoding %r" % name)
    return name


def _split_units(text, codec):
    """Split text into indivisible units with their encoded size (septets or octets)."""
    units = []
    if codec in ("gsm7", "gsm7p"):
        for c in text:
            units.append((c, 2 if c in GSM7_EXT else 1))
    elif codec == "ucs2":
        for c in text:
            units.append((c, 4 if ord(c) > 0xFFFF else 2))
    else:
        for c in text:
            units.append((c, 1))
    return units


def encode_units(text, codec):
    if codec == "gsm7":
        return bytes(gsm7_septets(text))
    if codec == "gsm7p":
        return pack_septets(gsm7_septets(text))
    if codec == "ucs2":
        return text.encode("utf-16-be")
    if codec == "latin1":
        return text.encode("latin-1", "replace")
    if codec == "ascii":
        return text.encode("ascii", "replace")
    if codec == "binary":
        return bytes.fromhex(text.replace(" ", ""))
    raise ValueError(codec)


def single_limit(codec):
    return 160 if codec in ("gsm7", "gsm7p") else 140


def part_limit(codec):
    """Payload capacity per segment when a 6-byte UDH is present."""
    return 153 if codec in ("gsm7", "gsm7p") else 134


def segment_text(text, codec, limit):
    parts, cur, size = [], [], 0
    for c, w in _split_units(text, codec):
        if size + w > limit and cur:
            parts.append("".join(cur))
            cur, size = [], 0
        cur.append(c)
        size += w
    if cur or not parts:
        parts.append("".join(cur))
    return parts


def text_size(text, codec):
    return sum(w for _, w in _split_units(text, codec))


class EncodedMessage:
    """The submit_sm parts a message expands to."""
    __slots__ = ("data_coding", "parts", "mode")

    def __init__(self, data_coding, parts, mode):
        self.data_coding = data_coding
        self.parts = parts      # list of (short_message_bytes, esm_class_or, extra_tlvs)
        self.mode = mode


def build_message(text, encoding="auto", long_mode="udh", data_coding_override=None):
    """Encode text into one or more submit_sm payloads.

    long_mode: udh | sar | payload | truncate
    """
    name = resolve_encoding(text, encoding)
    data_coding, codec = ENCODINGS[name]
    if data_coding_override is not None:
        data_coding = data_coding_override

    if codec == "binary":
        raw = encode_units(text, codec)
        if len(raw) <= 140 or long_mode == "truncate":
            return EncodedMessage(data_coding, [(raw[:254], 0, [])], "single")
        return EncodedMessage(data_coding, [(b"", 0, [(0x0424, raw)])], "payload")

    size = text_size(text, codec)
    if size <= single_limit(codec):
        return EncodedMessage(data_coding, [(encode_units(text, codec), 0, [])], "single")

    if long_mode == "truncate":
        part = segment_text(text, codec, single_limit(codec))[0]
        return EncodedMessage(data_coding, [(encode_units(part, codec), 0, [])], "truncated")

    if long_mode == "payload":
        return EncodedMessage(data_coding, [(b"", 0, [(0x0424, encode_units(text, codec))])], "payload")

    texts = segment_text(text, codec, part_limit(codec))
    if len(texts) > 255:
        raise ValueError("message too long (%d segments)" % len(texts))
    ref = random.randint(0, 255)
    total = len(texts)
    parts = []
    for i, t in enumerate(texts, 1):
        if long_mode == "sar":
            tlvs = [(0x020C, ref.to_bytes(2, "big")), (0x020E, bytes([total])), (0x020F, bytes([i]))]
            parts.append((encode_units(t, codec), 0, tlvs))
        else:
            udh = bytes([0x05, 0x00, 0x03, ref, total, i])
            if codec == "gsm7p":
                # 6 octet UDH = 48 bits -> 1 fill bit to reach septet boundary (49 = 7*7)
                payload = udh + pack_septets(gsm7_septets(t), fill_bits=1)
            else:
                payload = udh + encode_units(t, codec)
            parts.append((payload, 0x40, []))
    return EncodedMessage(data_coding, parts, long_mode)


def message_info(text, encoding="auto"):
    """Character/segment statistics shown in the GUI."""
    name = resolve_encoding(text, encoding) if text else "gsm7"
    codec = ENCODINGS[name][1]
    if codec == "binary":
        try:
            n = len(bytes.fromhex(text.replace(" ", "")))
        except ValueError:
            return {"encoding": name, "units": 0, "segments": 0, "error": "invalid hex"}
        return {"encoding": name, "units": n, "segments": 1 if n <= 140 else -(-n // 134)}
    size = text_size(text, codec)
    if size <= single_limit(codec):
        segs = 1
    else:
        segs = len(segment_text(text, codec, part_limit(codec)))
    return {"encoding": name, "units": size, "chars": len(text), "segments": segs,
            "unit_name": "septets" if codec.startswith("gsm7") else "octets",
            "single_limit": single_limit(codec), "part_limit": part_limit(codec)}
