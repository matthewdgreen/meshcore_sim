#pragma once
#include <Mesh.h>
#include <vector>
#include <string>
#include <cstdint>

// A known peer whose Advert we have received.
struct Contact {
    mesh::Identity  id;
    uint8_t         shared_secret[PUB_KEY_SIZE];  // ECDH with our private key
    bool            has_path   = false;
    std::vector<uint8_t> path; // direct path bytes (if known)
    std::string     name;      // from advert app_data (null-terminated string)
};

// ---------------------------------------------------------------------------
// SimNode — density-adaptive transmit-delay experiment.
//
// This variant implements the collision-mitigation proposal from:
//   "An Automatic and Optimized Collision Mitigation Strategy Using the
//    Existing Timing Randomization Role of rxdelay, txdelay and
//    direct.txdelay in MeshCore Routing"  (Privitt et al., 2026)
//
// Core mechanism
// --------------
// After each advert is received, the node counts its current neighbors
// (= contacts) and looks up a density-class in a table.  The table maps
// neighbor count to two timing multipliers:
//
//   txdelay        — scales the flood retransmit window
//   direct.txdelay — scales the direct/unicast retransmit window
//
// getRetransmitDelay() returns a uniform random delay in the range
//   [0,  5 × LORA_AIRTIME_MS × txdelay]          (flood)
//   [0,  5 × LORA_AIRTIME_MS × direct.txdelay]   (direct)
//
// The proposal's reasoning: probability of two simultaneous retransmissions
// ≈ 1 / (5 × txdelay), so a txdelay of 1.0 → ~20% collision probability
// between any pair, which drops rapidly with increasing txdelay.
//
// Comparison with node_agent baseline
// -------------------------------------
// The unmodified node_agent always returns 0 from getRetransmitDelay(), so
// every relay retransmits immediately.  Under the RF contention model this
// produces a collision storm.  This variant spreads retransmissions across
// a window proportional to neighbor density, dramatically reducing collisions.
// ---------------------------------------------------------------------------

// Estimated on-air time for a typical MeshCore packet at SF10/BW250 kHz/CR4-5.
// Matches the MeshCore default modulation (simple_repeater/MyMesh.cpp).
// Used to scale timing slots into wall-clock milliseconds.
#define LORA_AIRTIME_MS  330.0f

class SimNode : public mesh::Mesh {
    bool _is_relay;

    // Adaptive timing state.
    float _txdelay;          // current flood txdelay multiplier (from table)
    float _direct_txdelay;   // current direct txdelay multiplier (from table)
    int   _prev_neighbor_count;  // last count at which tuning was updated

    // Re-evaluate timing whenever neighbor count changes.
    void _update_timing();

protected:
    // Contact list and peer-search scratch space — accessible to subclasses.
    std::vector<Contact> _contacts;
    std::vector<int>     _search_results;

    // Emit a JSON log line to stdout (does NOT interfere with tx lines).
    void emitLog(const char* fmt, ...) const;
    // Emit an arbitrary JSON object line.
    void emitJson(const char* json) const;

    // ---- mesh::Mesh overrides ----
    bool     allowPacketForward(const mesh::Packet* packet) override;
    int      searchPeersByHash(const uint8_t* hash) override;
    void     getPeerSharedSecret(uint8_t* dest_secret, int peer_idx) override;

    // Returns a density-adaptive random delay in milliseconds.
    // Flood packets use _txdelay; direct packets use _direct_txdelay.
    uint32_t getRetransmitDelay(const mesh::Packet* packet) override;

    void  onPeerDataRecv(mesh::Packet* packet, uint8_t type,
                         int sender_idx, const uint8_t* secret,
                         uint8_t* data, size_t len) override;

    bool  onPeerPathRecv(mesh::Packet* packet, int sender_idx,
                         const uint8_t* secret,
                         uint8_t* path, uint8_t path_len,
                         uint8_t extra_type, uint8_t* extra,
                         uint8_t extra_len) override;

    void  onAdvertRecv(mesh::Packet* packet, const mesh::Identity& id,
                       uint32_t timestamp,
                       const uint8_t* app_data, size_t app_data_len) override;

    void  onAckRecv(mesh::Packet* packet, uint32_t ack_crc) override;

    void  logRx(mesh::Packet* packet, int len, float score) override;
    void  logTx(mesh::Packet* packet, int len) override;

public:
    SimNode(mesh::Radio& radio, mesh::MillisecondClock& ms,
            mesh::RNG& rng, mesh::RTCClock& rtc,
            mesh::PacketManager& mgr, mesh::MeshTables& tables,
            bool is_relay);

    virtual ~SimNode() = default;

    // ---- Application-level commands (called from main.cpp) ----
    bool sendTextTo(const std::string& dest_pub_hex, const std::string& text);
    void broadcastAdvert(const std::string& name = "");

    // Group channel support (stub — experiment agents use mesh::Mesh, not BaseChatMesh)
    void setupPublicChannel(const std::string& name);
    bool sendChannelText(const std::string& text);

    // Drive Dispatcher timing (BaseChatMesh loop equivalent).
    void loop();
};

// RoomServerNode is included for binary compatibility with main.cpp.
// It is not used in the adaptive_delay experiment but must be present
// because main.cpp references it.
class RoomServerNode : public SimNode {
protected:
    void onPeerDataRecv(mesh::Packet* packet, uint8_t type,
                        int sender_idx, const uint8_t* secret,
                        uint8_t* data, size_t len) override;

public:
    RoomServerNode(mesh::Radio& radio, mesh::MillisecondClock& ms,
                   mesh::RNG& rng, mesh::RTCClock& rtc,
                   mesh::PacketManager& mgr, mesh::MeshTables& tables);
};
