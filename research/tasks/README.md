# Open fact-core tasks

The four hand-offs whose landing flips one of the referee's expected failures. Each `xfail` in
`research/cases.py` names one of these files as its owner, and `python3 research/cases.py` checks the
file exists.

| Task | Flips |
|---|---|
| [resolution-engine.md](resolution-engine.md) | the supersession and entity-resolution cases |
| [taint-confidence-search-page.md](taint-confidence-search-page.md) | the taint and confidence case |
| [otel-traces-native.md](otel-traces-native.md) | the native traces case |
| [authority-ab-hardening.md](authority-ab-hardening.md) | the two authority cases |

They were written before the repository was reorganised. A `docs/tasks/<name>.md` they cite that is
not in this folder is a finished or retired hand-off; read it in the git history
(`git log --all -- docs/tasks/<name>.md`). The design they implement is
[`../fact-core-v2.md`](../fact-core-v2.md).
