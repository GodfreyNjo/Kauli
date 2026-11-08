"""Old addresses still work: /client/... goes to /account/... and /staff/clients/... to /staff/customers/...

The method and the query string are kept.

The OAuth callbacks are the exception: providers have those exact addresses registered, so they must not move.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import env  # noqa: E402,F401


def test_an_old_customer_address_redirects_to_the_new_one_keeping_the_query(env):
    reply = env.client.get("/client/orders/abc123?tab=notes")
    assert reply.status_code == 308 and reply.headers["location"] == "/account/orders/abc123?tab=notes"
    assert env.client.get("/client").headers["location"] == "/account"
    assert env.client.get("/client/billing").headers["location"] == "/account/billing"


def test_a_form_posted_to_an_old_address_keeps_its_method(env):
    reply = env.client.post("/client/orders/quote", data={"minutes": "10"})
    assert reply.status_code == 308 and reply.headers["location"] == "/account/orders/quote"   # 308 re-sends the POST


def test_the_staff_customer_list_has_a_new_address_and_the_old_one_redirects(env):
    assert env.client.get("/staff/clients").headers["location"] == "/staff/customers"
    assert env.client.get("/staff/clients/abc/followup").headers["location"] == "/staff/customers/abc/followup"


def test_the_new_address_serves_the_page_and_the_old_prefix_is_still_kept_out_of_search(env):
    assert env.client.get("/account/home").status_code == 200
    robots = env.client.get("/robots.txt").text
    assert "Disallow: /account" in robots and "Disallow: /client" in robots


def test_the_oauth_callbacks_keep_their_registered_addresses(env):
    reply = env.client.get("/client/youtube/callback?error=access_denied")
    assert reply.status_code != 308                      # served by its own route, not redirected
    assert env.client.get("/client/integrations/gdocs/callback?error=x").status_code != 308
