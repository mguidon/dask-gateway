#
#
#

makefile_path := $(abspath $(lastword $(MAKEFILE_LIST)))
makefile_dir := $(patsubst %/,%,$(dir $(makefile_path)))

.DEFAULT_GOAL := help

SHELL := /bin/bash

VENV_DIR = $(makefile_dir)/.venv
VENV_PYTHON = $(VENV_DIR)/bin/python


.PHONY: devenv .check-venv-active

.check-venv-active: ## check that the (correct) venv is activated
	# checking that the virtual environment (${1}) was activated
	@python3 -c "import sys, pathlib; assert pathlib.Path('${1}').resolve()==pathlib.Path(sys.prefix).resolve()" || (echo "--> To activate venv: source ${1}/bin/activate" && exit 1)

devenv: $(VENV_DIR) ## builds development environment
	# Installing python tools in $<
	@$</bin/pip --no-cache install \
		bump2version \
		pip-tools
	# Installing pre-commit hooks in current .git repo
	@$</bin/pip install pre-commit
	@$</bin/pre-commit install
	# Installing repo packages
	@$</bin/pip install -r $(makefile_dir)/requirements/dev.txt
	# Installed packages in $<
	@$</bin/pip list

$(VENV_DIR):
	# creating virtual environment
	@python3 -m venv $@
	# updating package managers
	@$@/bin/pip --no-cache install --upgrade \
		pip \
		setuptools \
		wheel

# .PHONY: up
run: ## start gateway-server
	dask-gateway-server -f config.py

up-worker:
	docker run -it -v /tmp/dask/input:/input -v /tmp/dask/output:/output -v /tmp/dask/log:/log -v /var/run/docker.sock:/var/run/docker.sock \
	-e DASK_SCHEDULER_ADDRESS=172.16.8.64:807 -e DASK_DASHBOARD_ADDRESS=172.17.0.1 -e DASK_WORKER_NAME=asdf \
	local/dask-sidecar:production /bin/bash

.PHONY: build build-nc rebuild build-devel build-devel-nc

define _docker_compose_build
export BUILD_TARGET=$(if $(findstring -devel,$@),development,production);\
docker buildx bake --file docker-compose-build.yml $(if $(target),$(target),);
endef

rebuild: build-nc # alias
build build-nc: $(VENV_DIR) ## Builds production images and tags them as 'local/{service-name}:production'. For single target e.g. 'make target=webserver build'
ifeq ($(target),)
	# Building services
	$(_docker_compose_build)
else
	# Building service $(target)
	$(_docker_compose_build)
endif


build-devel build-devel-nc: $(VENV_DIR) ## Builds development images and tags them as 'local/{service-name}:development'. For single target e.g. 'make target=webserver build-devel'
ifeq ($(target),)
	# Building services
	$(_docker_compose_build)
else
	# Building service $(target)
	@$(_docker_compose_build)
endif


up up-devel: $(VENV_DIR) ## Builds production images and tags them as 'local/{service-name}:production'. For single target e.g. 'make target=webserver build'
	$(eval COMPOSE_FILE := docker-compose$(if $(findstring -devel,$@),-devel.yml,.yml))
	export BUILD_TARGET=$(if $(findstring -devel,$@),development,production) && docker-compose -f ${COMPOSE_FILE} up

	
.PHONY: help
help: ## help on rule's targets
	@awk --posix 'BEGIN {FS = ":.*?## "} /^[[:alpha:][:space:]_-]+:.*?## / {printf "\033[36m%-25s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)
