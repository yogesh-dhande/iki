# 0126. An electronic signature is bound to the revision it signs

- Status: Accepted
- Date: 2026-11-18
- Refines: [0125](0125-a-state-is-a-field-each-state-says-who-sets-it.md),
  [0116](0116-lab-roles-are-declared-by-plugins-and-given-per-project.md),
  [0096](0096-bundles-carry-entities-between-deployments-with-their-history.md),
  [0025](0025-append-only-audit-log-and-database-roles.md)

## Context

Regulated labs decide things by signing: an analyst signs their results, a
reviewer signs that they checked them, QA signs a lot's release. Under
21 CFR Part 11 an electronic signature replaces a handwritten one only if:

- it shows the signer's printed name, the date and time, and what it means,
  wherever the record is shown or printed (§11.50);
- it's linked to its record so it can't be cut out, copied onto another, or
  kept while the record changes underneath it (§11.70);
- it's unique to one person and never reused or reassigned (§11.100(a));
- the organization has certified to the FDA that its electronic signatures
  bind as handwritten ones do (§11.100(c));
- each signing re-establishes who's signing, with both components of their
  sign-in (§11.200). That's [ADR 0124](0124-signing-signs-in-again-at-the-identity-provider.md).

Veeva Vault, Benchling, MasterControl and LabWare all ask for a password at
each signature, show a manifestation with the record, and attach required
signatures to a step of a record's lifecycle. Iki's records change through
revisions (ADR 0035), state fields whose states lab roles set (ADR 0125),
and can travel to
other deployments in bundles (ADR 0096).

## Decision

**A plugin says which states need signatures.** Setting one of a record's
state fields to such a state takes each of the state's `signatures`, each with a `meaning`
(authorship, review, approval, verification, responsibility, witness), a
label, the statement its signer attests to, and optionally its own lab
role, else one of the state's `set_by`. The state is set only once all of
them are given on the same revision of the record, and the last one sets
it, on a `state_changed` revision that names them in order. A state that needs
signatures is set only by them: setting it directly is refused (409
`signatures_required` from `POST /entities/{ref}/states/{field}`, and from
MCP's `set_entity_state`), whoever or whatever asks. The initial state, which
records start in unsigned, can't need any.

**Each signature is its own act, by one person.** Giving one takes a lab
role it names (any one) in the record's project. Core guarantees only that
each signature is a distinct, re-authenticated act on the exact revision;
whether one person may give two of a change's signatures, or in what order,
is workflow policy, which a plugin declares as a guard (step 58). Agents,
tokens, apps and service accounts never sign: the API refuses any token, and
the MCP server and both SDKs only read and verify.

**A signature is Ed25519 over a canonical document.** The document,
`iki/signature/v1`, is RFC 8785 canonical JSON naming the deployment, the
record (its ID, type, HRID, revision, and its registry entry's revision),
the change (field, from, to), which signature of how many, its meaning,
label and statement, the signer (ID, name and email as they were), the
reason, the time, how they signed in again (protocol, issuer, subject,
`auth_time` or `AuthnInstant`, methods, context), and the key. It also
names the SHA-256 of the record's **content** at that revision,
`iki/signed-content/v1`: its values as its history tables keep them, its
references and the SHA-256 of each file it holds. The deployment's key is
made on first use and kept encrypted with `IKI_ENCRYPTION_KEYS`, so the
database alone can't sign; `iki-server signing_key --rotate` retires it for
a new one, and a retired key still verifies what it signed.

**What it means is shown with the record (§11.50).** Each signature reads,
everywhere: "Approved by Ada Lovelace on 2026-11-18 at 14:03:22 UTC.
Meaning: QA approval." That's on the record's page, in its history, in the
API (`GET /entities/{ref}/signatures`), and in bundles. The signer's name
is the one the identity provider gave at signing, kept in the document;
someone without a name can't sign.

**Signatures stand or lapse, and never go away.** One is _pending_ while
its change waits for others, _carried_ once its change sets the state,
_withdrawn_ if its signer takes it back before then (with a reason), and
_lapsed_ if the record changes before the change is made, or its signer
loses the role: a lapsed signature counts for nothing, and the change needs
it again. Signatures, withdrawals, acknowledgments and certifications are
append-only: triggers refuse to update, delete or truncate them, for every
role.

A correction starts a new review cycle. Earlier signatures keep their
original place in history. For a result with signed review and release,
the QA signature identifies the review on which release depends. [ADR 0179](0179-a-corrected-result-goes-back-for-review.md)

**Anyone who reads the record can check its signatures (§11.70).**
Verifying one checks again that its key is one this deployment trusts and
signed the document; that the document's record, revision, change and
signer are its row's; that the content it names hashes to what it signed;
that the record's history, recomputed now, is still that content; and that
the revision that set the state names it. A value changed in SQL, a
signature moved to another record, a document edited, a signature deleted
(the revision that set the state still names it) each fail a named check.
`GET /signatures/{id}/verification` answers with each check.

**Bundles carry signatures, and verify offline.** A bundle has
`signatures.ndjson`, with each signature's document, signature and content,
and what was done to its record's history since, vouched for by the
exporting deployment's signing key (ADR 0148). Its manifest lists those
keys. `iki bundle verify` checks a bundle's own signature, then each
electronic signature against the bundle's history, without Iki: every value
the record signed must be there, read through what was done since. It
trusts the keys of a bundle signed by a key given with `--trust`, or keys
given with `--key`. An import checks each signature before writing anything,
and refuses a bundle with one that doesn't verify. It keeps the source's key
as trusted for what came with that import, and the signatures still verify
where they land, with the people and registry entries their records name as
they are there.

**The organization certifies first (§11.100(c)).** Nobody signs until an Org
Admin records the organization's certification letter to the FDA (when it was
sent, and where it's kept) in Settings → Signatures. Each person agrees, at
their first signature, that it's the legally binding equivalent of their
handwritten one.

## Consequences

- A signature can't be moved, edited or kept through a change without a
  check failing, whatever path changed the database.
- A record whose state needs signatures can only get there by people signing
  in again; automation can prepare it, never decide it.
- Four-eyes, ordering and who may sign twice are the plugin's to declare,
  as guards, when step 58 lands; until then a plugin can keep signers apart
  only by giving each signature a different role.
- The content format and canonicalization are a contract: changing either
  means a new format name, and verifiers keeping the old one.
