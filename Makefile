SHELL := /bin/bash

# Configuration
HOST ?= 127.0.0.1
PORT ?= 8989
TEST_PORT ?= 7979
UDS  ?= $(XDG_RUNTIME_DIR)/rclipboard.sock
KEY  ?= key.pem
CERT ?= cert.pem
LOG_LEVEL ?= warning
USER_CONFIG_HOME := $(if $(XDG_CONFIG_HOME),$(XDG_CONFIG_HOME),$(HOME)/.config)

# Proxy upstream config
UPSTREAM_HOST ?= 127.0.0.1
UPSTREAM_PORT ?= 8989
UPSTREAM_TEST_PORT ?= 7979
UPSTREAM_UDS  ?= $(XDG_RUNTIME_DIR)/rclipboard.sock
RCLIPBOARD_LOG_LEVEL := $(LOG_LEVEL)
RCLIPBOARD_PY_LOG_LEVEL := INFO

# Docker
IMAGE ?= rclipboard:latest
TUNEL_TEST_IMAGE ?= rcliptunel-test:latest
DEPLOY_TEST_IMAGE ?= rclipboard-deploy-test:latest

.PHONY: help install run run-dev run-uds run-https run-proxy run-dev-proxy cert cert-san health status topics docker-build docker-run docker-run-proxy docker-build-tunel-test plugin-install plugin-uninstall plugin-reload plugin-demo smoke proxy-smoke test test-functional test-integration test-http test-ws test-proxy-integration test-tunel test-https test-wss test-ssl-proxy-integration test-ssl test-soak test-soak-full test-install-docker test-install-docker-systemd service-install service-update service-reset service-uninstall systemd-user-install systemd-user-enable systemd-user-disable nvim-plugin-install nvim-plugin-pack install-no-systemd setup-wizard deploy deploy-dry docker-build-deploy-test test-deploy

help:
	@echo "Targets:"
	@echo "  install     - install Python deps"
	@echo "  install-exe - install Executable scripts"
	@echo "  run         - run server (TCP)"
	@echo "  run-dev     - run server with reload (TCP)"
	@echo "  run-uds     - run server on Unix Domain Socket"
	@echo "  run-uds-dev - run server with reload on Unix Domain Socket"
	@echo "  run-https   - run server with HTTPS (uses $(KEY)/$(CERT))"
	@echo "  run-proxy   - run server in proxy mode (env-driven upstream)"
	@echo "  run-proxy-dev - run server with reload + proxy mode"
	@echo "  cert        - generate self-signed cert (CN=localhost)"
	@echo "  cert-san    - generate self-signed cert with SANs"
	@echo "  status      - GET /status"
	@echo "  topics      - GET /topics"
	@echo "  health      - GET /health"
	@echo "  docker-build - build Docker image ($(IMAGE))"
	@echo "  docker-run   - run Docker image mapping port $(PORT)"
	@echo "  docker-run-proxy - run Docker image with proxy env"
	@echo "  docker-build-tunel-test - build SSH test image for tunnel tests"
	@echo "  plugin-install - install tmux plugin (symlink to ~/.tmux/plugins/tmux-rclipboard)"
	@echo "  plugin-uninstall - remove installed tmux plugin"
	@echo "  plugin-reload - reload tmux config to pick up plugin"
	@echo "  plugin-demo   - launch a temporary tmux session to test plugin"
	@echo "  smoke         - quick HTTP smoke test (health/clip/fetch)"
	@echo "  proxy-smoke   - start upstream+proxy servers and verify replication both ways"
	@echo "  test          - run all automated tests"
	@echo "  test-tunel    - SSH tunnel smoke tests (requires docker-build-tunel-test)"
	@echo "  test-functional - run functional HTTP/WS tests"
	@echo "  test-integration - run integration tests"
	@echo "  test-install-docker - installer tests in Docker (no systemd)"
	@echo "  test-install-docker-systemd - installer tests in Docker with real systemd (privileged)"
	@echo "  service-install      - install the per-user service"
	@echo "  service-update       - update the installed service from Git"
	@echo "  service-reset        - recreate managed installation artifacts"
	@echo "  service-uninstall    - uninstall the per-user service"
	@echo "  systemd-user-install - compatibility alias for service-install"
	@echo "  systemd-user-enable  - enable & start rclipboard.service"
	@echo "  systemd-user-disable - disable rclipboard.service"
	@echo "  install-no-systemd   - install service definitions without activating them"
	@echo "  setup-wizard         - run interactive rclipboard configuration wizard"
	@echo "  deploy               - deploy to all hosts in deploy/hosts"
	@echo "  deploy-dry           - dry-run deploy (print commands only)"
	@echo "  docker-build-deploy-test - build deploy/install integration test image"
	@echo "  test-deploy          - deploy + install + wizard integration tests (requires docker)"
	@echo "  nvim-plugin-install  - luarocks make (local) nvim-rclipboard"
	@echo "  nvim-plugin-pack     - luarocks pack rock for nvim-rclipboard"

.venv:
	pip install uv && \
	uv venv --seed -c && \
	source .venv/bin/activate && \
	pip install -U pip uv uvicorn && \
    uv pip install -r pyproject.toml && \
    uv pip install -e .

install: .venv

install-exe:
	install -m 0755 ./scripts/bin/rclipctl ~/bin/rclipctl
	install -m 0755 ./scripts/bin/rctrl-c ~/bin/rctrl-c
	install -m 0755 ./scripts/bin/rctrl-v ~/bin/rctrl-v
	install -m 0755 ./scripts/bin/rcliptunel ~/bin/rcliptunel

run: .venv
	RCLIPBOARD_PROXY=0 \
	RCLIPBOARD_LOG_LEVEL=$(RCLIPBOARD_LOG_LEVEL) \
	RCLIPBOARD_PY_LOG_LEVEL=$(RCLIPBOARD_PY_LOG_LEVEL) \
	RCLIPBOARD_UPSTREAM_ADDR=$(UPSTREAM_HOST) \
	RCLIPBOARD_UPSTREAM_PORT=$(UPSTREAM_PORT) \
	RCLIPBOARD_UDS="" \
	PYTHONPATH=src .venv/bin/python -m uvicorn rclipboard.main:app --host $(HOST) --port $(PORT) --log-level $(LOG_LEVEL)

run-dev: .venv
	RCLIPBOARD_PROXY=0 \
	RCLIPBOARD_XSEL=0 \
	RCLIPBOARD_LOG_LEVEL=$(RCLIPBOARD_LOG_LEVEL) \
	RCLIPBOARD_PY_LOG_LEVEL=$(RCLIPBOARD_PY_LOG_LEVEL) \
	PYTHONPATH=src .venv/bin/python -m uvicorn rclipboard.main:app --host $(HOST) --port $(PORT) --log-level $(LOG_LEVEL) --reload



run-test-dev: .venv
	RCLIPBOARD_PROXY=0 \
	RCLIPBOARD_LOG_LEVEL=$(RCLIPBOARD_LOG_LEVEL) \
	RCLIPBOARD_PY_LOG_LEVEL=$(RCLIPBOARD_PY_LOG_LEVEL) \
	RCLIPBOARD_BIND_ADDR=$(HOST) \
	RCLIPBOARD_BIND_PORT=$(TEST_PORT) \
	RCLIPBOARD_XSEL=0 \
	PYTHONPATH=src uvicorn rclipboard.main:app --host $(HOST) --port $(TEST_PORT) --log-level $(LOG_LEVEL) --reload

run-smoke-clip: .venv
	date | ./scripts/rclipctl clip -c --host $(HOST) --port $(TEST_PORT) --uds ""

run-smoke-get: .venv
	./scripts/rclipctl getclip -c --host $(HOST) --port $(TEST_PORT) --uds ""

run-smoke-test: .venv
	date | ./scripts/rclipctl clip -c --host $(HOST) --port $(TEST_PORT) --uds ""
	./scripts/rclipctl getclip -c --host $(HOST) --port $(TEST_PORT) --uds ""

run-uds: .venv
	RCLIPBOARD_PROXY=0 \
	RCLIPBOARD_LOG_LEVEL=$(RCLIPBOARD_LOG_LEVEL) \
	RCLIPBOARD_PY_LOG_LEVEL=$(RCLIPBOARD_PY_LOG_LEVEL) \
	RCLIPBOARD_UPSTREAM_ADDR=$(UPSTREAM_HOST) \
	RCLIPBOARD_UPSTREAM_PORT=$(UPSTREAM_PORT) \
	RCLIPBOARD_BIND_UDS=$(UDS) \
	PYTHONPATH=src .venv/bin/python -m uvicorn rclipboard.main:app --uds $(UDS) --log-level $(LOG_LEVEL)

run-uds-dev: .venv
	RCLIPBOARD_PROXY=0 \
	RCLIPBOARD_XSEL=0 \
	RCLIPBOARD_LOG_LEVEL=$(RCLIPBOARD_LOG_LEVEL) \
	RCLIPBOARD_PY_LOG_LEVEL=$(RCLIPBOARD_PY_LOG_LEVEL) \
	RCLIPBOARD_UPSTREAM_ADDR=$(UPSTREAM_HOST) \
	RCLIPBOARD_UPSTREAM_PORT=$(UPSTREAM_PORT) \
	RCLIPBOARD_BIND_UDS=$(UDS) \
	PYTHONPATH=src .venv/bin/python -m uvicorn rclipboard.main:app --uds $(UDS) --log-level $(LOG_LEVEL) --reload

run-https: $(KEY) $(CERT)
	RCLIPBOARD_PROXY=1 \
	RCLIPBOARD_LOG_LEVEL=$(RCLIPBOARD_LOG_LEVEL) \
	RCLIPBOARD_PY_LOG_LEVEL=$(RCLIPBOARD_PY_LOG_LEVEL) \
	RCLIPBOARD_UPSTREAM_ADDR=$(UPSTREAM_HOST) \
	RCLIPBOARD_UPSTREAM_PORT=$(UPSTREAM_PORT) \
	RCLIPBOARD_UPSTREAM_UDS=$(UPSTREAM_UDS) \
	PYTHONPATH=src .venv/bin/python -m uvicorn rclipboard.main:app --host $(HOST) --port $(PORT) \
		--ssl-keyfile $(KEY) --ssl-certfile $(CERT) --log-level $(LOG_LEVEL)

run-proxy: .venv
	RCLIPBOARD_PROXY=1 \
	RCLIPBOARD_LOG_LEVEL=$(RCLIPBOARD_LOG_LEVEL) \
	RCLIPBOARD_PY_LOG_LEVEL=$(RCLIPBOARD_PY_LOG_LEVEL) \
	RCLIPBOARD_UPSTREAM_ADDR=$(UPSTREAM_HOST) \
	RCLIPBOARD_UPSTREAM_PORT=$(UPSTREAM_PORT) \
	RCLIPBOARD_UPSTREAM_UDS=$(UPSTREAM_UDS) \
	PYTHONPATH=src .venv/bin/python -m uvicorn rclipboard.main:app --host $(HOST) --port $(PORT) --log-level $(LOG_LEVEL)

run-proxy-dev: .venv
	RCLIPBOARD_PROXY=1 \
	RCLIPBOARD_XSEL=0 \
	RCLIPBOARD_LOG_LEVEL=$(RCLIPBOARD_LOG_LEVEL) \
	RCLIPBOARD_PY_LOG_LEVEL=$(RCLIPBOARD_PY_LOG_LEVEL) \
	RCLIPBOARD_UPSTREAM_ADDR=$(UPSTREAM_HOST) \
	RCLIPBOARD_UPSTREAM_PORT=$(UPSTREAM_PORT) \
	RCLIPBOARD_UPSTREAM_UDS="" \
	PYTHONPATH=src .venv/bin/python -m uvicorn rclipboard.main:app --host $(HOST) --port 7878 --log-level $(LOG_LEVEL) --reload

cert:
	./scripts/gencert.sh --cn localhost --key $(KEY) --cert $(CERT)

cert-san:
	./scripts/gencert.sh --san --key $(KEY) --cert $(CERT)

status:
	./scripts/rclipctl status --host $(HOST) --port $(PORT)

topics:
	./scripts/rclipctl topics --host $(HOST) --port $(PORT)

health:
	./scripts/rclipctl health --host $(HOST) --port $(PORT)

docker-build:
	docker build -t $(IMAGE) .

docker-run:
	docker run --rm -it -p $(PORT):$(PORT) \
		-e RCLIPBOARD_BIND_ADDR=0.0.0.0 -e RCLIPBOARD_BIND_PORT=$(PORT) \
		$(IMAGE)

docker-run-proxy:
	docker run --rm -it -p $(PORT):$(PORT) \
		-e RCLIPBOARD_BIND_ADDR=0.0.0.0 -e RCLIPBOARD_BIND_PORT=$(PORT) \
		-e RCLIPBOARD_PROXY=1 \
		-e RCLIPBOARD_UPSTREAM_ADDR=$(UPSTREAM_HOST) \
		-e RCLIPBOARD_UPSTREAM_PORT=$(UPSTREAM_PORT) \
		$(IMAGE)

plugin-install:
	mkdir -p $$HOME/.tmux/plugins
	ln -snf "$(CURDIR)/tmux-rclipboard" $$HOME/.tmux/plugins/tmux-rclipboard
	@echo "Installed tmux plugin symlink at $$HOME/.tmux/plugins/tmux-rclipboard"

plugin-uninstall:
	rm -rf $$HOME/.tmux/plugins/tmux-rclipboard
	@echo "Removed tmux plugin at $$HOME/.tmux/plugins/tmux-rclipboard"

plugin-reload:
	tmux source-file $$HOME/.tmux.conf

plugin-demo:
	RCLIP_HOST=$(HOST) RCLIP_PORT=$(PORT) bash tmux-rclipboard/scripts/demo-session.sh

smoke:
	HOST=$(HOST) PORT=$(PORT) ./scripts/rclip-smoke.sh

proxy-smoke:
	PYTHONPATH=src .venv/bin/python -m tests.run_proxy_smoke

test: test-functional test-integration test-ssl

# Soak tests (excluded from default `make test` — run explicitly)
# Quick pass (30 s per class):  make test-soak
# Full nightly (5 min per class): make test-soak-full
SOAK_DURATION_S ?= 30
SOAK_WORKERS    ?= 8
SOAK_PAYLOAD_KB ?= 64

test-soak:
	SOAK_DURATION_S=$(SOAK_DURATION_S) SOAK_WORKERS=$(SOAK_WORKERS) \
	SOAK_PAYLOAD_KB=$(SOAK_PAYLOAD_KB) \
	PYTHONPATH=src .venv/bin/python -m unittest tests.test_soak -v

test-soak-full:
	SOAK_DURATION_S=300 SOAK_WORKERS=16 SOAK_PAYLOAD_KB=256 \
	PYTHONPATH=src .venv/bin/python -m unittest tests.test_soak -v

test-functional: test-http test-ws

test-integration: test-proxy-integration test-tunel

test-ssl: test-https test-wss test-ssl-proxy-integration

test-http:
	PYTHONPATH=src .venv/bin/python -m unittest tests.test_functional_http -v

test-ws:
	PYTHONPATH=src .venv/bin/python -m unittest tests.test_functional_ws -v

test-proxy-integration:
	PYTHONPATH=src .venv/bin/python -m unittest tests.test_integration_proxy -v

test-https:
	PYTHONPATH=src .venv/bin/python -m unittest tests.test_functional_https -v

test-wss:
	PYTHONPATH=src .venv/bin/python -m unittest tests.test_functional_wss -v

test-ssl-proxy-integration:
	PYTHONPATH=src .venv/bin/python -m unittest tests.test_integration_proxy_ssl -v

# Installer tests in Docker: clean env without systemd (fake systemctl stub
# from the test suite) and with a real booted systemd (systemctl --user).
test-install-docker:
	docker build -t rclipboard-test-install -f docker/test-install/Dockerfile .
	docker run --rm rclipboard-test-install

test-install-docker-systemd:
	docker build -t rclipboard-test-install-systemd \
		-f docker/test-install/Dockerfile.systemd .
	docker rm -f rclipboard-test-install-systemd 2>/dev/null || true
	docker run -d --name rclipboard-test-install-systemd --privileged \
		--cgroupns=host -v /sys/fs/cgroup:/sys/fs/cgroup:rw \
		rclipboard-test-install-systemd
	sleep 3
	docker exec rclipboard-test-install-systemd /usr/local/bin/systemd-test.sh; \
	rc=$$?; docker rm -f rclipboard-test-install-systemd >/dev/null; exit $$rc

docker-build-tunel-test:
	docker build -t $(TUNEL_TEST_IMAGE) tests/docker/tunel/

test-tunel:
	PYTHONPATH=src RCLIPBOARD_TUNEL_TEST_IMAGE=$(TUNEL_TEST_IMAGE) \
	.venv/bin/python -m unittest tests.test_integration_tunel -v

docker-build-deploy-test:
	docker build -t $(DEPLOY_TEST_IMAGE) tests/docker/deploy/

test-deploy:
	PYTHONPATH=src RCLIPBOARD_DEPLOY_TEST_IMAGE=$(DEPLOY_TEST_IMAGE) \
	.venv/bin/python -m unittest tests.test_integration_deploy -v

service-install:
	./scripts/install.sh

service-update:
	$(USER_CONFIG_HOME)/rclipboard/bin/rclipboard-update

service-reset:
	./scripts/install.sh --reset

service-uninstall:
	$(USER_CONFIG_HOME)/rclipboard/bin/rclipboard-uninstall

systemd-user-install: service-install
	@echo "systemd-user-install is a compatibility alias for service-install"

systemd-user-enable:
	systemctl --user enable --now rclipboard.service

systemd-user-disable:
	systemctl --user disable --now rclipboard.service || true

nvim-plugin-install:
	cd nvim-rclipboard && luarocks make --local nvim-rclipboard-0.1.0-1.rockspec

nvim-plugin-pack:
	cd nvim-rclipboard && luarocks pack nvim-rclipboard-0.1.0-1.rockspec

install-no-systemd:
	./scripts/install.sh --no-start

setup-wizard:
	~/.config/rclipboard/bin/rclipboard-setup

deploy:
	./scripts/deploy.sh

deploy-dry:
	./scripts/deploy.sh --dry-run
