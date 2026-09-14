"""fake_demo_data_diode CLI. SIMULATION ONLY -- see the package docstring.

    uv run python -m tools.fake_demo_data_diode ingress --send OUT --receive INBOX
    uv run python -m tools.fake_demo_data_diode egress  --send FRAMES --receive READER

Run each direction as its own invocation. That is not a style preference: it is how
this tool stays an honest simulation. A single process holding both sides could pass
a result from the receive side back to the send side, which is precisely the thing
real hardware makes impossible. The report below is printed by the RECEIVE side.

To replace this with real hardware: point the send side at the diode's transmit
interface and run the receive-side components on the far host. Nothing else changes,
because nothing on either side was ever allowed to ask the other side a question.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .diode import (
    DEFAULT_MAX_ITEMS_PER_TICK,
    DEMO_BANNER,
    Direction,
    FakeDemoDataDiode,
)

# bundle-format-spec egress §4: frames are fixed size, 28 + 64*8 + 32 for v0.1.
EGRESS_FRAME_BYTES = 28 + 64 * 8 + 32


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fake_demo_data_diode",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("direction", choices=[d.value for d in Direction])
    parser.add_argument("--send", required=True, type=Path)
    parser.add_argument("--receive", required=True, type=Path)
    parser.add_argument("--max-items", type=int, default=DEFAULT_MAX_ITEMS_PER_TICK)
    parser.add_argument(
        "--drain", action="store_true", help="tick until settled (demos only)"
    )
    parser.add_argument(
        "--corruption-rate",
        type=float,
        default=0.0,
        help="flip one bit in this fraction of items, to exercise fail-closed paths",
    )
    parser.add_argument("--drop-rate", type=float, default=0.0)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--quiet-banner", action="store_true")
    args = parser.parse_args(argv)

    if not args.quiet_banner:
        print(DEMO_BANNER, file=sys.stderr)

    direction = Direction(args.direction)
    diode = FakeDemoDataDiode(
        args.send,
        args.receive,
        direction,
        max_items_per_tick=1 if direction is Direction.EGRESS else args.max_items,
        max_item_bytes=EGRESS_FRAME_BYTES if direction is Direction.EGRESS else None,
        corruption_rate=args.corruption_rate,
        drop_rate=args.drop_rate,
        seed=args.seed,
    )

    record = diode.drain() if args.drain else diode.tick()

    # Printed by the receive side. The send side is a different process and does not
    # read this; on real hardware it could not.
    print(f"delivered {len(record.delivered)}")
    for name in record.delivered:
        print(f"  + {name}")
    for name in record.dropped:
        print(f"  - {name} (dropped; sender is not told)")
    for name in record.deferred_rate_cap:
        print(f"  . {name} (deferred to a later tick by the rate cap)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
