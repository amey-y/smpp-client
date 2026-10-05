"""One SMPP bind (one TCP connection) with windowing, keep-alive and auto-reconnect."""
import asyncio
import ssl
import struct
import time

from . import pdu as P

AUTH_FAILURES = {0x0E, 0x0F}   # invalid password / system id -> do not hammer the server
MAX_PDU = 64 * 1024
WRITE_HIGH_WATER = 512 * 1024


class Session:
    def __init__(self, conn, index, bind_type):
        self.conn = conn            # owning Connection
        self.index = index
        self.bind_type = bind_type  # TRX | TX | RX
        self.state = "idle"
        self.want = False
        self.reader = None
        self.writer = None
        self.task = None
        self.seq = 0
        self.pending = {}           # seq -> [sent_ts, ctx, command_id]
        self.inflight = 0
        self.last_error = ""
        self.server_system_id = ""
        self.bound_since = None
        self.reconnects = 0
        self.backoff = 1.0
        self.last_rx = 0.0
        self.enquire_sent = None
        self.enquire_rtt_ms = None
        self._bind_waiter = None
        self._unbind_waiter = None
        self.stats = {"submit": 0, "resp_ok": 0, "resp_err": 0, "timeout": 0,
                      "deliver": 0, "dlr": 0, "mo": 0, "nack": 0, "bytes_out": 0, "bytes_in": 0}

    # ------------------------------------------------------------- properties
    @property
    def profile(self):
        return self.conn.profile

    def can_submit(self):
        return (self.state == "bound" and self.bind_type != "RX" and
                self.inflight < self.profile["window"] and
                self.writer is not None and
                self.writer.transport.get_write_buffer_size() < WRITE_HIGH_WATER)

    def next_seq(self):
        self.seq = self.seq + 1 if self.seq < 0x7FFFFFFF else 1
        return self.seq

    def snapshot(self):
        return {"index": self.index, "bind_type": self.bind_type, "state": self.state,
                "inflight": self.inflight, "last_error": self.last_error,
                "server_system_id": self.server_system_id,
                "uptime": round(time.time() - self.bound_since) if self.bound_since else None,
                "reconnects": self.reconnects, "enquire_rtt_ms": self.enquire_rtt_ms,
                "stats": dict(self.stats)}

    def _set_state(self, state, error=None):
        self.state = state
        if error is not None:
            self.last_error = error
        self.conn.on_session_state(self)

    # ------------------------------------------------------------- lifecycle
    def start(self):
        self.want = True
        if self.task is None or self.task.done():
            self.backoff = 1.0
            self.task = asyncio.create_task(self._run(), name="smpp-%s-%d" % (self.conn.id, self.index))

    async def stop(self, graceful=True):
        self.want = False
        if self.task is None or self.task.done():
            if self.state not in ("idle",):
                self._set_state("idle")
            return
        if graceful and self.state == "bound":
            self._set_state("unbinding")
            # give in-flight submits a moment to be answered
            deadline = time.monotonic() + 5
            while self.inflight and time.monotonic() < deadline and self.writer:
                await asyncio.sleep(0.05)
            try:
                loop = asyncio.get_running_loop()
                self._unbind_waiter = loop.create_future()
                self._write(P.simple(P.UNBIND, self.next_seq()), P.UNBIND)
                await asyncio.wait_for(self._unbind_waiter, 3)
            except Exception:
                pass
        self.task.cancel()
        try:
            await self.task
        except BaseException:
            pass
        self._set_state("idle")

    async def _run(self):
        while self.want:
            try:
                self._set_state("connecting")
                await self._connect()
                self._set_state("binding")
                await self._bind()
                self.backoff = 1.0
                self.bound_since = time.time()
                self._set_state("bound", "")
                self.conn.log("info", "bind #%d bound (%s) server=%s" % (self.index, self.bind_type,
                                                                          self.server_system_id))
                await self._serve()
            except asyncio.CancelledError:
                self._close()
                self._fail_pending("cancelled")
                raise
            except BindRejected as e:
                self.last_error = str(e)
                self.conn.log("error", "bind #%d rejected: %s" % (self.index, e))
                if e.status in AUTH_FAILURES:
                    self.want = False
            except Exception as e:
                msg = "%s: %s" % (type(e).__name__, e) if str(e) else type(e).__name__
                self.last_error = msg
                if self.state == "bound":
                    self.conn.log("warn", "bind #%d connection lost: %s" % (self.index, msg))
                else:
                    self.conn.log("error", "bind #%d failed while %s: %s" % (self.index, self.state, msg))
            self._close()
            self._fail_pending("connection lost")
            self.bound_since = None
            if not self.want:
                break
            if not self.profile.get("reconnect", True):
                self.want = False
                break
            self._set_state("reconnecting")
            self.reconnects += 1
            await asyncio.sleep(self.backoff)
            self.backoff = min(self.backoff * 2, 30.0)
        self._set_state("idle" if not self.last_error else "error")

    async def _connect(self):
        p = self.profile
        ctx = None
        if p.get("tls"):
            ctx = ssl.create_default_context()
            if not p.get("tls_verify"):
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
        self.reader, self.writer = await asyncio.wait_for(
            asyncio.open_connection(p["host"], int(p["port"]), ssl=ctx, limit=MAX_PDU * 4),
            timeout=p.get("connect_timeout", 10))
        sock = self.writer.get_extra_info("socket")
        if sock is not None:
            import socket
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.last_rx = time.monotonic()

    async def _read_pdu(self):
        hdr = await self.reader.readexactly(16)
        length, cmd, status, seq = struct.unpack(">IIII", hdr)
        if length < 16 or length > MAX_PDU:
            raise ConnectionError("invalid command_length %d" % length)
        body = await self.reader.readexactly(length - 16) if length > 16 else b""
        self.last_rx = time.monotonic()
        self.stats["bytes_in"] += length
        if self.conn.trace:
            self.conn.add_trace(self, "in", cmd, status, seq, hdr + body)
        return cmd, status, seq, body

    async def _bind(self):
        p = self.profile
        seq = self.next_seq()
        data = P.bind(P.BIND_COMMANDS[self.bind_type], seq, p["system_id"], p["password"],
                      p.get("system_type", ""), int(p.get("interface_version", 0x34)),
                      int(p.get("addr_ton", 0)), int(p.get("addr_npi", 0)), p.get("address_range", ""))
        self._write(data, P.BIND_COMMANDS[self.bind_type])
        deadline = time.monotonic() + p.get("bind_timeout", 10)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("no bind response")
            cmd, status, rseq, body = await asyncio.wait_for(self._read_pdu(), remaining)
            if cmd & 0x80000000 and cmd & 0x7FFFFFFF == P.BIND_COMMANDS[self.bind_type]:
                if status != 0:
                    raise BindRejected(status)
                d = P.decode_body(cmd, body)
                self.server_system_id = d.get("system_id", "")
                return
            if cmd == P.GENERIC_NACK:
                raise BindRejected(status)
            if cmd == P.ENQUIRE_LINK:
                self._write(P.simple(P.ENQUIRE_LINK_RESP, rseq), P.ENQUIRE_LINK_RESP)

    async def _serve(self):
        keep = asyncio.create_task(self._keepalive())
        try:
            while True:
                cmd, status, seq, body = await self._read_pdu()
                self._dispatch(cmd, status, seq, body)
        finally:
            keep.cancel()

    def _dispatch(self, cmd, status, seq, body):
        if cmd == P.SUBMIT_SM_RESP or cmd == P.DATA_SM_RESP:
            entry = self.pending.pop(seq, None)
            if entry is None:
                return  # late response for an already timed out request
            self.inflight -= 1
            latency = (time.monotonic() - entry[0]) * 1000.0
            msg_id = P.decode_body(cmd, body).get("message_id", "") if status == 0 else ""
            if status == 0:
                self.stats["resp_ok"] += 1
            else:
                self.stats["resp_err"] += 1
            self.conn.on_submit_resp(self, entry[1], status, msg_id, latency)
        elif cmd == P.DELIVER_SM or cmd == P.DATA_SM:
            d = P.decode_body(cmd, body)
            resp = P.deliver_sm_resp(seq) if cmd == P.DELIVER_SM else P.data_sm_resp(seq)
            self._write(resp, cmd | 0x80000000)
            self.stats["deliver"] += 1
            self.conn.on_deliver(self, d)
        elif cmd == P.ENQUIRE_LINK:
            self._write(P.simple(P.ENQUIRE_LINK_RESP, seq), P.ENQUIRE_LINK_RESP)
        elif cmd == P.ENQUIRE_LINK_RESP:
            if self.enquire_sent is not None:
                self.enquire_rtt_ms = round((time.monotonic() - self.enquire_sent) * 1000, 1)
                self.enquire_sent = None
        elif cmd == P.UNBIND:
            self._write(P.simple(P.UNBIND_RESP, seq), P.UNBIND_RESP)
            self.conn.log("warn", "bind #%d: server sent unbind" % self.index)
            raise ConnectionError("unbind requested by server")
        elif cmd == P.UNBIND_RESP:
            if self._unbind_waiter and not self._unbind_waiter.done():
                self._unbind_waiter.set_result(True)
        elif cmd == P.GENERIC_NACK:
            self.stats["nack"] += 1
            entry = self.pending.pop(seq, None)
            if entry is not None:
                self.inflight -= 1
                self.conn.on_submit_resp(self, entry[1], status or 0xFF, "",
                                         (time.monotonic() - entry[0]) * 1000.0)
            self.conn.log("warn", "bind #%d: generic_nack seq=%d status=%s" %
                          (self.index, seq, P.status_name(status)))
        elif not cmd & 0x80000000:
            # unsupported request from server -> generic_nack(invalid command id)
            self._write(P.header(P.GENERIC_NACK, 0x03, seq, b""), P.GENERIC_NACK)

    async def _keepalive(self):
        p = self.profile
        interval = max(1.0, float(p.get("enquire_interval", 30)))
        resp_timeout = float(p.get("response_timeout", 30))
        last_enquire = time.monotonic()
        while True:
            await asyncio.sleep(0.5)
            now = time.monotonic()
            # expire requests that never got an answer
            if self.pending:
                expired = [s for s, e in self.pending.items() if now - e[0] > resp_timeout]
                for s in expired:
                    entry = self.pending.pop(s)
                    self.inflight -= 1
                    self.stats["timeout"] += 1
                    self.conn.on_submit_timeout(self, entry[1])
                if expired:
                    self.conn.capacity_changed()
            if self.enquire_sent is not None and now - self.enquire_sent > resp_timeout:
                self.conn.log("warn", "bind #%d: enquire_link_resp timeout, reconnecting" % self.index)
                self.writer.close()
                return
            if now - last_enquire >= interval:
                last_enquire = now
                if self.enquire_sent is None:
                    self.enquire_sent = now
                    self._write(P.simple(P.ENQUIRE_LINK, self.next_seq()), P.ENQUIRE_LINK)

    # ------------------------------------------------------------- sending
    def _write(self, data, cmd):
        if self.writer is None:
            raise ConnectionError("not connected")
        self.writer.write(data)
        self.stats["bytes_out"] += len(data)
        if self.conn.trace:
            self.conn.add_trace(self, "out", cmd, struct.unpack_from(">I", data, 8)[0],
                                struct.unpack_from(">I", data, 12)[0], data)

    def submit(self, build, ctx):
        """build(seq) -> PDU bytes. Caller must have checked can_submit()."""
        seq = self.next_seq()
        data = build(seq)
        self._write(data, P.SUBMIT_SM)
        self.pending[seq] = [time.monotonic(), ctx, P.SUBMIT_SM]
        self.inflight += 1
        self.stats["submit"] += 1
        return seq

    # ------------------------------------------------------------- cleanup
    def _close(self):
        if self.writer is not None:
            try:
                self.writer.close()
            except Exception:
                pass
        self.writer = None
        self.reader = None
        self.enquire_sent = None

    def _fail_pending(self, reason):
        if not self.pending:
            return
        pending, self.pending = self.pending, {}
        self.inflight = 0
        for entry in pending.values():
            self.conn.on_submit_lost(self, entry[1], reason)
        self.conn.capacity_changed()


class BindRejected(Exception):
    def __init__(self, status):
        super().__init__("%s (0x%02X)" % (P.status_name(status), status))
        self.status = status
