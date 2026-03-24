// adaptive_delay/main.cpp — thin wrapper around the shared main.cpp.
//
// Forces #include "SimNode.h" to resolve to the LOCAL header (with adaptive
// timing fields) rather than node_agent/SimNode.h (base class only).

#include "SimNode.h"
#define SIMNODE_H_GUARD
#include "../../node_agent/main.cpp"
