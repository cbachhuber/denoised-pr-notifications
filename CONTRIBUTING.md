# Development

Install uv with `curl -LsSf https://astral.sh/uv/install.sh | sh`, execute unit tests with `uv run pytest`.

This repository's lint rules live in [.pre-commit-config.yaml](.pre-commit-config.yaml) and are executed with [prek](https://prek.j178.dev/), a fast drop-in replacement for `pre-commit`.
You can locally run the same checks that CI runs.

Install prek with any method from the [prek installation guide](https://prek.j178.dev/installation/), for example:

```bash
uv tool install prek
```

Then install the Git hooks so the checks run before every commit:

```bash
prek install
```

To check the whole repository on demand:

```bash
prek run --all-files
```
