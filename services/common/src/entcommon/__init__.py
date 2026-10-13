"""Code shared by the producer, ingestion and UI backend services (entingestion-common).

Modules: authz (ARN matching and the per-source access policy), paging (next_token), dynamo
(Decimal conversion and error inspection), sigv4 (execute-api signing), timeutil (UTC ISO times).
"""

__version__ = "0.1.0"
