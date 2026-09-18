# Development
No third-party runtime dependencies; `uv` supplies the interpreter via PEP 723 inline metadata.

```bash
uv run --with pytest pytest pr_notify_test.py   # unit tests
uv run pr_notify.py                             # needs GH_TOKEN and TEAMS_WEBHOOK_URL
```
