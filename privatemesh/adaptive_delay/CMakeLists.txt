cmake_minimum_required(VERSION 3.16)
project(adaptive_agent CXX C)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_C_STANDARD 11)

# ---------------------------------------------------------------------------
# adaptive_agent — density-adaptive transmit-delay collision mitigation.
#
# Implements the collision-mitigation proposal (Privitt et al., 2026):
#   - Counts neighbors after each advert.
#   - Looks up txdelay / direct.txdelay in a density table.
#   - Returns a random retransmit delay in [0, 5 × airtime × txdelay] ms.
#
# All sources are shared with node_agent EXCEPT SimNode.cpp/SimNode.h, which
# live here and are the only files modified for this experiment.
#
# Patch-size measurement (from repo root):
#   diff node_agent/SimNode.cpp privatemesh/adaptive_delay/SimNode.cpp \
#        | grep '^[+-]' | grep -v '^---\|^+++' | wc -l
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
    ${MESHCORE_SRC}/helpers/BaseChatMesh.cpp
    ${MESHCORE_SRC}/helpers/TxtDataHelpers.cpp
    ${MESHCORE_SRC}/helpers/AdvertDataHelpers.cpp
)

file(GLOB ED25519_SOURCES ${MESHCORE_LIB}/ed25519/*.c)
list(FILTER ED25519_SOURCES EXCLUDE REGEX "seed\\.c$")

add_executable(adaptive_agent
    ${NODE_AGENT}/main.cpp        # unchanged from node_agent
    ${NODE_AGENT}/SimRadio.cpp    # unchanged from node_agent
    ${NODE_AGENT}/SimClock.cpp    # unchanged from node_agent
    ${NODE_AGENT}/SimRNG.cpp      # unchanged from node_agent
    SimNode.cpp                   # LOCAL OVERRIDE — only this file differs
    ${CRYPTO_DIR}/SHA256.cpp
    ${CRYPTO_DIR}/AES.cpp
    ${MESHCORE_CORE_SOURCES}
    ${ED25519_SOURCES}
)

target_include_directories(adaptive_agent PRIVATE
    # Local directory first so SimNode.h resolves to our modified version.
    ${CMAKE_CURRENT_SOURCE_DIR}
    ${SHIM_DIR}
    ${CRYPTO_DIR}
    ${NODE_AGENT}             # for SimRadio.h, SimClock.h, SimRNG.h
    ${MESHCORE_SRC}
    ${MESHCORE_SRC}/helpers
    ${MESHCORE_LIB}/ed25519
)

target_compile_definitions(adaptive_agent PRIVATE
    ED25519_NO_SEED=1
    MAX_GROUP_CHANNELS=4
    MAX_NEIGHBOURS=50
)

target_compile_options(adaptive_agent PRIVATE
    -Wall -Wextra
    -Wno-unused-parameter
)

target_link_libraries(adaptive_agent PRIVATE
    OpenSSL::SSL
    OpenSSL::Crypto
)
