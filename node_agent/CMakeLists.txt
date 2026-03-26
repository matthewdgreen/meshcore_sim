cmake_minimum_required(VERSION 3.16)
project(meshcore_node_agent CXX C)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_C_STANDARD 11)

# ---------------------------------------------------------------------------
# Dependency: OpenSSL (for SHA-256, HMAC-SHA-256, AES-128-ECB)
# ---------------------------------------------------------------------------
find_package(OpenSSL REQUIRED)

# ---------------------------------------------------------------------------
# Path aliases
# ---------------------------------------------------------------------------
set(ROOT          ${CMAKE_CURRENT_SOURCE_DIR}/..)
set(MESHCORE_SRC  ${ROOT}/MeshCore/src)
set(MESHCORE_LIB  ${ROOT}/MeshCore/lib)
set(SHIM_DIR      ${CMAKE_CURRENT_SOURCE_DIR}/arduino_shim)
set(CRYPTO_DIR    ${CMAKE_CURRENT_SOURCE_DIR}/crypto_shim)

# ---------------------------------------------------------------------------
# MeshCore core sources (no Arduino/hardware deps when compiled with our shims)
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# lib/ed25519 – standalone portable C (orlp/ed25519)
# seed.c uses platform entropy; we supply our own via SimRNG, so exclude it.
# ---------------------------------------------------------------------------
file(GLOB ED25519_SOURCES ${MESHCORE_LIB}/ed25519/*.c)
list(FILTER ED25519_SOURCES EXCLUDE REGEX "seed\\.c$")

# ---------------------------------------------------------------------------
# node_agent executable
# ---------------------------------------------------------------------------
add_executable(node_agent
    main.cpp
    SimRadio.cpp
    SimClock.cpp
    SimRNG.cpp
    SimNode.cpp
    ${CRYPTO_DIR}/SHA256.cpp
    ${CRYPTO_DIR}/AES.cpp
    ${MESHCORE_CORE_SOURCES}
    ${ED25519_SOURCES}
)

target_include_directories(node_agent PRIVATE
    # Our Arduino and crypto shims (searched first, before any system headers)
    ${SHIM_DIR}
    ${CRYPTO_DIR}
    # MeshCore headers
    ${MESHCORE_SRC}
    ${MESHCORE_SRC}/helpers
    ${MESHCORE_LIB}/ed25519
    # This directory (for SimRadio.h etc.)
    ${CMAKE_CURRENT_SOURCE_DIR}
)

target_compile_definitions(node_agent PRIVATE
    ED25519_NO_SEED=1       # We don't need the ed25519 platform seed function
    MAX_GROUP_CHANNELS=4    # Enable group channel support in BaseChatMesh
    # MESH_DEBUG=0          # Uncomment to silence all MESH_DEBUG_PRINT calls
)

target_compile_options(node_agent PRIVATE
    -Wall -Wextra
    # MeshCore uses C-style casts and other patterns that generate noise:
    -Wno-unused-parameter
)

target_link_libraries(node_agent PRIVATE
    OpenSSL::SSL
    OpenSSL::Crypto
)

# ---------------------------------------------------------------------------
# Convenience target: build and print binary path
# ---------------------------------------------------------------------------
add_custom_target(print_path
    COMMAND ${CMAKE_COMMAND} -E echo "Binary: $<TARGET_FILE:node_agent>"
    DEPENDS node_agent
)
