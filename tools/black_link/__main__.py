"""black-link CLI. Reference-grade; see the package docstring.

    uv run python -m tools.black_link keygen  --out KEYFILE
    uv run python -m tools.black_link send    --key KEYFILE --link-id N --state FILE \\
                                              --spool IN_DIR --cells OUT_DIR --ticks T
    uv run python -m tools.black_link receive --key KEYFILE --link-id N \\
                                              --cells IN_DIR --deliver OUT_DIR

A key is one link, one direction. Generate it once, carry it to both ends on
accounted media, and never use it for a second link or the reverse direction.

`send` takes every file in the spool as one message, emits exactly `--ticks` cells
(cover cells once the spool is drained), and removes each spool file it queued.
`receive` reads cell files in name order and writes each delivered message to the
delivery directory. Its replay window lives only as long as the process, so a
real receive end is one long-running process, not repeated invocations over the
same directory. The two are separate invocations for the reason the fake diode
CLI gives: nothing the receive side learns may reach the send side.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .link import CounterStore, LinkReceiver, LinkSender, load_link_key, write_link_key


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="black_link", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    keygen = sub.add_parser("keygen")
    keygen.add_argument("--out", required=True, type=Path)

    send = sub.add_parser("send")
    send.add_argument("--key", required=True, type=Path)
    send.add_argument("--link-id", required=True, type=int)
    send.add_argument("--state", required=True, type=Path, help="durable counter reservation")
    send.add_argument("--spool", required=True, type=Path)
    send.add_argument("--cells", required=True, type=Path)
    send.add_argument("--ticks", required=True, type=int)
    send.add_argument("--copies", type=int, default=1)

    receive = sub.add_parser("receive")
    receive.add_argument("--key", required=True, type=Path)
    receive.add_argument("--link-id", required=True, type=int)
    receive.add_argument("--cells", required=True, type=Path)
    receive.add_argument("--deliver", required=True, type=Path)

    args = parser.parse_args(argv)
    if args.cmd == "keygen":
        write_link_key(args.out)
        print(f"wrote {args.out}")
        return 0

    key = load_link_key(args.key)
    if args.cmd == "send":
        sender = LinkSender(key, args.link_id, CounterStore(args.state), copies=args.copies)
        for path in sorted(p for p in args.spool.iterdir() if p.is_file()):
            if not sender.enqueue(path.read_bytes()):
                break
            path.unlink()
        args.cells.mkdir(parents=True, exist_ok=True)
        for _ in range(args.ticks):
            cell = sender.tick()
            counter = int.from_bytes(cell[8:16], "big")
            (args.cells / f"cell-{counter:020d}.bin").write_bytes(cell)
        print(json.dumps({**sender.counters.as_metrics(), "queued_fragments": sender.queued_fragments}))
        return 0

    receiver = LinkReceiver(key, args.link_id)
    args.deliver.mkdir(parents=True, exist_ok=True)
    n = 0
    for path in sorted(p for p in args.cells.iterdir() if p.is_file()):
        for message in receiver.feed(path.read_bytes()):
            n += 1
            (args.deliver / f"message-{n:08d}.bin").write_bytes(message)
    print(json.dumps({**receiver.counters.as_metrics(), "by_reason": receiver.counters.by_reason}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
