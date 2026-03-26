#pragma once
#include <Dispatcher.h>
#include <queue>
#include <vector>
#include <cstdint>

// A single raw packet sitting in the inbound queue.
struct IncomingPacket {
    std::vector<uint8_t> data;
    float snr;
    float rssi;
};

// Simulator Radio implementation.
//
// recvRaw()      – pops one packet from the inbound queue populated by enqueue().
// startSendRaw() – writes a JSON "tx" line to stdout and returns immediately
//                  (the orchestrator decides if/when neighbours receive it).
// isSendComplete()– returns true only after the estimated on-air time has
//                  elapsed (wall-clock), enabling the Dispatcher's duty-cycle
//                  enforcement and realistic half-duplex timing.
class SimRadio : public mesh::Radio {
    std::queue<IncomingPacket> _rx_queue;
    float _last_snr   = 0.0f;
    float _last_rssi  = -100.0f;
    bool  _tx_pending = false;   // set by startSendRaw, cleared by isSendComplete

    // Listen-Before-Talk: set by notifyRxStart(), cleared when millis passes
    // the end time.  Simulates preamble detection on the LoRa demodulator.
    unsigned long _rx_active_until = 0;

    // LoRa radio parameters for airtime calculation (Semtech AN1200.13).
    int _sf;      // spreading factor (7–12)
    int _bw_hz;   // bandwidth in Hz
    int _cr;      // coding-rate offset (1=CR4/5 … 4=CR4/8)

    // Clock for realistic TX timing — isSendComplete() waits until the
    // estimated on-air time has elapsed before returning true.
    mesh::MillisecondClock& _ms;
    unsigned long _tx_done_at = 0;

public:
    // Construct with LoRa parameters and clock.
    // Defaults: EU Narrow (SF8/BW62.5/CR4-8).
    SimRadio(mesh::MillisecondClock& ms,
             int sf = 8, int bw_hz = 62500, int cr = 4);

    // Called by main.cpp to deliver a packet from the orchestrator.
    void enqueue(const uint8_t* data, int len, float snr, float rssi);

    // Called by main.cpp on rx_start: marks the radio as receiving for
    // duration_ms, so Dispatcher::checkSend() defers TX (Listen-Before-Talk).
    void notifyRxStart(uint32_t duration_ms);

    // ---- mesh::Radio interface ----
    int      recvRaw(uint8_t* bytes, int sz)         override;
    uint32_t getEstAirtimeFor(int len_bytes)          override;
    float    packetScore(float snr, int packet_len)   override;
    bool     startSendRaw(const uint8_t* bytes, int len) override;
    bool     isSendComplete()                         override;
    void     onSendFinished()                         override {}
    bool     isInRecvMode() const                     override { return !_tx_pending; }
    bool     isReceiving()                            override;
    float    getLastSNR()  const                      override { return _last_snr;  }
    float    getLastRSSI() const                      override { return _last_rssi; }
};
