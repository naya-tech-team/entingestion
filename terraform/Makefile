# Deploy a stack to an environment. The code lives once in stacks/<STACK>; each environment
# supplies only envs/<ENV>/common.tfvars (region, mandatory tags: every stack), then
# envs/<ENV>/<STACK>.tfvars (which may override them) and envs/<ENV>/<STACK>.s3.tfbackend.
# Nothing account- or region-specific is tracked: it comes from the environment.
#   AWS_ACCOUNT_ID   the account to deploy to (required)
#   STATE_REGION     where terraform/bootstrap put the state bucket (default: AWS_REGION; required)
#   AWS_PROFILE      default entingestion-<ENV> (the deploy role); ALLOW_AMBIENT=1 skips it
#
#   make build                             # build the Lambda zips the stacks deploy (no AWS)
#   make plan  ENV=dev STACK=producer      # init + plan, saves stacks/<STACK>/<ENV>.tfplan
#   make apply ENV=dev STACK=producer      # applies exactly that saved plan
#   make deploy ENV=dev STACK=ingestion    # plan + confirm + apply (+ migrate for ingestion)
#   make rollback ENV=dev FN=<fn> VERSION=<n>   # point a function's live alias back
#   make destroy ENV=dev STACK=producer    # disposable envs only (DRY_RUN=1 lists, changes nothing)
#   make destroy-all ENV=dev               # every stack, dependents first
#   make check                             # fmt, validate, tests, tflint, checkov (no AWS access)
#
# Upload UI (docs/UI.md):
#   make ui-password ENV=dev               # set the login (prompts; stores only a hash)
#   make ui-deploy   ENV=dev               # build the web app, upload it, invalidate the cache
#   make ui-url      ENV=dev               # print the URL
#   make ui-dev      ENV=dev               # local Vite server proxying /api to that environment

# One shared provider cache: each .terraform dir symlinks the AWS provider (~700 MB) instead of
# copying it. Module and example dirs have no lock file, so allow the cache for them too.
export TF_PLUGIN_CACHE_DIR ?= $(HOME)/.terraform.d/plugin-cache
export TF_PLUGIN_CACHE_MAY_BREAK_DEPENDENCY_LOCK_FILE ?= true
$(shell mkdir -p $(TF_PLUGIN_CACHE_DIR))

STACK ?= producer
ENV   ?=

STACK_DIR   := stacks/$(STACK)
COMMON_VARS := envs/$(ENV)/common.tfvars
VARS        := envs/$(ENV)/$(STACK).tfvars
BACKEND     := envs/$(ENV)/$(STACK).s3.tfbackend
PLAN        := $(ENV).tfplan
# Relative to the stack directory; common first, so the stack's own tfvars win.
VAR_FILES   := -var-file=../../$(COMMON_VARS) -var-file=../../$(VARS)

# Fails the recipe unless the make variable is set: $(call need,ENV,make plan ENV=dev).
# (The example must not contain a comma.)
need = @test -n "$($(1))" || { echo "$(1) is required, e.g. $(2)"; exit 1; }

# Credentials (docs/AWS_ACCESS.md): Terraform runs as the entingestion-<ENV>-deploy role through
# the AWS profile of that name, which assumes it from your `aws login` session. No access keys.
#   AWS_EXPORT_CREDS=1  the AWS CLI resolves the profile and hands Terraform plain environment
#                       credentials (for tools whose SDK can't read an `aws login` session)
#   ALLOW_AMBIENT=1     use whatever credentials are active, skipping the role check (break-glass)
AWS_PROFILE ?= entingestion-$(ENV)
ifndef ALLOW_AMBIENT
export AWS_PROFILE
endif

# The AWS account comes from the environment, never from a tracked file:
#   export AWS_ACCOUNT_ID=123456789012
# Terraform gets it as var.aws_account_id (the providers refuse any other account) and derives
# every ARN from it; the state bucket and its KMS key are named after it (bootstrap).
AWS_ACCOUNT_ID ?=
ifneq ($(AWS_ACCOUNT_ID),)
export TF_VAR_aws_account_id := $(AWS_ACCOUNT_ID)
endif
# People who write every producer source (Bruno, the CLI), also from the environment:
#   export OPERATOR_PRINCIPALS=user/alice,role/ops-*      # -> var.operator_principals
comma := ,
empty :=
space := $(empty) $(empty)
ifneq ($(OPERATOR_PRINCIPALS),)
export TF_VAR_operator_principals := [$(subst $(space),$(comma),$(patsubst %,"%",$(subst $(comma),$(space),$(OPERATOR_PRINCIPALS))))]
endif

# The state bucket and its KMS key (terraform/bootstrap) are named after the account and the
# region the bootstrap was applied in, which can differ from the stacks' region:
#   export STATE_REGION=<bootstrap region>    # default: AWS_REGION
AWS_REGION     ?=
STATE_REGION   ?= $(AWS_REGION)
AWS_PARTITION  ?= aws
STATE_BUCKET    = entingestion-tfstate-$(AWS_ACCOUNT_ID)-$(STATE_REGION)
STATE_KEY_ARN   = arn:$(AWS_PARTITION):kms:$(STATE_REGION):$(AWS_ACCOUNT_ID):alias/entingestion-tfstate
BACKEND_CONFIG  = -backend-config=../../$(BACKEND) -backend-config=region=$(STATE_REGION) \
                  -backend-config=bucket=$(STATE_BUCKET) -backend-config=kms_key_id=$(STATE_KEY_ARN)

# The stacks' region (envs/<ENV>/common.tfvars). AWS CLI calls on their resources use it, so an
# AWS_REGION set for the state bucket can't send them elsewhere.
STACK_REGION = $(shell awk -F'"' '/^region[[:space:]]*=/{print $$2}' $(COMMON_VARS) 2>/dev/null)

CREDS := $(if $(AWS_EXPORT_CREDS),eval "$$(aws configure export-credentials --format env)" &&,)
# The AWS CLI, with credentials (CREDS) and in the stacks' region.
AWS    = $(CREDS) AWS_REGION=$(STACK_REGION) AWS_DEFAULT_REGION=$(STACK_REGION) aws

# One .terraform directory per environment, so backend settings can never leak between envs.
TF := $(CREDS) cd $(STACK_DIR) && TF_DATA_DIR=.terraform/$(ENV) terraform

# Tool versions, pinned so a lint run can't change under us (Renovate proposes upgrades).
CHECKOV_VERSION ?= 3.3.27
RUFF_VERSION    ?= 0.17.0
TFLINT_VERSION  ?= v0.64.0

MODULE_DIRS  := $(wildcard modules/*)
STACK_DIRS   := $(wildcard stacks/*)
EXAMPLE_DIRS := $(wildcard modules/*/examples/*)

.PHONY: build whoami init quiet-init plan apply output deploy migrate rollback versions destroy destroy-all \
        check fmt validate test test-scripts lint clean require-env \
        ui-build ui-deploy ui-password ui-url ui-dev ui-init

# The stack a function belongs to, from its name: <stack>-<env>-<name>.
FN_STACK = $(firstword $(subst -, ,$(FN)))

# -- Build ------------------------------------------------------------------------------
#
# The Lambda zips the stacks deploy (each tfvars points at its dist/ zip). Deterministic: an
# unchanged tree gives an unchanged zip, so a plan only updates functions whose code changed.
build:
	../services/producer/scripts/build_lambda.sh
	../services/ingestion/scripts/build_lambda.sh all
	../services/ui/bff/scripts/build_lambda.sh

require-env:
	$(call need,ENV,make plan ENV=dev STACK=$(STACK))
	@test -d "$(STACK_DIR)" || { echo "no stack $(STACK_DIR)"; exit 1; }
	@test -f "$(COMMON_VARS)" || { echo "missing $(COMMON_VARS)"; exit 1; }
	@test -f "$(VARS)" || { echo "missing $(VARS)"; exit 1; }
	@test -f "$(BACKEND)" || { echo "missing $(BACKEND)"; exit 1; }
	@test -n "$(STACK_REGION)" || { echo "$(COMMON_VARS) must set region"; exit 1; }
	@echo "$(AWS_ACCOUNT_ID)" | grep -Eq '^[0-9]{12}$$' || { \
	  echo "AWS_ACCOUNT_ID must be the 12-digit account to deploy to, e.g. export AWS_ACCOUNT_ID=123456789012"; exit 1; }
	@test -n "$(STATE_REGION)" || { \
	  echo "STATE_REGION is required: the region of the state bucket (terraform/bootstrap), e.g. export STATE_REGION=<region>, or set AWS_REGION"; exit 1; }

# Refuse to run Terraform in any account but AWS_ACCOUNT_ID, or as anything but this
# environment's deploy role (unless ALLOW_AMBIENT).
whoami: require-env
	@arn=$$($(AWS) sts get-caller-identity --query Arn --output text 2>/dev/null) || { \
	  echo "No AWS session for profile $(AWS_PROFILE). Sign in: aws login --profile entingestion (docs/AWS_ACCESS.md)"; exit 1; }; \
	case "$$arn" in \
	  arn:*:*:*:$(AWS_ACCOUNT_ID):*) ;; \
	  *) echo "AWS_ACCOUNT_ID is $(AWS_ACCOUNT_ID) but the credentials are $$arn. Fix one of them."; exit 1 ;; \
	esac; \
	if [ -n "$(ALLOW_AMBIENT)" ]; then echo "ALLOW_AMBIENT: running as $$arn"; exit 0; fi; \
	case "$$arn" in \
	  *:assumed-role/entingestion-$(ENV)-deploy/*) echo "as $$arn" ;; \
	  *) echo "Expected role entingestion-$(ENV)-deploy, got $$arn. Check profile $(AWS_PROFILE), or ALLOW_AMBIENT=1 (break-glass)."; exit 1 ;; \
	esac

init: require-env whoami
	$(TF) init -input=false -reconfigure $(BACKEND_CONFIG)

# The same, quietly: for targets that only read a stack's outputs.
quiet-init: require-env whoami
	@$(TF) init -input=false -reconfigure $(BACKEND_CONFIG) >/dev/null

plan: init
	$(TF) plan -input=false $(VAR_FILES) -out=$(PLAN)

apply: require-env whoami
	@test -f "$(STACK_DIR)/$(PLAN)" || { echo "run make plan ENV=$(ENV) STACK=$(STACK) first"; exit 1; }
	$(TF) apply -input=false $(PLAN)
	rm -f $(STACK_DIR)/$(PLAN)

output: require-env whoami
	$(TF) output

# -- Deploy, migrate, roll back ----------------------------------------------------------
#
#   make deploy   ENV=prod STACK=ingestion   # plan, confirm, apply that plan, then (ingestion)
#                                            # migrate; AUTO_APPROVE=1 skips the confirmation
#   make migrate  ENV=prod                   # run the ingestion migrations (idempotent; the
#                                            # function is the ingestion output migrate_function_name)
#   make rollback ENV=prod FN=ingestion-prod-embed VERSION=12   # point the live alias back
#   make versions ENV=prod FN=ingestion-prod-embed              # what rollback can go back to
#
# Migrations are expand-only (add tables, columns, indexes; never drop or rename what running
# code reads), so code and schema may run in either order for a while. deploy applies first and
# migrates right after; code that reaches a table or column before its migration fails
# E_DB_SCHEMA, which is retried, so those runs recover. Remove things in a later release, once
# no deployed version reads them (expand, then contract). docs/RUNBOOK.md "Deploy".

MIGRATE_FN      ?=
MIGRATE_PAYLOAD ?= {"action": "migrate"}
ALIAS           ?= live

# A stack's output, read from ENV's state (after quiet-init): $(call TF_OUT,ingestion) <name>
TF_OUT = $(CREDS) cd stacks/$(1) && TF_DATA_DIR=.terraform/$(ENV) terraform output -raw

deploy:
	$(call need,ENV,make deploy ENV=dev STACK=$(STACK))
	$(MAKE) --no-print-directory plan ENV=$(ENV) STACK=$(STACK)
	@[ -n "$(AUTO_APPROVE)" ] || { printf "Apply this plan to $(ENV)? Type the env name: "; read answer; [ "$$answer" = "$(ENV)" ]; } || { echo "not applied"; exit 1; }
	$(MAKE) --no-print-directory apply ENV=$(ENV) STACK=$(STACK)
	@if [ "$(STACK)" = "ingestion" ]; then $(MAKE) --no-print-directory migrate ENV=$(ENV); fi

# Invokes the migrate function's live alias (the code this deploy published) and fails if the
# function did. Safe to repeat: applied migrations are skipped. The function comes from the
# ingestion stack's outputs (MIGRATE_FN=<name>:<alias> overrides it).
migrate:
	$(call need,ENV,make migrate ENV=dev)
	@$(MAKE) --no-print-directory quiet-init ENV=$(ENV) STACK=ingestion
	@fn="$(MIGRATE_FN)"; \
	  if [ -z "$$fn" ]; then fn=$$($(call TF_OUT,ingestion) migrate_function_name) && fn="$$fn:$(ALIAS)" || exit 1; fi; \
	  echo "invoking $$fn"; \
	  out=$$(mktemp) && \
	  status=$$($(AWS) lambda invoke --function-name "$$fn" \
	    --cli-binary-format raw-in-base64-out --payload '$(MIGRATE_PAYLOAD)' \
	    --cli-read-timeout 330 --query FunctionError --output text "$$out") ; rc=$$? ; \
	  cat "$$out"; echo; rm -f "$$out"; \
	  if [ $$rc -ne 0 ] || [ "$$status" != "None" ]; then echo "migrate FAILED ($$status)"; exit 1; fi; \
	  echo "migrate OK"

# Moves FN's live alias to VERSION (one of `make versions`). Holds until the next apply, which
# moves the alias to that apply's version: revert the code before deploying again.
rollback:
	$(call need,ENV,make rollback ENV=<env> FN=<function name> VERSION=<n>)
	$(call need,FN,make rollback ENV=<env> FN=<function name> VERSION=<n>)
	$(call need,VERSION,make rollback ENV=<env> FN=<function name> VERSION=<n>)
	@case "$(FN)" in *-$(ENV)-*) ;; *) echo "$(FN) isn't a $(ENV) function"; exit 1 ;; esac
	@$(MAKE) --no-print-directory whoami ENV=$(ENV) STACK=$(FN_STACK)
	$(AWS) lambda update-alias --function-name "$(FN)" --name $(ALIAS) --function-version "$(VERSION)" \
	  --query '{alias: Name, version: FunctionVersion}' --output text

versions:
	$(call need,ENV,make versions ENV=<env> FN=<function name>)
	$(call need,FN,make versions ENV=<env> FN=<function name>)
	@$(MAKE) --no-print-directory whoami ENV=$(ENV) STACK=$(FN_STACK)
	@$(AWS) lambda get-alias --function-name "$(FN)" --name $(ALIAS) --query '{live: FunctionVersion}' --output text
	@$(AWS) lambda list-versions-by-function --function-name "$(FN)" \
	  --query 'Versions[?Version!=`$$LATEST`].[Version, LastModified, CodeSha256]' --output text | tail -10

# Data stores carry prevent_destroy and Aurora must go before its network resources, so
# scripts/teardown.py runs the destroy: preflight, confirm, Aurora, terraform destroy, tables.
# A failed run resumes when repeated. See docs/RUNBOOK.md "Tear down a disposable environment".
DESTROY_FLAGS := $(if $(DRY_RUN),--dry-run) $(if $(FINAL_SNAPSHOT),--final-snapshot)

destroy: require-env init
	$(CREDS) AWS_REGION=$(STACK_REGION) AWS_DEFAULT_REGION=$(STACK_REGION) python3 scripts/teardown.py \
	  --env $(ENV) --stack $(STACK) --var-file ../../$(COMMON_VARS) --var-file ../../$(VARS) $(DESTROY_FLAGS)

# Reverse dependency order: ui reads both APIs and the producer key; ingestion reads the
# producer's stream, bucket and key.
DESTROY_ORDER := ui ingestion producer

destroy-all:
	$(call need,ENV,make destroy-all ENV=dev)
	@test -z "$(filter-out $(DESTROY_ORDER),$(notdir $(STACK_DIRS)))" || \
	  { echo "add $(filter-out $(DESTROY_ORDER),$(notdir $(STACK_DIRS))) to DESTROY_ORDER"; exit 1; }
	@for s in $(DESTROY_ORDER); do \
	  [ -f envs/$(ENV)/$$s.tfvars ] || continue; \
	  echo "== $$s"; \
	  $(MAKE) --no-print-directory destroy ENV=$(ENV) STACK=$$s DRY_RUN=$(DRY_RUN) FINAL_SNAPSHOT=$(FINAL_SNAPSHOT) || exit 1; \
	done

# -- Upload UI (docs/UI.md) -----------------------------------------------------------------
#
#   make ui-build                 # npm ci + vite build -> services/ui/web/dist (no AWS)
#   make ui-deploy   ENV=dev      # ui-build, upload to the site bucket, invalidate index.html
#   make ui-password ENV=dev      # prompt for a username and password; store only the hash
#   make ui-url      ENV=dev      # the CloudFront URL
#   make ui-dev      ENV=dev      # Vite on localhost:$UI_DEV_PORT (5173), /api proxied to ENV's CloudFront
#                                 # (UI_USER / UI_PASSWORD from your environment)
#
# They read the ui stack's outputs, so `make deploy ENV=<env> STACK=ui` comes first.

UI_WEB  := $(CURDIR)/../services/ui/web
UI_DIST := $(UI_WEB)/dist
UI_OUT   = $(call TF_OUT,ui)

ui-build:
	cd $(UI_WEB) && npm ci && npm run build

# Signs in as ENV's deploy role and points stacks/ui at ENV's state, quietly.
ui-init:
	$(call need,ENV,make ui-url ENV=dev)
	@$(MAKE) --no-print-directory quiet-init ENV=$(ENV) STACK=ui

# Hashed assets are immutable and kept (a page still open may load them); everything else,
# index.html first of all, is revalidated on every load.
ui-deploy: ui-init ui-build
	@test -f "$(UI_DIST)/index.html" || { echo "no $(UI_DIST)/index.html; did the build run?"; exit 1; }
	@bucket=$$($(UI_OUT) site_bucket) && dist=$$($(UI_OUT) distribution_id) && \
	  echo "== upload to s3://$$bucket" && \
	  if [ -d "$(UI_DIST)/assets" ]; then \
	    $(AWS) s3 sync "$(UI_DIST)/assets" "s3://$$bucket/assets" --only-show-errors \
	      --cache-control "public,max-age=31536000,immutable" || exit 1; \
	  fi && \
	  $(AWS) s3 sync "$(UI_DIST)" "s3://$$bucket" --only-show-errors --delete \
	    --exclude "assets/*" --cache-control "no-cache" && \
	  echo "== invalidate $$dist" && \
	  $(AWS) cloudfront create-invalidation --distribution-id "$$dist" --paths "/index.html" "/" \
	    --query Invalidation.Id --output text && \
	  echo "deployed: $$($(UI_OUT) url)"

# The password never reaches git, Terraform or the shell history: scripts/ui_password.sh reads
# it without echo and stores sha256("Basic " + base64(user:password)) in the KeyValueStore.
ui-password: ui-init
	@kvs=$$($(UI_OUT) kvs_arn) && $(CREDS) AWS_REGION=$(STACK_REGION) AWS_DEFAULT_REGION=$(STACK_REGION) scripts/ui_password.sh "$$kvs"

ui-url: ui-init
	@$(UI_OUT) url; echo

ui-dev: ui-init
	@url=$$($(UI_OUT) url) && echo "proxying /api to $$url" && \
	  cd $(UI_WEB) && UI_API_TARGET="$$url" npm run dev

# -- Static checks (no AWS access; run locally, CI is off) --------------------------------

check: fmt validate test test-scripts lint

fmt:
	terraform fmt -check -recursive

validate:
	@for d in bootstrap $(STACK_DIRS) $(EXAMPLE_DIRS); do \
	  echo "== validate $$d"; \
	  (cd $$d && TF_DATA_DIR=.terraform/validate terraform init -backend=false -input=false >/dev/null && \
	   TF_DATA_DIR=.terraform/validate terraform validate) || exit 1; \
	done

# Tests never call AWS. The bootstrap size check uses a real provider with dummy keys, which
# would otherwise try to load the exported AWS_PROFILE.
test:
	@for d in bootstrap $(MODULE_DIRS) $(STACK_DIRS); do \
	  [ -d $$d/tests ] || continue; \
	  echo "== test $$d"; \
	  (cd $$d && TF_DATA_DIR=.terraform/test terraform init -backend=false -input=false >/dev/null && \
	   env -u AWS_PROFILE TF_DATA_DIR=.terraform/test terraform test) || exit 1; \
	done

test-scripts:
	uvx ruff@$(RUFF_VERSION) check scripts && uvx ruff@$(RUFF_VERSION) format --check scripts
	python3 -m unittest discover -s scripts/tests -t .
	bash -n scripts/ui_password.sh
	# CloudFront Functions (stacks/*/functions), with a stand-in for the cloudfront module.
	node --test 'stacks/*/functions/*.test.mjs'

# Remove all .terraform working dirs (providers, modules, backend config); `init` recreates them.
clean:
	find . -name .terraform -type d -prune -exec rm -rf {} +

lint:
	@tflint --version | grep -q "$(TFLINT_VERSION:v%=%)" || echo "warning: tflint isn't $(TFLINT_VERSION) (CI uses that one)"
	tflint --init
	tflint --recursive --config "$(CURDIR)/.tflint.hcl"
	# Test files declare no infrastructure (and checkov's parser can't read some HCL test syntax).
	uvx checkov==$(CHECKOV_VERSION) --directory . --framework terraform --quiet --compact --skip-path tests
