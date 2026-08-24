#!/usr/bin/env python3
"""Monitor all network calls on a LinkedIn profile and search for follower data.

Run: python -m linkedin.api.network_monitor --profile <public_id> [--headless]

Captures every API response during page load, searches for follower-related
fields, and saves matching responses to a file for inspection.
"""
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def monitor_network(session, profile_url: str, output_dir: str = "network_dumps") -> dict:
    """
    Load a profile and capture all network responses containing follower data.

    Returns a dict mapping endpoint URLs to their responses.
    """
    from linkedin.api.client import PlaywrightLinkedinAPI

    output_path = Path(output_dir)
    output_path.mkdir(exist_ok=True)

    api = PlaywrightLinkedinAPI(session=session)
    browser = session.browser
    page = browser.new_page() if hasattr(browser, 'new_page') else browser.current_page

    captured_responses = {}
    follower_keywords = [
        "follower", "followers", "followerscount", "followercount",
        "networkinfo", "followingstate", "followeecount"
    ]

    def capture_response(response):
        """Intercept and log network responses."""
        url = response.url
        try:
            if response.status == 200 and "json" in response.headers.get("content-type", "").lower():
                body = response.json()
                body_str = json.dumps(body, default=str).lower()

                # Search for follower-related keywords
                for keyword in follower_keywords:
                    if keyword in body_str:
                        captured_responses[url] = {
                            "status": response.status,
                            "response": body,
                            "matched_keyword": keyword,
                        }
                        logger.info(
                            "Found '%s' in %s", keyword, url
                        )
                        break
        except Exception as e:
            logger.debug("Could not parse response from %s: %s", url, e)

    # Hook into response events
    page.on("response", capture_response)

    # Navigate to profile
    logger.info("Loading %s ...", profile_url)
    page.goto(profile_url, wait_until="networkidle")

    # Wait a bit for lazy-loaded requests
    import time
    time.sleep(2)

    page.close()

    # Save captured responses
    if captured_responses:
        output_file = output_path / "follower_responses.json"
        with output_file.open("w") as f:
            # Save in a readable format, one response per endpoint
            results = {}
            for url, data in captured_responses.items():
                results[url] = {
                    "status": data["status"],
                    "matched_keyword": data["matched_keyword"],
                    "response_preview": str(data["response"])[:500],  # First 500 chars
                }
            json.dump(results, f, indent=2)
        logger.info("Captured %d responses with follower data → %s", len(captured_responses), output_file)

        # Also save full responses for inspection
        full_file = output_path / "follower_responses_full.json"
        with full_file.open("w") as f:
            json.dump(captured_responses, f, indent=2, default=str)
        logger.info("Full responses saved → %s", full_file)
    else:
        logger.warning("No responses found containing follower-related keywords")

    return captured_responses


if __name__ == "__main__":
    from linkedin.browser.registry import cli_parser, cli_session

    parser = cli_parser("Monitor network calls to find follower count endpoints")
    parser.add_argument("--profile", default="me", help="Public identifier of the target profile (default: me)")
    parser.add_argument("--output-dir", default="network_dumps", help="Directory to save captured responses")
    args = parser.parse_args()

    session = cli_session(args)
    session.ensure_browser()

    profile_url = f"https://www.linkedin.com/in/{args.profile}/"
    results = monitor_network(session, profile_url, output_dir=args.output_dir)

    if results:
        print(f"\n✓ Found follower data in {len(results)} endpoint(s):")
        for url, data in results.items():
            print(f"\n  URL: {url}")
            print(f"  Matched keyword: {data['matched_keyword']}")
            print(f"  Response keys: {list(data['response'].keys())}")
    else:
        print("\n✗ No follower data found in any network responses")
