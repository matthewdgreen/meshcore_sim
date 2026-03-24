cmake_minimum_required(VERSION 3.16)
project(path2_agent CXX C)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_C_STANDARD 11)

# ---------------------------------------------------------------------------
# path2_agent — 2-byte path hashes (no PNI).
#
# Identical to node_agent except floods are originated with
# path_hash_size=2 instead of the default 1.  This halves collision
# probability (1/65536 vs 1/256) and is the control for the PNI experiment.
#
# Only SimNode.cpp differs from node_agent (3 sendFlood call sites changed).
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

add_executable(path2_agent
    ${NODE_AGENT}/main.cpp
    ${NODE_AGENT}/SimRadio.cpp
    ${NODE_AGENT}/SimClock.cpp
    ${NODE_AGENT}/SimRNG.cpp
    SimNode.cpp                   # LOCAL OVERRIDE — 2-byte sendFlood calls
    ${CRYPTO_DIR}/SHA256.cpp
    ${CRYPTO_DIR}/AES.cpp
    ${MESHCORE_CORE_SOURCES}
    ${ED25519_SOURCES}
)

target_include_directories(path2_agent PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}
    ${SHIM_DIR}
    ${CRYPTO_DIR}
    ${NODE_AGENT}
    ${MESHCORE_SRC}
    ${MESHCORE_SRC}/helpers
    ${MESHCORE_LIB}/ed25519
)

target_compile_definitions(path2_agent PRIVATE
    ED25519_NO_SEED=1
)

target_compile_options(path2_agent PRIVATE
    -Wall -Wextra
    -Wno-unused-parameter
)

target_link_libraries(path2_agent PRIVATE
    OpenSSL::SSL
    OpenSSL::Crypto
)
