# Documentation map

This directory separates documents by role so a maintainer does not have to
guess which file is authoritative.

| Need | Canonical document | Scope |
| --- | --- | --- |
| First install, first launch, common mistakes | [GETTING_STARTED.md](GETTING_STARTED.md) | End-user onboarding and troubleshooting |
| Current release health, test evidence, blockers | [STATUS.md](STATUS.md) | Current, time-bound facts |
| Remaining risk register and release judgement | [RISK_ASSESSMENT.md](RISK_ASSESSMENT.md) | Probability/impact/evidence/residual risk |
| Take over development, run, test, or recover deployment | [HANDOVER.md](HANDOVER.md) | Operational handover |
| Design and component boundaries | [ARCHITECTURE.md](ARCHITECTURE.md) | System map |
| Build a Windows package | [BUILDING.md](BUILDING.md) | Build procedure |
| Decide whether a release can publish | [RELEASE.md](RELEASE.md) | Release gate |
| Trust boundaries and residual risk | [SECURITY_MODEL.md](SECURITY_MODEL.md) | Security design |
| Wire up retrieval-augmented generation | [RAG.md](RAG.md) | RAG building blocks, recipes, pitfalls |
| Material changes and incidents | [HISTORY.md](HISTORY.md) | Project history |

Historical material from earlier phases is kept by the maintainer outside the
published tree. It is retained evidence, not current instructions, and must not
override the documents above. No document or command in this directory depends
on it, so a clone of this repository is self-contained.
