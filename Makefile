PY ?= python3.12

.PHONY: test demo red-check

test:
	$(PY) -m pytest

demo:
	$(PY) scripts/demo.py

red-check:
	$(PY) scripts/red_check.py
