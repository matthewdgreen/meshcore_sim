// node_agent/main.cpp
//
// A single simulated MeshCore node process.
//
// Communication with the Python simulator is via newline-delimited JSON on
// stdin/stdout.  Stderr is used for fatal error messages only.
//
// STDIN  (orchestrator → node):
//   {"type":"rx",    "hex":"<hex>", "snr":<float>, "rssi":<float>}
//   {"type":"time",  "epoch":<uint32>}
//   {"type":"send_text", "dest":"<pub-key-hex-prefix>", "text":"<utf8>"}
//   {"type":"advert","name":"<utf8>"}
//   {"type":"quit"}
//
// STDOUT (node → orchestrator):
//   {"type":"ready", "pub":"<hex>", "is_relay":<bool>}
//   {"type":"tx",    "hex":"<hex>"}
//   {"type":"recv_text","from":"<hex>","name":"<str>","text":"<str>"}
//   {"type":"recv_data","from":"<hex>","payload_type":<int>,"hex":"<hex>"}
//   {"type":"advert","pub":"<hex>","name":"<str>"}
//   {"type":"ack",   "crc":<uint>}
//   {"type":"log",   "msg":"<str>"}
//
// Usage:
//   node_agent [--relay] [--room-server] [--prv <64-byte-hex>] [--name <str>]

#include "SimRadio.h"
#include "SimClock.h"
#include "SimRNG.h"
#include <SimNode.h>

#include <helpers/SimpleMeshTables.h>
#include <helpers/StaticPoolPacketManager.h>

#include <memory>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <sys/select.h>
#include <unistd.h>
#include <errno.h>

// ---------------------------------------------------------------------------
// Minimal JSON field extraction (no external library needed).
// ---------------------------------------------------------------------------

// Returns pointer to the value string inside a JSON "key":"value" pair,
// and writes the length of the value into *out_len.
// Works for string values only.  Returns nullptr on failure.
static const char* json_str_field(const char* json, const char* key,
                                   size_t* out_len) {
    // Search for  "key":
    char search[64];
    snprintf(search, sizeof(search), "\"%s\":", key);
    const char* p = strstr(json, search);
    if (!p) return nullptr;
    p += strlen(search);
    while (*p == ' ') p++;
    if (*p != '"') return nullptr;
    p++;  // skip opening quote
    const char* start = p;
    while (*p && *p != '"') {
        if (*p == '\\') p++;  // skip escaped char
        if (*p) p++;
    }
    if (out_len) *out_len = (size_t)(p - start);
    return start;
}

// Extract a numeric (integer/float) field from JSON.
static double json_num_field(const char* json, const char* key,
                               double default_val = 0.0) {
    char search[64];
    snprintf(search, sizeof(search), "\"%s\":", key);
    const char* p = strstr(json, search);
    if (!p) return default_val;
    p += strlen(search);
    while (*p == ' ') p++;
    return strtod(p, nullptr);
}

// Decode a hex string into bytes.  Returns number of bytes written.
static int hex_to_bytes(uint8_t* out, const char* hex, size_t hex_len) {
    if (hex_len % 2 != 0) return 0;
    int n = (int)(hex_len / 2);
    for (int i = 0; i < n; i++) {
        auto nib = [](char c) -> uint8_t {
            if (c >= '0' && c <= '9') return c - '0';
            if (c >= 'a' && c <= 'f') return c - 'a' + 10;
            if (c >= 'A' && c <= 'F') return c - 'A' + 10;
            return 0;
        };
        out[i] = (nib(hex[i*2]) << 4) | nib(hex[i*2+1]);
    }
    return n;
}

static void bytes_to_hex_main(char* out, const uint8_t* in, size_t len) {
    static const char H[] = "0123456789ABCDEF";
    for (size_t i = 0; i < len; i++) {
        out[i*2]     = H[in[i] >> 4];
        out[i*2 + 1] = H[in[i] & 0x0f];
    }
    out[len*2] = '\0';
}

// ---------------------------------------------------------------------------
// Dispatch a single JSON command line.
// ---------------------------------------------------------------------------
static void dispatch(const char* line, SimRadio& radio, SimClock& clock,
                     SimNode& node) {
    // Determine message type.
    size_t type_len = 0;
    const char* type_val = json_str_field(line, "type", &type_len);
    if (!type_val) return;

    if (type_len == 2 && strncmp(type_val, "rx", 2) == 0) {
        // Deliver a raw packet to this node's radio queue.
        size_t hex_len = 0;
        const char* hex = json_str_field(line, "hex", &hex_len);
        if (!hex || hex_len == 0) return;
        uint8_t buf[MAX_TRANS_UNIT + 1];
        int n = hex_to_bytes(buf, hex, hex_len);
        if (n <= 0) return;
        float snr  = (float)json_num_field(line, "snr",  6.0);
        float rssi = (float)json_num_field(line, "rssi", -90.0);
        radio.enqueue(buf, n, snr, rssi);

    } else if (type_len == 4 && strncmp(type_val, "time", 4) == 0) {
        uint32_t epoch = (uint32_t)json_num_field(line, "epoch");
        if (epoch > 0) clock.setCurrentTime(epoch);

    } else if (type_len == 9 && strncmp(type_val, "send_text", 9) == 0) {
        size_t dest_len = 0, text_len = 0;
        const char* dest = json_str_field(line, "dest", &dest_len);
        const char* text = json_str_field(line, "text", &text_len);
        if (!dest || !text) return;
        node.sendTextTo(std::string(dest, dest_len),
                        std::string(text, text_len));

    } else if (type_len == 6 && strncmp(type_val, "advert", 6) == 0) {
        size_t name_len = 0;
        const char* name = json_str_field(line, "name", &name_len);
        node.broadcastAdvert(name ? std::string(name, name_len) : "");

    } else if (type_len == 4 && strncmp(type_val, "quit", 4) == 0) {
        exit(0);
    }
}

// ---------------------------------------------------------------------------
// main
// ---------------------------------------------------------------------------
int main(int argc, char* argv[]) {
    bool        is_relay     = false;
    bool        is_room_svr  = false;
    const char* prv_hex      = nullptr;
    const char* node_name    = "node";

    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--relay") == 0) {
            is_relay = true;
        } else if (strcmp(argv[i], "--room-server") == 0) {
            is_room_svr = true;
        } else if (strcmp(argv[i], "--prv") == 0 && i + 1 < argc) {
            prv_hex = argv[++i];
        } else if (strcmp(argv[i], "--name") == 0 && i + 1 < argc) {
            node_name = argv[++i];
        }
    }

    // -- Instantiate simulation objects --
    SimRNG   rng;
    SimClock clock;
    SimRadio radio;

    static const int POOL_SIZE = 16;
    StaticPoolPacketManager mgr(POOL_SIZE);
    SimpleMeshTables         tables;

    // Select the correct node type at runtime.
    std::unique_ptr<SimNode> node_ptr;
    if (is_room_svr) {
        node_ptr = std::make_unique<RoomServerNode>(
            radio, clock, rng, clock, mgr, tables);
    } else {
        node_ptr = std::make_unique<SimNode>(
            radio, clock, rng, clock, mgr, tables, is_relay);
    }
    SimNode& node = *node_ptr;

    // -- Initialise node identity --
    if (prv_hex && strlen(prv_hex) == PRV_KEY_SIZE * 2) {
        uint8_t prv[PRV_KEY_SIZE];
        hex_to_bytes(prv, prv_hex, PRV_KEY_SIZE * 2);
        node.self_id.readFrom(prv, PRV_KEY_SIZE);
    } else {
        node.self_id = mesh::LocalIdentity(&rng);
    }

    node.begin();

    // Emit ready signal with our public key.
    // "role" is one of "endpoint", "relay", or "room-server".
    const char* role = is_room_svr ? "room-server"
                     : is_relay    ? "relay"
                                   : "endpoint";
    char pub_hex[PUB_KEY_SIZE * 2 + 1];
    bytes_to_hex_main(pub_hex, node.self_id.pub_key, PUB_KEY_SIZE);
    fprintf(stdout,
            "{\"type\":\"ready\",\"pub\":\"%s\",\"is_relay\":%s,"
            "\"role\":\"%s\",\"name\":\"%s\"}\n",
            pub_hex,
            is_relay ? "true" : "false",
            role,
            node_name);
    fflush(stdout);

    // -- Main loop --
    // Use select() with a 1 ms timeout so we poll stdin without blocking,
    // and still drive node.loop() frequently enough for timing to work.
    char line_buf[4096];
    int  line_pos = 0;

    while (true) {
        fd_set rfds;
        FD_ZERO(&rfds);
        FD_SET(STDIN_FILENO, &rfds);
        struct timeval tv { .tv_sec = 0, .tv_usec = 1000 };  // 1 ms

        int ret = select(STDIN_FILENO + 1, &rfds, nullptr, nullptr, &tv);
        if (ret < 0 && errno != EINTR) {
            perror("select");
            break;
        }

        if (ret > 0 && FD_ISSET(STDIN_FILENO, &rfds)) {
            char c;
            while (read(STDIN_FILENO, &c, 1) == 1) {
                if (c == '\n') {
                    if (line_pos > 0) {
                        line_buf[line_pos] = '\0';
                        dispatch(line_buf, radio, clock, node);
                        line_pos = 0;
                    }
                } else if (line_pos < (int)sizeof(line_buf) - 1) {
                    line_buf[line_pos++] = c;
                }
                fd_set probe;
                FD_ZERO(&probe);
                FD_SET(STDIN_FILENO, &probe);
                struct timeval zero { 0, 0 };
                if (select(STDIN_FILENO + 1, &probe, nullptr, nullptr, &zero) <= 0)
                    break;
            }
        }

        node.loop();
    }

    return 0;
}
