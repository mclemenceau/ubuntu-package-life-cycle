PYTHON ?= python3
TEAM ?= foundations-bugs
OUT ?= dashboard
PORT ?= 8321
DAYS ?= 0
DB ?= $(or $(UPLC_DB),$(or $(XDG_DATA_HOME),$(HOME)/.local/share)/uplc/uplc.db)

.PHONY: help install test ci ingest bugs-sync refresh status stuck blockers html serve db clean

help:
	@echo "uplc — Ubuntu Package Life Cycle"
	@echo ""
	@echo "  make install   pip install -e . (adds the uplc entry point)"
	@echo "  make test      run the unit test suite"
	@echo "  make ci        reproduce the GitHub Actions CI job locally"
	@echo "  make ingest    fetch sources + snapshot current state (network)"
	@echo "  make bugs-sync sync team bugs from Launchpad (network; first run heavy)"
	@echo "  make refresh   ingest + bugs-sync, in that order"
	@echo "  make status    funnel summary + proposed pipeline"
	@echo "  make stuck     blocked packages, oldest first (DAYS=N to filter)"
	@echo "  make blockers  migrations blocking the most packages"
	@echo "  make html      write the dashboard site (OUT=dir, or OUT=x.html for overview only)"
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

# Mirrors .github/workflows/ci.yml (minus the version matrix): CI installs
# with pip, but this box runs stock system Python with PyYAML from apt, no
# pip — so check the dependency directly instead of via `pip install -e .`,
# and exercise the package the same no-install way every other target here
# does. Catches the same regressions without needing pip present.
ci:
	$(PYTHON) -c "import yaml" || (echo "PyYAML missing — apt install python3-yaml, or pip install PyYAML" >&2; exit 1)
	$(PYTHON) -m unittest discover -s tests -v
	$(PYTHON) -m uplc --help

ingest:
	$(PYTHON) -m uplc -v ingest --team $(TEAM)

bugs-sync:
	$(PYTHON) -m uplc -v bugs-sync --team $(TEAM)

# Sequential on purpose: bugs-sync reads the latest snapshot to find
# pipeline-referenced bugs, so it must run after ingest (not under -j).
refresh:
	$(PYTHON) -m uplc -v ingest --team $(TEAM)
	$(PYTHON) -m uplc -v bugs-sync --team $(TEAM)

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
	rm -rf *.egg-info dashboard.html dashboard/
