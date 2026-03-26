#pragma once
#include <helpers/BaseChatMesh.h>
#include <string>
#include <cstdint>
#include <memory>

// Concrete BaseChatMesh subclass used by the simulator.
//
// Inherits the full MeshCore chat stack: contact management, ACK tracking,
// timeout/retry, path exchange, and group channel support.  The pure-virtual
// "UI" hooks emit newline-delimited JSON to stdout for the orchestrator.
//
// Routing behaviour is controlled by the --relay flag at startup:
//   relay       – forwards flood packets it hasn't seen before (repeater node)
//   endpoint    – does not forward packets (leaf node)
//   room-server – endpoint that re-broadcasts received TXT_MSG to all contacts
class SimNode : public BaseChatMesh {
    bool _is_relay;
    std::string _node_name;  // stored for sendGroupMessage sender_name

    // Pending message for ACK tracking and retry.
    struct PendingMsg {
        uint32_t expected_ack;
        std::string dest_pub_hex;
        std::string text;
        int attempt;
    };
    std::unique_ptr<PendingMsg> _pending_msg;

    static constexpr int ACK_MAX_RETRIES = 3;

protected:
    // Emit a JSON log line to stdout.
    void emitLog(const char* fmt, ...) const;
    // Emit an arbitrary JSON object line.
    void emitJson(const char* json) const;

    // ---- Mesh routing overrides ----
    bool allowPacketForward(const mesh::Packet* packet) override;
    uint32_t getRetransmitDelay(const mesh::Packet* packet) override;
    uint32_t getDirectRetransmitDelay(const mesh::Packet* packet) override;

    // ---- BaseChatMesh "UI" pure virtuals ----
    void onDiscoveredContact(ContactInfo& contact, bool is_new,
                             uint8_t path_len, const uint8_t* path) override;
    ContactInfo* processAck(const uint8_t* data) override;
    void onContactPathUpdated(const ContactInfo& contact) override;
    void onMessageRecv(const ContactInfo& contact, mesh::Packet* pkt,
                       uint32_t sender_timestamp, const char* text) override;
    void onCommandDataRecv(const ContactInfo& contact, mesh::Packet* pkt,
                           uint32_t sender_timestamp, const char* text) override;
    void onSignedMessageRecv(const ContactInfo& contact, mesh::Packet* pkt,
                             uint32_t sender_timestamp,
                             const uint8_t* sender_prefix,
                             const char* text) override;
    uint32_t calcFloodTimeoutMillisFor(uint32_t pkt_airtime_millis) const override;
    uint32_t calcDirectTimeoutMillisFor(uint32_t pkt_airtime_millis,
                                        uint8_t path_len) const override;
    void onSendTimeout() override;
    void onChannelMessageRecv(const mesh::GroupChannel& channel,
                              mesh::Packet* pkt, uint32_t timestamp,
                              const char* text) override;
    uint8_t onContactRequest(const ContactInfo& contact,
                             uint32_t sender_timestamp,
                             const uint8_t* data, uint8_t len,
                             uint8_t* reply) override;
    void onContactResponse(const ContactInfo& contact,
                           const uint8_t* data, uint8_t len) override;

    // ---- Logging hooks ----
    void logRx(mesh::Packet* packet, int len, float score) override;
    void logTx(mesh::Packet* packet, int len) override;

public:
    SimNode(mesh::Radio& radio, mesh::MillisecondClock& ms,
            mesh::RNG& rng, mesh::RTCClock& rtc,
            mesh::PacketManager& mgr, mesh::MeshTables& tables,
            bool is_relay);

    virtual ~SimNode() = default;

    // ---- Application-level commands (called from main.cpp) ----

    // Send an encrypted text message to a known contact (looked up by pub-key hex prefix).
    // Uses BaseChatMesh::sendMessage() for real ACK tracking and retry.
    bool sendTextTo(const std::string& dest_pub_hex, const std::string& text);

    // Flood-broadcast an Advertisement from this node.
    void broadcastAdvert(const std::string& name = "");

    // Register the default public group channel ("Public") and store the node name.
    void setupPublicChannel(const std::string& name);

    // Send a text message on the public group channel (flood broadcast to all nodes).
    bool sendChannelText(const std::string& text);

    // Non-virtual loop that drives BaseChatMesh + ACK timeout.
    void loop();
};

// ---------------------------------------------------------------------------
// RoomServerNode — a non-relay endpoint that re-broadcasts every received
// TXT_MSG to all other known contacts, acting as a simple message hub.
// ---------------------------------------------------------------------------
class RoomServerNode : public SimNode {
protected:
    void onMessageRecv(const ContactInfo& contact, mesh::Packet* pkt,
                       uint32_t sender_timestamp, const char* text) override;

public:
    RoomServerNode(mesh::Radio& radio, mesh::MillisecondClock& ms,
                   mesh::RNG& rng, mesh::RTCClock& rtc,
                   mesh::PacketManager& mgr, mesh::MeshTables& tables);
};
