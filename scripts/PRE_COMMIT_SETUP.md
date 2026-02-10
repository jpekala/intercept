# Pre-Commit Hook Setup

This directory contains scripts for managing pre-commit hooks.

## Docker Version Auto-Update

The `pre-commit-update-docker-version.sh` script automatically validates that the Docker build workflow uses the correct version from `config.py`.

### Installation

Run the setup script to install the pre-commit hook:

```bash
./scripts/install-pre-commit-hook.sh
```

### How it Works

Before each git commit:
1. The hook extracts the `VERSION` from `config.py`
2. Verifies consistency with the Docker build workflow
3. Ensures the version is properly synced

### Manual Hook Removal

To remove the pre-commit hook:

```bash
rm .git/hooks/pre-commit
```

### Re-installation

If you need to reinstall or update the hook:

```bash
./scripts/install-pre-commit-hook.sh
```

## Notes

- The Docker workflow now dynamically reads the version from `config.py` at build time
- The pre-commit hook serves as a safety check to ensure version consistency
- All developers should run the installation script after cloning the repository
