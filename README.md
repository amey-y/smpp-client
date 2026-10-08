# SMPP Client / Load Tester

A web-GUI SMPP 3.4 client for functional and load testing of any SMSC or SMPP gateway.
Python 3.12 + asyncio, with its own SMPP codec (no third-party SMPP library), served by aiohttp.
It ships with a built-in mock SMSC, so you can try it without a real server.

## Start

### Docker

```bash
docker build -t smpp-client .
docker run -d --name smpp-client -p 8095:8095 -v smpp-data:/data \
  -e SMPPCLIENT_USER=admin -e SMPPCLIENT_PASSWORD=secret smpp-client
# GUI on http://<host>:8095
```

Or with Compose: `docker compose up -d` (set `SMPPCLIENT_USER` / `SMPPCLIENT_PASSWORD` in `.env` or the shell).

- Data (connection profiles, templates, job results) is stored in the `/data` volume.
  The container runs as UID 10001. If you bind-mount a host directory instead of a named volume, make it writable by that UID.
- Port `2775` is exposed for the built-in mock SMSC. Publish it (`-p 2775:2775`) only if you want to reach the mock from outside the container.
- Standalone mock SMSC: `docker run --rm -p 2775:2775 --no-healthcheck smpp-client mock --port 2775 --max-binds 10`
  (or `docker compose --profile mock up`).
- The image has a healthcheck on `GET /healthz`.

Push to Docker Hub (multi-arch):

```bash
docker login
docker buildx build --platform linux/amd64,linux/arm64 -t <dockerhub-user>/smpp-client:latest --push .
```

### Without Docker

```bash
./run.sh                                               # GUI on http://<server-ip>:8095
./run.sh --port 9000 --user admin --password secret    # with HTTP basic auth (recommended)
./run.sh mock --port 2775 --max-binds 10               # standalone mock SMSC
```

`run.sh` creates `.venv` and installs `aiohttp` and `uvloop` on first run. You can also run `python -m smppclient` directly.

To run it as a systemd service, copy the project to `/opt/smpp-client`, adjust `User=` in `smpp-client.service`, copy the file to `/etc/systemd/system/`, then run `systemctl enable --now smpp-client`.

### Configuration

| Option | Env var | Default |
|---|---|---|
| `--host` | `SMPPCLIENT_HOST` | `0.0.0.0` |
| `--port` | `SMPPCLIENT_PORT` | `8095` |
| `--user` / `--password` | `SMPPCLIENT_USER` / `SMPPCLIENT_PASSWORD` | unset (no auth) |
| | `SMPPCLIENT_DATA` | `./data` (`/data` in Docker) |
| | `SMPPCLIENT_LOG_STDOUT` | `1`: the event log is also written to stdout (`docker logs`) |

## Features

**Connections & binds**
- Multiple connection profiles (host/port/system_id/password/system_type/TON/NPI/address_range/interface version), saved to `data/connections.json` (mode 600).
- N binds per profile (TRX, TX or RX), plus optional extra RX binds for providers that send DLRs on a separate receiver. Binds are staggered (configurable delay) so they don't all hit the server at once.
- **Max TPS per connection**: an account-wide cap shared by all jobs on that connection, for providers that enforce a per-account limit.
- Each bind is kept alive on its own. It sends periodic `enquire_link`, answers the server's `enquire_link`, detects a missing `enquire_link_resp`, handles server `unbind`, sets TCP keepalive, and auto-reconnects with exponential backoff (1 s → 30 s). It stops retrying on an invalid password or system_id.
- Per-bind window (max unacknowledged submit_sm) and response timeout.
- Per-bind live table: state, uptime, in-flight/window, submitted/OK/error/timeout/DLR/MO, enquire_link RTT, reconnect count, last error. The server's system_id and `sc_interface_version` are shown once bound. You can bind, unbind, add or remove individual binds at runtime.
- Optional TLS.

**Sending**
- **Single SMS**: shows submit_sm_resp status, message_id, latency and the matched DLR.
- **Bulk list**: paste numbers one per line (commas and semicolons also work). `+`, `00`, spaces and dashes are stripped. Optional dedupe, and a live valid/duplicate/invalid count.
- **Generate numbers**: count, country code, number length, prefixes, random or sequential, unique.
- **CSV upload**: one number per line, or columns with an optional per-row message column. Header and invalid rows are skipped. Large files are streamed to disk.
- **Placeholders** in the text: `{number} {seq} {seq:6} {otp} {rand:4} {uuid:8} {time} {date} {job}`.
- **Encodings**: Auto (GSM7 or UCS2), GSM 03.38 unpacked or packed, IA5, Latin-1 (DC 3), Latin-1 as SMSC default alphabet (DC 0, for servers that treat DC 0 as ISO-8859-1), UCS2, binary (hex), plus a raw `data_coding` override. Live character/segment counter.
- **Long messages**: UDH concatenation with 8-bit or 16-bit reference, SAR TLVs, `message_payload`, or truncate.
- **Optional TLVs**: any tag with string/cstring/hex/int8/16/32 values. Presets cover common parameters (user_message_reference, ports, language_indicator, callback_num, ussd_service_op, …) and regional ones such as India DLT (`0x1400`/`0x1401`/`0x1402`). Vendor-specific tags vary between SMSCs, so check your provider's spec.
- **Advanced submit_sm fields**: source/dest TON & NPI, service_type, esm_class, protocol_id, priority, validity_period, schedule_delivery_time, data_coding override, registered_delivery.
- System_id and password longer than the SMPP 3.4 limits (15/8) are sent unchanged instead of being silently truncated.
- **Saved message templates.** The form also remembers your last values.

**Load control**
- **TPS**: a token bucket per job, in submit_sm PDUs per second, spread round-robin over all bound TX/TRX binds and bounded by each bind's window and the connection's max TPS. `0` means unlimited.
- **Live control**: change TPS while a job runs, ramp up gradually, pause/resume/stop.
- **Repeat / duration / cap**: repeat the recipient list N times, run for a fixed duration (soak test), or cap the total message count.
- **Retries**: automatic retry with backoff on `ESME_RTHROTTLED` / `ESME_RMSGQFUL`.

**Receiving**
- **DLR matching**: by `receipted_message_id` TLV or the `id:` field in the receipt text. Case and leading-zero differences are ignored, and hex↔decimal IDs are also tried. `stat`/`err` come from the receipt text, or from the `message_state` / `network_error_code` TLVs.
- **MO messages**: concatenated MO (UDH 8/16-bit or SAR TLVs) is reassembled. UDH bytes are stripped from the displayed text.
- Spec-compliant handling of `alert_notification` (no response) and unknown server requests (`generic_nack`).

**Monitoring & results**
- **Dashboard**: submit / response / DLR / MO per second (5-minute chart), bound binds, in-flight, totals.
- **Job detail**: progress and ETA, latency min/avg/p50/p95/p99/max, error breakdown by command_status, DLR status breakdown, DLR latency, latest messages.
- **CSV downloads per job**: `submits.csv` (every PDU with status, message_id and latency) and `dlrs.csv`. History of past jobs is kept under `data/jobs/`.
- **DLR / MO viewer** with filters.
- **PDU trace** with hex dumps, enabled per connection.
- **Event log** (also on stdout).
- **Prometheus metrics** at `GET /metrics`: binds, in-flight, submit/response/timeout/DLR/MO counters per connection, active jobs, unmatched DLRs.

**Mock SMSC**
- A built-in simulator for testing without a real SMSC. It enforces max binds per system_id and a per-bind TPS limit (`ESME_RTHROTTLED`), and supports a reject rate, response delay, and DLR delay / UNDELIV rate. DLRs go back on the submitting TRX bind, or on an RX bind of the same system_id.
- Start it from the GUI, or standalone: `./run.sh mock --port 2775 --max-binds 10 --tps-per-bind 100`.

## HTTP API

The GUI uses a JSON API that you can also script against (with basic auth if configured):

```bash
# create a connection profile and bind it
curl -u admin:secret -X POST localhost:8095/api/connections -H 'Content-Type: application/json' \
  -d '{"name":"provider","host":"smpp.example.com","port":2775,"system_id":"user","password":"pass","binds":2}'
curl -u admin:secret -X POST localhost:8095/api/connections/<id>/bind

# start a job
curl -u admin:secret -X POST localhost:8095/api/jobs -H 'Content-Type: application/json' \
  -d '{"connection":"<id>","source":"list","numbers":"15551234567","message":"Hello {seq}","source_addr":"TEST","tps":10}'
curl -u admin:secret localhost:8095/api/jobs/<job-id>
```

Other endpoints: `GET /api/state`, `POST /api/jobs/<id>/{pause,resume,stop,tps}`, `GET /api/jobs/<id>/{submits,dlrs}.csv`, `GET /api/history`, `GET /api/inbound`, `POST /api/mock/{start,stop}`, `GET /healthz` (no auth), `GET /metrics`.

## Performance

With the mock SMSC in the same process, 10 TRX binds and window 100, unlimited speed reaches about 10k submit/s on a single core, with all DLRs matched. TPS targets are held closely. Real-world numbers depend on the SMSC and the network. For more throughput, run several instances.

## Notes

- With no `--user/--password`, the GUI is open to anyone who can reach the port. Set credentials on shared networks.
- PDU tracing costs CPU. Turn it off for full-speed tests.
- Per-message DLR tracking keeps up to 3M message_ids for 24 h. Unmatched DLRs are counted on the dashboard.
- SMPP 3.3 servers don't support TLVs. Use UDH concatenation and leave optional TLVs empty for them.
