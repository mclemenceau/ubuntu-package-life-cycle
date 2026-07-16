PYTHON ?= python3
TEAM ?= foundations-bugs
OUT ?= dashboard.html
PORT ?= 8321
DAYS ?= 0
DB ?= $(or $(UPLC_DB),$(or $(XDG_DATA_HOME),$(HOME)/.local/share)/uplc/uplc.db)

.PHONY: help install test ingest status stuck blockers html serve db clean

help:
	@echo "uplc — Ubuntu Package Life Cycle"
	@echo ""
	@echo "  make install   pip install -e . (adds the uplc entry point)"
	@echo "  make test      run the unit test suite"
	@echo "  make ingest    fetch sources + snapshot current state (network)"
	@echo "  make status    funnel summary + proposed pipeline"
	@echo "  make stuck     blocked packages, oldest first (DAYS=N to filter)"
	@echo "  make blockers  migrations blocking the most packages"
	@echo "  make html      write the self-contained dashboard (OUT=path)"
	@echo "  make serve     serve the dashboard on localhost (PORT=n)"
	@echo "  make db        open the sqlite database with sqlite3 (DB=path)"
	@echo "  make clean     remove caches and build artifacts"
	@echo ""
	@echo "Common vars: TEAM=$(TEAM)"
	@echo "DB: $(DB)"

install:
	$(PYTHON) -m pip install -e .

test:
	$(PYTHON) -m unittest discover -s tests

ingest:
	$(PYTHON) -m uplc -v ingest --team $(TEAM)

status:
	$(PYTHON) -m uplc status --team $(TEAM)

stuck:
	$(PYTHON) -m uplc stuck --team $(TEAM) --days $(DAYS)

blockers:
	$(PYTHON) -m uplc blockers --team $(TEAM)

html:
	$(PYTHON) -m uplc html --team $(TEAM) -o $(OUT)

serve:
	$(PYTHON) -m uplc serve --team $(TEAM) -p $(PORT)

db:
	sqlite3 $(DB)

clean:
	find . -name '__pycache__' -type d -exec rm -rf {} +
	rm -rf *.egg-info dashboard.html
