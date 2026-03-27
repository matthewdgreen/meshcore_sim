#include "SimNode.h"
#include <stdio.h>
#include <stdarg.h>
#include <string.h>
#include <algorithm>

// ---------------------------------------------------------------------------
// Density table — maps neighbor count to timing multipliers.
//
// Source: Table 9.4.2 of the collision-mitigation proposal (Privitt et al.).
// Each entry covers neighbors >= min_neighbors up to the next entry's threshold.
//
// txdelay        — flood retransmit window multiplier
// direct_txdelay — direct/unicast retransmit window multiplier
//
// The actual delay window in ms is:
//   5 × LORA_AIRTIME_MS × txdelay   (flood)
//   5 × LORA_AIRTIME_MS × direct_txdelay  (direct)
// ---------------------------------------------------------------------------
struct DelayEntry {
    int   min_neighbors;
    float txdelay;
    float direct_txdelay;
};

static const DelayEntry DENSITY_TABLE[] = {
    {  0, 1.0f, 0.4f },   // Sparse (0 neighbors)
    {  1, 1.1f, 0.5f },   // Sparse (1 neighbor)
    {  2, 1.2f, 0.6f },   // Sparse (2–3 neighbors)
    {  4, 1.3f, 0.7f },   // Medium (4–5 neighbors)
    {  6, 1.5f, 0.7f },   // Medium (6–7 neighbors)
    {  8, 1.7f, 0.8f },   // Medium (8 neighbors)
    {  9, 1.8f, 0.8f },   // Dense  (9 neighbors)
    { 10, 1.9f, 0.9f },   // Dense  (10 neighbors)
    { 11, 2.0f, 0.9f },   // Regional (11 neighbors)
    { 12, 2.1f, 0.9f },   // Regional (12+ neighbors)
};
static const int DENSITY_TABLE_LEN =
    (int)(sizeof(DENSITY_TABLE) / sizeof(DENSITY_TABLE[0]));

// ---------------------------------------------------------------------------
// Hex helpers
// ---------------------------------------------------------------------------
static const char HEX_UC[] = "0123456789ABCDEF";

static void bytes_to_hex(char* out, const uint8_t* in, size_t len) {
    for (size_t i = 0; i < len; i++) {
        out[i*2]     = HEX_UC[in[i] >> 4];
        out[i*2 + 1] = HEX_UC[in[i] & 0x0F];
    }
    out[len*2] = '\0';
}

// ---------------------------------------------------------------------------
// Constructor
// ---------------------------------------------------------------------------
SimNode::SimNode(mesh::Radio& radio, mesh::MillisecondClock& ms,
                 mesh::RNG& rng, mesh::RTCClock& rtc,
                 mesh::PacketManager& mgr, mesh::MeshTables& tables,
                 bool is_relay)
    : mesh::Mesh(radio, ms, rng, rtc, mgr, tables),
      _is_relay(is_relay),
      _txdelay(DENSITY_TABLE[0].txdelay),
      _direct_txdelay(DENSITY_TABLE[0].direct_txdelay),
      _prev_neighbor_count(-1)
{}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------
void SimNode::emitLog(const char* fmt, ...) const {
    char msg[512];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(msg, sizeof(msg), fmt, ap);
    va_end(ap);
    fprintf(stdout, "{\"type\":\"log\",\"msg\":\"%s\"}\n", msg);
    fflush(stdout);
}

void SimNode::emitJson(const char* json) const {
    fprintf(stdout, "%s\n", json);
    fflush(stdout);
}

// ---------------------------------------------------------------------------
// Adaptive timing
// ---------------------------------------------------------------------------
void SimNode::_update_timing() {
    int n = (int)_contacts.size();
    if (n == _prev_neighbor_count) return;  // nothing changed

    // Find highest-threshold entry that applies (table is sorted ascending).
    const DelayEntry* best = &DENSITY_TABLE[0];
    for (int i = 1; i < DENSITY_TABLE_LEN; i++) {
        if (DENSITY_TABLE[i].min_neighbors <= n)
            best = &DENSITY_TABLE[i];
        else
            break;
    }

    _prev_neighbor_count = n;
    _txdelay        = best->txdelay;
    _direct_txdelay = best->direct_txdelay;

    char json[192];
    snprintf(json, sizeof(json),
             "{\"type\":\"txdelay_update\",\"neighbor_count\":%d,"
             "\"txdelay\":%.2f,\"direct_txdelay\":%.2f}",
             n, _txdelay, _direct_txdelay);
    emitJson(json);
}

// ---------------------------------------------------------------------------
// getRetransmitDelay — density-adaptive random backoff
//
// Adaptive delay scope: DATA packets only (TXT_MSG, PATH, etc.).
// ADVERT packets return 0 (no delay), identical to the base SimNode behaviour.
//
// Why zero for adverts: the adaptive delay window is intentionally wide to
// spread relay retransmissions of DATA floods.  Applying that same wide window
// to ADVERT relays produces a ~61% chance of symmetric last-hop collisions
// when two relay nodes (e.g. n_0_1 and n_1_0) independently draw delays
// within one airtime of each other and both attempt to relay to the same
// corner node (e.g. n_0_0) simultaneously.  With zero delay, advert flooding
// is deterministic and relies on multi-path propagation across multiple advert
// rounds to reach all nodes — identical to the base node_agent behaviour that
// is already validated by the contention experiment.
//
// Returns:
//   ADVERT: 0  (no delay — base class behaviour)
//   other:  uniform in [0, 5 × LORA_AIRTIME_MS × txdelay)  (adaptive)
//
// Rationale (from proposal section 3.1):
//   t = floor(5 × txdelay) slots, each slot LORA_AIRTIME_MS wide.
//   P(two nodes pick the same slot) ≈ 1 / t ≈ 1 / (5 × txdelay).
//   For txdelay=1.0 (0 neighbors): P ≈ 20%.
//   For txdelay=1.3 (4 neighbors): P ≈ 15%.
//   For txdelay=2.0 (11 neighbors): P ≈ 10%.
// ---------------------------------------------------------------------------
uint32_t SimNode::getRetransmitDelay(const mesh::Packet* packet) {
    float max_ms;

    if (packet->getPayloadType() == PAYLOAD_TYPE_ADVERT) {
        // No delay for advert relays — avoids symmetric last-hop collision.
        return 0;
    } else {
        float td = packet->isRouteDirect() ? _direct_txdelay : _txdelay;
        max_ms = 5.0f * LORA_AIRTIME_MS * td;
    }

    // Uniform random float in [0, 1) from the node's RNG.
    uint8_t buf[4];
    getRNG()->random(buf, 4);
    uint32_t r = ((uint32_t)buf[0] << 24) | ((uint32_t)buf[1] << 16) |
                 ((uint32_t)buf[2] << 8)  |  (uint32_t)buf[3];
    // Divide by 2^32 to get a value in [0, 1).
    float frac = (float)r * (1.0f / 4294967296.0f);
    return (uint32_t)(frac * max_ms);
}

// ---------------------------------------------------------------------------
// Routing overrides
// ---------------------------------------------------------------------------
bool SimNode::allowPacketForward(const mesh::Packet* /*packet*/) {
    return _is_relay;
}

int SimNode::searchPeersByHash(const uint8_t* hash) {
    _search_results.clear();
    for (int i = 0; i < (int)_contacts.size(); i++) {
        if (_contacts[i].id.isHashMatch(hash)) {
            _search_results.push_back(i);
        }
    }
    return (int)_search_results.size();
}

void SimNode::getPeerSharedSecret(uint8_t* dest_secret, int peer_idx) {
    if (peer_idx < 0 || peer_idx >= (int)_search_results.size()) return;
    int idx = _search_results[peer_idx];
    memcpy(dest_secret, _contacts[idx].shared_secret, PUB_KEY_SIZE);
}

// ---------------------------------------------------------------------------
// Event callbacks
// ---------------------------------------------------------------------------
void SimNode::onPeerDataRecv(mesh::Packet* packet, uint8_t type,
                              int sender_idx, const uint8_t* /*secret*/,
                              uint8_t* data, size_t len) {
    if (sender_idx < 0 || sender_idx >= (int)_search_results.size()) return;
    int idx = _search_results[sender_idx];
    Contact& c = _contacts[idx];

    char pub_hex[PUB_KEY_SIZE * 2 + 1];
    bytes_to_hex(pub_hex, c.id.pub_key, PUB_KEY_SIZE);

    if (type == PAYLOAD_TYPE_TXT_MSG && len > 4) {
        const char* text = (const char*)(data + 4);
        size_t text_len  = len - 4;
        char escaped[256];
        size_t ei = 0;
        for (size_t ti = 0; ti < text_len && ei < sizeof(escaped) - 2; ti++) {
            char ch = text[ti];
            if (ch == '"' || ch == '\\') escaped[ei++] = '\\';
            escaped[ei++] = ch;
        }
        escaped[ei] = '\0';
        char json[512];
        snprintf(json, sizeof(json),
                 "{\"type\":\"recv_text\",\"from\":\"%s\",\"name\":\"%s\",\"text\":\"%s\"}",
                 pub_hex, c.name.c_str(), escaped);
        emitJson(json);
    } else {
        char hex[MAX_PACKET_PAYLOAD * 2 + 1];
        bytes_to_hex(hex, data, std::min(len, (size_t)MAX_PACKET_PAYLOAD));
        char json[512];
        snprintf(json, sizeof(json),
                 "{\"type\":\"recv_data\",\"from\":\"%s\",\"payload_type\":%d,\"hex\":\"%s\"}",
                 pub_hex, (int)type, hex);
        emitJson(json);
    }

    // Path exchange: learn and teach the direct route on first flood TXT_MSG.
    if (type == PAYLOAD_TYPE_TXT_MSG
        && packet->isRouteFlood()
        && !c.has_path
        && packet->getPathHashCount() > 0) {

        uint8_t sz  = packet->getPathHashSize();
        uint8_t cnt = packet->getPathHashCount();

        c.path.resize((size_t)cnt * sz);
        for (uint8_t i = 0; i < cnt; i++) {
            memcpy(c.path.data() + (size_t)(cnt - 1 - i) * sz,
                   packet->path + (size_t)i * sz, sz);
        }
        c.has_path = true;

        mesh::Packet* rpath = createPathReturn(
            c.id, c.shared_secret,
            packet->path, packet->path_len,
            0, nullptr, 0);
        if (rpath) sendFlood(rpath);

        char pub_hex2[PUB_KEY_SIZE * 2 + 1];
        bytes_to_hex(pub_hex2, c.id.pub_key, PUB_KEY_SIZE);
        emitLog("path-exchange: stored %d-hop reverse path to %.16s; sent PATH return",
                (int)cnt, pub_hex2);
    }
}

bool SimNode::onPeerPathRecv(mesh::Packet* /*packet*/, int sender_idx,
                              const uint8_t* /*secret*/,
                              uint8_t* path, uint8_t path_len,
                              uint8_t /*extra_type*/, uint8_t* /*extra*/,
                              uint8_t /*extra_len*/) {
    if (sender_idx < 0 || sender_idx >= (int)_search_results.size()) return false;
    int idx = _search_results[sender_idx];
    Contact& c = _contacts[idx];

    c.has_path = true;
    c.path.assign(path, path + path_len);

    char pub_hex[PUB_KEY_SIZE * 2 + 1];
    bytes_to_hex(pub_hex, c.id.pub_key, PUB_KEY_SIZE);
    emitLog("path learned to %s (len=%d)", pub_hex, (int)path_len);
    return false;
}

void SimNode::onAdvertRecv(mesh::Packet* /*packet*/, const mesh::Identity& id,
                           uint32_t /*timestamp*/,
                           const uint8_t* app_data, size_t app_data_len) {
    if (id.matches(self_id)) return;

    Contact* existing = nullptr;
    for (auto& c : _contacts) {
        if (c.id.matches(id)) { existing = &c; break; }
    }
    if (!existing) {
        _contacts.emplace_back();
        existing = &_contacts.back();
        existing->id = id;
        self_id.calcSharedSecret(existing->shared_secret, id);
    }
    if (app_data && app_data_len > 0) {
        existing->name = std::string((const char*)app_data,
                                     strnlen((const char*)app_data, app_data_len));
    }

    char pub_hex[PUB_KEY_SIZE * 2 + 1];
    bytes_to_hex(pub_hex, id.pub_key, PUB_KEY_SIZE);
    char json[256];
    snprintf(json, sizeof(json),
             "{\"type\":\"advert\",\"pub\":\"%s\",\"name\":\"%s\"}",
             pub_hex, existing->name.c_str());
    emitJson(json);

    // Retune timing whenever neighbor count changes.
    _update_timing();
}

void SimNode::onAckRecv(mesh::Packet* /*packet*/, uint32_t ack_crc) {
    char json[128];
    snprintf(json, sizeof(json), "{\"type\":\"ack\",\"crc\":%u}", ack_crc);
    emitJson(json);
}

void SimNode::logRx(mesh::Packet* packet, int len, float score) {
    emitLog("RX len=%d type=%d route=%s score=%.2f",
            len, (int)packet->getPayloadType(),
            packet->isRouteDirect() ? "D" : "F", score);
}

void SimNode::logTx(mesh::Packet* packet, int len) {
    emitLog("TX len=%d type=%d route=%s",
            len, (int)packet->getPayloadType(),
            packet->isRouteDirect() ? "D" : "F");
}

// ---------------------------------------------------------------------------
// Application-level helpers
// ---------------------------------------------------------------------------
bool SimNode::sendTextTo(const std::string& dest_pub_hex,
                         const std::string& text) {
    Contact* target = nullptr;
    for (auto& c : _contacts) {
        char pub_hex[PUB_KEY_SIZE * 2 + 1];
        bytes_to_hex(pub_hex, c.id.pub_key, PUB_KEY_SIZE);
        if (std::string(pub_hex).rfind(dest_pub_hex, 0) == 0) {
            target = &c;
            break;
        }
    }
    if (!target) {
        emitLog("sendTextTo: unknown destination %s", dest_pub_hex.c_str());
        return false;
    }

    uint32_t ts = getRTCClock()->getCurrentTimeUnique();
    std::vector<uint8_t> payload(4 + text.size());
    memcpy(payload.data(), &ts, 4);
    memcpy(payload.data() + 4, text.data(), text.size());

    mesh::Packet* pkt = createDatagram(PAYLOAD_TYPE_TXT_MSG,
                                       target->id,
                                       target->shared_secret,
                                       payload.data(), payload.size());
    if (!pkt) {
        emitLog("sendTextTo: createDatagram failed (pool exhausted?)");
        return false;
    }

    if (target->has_path && !target->path.empty()) {
        sendDirect(pkt, target->path.data(), (uint8_t)target->path.size());
    } else {
        sendFlood(pkt);
    }
    return true;
}

void SimNode::broadcastAdvert(const std::string& name) {
    uint8_t app_data[MAX_ADVERT_DATA_SIZE];
    size_t  app_len = 0;
    if (!name.empty()) {
        app_len = std::min(name.size(), (size_t)MAX_ADVERT_DATA_SIZE - 1);
        memcpy(app_data, name.data(), app_len);
        app_data[app_len++] = '\0';
    }
    mesh::Packet* pkt = createAdvert(self_id,
                                     app_len ? app_data : nullptr,
                                     app_len);
    if (pkt) sendFlood(pkt);
}

void SimNode::setupPublicChannel(const std::string& /*name*/) {
    // Stub: adaptive_delay experiment uses mesh::Mesh (not BaseChatMesh).
    // Group channels require BaseChatMesh — a future upgrade.
}

bool SimNode::sendChannelText(const std::string& /*text*/) {
    emitLog("sendChannelText: not supported (experiment agent)");
    return false;
}

void SimNode::loop() {
    mesh::Mesh::loop();
}

// ---------------------------------------------------------------------------
// RoomServerNode — included for binary compatibility only.
// ---------------------------------------------------------------------------
static bool json_escape(char* out, size_t out_size,
                        const char* in, size_t in_len) {
    size_t wi = 0;
    for (size_t ri = 0; ri < in_len; ri++) {
        unsigned char ch = (unsigned char)in[ri];
        if (wi + 3 >= out_size) { out[wi] = '\0'; return false; }
        if (ch == '"' || ch == '\\') { out[wi++] = '\\'; }
        out[wi++] = (char)ch;
    }
    out[wi] = '\0';
    return true;
}

RoomServerNode::RoomServerNode(mesh::Radio& radio, mesh::MillisecondClock& ms,
                               mesh::RNG& rng, mesh::RTCClock& rtc,
                               mesh::PacketManager& mgr,
                               mesh::MeshTables& tables)
    : SimNode(radio, ms, rng, rtc, mgr, tables, /*is_relay=*/false)
{}

void RoomServerNode::onPeerDataRecv(mesh::Packet* packet, uint8_t type,
                                    int sender_idx, const uint8_t* secret,
                                    uint8_t* data, size_t len) {
    SimNode::onPeerDataRecv(packet, type, sender_idx, secret, data, len);

    if (type != PAYLOAD_TYPE_TXT_MSG || len <= 4) return;
    if (sender_idx < 0 || sender_idx >= (int)_search_results.size()) return;
    int idx = _search_results[sender_idx];
    Contact& sender = _contacts[idx];

    const char* raw_text = (const char*)(data + 4);
    size_t      raw_len  = len - 4;

    char esc_name[128], esc_text[256];
    json_escape(esc_name, sizeof(esc_name), sender.name.c_str(), sender.name.size());
    json_escape(esc_text, sizeof(esc_text), raw_text, raw_len);

    char pub_hex[PUB_KEY_SIZE * 2 + 1];
    bytes_to_hex(pub_hex, sender.id.pub_key, PUB_KEY_SIZE);
    char event_json[640];
    snprintf(event_json, sizeof(event_json),
             "{\"type\":\"room_post\",\"from\":\"%s\","
             "\"name\":\"%s\",\"text\":\"%s\"}",
             pub_hex, esc_name, esc_text);
    emitJson(event_json);

    char fwd[320];
    snprintf(fwd, sizeof(fwd), "[%s]: %.*s",
             sender.name.c_str(), (int)raw_len, raw_text);
    std::string fwd_str(fwd);

    for (int ci = 0; ci < (int)_contacts.size(); ci++) {
        if (ci == idx) continue;
        char dest_hex[PUB_KEY_SIZE * 2 + 1];
        bytes_to_hex(dest_hex, _contacts[ci].id.pub_key, PUB_KEY_SIZE);
        sendTextTo(std::string(dest_hex), fwd_str);
    }
}
