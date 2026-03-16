"""
topology.py — Adjacency graph built from TopologyConfig.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import TopologyConfig, NodeConfig, EdgeConfig


@dataclass
class EdgeLink:
    """One directed view of an undirected edge (towards `other`)."""
    other: str
    loss: float
    latency_ms: float
    snr: float
    rssi: float


class Topology:
    def __init__(self, config: TopologyConfig) -> None:
        self._node_map: dict[str, NodeConfig] = {
            n.name: n for n in config.nodes
        }
        # Build bidirectional adjacency list
        self._adj: dict[str, list[EdgeLink]] = {
            n.name: [] for n in config.nodes
        }
        for edge in config.edges:
            link = EdgeLink(
                other=edge.b,
                loss=edge.loss,
                latency_ms=edge.latency_ms,
                snr=edge.snr,
                rssi=edge.rssi,
            )
            self._adj[edge.a].append(link)
            self._adj[edge.b].append(
                EdgeLink(
                    other=edge.a,
                    loss=edge.loss,
                    latency_ms=edge.latency_ms,
                    snr=edge.snr,
                    rssi=edge.rssi,
                )
            )

    def neighbours(self, node_name: str) -> list[EdgeLink]:
        """Return all nodes directly reachable from node_name."""
        return self._adj.get(node_name, [])

    def node_config(self, name: str) -> NodeConfig:
        return self._node_map[name]

    def all_names(self) -> list[str]:
        return list(self._node_map)

    def endpoint_names(self) -> list[str]:
        """Non-relay nodes only."""
        return [n for n, cfg in self._node_map.items() if not cfg.relay]

    def relay_names(self) -> list[str]:
        return [n for n, cfg in self._node_map.items() if cfg.relay]
