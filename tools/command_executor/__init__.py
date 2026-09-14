"""The command executor (bundle-format-spec §9): signed dev commands -> the run loop.

The receiver has already verified the signature, the sequence, and that the key is
allowed to sign commands. What is left is the part the receiver deliberately does
not do: decide whether *this* op with *these* params may be applied to *this*
state. Each op has a fixed parameter set, each is applied whole or refused with a
code, and there is no op that carries code, a script, or a config body inline.

`rotate_verification_key` is the one op that changes what the receiver trusts, so
it needs a signer from a separately designated rotation set on top of everything
the receiver checked.
"""

from .executor import (
    CommandExecutor,
    CommandReceipt,
    CommandStatus,
    ExecutorCounters,
)
from .run_control import RunControl, RunState

__all__ = [
    "CommandExecutor",
    "CommandReceipt",
    "CommandStatus",
    "ExecutorCounters",
    "RunControl",
    "RunState",
]
