"""SMPP 3.4 PDU codec: encoding/decoding of the PDUs a client needs."""
import struct

# ---------------------------------------------------------------- command ids
GENERIC_NACK = 0x80000000
BIND_RECEIVER = 0x00000001
BIND_RECEIVER_RESP = 0x80000001
BIND_TRANSMITTER = 0x00000002
BIND_TRANSMITTER_RESP = 0x80000002
SUBMIT_SM = 0x00000004
SUBMIT_SM_RESP = 0x80000004
DELIVER_SM = 0x00000005
DELIVER_SM_RESP = 0x80000005
UNBIND = 0x00000006
UNBIND_RESP = 0x80000006
BIND_TRANSCEIVER = 0x00000009
BIND_TRANSCEIVER_RESP = 0x80000009
ENQUIRE_LINK = 0x00000015
ENQUIRE_LINK_RESP = 0x80000015
OUTBIND = 0x0000000B
ALERT_NOTIFICATION = 0x00000102
DATA_SM = 0x00000103
DATA_SM_RESP = 0x80000103

COMMAND_NAMES = {
    GENERIC_NACK: "generic_nack",
    BIND_RECEIVER: "bind_receiver", BIND_RECEIVER_RESP: "bind_receiver_resp",
    BIND_TRANSMITTER: "bind_transmitter", BIND_TRANSMITTER_RESP: "bind_transmitter_resp",
    SUBMIT_SM: "submit_sm", SUBMIT_SM_RESP: "submit_sm_resp",
    DELIVER_SM: "deliver_sm", DELIVER_SM_RESP: "deliver_sm_resp",
    UNBIND: "unbind", UNBIND_RESP: "unbind_resp",
    BIND_TRANSCEIVER: "bind_transceiver", BIND_TRANSCEIVER_RESP: "bind_transceiver_resp",
    ENQUIRE_LINK: "enquire_link", ENQUIRE_LINK_RESP: "enquire_link_resp",
    DATA_SM: "data_sm", DATA_SM_RESP: "data_sm_resp",
    OUTBIND: "outbind", ALERT_NOTIFICATION: "alert_notification",
}

BIND_COMMANDS = {"TRX": BIND_TRANSCEIVER, "TX": BIND_TRANSMITTER, "RX": BIND_RECEIVER}

# ---------------------------------------------------------------- statuses
STATUS_NAMES = {
    0x00: "ESME_ROK", 0x01: "ESME_RINVMSGLEN", 0x02: "ESME_RINVCMDLEN", 0x03: "ESME_RINVCMDID",
    0x04: "ESME_RINVBNDSTS", 0x05: "ESME_RALYBND", 0x06: "ESME_RINVPRTFLG", 0x07: "ESME_RINVREGDLVFLG",
    0x08: "ESME_RSYSERR", 0x0A: "ESME_RINVSRCADR", 0x0B: "ESME_RINVDSTADR", 0x0C: "ESME_RINVMSGID",
    0x0D: "ESME_RBINDFAIL", 0x0E: "ESME_RINVPASWD", 0x0F: "ESME_RINVSYSID", 0x11: "ESME_RCANCELFAIL",
    0x13: "ESME_RREPLACEFAIL", 0x14: "ESME_RMSGQFUL", 0x15: "ESME_RINVSERTYP", 0x33: "ESME_RINVNUMDESTS",
    0x34: "ESME_RINVDLNAME", 0x40: "ESME_RINVDESTFLAG", 0x42: "ESME_RINVSUBREP", 0x43: "ESME_RINVESMCLASS",
    0x44: "ESME_RCNTSUBDL", 0x45: "ESME_RSUBMITFAIL", 0x48: "ESME_RINVSRCTON", 0x49: "ESME_RINVSRCNPI",
    0x50: "ESME_RINVDSTTON", 0x51: "ESME_RINVDSTNPI", 0x53: "ESME_RINVSYSTYP", 0x54: "ESME_RINVREPFLAG",
    0x55: "ESME_RINVNUMMSGS", 0x58: "ESME_RTHROTTLED", 0x61: "ESME_RINVSCHED", 0x62: "ESME_RINVEXPIRY",
    0x63: "ESME_RINVDFTMSGID", 0x64: "ESME_RX_T_APPN", 0x65: "ESME_RX_P_APPN", 0x66: "ESME_RX_R_APPN",
    0x67: "ESME_RQUERYFAIL", 0xC0: "ESME_RINVOPTPARSTREAM", 0xC1: "ESME_ROPTPARNOTALLWD",
    0xC2: "ESME_RINVPARLEN", 0xC3: "ESME_RMISSINGOPTPARAM", 0xC4: "ESME_RINVOPTPARAMVAL",
    0xFE: "ESME_RDELIVERYFAILURE", 0xFF: "ESME_RUNKNOWNERR",
}


def status_name(code):
    return STATUS_NAMES.get(code, "0x%08X" % code)


# ---------------------------------------------------------------- TLV tags
TLV_SAR_MSG_REF_NUM = 0x020C
TLV_SAR_TOTAL_SEGMENTS = 0x020E
TLV_SAR_SEGMENT_SEQNUM = 0x020F
TLV_MESSAGE_PAYLOAD = 0x0424
TLV_RECEIPTED_MESSAGE_ID = 0x001E
TLV_MESSAGE_STATE = 0x0427
TLV_NETWORK_ERROR_CODE = 0x0423
TLV_SC_INTERFACE_VERSION = 0x0210

MESSAGE_STATES = {1: "ENROUTE", 2: "DELIVRD", 3: "EXPIRED", 4: "DELETED", 5: "UNDELIV",
                  6: "ACCEPTD", 7: "UNKNOWN", 8: "REJECTD"}


class PDUError(ValueError):
    pass


def cstr(value, maxlen=None):
    if value is None:
        value = b""
    if isinstance(value, str):
        value = value.encode("latin-1", "replace")
    if maxlen is not None:
        value = value[:maxlen - 1]
    return value + b"\x00"


def header(command_id, status, seq, body):
    return struct.pack(">IIII", 16 + len(body), command_id, status, seq) + body


def encode_tlvs(tlvs):
    out = bytearray()
    for tag, value in tlvs:
        out += struct.pack(">HH", tag, len(value)) + value
    return bytes(out)


def bind(command_id, seq, system_id, password, system_type="", interface_version=0x34,
         addr_ton=0, addr_npi=0, address_range=""):
    # system_id / password are not truncated: SMPP 3.4 caps them at 15 / 8 chars, but many
    # servers issue longer credentials, and a silently truncated password is hard to debug.
    body = (cstr(system_id) + cstr(password) + cstr(system_type, 13) +
            struct.pack(">BBB", interface_version, addr_ton, addr_npi) + cstr(address_range, 41))
    return header(command_id, 0, seq, body)


class SubmitTemplate:
    """Pre-encodes the submit_sm fields that are identical for every message of a job, so only
    destination, esm_class, data_coding, short_message and per-part TLVs are encoded per PDU."""
    __slots__ = ("head", "esm_class", "mid", "tail", "sm_default_msg_id", "tlvs")

    def __init__(self, *, service_type="", source_addr_ton=0, source_addr_npi=0, source_addr="",
                 dest_addr_ton=1, dest_addr_npi=1, esm_class=0, protocol_id=0, priority_flag=0,
                 schedule_delivery_time="", validity_period="", registered_delivery=0,
                 replace_if_present_flag=0, sm_default_msg_id=0, tlvs=()):
        self.head = (cstr(service_type, 6) + struct.pack(">BB", source_addr_ton, source_addr_npi) +
                     cstr(source_addr, 21) + struct.pack(">BB", dest_addr_ton, dest_addr_npi))
        if not 0 <= esm_class <= 0xFF:
            raise PDUError("esm_class must be 0-255")
        self.esm_class = esm_class
        self.mid = (struct.pack(">BB", protocol_id, priority_flag) +
                    cstr(schedule_delivery_time, 17) + cstr(validity_period, 17) +
                    struct.pack(">BB", registered_delivery, replace_if_present_flag))
        self.sm_default_msg_id = sm_default_msg_id
        self.tlvs = encode_tlvs(tlvs)

    def build(self, seq, destination_addr, short_message, data_coding, esm_or=0, tlvs=b""):
        if len(short_message) > 254:
            raise PDUError("short_message longer than 254 octets; use message_payload")
        body = b"".join((self.head, cstr(destination_addr, 21), bytes((self.esm_class | esm_or,)), self.mid,
                         bytes((data_coding, self.sm_default_msg_id, len(short_message))), short_message,
                         tlvs, self.tlvs))
        return header(SUBMIT_SM, 0, seq, body)


def submit_sm(seq, *, destination_addr="", data_coding=0, short_message=b"", tlvs=(), **fields):
    return SubmitTemplate(**fields).build(seq, destination_addr, short_message, data_coding,
                                          tlvs=encode_tlvs(tlvs))


def simple(command_id, seq, status=0):
    return header(command_id, status, seq, b"")


def deliver_sm_resp(seq, status=0):
    return header(DELIVER_SM_RESP, status, seq, b"\x00")


def data_sm_resp(seq, status=0):
    return header(DATA_SM_RESP, status, seq, b"\x00")


# ---------------------------------------------------------------- decoding
class _Reader:
    __slots__ = ("buf", "pos")

    def __init__(self, buf):
        self.buf = buf
        self.pos = 0

    def cstr(self):
        end = self.buf.find(b"\x00", self.pos)
        if end < 0:
            val = self.buf[self.pos:]
            self.pos = len(self.buf)
        else:
            val = self.buf[self.pos:end]
            self.pos = end + 1
        return val.decode("latin-1")

    def u8(self):
        if self.pos >= len(self.buf):
            raise PDUError("truncated PDU")
        v = self.buf[self.pos]
        self.pos += 1
        return v

    def take(self, n):
        v = self.buf[self.pos:self.pos + n]
        self.pos += n
        return v

    def tlvs(self):
        out = {}
        while self.pos + 4 <= len(self.buf):
            tag, ln = struct.unpack_from(">HH", self.buf, self.pos)
            self.pos += 4
            out[tag] = self.buf[self.pos:self.pos + ln]
            self.pos += ln
        return out


def decode_body(command_id, body):
    """Decode the fields of PDUs we receive. Returns a dict (may be empty)."""
    r = _Reader(body)
    try:
        if command_id in (BIND_TRANSCEIVER_RESP, BIND_TRANSMITTER_RESP, BIND_RECEIVER_RESP):
            d = {"system_id": r.cstr() if body else ""}
            d["tlvs"] = r.tlvs()
            return d
        if command_id in (SUBMIT_SM_RESP, DATA_SM_RESP):
            return {"message_id": r.cstr() if body else ""}
        if command_id in (DELIVER_SM, DATA_SM, SUBMIT_SM):
            d = {"service_type": r.cstr(), "source_addr_ton": r.u8(), "source_addr_npi": r.u8(),
                 "source_addr": r.cstr(), "dest_addr_ton": r.u8(), "dest_addr_npi": r.u8(),
                 "destination_addr": r.cstr(), "esm_class": r.u8()}
            if command_id == DATA_SM:
                d["registered_delivery"] = r.u8()
                d["data_coding"] = r.u8()
                d["short_message"] = b""
            else:
                d["protocol_id"] = r.u8()
                d["priority_flag"] = r.u8()
                d["schedule_delivery_time"] = r.cstr()
                d["validity_period"] = r.cstr()
                d["registered_delivery"] = r.u8()
                d["replace_if_present_flag"] = r.u8()
                d["data_coding"] = r.u8()
                d["sm_default_msg_id"] = r.u8()
                d["short_message"] = r.take(r.u8())
            d["tlvs"] = r.tlvs()
            if not d["short_message"] and TLV_MESSAGE_PAYLOAD in d["tlvs"]:
                d["short_message"] = d["tlvs"][TLV_MESSAGE_PAYLOAD]
            return d
    except (PDUError, IndexError, struct.error) as e:
        return {"decode_error": str(e)}
    return {}


def hexdump(data, limit=512):
    data = data[:limit]
    return " ".join("%02x" % b for b in data)
