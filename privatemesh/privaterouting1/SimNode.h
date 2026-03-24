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
// SimNode — privaterouting1: 2-byte path hashes + PNI (Permuted Neighbor
// Identifiers) for relay path privacy.
//
// On each flood forward, the relay writes a fresh random PNI into the path
// instead of its real pub_key prefix.  On direct routing, the relay checks
// both its real hash and all stored PNIs.
//
// See PRIVATEROUTING1.md for the full spec.
// ---------------------------------------------------------------------------

class SimNode : public mesh::Mesh {
    bool _is_relay;

    // ---- PNI table (fixed-size ring buffer, FIFO eviction) ----
    static constexpr int PNI_TABLE_CAP = 128;
    struct PNIEntry {
        uint8_t pni[3];    // 1–3 bytes depending on hash_size
        uint8_t sz;         // actual size used
    };
    PNIEntry _pni_table[PNI_TABLE_CAP];
    int      _pni_count = 0;
    int      _pni_head  = 0;   // next insertion index

    bool pniExists(const uint8_t* pni, uint8_t sz) const;
    bool pniLookup(const uint8_t* hash, uint8_t sz) const;
    void storePNI(const uint8_t* pni, uint8_t sz);

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

    // Return zero retransmit jitter (same as node_agent baseline).
    uint32_t getRetransmitDelay(const mesh::Packet* packet) override { return 0; }

    // ---- PNI overrides ----
    void writeSelfPathHash(uint8_t* dest, uint8_t sz) override;
    bool isSelfPathHash(const uint8_t* hash, uint8_t sz) const override;

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
};

// RoomServerNode — included for binary compatibility with main.cpp.
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
