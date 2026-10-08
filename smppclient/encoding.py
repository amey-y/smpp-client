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
    "latin1_dc0": (0x00, "latin1"),  # Latin-1 octets as SMSC default alphabet (SMSC transcodes)
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


# udh length (octets) used per long-message mode; SAR parts get the UDH added by the SMSC
UDH_LEN = {"udh": 6, "udh16": 7, "sar": 6}


def counts_septets(name):
    """True when segment limits are in GSM 7-bit characters (the SMSC sends them as GSM7)."""
    return ENCODINGS[name][0] == 0x00


def single_limit(name):
    return 160 if counts_septets(name) else 140


def part_limit(name, udh_len=6):
    """Payload capacity per segment when a UDH of udh_len octets is present."""
    if counts_septets(name):
        return (140 - udh_len) * 8 // 7
    n = 140 - udh_len
    return n - n % 2 if ENCODINGS[name][1] == "ucs2" else n


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

    long_mode: udh | udh16 | sar | payload | truncate
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
    if size <= single_limit(name):
        return EncodedMessage(data_coding, [(encode_units(text, codec), 0, [])], "single")

    if long_mode == "truncate":
        part = segment_text(text, codec, single_limit(name))[0]
        return EncodedMessage(data_coding, [(encode_units(part, codec), 0, [])], "truncated")

    if long_mode == "payload":
        return EncodedMessage(data_coding, [(b"", 0, [(0x0424, encode_units(text, codec))])], "payload")

    if long_mode not in UDH_LEN:
        raise ValueError("unknown long message mode %r" % long_mode)
    udh_len = UDH_LEN[long_mode]
    texts = segment_text(text, codec, part_limit(name, udh_len))
    if len(texts) > 255:
        raise ValueError("message too long (%d segments)" % len(texts))
    ref = random.randint(0, 0xFFFF if long_mode == "udh16" else 0xFF)
    total = len(texts)
    parts = []
    for i, t in enumerate(texts, 1):
        if long_mode == "sar":
            tlvs = [(0x020C, ref.to_bytes(2, "big")), (0x020E, bytes([total])), (0x020F, bytes([i]))]
            parts.append((encode_units(t, codec), 0, tlvs))
            continue
        if long_mode == "udh16":
            udh = bytes([0x06, 0x08, 0x04, ref >> 8, ref & 0xFF, total, i])
        else:
            udh = bytes([0x05, 0x00, 0x03, ref, total, i])
        if codec == "gsm7p":
            # pad the UDH to a septet boundary
            payload = udh + pack_septets(gsm7_septets(t), fill_bits=(7 - len(udh) * 8 % 7) % 7)
        else:
            payload = udh + encode_units(t, codec)
        parts.append((payload, 0x40, []))
    return EncodedMessage(data_coding, parts, long_mode)


def split_udh(data):
    """Split a short_message that starts with a UDH. Returns (concat_info_or_None, body), where
    concat_info is (ref, total, seq) from an 8- or 16-bit concatenation IE."""
    if not data:
        return None, data
    udhl = data[0]
    udh, body = data[1:1 + udhl], data[1 + udhl:]
    i, concat = 0, None
    while i + 2 <= len(udh):
        iei, ln = udh[i], udh[i + 1]
        ie = udh[i + 2:i + 2 + ln]
        if iei == 0x00 and ln == 3:
            concat = (ie[0], ie[1], ie[2])
        elif iei == 0x08 and ln == 4:
            concat = ((ie[0] << 8) | ie[1], ie[2], ie[3])
        i += 2 + ln
    return concat, body


def message_info(text, encoding="auto", long_mode="udh"):
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
    plimit = part_limit(name, UDH_LEN.get(long_mode, 6))
    if size <= single_limit(name):
        segs = 1
    else:
        segs = len(segment_text(text, codec, plimit))
    return {"encoding": name, "units": size, "chars": len(text), "segments": segs,
            "unit_name": "septets" if codec.startswith("gsm7") else "octets",
            "single_limit": single_limit(name), "part_limit": plimit}
