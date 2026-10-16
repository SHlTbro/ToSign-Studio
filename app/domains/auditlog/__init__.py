"""Audit-log read-only query domain (T58B, contract v0.2 section 8).

Reuses the core-owned ``audit_logs`` table (app/core/audit.py) — no new
table, no write path. ``GET /api/v1/audit-logs`` is admin-only (v0.2.1
section 7 gate pattern, same as the backups domain) with server-side
filters and pagination; the page never receives the full table.
"""
