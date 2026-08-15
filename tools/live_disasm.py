#!/usr/bin/env python3
"""Disassemble the RUNNING SkyrimSE.exe through the game bridge.

WHY THIS EXISTS
---------------
The Steam build is DRM-packed on disk, so it cannot be disassembled statically.
The usual workaround is to read the GOG/AE 1.6.659 copy instead and translate
addresses through the Address Library -- but that only gives you what 659 looks
like. Every struct offset, branch, and inlined check you read there is a GUESS
about the build actually running, and this project has already burned hours on
offsets that did not survive the translation.

The packing only protects the FILE. Once the game is running, .text is
decrypted in memory, and the bridge can read it. So the running build
disassembles perfectly -- with no translation, no guessing, and RVAs that match
the process you are debugging.

    # disassemble at a stable id, an rva, or an absolute address
    python tools/live_disasm.py --id 21954 --count 60
    python tools/live_disasm.py --rva 0x341300 --count 40
    python tools/live_disasm.py --address 0x7ff7a8c812d0

    # follow the call/jmp targets found along the way
    python tools/live_disasm.py --id 21954 --count 80 --follow

    # find which instructions reference an address (e.g. a string literal)
    python tools/live_disasm.py --xref 0x7ff7abc12340 --scan-id 21954 --scan-len 0x400

Requires: the game running with TESGameBridge.dll, and `pip install capstone`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from game_bridge import Bridge, BridgeError  # noqa: E402

try:
    import capstone
except ImportError:  # pragma: no cover - dependency hint
    print("needs capstone:  pip install capstone", file=sys.stderr)
    raise SystemExit(2)


def read(b: Bridge, addr: int, length: int) -> bytes:
    """Read live memory in chunks the pipe is happy with."""
    out = bytearray()
    while len(out) < length:
        n = min(512, length - len(out))
        r = b.readmem(address=addr + len(out), length=n)
        hexs = r.get("hex")
        if not hexs:
            break
        out += bytes(int(x, 16) for x in hexs.split())
    return bytes(out)


def resolve(b: Bridge, args) -> int:
    if args.address is not None:
        return args.address
    if args.id is not None:
        return int(b.resolve(id=args.id)["address"])
    if args.rva is not None:
        return int(b.resolve(rva=args.rva)["address"])
    raise SystemExit("give one of --address / --rva / --id")


def disasm(b: Bridge, addr: int, count: int, base: int,
           follow: bool = False, _seen: set[int] | None = None,
           depth: int = 0) -> list[int]:
    """Print `count` instructions at `addr`; return call/jmp targets seen."""
    seen = _seen if _seen is not None else set()
    if addr in seen or depth > 3:
        return []
    seen.add(addr)

    data = read(b, addr, max(16 * count, 64))
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    targets: list[int] = []

    pad = "  " * depth
    print(f"{pad}--- {addr:#x} (rva {addr - base:#x}) ---")
    for i, ins in enumerate(md.disasm(data, addr)):
        if i >= count:
            break
        print(f"{pad}{ins.address:#018x}  rva {ins.address - base:<#10x}  "
              f"{ins.mnemonic:<8s} {ins.op_str}")
        if ins.mnemonic in ("call", "jmp") and ins.op_str.startswith("0x"):
            try:
                targets.append(int(ins.op_str, 16))
            except ValueError:
                pass

    if follow:
        for t in targets:
            disasm(b, t, min(count, 24), base, follow, seen, depth + 1)
    return targets


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("--address", type=lambda s: int(s, 0))
    ap.add_argument("--rva", type=lambda s: int(s, 0))
    ap.add_argument("--id", type=int)
    ap.add_argument("--count", type=int, default=40)
    ap.add_argument("--follow", action="store_true",
                    help="also disassemble call/jmp targets")
    ap.add_argument("--xref", type=lambda s: int(s, 0),
                    help="report instructions whose operand resolves here")
    ap.add_argument("--scan-len", type=lambda s: int(s, 0), default=0x400,
                    help="how many bytes to scan for --xref")
    args = ap.parse_args(argv)

    try:
        with Bridge().connect(retries=2) as b:
            base = int(b.resolve(rva=0)["address"])
            addr = resolve(b, args)

            if args.xref is not None:
                data = read(b, addr, args.scan_len)
                md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
                md.detail = True
                for ins in md.disasm(data, addr):
                    # rip-relative operands resolve against the NEXT instruction
                    if f"rip" in ins.op_str:
                        for tok in ins.op_str.split():
                            if tok.startswith("0x"):
                                try:
                                    d = int(tok.rstrip("]"), 16)
                                except ValueError:
                                    continue
                                if ins.address + ins.size + d == args.xref:
                                    print(f"{ins.address:#x}  {ins.mnemonic} {ins.op_str}")
                    elif ins.op_str.startswith("0x") and int(ins.op_str, 16) == args.xref:
                        print(f"{ins.address:#x}  {ins.mnemonic} {ins.op_str}")
                return 0

            disasm(b, addr, args.count, base, args.follow)
    except BridgeError as exc:
        print(f"error [{exc.code}]: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
