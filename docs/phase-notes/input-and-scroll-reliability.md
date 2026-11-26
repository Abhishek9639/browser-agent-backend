# Input and scrolling reliability

> Historical report from 22 November 2026. Subsequent Gemini and real Uber
> pre-booking tests have now run. See `gemini-live-task-reliability.md`
> for current results; the original observations below describe the earlier setup.

The user reported Uber task failures during address entry and scrolling. The
local database contains no Uber task trace, and this checkout is configured with
the SkyFare mock provider and a localhost-only allow-list. These changes fix
concrete driver defects; they do not establish that the live Uber workflow works.

## Changes

- Preserve existing input when `clear_first` is false.
- Require the full intended value after input, including after keyboard recovery.
  Partial values no longer pass merely because one string contains the other.
- Handle Unicode, empty values and contenteditable text when validating input.
- Choose a visible, editable descendant instead of a hidden clone.
- Wait for a popup to focus an editable field before sending keyboard shortcuts.
  Refuse to type into buttons or an unrelated previously focused input.
- Scroll the active autocomplete list, focused panel or visible dialog before
  falling back to the document. Remove the old minimum-panel-area rule that
  excluded narrow ride-option and address panels.
- Use the same scroll-target selection in execution and perception.
- Add local cab-form coverage: delayed pickup popup, keyboard-sensitive search,
  address selection, drop-off entry, scrolling and selecting a demo ride option.
  The fixture does not place a booking and is not a copy of Uber's live website.

## Verification

Lint, formatting and mypy pass. All 1,219 non-browser unit tests pass; one live
provider test is skipped. The live SkyFare API/WebSocket smoke passes after these
changes. The focused browser run passed 19 input/scroll tests. The cab-form test
initially expected 500 pixels despite the panel having only 451 pixels of scroll
range. Its corrected assertion uses the actual panel limit, and the isolated
recheck passed in 5.70 seconds. Thus all 20 focused checks passed across two runs;
this was not a full-suite rerun after the input/scroll changes.

The earlier full suite, before this follow-up, passed 1,777 tests, skipped the
live-provider test and failed the local Firefox startup case. That Firefox
limitation is documented in `docs/LOCAL_DEMO.md`.

## Real Uber reproduction still needed

The user identified Gemini as the provider. Use the actual failing instruction,
confirmed pickup and destination, and an available Gemini model. Configure its API key locally; do not paste secrets into
chat. Replace the mock provider and local default start URL, and allow the exact
Uber hosts needed by the observed flow. Login or OTP may need user interaction.
Stop before requesting a ride; no real ride or payment is authorized by this
repair task. Save the task ID and failing step to distinguish planner mistakes,
site restrictions, stale selectors, rejected inputs and driver failures.

## Viva questions

1. Why can a successful browser action still represent a failed task step?
   A browser API can dispatch an event while the page rejects the value or the
   wrong panel moves. The resulting state must be checked.
2. Why do scrolling and perception share a selection rule?
   Measuring the background while scrolling a dialog gives contradictory state
   and can trigger false recovery or repeated scrolling.
3. Why is a local cab-form test insufficient to claim Uber support?
   It tests controlled browser interactions, not Uber's current UI, authentication,
   anti-automation behavior, network responses or the real planner's decisions.
