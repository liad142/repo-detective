# Repo Detective Report: RIAEvangelist/node-ipc

- **Investigation ID:** `48052c2e-d266-4e4d-b723-d88592a0cac3`
- **Status:** `completed`
- **Revision:** 1
- **Repository:** https://github.com/RIAEvangelist/node-ipc
- **Goal:** Should our engineering team adopt this open-source project?
- **LLM investigation calls:** 7 / 30
- **Remaining investigation calls:** 23

## Verdict

**REJECT** (confidence: high)

Reject node-ipc. GitHub advisories document repeated maintainer-introduced malicious or hidden behavior in this npm package, including critical code intended to overwrite arbitrary files based on user geolocation. A recent 14.0.0 release and active repository do not restore sufficient trust in the maintainer-controlled supply chain.

### Positive signals

- The repository is active and published version 14.0.0 on 2026-08-24. (observed; `EV-92174311BC`, `EV-59B6B0EBD4`)
- Version 14.0.0 includes a detailed security policy and declares hardened Guarded and Assured profiles. (observed; `EV-E84D52767F`, `EV-EA259FFC8B`)

### Risk factors

- A critical reviewed advisory states that the maintainer introduced embedded malicious code in versions 10.1.1–10.1.2 intended to overwrite arbitrary files based on user geolocation. (observed; `EV-46E35E07C6`)
- Another reviewed advisory documents maintainer-introduced hidden functionality in version 9.2.2, while a separate advisory covers an unsolicited desktop-writing behavior in 11.x. (observed; `EV-46E35E07C6`)
- A malware advisory identifies version 9.1.6 as critical and advises treating systems running it as fully compromised. (observed; `EV-DD6B4FAA4B`)
- Repeated intentional behavior attributed to the maintainer makes the maintainer-controlled package supply chain untrustworthy, notwithstanding later clean-looking releases or policy documents. (inference; `EV-DD6B4FAA4B`, `EV-46E35E07C6`, `EV-59B6B0EBD4`, `EV-E84D52767F`)

### Unverified items

- The failed 9.1.5-to-9.1.6 ref comparison did not verify the malicious patch's commit provenance because those refs were not found in this repository.
- The investigation did not independently audit the full 14.0.0 source or published npm tarball; doing so would not resolve the established maintainer-trust failure.

## Intake snapshot

- Description: A nodejs module for local and remote Inter Process Communication (IPC), Neural Networking, and able to facilitate machine learning.
- Stars: 64
- Forks: 11
- Archived: False
- Default branch: main
- Last push: 2026-08-24T22:18:46Z
- Latest release: 14.0.0

## Investigation log

### Step 1 - github_tool

- **Why:** A four-day-old major release and 68.4% sampled contribution concentration make package identity, install guidance, and security/deprecation notices the most decision-relevant unknowns.
- **Question:** What manifests and project guidance identify the published package, supported version, security posture, and any deprecation or migration warning?
- **Based on:** EV-59B6B0EBD4, EV-58D6FCD030, EV-92174311BC
- **Action:** read_repository_file
- **Result:** Listed 24 entries in RIAEvangelist/node-ipc
- **Evidence:** EV-528B16A0C8

### Step 2 - github_tool

- **Why:** The root listing confirms an npm manifest and lockfile; reading the manifest now identifies the installable package, release scripts, dependency ranges, and whether deeper release or vulnerability checks are warranted.
- **Question:** What package identity, runtime requirements, dependencies, and lifecycle or release scripts does version 14.0.0 declare?
- **Based on:** EV-528B16A0C8, EV-59B6B0EBD4
- **Action:** read_repository_file
- **Result:** Read package.json from RIAEvangelist/node-ipc (4929 characters)
- **Evidence:** EV-EA259FFC8B

### Step 3 - github_tool

- **Why:** The installable package is confirmed as node-ipc 14.0.0, and the repository ships a substantial SECURITY.md; its supported-version and incident guidance could directly determine trust and adoption.
- **Question:** Does SECURITY.md disclose past supply-chain incidents, supported versions, or project-specific security risks relevant to adopting 14.0.0?
- **Based on:** EV-EA259FFC8B, EV-528B16A0C8, EV-59B6B0EBD4
- **Action:** read_repository_file
- **Result:** Read SECURITY.md from RIAEvangelist/node-ipc (6332 characters)
- **Evidence:** EV-E84D52767F

### Step 4 - github_tool

- **Why:** The supported npm package is network-facing and security-sensitive; checking malware advisories is the fastest way to detect a project-specific supply-chain trust failure before evaluating routine maintenance.
- **Question:** Has the npm package node-ipc ever been identified in GitHub’s advisory database as malware?
- **Based on:** EV-EA259FFC8B, EV-E84D52767F
- **Action:** search_global_advisories
- **Result:** Global advisory search found 1 malware advisories affecting node-ipc
- **Evidence:** EV-DD6B4FAA4B

### Step 5 - github_tool

- **Why:** A critical malware advisory identifies version 9.1.6; comparing it with the preceding release can verify the malicious changes and expose their commit provenance, directly testing whether this supply chain is trustworthy.
- **Question:** What changed between 9.1.5 and malware-flagged 9.1.6, and which commits introduced those changes?
- **Based on:** EV-DD6B4FAA4B, EV-EA259FFC8B
- **Action:** compare_refs
- **Result:** Ref comparison could not be verified: Not Found
- **Evidence:** EV-FD0355C357

### Step 6 - github_tool

- **Why:** A critical malware advisory targets the same npm package, but the failed tag comparison leaves provenance unresolved. Reviewed advisories may independently document affected releases, behavior, and remediation needed for a trust decision.
- **Question:** Do reviewed GitHub advisories corroborate intentional malicious behavior or other security exposure in node-ipc, and which versions are affected?
- **Based on:** EV-DD6B4FAA4B, EV-FD0355C357, EV-EA259FFC8B
- **Action:** search_global_advisories
- **Result:** Global advisory search found 3 reviewed advisories affecting node-ipc
- **Evidence:** EV-46E35E07C6

### Step 7 - submit_verdict

- **Why:** Multiple advisories establish repeated maintainer-introduced malicious or hidden behavior in the same npm package, including arbitrary file overwrites. This is sufficient to reject the supply chain despite current activity and a newer release.
- **Question:** N/A
- **Based on:** EV-DD6B4FAA4B, EV-46E35E07C6, EV-EA259FFC8B, EV-59B6B0EBD4, EV-92174311BC, EV-E84D52767F
- **Action:** submit_verdict
- **Result:** Submitted verdict: reject
- **Evidence:** EV-46E35E07C6, EV-DD6B4FAA4B, EV-EA259FFC8B

## Evidence

| ID | Tool | Status | Summary | Source |
|---|---|---|---|---|
| `EV-92174311BC` | `get_repository` | `verified` | Repository RIAEvangelist/node-ipc: 64 stars, archived=False, last push=2026-08-24T22:18:46Z | [link](https://github.com/RIAEvangelist/node-ipc) |
| `EV-59B6B0EBD4` | `get_latest_release` | `verified` | Latest GitHub Release is 14.0.0, published 2026-08-24T22:19:02Z | [link](https://github.com/RIAEvangelist/node-ipc/releases/tag/14.0.0) |
| `EV-58D6FCD030` | `list_top_contributors` | `verified` | Top 10 contributor sample: RIAEvangelist accounts for 68.4% of sampled contributions | [link](https://github.com/RIAEvangelist/node-ipc/graphs/contributors) |
| `EV-528B16A0C8` | `read_repository_file` | `verified` | Listed 24 entries in RIAEvangelist/node-ipc | [link](https://github.com/RIAEvangelist/node-ipc/tree/main) |
| `EV-EA259FFC8B` | `read_repository_file` | `verified` | Read package.json from RIAEvangelist/node-ipc (4929 characters) | [link](https://github.com/RIAEvangelist/node-ipc/blob/14.0.0/package.json) |
| `EV-E84D52767F` | `read_repository_file` | `verified` | Read SECURITY.md from RIAEvangelist/node-ipc (6332 characters) | [link](https://github.com/RIAEvangelist/node-ipc/blob/14.0.0/SECURITY.md) |
| `EV-DD6B4FAA4B` | `search_global_advisories` | `verified` | Global advisory search found 1 malware advisories affecting node-ipc | [link](https://github.com/advisories) |
| `EV-FD0355C357` | `compare_refs` | `not_found` | Comparison could not be verified: Not Found | [link](https://api.github.com/repos/RIAEvangelist/node-ipc/compare/9.1.5...9.1.6) |
| `EV-46E35E07C6` | `search_global_advisories` | `verified` | Global advisory search found 3 reviewed advisories affecting node-ipc | [link](https://github.com/advisories) |

## Method note

This report was rendered deterministically from the stored verdict, investigation log, and GitHub API evidence. No LLM call was used to write it.

