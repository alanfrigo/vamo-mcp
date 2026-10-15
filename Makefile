.PHONY: build test run
build:
	docker build -t ephemeral-desktop-sandbox:latest sandbox_image
test:
	uv run --extra test python tests/test_e2e.py
run:
	uv run vamo-mcp
