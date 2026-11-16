PYTHON ?= .venv-dashboard/bin/python
NODE ?= node

.PHONY: test test-live

test:
	$(PYTHON) -m unittest discover -s tests -v
	$(NODE) --test tests/test_dashboard_js.cjs
	$(NODE) --check job_search/static/dashboard.js

test-live:
	$(PYTHON) -m unittest discover -s tests/integration -v
