"""Actions for record.py — log into the-internet.herokuapp.com.

A `run(page)` coroutine that record.py executes against the live page while the
session is being recorded. Credentials are supplied only at execution time via
DEMO_USERNAME and DEMO_PASSWORD; they never enter the recording or generated
script.
"""

import os


async def run(page):
    username = os.environ.get("DEMO_USERNAME")
    password = os.environ.get("DEMO_PASSWORD")
    if not username or not password:
        raise RuntimeError("Set DEMO_USERNAME and DEMO_PASSWORD for the demo login")
    await page.locator("#username").fill(username)
    await page.locator("#password").fill(password)
    await page.locator("button[type='submit']").click()
