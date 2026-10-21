# Chromium sandbox

`chromium-seccomp.json` is copied unchanged from the official Playwright repository:
https://github.com/microsoft/playwright/blob/v1.63.0/utils/docker/seccomp_profile.json

Playwright is licensed under Apache-2.0. The profile extends Docker's normal syscall allowlist to permit creating the user namespaces Chromium needs for its sandbox. The worker runs as UID 10001, with `chromium_sandbox=True`, `no-new-privileges` and a private shared-memory allocation. No privileged mode or extra Linux capabilities are configured.

The installed Playwright version and matching browser binary are controlled by `requirements.lock` and `python -m playwright install`; the syscall policy is pinned independently.
