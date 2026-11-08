# Documentation map

This directory separates documents by role so a reader does not have to guess
which file is authoritative.

The project README exists in two languages and both are maintained:
[`README.md`](../README.md) (简体中文) and [`README.en.md`](../README.en.md)
(English). Documents below stay in whichever language they were written in;
there is no requirement that every technical document be bilingual.

## Using LunarShroud

| Need | Document |
| --- | --- |
| Install, first launch, common mistakes, failure triage | [GETTING_STARTED.md](GETTING_STARTED.md) |
| What changed in each version | [../CHANGELOG.md](../CHANGELOG.md) |
| Build a Windows package from source | [BUILDING.md](BUILDING.md) |
| Publish a release (gate and checklist) | [RELEASE.md](RELEASE.md) |

## Understanding the design

| Need | Document |
| --- | --- |
| Component boundaries and data flow | [ARCHITECTURE.md](ARCHITECTURE.md) |
| 1.0 shell: tokens, layout, surface hierarchy | [DESIGN_1.0.md](DESIGN_1.0.md) |
| Model manifests and runtime profiles | [MANIFEST_AND_RUNTIME_MODEL.md](MANIFEST_AND_RUNTIME_MODEL.md) |
| Trust boundaries | [SECURITY_MODEL.md](SECURITY_MODEL.md) |
| Offline / no-telemetry contract and its automated guard | [OFFLINE.md](OFFLINE.md) |

## Knowledge base and RAG

| Need | Document |
| --- | --- |
| RAG building blocks, recipes, pitfalls | [RAG.md](RAG.md) |
| Indexing, quotas, citations, measured retrieval quality | [KNOWLEDGE_BASE.md](KNOWLEDGE_BASE.md) |

## Contributing

| Need | Document |
| --- | --- |
| Engineering rules and invariants before touching the code | [../AGENTS.md](../AGENTS.md) |
| Contribution process | [../CONTRIBUTING.md](../CONTRIBUTING.md) |
| Code of conduct | [../CODE_OF_CONDUCT.md](../CODE_OF_CONDUCT.md) |
| Dependency baseline | [../DEPENDENCY_AUDIT.md](../DEPENDENCY_AUDIT.md) |

Maintainer working material — per-round test evidence, the risk register, the
internal roadmap, the event timeline and the handover/recovery steps — is kept
in the maintainer's tree and is deliberately **not** published here. It records
how the work was done rather than what a user or contributor needs, and the
user-visible outcome of it is consolidated in
[../CHANGELOG.md](../CHANGELOG.md).

## Documented provenance

Two sources are recorded in the project READMEs under "致谢与来源" /
"Acknowledgements and provenance":

- AI-assisted development by **DeepSeek-V4.1-Flash**
  (`deepseek-v4.1-flash-10-17`).
- Frontend design rules from the **polishkit** plugin (v0.1.2, formerly
  `frontend-aesthetics`),
  <https://github.com/1LunarVeil/polishkit.git> — a method and token
  specification, not a runtime dependency, and not part of the shipped
  artifacts.
