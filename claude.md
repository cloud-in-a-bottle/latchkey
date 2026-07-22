- read README.md and style_guide.md at the beginning of every session.
- on first init, run `just setup` — this installs dependencies, the pre-commit hooks, and the playwright chromium browser. pre-commit runs ruff and mypy on commit.
- use uv for all python work (`uv run ...`, `uv add ...`, `uv sync`).
- this is an OpenHost app. `openhost.toml` is the app manifest.
- the app is a litestar/hypercorn backend that serves on port 8080 and exposes a `/health` endpoint. see "deploying & debugging on openhost" below.
- tests use the OpenHost test harness (the `openhost[test-harness]` package, imported as `openhost_test_harness`): each run builds the Dockerfile, runs the app under podman per `openhost.toml`, and fronts it with the real OpenHost router. so `just test` requires podman running on the host. `stack.url` goes through the router and requires owner auth (use `stack.owner_session` for requests, or `stack.playwright_login(page)` for browser tests); `stack.app_url` hits the container directly.

## deploying & debugging on openhost

- openhost is a cloud platform for self-hosting apps. there's context on openhost at `~/work/openhost`; read `docs/src/creating_an_app.md` there for how apps are built and run.
- instances are managed via the `oh` cli. `oh instance list` shows the configured instances and the URL each is available at. the user will tell you which instance to use; do not touch the others. most commands take `--instance <name>`.
- these instances have web servers facing the public internet. be careful with anything that could open unsecured public access — eg adding `public_paths` in `openhost.toml`.
- prefer `oh` commands for debugging since they handle auth: `oh instance ssh` and `oh curl`. `oh instance token --instance <name>` gives a raw API token (Bearer auth) only if absolutely necessary — better not to see it, and never put it anywhere that might get committed.
- typical deploy loop: commit + push, then `oh app reload <app> --update --wait --instance <name>` to pull the changes and reload, then `oh app logs <app> --instance <name>` to check the logs.
- to test pages in a browser as the user would see them, use playwright and inject the API token as a Bearer header — this matches a request made with the owner's login cookies.

## specific to this repo

- if you run into any cases where the app test harness doesn't match the expected/real behavior of openhost, stop and mention this so that we can fix the test harness - don't just make some workaround to the issue.
- if you run into cases where openhost itself doesn't behave as expected, also stop and mention this so we can open a PR there to fix upstream.
- this app packages latchkey (github.com/imbue-ai/latchkey) as an OpenHost service provider. read README.md for the architecture and services/latchkey/ for the service interface (spec URL convention like the openhost repo's services/ folder). latchkey is installed as a pinned npm package in the Dockerfile; a reference checkout for reading its source is useful (`git clone https://github.com/imbue-ai/latchkey /tmp/latchkey-review`).
- the front server always attaches a per-consumer permissions-override JWT when proxying to the latchkey gateway; the gateway's default permissions.json is deny-all on purpose. never weaken either side.
- latchkey changes should be PRed upstream (it's an imbue repo); meanwhile npm can install from a fork branch. the upstream wishlist lives in README.md.
