#include "SimNode.h"
#include <stdio.h>
#include <stdarg.h>
#include <string.h>
#include <algorithm>

#if __has_include(<helpers/DelayTuning.h>)
#define HAS_AUTOTUNE 1
#endif

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

// ---------------------------------------------------------------------------
// Constructor
// ---------------------------------------------------------------------------
SimNode::SimNode(mesh::Radio& radio, mesh::MillisecondClock& ms,
                 mesh::RNG& rng, mesh::RTCClock& rtc,
                 mesh::PacketManager& mgr, mesh::MeshTables& tables,
                 bool is_relay)
    : BaseChatMesh(radio, ms, rng, rtc, mgr, tables), _is_relay(is_relay)
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
// Routing override
// ---------------------------------------------------------------------------
bool SimNode::allowPacketForward(const mesh::Packet* /*packet*/) {
    return _is_relay;
}

// Use base Mesh::getRetransmitDelay() / getDirectRetransmitDelay() which
// respect _tx_delay_factor and _direct_tx_delay_factor — these are set
// by autoTuneByNeighborCount() in onDiscoveredContact() below.

// ---------------------------------------------------------------------------
// BaseChatMesh pure-virtual implementations
// ---------------------------------------------------------------------------

void SimNode::onDiscoveredContact(ContactInfo& contact, bool is_new,
                                   uint8_t /*path_len*/,
                                   const uint8_t* /*path*/) {
    char pub_hex[PUB_KEY_SIZE * 2 + 1];
    bytes_to_hex(pub_hex, contact.id.pub_key, PUB_KEY_SIZE);
    char json[256];
    snprintf(json, sizeof(json),
             "{\"type\":\"advert\",\"pub\":\"%s\",\"name\":\"%s\"}",
             pub_hex, contact.name);
    emitJson(json);

#if HAS_AUTOTUNE
    autoTuneByNeighborCount(getNumContacts());
#endif
}

ContactInfo* SimNode::processAck(const uint8_t* data) {
    if (!_pending_msg) return nullptr;
    uint32_t received_crc;
    memcpy(&received_crc, data, 4);
    if (received_crc == _pending_msg->expected_ack) {
        emitLog("ACK confirmed (attempt %d)", _pending_msg->attempt);
        // Look up the contact to return to BaseChatMesh
        uint8_t pub[PUB_KEY_SIZE];
        int n = hex_to_bytes(pub, _pending_msg->dest_pub_hex.c_str(),
                             _pending_msg->dest_pub_hex.size());
        _pending_msg.reset();
        if (n > 0) {
            return lookupContactByPubKey(pub, n);
        }
        return nullptr;
    }
    return nullptr;
}

void SimNode::onContactPathUpdated(const ContactInfo& contact) {
    char pub_hex[PUB_KEY_SIZE * 2 + 1];
    bytes_to_hex(pub_hex, contact.id.pub_key, PUB_KEY_SIZE);
    emitLog("path updated to %s (len=%d)", pub_hex, (int)contact.out_path_len);
}

void SimNode::onMessageRecv(const ContactInfo& contact, mesh::Packet* /*pkt*/,
                             uint32_t /*sender_timestamp*/, const char* text) {
    char pub_hex[PUB_KEY_SIZE * 2 + 1];
    bytes_to_hex(pub_hex, contact.id.pub_key, PUB_KEY_SIZE);

    // Escape quotes in text for JSON safety.
    char escaped[256];
    size_t ei = 0;
    for (const char* p = text; *p && ei < sizeof(escaped) - 2; p++) {
        if (*p == '"' || *p == '\\') escaped[ei++] = '\\';
        escaped[ei++] = *p;
    }
    escaped[ei] = '\0';

    char json[512];
    snprintf(json, sizeof(json),
             "{\"type\":\"recv_text\",\"from\":\"%s\",\"name\":\"%s\",\"text\":\"%s\"}",
             pub_hex, contact.name, escaped);
    emitJson(json);
}

void SimNode::onCommandDataRecv(const ContactInfo& contact, mesh::Packet* /*pkt*/,
                                 uint32_t /*sender_timestamp*/, const char* text) {
    char pub_hex[PUB_KEY_SIZE * 2 + 1];
    bytes_to_hex(pub_hex, contact.id.pub_key, PUB_KEY_SIZE);
    char json[512];
    snprintf(json, sizeof(json),
             "{\"type\":\"recv_data\",\"from\":\"%s\",\"payload_type\":%d,\"text\":\"%s\"}",
             pub_hex, (int)PAYLOAD_TYPE_TXT_MSG, text);
    emitJson(json);
}

void SimNode::onSignedMessageRecv(const ContactInfo& contact, mesh::Packet* pkt,
                                   uint32_t sender_timestamp,
                                   const uint8_t* /*sender_prefix*/,
                                   const char* text) {
    // Treat signed messages the same as plain for simulation purposes.
    onMessageRecv(contact, pkt, sender_timestamp, text);
}

// ---------------------------------------------------------------------------
// Timeout & retransmit constants — match companion_radio/MyMesh.cpp exactly.
// ---------------------------------------------------------------------------
static constexpr uint32_t SEND_TIMEOUT_BASE_MILLIS        = 500;
static constexpr float    FLOOD_SEND_TIMEOUT_FACTOR       = 16.0f;
static constexpr float    DIRECT_SEND_PERHOP_FACTOR       = 6.0f;
static constexpr uint32_t DIRECT_SEND_PERHOP_EXTRA_MILLIS = 250;

uint32_t SimNode::calcFloodTimeoutMillisFor(uint32_t pkt_airtime_millis) const {
    return SEND_TIMEOUT_BASE_MILLIS +
           (uint32_t)(FLOOD_SEND_TIMEOUT_FACTOR * pkt_airtime_millis);
}

uint32_t SimNode::calcDirectTimeoutMillisFor(uint32_t pkt_airtime_millis,
                                              uint8_t path_len) const {
    uint8_t path_hash_count = path_len & 63;
    return SEND_TIMEOUT_BASE_MILLIS +
           ((uint32_t)(pkt_airtime_millis * DIRECT_SEND_PERHOP_FACTOR +
                       DIRECT_SEND_PERHOP_EXTRA_MILLIS) *
            (path_hash_count + 1));
}

void SimNode::onSendTimeout() {
    if (!_pending_msg) return;
    _pending_msg->attempt++;
    if (_pending_msg->attempt > ACK_MAX_RETRIES) {
        emitLog("msg delivery failed after %d attempts", ACK_MAX_RETRIES + 1);
        _pending_msg.reset();
        return;
    }
    emitLog("ACK timeout, retry %d/%d", _pending_msg->attempt, ACK_MAX_RETRIES);

    // Look up contact for resend.
    uint8_t pub[PUB_KEY_SIZE];
    int n = hex_to_bytes(pub, _pending_msg->dest_pub_hex.c_str(),
                         _pending_msg->dest_pub_hex.size());
    if (n <= 0) { _pending_msg.reset(); return; }
    ContactInfo* target = lookupContactByPubKey(pub, n);
    if (!target) { _pending_msg.reset(); return; }

    uint32_t ts = getRTCClock()->getCurrentTimeUnique();
    uint32_t expected_ack, est_timeout;
    int result = sendMessage(*target, ts, (uint8_t)_pending_msg->attempt,
                             _pending_msg->text.c_str(),
                             expected_ack, est_timeout);
    if (result == MSG_SEND_FAILED) {
        emitLog("retry sendMessage failed");
        _pending_msg.reset();
        return;
    }
    _pending_msg->expected_ack = expected_ack;
}

void SimNode::onChannelMessageRecv(const mesh::GroupChannel& /*channel*/,
                                    mesh::Packet* /*pkt*/,
                                    uint32_t /*timestamp*/,
                                    const char* text) {
    // Escape text for JSON safety.
    char escaped[512];
    size_t ei = 0;
    for (const char* p = text; *p && ei < sizeof(escaped) - 2; p++) {
        if (*p == '"' || *p == '\\') escaped[ei++] = '\\';
        escaped[ei++] = *p;
    }
    escaped[ei] = '\0';

    char json[640];
    snprintf(json, sizeof(json),
             "{\"type\":\"recv_channel\",\"channel\":\"Public\",\"text\":\"%s\"}",
             escaped);
    emitJson(json);
}

uint8_t SimNode::onContactRequest(const ContactInfo& /*contact*/,
                                   uint32_t /*sender_timestamp*/,
                                   const uint8_t* /*data*/, uint8_t /*len*/,
                                   uint8_t* /*reply*/) {
    return 0;  // No request handling.
}

void SimNode::onContactResponse(const ContactInfo& /*contact*/,
                                 const uint8_t* /*data*/, uint8_t /*len*/) {
    // No response handling.
}

// ---------------------------------------------------------------------------
// Logging hooks
// ---------------------------------------------------------------------------
void SimNode::logRx(mesh::Packet* packet, int len, float score) {
    emitLog("RX len=%d type=%d route=%s score=%.2f",
            len, (int)packet->getPayloadType(),
            packet->isRouteDirect() ? "D" : "F", score);
}

void SimNode::logTx(mesh::Packet* packet, int len) {
    emitLog("TX len=%d type=%d route=%s rxd(%.2f) txd(%.2f) dtxd(%.2f)",
            len, (int)packet->getPayloadType(),
            packet->isRouteDirect() ? "D" : "F",
            _rx_delay_base, _tx_delay_factor, _direct_tx_delay_factor);
}

// ---------------------------------------------------------------------------
// Loop — drives BaseChatMesh (which drives Mesh which drives Dispatcher).
// ---------------------------------------------------------------------------
void SimNode::loop() {
    BaseChatMesh::loop();
}

// ---------------------------------------------------------------------------
// Application-level helpers
// ---------------------------------------------------------------------------
bool SimNode::sendTextTo(const std::string& dest_pub_hex,
                         const std::string& text) {
    // Find contact by pub-key hex prefix.
    uint8_t pub[PUB_KEY_SIZE];
    int n = hex_to_bytes(pub, dest_pub_hex.c_str(), dest_pub_hex.size());
    if (n <= 0) {
        emitLog("sendTextTo: invalid pub key hex");
        return false;
    }
    ContactInfo* target = lookupContactByPubKey(pub, n);
    if (!target) {
        emitLog("sendTextTo: unknown destination %s", dest_pub_hex.c_str());
        return false;
    }

    uint32_t ts = getRTCClock()->getCurrentTimeUnique();
    uint32_t expected_ack, est_timeout;
    int result = sendMessage(*target, ts, 0, text.c_str(),
                             expected_ack, est_timeout);
    if (result == MSG_SEND_FAILED) {
        emitLog("sendTextTo: sendMessage failed (pool exhausted?)");
        return false;
    }

    // Track for ACK matching and retry.
    _pending_msg = std::make_unique<PendingMsg>(
        PendingMsg{expected_ack, dest_pub_hex, text, 0});

    return true;
}

void SimNode::broadcastAdvert(const std::string& name) {
    mesh::Packet* pkt = createSelfAdvert(name.empty() ? "" : name.c_str());
    if (pkt) sendFlood(pkt);
}

void SimNode::setupPublicChannel(const std::string& name) {
    _node_name = name;
    addChannel("Public", "izOH6cXN6mrJ5e26oRXNcg==");
}

bool SimNode::sendChannelText(const std::string& text) {
    // Channel 0 is always the Public channel set up by setupPublicChannel().
    ChannelDetails ch;
    if (!getChannel(0, ch)) {
        emitLog("sendChannelText: no channel at index 0");
        return false;
    }
    uint32_t ts = getRTCClock()->getCurrentTimeUnique();
    return sendGroupMessage(ts, ch.channel, _node_name.c_str(),
                            text.c_str(), (int)text.size());
}

// ---------------------------------------------------------------------------
// RoomServerNode implementation
// ---------------------------------------------------------------------------

// JSON-escape a raw string into a fixed-size buffer.
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

void RoomServerNode::onMessageRecv(const ContactInfo& contact,
                                    mesh::Packet* pkt,
                                    uint32_t sender_timestamp,
                                    const char* text) {
    // Let the base class emit recv_text.
    SimNode::onMessageRecv(contact, pkt, sender_timestamp, text);

    // Emit room_post event.
    char pub_hex[PUB_KEY_SIZE * 2 + 1];
    bytes_to_hex(pub_hex, contact.id.pub_key, PUB_KEY_SIZE);
    char esc_name[128], esc_text[256];
    json_escape(esc_name, sizeof(esc_name), contact.name, strlen(contact.name));
    json_escape(esc_text, sizeof(esc_text), text, strlen(text));

    char event_json[640];
    snprintf(event_json, sizeof(event_json),
             "{\"type\":\"room_post\",\"from\":\"%s\","
             "\"name\":\"%s\",\"text\":\"%s\"}",
             pub_hex, esc_name, esc_text);
    emitJson(event_json);

    // Forward "[sender_name]: text" to every OTHER contact.
    char fwd[320];
    snprintf(fwd, sizeof(fwd), "[%s]: %s", contact.name, text);
    std::string fwd_str(fwd);

    int num = getNumContacts();
    for (int ci = 0; ci < num; ci++) {
        ContactInfo ci_info;
        if (!getContactByIdx(ci, ci_info)) continue;
        if (ci_info.id.matches(contact.id)) continue;  // don't echo to sender

        char dest_hex[PUB_KEY_SIZE * 2 + 1];
        bytes_to_hex(dest_hex, ci_info.id.pub_key, PUB_KEY_SIZE);
        sendTextTo(std::string(dest_hex), fwd_str);
    }
}
