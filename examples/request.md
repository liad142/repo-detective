# Repo Detective Report: request/request

- **Investigation ID:** `a8922313-21ec-4e83-95ef-feb35b4886a7`
- **Status:** `completed`
- **Revision:** 1
- **Repository:** https://github.com/request/request
- **Goal:** Should our engineering team adopt this open-source project?
- **LLM investigation calls:** 2 / 30
- **Remaining investigation calls:** 28

## Verdict

**REJECT** (confidence: high)

Do not adopt request/request for new engineering work. Its README states that it has been fully deprecated since February 11, 2020 and that no new changes are expected; repository metadata also shows no push since August 14, 2024 and no latest GitHub Release was found.

### Positive signals

- The repository is public, unarchived, Apache-2.0 licensed, and widely used historically, with 25,512 stars and 3,145 forks. (observed; `EV-E70C41ACCD`)

### Risk factors

- The project maintainers explicitly state that request is fully deprecated and no new changes are expected. (observed; `EV-142810C8A9`)
- Repository metadata records the last push on August 14, 2024, while the latest GitHub Release endpoint returned no release. (observed; `EV-E70C41ACCD`, `EV-0C7940CDB7`)
- A deprecated HTTP client is unsuitable for new adoption because future compatibility and security fixes should not be expected. (inference; `EV-142810C8A9`)

### Unverified items

- The current npm package version and resolved dependency versions were not examined.
- Known vulnerabilities and malware advisories were not checked because explicit deprecation already determines the adoption decision.
- The README-linked alternatives were not evaluated.

## Intake snapshot

- Description: 🏊🏾 Simplified HTTP request client.
- Stars: 25512
- Forks: 3145
- Archived: False
- Default branch: master
- Last push: 2024-08-14T00:09:41Z
- Latest release: not verified/found

## Investigation log

### Step 1 - github_tool

- **Why:** The repository is unarchived but has no GitHub release and no push since 2024; project guidance can confirm whether it is deprecated or merely stable.
- **Question:** Does the README declare this project deprecated or recommend a maintained successor?
- **Based on:** EV-E70C41ACCD, EV-0C7940CDB7
- **Action:** read_repository_file
- **Result:** Read README.md from request/request (24000 characters, truncated)
- **Evidence:** EV-142810C8A9

### Step 2 - submit_verdict

- **Why:** The maintainers explicitly declare the project fully deprecated with no expected changes, directly satisfying the rejection criterion; further security or activity checks would not make a deprecated project suitable for new adoption.
- **Question:** N/A
- **Based on:** EV-142810C8A9, EV-E70C41ACCD, EV-0C7940CDB7
- **Action:** submit_verdict
- **Result:** Submitted verdict: reject
- **Evidence:** EV-142810C8A9

## Evidence

| ID | Tool | Status | Summary | Source |
|---|---|---|---|---|
| `EV-E70C41ACCD` | `get_repository` | `verified` | Repository request/request: 25512 stars, archived=False, last push=2024-08-14T00:09:41Z | [link](https://github.com/request/request) |
| `EV-0C7940CDB7` | `get_latest_release` | `not_found` | No latest GitHub Release was found | [link](https://api.github.com/repos/request/request/releases/latest) |
| `EV-763C30770A` | `list_top_contributors` | `verified` | Top 10 contributor sample: mikeal accounts for 38.4% of sampled contributions | [link](https://github.com/request/request/graphs/contributors) |
| `EV-142810C8A9` | `read_repository_file` | `verified` | Read README.md from request/request (24000 characters, truncated) | [link](https://github.com/request/request/blob/master/README.md) |

## Method note

This report was rendered deterministically from the stored verdict, investigation log, and GitHub API evidence. No LLM call was used to write it.

