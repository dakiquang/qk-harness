"""`python3 -m harness doctor`: fetch and check the locked qk, then print what was found."""

import argparse
import json
from pathlib import Path
import sys

from harness import qk_cli


LOCK = Path(__file__).resolve().parent.parent / "qk.lock"


def main(argv=None):
    parser = argparse.ArgumentParser(prog="qk-harness")
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor", help="fetch the qk in qk.lock and check its contract")
    doctor.add_argument("--lock", default=str(LOCK), help="default: qk.lock next to the harness")
    args = parser.parse_args(argv)
    try:
        lock = qk_cli.load_lock(args.lock)
        qk = qk_cli.Qk(qk_cli.fetch(lock), lock)
    except qk_cli.QkError as error:
        print(f"qk-harness: {error}", file=sys.stderr)
        return 2
    print(json.dumps({"qk": qk.version, "contract": qk_cli.CONTRACT, "tag": lock["tag"], "sha": lock["sha"],
                      "root": str(qk.root)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
