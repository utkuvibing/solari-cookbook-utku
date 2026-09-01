"""Non-destructive post-login action for the profile workflow."""


async def run(page):
    """Click a unique element that exists only on the authenticated page."""
    await page.get_by_role("heading", name="Secure Area", exact=True).click()
