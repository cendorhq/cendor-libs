---
name: write-tests
description: Testing conventions for cendor packages — pytest, mocked provider clients, golden token counts, and no network calls. Use when writing or reviewing tests in this repo.
---
# Testing conventions

- Use **pytest**; async tests via **pytest-asyncio**.
- **No network in tests.** Mock provider clients; never call a real API.
- **Token counting:** golden tests — known input string → expected token count per model.
- **Money:** assert `Decimal` exactness; never compare floats.
- **`instrument()`:** test that wrapping is idempotent (double-wrap is a no-op) and that a fake subscriber receives a normalized `LLMCall`.
- **`tokenguard`:** test pre-flight `raise`/`truncate` and post-flight tag aggregation using a mock call event on the bus.
- **`cassette`:** the tool *is* test infrastructure — prefer its own record/replay fixtures over hand-written mocks.
- Keep each test fast (< 1s) and deterministic. Aim for high coverage on the public API.
