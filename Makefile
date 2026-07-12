.PHONY: install dev sync-core lint test login-check plan build clean

install:
	python -m pip install -e .

dev:
	python -m pip install -e ".[dev]"

# moss_core.py is shared; deploy/moss_core.py is the source of truth.
sync-core:
	cp deploy/moss_core.py deploy/modal/moss_core.py
	cp deploy/moss_core.py deploy/runpod/moss_core.py
	cp deploy/moss_core.py deploy/cerebrium/moss_core.py

lint:
	python -m ruff check .

test:
	python -m pytest -q

login-check:
	python -m gpuhedge login-check

plan:
	python -m gpuhedge plan

build:
	python -m build

clean:
	rm -rf build dist src/*.egg-info .pytest_cache .ruff_cache
