<!-- SPDX-License-Identifier: Apache-2.0 -->
# Identity Card: one card for people and agents

Status: design, **planned**. Nothing in this document is implemented. Today Baton has token-to-principal mappings (see chapter 7); the capability table in [space-replica.md](space-replica.md) and in the [README](../../README.md) is the source of truth for what ships. Milestones are in [roadmap.md](roadmap.md) (W2-3 and W3-1).

Origin: the owner requirement recorded as DECISIONS 292 (Baton task ba60083f): in Baton, people and agents both carry an "Identity Card". Delegation rules referenced here are those of DECISIONS 287 and 291 as restated in W2-2 of the roadmap.

Principle: connect to standards, do not invent them. The card is a Baton data model that carries and points to existing standards (DID, OIDC, DPoP, mTLS, SPIFFE, ID-JAG, A2A Agent Card, JWS, JCS, token status). Where a mechanism is a Baton proposal rather than a standard, the text says so. Verification date for the sources below: 2026-10-17. Anything not confirmed from the cited page is marked "(not yet verified)".

Sources used (official pages):
- W3C DID Core, W3C Recommendation 19 July 2022: <https://www.w3.org/TR/did-core/>
- OpenID Connect Core 1.0: <https://openid.net/specs/openid-connect-core-1_0.html>
- RFC 9449, OAuth 2.0 DPoP: <https://www.rfc-editor.org/rfc/rfc9449>
- RFC 8705, OAuth 2.0 mutual-TLS and certificate-bound access tokens: <https://www.rfc-editor.org/rfc/rfc8705>
- RFC 8693, OAuth 2.0 Token Exchange: <https://www.rfc-editor.org/rfc/rfc8693>
- RFC 7009, OAuth 2.0 Token Revocation: <https://www.rfc-editor.org/rfc/rfc7009>
- RFC 7662, OAuth 2.0 Token Introspection: <https://www.rfc-editor.org/rfc/rfc7662>
- RFC 7515, JSON Web Signature: <https://www.rfc-editor.org/rfc/rfc7515>
- RFC 8785, JSON Canonicalization Scheme: <https://www.rfc-editor.org/rfc/rfc8785>
- IETF Token Status List, Active Internet-Draft `draft-ietf-oauth-status-list` (revision -21 at the verification date; a draft, not an RFC): <https://datatracker.ietf.org/doc/draft-ietf-oauth-status-list/>
- IETF Identity Assertion JWT Authorization Grant (ID-JAG), Active Internet-Draft, revision -04 at the verification date (a draft, not an RFC): <https://datatracker.ietf.org/doc/draft-ietf-oauth-identity-assertion-authz-grant/>
- SPIFFE concepts (SPIFFE ID, SVID): <https://spiffe.io/docs/latest/spiffe-about/spiffe-concepts/>
- A2A v1.0 specification, including section 8.4 "Agent Card Signing" and the `AgentProvider`, `AgentSkill`, `AgentExtension` and `AgentCardSignature` objects (4.4.x): <https://a2a-protocol.org/latest/specification/>

## 1. Card model

One structure for humans and agents. The card describes who a principal is and what it may do; it does not grant access by itself (chapter 4).

| Field | Meaning |
| --- | --- |
| `id` | Stable identifier, a URI: `baton:principal:<id>`. Never reused, never reassigned. Optionally also expressed as a W3C DID (see below). |
| `kind` | `human` or `agent`. |
| `display_name` | Name shown to people. Not an identifier and not unique. |
| `issuer`, `issued_at` | Which Baton instance issued the card, and when. |
| `status` | `active`, `suspended` or `revoked`. `revoked` is final. |
| `keys` | Public keys only. Each key has a `purpose`: `dpop` (binds access tokens, chapter 4) or `sign` (verifies signatures made by this principal, for example an agent's Agent Card). Private keys never reach the card or the issuer. |
| `memberships` | Spaces the principal belongs to and its role in each (sharing roles such as view, comment, edit). |
| `delegations_held` | Approver delegations the principal holds: `grantor`, `scope`, `expires_at`. |
| `audit` | A pointer to the principal's audit trail (`x_audit`), so any action can be traced back to a card. |
| agent only | `accountable`, `runtime`, `capabilities`, `agent_card` (chapter 2). |
| human only | `external_identities` (chapter 3). |

Identifier and DID. The primary identifier is the `baton:principal:<id>` URI. A DID representation is optional: DID Core defines a DID as a URI that resolves to a DID document holding verification methods ([DID Core](https://www.w3.org/TR/did-core/)). If used, the card's public part can be rendered as a DID document with the card's keys as verification methods, under a method Baton would have to define and register; the choice of method (for example `did:web` under the instance's domain) is an open question (chapter 8) and nothing here claims DID method registration.

Delegations. A delegation says "grantor G lets this principal act as approver within scope S until time T". Rules (DECISIONS 287 and 291, as restated in roadmap W2-2): the space owner is the root; delegations flow down from the root; a delegation cannot exceed the grantor's own scope; cyclic delegations are refused; nobody but the root approves their own work, and the root's self-approval is accepted only when explicitly marked and audited. The authoritative record of a delegation stays in Baton's store and audit; the card shows the delegations currently held, so a reader does not have to query the store.

Example (illustrative, not a wire format; all values are placeholders):

```json
{
  "id": "baton:principal:7f3a9c21",
  "did": "did:web:baton.example.com:principal:7f3a9c21",
  "kind": "agent",
  "display_name": "Release reviewer",
  "issuer": "https://baton.example.com",
  "issued_at": "2026-10-20T09:00:00Z",
  "status": "active",
  "keys": [
    {
      "kid": "dpop-1",
      "purpose": "dpop",
      "jwk": { "kty": "EC", "crv": "P-256", "x": "<public x>", "y": "<public y>" }
    },
    {
      "kid": "sign-1",
      "purpose": "sign",
      "jwk": { "kty": "EC", "crv": "P-256", "x": "<public x>", "y": "<public y>" }
    }
  ],
  "memberships": [
    { "space": "space-example", "role": "edit" }
  ],
  "delegations_held": [
    {
      "role": "approver",
      "grantor": "baton:principal:0c11d5aa",
      "scope": "space-example/tasks:release",
      "expires_at": "2026-11-20T00:00:00Z"
    }
  ],
  "accountable": {
    "chain": [
      { "type": "human", "ref": "baton:principal:0c11d5aa" }
    ],
    "terminal": "baton:principal:0c11d5aa"
  },
  "runtime": { "platform": "Cursor", "model": "example-model" },
  "capabilities": ["review", "run-tests"],
  "agent_card": "https://baton.example.com/.well-known/agent-card.json",
  "audit": "/audit?actor=baton:principal:7f3a9c21",
  "signature": { "protected": "<base64url>", "signature": "<base64url>" }
}
```

Signature. The public part of a card is signed by the issuing instance as a JWS ([RFC 7515](https://www.rfc-editor.org/rfc/rfc7515)) over the JCS-canonicalized card ([RFC 8785](https://www.rfc-editor.org/rfc/rfc8785)), using the same procedure A2A specifies for Agent Cards (section 8.4: remove default values, exclude the `signatures` field, canonicalize with JCS, sign with a protected header carrying `alg`, `typ` and `kid`). Reusing that procedure lets one verifier implementation check both kinds of card.

What is public and what is not (Baton proposal): the public part contains `id`, `kind`, `display_name`, `issuer`, `issued_at`, `status`, public `keys`, and for agents `accountable`, `runtime`, `capabilities` and `agent_card`. `memberships`, `delegations_held` and `external_identities` are visible only to authenticated members of the relevant space. Whether the member view is signed separately is an open question.

## 2. Agent cards

An agent card is an Identity Card with `kind: agent` and three mandatory additions.

**Accountable party.** Every agent card names a "responsible person or organization": a guardianship chain, an ordered list of cards ending at a human or an organization. Rules proposed by Baton:
- the chain terminates at a human card or at an organization entry; an organization entry must name at least one human card that answers for it;
- every link is an existing card, and a cycle is refused;
- an agent card cannot be issued without an accountable party;
- if any card in the chain is `suspended` or `revoked`, the agent's effective status is at most `suspended` until the chain is repaired by an explicit, audited change. This is a design rule, not a standard.

This matches the ownership model already adopted in the Protocols chapter of [space-replica.md](space-replica.md#protocols) (item 10, Linear: the agent is the delegate, the human stays accountable) and the delegation rules above: an agent may hold an approver delegation only from a human's chain.

**Runtime operator.** The model or platform that runs the agent (for example Claude, Cursor, Codex), recorded as `runtime`. It is a claim by the issuer or the operator and says what executes the agent, not who is responsible for it. Runtime identity of a workload may additionally be carried by a SPIFFE ID (chapter 4).

**Capabilities and the A2A Agent Card.** `capabilities` link to the skills of the agent's A2A Agent Card (`AgentSkill`, A2A 4.4.5) by skill id, instead of redefining a skill vocabulary. The two documents reference each other:
- the Identity Card holds `agent_card`, the URL of the Agent Card (A2A well-known location `/.well-known/agent-card.json`);
- the Agent Card references the Identity Card id through an `AgentExtension` (A2A 4.4.4) that Baton documents; the extension URI is to be defined and is not yet published. The Agent Card's `provider` (`AgentProvider`, A2A 4.4.2: `url` and `organization`) should agree with the terminal accountable party when that party is an organization.

The Agent Card is signed with JWS as A2A section 8.4 and `AgentCardSignature` (A2A 4.4.7) define; verification uses the `sign` key on the Identity Card (`kid` match) or `jku` where the card publishes a JWKS. A verifier accepts the pair only if both signatures verify and each document names the other. A mismatch is refused. How the instance's own Agent Card (roadmap W2-1) relates to the cards of the agents it lists is covered in the same design: Baton signs its own card and verifies listed ones.

## 3. Human cards and external identity

A human card binds to an external identity provider through OpenID Connect ([OIDC Core](https://openid.net/specs/openid-connect-core-1_0.html)). The card stores only:

```json
"external_identities": [
  { "issuer": "https://idp.example.com", "subject": "<sub claim>" }
]
```

Rules:
- the binding is the pair (ID Token `iss`, `sub`); OIDC Core defines `sub` as a locally unique, never reassigned identifier within the issuer, which is why the pair, not an email or a name, is the key;
- the card never stores passwords, refresh tokens, ID tokens or IdP client secrets. Baton's own credentials as an OIDC relying party live in operator configuration, not on any card;
- signing in verifies the ID Token (signature, `iss`, `aud`, expiry, nonce) and looks up the card by the bound pair; an unbound pair does not create access by itself, and binding is an audited operation by the space owner or an administrator;
- the same mechanism covers Feishu, Okta and Entra. Okta and Entra document OpenID Connect support: <https://developer.okta.com/docs/concepts/oauth-openid/>, <https://learn.microsoft.com/en-us/entra/identity-platform/v2-protocols-oidc>. Whether Feishu's login flow issues an OIDC-conformant ID Token Baton can validate this way is (not yet verified); the Feishu login page is <https://open.feishu.cn/document/common-capabilities/sso/web-application-sso/web-app-overview>. If it does not, the Feishu path needs an adapter and the card model is unchanged.

Enterprise agents and workloads reach Baton through the exchange in chapter 4; human sign-in is the only OIDC browser flow.

## 4. The card is not a token

A card says who a principal is. An access token is a short-lived credential issued against a card. The two are separate objects: possessing or copying a card (it is public) gives no access.

Token properties (planned):
- **Short-lived and rotatable.** Lifetime is a documented number set by configuration; the default is an open question. Rotation issues a new token for the same card without changing the card.
- **Sender-constrained.** The token is bound to a key the card registers, so a copied token is useless without the key:
  - DPoP ([RFC 9449](https://www.rfc-editor.org/rfc/rfc9449)): the client proves possession of a private key on each request with a DPoP proof JWT (section 4); the access token carries the key's JWK thumbprint in `cnf.jkt` (section 6.1); the resource server checks the proof and the binding (sections 4.3 and 7). Proof replay is addressed by `jti` tracking and, optionally, server-provided nonces (sections 8 and 11.1);
  - or mutual TLS ([RFC 8705](https://www.rfc-editor.org/rfc/rfc8705)): the token is bound to the client certificate thumbprint (section 3.1) for deployments that already terminate mutual TLS.
- **Subject and acting party.** The token's subject is the card `id`. When an agent acts for a person, the delegation is expressed with the actor (`act`) claim of OAuth Token Exchange ([RFC 8693](https://www.rfc-editor.org/rfc/rfc8693), section 4.1), and the audit records "agent X acting for person Y" together with the actual client key, as roadmap W2-3 requires.
- **Workloads.** A workload that already has a SPIFFE identity may present an X.509 or JWT SVID ([SPIFFE concepts](https://spiffe.io/docs/latest/spiffe-about/spiffe-concepts/)); the SPIFFE ID is bound to the card as an additional external identity and the SVID is used instead of a Baton-issued key. The exact binding rules are an open question.
- **Enterprise exchange.** An enterprise IdP issues an ID-JAG that the agent exchanges for a Baton access token ([ID-JAG draft](https://datatracker.ietf.org/doc/draft-ietf-oauth-identity-assertion-authz-grant/), built on RFC 8693 token exchange and the JWT grant), the XAA flow described in the Identity chapter of [space-replica.md](space-replica.md). Phase 1 defines the interface only; no IdP integration is claimed.

Root fix for the 2026-10-16 copied-token incident. In that incident (see "Identity lesson" in [space-replica.md](space-replica.md#identity-lesson-2026-10-16-real-incident)) a bearer token was read from a host and replayed from another client, and the audit could not tell the actors apart. With a sender-constrained token the copied token is refused because the thief cannot produce a DPoP proof for the bound key. Honest limits: the key must be generated on the member's own host and the private key never leaves it; DPoP does not protect against an attacker who controls that host and can use the key there. The audit of the actual client (device or key fingerprint) stays, as the incident rules require. Also unchanged: the issuer keeps no copy of a token after delivery; a suspected compromise means revoke and re-issue.

## 5. Revocation and lookup

Revocation:
- Setting a card `revoked` (or `suspended`) takes effect immediately on Baton's own API: token validation consults the card status in Baton's store on every request, so no token for that card works from the next request on. A revoked card cannot be reactivated; a new card is issued.
- For parties that verify tokens or cards without calling Baton on each request, status is published two ways: a status endpoint (a per-card status resource, consistent with token revocation [RFC 7009](https://www.rfc-editor.org/rfc/rfc7009) and introspection [RFC 7662](https://www.rfc-editor.org/rfc/rfc7662) semantics), and a status list following the IETF Token Status List draft (a draft, so format details may change). For these consumers the effect is bounded by the status list's documented cache lifetime plus the token lifetime; the bound is written in the docs and tested (roadmap W2-3).
- Individual tokens can be revoked without revoking the card (RFC 7009 semantics).

Lookup:
- Anyone can fetch the public part of a card by id, from a public endpoint on the issuing instance (path to be defined; for example `/cards/<id>`). The response is the signed card of chapter 1.
- The verifier checks the JWS against the issuer's published keys, then checks `status` through the endpoint or status list. A card whose signature fails, or whose status is not `active`, is not accepted.
- Issuer keys are published by the instance (for example as a JWKS); key rotation of the issuer key is an open question.

Audit: every issue, change (keys, memberships, delegations, accountable party, external bindings, status) and revocation is written to `x_audit`, with the acting principal, the previous and new card hashes, and the reason.

## 6. Display

The board and every thread message show a card summary of the sender, so people can always tell who is speaking and on whose behalf:
- human or agent;
- for an agent, the accountable party (the terminal of the chain, with the full chain on expansion) and the runtime;
- whether the sender currently holds an approver delegation, and from whom;
- status, if not `active`.

A message records the card id and the card version (hash) at the time it was sent. The summary is rendered from that version, and the UI marks the message when the card has since been suspended or revoked, so history is not rewritten by a later change. A message sent through a delegation shows both parties ("agent X acting for person Y").

## 7. Relation to today's Baton

Today (implemented, unchanged by this document): each token maps to a principal with `agent` (the principal name), `role`, `kind` (`agent` or `human`; the former internal `service` kind belonged to the removed built-in worker's dispatcher and no longer authenticates) and `account` (the connected account); only the SHA-256 hash of the token is stored; `active` is the only status; only an offline administrator can set `kind` and `account` (`scripts/tokens.py` and the `admin` commands in the [README](../../README.md)). Tokens are long-lived bearer tokens.

Mapping:

| Today | Card |
| --- | --- |
| principal (`agent` name) | `id` = `baton:principal:<principal>` |
| `kind` human / agent | `kind` |
| `account` | an operator-managed `connected_account` attribute, not public |
| `active` true / false | `status` active / revoked (suspended is new) |
| internal `service` principals | open question (chapter 8); no public card proposed |
| token hash | stays as the token record; tokens reference the card |

Migration path (planned; all steps are additive and opt-in):
1. Card data model and storage; backfill one card per existing principal with an operator-supplied accountable party for agents. No change to authentication.
2. Signed public card endpoint and revocation status.
3. Short-lived tokens bound to the card's DPoP key, opt-in per principal; existing bearer tokens keep working for a documented window, then are refused where the operator turns enforcement on.
4. Agent Card cross-reference (with W2-1).
5. OIDC binding and human cards in the board and threads (W3-1).

Steps 1 to 4 are roadmap W2-3; step 5 is W3-1.

Operator-managed on self-hosted test instances: the instance signing key; issuing, suspending and revoking cards through the offline administrator tool; the choice to keep bearer tokens; the status-list cache lifetime; the connected-account mapping; and the Baton OIDC client configuration. A self-hosted instance needs no external IdP; it can run with administrator-issued cards and DPoP-bound tokens only. Requests and the HTTP API still cannot change a principal's own identity.

## 8. Open questions

- DID: whether to offer a DID at all, and with which method (`did:web` or other); who maintains the method.
- Whether the member view of a card (memberships and delegations) is signed separately from the public view.
- Delegation cascade: when a grantor's delegation is revoked or expires, do downstream delegations end at once (proposed) or stay until reviewed?
- Organization as a card kind or as an entry inside the accountable chain (the model above uses an entry; the owner asked for human and agent kinds only).
- Internal service principals (the `baton-service` actor that delivers webhooks and escalates; it has no client credential): no card, or a card of kind `agent` whose accountable party is the instance operator.
- Token format: opaque tokens with a store lookup (simplest instant revocation) or JWT access tokens ([RFC 9068](https://www.rfc-editor.org/rfc/rfc9068) profile, offline verification, bounded revocation).
- Default token lifetime and the status-list cache lifetime that define the documented revocation bound.
- DPoP key custody for agents on shared or ephemeral hosts (non-exportable keys, per-session keys), and key loss and recovery.
- The `AgentExtension` URI and fields for referencing an Identity Card from an Agent Card, and whether peers outside Baton can be expected to understand it.
- How SPIFFE IDs bind to cards, and whether the IETF WIMSE agent-identity drafts (see the Protocols chapter) change the model.
- Whether Feishu login yields an OIDC-conformant ID Token (not yet verified), and Feishu tenant scoping of `sub`.
- Privacy: which fields of a human card may be public, and how a human's card is deleted or erased while the audit stays append-only.
- Issuer key rotation and cross-instance trust: whether a card issued by one Baton instance is accepted by another.
