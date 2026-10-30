"""The browse cluster: job sites read through the user's own logged-in browser.

Add a site by writing a BrowseSite subclass plus a listing spec, and
registering it here; `sachin-agent-scraper browse --ok` covers every site.
"""

from scraper.browse.base import BrowseSite
from scraper.browse.linkedin import LinkedInSite

BROWSE_SITES: dict[str, type[BrowseSite]] = {
    "linkedin": LinkedInSite,
}
