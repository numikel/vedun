"""HTTP service around the knowledge base: REST API, background jobs, optional dashboard and MCP mounts.

``vedun.server.app:app`` is the ASGI application; ``vedun.server.jobs`` is the job contract used by the dashboard.
Nothing is imported here on purpose: the dashboard probes ``vedun.server.jobs`` with ``importlib``.
"""
