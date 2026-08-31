"""Actions for record.py — log into the-internet.herokuapp.com.

A `run(page)` coroutine that record.py executes against the live page while the
session is being recorded. The rrweb events from these interactions are what
replaysynth turns into replay steps.
"""

async def run(page):
    # Elemental Selenium's classic demo login (public test account).
    await page.locator("#username").fill("tomsmith")
    await page.locator("#password").fill("SuperSecretPassword!")
    await page.locator("button[type='submit']").click()
