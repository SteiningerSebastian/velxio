/*
 * net-bridge.c — a Velxio custom chip that asks a server outside the canvas
 * for the state of the machine it models, and reports its own back.
 *
 * This is the pattern the HTTP egress API (vx_net_*, see velxio-chip.h) exists
 * for: firmware that cannot be changed (or must stay byte-identical to what
 * ships on the real robot) talks to a chip across real buses, and the chip
 * relays the interesting part to a physics / kinematics simulator running
 * beside Velxio — a Docker container, a Python rig, a co-simulation peer.
 *
 *   [unmodified firmware] ──SPI/I2C/UART──▶ [this chip] ──HTTP──▶ [your server]
 *
 * Two things this file is careful about, because they are the two ways a
 * network in a simulated device goes wrong:
 *
 *  1. It never blocks the guest. All the waiting happens between vx_net_step
 *     calls with a ZERO wait budget, paced by a chip timer on SIMULATED time.
 *     A millisecond of wall-clock jitter inside a bus callback would show up
 *     as a stretched SPI clock in the logic analyser.
 *
 *  2. It models itself offline. A host without egress (the browser tab, a
 *     Linux board) answers vx_net_available() with 0, and the chip then
 *     serves its last known values instead of stalling the firmware.
 *
 * Edit REMOTE_URL / the pins to match your server. The default points at a
 * simulator on the same machine, which is the one case the shipped allowlist
 * grants off the school domain.
 */
#include "velxio-chip.h"

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* Where the simulator listens. Must be on the deployment's allowlist
 * (VELXIO_CHIP_NET_ALLOW; see backend/.env.example). */
#define REMOTE_URL      "http://localhost:3000/chip/state"
#define REMOTE_CTYPE    "application/json"

/* How often the chip asks, in SIMULATED nanoseconds. 10 ms keeps a robot arm
 * responsive without turning a 60 fps canvas into a request flood. The timer
 * itself ticks every 1 ms so an arrived reply or a DIN strobe is picked up
 * promptly without waiting a full poll period. */
#define POLL_PERIOD_NS  10000000ull
#define TICK_PERIOD_NS   1000000ull

/* Long enough for a JSON pose plus a status line; the host caps the reply at
 * VELXIO_CHIP_NET_MAX_BYTES regardless. */
#define REPLY_CAP       512
#define URL_CAP         256

typedef struct {
  vx_pin  dout;          /* chip → firmware: one bit, the "ready" flag */
  vx_pin  din;           /* firmware → chip: one bit, a command strobe   */
  vx_attr url_attr;      /* optional URL override from chip.json / UI    */
  vx_timer tick;
  int     online;
  int     ready;         /* the last exchange succeeded */
  uint64_t next_poll_ns;
  char    url[URL_CAP];
  char    reply[REPLY_CAP];
} bridge_t;

static bridge_t *g;

static const char *target_url(void) {
  uint32_t n = vx_attr_string_read(g->url_attr, g->url, sizeof(g->url));
  return (n > 0 && g->url[0] != '\0') ? g->url : REMOTE_URL;
}

/* ── The server conversation ────────────────────────────────────────────── */

/*
 * Step the exchange once. Returns:
 *   1  when a reply arrived inside this call (g->reply is populated),
 *   0  while the request was just dispatched or is still in flight,
 *  -1  when the request was refused or failed.
 *
 * A zero wait budget is deliberate: the guest's clock must not depend on the
 * host's scheduler. See "Never blocking the guest" in
 * docs/wiki/custom-chips-api-reference.md.
 */
static int step_remote(const char *body) {
  int32_t n = vx_net_step("POST", target_url(), REMOTE_CTYPE, body,
                          body ? (uint32_t)strlen(body) : 0u,
                          0u /* wait_ms */);
  if (n == VX_NET_BUSY) {
    return 0;                           /* still out; collect on a later tick */
  }
  if (n < 0) {
    /* DENIED / FAILED / BADARG / UNAVAILABLE, or EMPTY after an async transport
     * failure (vx_net_status() < 0). The host logs the reason once; all this
     * chip needs to do is drop DOUT so the firmware sees the link is down. */
    g->ready = 0;
    return -1;
  }
  uint32_t got = (uint32_t)vx_net_resp_read(0, g->reply, sizeof(g->reply) - 1);
  g->reply[got] = '\0';
  g->ready = 1;
  return 1;
}

/* A tiny "parse": find "ready":1 or "ready": true in whatever the server
 * sent. A real model would carry a JSON scanner; this example keeps the C
 * readable so the plumbing is the thing on show. */
static int reply_says_ready(void) {
  const char *p = strstr(g->reply, "\"ready\"");
  if (!p) return 0;
  p = strchr(p, ':');
  if (!p) return 0;
  p++;
  while (*p == ' ') p++;
  return (*p == '1' || strncmp(p, "true", 4) == 0);
}

/* ── What the firmware sees ──────────────────────────────────────────────── */

/* The command strobe: the firmware pulses DIN to say "report now". */
static void on_cmd(void *ud, vx_pin pin, int value) {
  (void)ud; (void)pin;
  if (!value || !g->online) return;
  /* Fire an early poll rather than a second request: one is in flight. */
  g->next_poll_ns = 0;
}

/*
 * The chip's own clock. Simulated time, so a paused canvas pauses the polling
 * too, and a saved project does not wake up with a burst of stale requests.
 */
static void on_tick(void *ud) {
  (void)ud;
  if (!g->online) return;
  uint64_t now = vx_sim_now_nanos();
  if (now < g->next_poll_ns || vx_net_busy()) return;

  /* What the firmware "reports" to the server. A real bridge would fill this
   * from the bus bytes it has decoded; here it is the chip's own state. */
  char report[96];
  snprintf(report, sizeof(report),
           "{\"report\":{\"ready\":%d,\"t_ns\":%llu}}",
           g->ready, (unsigned long long)now);

  int rc = step_remote(report);
  if (rc == 0) {
    /* Request is now in flight; keep next_poll_ns where it is so the next 1 ms
     * tick collects the reply as soon as it lands. */
    return;
  }
  vx_pin_write(g->dout, (rc > 0 && reply_says_ready()) ? 1 : 0);
  g->next_poll_ns = now + POLL_PERIOD_NS;
}

/* ── Lifecycle ──────────────────────────────────────────────────────────── */

void chip_setup(void) {
  g = (bridge_t *)calloc(1, sizeof(bridge_t));
  if (!g) return;

  g->dout     = vx_pin_register("DOUT", VX_OUTPUT_LOW);
  g->din      = vx_pin_register("DIN",  VX_INPUT_PULLDOWN);
  g->url_attr = vx_attr_register_string("url", REMOTE_URL);

  /* Ask the host, once. This is the call that lets one binary run unchanged
   * in the editor on an Uno (no egress) and in the ESP32 worker (egress). */
  g->online = vx_net_available() != 0;
  if (!g->online) {
    vx_log("net-bridge: no HTTP egress on this host; serving last known state");
    return;                            /* the chip still drives DOUT: low */
  }

  vx_pin_watch(g->din, VX_EDGE_RISING, on_cmd, g);

  g->tick = vx_timer_create(on_tick, g);
  vx_timer_start(g->tick, TICK_PERIOD_NS, true);

  vx_log("net-bridge ready: polling " REMOTE_URL);
}
