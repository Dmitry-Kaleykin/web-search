# Historical autonomous research design

The active design is documented in [ARCHITECTURE.md](../ARCHITECTURE.md). The server exposes
`web_search` for discovery and `read_url` for source access. Research reasoning belongs to the caller.

Earlier versions implemented research inside the server using a controller, model clients, a
reranker, an evidence ledger, and page/source/coverage thresholds. Stage 3 removes those unused
modules, configuration fields, and tests. They are not supported library APIs or runtime options.
Their evaluations must not be used as evidence of the current caller's research quality.

The full historical design, implementation, and tests are preserved at Git revision `593d65f`.
Inspect them without changing your checkout:

```sh
git show 593d65f:docs/legacy-research-architecture.md
git show 593d65f:src/web_research/controller.py
git ls-tree -r --name-only 593d65f src/web_research tests
```

Existing SQLite research journals remain untouched for historical inspection. They are no longer
written by the application. The Stage 1 caller policy used for benchmark comparisons is retained as
packaged benchmark data; that comparison evaluates caller instructions on today's retrieval tools,
not the retired controller.
