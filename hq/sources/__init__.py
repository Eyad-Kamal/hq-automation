"""Read-only clients for competitive-programming platforms.

Each module (``codeforces``, ``atcoder``, ``leetcode``) exposes plain functions
that take a ``requests.Session`` and return models from ``hq.models``. They
never raise on network or parse failure; they log a warning and return an
empty list (or empty dict).
"""
