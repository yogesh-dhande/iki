"""Human heavy chain constant regions, and which one a heavy chain's is.

An antibody's isotype is its heavy chain constant region's class and subclass, so a
heavy chain whose constant region is there says what it is. `assemble` reads it off the
residues after the chain's variable domain: it aligns them to each human constant
region below, and the one they match says the isotype.

The references are IMGT's: the secreted form of each human IGHC gene's first allele
(\\*01), domain by domain, as IMGT's protein display of the human IGH C-REGIONs gives
them (imgt.org, IMGT Repertoire, "Protein display: Human IGH C-REGIONs", updated
2 October 2025; IMGT data are CC BY 4.0). Each is CH1, the hinge (IgG3's four hinge
exons, IgD's two), CH2 and CH3, and for IgM and IgE, which have no hinge, CH4; the last
domain ends with the secreted form's tail (CHS), and residues made by splicing two exons
are included. The accession is the EMBL/GenBank entry IMGT read the allele from. Each
matches UniProt's secreted isoform of the gene (P01857-1 for IGHG1, and so on) residue
for residue, but where an allele differs from UniProt's (IGHG2*01 at 257, IGHA2*01 at
seven, IGHM*01 at 40 and IGHE*01 at 43); test_antibody_isotype.py checks each against
UniProt's sequence.

A chain is compared with each reference as a whole constant region would have it, from
CH1, and as an Fc, or a fusion to one, has it, from the domain after CH1; and, for a
subclass whose hinge's length differs between alleles (IgG3's has two, three or four
exons), with each natural hinge (HINGES), so a short-hinge allele such as IGHG3*04 is
IgG3's. Each comparison keeps the form it aligns best with.

A constant region is called the isotype whose reference it's nearest, when it's at least
90% identical to it over the residues they share, and the next nearest is at least 3
differences further away. Allotypes differ from the \\*01 alleles by a residue or two and
engineered Fc regions (LALA, YTE, S228P and the like) by a few, while the nearest
subclasses differ by 20 or more over a whole constant region and by 4 or more over CH1
alone, so a Fab's CH1 says its subclass too. A chain whose domains are different
subclasses' is called a hybrid of them, domain by domain: one domain is another
subclass's when it's at least 3 differences nearer it. A domain can change subclass part
way along, as eculizumab's CH2 does (CH1, the hinge and the start of CH2 IgG2's, the rest
of CH2 and CH3 IgG4's, as its Japanese review report gives it: PMDA, Soliris, 2010): a
domain read as one subclass's and then another's, with at least 3 fewer differences than
read as any one's, is a crossover, said with where it is, as far as the two subclasses
tell it. One less than 90% identical to every reference, such as a mouse's, isn't called
anything: these are human references only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import accumulate

from Bio import Align
from Bio.Align import substitution_matrices

#: Each isotype's reference: the IMGT gene and allele, the EMBL/GenBank accessions IMGT read
#: it from, UniProt's secreted isoform of the gene, and its domains in order.
REFERENCES: dict[str, tuple[str, tuple[str, ...], str, tuple[tuple[str, str], ...]]] = {
    "igg1": (
        "IGHG1*01",
        ("J00228",),
        "P01857-1",
        (
            (
                "CH1",
                "ASTKGPSVFPLAPSSKSTSGGTAALGCLVKDYFPEPVTVSWNSGALTSGVHTFPAVLQSSGLYSLSSVVTVPSSSLGTQ"
                "TYICNVNHKPSNTKVDKKV",
            ),
            ("hinge", "EPKSCDKTHTCPPCP"),
            (
                "CH2",
                "APELLGGPSVFLFPPKPKDTLMISRTPEVTCVVVDVSHEDPEVKFNWYVDGVEVHNAKTKPREEQYNSTYRVVSVLTV"
                "LHQDWLNGKEYKCKVSNKALPAPIEKTISKAK",
            ),
            (
                "CH3",
                "GQPREPQVYTLPPSRDELTKNQVSLTCLVKGFYPSDIAVEWESNGQPENNYKTTPPVLDSDGSFFLYSKLTVDKSRWQ"
                "QGNVFSCSVMHEALHNHYTQKSLSLSPGK",
            ),
        ),
    ),
    "igg2": (
        "IGHG2*01",
        ("J00230",),
        "P01859-1",
        (
            (
                "CH1",
                "ASTKGPSVFPLAPCSRSTSESTAALGCLVKDYFPEPVTVSWNSGALTSGVHTFPAVLQSSGLYSLSSVVTVPSSNFGTQ"
                "TYTCNVDHKPSNTKVDKTV",
            ),
            ("hinge", "ERKCCVECPPCP"),
            (
                "CH2",
                "APPVAGPSVFLFPPKPKDTLMISRTPEVTCVVVDVSHEDPEVQFNWYVDGVEVHNAKTKPREEQFNSTFRVVSVLTVV"
                "HQDWLNGKEYKCKVSNKGLPAPIEKTISKTK",
            ),
            (
                "CH3",
                "GQPREPQVYTLPPSREEMTKNQVSLTCLVKGFYPSDIAVEWESNGQPENNYKTTPPMLDSDGSFFLYSKLTVDKSRWQ"
                "QGNVFSCSVMHEALHNHYTQKSLSLSPGK",
            ),
        ),
    ),
    "igg3": (
        "IGHG3*01",
        ("X03604",),
        "P01860-1",
        (
            (
                "CH1",
                "ASTKGPSVFPLAPCSRSTSGGTAALGCLVKDYFPEPVTVSWNSGALTSGVHTFPAVLQSSGLYSLSSVVTVPSSSLGTQ"
                "TYTCNVNHKPSNTKVDKRV",
            ),
            (
                "hinge",
                "ELKTPLGDTTHTCPRCPEPKSCDTPPPCPRCPEPKSCDTPPPCPRCPEPKSCDTPPPCPRCP",
            ),
            (
                "CH2",
                "APELLGGPSVFLFPPKPKDTLMISRTPEVTCVVVDVSHEDPEVQFKWYVDGVEVHNAKTKPREEQYNSTFRVVSVLTV"
                "LHQDWLNGKEYKCKVSNKALPAPIEKTISKTK",
            ),
            (
                "CH3",
                "GQPREPQVYTLPPSREEMTKNQVSLTCLVKGFYPSDIAVEWESSGQPENNYNTTPPMLDSDGSFFLYSKLTVDKSRWQ"
                "QGNIFSCSVMHEALHNRFTQKSLSLSPGK",
            ),
        ),
    ),
    "igg4": (
        "IGHG4*01",
        ("K01316",),
        "P01861-1",
        (
            (
                "CH1",
                "ASTKGPSVFPLAPCSRSTSESTAALGCLVKDYFPEPVTVSWNSGALTSGVHTFPAVLQSSGLYSLSSVVTVPSSSLGTK"
                "TYTCNVDHKPSNTKVDKRV",
            ),
            ("hinge", "ESKYGPPCPSCP"),
            (
                "CH2",
                "APEFLGGPSVFLFPPKPKDTLMISRTPEVTCVVVDVSQEDPEVQFNWYVDGVEVHNAKTKPREEQFNSTYRVVSVLTV"
                "LHQDWLNGKEYKCKVSNKGLPSSIEKTISKAK",
            ),
            (
                "CH3",
                "GQPREPQVYTLPPSQEEMTKNQVSLTCLVKGFYPSDIAVEWESNGQPENNYKTTPPVLDSDGSFFLYSRLTVDKSRWQ"
                "EGNVFSCSVMHEALHNHYTQKSLSLSLGK",
            ),
        ),
    ),
    "iga1": (
        "IGHA1*01",
        ("J00220",),
        "P01876-1",
        (
            (
                "CH1",
                "ASPTSPKVFPLSLCSTQPDGNVVIACLVQGFFPQEPLSVTWSESGQGVTARNFPPSQDASGDLYTTSSQLTLPATQCLA"
                "GKSVTCHVKHYTNPSQDVTVPCP",
            ),
            ("hinge", "VPSTPPTPSPSTPPTPSPS"),
            (
                "CH2",
                "CCHPRLSLHRPALEDLLLGSEANLTCTLTGLRDASGVTFTWTPSSGKSAVQGPPERDLCGCYSVSSVLPGCAEPWNHG"
                "KTFTCTAAYPESKTPLTATLSKS",
            ),
            (
                "CH3",
                "GNTFRPEVHLLPPPSEELALNELVTLTCLARGFSPKDVLVRWLQGSQELPREKYLTWASRQEPSQGTTTFAVTSILRV"
                "AAEDWKKGDTFSCMVGHEALPLAFTQKTIDRLAGKPTHVNVSVVMAEVDGTCY",
            ),
        ),
    ),
    "iga2": (
        "IGHA2*01",
        ("J00221",),
        "P01877-1",
        (
            (
                "CH1",
                "ASPTSPKVFPLSLDSTPQDGNVVVACLVQGFFPQEPLSVTWSESGQNVTARNFPPSQDASGDLYTTSSQLTLPATQCPD"
                "GKSVTCHVKHYTNPSQDVTVPCP",
            ),
            ("hinge", "VPPPPP"),
            (
                "CH2",
                "CCHPRLSLHRPALEDLLLGSEANLTCTLTGLRDASGATFTWTPSSGKSAVQGPPERDLCGCYSVSSVLPGCAQPWNHG"
                "ETFTCTAAHPELKTPLTANITKS",
            ),
            (
                "CH3",
                "GNTFRPEVHLLPPPSEELALNELVTLTCLARGFSPKDVLVRWLQGSQELPREKYLTWASRQEPSQGTTTFAVTSILRV"
                "AAEDWKKGDTFSCMVGHEALPLAFTQKTIDRLAGKPTHVNVSVVMAEVDGTCY",
            ),
        ),
    ),
    "igm": (
        "IGHM*01",
        ("X14940",),
        "P01871-1",
        (
            (
                "CH1",
                "GSASAPTLFPLVSCENSPSDTSSVAVGCLAQDFLPDSITLSWKYKNNSDISSTRGFPSVLRGGKYAATSQVLLPSKDV"
                "MQGTDEHVVCKVQHPNGNKEKNVPLP",
            ),
            (
                "CH2",
                "VIAELPPKVSVFVPPRDGFFGNPRKSKLICQATGFSPRQIQVSWLREGKQVGSGVTTDQVQAEAKESGPTTYKVTSTLT"
                "IKESDWLGQSMFTCRVDHRGLTFQQNASSMCVP",
            ),
            (
                "CH3",
                "DQDTAIRVFAIPPSFASIFLTKSTKLTCLVTDLTTYDSVTISWTRQNGEAVKTHTNISESHPNATFSAVGEASICEDDW"
                "NSGERFTCTVTHTDLPSPLKQTISRPK",
            ),
            (
                "CH4",
                "GVALHRPDVYLLPPAREQLNLRESATITCLVTGFSPADVFVQWMQRGQPLSPEKYVTSAPMPEPQAPGRYFAHSILTV"
                "SEEEWNTGETYTCVVAHEALPNRVTERTVDKSTGKPTLYNVSLVMSDTAGTCY",
            ),
        ),
    ),
    "ige": (
        "IGHE*01",
        ("J00222",),
        "P01854-1",
        (
            (
                "CH1",
                "ASTQSPSVFPLTRCCKNIPSNATSVTLGCLATGYFPEPVMVTCDTGSLNGTTMTLPATTLTLSGHYATISLLTVSGAWA"
                "KQMFTCRVAHTPSSTDWVDNKTFS",
            ),
            (
                "CH2",
                "VCSRDFTPPTVKILQSSCDGGGHFPPTIQLLCLVSGYTPGTINITWLEDGQVMDVDLSTASTTQEGELASTQSELTLSQ"
                "KHWLSDRTYTCQVTYQGHTFEDSTKKCA",
            ),
            (
                "CH3",
                "DSNPRGVSAYLSRPSPFDLFIRKSPTITCLVVDLAPSKGTVNLTWSRASGKPVNHSTRKEEKQRNGTLTVTSTLPVGTR"
                "DWIEGETYQCRVTHPHLPRALMRSTTKTS",
            ),
            (
                "CH4",
                "GPRAAPEVYAFATPEWPGSRDKRTLACLIQNFMPEDISVQWLHNEVQLPDARHSTTQPRKTKGSGFFVFSRLEVTRAEW"
                "EQKDEFICRAVHEAASPSQTVQRAVSVNPGK",
            ),
        ),
    ),
    "igd": (
        "IGHD*01",
        ("K02875", "K02876", "K02877", "K02878", "K02879"),
        "P01880-1",
        (
            (
                "CH1",
                "APTKAPDVFPIISGCRHPKDNSPVVLACLITGYHPTSVTVTWYMGTQSQPQRTFPEIQRRDSYYMTSSQLSTPLQQWRQ"
                "GEYKCVVQHTASKSKKEIFRWP",
            ),
            ("hinge", "ESPKAQASSVPTAQPQAEGSLAKATTAPATTRNTGRGGEEKKKEKEKEEQEERETKTP"),
            (
                "CH2",
                "ECPSHTQPLGVYLLTPAVQDLWLRDKATFTCFVVGSDLKDAHLTWEVAGKVPTGGVEEGLLERHSNGSQSQHSRLTLP"
                "RSLWNAGTSVTCTLNHPSLPPQRLMALREP",
            ),
            (
                "CH3",
                "AAQAPVKLSLNLLASSDPPEAASWLLCEVSGFSPPNILLMWLEDQREVNTSGFAPARPPPQPRSTTFWAWSVLRVPAPP"
                "SPQPATYTCVVSHEDSRTLLNASRSLEVSYVTDHGPMK",
            ),
        ),
    ),
}

#: Natural hinges other than the reference allele's, for a subclass whose hinge's length
#: differs between alleles: each allele's IMGT name, the EMBL/GenBank entry it's read from,
#: how many hinge exons it has, and its hinge. IgG3's hinge is exon h1 (17 residues) and
#: then two, one or three of 15 (Dard et al., Eur J Hum Genet 9:765-772, 2001: "genes
#: having either 2, 3 or 4 exons coding for the hinge"): IGHG3*01 has four (h1 to h4),
#: IGHG3*03 three (h1, h3 and h4; Huck et al., Immunogenetics 30:250-257, 1989) and
#: IGHG3*04 two (h1 and h4; Dard et al., Hum Genet 99:138-141, 1997). The exons' residues
#: are the same in all three, as the entries translate. A chain is compared with each
#: hinge, so an allele's hinge length doesn't make it another subclass's.
HINGES: dict[str, tuple[tuple[str, tuple[str, ...], int, str], ...]] = {
    "igg3": (
        ("IGHG3*03", ("X16110",), 3, "ELKTPLGDTTHTCPRCPEPKSCDTPPPCPRCPEPKSCDTPPPCPRCP"),
        ("IGHG3*04", ("X99549",), 2, "ELKTPLGDTTHTCPRCPEPKSCDTPPPCPRCP"),
    ),
}

#: How each isotype is written.
NAMES = {
    "igg1": "IgG1",
    "igg2": "IgG2",
    "igg3": "IgG3",
    "igg4": "IgG4",
    "iga1": "IgA1",
    "iga2": "IgA2",
    "igm": "IgM",
    "ige": "IgE",
    "igd": "IgD",
}
#: A constant region less identical than this to every reference isn't called anything.
MIN_IDENTITY = 0.9
#: How many more differences the next nearest reference must have, for the nearest to be
#: called, over the whole region and in each domain; and how many fewer a domain must
#: have when it's read as one subclass's and then another's, for it to be called a
#: crossover.
MARGIN = 3
#: A domain is compared on its own only where the chain has at least this many residues
#: of it: fewer can't tell subclasses apart.
MIN_DOMAIN = 10

_ALIGNER = Align.PairwiseAligner(
    mode="global",
    substitution_matrix=substitution_matrices.load("BLOSUM62"),
    open_gap_score=-11,
    extend_gap_score=-1,
    # Either may run on past the other: a Fab's chain stops after CH1, and a fusion goes on
    # after CH3.
    end_gap_score=0,
)


@dataclass(frozen=True)
class Form:
    """One way a chain may have an isotype's constant region: from CH1 or, as an Fc or a
    fusion to one has it, from the domain after CH1; with the reference allele's hinge
    or another allele's."""

    domains: tuple[tuple[str, str], ...]
    #: The other allele whose hinge this is, and how many exons it has, or None.
    hinge: tuple[str, tuple[str, ...], int] | None = None


def forms(isotype: str) -> list[Form]:
    """The ways a chain may have `isotype`'s constant region."""
    domains = REFERENCES[isotype][3]
    hinged = [Form(domains)]
    for gene, accessions, exons, hinge in HINGES.get(isotype, ()):
        swapped = tuple((n, hinge if n == "hinge" else s) for n, s in domains)
        hinged.append(Form(swapped, (gene, accessions, exons)))
    return [*hinged, *(Form(f.domains[1:], f.hinge) for f in hinged)]


@dataclass(frozen=True)
class Match:
    """How a constant region compares with one isotype's reference."""

    isotype: str
    #: Columns of the alignment where both have a residue, or one has a gap, between the
    #: first and last where both have one.
    compared: int
    differences: int
    #: The differences in each of the reference's domains the chain has residues of, with
    #: how many residues of it the chain has.
    domains: dict[str, tuple[int, int]] = field(default_factory=dict)
    #: The other allele whose hinge the chain's is nearer, and how many exons it has.
    hinge: tuple[str, tuple[str, ...], int] | None = None
    #: For each of the chain's residues, the reference's domain it's aligned in, or None
    #: outside the aligned span; and whether it differs from the reference (a residue
    #: the reference has another of, or none of, or that the reference has more of
    #: before it).
    owners: tuple[str | None, ...] = field(default=(), repr=False, compare=False)
    costs: tuple[int, ...] = field(default=(), repr=False, compare=False)

    @property
    def identity(self) -> float:
        return (self.compared - self.differences) / self.compared if self.compared else 0.0

    def describe(self) -> str:
        gene, accessions, _, _ = REFERENCES[self.isotype]
        hinge = ""
        if self.hinge is not None:
            other, where, exons = self.hinge
            hinge = f", with {other}'s {exons}-exon hinge ({', '.join(where)}),"
        return (
            f"{self.identity:.1%} identical to IMGT's {gene} ({', '.join(accessions)}){hinge} "
            f"over {self.compared} residues"
        )


@dataclass(frozen=True)
class Crossover:
    """A domain that's one subclass's along its first part and another's along the rest:
    ``first``'s up to its residue ``last_first`` and ``then``'s from ``first_then`` on,
    counting the chain's residues of the domain from 1. Between them, both are the same."""

    first: str
    then: str
    last_first: int
    first_then: int

    def describe(self, domain: str) -> str:
        said = (
            f"its {domain} {NAMES[self.first]}'s to its residue {self.last_first} and "
            f"{NAMES[self.then]}'s from its residue {self.first_then}"
        )
        if self.first_then > self.last_first + 1:
            said += ", the two the same between"
        return said


@dataclass(frozen=True)
class Call:
    """What a constant region is.

    ``isotypes`` holds one isotype when it's that; several, nearest first, when it's as near
    one as another (``hybrid`` False) or its domains are different ones' (``hybrid`` True,
    with ``parts`` saying which domains are whose, and ``crossovers`` which domains are one
    subclass's and then another's along them, each in both of their ``parts``); and none
    when it's no human reference's. ``matches`` compares it with each reference, nearest
    first.
    """

    isotypes: tuple[str, ...]
    matches: tuple[Match, ...]
    hybrid: bool = False
    parts: dict[str, tuple[str, ...]] = field(default_factory=dict)
    crossovers: dict[str, Crossover] = field(default_factory=dict)

    def match(self, isotype: str) -> Match:
        return next(m for m in self.matches if m.isotype == isotype)

    def has(self, domain: str) -> bool:
        """Whether the chain has enough of `domain` for it to be called, as the nearest
        reference aligns it: at least :data:`MIN_DOMAIN` residues. Only for a constant
        region a reference matches."""
        found = self.matches[0].domains.get(domain)
        return found is not None and found[1] >= MIN_DOMAIN

    def describe(self) -> str:
        """What it is, and how sure, for a run's messages: "is IgG1's, 99.4% identical to
        …"."""
        nearest, runner_up = self.matches[0], self.matches[1]
        if not self.isotypes:
            return (
                "matches no human constant region: the nearest is "
                f"{NAMES[nearest.isotype]}'s, {nearest.describe()}"
            )
        if self.hybrid:
            said = [
                f"its {' and '.join(whole)} {NAMES[isotype]}'s"
                for isotype, domains in self.parts.items()
                if (whole := [d for d in domains if d not in self.crossovers])
            ]
            said += [c.describe(domain) for domain, c in self.crossovers.items()]
            return "is a hybrid: " + "; ".join(said)
        if len(self.isotypes) > 1:
            return (
                "is as near "
                + " as ".join(f"{NAMES[i]}'s" for i in self.isotypes)
                + f": {nearest.describe()}, and "
                + ", ".join(
                    f"{self.match(i).identity:.1%} to {NAMES[i]}'s" for i in self.isotypes[1:]
                )
            )
        return (
            f"is {NAMES[nearest.isotype]}'s, {nearest.describe()}; the next nearest, "
            f"{NAMES[runner_up.isotype]}'s, is {runner_up.identity:.1%}"
        )


def _compare(constant: str, isotype: str) -> Match:
    """`constant` against `isotype`'s reference, in the form it aligns best with."""
    best = max(
        (
            (_ALIGNER.align(constant, "".join(s for _, s in f.domains))[0], f)
            for f in forms(isotype)
        ),
        key=lambda found: found[0].score,
    )
    alignment, form = best
    owner = [name for name, sequence in form.domains for _ in sequence]
    query, target = alignment[0], alignment[1]
    both = [i for i, (q, t) in enumerate(zip(query, target, strict=True)) if q != "-" and t != "-"]
    if not both:
        return Match(
            isotype,
            0,
            0,
            hinge=form.hinge,
            owners=(None,) * len(constant),
            costs=(1,) * len(constant),
        )
    compared = differences = 0
    per_domain: dict[str, list[int]] = {}
    owners: list[str | None] = [None] * sum(1 for q in query[: both[0]] if q != "-")
    costs = [1] * len(owners)
    pending = 0  # reference residues the chain hasn't, before its next residue
    at = sum(1 for t in target[: both[0]] if t != "-")  # reference residues before the span
    domain = owner[min(at, len(owner) - 1)]
    for q, t in zip(query[both[0] : both[-1] + 1], target[both[0] : both[-1] + 1], strict=True):
        if t != "-":
            domain = owner[at]
            at += 1
        counts = per_domain.setdefault(domain, [0, 0])
        compared += 1
        if q != t:
            differences += 1
            counts[0] += 1
        if q != "-":
            counts[1] += 1
            owners.append(domain)
            costs.append(int(q != t) + pending)
            pending = 0
        else:
            pending += 1
    owners += [None] * (len(constant) - len(owners))
    costs += [1] * (len(constant) - len(costs))
    return Match(
        isotype,
        compared,
        differences,
        {name: (d, n) for name, (d, n) in per_domain.items()},
        form.hinge,
        tuple(owners),
        tuple(costs),
    )


def _crossover(name: str, nearest: Match, matches: list[Match]) -> Crossover | None:
    """Whether the chain's `name` domain, as the nearest reference aligns it, is one
    subclass's along its first part and another's along the rest: read so, it has at
    least :data:`MARGIN` fewer differences than read as any one subclass's."""
    where = [i for i, o in enumerate(nearest.owners) if o == name]
    if not where:
        return None
    start, end = where[0], where[-1] + 1
    sums = {
        m.isotype: [0, *accumulate(m.costs[start:end])]
        for m in matches
        if len(m.costs) == len(nearest.costs)
    }
    single = min(s[-1] for s in sums.values())
    best: tuple[int, str, str, int, int] | None = None
    for first, before in sums.items():
        for then, after in sums.items():
            if first == then:
                continue
            total = after[-1]
            splits = [before[k] + total - after[k] for k in range(1, end - start)]
            fewest = min(splits)
            if best is None or fewest < best[0]:
                at = [k + 1 for k, s in enumerate(splits) if s == fewest]
                best = (fewest, first, then, at[0], at[-1])
    if best is None or best[0] + MARGIN > single:
        return None
    _, first, then, earliest, latest = best
    # The first subclass's residues run to the earliest split that's as good, and the
    # other's from the latest one; between, the two are the same.
    return Crossover(first, then, earliest, latest + 1)


def call(constant: str) -> Call:
    """What the constant region `constant` (the residues after a heavy chain's variable
    domain) is."""
    matches = sorted(
        (_compare(constant, isotype) for isotype in REFERENCES),
        key=lambda m: (m.differences, -m.compared),
    )
    nearest = matches[0]
    if nearest.identity < MIN_IDENTITY:
        return Call((), tuple(matches))
    near = tuple(m.isotype for m in matches if m.differences < nearest.differences + MARGIN)
    if len(near) > 1:
        return Call(near, tuple(matches))
    # Each domain the chain has enough of, compared on its own: a hybrid's domains are
    # nearer another subclass's, or one subclass's along part of it and another's along
    # the rest.
    parts: dict[str, list[str]] = {}
    crossovers: dict[str, Crossover] = {}
    for name, (differences, residues) in nearest.domains.items():
        owner = nearest.isotype
        if residues >= MIN_DOMAIN:
            for other in matches[1:]:
                found = other.domains.get(name)
                if (
                    found is not None
                    and found[1] >= MIN_DOMAIN
                    and (found[0] + MARGIN <= differences)
                ):
                    owner = other.isotype
                    differences = found[0]
            crossover = _crossover(name, nearest, matches)
            if crossover is not None:
                crossovers[name] = crossover
                parts.setdefault(crossover.first, []).append(name)
                parts.setdefault(crossover.then, []).append(name)
                continue
        parts.setdefault(owner, []).append(name)
    if len(parts) > 1:
        ordered = {i: tuple(parts[i]) for i in REFERENCES if i in parts}
        return Call(
            tuple(ordered), tuple(matches), hybrid=True, parts=ordered, crossovers=crossovers
        )
    return Call((nearest.isotype,), tuple(matches))
