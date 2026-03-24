// privaterouting1/main.cpp — thin wrapper around the shared main.cpp.
//
// Forces #include "SimNode.h" to resolve to the LOCAL header (with PNI
// table) rather than node_agent/SimNode.h (base class only).  Without this
// wrapper, make_unique<SimNode> allocates sizeof(base-SimNode) bytes but
// the constructor writes sizeof(privaterouting1-SimNode) → heap overflow.

// Include OUR SimNode.h first (has PNI fields, correct sizeof).
#include "SimNode.h"

// Define the base header's include guard so the #include "SimNode.h" inside
// node_agent/main.cpp becomes a no-op (it would otherwise find the base
// header via source-file-relative lookup and create an ODR violation).
#define SIMNODE_H_GUARD

// Pull in the full shared implementation.
#include "../../node_agent/main.cpp"
