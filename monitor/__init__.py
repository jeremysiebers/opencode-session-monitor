"""Read-only local OpenCode Session Monitor.

This package polls the OpenCode HTTP API through a strictly GET-only CLI wrapper
and serves a loopback-only dashboard. It never issues a mutating OpenCode call
and never writes outside its own runtime directory.
"""
