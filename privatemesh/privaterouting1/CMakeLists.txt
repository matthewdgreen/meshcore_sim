cmake_minimum_required(VERSION 3.16)
project(privaterouting1_agent CXX C)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_C_STANDARD 11)

# ---------------------------------------------------------------------------
# privaterouting1_agent — 2-byte path hashes + PNI (Permuted Neighbor
# Identifiers) for relay path privacy.
#
# Each relay writes a fresh random PNI into the flood path instead of its
# real pub_key prefix.  On direct routing, the relay recognises both its
# real hash and all stored PNIs.  See PRIVATEROUTING1.md for the full spec.
#
# Differs from node_agent in SimNode.h (PNI table + overrides) and
# SimNode.cpp (PNI methods + 2-byte sendFlood calls).
# ---------------------------------------------------------------------------

find_package(OpenSSL REQUIRED)

set(ROOT          ${CMAKE_CURRENT_SOURCE_DIR}/../..)
set(NODE_AGENT    ${ROOT}/node_agent)
set(MESHCORE_SRC  ${ROOT}/MeshCore/src)
set(MESHCORE_LIB  ${ROOT}/MeshCore/lib)
set(SHIM_DIR      ${NODE_AGENT}/arduino_shim)
set(CRYPTO_DIR    ${NODE_AGENT}/crypto_shim)

set(MESHCORE_CORE_SOURCES
    ${MESHCORE_SRC}/Packet.cpp
    ${MESHCORE_SRC}/Dispatcher.cpp
    ${MESHCORE_SRC}/Mesh.cpp
    ${MESHCORE_SRC}/Utils.cpp
    ${MESHCORE_SRC}/Identity.cpp
    ${MESHCORE_SRC}/helpers/StaticPoolPacketManager.cpp
)

file(GLOB ED25519_SOURCES ${MESHCORE_LIB}/ed25519/*.c)
list(FILTER ED25519_SOURCES EXCLUDE REGEX "seed\\.c$")

add_executable(privaterouting1_agent
    main.cpp                      # LOCAL OVERRIDE — ensures correct SimNode.h
    ${NODE_AGENT}/SimRadio.cpp
    ${NODE_AGENT}/SimClock.cpp
    ${NODE_AGENT}/SimRNG.cpp
    SimNode.cpp                   # LOCAL OVERRIDE — PNI + 2-byte paths
    ${CRYPTO_DIR}/SHA256.cpp
    ${CRYPTO_DIR}/AES.cpp
    ${MESHCORE_CORE_SOURCES}
    ${ED25519_SOURCES}
)

target_include_directories(privaterouting1_agent PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}
    ${SHIM_DIR}
    ${CRYPTO_DIR}
    ${NODE_AGENT}
    ${MESHCORE_SRC}
    ${MESHCORE_SRC}/helpers
    ${MESHCORE_LIB}/ed25519
)

target_compile_definitions(privaterouting1_agent PRIVATE
    ED25519_NO_SEED=1
)

target_compile_options(privaterouting1_agent PRIVATE
    -Wall -Wextra
    -Wno-unused-parameter
)

target_link_libraries(privaterouting1_agent PRIVATE
    OpenSSL::SSL
    OpenSSL::Crypto
)
