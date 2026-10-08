"""A small SMPP 3.4 server simulator for testing the client without a real SMSC.

Features: accepts any credentials (or a fixed system_id/password), limits binds per system_id,
optional per-bind TPS throttle (returns ESME_RTHROTTLED), random reject rate, response delay,
and delivery receipts with a configurable delay and status mix. Receipts go back on the TRX bind that
submitted the message, or on an RX/TRX bind of the same system_id for TX binds.

Standalone:  python -m smppclient.mock_smsc --port 2775 --max-binds 10
"""
import argparse
import asyncio
import collections
import random
import struct
import time
import uuid

from . import pdu as P

DEFAULTS = {
    "host": "0.0.0.0", "port": 2775, "system_id": "", "password": "", "max_binds": 10,
    "tps_per_bind": 0, "reject_rate": 0.0, "resp_delay_ms": 0, "dlr_delay_ms": 1000,
    "dlr_undeliv_rate": 0.05, "send_dlr": True,
}


class MockSMSC:
    def __init__(self, log=print, **cfg):
        self.cfg = dict(DEFAULTS)
        self.cfg.update({k: v for k, v in cfg.items() if v is not None})
        self.log = log
        self.server = None
        self.binds = collections.Counter()
        self.receivers = collections.defaultdict(list)   # system_id -> [(send, next_seq)] of RX/TRX binds
        self.clients = set()
        self.c = collections.Counter()

    @property
    def running(self):
        return self.server is not None

    async def start(self):
        self.server = await asyncio.start_server(self._client, self.cfg["host"], int(self.cfg["port"]))
        self.log("info", "mock SMSC listening on %s:%s" % (self.cfg["host"], self.cfg["port"]))

    async def stop(self):
        if self.server:
            self.server.close()
            for w in list(self.clients):
                w.close()
            await self.server.wait_closed()
            self.server = None
            self.binds.clear()
            self.receivers.clear()
            self.log("info", "mock SMSC stopped")

    def snapshot(self):
        return {"running": self.running, "config": self.cfg, "binds": dict(self.binds),
                "connections": len(self.clients), "counts": dict(self.c)}

    async def _client(self, reader, writer):
        self.clients.add(writer)
        bound_as = None
        seq = [0]
        window = collections.deque()
        tasks = set()
        rx_entry = None

        def send(data):
            if not writer.is_closing():
                writer.write(data)

        def next_seq():
            seq[0] += 1
            return seq[0]

        async def later(delay, data):
            await asyncio.sleep(delay)
            send(data)

        def spawn(coro):
            t = asyncio.create_task(coro)
            tasks.add(t)
            t.add_done_callback(tasks.discard)

        try:
            while True:
                hdr = await reader.readexactly(16)
                length, cmd, status, sq = struct.unpack(">IIII", hdr)
                body = await reader.readexactly(length - 16) if length > 16 else b""
                if cmd in (P.BIND_TRANSCEIVER, P.BIND_TRANSMITTER, P.BIND_RECEIVER):
                    r = P._Reader(body)
                    sid, pwd = r.cstr(), r.cstr()
                    resp_cmd = cmd | 0x80000000
                    if bound_as:
                        send(P.header(resp_cmd, 0x05, sq, b"\x00"))
                    elif self.cfg["system_id"] and (sid != self.cfg["system_id"]):
                        send(P.header(resp_cmd, 0x0F, sq, b"\x00"))
                    elif self.cfg["password"] and pwd != self.cfg["password"]:
                        send(P.header(resp_cmd, 0x0E, sq, b"\x00"))
                    elif self.binds[sid] >= int(self.cfg["max_binds"]):
                        self.c["bind_rejected"] += 1
                        send(P.header(resp_cmd, 0x0D, sq, b"\x00"))
                    else:
                        bound_as = (sid, cmd)
                        if cmd != P.BIND_TRANSMITTER:
                            rx_entry = (send, next_seq)
                            self.receivers[sid].append(rx_entry)
                        self.binds[sid] += 1
                        self.c["binds"] += 1
                        send(P.header(resp_cmd, 0, sq, P.cstr("MOCKSMSC")))
                elif cmd == P.SUBMIT_SM:
                    if not bound_as or bound_as[1] == P.BIND_RECEIVER:
                        send(P.header(P.SUBMIT_SM_RESP, 0x04, sq, b""))
                        continue
                    self.c["submit"] += 1
                    now = time.monotonic()
                    tps = float(self.cfg["tps_per_bind"] or 0)
                    if tps > 0:
                        while window and now - window[0] > 1.0:
                            window.popleft()
                        if len(window) >= tps:
                            self.c["throttled"] += 1
                            send(P.header(P.SUBMIT_SM_RESP, 0x58, sq, b"\x00"))
                            continue
                        window.append(now)
                    if random.random() < float(self.cfg["reject_rate"]):
                        self.c["rejected"] += 1
                        send(P.header(P.SUBMIT_SM_RESP, 0x45, sq, b"\x00"))
                        continue
                    d = P.decode_body(P.SUBMIT_SM, body)
                    msg_id = uuid.uuid4().hex[:20]
                    resp = P.header(P.SUBMIT_SM_RESP, 0, sq, P.cstr(msg_id))
                    delay = float(self.cfg["resp_delay_ms"] or 0) / 1000.0
                    if delay > 0:
                        spawn(later(delay, resp))
                    else:
                        send(resp)
                    if self.cfg["send_dlr"] and d.get("registered_delivery", 0) & 0x03:
                        spawn(self._dlr(bound_as[0], rx_entry, d, msg_id))
                elif cmd == P.ENQUIRE_LINK:
                    send(P.simple(P.ENQUIRE_LINK_RESP, sq))
                elif cmd == P.UNBIND:
                    send(P.simple(P.UNBIND_RESP, sq))
                    await writer.drain()
                    break
                elif cmd & 0x80000000:
                    pass   # responses to our deliver_sm / enquire_link
                else:
                    send(P.header(P.GENERIC_NACK, 0x03, sq, b""))
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            for t in tasks:
                t.cancel()
            if rx_entry is not None:
                self.receivers[bound_as[0]].remove(rx_entry)
                if not self.receivers[bound_as[0]]:
                    del self.receivers[bound_as[0]]
            if bound_as:
                self.binds[bound_as[0]] -= 1
                if self.binds[bound_as[0]] <= 0:
                    del self.binds[bound_as[0]]
            self.clients.discard(writer)
            writer.close()

    async def _dlr(self, system_id, own, d, msg_id):
        await asyncio.sleep(float(self.cfg["dlr_delay_ms"]) / 1000.0 * random.uniform(0.5, 1.5))
        target = own or (random.choice(self.receivers[system_id]) if self.receivers.get(system_id) else None)
        if target is None:
            self.c["dlr_no_receiver"] += 1
            return
        send, next_seq = target
        undeliv = random.random() < float(self.cfg["dlr_undeliv_rate"])
        stat, err, dlvrd = ("UNDELIV", "001", "000") if undeliv else ("DELIVRD", "000", "001")
        ts = time.strftime("%y%m%d%H%M")
        text = ("id:%s sub:001 dlvrd:%s submit date:%s done date:%s stat:%s err:%s Text:" %
                (msg_id, dlvrd, ts, ts, stat, err)).encode()
        body = (P.cstr("") + bytes([1, 1]) + P.cstr(d.get("destination_addr", "")) +
                bytes([d.get("source_addr_ton", 0), d.get("source_addr_npi", 0)]) +
                P.cstr(d.get("source_addr", "")) + bytes([0x04, 0, 0]) + b"\x00\x00" +
                bytes([0, 0, 0, 0, len(text)]) + text +
                P.encode_tlvs([(P.TLV_RECEIPTED_MESSAGE_ID, msg_id.encode() + b"\x00"),
                               (P.TLV_MESSAGE_STATE, bytes([5 if undeliv else 2]))]))
        self.c["dlr"] += 1
        send(P.header(P.DELIVER_SM, 0, next_seq(), body))


def main():
    ap = argparse.ArgumentParser(prog="smppclient mock", description="Mock SMPP server")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=2775)
    ap.add_argument("--system-id", default="")
    ap.add_argument("--password", default="")
    ap.add_argument("--max-binds", type=int, default=10)
    ap.add_argument("--tps-per-bind", type=float, default=0)
    ap.add_argument("--reject-rate", type=float, default=0.0)
    ap.add_argument("--resp-delay-ms", type=float, default=0)
    ap.add_argument("--dlr-delay-ms", type=float, default=1000)
    ap.add_argument("--dlr-undeliv-rate", type=float, default=0.05)
    a = ap.parse_args()

    async def run():
        m = MockSMSC(log=lambda lvl, msg: print("[%s] %s" % (lvl, msg)), **{
            "host": a.host, "port": a.port, "system_id": a.system_id, "password": a.password,
            "max_binds": a.max_binds, "tps_per_bind": a.tps_per_bind, "reject_rate": a.reject_rate,
            "resp_delay_ms": a.resp_delay_ms, "dlr_delay_ms": a.dlr_delay_ms,
            "dlr_undeliv_rate": a.dlr_undeliv_rate})
        await m.start()
        while True:
            await asyncio.sleep(10)
            print("[stats]", dict(m.c), "binds:", dict(m.binds))
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
