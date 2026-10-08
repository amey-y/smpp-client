"""Connections (bind pools), send jobs, rate limiting, DLR matching and statistics."""
import asyncio
import collections
import csv
import itertools
import json
import os
import random
import re
import sys
import time
import uuid

from . import pdu as P
from .encoding import build_message, decode_text, split_udh
from .session import Session

DATA_DIR = os.environ.get("SMPPCLIENT_DATA", os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "data"))
LOG_STDOUT = os.environ.get("SMPPCLIENT_LOG_STDOUT", "1").lower() not in ("0", "false", "no", "")

DEFAULT_PROFILE = {
    "name": "default", "host": "127.0.0.1", "port": 2775, "system_id": "", "password": "",
    "system_type": "", "interface_version": 0x34, "addr_ton": 0, "addr_npi": 0, "address_range": "",
    "bind_type": "TRX", "binds": 1, "rx_binds": 0, "window": 50, "enquire_interval": 30,
    "response_timeout": 30, "bind_timeout": 10, "connect_timeout": 10, "reconnect": True,
    "tls": False, "tls_verify": False, "bind_stagger_ms": 100, "max_tps": 0,
}

RETRYABLE = {0x58, 0x14}   # ESME_RTHROTTLED, ESME_RMSGQFUL
DLR_RE = re.compile(r"id:(?P<id>\S+).*?stat:(?P<stat>\w+)(?:.*?err:(?P<err>\w+))?", re.I | re.S)
SERIES_LEN = 300
DLR_ESM_TYPES = (0x04, 0x08, 0x20)   # SMSC delivery receipt, SME delivery ack, intermediate notification
MO_PART_TTL = 600                    # seconds to wait for the missing parts of a concatenated MO


def _canon_msgid(msg_id):
    """SMSCs disagree on case and zero padding of message ids between submit_sm_resp and DLR."""
    return msg_id.strip().lower().lstrip("0") or "0"


def now_ms():
    return int(time.time() * 1000)


def _pct(sorted_vals, p):
    if not sorted_vals:
        return None
    k = min(len(sorted_vals) - 1, max(0, int(round(p / 100.0 * (len(sorted_vals) - 1)))))
    return round(sorted_vals[k], 1)


def latency_summary(samples):
    if not samples:
        return None
    s = sorted(samples)
    return {"avg": round(sum(s) / len(s), 1), "min": round(s[0], 1), "p50": _pct(s, 50),
            "p95": _pct(s, 95), "p99": _pct(s, 99), "max": round(s[-1], 1), "n": len(s)}


# ====================================================================== rate limiter
class RateLimiter:
    """Token bucket; tps <= 0 means unlimited. Tokens are released in ~10ms bursts."""

    def __init__(self, tps, ramp_seconds=0):
        self.tps = float(tps)
        self.ramp = float(ramp_seconds or 0)
        self.start = time.monotonic()
        self.tokens = 0.0
        self.last = self.start

    def current_tps(self):
        if self.tps <= 0:
            return 0
        if self.ramp > 0:
            frac = min(1.0, max(0.05, (time.monotonic() - self.start) / self.ramp))
            return self.tps * frac
        return self.tps

    def reset_clock(self):
        self.last = time.monotonic()
        self.tokens = 0.0

    async def acquire(self):
        tps = self.current_tps()
        if tps <= 0:
            return
        burst = max(1.0, tps / 100.0)
        while True:
            now = time.monotonic()
            self.tokens = min(burst, self.tokens + (now - self.last) * tps)
            self.last = now
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return
            await asyncio.sleep(max(0.001, (1.0 - self.tokens) / tps))
            tps = self.current_tps()
            burst = max(1.0, tps / 100.0)


# ====================================================================== recipients
def clean_number(n):
    n = n.strip().strip('"').strip("'")
    for ch in " -()\t":
        n = n.replace(ch, "")
    if n.startswith("+"):
        n = n[1:]
    elif n.startswith("00"):
        n = n[2:]
    return n


def parse_number_list(text, dedupe=True):
    out, seen, invalid = [], set(), 0
    for raw in re.split(r"[\n,;]+", text or ""):
        if not raw.strip():
            continue
        n = clean_number(raw)
        if not n.isdigit() or len(n) > 20:
            invalid += 1
            continue
        if dedupe:
            if n in seen:
                continue
            seen.add(n)
        out.append(n)
    return out, invalid


def read_csv_recipients(path, number_col=0, message_col=None, dedupe=False):
    """Return list of (number, message_or_None). Rows with a non numeric number are skipped
    (this skips header lines)."""
    out, seen, skipped = [], set(), 0
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
        sample = fh.read(4096)
        fh.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|") if sample.strip() else csv.excel
        except csv.Error:
            dialect = csv.excel
        for row in csv.reader(fh, dialect):
            if not row or len(row) <= number_col:
                continue
            n = clean_number(row[number_col])
            if not n.isdigit() or len(n) > 20:
                skipped += 1
                continue
            if dedupe:
                if n in seen:
                    continue
                seen.add(n)
            msg = None
            if message_col is not None and len(row) > message_col and row[message_col] != "":
                msg = row[message_col]
            out.append((n, msg))
    return out, skipped


class NumberGenerator:
    """Generates destination numbers: <country_code><prefix><random/sequential digits>."""

    def __init__(self, count, country_code="", prefixes="", length=10, mode="random", start=0, unique=True):
        self.count = int(count)
        self.cc = re.sub(r"\D", "", str(country_code or ""))
        self.prefixes = [p for p in re.split(r"[,\s]+", str(prefixes or "")) if p.isdigit()] or [""]
        self.length = int(length)
        self.mode = mode
        self.start = int(start or 0)
        self.unique = unique
        for p in self.prefixes:
            if len(p) >= self.length:
                raise ValueError("prefix %s is not shorter than number length %d" % (p, self.length))
        space = sum(10 ** (self.length - len(p)) for p in self.prefixes)
        if self.count > space:
            raise ValueError("only %d distinct numbers possible with these settings" % space)

    def __iter__(self):
        if self.mode == "sequential":
            per = [10 ** (self.length - len(p)) for p in self.prefixes]
            produced = 0
            for p, cap in zip(self.prefixes, per):
                width = self.length - len(p)
                i = self.start if p == self.prefixes[0] else 0
                while i < cap and produced < self.count:
                    yield self.cc + p + str(i).zfill(width)
                    i += 1
                    produced += 1
                if produced >= self.count:
                    return
            return
        seen = set() if self.unique and self.count <= 5_000_000 else None
        produced = 0
        rnd = random.Random()
        while produced < self.count:
            p = rnd.choice(self.prefixes)
            width = self.length - len(p)
            n = self.cc + p + str(rnd.randrange(10 ** width)).zfill(width)
            if seen is not None:
                if n in seen:
                    continue
                seen.add(n)
            produced += 1
            yield n


# ====================================================================== templating
_TOKEN_RE = re.compile(r"\{(number|seq|rand|otp|uuid|time|date|job|bind)(?::(\d+))?\}")


def render_template(tpl, number, seq, job_id):
    if "{" not in tpl:
        return tpl

    def sub(m):
        name, arg = m.group(1), m.group(2)
        if name == "number":
            return number
        if name == "seq":
            return str(seq).zfill(int(arg)) if arg else str(seq)
        if name in ("rand", "otp"):
            width = int(arg) if arg else 6
            return str(random.randrange(10 ** width)).zfill(width)
        if name == "uuid":
            return uuid.uuid4().hex[: int(arg)] if arg else uuid.uuid4().hex
        if name == "time":
            return time.strftime("%H:%M:%S")
        if name == "date":
            return time.strftime("%Y-%m-%d")
        if name == "job":
            return job_id
        return m.group(0)
    return _TOKEN_RE.sub(sub, tpl)


def template_is_static(tpl):
    return not _TOKEN_RE.search(tpl or "")


# ====================================================================== job
class Item:
    __slots__ = ("job", "idx", "dest", "part", "nparts", "payload", "esm", "tlvs", "dc", "attempt",
                 "submitted_at")

    def __init__(self, job, idx, dest, part, nparts, payload, esm, tlvs, dc):
        self.job = job
        self.idx = idx
        self.dest = dest
        self.part = part
        self.nparts = nparts
        self.payload = payload
        self.esm = esm
        self.tlvs = tlvs
        self.dc = dc
        self.attempt = 0
        self.submitted_at = 0.0


class Job:
    def __init__(self, manager, conn, spec):
        self.manager = manager
        self.conn = conn
        self.id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        self.spec = spec
        self.name = spec.get("name") or spec.get("kind", "job")
        self.kind = spec.get("kind", "bulk")
        self.state = "pending"
        self.error = ""
        self.created = time.time()
        self.started = None
        self.finished = None
        self.planned_messages = 0
        self.limiter = RateLimiter(spec.get("tps", 0), spec.get("ramp_seconds", 0))
        self.want_dlr = bool(int(spec.get("registered_delivery", 1)) & 0x03)
        self.template = P.SubmitTemplate(
            service_type=spec.get("service_type", ""),
            source_addr_ton=int(spec.get("source_ton", 5)), source_addr_npi=int(spec.get("source_npi", 0)),
            source_addr=spec.get("source_addr", ""),
            dest_addr_ton=int(spec.get("dest_ton", 1)), dest_addr_npi=int(spec.get("dest_npi", 1)),
            esm_class=int(spec.get("esm_class", 0)), protocol_id=int(spec.get("protocol_id", 0)),
            priority_flag=int(spec.get("priority", 0)),
            schedule_delivery_time=spec.get("schedule_delivery_time", ""),
            validity_period=spec.get("validity_period", ""),
            registered_delivery=int(spec.get("registered_delivery", 1)),
            tlvs=manager.static_tlvs(spec))
        self.paused = asyncio.Event()
        self.paused.set()
        self.stop_flag = False
        self.retry_q = collections.deque()
        self.c = collections.Counter()   # messages, parts, submitted, ok, err, timeout, lost, retries, dlr
        self.errors = collections.Counter()
        self.dlr_stats = collections.Counter()
        self.latency = collections.deque(maxlen=20000)
        self.dlr_latency = collections.deque(maxlen=20000)
        self.recent = collections.deque(maxlen=300)
        self.series = collections.deque(maxlen=SERIES_LEN)
        self._last_counts = (0, 0, 0)
        self.task = None
        self.single_result = None
        self.dir = os.path.join(DATA_DIR, "jobs", self.id)
        os.makedirs(self.dir, exist_ok=True)
        self._sub_fh = open(os.path.join(self.dir, "submits.csv"), "w", newline="")
        self._sub_csv = csv.writer(self._sub_fh)
        self._sub_csv.writerow(["time", "msg_no", "destination", "part", "parts", "bind", "attempt",
                                "status", "status_name", "message_id", "latency_ms"])
        self._dlr_fh = open(os.path.join(self.dir, "dlrs.csv"), "w", newline="")
        self._dlr_csv = csv.writer(self._dlr_fh)
        self._dlr_csv.writerow(["time", "message_id", "destination", "stat", "err", "dlr_latency_ms",
                                "bind", "text"])
        self._files_open = True

    # ---------------------------------------------------------- recipients
    def _recipients(self):
        """Yields (number, message_override_or_None)."""
        spec = self.spec
        src = spec.get("source", "list")
        if src == "list":
            nums, _ = parse_number_list(spec.get("numbers", ""), spec.get("dedupe", True))
            self.planned_base = len(nums)
            return ((n, None) for n in nums)
        if src == "generate":
            g = NumberGenerator(spec.get("gen_count", 10), spec.get("gen_country_code", ""),
                                spec.get("gen_prefixes", ""), spec.get("gen_length", 10),
                                spec.get("gen_mode", "random"), spec.get("gen_start", 0),
                                spec.get("gen_unique", True))
            self.planned_base = g.count
            return ((n, None) for n in g)
        if src == "csv":
            path = self.manager.upload_path(spec.get("csv_id", ""))
            mcol = spec.get("csv_message_col")
            mcol = int(mcol) - 1 if mcol not in (None, "", 0, "0") else None
            rows, _ = read_csv_recipients(path, int(spec.get("csv_number_col", 1)) - 1, mcol,
                                          spec.get("dedupe", False))
            self.planned_base = len(rows)
            return iter(rows)
        raise ValueError("unknown recipient source %r" % src)

    def _stream(self):
        repeat = max(1, int(self.spec.get("repeat", 1) or 1))
        duration = float(self.spec.get("duration", 0) or 0)
        max_msgs = int(self.spec.get("max_messages", 0) or 0)
        first = list(self._recipients()) if (repeat > 1 or duration) else None
        if first is not None:
            if not first:
                return
            self.planned_messages = 0 if duration else len(first) * repeat
            loops = itertools.count() if duration else range(repeat)
            src = (r for _ in loops for r in first)
        else:
            src = self._recipients()
            self.planned_messages = self.planned_base
        if max_msgs:
            self.planned_messages = min(self.planned_messages, max_msgs) if self.planned_messages else max_msgs
            src = itertools.islice(src, max_msgs)
        deadline = time.monotonic() + duration if duration else None
        for r in src:
            if deadline and time.monotonic() >= deadline:
                return
            yield r

    # ---------------------------------------------------------- run
    def start(self):
        self.task = asyncio.create_task(self._run())

    async def _run(self):
        spec = self.spec
        self.state = "running"
        self.started = time.time()
        self.manager.log("info", "job %s (%s) started on %s" % (self.id, self.name, self.conn.profile["name"]))
        try:
            text_tpl = spec.get("message", "")
            static = template_is_static(text_tpl)
            enc_name = spec.get("encoding", "auto")
            long_mode = spec.get("long_mode", "udh")
            dc_override = spec.get("data_coding_override")
            dc_override = int(dc_override) if dc_override not in (None, "") else None
            cached = build_message(text_tpl, enc_name, long_mode, dc_override) if static else None
            idx = 0
            self.limiter.reset_clock()
            for number, override in self._stream():
                if self.stop_flag:
                    break
                idx += 1
                if override is not None:
                    enc = build_message(render_template(override, number, idx, self.id), enc_name,
                                        long_mode, dc_override)
                elif cached is not None:
                    enc = cached
                else:
                    enc = build_message(render_template(text_tpl, number, idx, self.id), enc_name,
                                        long_mode, dc_override)
                nparts = len(enc.parts)
                for pno, (payload, esm, tlvs) in enumerate(enc.parts, 1):
                    if not await self._send(Item(self, idx, number, pno, nparts, payload, esm, tlvs,
                                                 enc.data_coding)):
                        break
                    if pno == 1:
                        self.c["messages"] += 1
                    while self.retry_q and self.retry_q[0][0] <= time.monotonic() and not self.stop_flag:
                        if not await self._send(self.retry_q.popleft()[1]):
                            break
            # drain retries and wait for outstanding responses
            while not self.stop_flag and (self.retry_q or self._outstanding()):
                if self.retry_q and self.retry_q[0][0] <= time.monotonic():
                    if not await self._send(self.retry_q.popleft()[1]):
                        break
                else:
                    await asyncio.sleep(0.02)
            self.state = "stopped" if self.stop_flag else "completed"
        except Exception as e:
            self.state = "failed"
            self.error = "%s: %s" % (type(e).__name__, e)
            self.manager.log("error", "job %s failed: %s" % (self.id, self.error))
        finally:
            self.finished = time.time()
            self.flush()
            self.manager.log("info", "job %s %s: %d msgs, %d ok, %d err" % (
                self.id, self.state, self.c["messages"], self.c["ok"], self.c["err"] + self.c["timeout"]))
            # keep files open a while for late DLRs; manager closes them later
            self.manager.save_job_summary(self)

    def _outstanding(self):
        return self.c["submitted"] > self.c["ok"] + self.c["err"] + self.c["timeout"] + self.c["lost"] + \
            self.c["retried"]

    async def _send(self, item):
        """Wait for pause/rate/window then submit. Returns False if job stopped."""
        while True:
            if not self.paused.is_set():
                await self.paused.wait()
                self.limiter.reset_clock()
            if self.stop_flag:
                return False
            await self.limiter.acquire()
            await self.conn.limiter.acquire()   # account-wide cap shared by all jobs on this connection
            session = await self.conn.acquire(self)
            if session is None:
                return False
            if not self.paused.is_set():
                continue
            break
        tpl, tlvs = self.template, P.encode_tlvs(item.tlvs) if item.tlvs else b""
        try:
            session.submit(lambda seq: tpl.build(seq, item.dest, item.payload, item.dc, item.esm, tlvs), item)
        except Exception as e:
            self.c["submitted"] += 1
            self.c["lost"] += 1
            self.errors["send error: %s" % e] += 1
            return True
        item.submitted_at = time.time()
        self.c["submitted"] += 1
        return True

    # ---------------------------------------------------------- callbacks
    def on_resp(self, session, item, status, msg_id, latency):
        if status in RETRYABLE and item.attempt < int(self.spec.get("max_retries", 3)) \
                and not self.stop_flag:
            item.attempt += 1
            self.c["retried"] += 1
            self.errors[P.status_name(status) + " (retried)"] += 1
            # back off a little (grows with each attempt) before re-submitting
            delay = float(self.spec.get("retry_delay_ms", 250)) / 1000.0 * item.attempt
            self.retry_q.append((time.monotonic() + delay, item))
            return
        self.latency.append(latency)
        if status == 0:
            self.c["ok"] += 1
            if self.want_dlr and msg_id:
                self.manager.track_msg_id(msg_id, self, item.dest)
        else:
            self.c["err"] += 1
            self.errors[P.status_name(status)] += 1
        self._record(session, item, status, msg_id, latency)

    def on_timeout(self, session, item):
        self.c["timeout"] += 1
        self.errors["response timeout"] += 1
        self._record(session, item, -1, "", None)

    def on_lost(self, session, item, reason):
        self.c["lost"] += 1
        self.errors["no response (%s)" % reason] += 1
        self._record(session, item, -2, "", None)

    def _record(self, session, item, status, msg_id, latency):
        name = {-1: "TIMEOUT", -2: "CONN_LOST"}.get(status) or P.status_name(status)
        lat = round(latency, 1) if latency is not None else ""
        if self._files_open:
            self._sub_csv.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), item.idx, item.dest, item.part,
                                    item.nparts, session.index, item.attempt, status, name, msg_id, lat])
        self.recent.append({"t": now_ms(), "n": item.idx, "dest": item.dest, "part": "%d/%d" % (item.part, item.nparts),
                            "bind": session.index, "status": name, "ok": status == 0, "msg_id": msg_id,
                            "lat": lat, "dlr": None})
        if self.kind == "single" and (status != 0 or item.part == item.nparts):
            self.single_result = {"status": name, "message_id": msg_id, "latency_ms": lat}

    def on_dlr(self, msg_id, dest, stat, err, submit_ts, session, text):
        self.c["dlr"] += 1
        self.dlr_stats[stat] += 1
        lat = (time.time() - submit_ts) * 1000.0
        self.dlr_latency.append(lat)
        if self._files_open:
            self._dlr_csv.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), msg_id, dest, stat, err,
                                    round(lat, 1), session.index, text])
        for r in reversed(self.recent):
            if r["msg_id"] == msg_id:
                r["dlr"] = stat
                break

    # ---------------------------------------------------------- control
    def pause(self):
        if self.state == "running":
            self.paused.clear()
            self.state = "paused"

    def resume(self):
        if self.state == "paused":
            self.state = "running"
            self.paused.set()

    def stop(self):
        self.stop_flag = True
        self.paused.set()
        if self.state in ("running", "paused", "pending"):
            self.state = "stopping"

    def set_tps(self, tps):
        self.limiter.tps = float(tps)
        self.spec["tps"] = float(tps)

    def flush(self):
        if self._files_open:
            self._sub_fh.flush()
            self._dlr_fh.flush()

    def close_files(self):
        if self._files_open:
            self._files_open = False
            self._sub_fh.close()
            self._dlr_fh.close()

    def tick(self):
        sub, ok, dlr = self.c["submitted"], self.c["ok"] + self.c["err"], self.c["dlr"]
        ls, lo, ld = self._last_counts
        self._last_counts = (sub, ok, dlr)
        self.series.append((now_ms(), sub - ls, ok - lo, dlr - ld))

    def snapshot(self, detail=False):
        elapsed = ((self.finished or time.time()) - self.started) if self.started else 0
        d = {"id": self.id, "name": self.name, "kind": self.kind, "state": self.state, "error": self.error,
             "connection": self.conn.id, "connection_name": self.conn.profile["name"],
             "created": self.created, "started": self.started, "finished": self.finished,
             "elapsed": round(elapsed, 1), "planned": self.planned_messages,
             "tps_target": self.limiter.tps, "tps_effective": round(self.limiter.current_tps(), 1),
             "counts": dict(self.c), "errors": dict(self.errors), "dlr_stats": dict(self.dlr_stats),
             "avg_tps": round(self.c["submitted"] / elapsed, 1) if elapsed > 0 else 0,
             "single_result": self.single_result}
        if detail:
            d["latency"] = latency_summary(self.latency)
            d["dlr_latency"] = latency_summary(self.dlr_latency)
            d["recent"] = list(self.recent)[-150:]
            d["series"] = list(self.series)
            d["spec"] = {k: v for k, v in self.spec.items() if k != "numbers" and not k.startswith("_")}
        return d


# ====================================================================== connection
class Connection:
    def __init__(self, manager, cid, profile):
        self.manager = manager
        self.id = cid
        self.profile = dict(DEFAULT_PROFILE)
        self.profile.update(profile)
        self.sessions = []
        self.rr = 0
        self.trace = False
        self.capacity = asyncio.Event()
        self.limiter = RateLimiter(self.profile.get("max_tps", 0))
        self.c = collections.Counter()
        self.series = collections.deque(maxlen=SERIES_LEN)
        self._last = (0, 0, 0, 0)

    def log(self, level, msg):
        self.manager.log(level, "[%s] %s" % (self.profile["name"], msg))

    def _desired_layout(self):
        p = self.profile
        layout = [p["bind_type"]] * max(0, int(p["binds"]))
        layout += ["RX"] * max(0, int(p.get("rx_binds", 0) or 0))
        return layout

    async def bind(self):
        layout = self._desired_layout()
        if [s.bind_type for s in self.sessions] != layout:
            # keep sessions that still fit the layout, stop the rest
            keep, drop = [], []
            for i, bt in enumerate(layout):
                old = self.sessions[i] if i < len(self.sessions) else None
                if old is not None and old.bind_type == bt:
                    keep.append(old)
                else:
                    if old is not None:
                        drop.append(old)
                    keep.append(Session(self, i + 1, bt))
            drop += self.sessions[len(layout):]
            await asyncio.gather(*(s.stop() for s in drop), return_exceptions=True)
            self.sessions = keep
        stagger = max(0, int(self.profile.get("bind_stagger_ms", 100))) / 1000.0
        for s in self.sessions:
            if not s.want:
                s.start()
                if stagger:
                    await asyncio.sleep(stagger)

    async def unbind(self):
        await asyncio.gather(*(s.stop() for s in self.sessions), return_exceptions=True)

    async def bind_one(self, index):
        for s in self.sessions:
            if s.index == index:
                s.last_error = ""
                s.start()

    async def unbind_one(self, index):
        for s in self.sessions:
            if s.index == index:
                await s.stop()

    async def add_bind(self, bind_type=None):
        bt = bind_type or self.profile["bind_type"]
        s = Session(self, (max((x.index for x in self.sessions), default=0) + 1), bt)
        self.sessions.append(s)
        if bt == "RX":
            self.profile["rx_binds"] = int(self.profile.get("rx_binds", 0)) + 1
        else:
            self.profile["binds"] = int(self.profile["binds"]) + 1
        s.start()

    async def remove_bind(self, index):
        for s in list(self.sessions):
            if s.index == index:
                await s.stop()
                self.sessions.remove(s)
                key = "rx_binds" if s.bind_type == "RX" and self.profile["bind_type"] != "RX" else "binds"
                self.profile[key] = max(0, int(self.profile.get(key, 0)) - 1)

    def bound_count(self):
        return sum(1 for s in self.sessions if s.state == "bound")

    def _pick(self):
        n = len(self.sessions)
        for i in range(n):
            s = self.sessions[(self.rr + i) % n]
            if s.can_submit():
                self.rr = (self.rr + i + 1) % n
                return s
        return None

    async def acquire(self, job):
        """Wait until a bound TX/TRX session has free window."""
        warned = False
        waited = 0.0
        while True:
            if job.stop_flag:
                return None
            if self.sessions:
                s = self._pick()
                if s is not None:
                    return s
            self.capacity.clear()
            try:
                await asyncio.wait_for(self.capacity.wait(), 0.5)
            except asyncio.TimeoutError:
                waited += 0.5
            if not warned and waited >= 5 and not any(s.state == "bound" and s.bind_type != "RX"
                                                      for s in self.sessions):
                warned = True
                self.log("warn", "job %s waiting: no bound transmitter sessions" % job.id)

    def capacity_changed(self):
        self.capacity.set()

    def on_session_state(self, s):
        if s.state == "bound":
            self.capacity.set()
        self.manager.dirty = True

    # session -> connection callbacks
    def on_submit_resp(self, session, item, status, msg_id, latency):
        self.capacity.set()
        if status == 0:
            self.c["ok"] += 1
        else:
            self.c["err"] += 1
        item.job.on_resp(session, item, status, msg_id, latency)

    def on_submit_timeout(self, session, item):
        self.c["timeout"] += 1
        item.job.on_timeout(session, item)

    def on_submit_lost(self, session, item, reason):
        item.job.on_lost(session, item, reason)

    def on_deliver(self, session, d):
        self.manager.on_deliver(self, session, d)

    def add_trace(self, session, direction, cmd, status, seq, raw):
        self.manager.trace_buf.append({
            "t": now_ms(), "conn": self.profile["name"], "bind": session.index, "dir": direction,
            "cmd": P.COMMAND_NAMES.get(cmd, "0x%08X" % cmd), "status": P.status_name(status) if
            cmd & 0x80000000 else "", "seq": seq, "len": len(raw), "hex": P.hexdump(raw)})

    def tick(self):
        sub = sum(s.stats["submit"] for s in self.sessions)
        resp = self.c["ok"] + self.c["err"]
        dlr = self.c["dlr"]
        mo = self.c["mo"]
        ls, lr, ld, lm = self._last
        self._last = (sub, resp, dlr, mo)
        self.series.append((now_ms(), max(0, sub - ls), resp - lr, dlr - ld, mo - lm))

    def snapshot(self):
        p = {k: v for k, v in self.profile.items() if k != "password"}
        p["has_password"] = bool(self.profile.get("password"))
        totals = collections.Counter()
        for s in self.sessions:
            totals.update(s.stats)
        last = self.series[-1] if self.series else (0, 0, 0, 0, 0)
        return {"id": self.id, "profile": p, "trace": self.trace,
                "sessions": [s.snapshot() for s in self.sessions],
                "bound": self.bound_count(), "total": len(self.sessions),
                "inflight": sum(s.inflight for s in self.sessions),
                "totals": dict(totals), "counts": dict(self.c),
                "rate": {"submit": last[1], "resp": last[2], "dlr": last[3], "mo": last[4]}}


# ====================================================================== manager
class Manager:
    def __init__(self):
        os.makedirs(os.path.join(DATA_DIR, "uploads"), exist_ok=True)
        os.makedirs(os.path.join(DATA_DIR, "jobs"), exist_ok=True)
        self.connections = {}
        self.jobs = collections.OrderedDict()
        self.logs = collections.deque(maxlen=2000)
        self.log_seq = 0
        self.inbound = collections.deque(maxlen=1000)
        self.inbound_seq = 0
        self.trace_buf = collections.deque(maxlen=3000)
        self.msgids = {}               # canonical message_id -> (job, dest, submit_ts, message_id)
        self.mo_parts = {}             # (conn, src, dst, ref) -> partial concatenated MO
        self.msgid_cap = 3_000_000
        self.unmatched_dlr = 0
        self.dirty = False
        self.templates = []
        self._load()

    # ---------------------------------------------------------- persistence
    def _path(self, name):
        return os.path.join(DATA_DIR, name)

    def _load(self):
        try:
            with open(self._path("connections.json")) as fh:
                for c in json.load(fh):
                    cid = c.pop("id", None) or uuid.uuid4().hex[:8]
                    self.connections[cid] = Connection(self, cid, c)
        except FileNotFoundError:
            pass
        except Exception as e:
            self.log("error", "could not load connections.json: %s" % e)
        try:
            with open(self._path("templates.json")) as fh:
                self.templates = json.load(fh)
        except FileNotFoundError:
            pass
        except Exception as e:
            self.log("error", "could not load templates.json: %s" % e)
        try:
            history = []
            for d in sorted(os.listdir(self._path("jobs")))[-200:]:
                f = os.path.join(self._path("jobs"), d, "summary.json")
                if os.path.exists(f):
                    with open(f) as fh:
                        history.append(json.load(fh))
            self.history = history
        except Exception:
            self.history = []

    def save_connections(self):
        data = [dict(c.profile, id=c.id) for c in self.connections.values()]
        tmp = self._path("connections.json.tmp")
        with open(tmp, "w") as fh:
            json.dump(data, fh, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self._path("connections.json"))

    def save_templates(self):
        with open(self._path("templates.json"), "w") as fh:
            json.dump(self.templates, fh, indent=2)

    def save_job_summary(self, job):
        snap = job.snapshot(detail=True)
        snap.pop("recent", None)
        snap.pop("series", None)
        try:
            with open(os.path.join(job.dir, "summary.json"), "w") as fh:
                json.dump(snap, fh, indent=2, default=str)
        except Exception as e:
            self.log("error", "could not save job summary: %s" % e)

    def upload_path(self, upload_id):
        if not re.fullmatch(r"[0-9a-f]{32}", upload_id or ""):
            raise ValueError("invalid upload id")
        return os.path.join(DATA_DIR, "uploads", upload_id + ".csv")

    # ---------------------------------------------------------- logging
    def log(self, level, msg):
        self.log_seq += 1
        self.logs.append({"id": self.log_seq, "t": now_ms(), "level": level, "msg": msg})
        if LOG_STDOUT:
            print("%s [%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), level, msg), file=sys.stdout, flush=True)

    # ---------------------------------------------------------- connections
    def upsert_connection(self, data):
        cid = data.pop("id", None)
        clean = {}
        for k, default in DEFAULT_PROFILE.items():
            if k in data:
                v = data[k]
                if isinstance(default, bool):
                    v = bool(v)
                elif isinstance(default, int):
                    v = int(v)
                clean[k] = v
        if clean.get("bind_type", "TRX") not in ("TRX", "TX", "RX"):
            raise ValueError("bind_type must be TRX, TX or RX")
        if cid and cid in self.connections:
            conn = self.connections[cid]
            if "password" in data and data["password"] == "" and conn.profile.get("password"):
                clean.pop("password")   # empty password field = keep stored password
            conn.profile.update(clean)
            conn.limiter.tps = float(conn.profile.get("max_tps", 0) or 0)
        else:
            cid = uuid.uuid4().hex[:8]
            conn = Connection(self, cid, clean)
            self.connections[cid] = conn
        self.save_connections()
        return conn

    async def delete_connection(self, cid):
        conn = self.connections.pop(cid, None)
        if conn:
            for j in self.jobs.values():
                if j.conn is conn:
                    j.stop()
            await conn.unbind()
            self.save_connections()

    def get_conn(self, cid):
        if cid not in self.connections:
            raise KeyError("unknown connection %s" % cid)
        return self.connections[cid]

    # ---------------------------------------------------------- jobs
    def static_tlvs(self, spec):
        cache = spec.get("_tlv_cache")
        if cache is not None:
            return cache
        tlvs = []
        # legacy REST shortcut for the India DLT tags; the GUI sends these as custom_tlvs
        nul = b"\x00" if spec.get("dlt_null_terminate") else b""
        for key, tag in (("dlt_entity_id", 0x1400), ("dlt_template_id", 0x1401), ("dlt_tm_id", 0x1402)):
            v = (spec.get(key) or "").strip()
            if v:
                tlvs.append((tag, v.encode() + nul))
        for t in spec.get("custom_tlvs") or []:
            tag = int(str(t.get("tag", "0")), 0)
            if not 0 < tag <= 0xFFFF:
                raise ValueError("TLV tag %s out of range" % t.get("tag"))
            fmt = t.get("format", "string")
            val = t.get("value", "")
            if fmt == "hex":
                raw = bytes.fromhex(val.replace(" ", ""))
            elif fmt == "int8":
                raw = int(val, 0).to_bytes(1, "big")
            elif fmt == "int16":
                raw = int(val, 0).to_bytes(2, "big")
            elif fmt == "int32":
                raw = int(val, 0).to_bytes(4, "big")
            elif fmt == "cstring":
                raw = val.encode() + b"\x00"
            else:
                raw = val.encode()
            if len(raw) > 0xFFFF:
                raise ValueError("TLV 0x%04X value too long" % tag)
            tlvs.append((tag, raw))
        spec["_tlv_cache"] = tlvs
        return tlvs

    def create_job(self, spec):
        conn = self.get_conn(spec.get("connection"))
        if not spec.get("message") and spec.get("source") != "csv":
            raise ValueError("message text is empty")
        if spec.get("source") == "csv":
            self.upload_path(spec.get("csv_id", ""))
        if spec.get("source") == "generate":
            NumberGenerator(spec.get("gen_count", 10), spec.get("gen_country_code", ""),
                            spec.get("gen_prefixes", ""), spec.get("gen_length", 10),
                            spec.get("gen_mode", "random"), spec.get("gen_start", 0),
                            spec.get("gen_unique", True))
        if spec.get("source") == "list":
            nums, invalid = parse_number_list(spec.get("numbers", ""))
            if not nums:
                raise ValueError("no valid destination numbers")
        if conn.bound_count() == 0:
            self.log("warn", "job created while connection %s has no bound sessions; it will wait"
                     % conn.profile["name"])
        job = Job(self, conn, spec)
        self.jobs[job.id] = job
        job.start()
        self._trim_jobs()
        return job

    def _trim_jobs(self):
        finished = [j for j in self.jobs.values() if j.state in ("completed", "stopped", "failed")]
        while len(self.jobs) > 50 and finished:
            j = finished.pop(0)
            j.close_files()
            del self.jobs[j.id]

    # ---------------------------------------------------------- DLR / MO
    def track_msg_id(self, msg_id, job, dest):
        if len(self.msgids) >= self.msgid_cap:
            for _ in range(10000):
                self.msgids.pop(next(iter(self.msgids)))
        self.msgids[_canon_msgid(msg_id)] = (job, dest, time.time(), msg_id)

    def _lookup(self, msg_id):
        key = _canon_msgid(msg_id)
        hit = self.msgids.pop(key, None)
        if hit:
            return hit
        # some SMSCs report hex ids in the response and decimal in the DLR (or vice versa)
        alts = []
        try:
            alts.append(_canon_msgid(format(int(key), "x")))
        except ValueError:
            pass
        try:
            alts.append(_canon_msgid(str(int(key, 16))))
        except ValueError:
            pass
        for k in alts:
            hit = self.msgids.pop(k, None)
            if hit:
                return hit
        return None

    def on_deliver(self, conn, session, d):
        if "decode_error" in d:
            self.log("warn", "could not decode deliver_sm: %s" % d["decode_error"])
            return
        tlvs = d.get("tlvs", {})
        esm = d.get("esm_class", 0)
        sm = d.get("short_message", b"")
        concat = None
        if esm & 0x40:
            concat, sm = split_udh(sm)
        elif tlvs.get(P.TLV_SAR_MSG_REF_NUM) and tlvs.get(P.TLV_SAR_TOTAL_SEGMENTS) and \
                tlvs.get(P.TLV_SAR_SEGMENT_SEQNUM):
            concat = (int.from_bytes(tlvs[P.TLV_SAR_MSG_REF_NUM], "big"), tlvs[P.TLV_SAR_TOTAL_SEGMENTS][0],
                      tlvs[P.TLV_SAR_SEGMENT_SEQNUM][0])
        text = decode_text(sm, d.get("data_coding", 0))
        is_dlr = (esm & 0x3C) in DLR_ESM_TYPES or P.TLV_RECEIPTED_MESSAGE_ID in tlvs
        m = DLR_RE.search(text) if (is_dlr or "stat:" in text) else None
        rec = {"t": now_ms(), "conn": conn.profile["name"], "bind": session.index,
               "src": d.get("source_addr"), "dst": d.get("destination_addr"), "esm": esm,
               "dc": d.get("data_coding", 0), "text": text}
        if is_dlr or m:
            conn.c["dlr"] += 1
            session.stats["dlr"] += 1
            msg_id = None
            stat = err = ""
            if P.TLV_RECEIPTED_MESSAGE_ID in tlvs:
                msg_id = tlvs[P.TLV_RECEIPTED_MESSAGE_ID].rstrip(b"\x00").decode("latin-1")
            if P.TLV_MESSAGE_STATE in tlvs and tlvs[P.TLV_MESSAGE_STATE]:
                stat = P.MESSAGE_STATES.get(tlvs[P.TLV_MESSAGE_STATE][0], str(tlvs[P.TLV_MESSAGE_STATE][0]))
            if m:
                msg_id = msg_id or m.group("id")
                stat = m.group("stat").upper() or stat
                err = m.group("err") or ""
            nec = tlvs.get(P.TLV_NETWORK_ERROR_CODE)
            if not err and nec and len(nec) == 3:
                err = str(int.from_bytes(nec[1:3], "big"))
            rec.update(kind="DLR", msg_id=msg_id, stat=stat, err=err)
            if msg_id:
                hit = self._lookup(msg_id)
                if hit:
                    job, dest, ts, key = hit
                    rec["job"] = job.id
                    job.on_dlr(key, dest, stat, err, ts, session, text)
                else:
                    self.unmatched_dlr += 1
        else:
            conn.c["mo"] += 1
            session.stats["mo"] += 1
            rec["kind"] = "MO"
            if concat and concat[1] > 1:
                key = (conn.id, rec["src"], rec["dst"], concat[0])
                buf = self.mo_parts.setdefault(key, {"t": time.time(), "total": concat[1], "parts": {}, "rec": rec})
                buf["parts"][concat[2]] = text
                if len(buf["parts"]) < buf["total"]:
                    return
                del self.mo_parts[key]
                rec["text"] = "".join(buf["parts"][i] for i in sorted(buf["parts"]))
                rec["parts"] = "%d" % buf["total"]
        self._add_inbound(rec)

    def _add_inbound(self, rec):
        self.inbound_seq += 1
        rec["id"] = self.inbound_seq
        self.inbound.append(rec)

    def _expire_mo_parts(self):
        cutoff = time.time() - MO_PART_TTL
        for key in [k for k, b in self.mo_parts.items() if b["t"] < cutoff]:
            buf = self.mo_parts.pop(key)
            rec = buf["rec"]
            rec["text"] = "".join(buf["parts"][i] for i in sorted(buf["parts"]))
            rec["parts"] = "%d/%d incomplete" % (len(buf["parts"]), buf["total"])
            self._add_inbound(rec)
            self.log("warn", "concatenated MO from %s: only %s parts arrived" % (rec["src"], rec["parts"]))

    # ---------------------------------------------------------- periodic
    async def ticker(self):
        n = 0
        while True:
            await asyncio.sleep(1)
            n += 1
            for c in self.connections.values():
                c.tick()
            for j in list(self.jobs.values()):
                if j.state in ("running", "paused", "stopping") or (j.finished and time.time() - j.finished < 5):
                    j.tick()
                if n % 5 == 0:
                    j.flush()
                # close result files 10 min after completion (late DLRs still counted in memory)
                if j.finished and time.time() - j.finished > 600 and j._files_open:
                    j.close_files()
            if n % 60 == 0:
                # drop DLR tracking for msg ids older than 24h
                cutoff = time.time() - 86400
                while self.msgids:
                    k = next(iter(self.msgids))
                    if self.msgids[k][2] >= cutoff:
                        break
                    self.msgids.pop(k)
                self._expire_mo_parts()

    def snapshot(self):
        return {"t": now_ms(),
                "connections": [c.snapshot() for c in self.connections.values()],
                "jobs": [j.snapshot() for j in reversed(self.jobs.values())],
                "tracked_msgids": len(self.msgids), "unmatched_dlr": self.unmatched_dlr}

    async def shutdown(self):
        for j in self.jobs.values():
            j.stop()
        await asyncio.gather(*(c.unbind() for c in self.connections.values()), return_exceptions=True)
        for j in self.jobs.values():
            j.flush()
            j.close_files()
