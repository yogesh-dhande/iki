# 0104. An identity can name an optional field, beside a required one

- Status: Accepted
- Date: 2026-11-16
- Refines: [0035](0035-entity-revisions-provenance-and-edit-rules.md)

## Context

A type's identity names the fields that make two of its entities the same,
and Iki finds duplicates by a hash of their values. Every identity field had
to be required and immutable, since an identity that could be empty or change
can't tell entities apart.

An antibody is its chains. A whole antibody or a Fab has a heavy chain and a
light chain. A single-domain antibody (a VHH) has a heavy chain alone, and so
does an scFv, whose one chain carries both variable domains. Its light chain is
part of what it is, and also optional. Leaving the light chain out of the
identity would make a VHH and every antibody with that heavy chain the same
antibody. Making it required leaves no way to register a VHH.

## Decision

**Each identity field must be immutable, and at least one must be required.**
An optional field without a value is part of the identity as JSON `null`:

```json
{
  "type": "antibody.Antibody",
  "v": 1,
  "values": { "heavy_chain": "01890a5d-…", "light_chain": null }
}
```

So a VHH is a different antibody from one with the same heavy chain and a light
chain, and two VHHs with the same heavy chain are the same one. An identity of
optional fields alone could be empty, so the validator refuses it
(`identity-not-required`).

Encoding a missing value as `null` changes no existing hash: every identity
field was required until now, so none was ever missing. The spec's encoding is
still version 1, and its README gives a test vector for other implementations.

## Consequences

- The antibody plugin's `Antibody` is its heavy chain and its light chain.
- An optional identity field is still immutable: adding a light chain to a
  VHH isn't changing it but making another antibody, which assembling does.
