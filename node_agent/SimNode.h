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

// Concrete Mesh subclass used by the simulator.
//
// Routing behaviour is controlled by the --relay flag at startup:
//   relay       – forwards flood packets it hasn't seen before (repeater node)
//   endpoint    – does not forward packets (leaf node)
//   room-server – endpoint that re-broadcasts received TXT_MSG to all contacts
//
// All notable events (received messages, new adverts, tx/rx) are reported to
// the orchestrator as newline-delimited JSON written to stdout.
class SimNode : public mesh::Mesh {
    bool _is_relay;

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

    // Return zero retransmit jitter so flood propagation is fast and
    // deterministic in simulation (no random multi-second delays).
    uint32_t getRetransmitDelay(const mesh::Packet* packet) override { return 0; }

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

    // Send an encrypted text message to a known contact (looked up by pub-key hex prefix).
    // Returns false if the destination is not in our contact list.
    bool sendTextTo(const std::string& dest_pub_hex, const std::string& text);

    // Flood-broadcast an Advertisement from this node.
    // name is embedded as the app_data (max 31 bytes, null terminated).
    void broadcastAdvert(const std::string& name = "");
};

// ---------------------------------------------------------------------------
// RoomServerNode — a non-relay endpoint that re-broadcasts every received
// TXT_MSG to all other known contacts, acting as a simple message hub.
//
// On receipt of a TXT_MSG it:
//   1. Calls the SimNode base handler (emits recv_text, handles path exchange).
//   2. Emits a "room_post" event so the orchestrator can present the message.
//   3. Forwards "[sender_name]: text" to every other contact.
// ---------------------------------------------------------------------------
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
