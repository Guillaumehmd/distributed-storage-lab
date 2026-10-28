# Distributed Storage Lab: every operation is a make target.
#   make help      list targets
#   make up load verify placement

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

COMPOSE  ?= docker compose
ENDPOINT ?= http://127.0.0.1:8333
COUNT    ?= 1000
DURATION ?= 180
LABEL    ?= run
VENV     := .venv
PY       := $(VENV)/bin/python

.PHONY: help venv up down clean ps logs load traffic verify placement watch-placement \
        volume-list weed shell chaos netem-test vol5 test

help: ## list the targets
	@grep -E '^[a-z0-9-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "} {printf "  \033[1m%-16s\033[0m %s\n", $$1, $$2}'

$(VENV)/.installed: requirements.txt
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install --quiet --upgrade pip
	$(VENV)/bin/pip install --quiet -r requirements.txt
	touch $@

venv: $(VENV)/.installed ## create the Python virtualenv (needs python3-venv)

# ---------------------------------------------------------------- cluster

up: ## start the cluster and wait until it serves traffic (< 2 min)
	$(COMPOSE) up -d
	python3 scripts/wait_ready.py --volumes 4 --racks 2 --timeout 120

down: ## stop and remove the containers, keep the data
	$(COMPOSE) --profile scale down

clean: ## remove containers AND data volumes, reset the manifest
	$(COMPOSE) --profile scale down --volumes --remove-orphans
	rm -rf .state

ps: ## container states
	$(COMPOSE) --profile scale ps -a

logs: ## follow logs, e.g. make logs S=vol3
	$(COMPOSE) logs -f --tail=100 $(S)

vol5: ## add a 5th volume server (rackA, or VOL5_RACK=rackB) for the rebalancing test
	$(COMPOSE) --profile scale up -d vol5
	python3 scripts/wait_ready.py --volumes 5 --racks 2 --timeout 60

# ------------------------------------------------------------------- data

load: venv ## write COUNT objects (1 KiB-16 MiB) and record their SHA-256
	$(PY) scripts/load.py --endpoint $(ENDPOINT) --count $(COUNT)

traffic: venv ## read/write traffic during a scenario, e.g. make traffic LABEL=s1 DURATION=180
	$(PY) scripts/load.py --endpoint $(ENDPOINT) --continuous --duration $(DURATION) --label $(LABEL)

verify: venv ## re-read every object of the manifest and compare hashes
	$(PY) scripts/verify.py --endpoint $(ENDPOINT)

placement: ## check that every volume has its copies on both racks
	python3 scripts/placement.py

watch-placement: ## one line every 5 s: under-replicated volumes, live volume servers
	python3 scripts/placement.py --watch 5

# ------------------------------------------------------------- operations

volume-list: ## weed shell: volume.list
	./scripts/weed.sh volume.list

weed: ## weed shell commands, e.g. make weed CMD="cluster.ps" or CMD="help volume.balance"
	./scripts/weed.sh "$(CMD)"

shell: ## interactive weed shell
	./scripts/weed.sh

chaos: ## fault injection, e.g. make chaos ARGS="kill vol3" (make chaos ARGS=help)
	./scripts/chaos.sh $(ARGS)

netem-test: ## check that tc netem works on this kernel (T02)
	./scripts/chaos.sh netem-test

# ------------------------------------------------------------------ tests

test: up venv ## smoke test used by CI: 100 objects written, verified, placement checked
	$(PY) scripts/load.py --endpoint $(ENDPOINT) --count 100 --max-size 1MiB \
		--prefix smoke/ --manifest .state/smoke.jsonl
	$(PY) scripts/verify.py --endpoint $(ENDPOINT) --manifest .state/smoke.jsonl
	python3 scripts/placement.py > /dev/null && echo "placement: ok"
