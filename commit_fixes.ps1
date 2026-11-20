# Run this in your terminal to commit all driver stealth + reliability fixes
Set-Location "c:\Users\ishaa\OneDrive\Desktop\BAA Backend'\browser-agent-backend"
git add app/agent/browser/driver.py
git commit -m "fix: resolve AttributeError and improve browser stealth & reliability

Bug fix:
- driver.py: fix AttributeError: 'Locator' object has no attribute 'triple_click'
  Replaced with locator.click(click_count=3) and catch Exception so no unhandled
  exception escapes driver.execute to abort orchestrator with AttributeError.

Stealth anti-detection (driver.py):
- launch_args: add --disable-blink-features=AutomationControlled + no-first-run flags
- BrowserConfig.user_agent: default to real Chrome 131 UA (removes HeadlessChrome string)
- launch: inject navigator.webdriver=undefined via add_init_script on every page
  Fixes: Google 'This browser or app may not be secure' sign-in block

Agent reliability (driver, orchestrator, verifier, recovery):
- driver.py: click(click_count=3) + settle delay in _type for autocomplete fields
- driver.py: double Ctrl+A/Delete in _type_at_keyboard for stubborn editors
- driver.py: Ctrl+A + Delete twice in _insert_block for Monaco/CodeMirror
- driver.py: force=True click fallback when normal click times out
- driver.py: Tab key after Escape in _clear_obstruction for focus-out dismissal
- driver.py: mouse.wheel fallback in _scroll for event-driven scroll containers
- verifier.py: _scrolled counts DOM/text change as scroll success (virtual scroll)
- orchestrator.py: MAX_IDLE_SCROLLS=4, MAX_IDLE_WAITS=3, MAX_IDLE_EXTRACTS=4
- strategies.py: lower FUZZY_THRESHOLD 0.8->0.65 for reworded button labels
- planner_v2.md: guidance on idle scrolls, covered-element recovery, autocomplete

Plan-step sync fix (events.py, stream.py, orchestrator.py):
- PlanEvent: add current_step field (1-based index of active plan item)
- _follow_plan: publish PlanEvent(current_step=N) before each step runs
- main loop: publish PlanEvent(current_step=N+1) after each verified step
- stream.py replay: derive current_step from highest verified plan_step in DB
- Plan cursor publish is event-only (no DB write) when plan list unchanged
"
git push -u origin feat/own-browser-mode
