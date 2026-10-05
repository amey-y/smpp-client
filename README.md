# SMPP Client / Load Tester

A web-GUI SMPP 3.4 client for functional and load testing of an SMSC.
Python 3.12 + asyncio, with its own SMPP codec (no third-party SMPP library), served by aiohttp.

## Start

```bash
cd /home/onexadmin/onextel/smpp-client
./run.sh                                   # GUI on http://<server-ip>:8095
./run.sh --port 9000 --user admin --password secret    # with HTTP basic auth (recommended)
```

`run.sh` creates `.venv` and installs `aiohttp` and `uvloop` on first run.
You can also set the options as env vars: `SMPPCLIENT_PORT`, `SMPPCLIENT_HOST`, `SMPPCLIENT_USER`,
`SMPPCLIENT_PASSWORD` and `SMPPCLIENT_DATA` (the data directory).

To run it as a service, see `smpp-client.service` (copy it to `/etc/systemd/system/`, then `systemctl enable --now smpp-client`).

## Features

**Connections & binds**
- Multiple connection profiles (host/port/system_id/password/system_type/TON/NPI/address_range/interface version), saved to `data/connections.json` (mode 600).
- N binds per profile (TRX, TX or RX), plus optional extra RX binds for DLRs. Binds are staggered (configurable delay) so they don't all hit the server at once.
- Each bind is kept alive by itself: periodic `enquire_link`, answers the server's `enquire_link`, detects missing `enquire_link_resp`, handles server `unbind`, and auto-reconnects with exponential backoff (1 s → 30 s). It stops retrying on invalid password or system_id.
- Per-bind window (max unacknowledged submit_sm) and response timeout.
- Per-bind live table: state, uptime, in-flight/window, submitted/OK/error/timeout/DLR/MO, enquire_link RTT, reconnect count, last error. You can bind, unbind, add or remove individual binds at runtime.
- Optional TLS.

**Sending**
- **Single SMS**: shows submit_sm_resp status, message_id, latency and the matched DLR.
- **Bulk list**: paste numbers one per line (commas and semicolons also work). `+`, `00`, spaces and dashes are stripped. Optional dedupe, and a live valid/duplicate/invalid count.
- **Generate numbers**: count, country code, number length, operator prefixes, random or sequential, unique.
- **CSV upload**: one number per line, or columns with an optional per-row message column. Header and invalid rows are skipped. Large files are streamed to disk.
- **Placeholders** in the text: `{number} {seq} {seq:6} {otp} {rand:4} {uuid:8} {time} {date} {job}`.
- **Encodings**: Auto (GSM7 or UCS2), GSM 03.38 unpacked or packed, IA5, Latin-1, UCS2, binary (hex). Live character/segment counter.
- **Long messages**: UDH concatenation, SAR TLVs, `message_payload`, or truncate.
- **DLT TLVs**: Entity ID `0x1400`, Template ID `0x1401` and TM ID `0x1402` (the tags the onex SMSC reads), plus arbitrary custom TLVs (string/cstring/hex/int8/16/32).
- **Advanced submit_sm fields**: source/dest TON & NPI, service_type, esm_class, protocol_id, priority, validity_period, schedule_delivery_time, data_coding override, registered_delivery.
- **Saved message templates.** The form also remembers your last values.

**Load control**
- **TPS**: a global token bucket per job, measured in submit_sm PDUs per second, spread round-robin over all bound TX/TRX binds and bounded by each bind's window. `0` means unlimited.
- **Live control**: change TPS while a job runs, ramp up gradually, pause/resume/stop.
- **Repeat / duration / cap**: repeat the recipient list N times, run for a fixed duration (soak test), or cap the total message count.
- **Retries**: automatic retry with backoff on `ESME_RTHROTTLED` / `ESME_RMSGQFUL`.

**Monitoring & results**
- **Dashboard**: submit / response / DLR / MO per second (5-minute chart), bound binds, in-flight, totals.
- **Job detail**: progress and ETA, latency min/avg/p50/p95/p99/max, error breakdown by command_status, DLR status breakdown, DLR latency, latest messages.
- **DLR matching**: by `receipted_message_id` TLV or the `id:` field in the receipt text. If the IDs don't match directly, it also tries hex↔decimal conversion.
- **CSV downloads per job**: `submits.csv` (every PDU with status, message_id and latency) and `dlrs.csv`. History of past jobs is kept under `data/jobs/`.
- **DLR / MO viewer** with filters.
- **PDU trace** with hex dumps, enabled per connection.
- **Event log.**

**Mock SMSC**
- A built-in simulator for testing without a real SMSC. It enforces max binds per system_id and a per-bind TPS limit (`ESME_RTHROTTLED`), and supports a reject rate, response delay, and DLR delay / UNDELIV rate.
- Start it from the GUI, or standalone: `./run.sh mock --port 2775 --max-binds 10 --tps-per-bind 100`.

## Measured performance

On this server, against the mock SMSC running in the same process, with 10 TRX binds and window 100:
- the TPS target is held (3000 target → ~2900/s sustained including start/stop);
- unlimited speed reaches ~4800 submit/s with all DLRs matched.

The client runs on a single core. For more than ~5k TPS, run several instances.

## Notes

- With no `--user/--password` the GUI is open to anyone who can reach the port. Set credentials on shared networks.
- PDU tracing costs CPU. Turn it off for full-speed tests.
- Per-message DLR tracking keeps up to 3M message_ids for 24 h. Unmatched DLRs are counted on the dashboard.
