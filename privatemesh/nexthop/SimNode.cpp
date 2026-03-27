#include "SimNode.h"
#include <stdio.h>
#include <stdarg.h>
#include <string.h>
#include <algorithm>

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

// Write a routing-table diagnostic log as a JSON line on stdout.
// Uses the same wire format as SimNode::emitLog but is callable from
// file-scope static functions that don't have a SimNode* in scope.
static void rt_log(const char* fmt, ...) {
    char msg[256];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(msg, sizeof(msg), fmt, ap);
    va_end(ap);
    fprintf(stdout, "{\"type\":\"log\",\"msg\":\"%s\"}\n", msg);
    fflush(stdout);
}

// ---------------------------------------------------------------------------
// Proactive next-hop routing table  (experiment: nexthop)
//
// Design overview
// ---------------
// During the advert-flood warmup period every node broadcasts its identity to
// the network.  For each received advert this node records a RouteEntry that
// maps a 4-byte destination prefix to the shortest reversed relay path needed
// by sendDirect(), plus bookkeeping fields for expiry, usage tracking, and
// the pinned flag.
//
// When sendTextTo() is called and a useful multi-hop entry is found the packet
// is sent directly — no flood→path-exchange round-trip required.  Destinations
// whose paths exceed RT_PATH_CAP are stored as metric-only entries (path_bytes=0)
// that still contribute to the flood-suppression horizon but are never used for
// sendDirect.
//
// If rt_find() returns nullptr or path_bytes==0 the node falls back to the
// standard node_agent behaviour (path-exchange direct or flood), preserving
// full backward compatibility.
//
// Memory budget: RT_MAX_ROUTES × sizeof(RouteEntry) = 128 × 73 = 9344 bytes.
// ---------------------------------------------------------------------------

// ---- Compile-time parameters (see nexthop/README.md for rationale) --------
#define RT_MAX_ROUTES     128   // table slots; must exceed expected network size
#define RT_MAX_AGE          3   // expire after this many age cycles without refresh
#define RT_PATH_CAP        64   // max path bytes stored per entry (= MAX_PATH_SIZE)
#define RT_AGE_EVERY       16   // age entries every N adverts received
#define RT_ADMIT_PER_CYCLE 16   // Sybil rate limit: max NEW entries per age cycle

// ---- Pinned bit encoding in the age field ---------------------------------
// Bit 7 of RouteEntry::age is the pinned flag; bits [6:0] carry the counter.
// RT_MAX_AGE=3 fits in 2 bits, leaving bits [6:2] spare.
#define RT_PINNED_BIT  0x80u
#define RT_AGE_MASK    0x7Fu

// ---- RouteEntry struct (73 bytes each) ------------------------------------
struct RouteEntry {
    uint8_t dest[4];            // first 4 bytes of destination public key
    uint8_t path[RT_PATH_CAP];  // reversed relay path bytes (for sendDirect)
    uint8_t path_bytes;         // byte count in path[] (0 = metric-only entry)
    uint8_t hash_sz;            // bytes per relay hash (normally 1)
    uint8_t metric;             // hop count to destination (1 = direct neighbour)
    uint8_t age;                // bit[7]=pinned; bits[6:0]=age counter
    uint8_t use_count;          // times this route was used for sendDirect (sat. 255)
};

static RouteEntry rt_table[RT_MAX_ROUTES];
static uint8_t    rt_count            = 0;   // live entries
static uint32_t   rt_advert_seen      = 0;   // total adverts received (drives aging)
static uint8_t    rt_admits_this_cycle = 0;  // new entries in current age window

// ---- Table helpers --------------------------------------------------------

// Find entry whose first 4 key bytes match dest[0..3], or nullptr.
static RouteEntry* rt_find(const uint8_t* dest) {
    for (uint8_t i = 0; i < rt_count; i++) {
        if (memcmp(rt_table[i].dest, dest, 4) == 0)
            return &rt_table[i];
    }
    return nullptr;
}

// Increment all non-pinned age counters; evict entries that exceed RT_MAX_AGE;
// reset the Sybil admission window for the new cycle.
static void rt_age_entries() {
    rt_admits_this_cycle = 0;
    uint8_t i = 0;
    while (i < rt_count) {
        if (rt_table[i].age & RT_PINNED_BIT) { i++; continue; }  // pinned: skip
        uint8_t new_age = (rt_table[i].age & RT_AGE_MASK) + 1;
        if (new_age > RT_MAX_AGE) {
            rt_table[i] = rt_table[--rt_count];  // evict: swap with tail
        } else {
            rt_table[i].age = new_age;            // keep pinned bit clear
            i++;
        }
    }
}

// Insert or refresh a route.
//   - Existing entry: update if new metric is equal or better; reset age.
//   - New entry: admit if below Sybil rate limit and space available.
//   - Table full: evict using priority order (see nexthop/README.md).
static void rt_upsert(const uint8_t* dest,
                      const uint8_t* rev_path, uint8_t path_bytes,
                      uint8_t hash_sz, uint8_t metric) {
    // Update existing entry.
    RouteEntry* e = rt_find(dest);
    if (e) {
        if (metric <= e->metric) {
            uint8_t n = std::min(path_bytes, (uint8_t)RT_PATH_CAP);
            memcpy(e->path, rev_path, n);
            e->path_bytes = n;
            e->hash_sz    = hash_sz;
            e->metric     = metric;
            e->age        = e->age & RT_PINNED_BIT;  // reset age, keep pinned
        }
        return;
    }

    // Sybil admission rate limit: cap new entries per age cycle.
    if (rt_admits_this_cycle >= RT_ADMIT_PER_CYCLE) {
        char dhex[9]; bytes_to_hex(dhex, dest, 4);
        rt_log("rt: admission limit, dropping %.8s", dhex);
        return;
    }

    // Insert into a free slot.
    if (rt_count < RT_MAX_ROUTES) {
        RouteEntry& ne = rt_table[rt_count++];
        memcpy(ne.dest, dest, 4);
        uint8_t n  = std::min(path_bytes, (uint8_t)RT_PATH_CAP);
        memcpy(ne.path, rev_path, n);
        ne.path_bytes  = n;
        ne.hash_sz     = hash_sz;
        ne.metric      = metric;
        ne.age         = 0;
        ne.use_count   = 0;
        rt_admits_this_cycle++;
        return;
    }

    // Table full — find the best eviction candidate.
    // Priority (worst = first to evict):
    //   1. Never evict pinned entries.
    //   2. Evict metric-only / useless entries (path_bytes=0) first.
    //   3. Among useless: stalest wins.
    //   4. Among useful: evict use_count=0 before used entries.
    //   5. Among used: evict stalest.
    uint8_t worst = RT_MAX_ROUTES;  // sentinel: no candidate yet
    for (uint8_t i = 0; i < rt_count; i++) {
        if (rt_table[i].age & RT_PINNED_BIT) continue;
        if (worst == RT_MAX_ROUTES) { worst = i; continue; }

        bool i_useless = (rt_table[i].path_bytes == 0);
        bool w_useless = (rt_table[worst].path_bytes == 0);
        uint8_t i_age  = rt_table[i].age   & RT_AGE_MASK;
        uint8_t w_age  = rt_table[worst].age & RT_AGE_MASK;

        if (i_useless && !w_useless) {
            worst = i;  // always prefer evicting a useless entry
        } else if (i_useless == w_useless) {
            if (!i_useless) {
                // Both useful: prefer unused (use_count=0), then stalest.
                if (rt_table[worst].use_count > 0 && rt_table[i].use_count == 0)
                    worst = i;
                else if (rt_table[i].use_count == rt_table[worst].use_count
                         && i_age > w_age)
                    worst = i;
            } else {
                if (i_age > w_age) worst = i;  // both useless: stalest
            }
        }
    }

    if (worst == RT_MAX_ROUTES) return;  // every entry is pinned; cannot evict

    // Only evict for a useful incoming route.
    if (path_bytes == 0) return;

    // Warn when displacing a useful (used) entry.
    if (rt_table[worst].path_bytes > 0 && rt_table[worst].use_count > 0) {
        char dhex[9]; bytes_to_hex(dhex, rt_table[worst].dest, 4);
        rt_log("rt: table full, evicting %.8s (metric=%d use=%d)",
               dhex, (int)rt_table[worst].metric,
               (int)rt_table[worst].use_count);
    }

    memcpy(rt_table[worst].dest, dest, 4);
    uint8_t n = std::min(path_bytes, (uint8_t)RT_PATH_CAP);
    memcpy(rt_table[worst].path, rev_path, n);
    rt_table[worst].path_bytes  = n;
    rt_table[worst].hash_sz     = hash_sz;
    rt_table[worst].metric      = metric;
    rt_table[worst].age         = 0;
    rt_table[worst].use_count   = 0;
    rt_admits_this_cycle++;
}

// ---------------------------------------------------------------------------
// Constructor
// ---------------------------------------------------------------------------
SimNode::SimNode(mesh::Radio& radio, mesh::MillisecondClock& ms,
                 mesh::RNG& rng, mesh::RTCClock& rtc,
                 mesh::PacketManager& mgr, mesh::MeshTables& tables,
                 bool is_relay)
    : mesh::Mesh(radio, ms, rng, rtc, mgr, tables), _is_relay(is_relay)
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
// Routing overrides
// ---------------------------------------------------------------------------

// allowPacketForward — standard relay behaviour (Strategy C disabled).
//
// Strategy C was an attempt to suppress flood re-transmission at relays whose
// routing horizon has been surpassed by the packet's hop count.  The design
// was fundamentally flawed: "horizon" was computed as the max distance from
// *this relay* to its farthest known destination, but the suppression condition
// `hops >= horizon` compares against distance from the *source* to this relay —
// two different coordinate origins.  Example: relay n_9_4 is 5 hops from
// destination n_9_9 (horizon ≈ 6), but a corner-to-corner flood arrives with
// hops=13.  13 ≥ 6 → suppressed — yet the destination is only 5 hops away.
//
// A correct metric-horizon design would need either:
//   (a) the source's position/identity (not available; packet is encrypted), or
//   (b) a global "network diameter" constant, or
//   (c) a position-agnostic bound such as `hops < RT_MAX_PATH_HOPS` where
//       RT_MAX_PATH_HOPS is a deployment-time estimate of the network diameter.
//
// For now we match the baseline node_agent behaviour exactly.
// Flood suppression can be re-introduced once the geometry is correct.
bool SimNode::allowPacketForward(const mesh::Packet* packet) {
    (void)packet;
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

    // -----------------------------------------------------------------------
    // Path exchange — triggered on BOTH flood and direct-routed TXT_MSG.
    //
    // Standard node_agent only triggers on flood packets (isRouteFlood()).
    // This node also triggers on direct-routed first messages so that the
    // receiver learns the sender's forward path immediately, closing the
    // one-round reply-path asymmetry that arises when the nexthop sender
    // uses its routing table to skip the initial flood.
    // -----------------------------------------------------------------------
    if (type == PAYLOAD_TYPE_TXT_MSG
        && (packet->isRouteFlood() || packet->isRouteDirect())  // ← key change
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
    if (sender_idx < 0 || sender_idx >= (int)_search_results.size()) {
        return false;
    }
    int idx = _search_results[sender_idx];
    Contact& c = _contacts[idx];

    c.has_path = true;
    c.path.assign(path, path + path_len);

    char pub_hex[PUB_KEY_SIZE * 2 + 1];
    bytes_to_hex(pub_hex, c.id.pub_key, PUB_KEY_SIZE);
    emitLog("path learned to %s (len=%d)", pub_hex, (int)path_len);

    return false;
}

void SimNode::onAdvertRecv(mesh::Packet* packet, const mesh::Identity& id,
                           uint32_t /*timestamp*/,
                           const uint8_t* app_data, size_t app_data_len) {
    if (id.matches(self_id)) return;

    // Standard contact-list update (unchanged from node_agent).
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

    // --- routing table update ---
    // The advert packet's path field is [r1, r2, ..., rN] (origin → us).
    // sendDirect needs the reversed sequence [rN, ..., r2, r1] (nearest first).
    uint8_t hash_sz       = packet->getPathHashSize();
    uint8_t hash_cnt      = packet->getPathHashCount();
    uint8_t metric        = hash_cnt + 1;          // 1 = direct neighbour
    uint8_t full_path_bytes = hash_cnt * hash_sz;

    uint8_t rev_path[RT_PATH_CAP] = {};
    uint8_t rev_bytes = 0;

    if (full_path_bytes > RT_PATH_CAP) {
        // Path too long to store completely.  Saving a truncated path would
        // cause sendDirect to drop packets midway — store metric only so the
        // horizon calculation in allowPacketForward remains accurate.
        emitLog("rt: path to %.8s too long (%d bytes > cap %d), storing metric only",
                pub_hex, (int)full_path_bytes, (int)RT_PATH_CAP);
        rev_bytes = 0;   // metric-only entry; falls back to flood in sendTextTo
    } else {
        uint8_t copy_cnt = hash_cnt;
        for (uint8_t i = 0; i < copy_cnt; i++) {
            uint8_t src_off = (hash_cnt - 1 - i) * hash_sz;
            uint8_t dst_off = i * hash_sz;
            memcpy(rev_path + dst_off, packet->path + src_off, hash_sz);
            rev_bytes += hash_sz;
        }
    }

    // Age all entries every RT_AGE_EVERY adverts received.
    if ((++rt_advert_seen % RT_AGE_EVERY) == 0)
        rt_age_entries();

    rt_upsert(id.pub_key, rev_path, rev_bytes, hash_sz, metric);
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

    // --- routing table lookup ---
    // Use a proactively cached multi-hop path from the advert flood.
    // Conditions for table-direct send:
    //   • entry exists with matching 4-byte prefix
    //   • it is a multi-hop destination (metric > 1, i.e. not a direct neighbour)
    //   • the path bytes are stored (path_bytes > 0, i.e. not a metric-only entry)
    RouteEntry* r = rt_find(target->id.pub_key);
    if (r && r->metric > 1 && r->path_bytes > 0) {
        sendDirect(pkt, r->path, r->path_bytes);
        if (r->use_count < 255) r->use_count++;  // track route utilisation
        emitLog("nexthop: table-direct to %.8s (metric=%d age=%d use=%d)",
                dest_pub_hex.c_str(), (int)r->metric,
                (int)(r->age & RT_AGE_MASK), (int)r->use_count);
        return true;
    }

    // Fallback: standard path-exchange direct or flood.
    if (target->has_path && !target->path.empty()) {
        sendDirect(pkt, target->path.data(), (uint8_t)target->path.size());
    } else {
        sendFlood(pkt);
    }
    return true;
}

// ---------------------------------------------------------------------------
// RoomServerNode implementation
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
    json_escape(esc_name, sizeof(esc_name),
                sender.name.c_str(), sender.name.size());
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
    // Stub: nexthop experiment uses mesh::Mesh (not BaseChatMesh).
    // Group channels require BaseChatMesh — a future upgrade.
}

bool SimNode::sendChannelText(const std::string& /*text*/) {
    emitLog("sendChannelText: not supported (experiment agent)");
    return false;
}

void SimNode::loop() {
    mesh::Mesh::loop();
}
