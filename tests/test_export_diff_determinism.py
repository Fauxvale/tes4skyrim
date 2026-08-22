"""diff_records must return keys in a STABLE order.

Its result becomes `pending` in override_builder, whose insert loop places
newly-inserted subrecords relative to each other in dict order -- so a
non-deterministic key order reaches the emitted subrecord order and the output
ESM stops being byte-reproducible (docs/performance_notes.md).

The real failure: CATShipCabinDoorExteriorREF in Morrowind_ob came out
`... DATA EDID XTEL` in one build and `... DATA XTEL EDID` in the next,
because `set(m) | set(p)` iterates in an order that depends on Python's
per-process string hash randomization.
"""
import subprocess
import sys
import textwrap

from tes5_import.export_diff import diff_records

MASTER = {
    'Signature': 'REFR', 'FormID': '01841792', 'NAME': '02200072',
    'XLOC.Key': '00000000', 'XLOC.Flags': '255', 'XLOC.Level': '0',
    'XTEL.Door': '0000FF01', 'XTEL.PosX': '1.0', 'XTEL.PosY': '2.0',
    'XTEL.PosZ': '3.0', 'XTEL.RotX': '0.0', 'XTEL.RotY': '0.0',
    'XTEL.RotZ': '0.0', 'ParentCELL': '0000AAAA', 'RecordFlags': '0',
}
PLUGIN = dict(MASTER, **{
    'XLOC.Key': '0003905B', 'XTEL.Door': '0300FF5E', 'XTEL.PosX': '-123.0',
    'XTEL.PosY': '-268.0', 'XTEL.PosZ': '-45.0', 'XTEL.RotX': '0.1',
    'XTEL.RotY': '0.2', 'XTEL.RotZ': '0.3', 'ParentCELL': '0000BBBB',
    'RecordFlags': '1024', 'EditorID': 'CATShipCabinDoorExteriorREF',
})


def test_key_order_is_sorted():
    keys = list(diff_records(MASTER, PLUGIN))
    assert keys == sorted(keys)


def test_key_order_survives_hash_randomization():
    """Run the same diff under opposed PYTHONHASHSEEDs; the order must match."""
    script = textwrap.dedent(f'''
        import sys
        sys.path.insert(0, {__import__('os').path.dirname(__import__('os').path.dirname(__import__('os').path.abspath(__file__)))!r})
        from tes5_import.export_diff import diff_records
        print(",".join(diff_records({MASTER!r}, {PLUGIN!r})))
    ''')
    orders = []
    for seed in ('1', '999', '12345'):
        out = subprocess.run([sys.executable, '-c', script], check=True,
                             capture_output=True, text=True,
                             env={**__import__('os').environ,
                                  'PYTHONHASHSEED': seed})
        orders.append(out.stdout.strip())
    assert len(set(orders)) == 1, f'key order varied by hash seed: {orders}'
