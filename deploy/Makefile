ENV ?= secrets/.env
UV ?= uv
DOCKER ?= docker
PYTHON = $(UV) run --python 3.12 --with pyyaml==6.0.2 python
RUN = $(PYTHON) prepare.py
EDGE = $(DOCKER) compose --env-file $(ENV) -f docker-compose.yml
DIFY = $(DOCKER) compose --env-file secrets/dify.env -f data/dify/docker/eb-compose.yaml
HR = $(DOCKER) compose --env-file $(ENV) -f data/frappe/eb-compose.yaml
.PHONY: init prepare config verify build up wechat status down policy-sync hr-assistant wecom-cards
init:
	$(RUN) init
prepare:
	$(RUN) $(ENV)
config: prepare
	$(EDGE) --profile wechat config --quiet
	$(DIFY) config --quiet
	$(HR) --profile bootstrap --profile policy-sync --profile hr-assistant config --quiet
verify:
	$(PYTHON) validate.py
	$(PYTHON) -m unittest discover -s tests -p 'test_backup_bundle.py'
	$(PYTHON) -m unittest discover -s tests -p 'test_backup_collect.py'
	$(PYTHON) -m unittest discover -s tests -p 'test_backup_entry.py'
	$(PYTHON) -m unittest discover -s tests -p 'test_restore_guard.py'
	$(PYTHON) -m unittest discover -s tests -p 'test_restore_frappe.py'
build: prepare
	$(DOCKER) build --platform linux/amd64 -f data/frappe/images/custom/Containerfile --secret id=apps_json,src=apps.lock.json --build-arg FRAPPE_BRANCH=v15.123.1 --build-arg PYTHON_VERSION=3.12.12 --build-arg DEBIAN_BASE=bookworm --build-arg NODE_VERSION=22.20.0 --build-arg INSTALL_CHROMIUM=false --build-arg BUILDKIT_SYNTAX=docker/dockerfile:1.19.0 -t enterprise-base/frappe:15.123.1-hrms15.64.3-wiki3.4.0 data/frappe
	$(DOCKER) build -f Frappe.Containerfile -t enterprise-base/frappe:15.123.1-hrms15.64.3-wiki3.4.0-eb0.1.0 ../frappe_apps/enterprise_base
up: config
	$(RUN) check $(ENV)
	$(DOCKER) image inspect enterprise-base/frappe:15.123.1-hrms15.64.3-wiki3.4.0-eb0.1.0 >/dev/null 2>&1 || $(MAKE) build
	$(EDGE) up -d --force-recreate caddy
	$(PYTHON) wecom_cards.py $(ENV) --network
	$(DIFY) up -d --wait --wait-timeout 600
	$(HR) up -d --wait --wait-timeout 600
	$(HR) run --rm create-site
wechat: prepare
	$(RUN) wechat $(ENV)
	$(EDGE) --profile wechat up -d wechat
wecom-cards: wechat
	$(PYTHON) wecom_cards.py $(ENV)
	$(HR) up -d --force-recreate frontend backend queue-short queue-long scheduler
	$(EDGE) up -d --force-recreate caddy
status:
	$(EDGE) --profile wechat ps
	$(DIFY) ps
	$(HR) --profile policy-sync --profile hr-assistant ps
down:
	$(HR) --profile policy-sync --profile hr-assistant down
	$(DIFY) down
	$(EDGE) --profile wechat down

policy-sync: prepare
	$(RUN) policy-sync $(ENV)
	$(HR) --profile policy-sync up -d --wait --wait-timeout 300 policy-sync

hr-assistant: prepare
	$(RUN) hr-assistant $(ENV)
	$(DIFY) up -d --force-recreate ssrf_proxy
	$(HR) --profile hr-assistant up -d --wait --wait-timeout 300 kb-gateway frappe-mcp

.PHONY: backup
backup:
	@$(PYTHON) backup.py "$(ENV)"
