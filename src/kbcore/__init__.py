"""kbcore — the transaction-scoped service layer for the Knowledge Vault.

Every write to Postgres goes through this package (PLAN.md Invariant 2). MCP,
worker, and CLI call kbcore functions; they never touch SQL directly.
"""
