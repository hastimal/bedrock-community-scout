"""
AgentCore Runtime direct-code entry point for the Community Scout.

Amazon Bedrock AgentCore Runtime (direct code deployment) requires a root-level
``.py`` entry-point file that exposes a ``BedrockAgentCoreApp`` decorated with
``@app.entrypoint``. The Community Scout's application already defines exactly such
an app and handler in :mod:`src.scout`; this module is a THIN ADAPTER that re-exposes
them at the package root so the Runtime can locate the entry point.

No business logic lives here. Orchestration, the Strands agent loop, AgentCore Web
Search, normalization, ranking, deduplication, and response formatting all remain in
their existing modules and are unchanged. The deployment ZIP is decompressed and
mounted at ``/var/task`` (first on ``sys.path``), so ``import src.scout`` resolves
against the packaged source tree.

The Runtime invokes the ``@app.entrypoint`` handler; importing ``app`` here (and
starting it under ``__main__``) is all that is required to serve it.
"""

# Re-export the existing AgentCore application and its entry-point handler. The
# ``@app.entrypoint`` decorator in ``src.scout`` registers ``handler`` on import.
from src.scout import app, handler  # noqa: F401  (re-exported for the Runtime)

__all__ = ["app", "handler"]


if __name__ == "__main__":  # pragma: no cover - exercised only in the Runtime container
    # AgentCore Runtime starts the packaged app via this entry point. ``app.run()`` is
    # provided by BedrockAgentCoreApp and serves the registered @app.entrypoint handler.
    app.run()
