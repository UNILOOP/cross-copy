.PHONY: install dev test

# User install via the installer script (pipx or dedicated venv)
install:
	./install.sh

# Editable install into the current environment for development
dev:
	python3 -m pip install -e .

test:
	python3 -m pytest
