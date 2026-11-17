# AI Guide — Writing Networked Custom Chips for an External Simulation

**Audience:** an AI coding agent asked to build a Velxio custom chip (C →
WebAssembly) that connects ESP32 firmware to an external simulation (a robot,
a physics model, a test rig) over HTTP, and/or to build that simulation's HTTP
side.

This document is self-contained. Deeper references, if you need them:
`backend/sdk/velxio-chip.h` (the authoritative API), and
`docs/wiki/custom-chips-api-reference.md`, `docs/wiki/custom-chips-build-and-test.md`,
`docs/wiki/custom-chips-examples.md`. The working reference implementation is
`frontend/src/components/customChips/examples/net-bridge.c`.

---

## 1. The architecture in one picture

```
 ESP32 firmware (student's Arduino/ESP-IDF sketch, unmodified)
        │ real GPIO / I2C / SPI / UART / PWM
        ▼
 Custom chip  (your C code → .wasm, runs in the backend's QEMU worker)
        │ vx_net_request / vx_net_step  (non-blocking HTTP)
        │ allowlist check in the worker
        ├── localhost URL ─► relayed over WebSocket ─► student's BROWSER does fetch()
        │                                                   ▼
        │                              Simulator on the student's PC (http://localhost:PORT)
        └── other allowed URL ─► fetched by the Velxio server itself
```

Key facts:

- The chip is a **device model**. To the firmware it looks like real hardware
  (a motor driver, an encoder, an IMU, a sensor). To the simulator it is an
  HTTP client.
- **Only chips attached to an ESP32-family board get networking.** On AVR /
  RP2040 / Linux boards every `vx_net_*` call answers `VX_NET_UNAVAILABLE`
  and `vx_net_available()` returns 0. The chip must still load and behave
  sensibly ("offline mode").
- Velxio may run on a **remote server** while the simulator runs on the
  **student's PC**. That works because `localhost` URLs are fetched by the
  student's browser, not by the server. The chip code does not change.
- The chip can **never block**. Every request is asynchronous: start it, then
  collect the reply on a later tick.

---

## 2. Deliverables you will usually produce

1. `mychip.chip.c` — the chip model in C (C11, WASI libc available).
2. `mychip.chip.json` — the chip's manifest: pins, attributes, metadata.
3. The simulator's HTTP endpoint(s), which must speak CORS (section 7).
4. Optionally an Arduino sketch for the ESP32 that drives the chip's pins/bus.

---

## 3. `chip.json` manifest

```json
{
  "schema": "velxio-chip/v1",
  "name": "Robot Joint Bridge",
  "author": "",
  "license": "MIT",
  "description": "Drives a simulated robot joint over HTTP from PWM/DIR pins.",
  "pins": ["PWM", "DIR", "ENC_A", "ENC_B", "GND", "VCC"],
  "attributes": [
    { "name": "url",   "label": "Simulator URL", "type": "string", "default": "http://localhost:3000/joint/0" },
    { "name": "rate",  "label": "Polls / s",     "type": "int",    "default": 50, "min": 1, "max": 200 }
  ]
}
```

Rules:

- Every name in `pins` is a pad on the chip's footprint. The C code must
  register exactly these names with `vx_pin_register("NAME", mode)`; wires in
  the diagram attach to these names. Include `GND` and `VCC` (outputs swing
  to the VCC level).
- `attributes` appear in the part's property panel. `type`: `int`, `float`
  (or `number`), `string`. `min` + `max` together give a slider; `step` is optional.
  The `name` is what C passes to `vx_attr_register` / `vx_attr_register_string`.
- **Always expose the simulator URL as a `string` attribute** so a student can
  change port/path without recompiling.
- Optional: `"display": { "width": W, "height": H }` for a framebuffer chip.

---

## 4. The C API you need

Include only `#include "velxio-chip.h"` plus standard headers (`stdint.h`,
`stdbool.h`, `stdlib.h`, `string.h`, `stdio.h`). There is **no `main()`**; the
host calls the exported `void chip_setup(void)` once per chip instance.

### 4.1 Lifecycle and state

```c
typedef struct { /* all per-instance state */ } chip_t;
void chip_setup(void) {
  chip_t *s = calloc(1, sizeof *s);   /* heap, one per instance; never globals-only
                                          if two instances might exist */
  /* register pins, attributes, watches, timers here */
}
```

Every callback receives a `void* user_data` — pass your state pointer.

### 4.2 Pins

```c
vx_pin vx_pin_register(const char* name, vx_pin_mode mode);
  /* modes: VX_INPUT, VX_OUTPUT, VX_INPUT_PULLUP, VX_INPUT_PULLDOWN, VX_ANALOG,
            VX_OUTPUT_LOW, VX_OUTPUT_HIGH (drive that level immediately) */
int    vx_pin_read(vx_pin p);                 /* 0/1, level on the wire */
void   vx_pin_write(vx_pin p, int value);     /* drive 0/1 on an output */
double vx_pin_read_analog(vx_pin p);          /* volts on the pad's net */
void   vx_pin_dac_write(vx_pin p, double volts);
void   vx_pin_pwm_write(vx_pin p, double duty);  /* report a duty 0..1 */
int    vx_pin_wired(vx_pin p);                /* 1 if a wire reaches the pad */
void   vx_pin_set_mode(vx_pin p, vx_pin_mode mode);
void   vx_pin_watch(vx_pin p, vx_edge edge,   /* VX_EDGE_RISING/FALLING/BOTH */
                    void (*cb)(void* ud, vx_pin pin, int value), void* ud);
void   vx_pin_watch_stop(vx_pin p);
```

To measure a PWM duty from firmware (e.g. `analogWrite`/`ledcWrite` to a motor
pin), watch `VX_EDGE_BOTH` and time the edges with `vx_sim_now_nanos()`.

### 4.3 Attributes

```c
vx_attr  vx_attr_register(const char* name, double default_val);
double   vx_attr_read(vx_attr a);              /* re-read in callbacks: it can change live */
vx_attr  vx_attr_register_string(const char* name, const char* default_val);
uint32_t vx_attr_string_len(vx_attr a);
uint32_t vx_attr_string_read(vx_attr a, char* buf, uint32_t cap);  /* NUL-terminates if room */
```

### 4.4 Time and timers (SIMULATED time)

```c
uint64_t vx_sim_now_nanos(void);   /* guest clock; inside a timer cb = that deadline */
vx_timer vx_timer_create(void (*cb)(void* ud), void* ud);
void     vx_timer_start(vx_timer t, uint64_t period_nanos, bool repeat);
void     vx_timer_stop(vx_timer t);
```

Drive all networking from a repeating timer. Simulated time pauses with the
simulation, so a paused canvas makes no requests.

### 4.5 Buses (when the firmware talks to the chip over a bus)

- **I2C target:** fill `vx_i2c_config` (`address` 7-bit, `scl`, `sda`,
  `on_connect`, `on_read`, `on_write`, `on_stop`, `user_data`; zero
  `reserved`) → `vx_i2c_attach(&cfg)`.
- **SPI target:** `vx_spi_config` (`sck`, `mosi`, `miso`, `cs` or
  `(vx_pin)-1`, `mode` 0..3, `on_done`, `user_data`, optional `on_exchange`
  for per-byte answers) → `vx_spi_attach`, arm with `vx_spi_start(s, buf, n)`.
- **UART:** `vx_uart_config` (`rx`, `tx`, `baud_rate`, `on_rx_byte`,
  `on_tx_done`, `user_data`) → `vx_uart_attach`; send with `vx_uart_write`.

Always zero-initialise configs (`vx_i2c_config cfg = {0};` or designated
initialisers). **Bus callbacks must answer immediately from cached state** —
never wait for the network inside them (section 5).

### 4.6 Logging

`vx_log("text")` or `printf(...)` (WASI stdout). Shown in the browser console
as `[chip:<board>] ...` and in the backend log. Don't log on every tick.

---

## 5. The networking API (`vx_net_*`)

```c
int32_t vx_net_available(void);   /* 1 = this host offers HTTP; 0 = offline */
int32_t vx_net_request(const char* method, const char* url,
                       const char* content_type,      /* may be NULL */
                       const void* body, uint32_t body_len);  /* 0 = dispatched */
int32_t vx_net_poll(void);        /* >=0 body length (reply ready), or negative code */
int32_t vx_net_step(const char* method, const char* url, const char* content_type,
                    const void* body, uint32_t body_len, uint32_t wait_ms);
                                  /* request-if-idle + poll; use wait_ms = 0 */
int32_t vx_net_resp_len(void);    /* length of last reply body */
int32_t vx_net_resp_read(uint32_t offset, void* dst, uint32_t cap);  /* NOT NUL-terminated */
int32_t vx_net_status(void);      /* HTTP status of last reply, or negative code */
int32_t vx_net_busy(void);        /* 1 while a request is in flight */
```

Result codes (non-negative = byte count):

| Code | Value | Meaning / what to do |
|---|---|---|
| `VX_NET_BUSY` | -1 | Still in flight. Try again next tick. |
| `VX_NET_EMPTY` | -2 | Nothing to collect. If `vx_net_status() < 0`, the last request failed. |
| `VX_NET_DENIED` | -3 | URL not on the server's allowlist. Reason logged once. |
| `VX_NET_BADARG` | -4 | Bad method/URL/pointer. |
| `VX_NET_FAILED` | -5 | Network error, timeout, CORS refusal, simulator not running. |
| `VX_NET_UNAVAILABLE` | -6 | Host has no networking (non-ESP32 board, or disabled). |

Hard rules:

1. **Call `vx_net_available()` once in `chip_setup`.** If 0, run an offline
   model (hold last values, report "not ready") and never call the rest.
2. **One request in flight per chip instance.** A second `vx_net_request`
   while busy returns `VX_NET_BUSY`. Combine everything into one request per
   exchange.
3. **Never block.** Use `wait_ms = 0`. Any wait stalls the whole emulated ESP32.
4. **A reply is handed out once.** After `vx_net_poll`/`vx_net_step` returns
   `n >= 0`, copy it out with `vx_net_resp_read` and NUL-terminate it yourself.
5. **Bus/pin callbacks read cached state only.** The timer does the
   networking and updates the cache; callbacks serve from the cache.
6. **Methods:** GET, HEAD, POST, PUT, PATCH, DELETE, OPTIONS. Schemes: http,
   https. Redirects are **not** followed.
7. **Limits:** request timeout 5 s (`VELXIO_CHIP_NET_TIMEOUT`), reply body cap
   1 MiB (cut, not failed), URL ≤ 4096 bytes. Keep replies small (< 1 KB ideal).
8. **Rate:** each relayed exchange goes worker → server → browser → simulator
   and back. Expect ~2–10 ms with Velxio local and ~20–100 ms with Velxio
   remote. Design for **10–50 exchanges/s**, batch all state into each one,
   and make the simulation tolerate latency (interpolate, don't lock-step).

### Allowlist (server-side, per deployment)

Default: `localhost`, `127.0.0.1`, `[::1]` on any port (http/https), and
`*.apps.htl-neufelden.at` on 443/https. Others need `VELXIO_CHIP_NET_ALLOW`
on the Velxio server. `VELXIO_CHIP_NET_ROUTE` = `auto` (default: localhost
via the browser, rest via the server) | `browser` | `server` (use `server`
when Velxio and the simulator share a docker-compose network and the chip
targets `http://<service-name>:<port>`; add that host to the allowlist).

---

## 6. Canonical chip template

The pattern to follow: a timer polls; one combined POST per exchange carries
what the firmware did; the reply carries what the world did; pins and buses
serve the cached reply.

```c
#include "velxio-chip.h"
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define DEFAULT_URL "http://localhost:3000/joint/0"
#define TICK_NS     1000000ull          /* timer every 1 ms simulated */

typedef struct {
  vx_pin pwm, dir, enc_a;
  vx_attr url_attr, rate_attr;
  vx_timer tick;
  int online, link_ok;
  /* firmware → world (measured from pins) */
  uint64_t pwm_rise_ns, pwm_high_ns, pwm_period_ns;
  /* world → firmware (cached from the last reply) */
  long encoder_count;
  uint64_t next_ns;
  char url[256];
  char reply[512];
} chip_t;

static const char *url_of(chip_t *s) {
  uint32_t n = vx_attr_string_read(s->url_attr, s->url, sizeof s->url);
  return (n > 0 && s->url[0]) ? s->url : DEFAULT_URL;
}

/* Minimal "parse": find "key": <integer>. Replace with a real scanner if needed. */
static int json_long(const char *js, const char *key, long *out) {
  char pat[48];
  snprintf(pat, sizeof pat, "\"%s\"", key);
  const char *p = strstr(js, pat);
  if (!p || !(p = strchr(p + strlen(pat), ':'))) return 0;
  *out = strtol(p + 1, NULL, 10);
  return 1;
}

static void on_pwm(void *ud, vx_pin pin, int v) {
  chip_t *s = ud; (void)pin;
  uint64_t t = vx_sim_now_nanos();
  if (v) { if (s->pwm_rise_ns) s->pwm_period_ns = t - s->pwm_rise_ns; s->pwm_rise_ns = t; }
  else if (s->pwm_rise_ns)    { s->pwm_high_ns = t - s->pwm_rise_ns; }
}

static void on_tick(void *ud) {
  chip_t *s = ud;
  uint64_t now = vx_sim_now_nanos();
  if (now < s->next_ns) return;

  double duty = s->pwm_period_ns ? (double)s->pwm_high_ns / (double)s->pwm_period_ns : 0.0;
  char body[128];
  snprintf(body, sizeof body, "{\"duty\":%.3f,\"dir\":%d,\"t_ns\":%llu}",
           duty, vx_pin_read(s->dir), (unsigned long long)now);

  /* text/plain avoids a CORS preflight (half the round trips); body is JSON. */
  int32_t n = vx_net_step("POST", url_of(s), "text/plain", body, (uint32_t)strlen(body), 0);
  if (n == VX_NET_BUSY) return;                 /* collect on a later tick */
  if (n < 0) {                                  /* failed/denied: back off */
    s->link_ok = 0;
    s->next_ns = now + 100000000ull;            /* retry in 100 ms */
    return;
  }
  int32_t got = vx_net_resp_read(0, s->reply, sizeof s->reply - 1);
  s->reply[got > 0 ? got : 0] = '\0';
  s->link_ok = (vx_net_status() == 200);
  long enc;
  if (s->link_ok && json_long(s->reply, "encoder", &enc)) s->encoder_count = enc;
  /* reflect world state onto pins here (e.g. quadrature from encoder_count) */

  double rate = vx_attr_read(s->rate_attr);
  s->next_ns = now + (uint64_t)(1e9 / (rate > 0 ? rate : 50));
}

void chip_setup(void) {
  chip_t *s = calloc(1, sizeof *s);
  if (!s) return;
  s->pwm   = vx_pin_register("PWM",   VX_INPUT);
  s->dir   = vx_pin_register("DIR",   VX_INPUT);
  s->enc_a = vx_pin_register("ENC_A", VX_OUTPUT_LOW);
  vx_pin_register("ENC_B", VX_OUTPUT_LOW);
  s->url_attr  = vx_attr_register_string("url", DEFAULT_URL);
  s->rate_attr = vx_attr_register("rate", 50);

  s->online = vx_net_available() != 0;
  if (!s->online) { vx_log("joint-bridge: no network on this board (ESP32 only); offline"); return; }

  vx_pin_watch(s->pwm, VX_EDGE_BOTH, on_pwm, s);
  s->tick = vx_timer_create(on_tick, s);
  vx_timer_start(s->tick, TICK_NS, true);
}
```

Note: `vx_net_step` with `wait_ms = 0` dispatches when idle and returns
`VX_NET_BUSY`; a later call (same arguments are fine) collects the reply.
Don't change `s->next_ns` while busy, so the next 1 ms tick collects promptly.

---

## 7. The simulator's HTTP side (the peer)

Requests to `localhost` arrive as a **cross-origin `fetch()` from the Velxio
web page**. The simulator must:

- **Send CORS headers on every response**, including errors:
  `Access-Control-Allow-Origin: *` (or the exact Velxio origin).
- **Answer `OPTIONS` preflights** with 204 and
  `Access-Control-Allow-Methods: GET, POST, PUT, PATCH, DELETE, OPTIONS`,
  `Access-Control-Allow-Headers: Content-Type`,
  `Access-Control-Allow-Private-Network: true`.
  A preflight happens for `PUT/PATCH/DELETE` or a `Content-Type` other than
  `text/plain` / form types. Prefer `POST` with `text/plain` to skip it.
- **Listen on `127.0.0.1`** and a fixed, documented port (default the chip's URL).
- **Answer fast** (< 5 ms of work). Run the physics loop on its own thread or
  timer; the HTTP handler just exchanges the latest state, never steps the
  simulation synchronously per request unless that is cheap.
- **Return small JSON**, flat keys, numbers only where possible, so the C side
  can parse it with `strstr`/`strtol`.
- Expect the first request from a public `https://` Velxio to trigger a
  one-time browser prompt ("allow access to local network"); tell the user.

Minimal Python peer (stdlib only):

```python
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json, threading, time

CORS = {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'GET, POST, PUT, PATCH, DELETE, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
    'Access-Control-Allow-Private-Network': 'true',
}
lock = threading.Lock()
cmd = {'duty': 0.0, 'dir': 0}
world = {'encoder': 0}

def physics():                       # runs independently of HTTP
    pos = 0.0
    while True:
        with lock:
            v = cmd['duty'] * (1 if cmd['dir'] else -1) * 600.0   # counts/s
            pos += v * 0.001
            world['encoder'] = int(pos)
        time.sleep(0.001)

class H(BaseHTTPRequestHandler):
    def _send(self, code, obj=None):
        body = json.dumps(obj).encode() if obj is not None else b''
        self.send_response(code)
        for k, v in CORS.items(): self.send_header(k, v)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers(); self.wfile.write(body)
    def do_OPTIONS(self): self._send(204)
    def do_GET(self):
        with lock: self._send(200, dict(world))
    def do_POST(self):
        n = int(self.headers.get('Content-Length', 0))
        try: data = json.loads(self.rfile.read(n) or b'{}')
        except ValueError: return self._send(400, {'error': 'bad json'})
        with lock:
            cmd.update({k: data[k] for k in ('duty', 'dir') if k in data})
            reply = dict(world)
        self._send(200, reply)
    def log_message(self, *a): pass   # keep the console quiet at 50 req/s

threading.Thread(target=physics, daemon=True).start()
ThreadingHTTPServer(('127.0.0.1', 3000), H).serve_forever()
```

### Protocol design guidance

- One endpoint per chip instance, e.g. `/joint/0`, `/joint/1` (set via the
  `url` attribute), or one endpoint with an `"id"` field.
- **POST = "here is what the firmware is doing; tell me the world now"** — one
  round trip per tick, carries both directions.
- Include the chip's simulated time (`t_ns`) so the simulator can detect
  pauses/resets and avoid integrating wall time while Velxio is paused.
- Make the simulator robust to gaps (tab in background, simulation paused,
  network hiccup): hold last command, time it out to a safe state (motor off).
- Version the payload (`"v":1`) if the protocol may evolve.

---

## 8. Build, load, run

- **In the app:** add a Custom Chip to the canvas (blank or start from the
  "HTTP Bridge (vx_net_*)" example), paste the `.chip.c` and `.chip.json`,
  wire its pins to an ESP32 board, compile, then Run. Compilation happens on
  the backend (`POST /api/compile-chip/` with `{ "source": "<C>", "chip_json": "<json>" }`
  → `{ "success", "wasm_base64", "stderr", "error" }`; check availability with
  `GET /api/compile-chip/status`).
- **Locally** (WASI-SDK 22):

  ```bash
  $WASI_SDK/bin/clang --target=wasm32-unknown-wasip1 -O2 -nostartfiles \
    -Wl,--import-memory -Wl,--export-table -Wl,--no-entry \
    -Wl,--export=chip_setup -Wl,--allow-undefined \
    -I backend/sdk mychip.chip.c -o mychip.wasm
  ```

- **Order:** start the simulator first, then Run in Velxio. Check the browser
  console for `[chip:...]` lines; a failure reason (DENIED, CORS, timeout)
  is logged once.

---

## 9. Checklist before you hand it over

- [ ] Pin names in C == `pins` in chip.json; `GND`/`VCC` included.
- [ ] State on the heap via `calloc`, passed as `user_data`; no `main()`.
- [ ] `vx_net_available()` checked in `chip_setup`; offline behaviour defined.
- [ ] All networking in a `vx_timer` callback, `wait_ms = 0`, back-off on errors.
- [ ] Bus/pin callbacks never touch the network; they serve cached values.
- [ ] Reply copied with `vx_net_resp_read` and NUL-terminated; buffer bounds checked.
- [ ] URL is a `string` attribute with a localhost default.
- [ ] Simulator: CORS on every response, `OPTIONS` handled, binds 127.0.0.1,
      answers quickly, state updated on its own loop.
- [ ] Rate ≤ ~50/s and every exchange carries the full state.
- [ ] Board is ESP32-family (other boards: chip runs offline by design).

## 10. Common mistakes

| Symptom | Cause |
|---|---|
| Chip always offline | Not on an ESP32 board, or `VELXIO_CHIP_NET=off` on the server. |
| `VX_NET_DENIED` | URL host/port/scheme not on the allowlist (`VELXIO_CHIP_NET_ALLOW`). |
| `VX_NET_FAILED`, log mentions CORS | Simulator not running, wrong port, or missing CORS / `OPTIONS` handling. |
| Firmware timing jitters / ESP32 freezes | `wait_ms > 0`, or networking inside a bus callback. |
| Stale or garbage reply text | Forgot to NUL-terminate after `vx_net_resp_read`. |
| Always `VX_NET_BUSY` | Requests outpace round-trip latency; lower the rate. |
| Wires attach to the chip corner | Pin name in the diagram ≠ name in `pins` / `vx_pin_register`. |
