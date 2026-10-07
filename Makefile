COMPOSE := docker compose -f deploy/compose/dev.yml

.PHONY: help install up down reset logs ps psql web lint format typecheck test check

help:
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F ':.*## ' '{printf "  %-10s %s\n", $$1, $$2}'

install: ## Install Python and web dependencies
	uv sync
	cd apps/web && pnpm install

up: ## Start PostgreSQL, Temporal and object storage
	$(COMPOSE) up -d

down: ## Stop the dev stack (data is kept)
	$(COMPOSE) down

reset: ## Stop the dev stack and delete its data
	$(COMPOSE) down -v

logs: ## Follow dev stack logs
	$(COMPOSE) logs -f

ps: ## Show dev stack status
	$(COMPOSE) ps -a

psql: ## Open a psql shell in the dev database
	$(COMPOSE) exec postgres psql -U openmarketer -d openmarketer

web: ## Run the dashboard on http://localhost:3000
	cd apps/web && pnpm dev

lint: ## Lint Python and web
	uv run ruff check .
	uv run ruff format --check .
	cd apps/web && pnpm lint

format: ## Format and auto-fix Python
	uv run ruff format .
	uv run ruff check --fix .

typecheck: ## Type-check Python and web
	uv run pyright
	cd apps/web && pnpm typecheck

test: ## Run Python tests
	uv run pytest

check: lint typecheck test ## Everything CI runs
