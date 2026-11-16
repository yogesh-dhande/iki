"""Records what ANARCI says about the antibody plugin's test chains, for its tests
(server/tests/test_antibody_numbering.py), which put a stand-in ANARCI on the path that
answers from it. Run it in the numbering-tools image, offline:

    docker run --rm --network none -v "$PWD/server/tests/data:/data:ro" \
        iki-antibody-numbering-tools:ci python -I /data/record_anarci.py \
        > server/tests/data/anarci.json

Trastuzumab's chains are PDB 1N8Z's; the VHH and the lambda chain are typical of their
kinds; D1.3's heavy chain is a mouse's; lysozyme has no antibody domain.
"""

import json
import sys

from anarci import anarci

VH = (
    "EVQLVESGGGLVQPGGSLRLSCAASGFNIKDTYIHWVRQAPGKGLEWVARIYPTNGYTRYADSVKGRFTISADTSKNTAYLQMNSLR"
    "AEDTAVYYCSRWGGDGFYAMDYWGQGTLVTVSS"
)
VL = (
    "DIQMTQSPSSLSASVGDRVTITCRASQDVNTAVAWYQQKPGKAPKLLIYSASFLYSGVPSRFSGSRSGTDFTLTISSLQPEDFATYY"
    "CQQHYTTPPTFGQGTKVEIK"
)
SEQUENCES = {
    "vh": VH,
    "vl": VL,
    "scfv": VH + "GGGGSGGGGSGGGGS" + VL,
    "vhh": (
        "EVQLVESGGGLVQPGGSLRLSCAASGRTFSYNPMGWFRQAPGKGRELVAAISRTGGSTYYPDSVEGRFTISRDNAKRMVYLQ"
        "MNSLRAEDTAVYYCAAAGVRAEDGRVRTLPSEYTFWGQGTQVTVSS"
    ),
    "lambda": (
        "QSALTQPASVSGSPGQSITISCTGTSSDVGGYNYVSWYQQHPGKAPKLMIYEVSNRPSGVSNRFSGSKSGNTASLTISGLQAE"
        "DEADYYCSSYTSSSTLVFGGGTKLTVL"
    ),
    # Anti-lysozyme D1.3's VH (PDB 1VFB).
    "mouse": (
        "QVQLKESGPGLVAPSQSLSITCTVSGFSLTGYGVNWVRQPPGKGLEWLGMIWGDGNTDYNSALKSRLSISKDNSKSQVFLKMNSL"
        "HTDDTARYYCARERDYRLDYWGQGTTLTVSS"
    ),
    "lysozyme": (
        "KVFGRCELAAAMKRHGLDNYRGYSLGNWVCAAKFESNFNTQATNRNTDGSTDYGILQINSRWWCNDGRTPGSRNLCNIPCSA"
        "LLSSDITASVNCAKKIVSDGNGMNAWVAWRNRCKGTDVQAWIRGCRL"
    ),
}

#: Each scheme's answers, with every species' models ("all"), then with human's alone.
schemes: dict[str, dict[str, object]] = {}
for scheme in ("imgt", "kabat", "chothia"):
    for species in ("all", "human"):
        numbered, details, _ = anarci(
            list(SEQUENCES.items()),
            scheme=scheme,
            allow={"H", "K", "L"},
            assign_germline=True,
            allowed_species=None if species == "all" else [species],
        )
        schemes.setdefault(scheme, {})[species] = {
            name: [numbered[i], details[i]] for i, name in enumerate(SEQUENCES)
        }
recorded = {"sequences": SEQUENCES, "schemes": schemes}
sys.stdout.write(json.dumps(recorded, separators=(",", ":")) + "\n")
