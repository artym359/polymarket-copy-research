# Security and privacy

This project is intended for public-data research and local simulation.

Before running it, keep the following outside Git:

- `.env` files and credentials;
- wallet seed lists, private identifiers and account metadata;
- SQLite databases, raw API responses, logs and exported reports;
- browser sessions or any authenticated trading material.

If a credential is ever committed, revoke it immediately and remove it from the complete Git history. Deleting the file in a later commit is not sufficient.
