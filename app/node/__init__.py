from ..config import Settings
from .base import BaseNode, Emit, NodeError


def create_node(settings: Settings, emit: Emit) -> BaseNode:
    """Wählt je nach Konfiguration Simulator oder echten Node."""
    if settings.mode == "sim":
        from .sim import SimNode

        return SimNode(emit)
    from .real import RealNode

    return RealNode(emit, settings)


__all__ = ["create_node", "BaseNode", "NodeError"]
