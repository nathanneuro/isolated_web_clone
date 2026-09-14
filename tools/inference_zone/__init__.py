"""The inference zone: the only place model weights exist inside the airgap.

Per agent-sandbox-spec §3.3 the agent under evaluation cannot reach this zone. It
reaches the action broker, which reaches this. The separation is what stops an agent
from editing the policy it is being evaluated on.

For the demo this is `transformers` on CPU with a sub-1B model, which is the honest
choice: we are demonstrating an architecture, not inference throughput. A real lab
substitutes vLLM, TensorRT-LLM, or its own stack behind the same broker interface,
and nothing else in the design changes.
"""

from .server import GenerationLimits, ModelServer

__all__ = ["GenerationLimits", "ModelServer"]
