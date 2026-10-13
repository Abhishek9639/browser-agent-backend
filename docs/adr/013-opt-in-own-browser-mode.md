# ADR-013: An opt-in mode that drives the user's own Chrome

- **Status:** accepted, implemented
- **Date:** 2026-10 (part B32)
- **Spec:** section 4.13 (exception noted there)

## Context

The specification gives every task a fresh, isolated, download-disabled context. That is the right default for a sandbox, but a user who wants to watch the agent work, or reuse a window they are already signed in to, cannot do that with a hidden or separate browser.

## Decision

`AGENT_CDP_URL`, empty by default. When set, `PlaywrightDriver` and `TaskRunner` call `connect_over_cdp` and use the browser's existing default context. The agent opens its own tab, raises it with `bring_to_front`, and on teardown closes only the tabs it tracked. It never closes the user's context or browser. The domain allow-list still applies. The runner drops to one concurrent task, because two tasks would share one context and fight over focus.

## Alternatives rejected

- **Always attach.** Removes the isolation guarantee for every task.
- **A browser extension.** Out of scope in the specification, and a new surface to secure.
- **Attach to the default profile.** Chrome 136+ refuses remote debugging there, so `scripts/chrome_debug.ps1` uses a dedicated profile.

## Consequences

In this mode cookies and storage are the user's, viewport, locale and download settings are not enforced, and a tab the user opens by hand during a task can be tracked and closed at the end. Isolation is unchanged when the setting is empty.

## Tests

`test_attached_driver_uses_the_users_browser_and_leaves_it_running`, `test_visible_browser_raises_the_tab_it_acts_on`, `test_headless_never_tries_to_raise_a_tab`.
