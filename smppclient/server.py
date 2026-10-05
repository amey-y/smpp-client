"""Web GUI + REST/WebSocket API."""
import argparse
import asyncio
import base64
import hmac
import json
import os
import uuid

from aiohttp import web, WSMsgType

from .encoding import message_info
from .engine import DATA_DIR, DEFAULT_PROFILE, Manager, read_csv_recipients
from .mock_smsc import DEFAULTS as MOCK_DEFAULTS, MockSMSC

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")
MAX_UPLOAD = 512 * 1024 * 1024


def jresp(data, status=200):
    return web.json_response(data, status=status, dumps=lambda o: json.dumps(o, default=str))


def api(handler):
    async def wrapped(request):
        try:
            return await handler(request)
        except web.HTTPException:
            raise
        except (KeyError, ValueError, TypeError) as e:
            return jresp({"error": str(e).strip("'\"")}, 400)
        except Exception as e:
            request.app["mgr"].log("error", "API %s failed: %s: %s" % (request.path, type(e).__name__, e))
            return jresp({"error": "%s: %s" % (type(e).__name__, e)}, 500)
    return wrapped


def make_auth_middleware(user, password):
    expected = "Basic " + base64.b64encode(("%s:%s" % (user, password)).encode()).decode()

    @web.middleware
    async def auth(request, handler):
        if not hmac.compare_digest(request.headers.get("Authorization", ""), expected):
            return web.Response(status=401, headers={"WWW-Authenticate": 'Basic realm="SMPP Client"'})
        return await handler(request)
    return auth


routes = web.RouteTableDef()


@routes.get("/")
async def index(request):
    return web.FileResponse(os.path.join(STATIC_DIR, "index.html"), headers={"Cache-Control": "no-cache"})


@routes.get("/api/state")
@api
async def state(request):
    mgr = request.app["mgr"]
    return jresp(dict(mgr.snapshot(), defaults=DEFAULT_PROFILE, templates=mgr.templates,
                      mock=request.app["rt"]["mock"].snapshot()))


# ------------------------------------------------------------------ connections
@routes.post("/api/connections")
@api
async def save_connection(request):
    mgr = request.app["mgr"]
    conn = mgr.upsert_connection(await request.json())
    mgr.log("info", "connection profile '%s' saved" % conn.profile["name"])
    return jresp(conn.snapshot())


@routes.delete("/api/connections/{cid}")
@api
async def delete_connection(request):
    await request.app["mgr"].delete_connection(request.match_info["cid"])
    return jresp({"ok": True})


@routes.post("/api/connections/{cid}/{action}")
@api
async def connection_action(request):
    mgr = request.app["mgr"]
    conn = mgr.get_conn(request.match_info["cid"])
    action = request.match_info["action"]
    body = await request.json() if request.can_read_body else {}
    if action == "bind":
        asyncio.create_task(conn.bind())
    elif action == "unbind":
        await conn.unbind()
    elif action == "rebind":
        await conn.unbind()
        asyncio.create_task(conn.bind())
    elif action == "bind_one":
        await conn.bind_one(int(body["index"]))
    elif action == "unbind_one":
        await conn.unbind_one(int(body["index"]))
    elif action == "add_bind":
        await conn.add_bind(body.get("bind_type"))
        mgr.save_connections()
    elif action == "remove_bind":
        await conn.remove_bind(int(body["index"]))
        mgr.save_connections()
    elif action == "trace":
        conn.trace = bool(body.get("on"))
    elif action == "reset_stats":
        conn.c.clear()
        for s in conn.sessions:
            for k in s.stats:
                s.stats[k] = 0
        conn._last = (0, 0, 0, 0)
    else:
        raise ValueError("unknown action %s" % action)
    return jresp({"ok": True})


# ------------------------------------------------------------------ jobs
@routes.post("/api/jobs")
@api
async def create_job(request):
    spec = await request.json()
    job = request.app["mgr"].create_job(spec)
    return jresp(job.snapshot())


@routes.get("/api/jobs/{jid}")
@api
async def get_job(request):
    mgr = request.app["mgr"]
    jid = request.match_info["jid"]
    if jid in mgr.jobs:
        return jresp(mgr.jobs[jid].snapshot(detail=True))
    f = os.path.join(DATA_DIR, "jobs", os.path.basename(jid), "summary.json")
    if os.path.exists(f):
        with open(f) as fh:
            return jresp(json.load(fh))
    raise web.HTTPNotFound()


@routes.post("/api/jobs/{jid}/{action}")
@api
async def job_action(request):
    mgr = request.app["mgr"]
    job = mgr.jobs.get(request.match_info["jid"])
    if job is None:
        raise web.HTTPNotFound()
    action = request.match_info["action"]
    body = await request.json() if request.can_read_body else {}
    if action == "pause":
        job.pause()
    elif action == "resume":
        job.resume()
    elif action == "stop":
        job.stop()
    elif action == "tps":
        job.set_tps(float(body["tps"]))
        mgr.log("info", "job %s TPS set to %s" % (job.id, body["tps"]))
    else:
        raise ValueError("unknown action %s" % action)
    return jresp(job.snapshot())


@routes.get("/api/jobs/{jid}/{kind:submits|dlrs}.csv")
async def job_csv(request):
    mgr = request.app["mgr"]
    jid = os.path.basename(request.match_info["jid"])
    if jid in mgr.jobs:
        mgr.jobs[jid].flush()
    path = os.path.join(DATA_DIR, "jobs", jid, request.match_info["kind"] + ".csv")
    if not os.path.exists(path):
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={
        "Content-Disposition": 'attachment; filename="%s-%s.csv"' % (jid, request.match_info["kind"])})


@routes.get("/api/history")
@api
async def history(request):
    out = []
    jobs_dir = os.path.join(DATA_DIR, "jobs")
    for d in sorted(os.listdir(jobs_dir), reverse=True)[:300]:
        f = os.path.join(jobs_dir, d, "summary.json")
        if os.path.exists(f):
            try:
                with open(f) as fh:
                    s = json.load(fh)
                out.append({k: s.get(k) for k in ("id", "name", "kind", "state", "connection_name", "started",
                                                   "finished", "elapsed", "counts", "avg_tps", "dlr_stats",
                                                   "latency")})
            except Exception:
                pass
    return jresp(out)


# ------------------------------------------------------------------ uploads / helpers
@routes.post("/api/upload")
@api
async def upload(request):
    reader = await request.multipart()
    field = await reader.next()
    if field is None or field.name != "file":
        raise ValueError("expected a 'file' field")
    upload_id = uuid.uuid4().hex
    path = os.path.join(DATA_DIR, "uploads", upload_id + ".csv")
    size = 0
    with open(path, "wb") as fh:
        while True:
            chunk = await field.read_chunk(1 << 16)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_UPLOAD:
                fh.close()
                os.remove(path)
                raise ValueError("file too large")
            fh.write(chunk)
    rows, skipped = await asyncio.get_running_loop().run_in_executor(
        None, read_csv_recipients, path, 0, 1, False)
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
        preview = [next(fh, "").rstrip("\r\n") for _ in range(5)]
    return jresp({"id": upload_id, "filename": field.filename, "bytes": size, "rows": len(rows),
                  "skipped": skipped, "unique": len({r[0] for r in rows}),
                  "has_message_col": any(r[1] for r in rows[:100]),
                  "preview": [p for p in preview if p]})


@routes.post("/api/msginfo")
@api
async def msginfo(request):
    body = await request.json()
    return jresp(message_info(body.get("text", ""), body.get("encoding", "auto")))


@routes.post("/api/templates")
@api
async def save_templates(request):
    mgr = request.app["mgr"]
    body = await request.json()
    if not isinstance(body, list):
        raise ValueError("expected a list")
    mgr.templates = body
    mgr.save_templates()
    return jresp({"ok": True})


@routes.get("/api/inbound")
@api
async def inbound(request):
    return jresp(list(request.app["mgr"].inbound))


@routes.post("/api/inbound/clear")
@api
async def inbound_clear(request):
    request.app["mgr"].inbound.clear()
    return jresp({"ok": True})


@routes.get("/api/trace")
@api
async def trace(request):
    return jresp(list(request.app["mgr"].trace_buf))


@routes.post("/api/trace/clear")
@api
async def trace_clear(request):
    request.app["mgr"].trace_buf.clear()
    return jresp({"ok": True})


# ------------------------------------------------------------------ mock SMSC
@routes.post("/api/mock/{action:start|stop}")
@api
async def mock_action(request):
    app = request.app
    mock = app["rt"]["mock"]
    if request.match_info["action"] == "start":
        body = await request.json() if request.can_read_body else {}
        if mock.running:
            await mock.stop()
        cfg = dict(MOCK_DEFAULTS)
        for k, v in body.items():
            if k in cfg:
                cfg[k] = type(cfg[k])(v) if not isinstance(cfg[k], bool) else bool(v)
        app["rt"]["mock"] = mock = MockSMSC(log=app["mgr"].log, **cfg)
        await mock.start()
    else:
        await mock.stop()
    return jresp(mock.snapshot())


# ------------------------------------------------------------------ websocket
@routes.get("/ws")
async def ws_handler(request):
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    mgr = request.app["mgr"]
    sub = {"job": None, "trace": False, "log_id": 0, "inbound_id": 0, "trace_t": 0}

    async def reader():
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    sub.update(json.loads(msg.data))
                except Exception:
                    pass

    rtask = asyncio.create_task(reader())
    try:
        while not ws.closed:
            snap = mgr.snapshot()
            snap["logs"] = [l for l in mgr.logs if l["id"] > sub["log_id"]][-300:]
            if snap["logs"]:
                sub["log_id"] = snap["logs"][-1]["id"]
            snap["inbound"] = [r for r in mgr.inbound if r["id"] > sub["inbound_id"]][-300:]
            if snap["inbound"]:
                sub["inbound_id"] = snap["inbound"][-1]["id"]
            snap["mock"] = request.app["rt"]["mock"].snapshot()
            snap["series"] = {c.id: list(c.series) for c in mgr.connections.values()}
            if sub.get("job") and sub["job"] in mgr.jobs:
                snap["job_detail"] = mgr.jobs[sub["job"]].snapshot(detail=True)
            if sub.get("trace"):
                tr = [t for t in mgr.trace_buf if t["t"] > sub["trace_t"]][-500:]
                if tr:
                    sub["trace_t"] = tr[-1]["t"]
                snap["trace"] = tr
            await ws.send_str(json.dumps(snap, default=str))
            await asyncio.sleep(1)
    except (ConnectionResetError, RuntimeError):
        pass
    finally:
        rtask.cancel()
    return ws


# ------------------------------------------------------------------ app
def build_app(user=None, password=None):
    middlewares = [make_auth_middleware(user, password)] if user and password else []
    app = web.Application(middlewares=middlewares, client_max_size=MAX_UPLOAD + 1024 * 1024)
    app.add_routes(routes)
    app.router.add_static("/static/", STATIC_DIR)

    async def on_startup(app):
        app["mgr"] = Manager()
        app["rt"] = {"mock": MockSMSC(log=app["mgr"].log)}
        app["ticker"] = asyncio.create_task(app["mgr"].ticker())
        app["mgr"].log("info", "SMPP client started")

    async def on_cleanup(app):
        app["ticker"].cancel()
        await app["rt"]["mock"].stop()
        await app["mgr"].shutdown()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def main():
    ap = argparse.ArgumentParser(description="SMPP client / load tester with web GUI")
    ap.add_argument("--host", default=os.environ.get("SMPPCLIENT_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("SMPPCLIENT_PORT", "8095")))
    ap.add_argument("--user", default=os.environ.get("SMPPCLIENT_USER"))
    ap.add_argument("--password", default=os.environ.get("SMPPCLIENT_PASSWORD"))
    a = ap.parse_args()
    try:
        import uvloop
        uvloop.install()
    except ImportError:
        pass
    if not (a.user and a.password):
        print("WARNING: no --user/--password set; the GUI is open to anyone who can reach %s:%d" % (a.host, a.port))
    web.run_app(build_app(a.user, a.password), host=a.host, port=a.port, access_log=None,
                print=lambda s: print(s.replace("0.0.0.0", "<server-ip>")))


if __name__ == "__main__":
    main()
