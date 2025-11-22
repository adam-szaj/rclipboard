SHELL := /bin/bash

# Configuration
HOST ?= 127.0.0.1
PORT ?= 8989
UDS  ?= /tmp/rclip.sock
KEY  ?= key.pem
CERT ?= cert.pem
LOG_LEVEL ?= info

# Proxy upstream config
UPSTREAM_HOST ?= 127.0.0.1
UPSTREAM_PORT ?= 8989
UPSTREAM_UDS  ?=

# Docker
IMAGE ?= rclipboard:latest

.PHONY: help install run run-dev run-uds run-https run-proxy run-dev-proxy cert cert-san health status topics docker-build docker-run docker-run-proxy plugin-install plugin-uninstall plugin-reload plugin-demo smoke systemd-user-install systemd-user-enable systemd-user-enable-socket systemd-user-disable nvim-plugin-install nvim-plugin-pack

help:
	@echo "Targets:"
	@echo "  install     - install Python deps"
	@echo "  run         - run server (TCP)"
	@echo "  run-dev     - run server with reload (TCP)"
	@echo "  run-uds     - run server on Unix Domain Socket"
	@echo "  run-https   - run server with HTTPS (uses $(KEY)/$(CERT))"
	@echo "  run-proxy   - run server in proxy mode (env-driven upstream)"
	@echo "  run-dev-proxy - run server with reload + proxy mode"
	@echo "  cert        - generate self-signed cert (CN=localhost)"
	@echo "  cert-san    - generate self-signed cert with SANs"
	@echo "  status      - GET /status"
	@echo "  topics      - GET /topics"
	@echo "  health      - GET /health"
	@echo "  docker-build - build Docker image ($(IMAGE))"
	@echo "  docker-run   - run Docker image mapping port $(PORT)"
	@echo "  docker-run-proxy - run Docker image with proxy env"
	@echo "  plugin-install - install tmux plugin (symlink to ~/.tmux/plugins/tmux-rclipboard)"
	@echo "  plugin-uninstall - remove installed tmux plugin"
	@echo "  plugin-reload - reload tmux config to pick up plugin"
	@echo "  plugin-demo   - launch a temporary tmux session to test plugin"
	@echo "  smoke         - quick HTTP smoke test (health/publish/fetch)"
	@echo "  systemd-user-install - install user units + env (override WorkingDirectory)"
	@echo "  systemd-user-enable  - enable & start rclipboard.service"
	@echo "  systemd-user-enable-socket - enable & start rclipboard.socket"
	@echo "  systemd-user-disable  - disable all rclipboard user units"
	@echo "  nvim-plugin-install  - luarocks make (local) nvim-rclipboard"
	@echo "  nvim-plugin-pack     - luarocks pack rock for nvim-rclipboard"

install:
	python -m pip install -U pip fastapi uvicorn websockets

run:
	uvicorn app.main:app --host $(HOST) --port $(PORT) --log-level $(LOG_LEVEL)

run-dev:
	uvicorn app.main:app --host $(HOST) --port $(PORT) --log-level $(LOG_LEVEL) --reload

run-uds:
	uvicorn app.main:app --uds $(UDS) --log-level $(LOG_LEVEL)

run-https: $(KEY) $(CERT)
	uvicorn app.main:app --host $(HOST) --port $(PORT) \
		--ssl-keyfile $(KEY) --ssl-certfile $(CERT) --log-level $(LOG_LEVEL)

run-proxy:
	RCLIPBOARD_PROXY=1 \
	RCLIPBOARD_PROXY_ADDR=$(UPSTREAM_HOST) \
	RCLIPBOARD_PROXY_PORT=$(UPSTREAM_PORT) \
	RCLIPBOARD_PROXY_UDS=$(UPSTREAM_UDS) \
	uvicorn app.main:app --host $(HOST) --port $(PORT) --log-level $(LOG_LEVEL)

run-dev-proxy:
	RCLIPBOARD_PROXY=1 \
	RCLIPBOARD_PROXY_ADDR=$(UPSTREAM_HOST) \
	RCLIPBOARD_PROXY_PORT=$(UPSTREAM_PORT) \
	RCLIPBOARD_PROXY_UDS=$(UPSTREAM_UDS) \
	uvicorn app.main:app --host $(HOST) --port $(PORT) --log-level $(LOG_LEVEL) --reload

cert:
	./scripts/gencert.sh --cn localhost --key $(KEY) --cert $(CERT)

cert-san:
	./scripts/gencert.sh --san --key $(KEY) --cert $(CERT)

status:
	./rclipctl status --host $(HOST) --port $(PORT)

topics:
	./rclipctl topics --host $(HOST) --port $(PORT)

health:
	./rclipctl health --host $(HOST) --port $(PORT)

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
		-e RCLIPBOARD_PROXY_ADDR=$(UPSTREAM_HOST) \
		-e RCLIPBOARD_PROXY_PORT=$(UPSTREAM_PORT) \
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

systemd-user-install:
	REPO_DIR="$(CURDIR)" ./scripts/install-systemd-user.sh

systemd-user-enable:
	systemctl --user enable --now rclipboard.service

systemd-user-enable-socket:
	systemctl --user enable --now rclipboard.socket

systemd-user-disable:
	-systemctl --user disable --now rclipboard.service rclipboard-proxy.service rclipboard.socket || true

nvim-plugin-install:
	cd nvim-rclipboard && luarocks make --local nvim-rclipboard-0.1.0-1.rockspec

nvim-plugin-pack:
	cd nvim-rclipboard && luarocks pack nvim-rclipboard-0.1.0-1.rockspec
