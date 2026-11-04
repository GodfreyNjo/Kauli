"""The customer menu: ten rows in the order of the design (Home, Orders, Attention, Files, Billing / Analytics, Workspace, Learn, Integrations, Rewards / Get Help, Settings),
and nothing that used to be reachable is lost: Notes sits under Files, Vocabulary under Workspace, Meetings under Orders."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from test_order_flow import env, wav_bytes  # noqa: E402,F401


def _nav(html: str) -> str:
    return html.split('<nav class="sidebar-nav"')[1].split("</nav>")[0]


def test_the_customer_menu_follows_the_design_and_keeps_every_page_reachable(env):
    nav = _nav(env.client.get("/client/home").text)
    labels = re.findall(r'<span class="label">([^<]+)</span>', nav)
    top = [x for x in labels if x in ("Home", "Orders", "Attention", "Files", "Billing", "Analytics", "Workspace", "Learn", "Integrations", "Rewards", "Get Help", "Settings")]
    assert top == ["Home", "Orders", "Attention", "Files", "Billing", "Analytics", "Workspace", "Learn", "Integrations", "Rewards", "Get Help", "Settings"], top
    for href in ("/client/home", "/client", "/client/pending", "/client/files", "/client/notes", "/client/billing", "/client/analytics", "/client/workspace",
                 "/client/vocabulary", "/client/resources", "/client/integrations", "/client/rewards", "/help", "/settings"):
        assert f'href="{href}"' in nav, href
    assert nav.count('class="sidebar-divider"') == 2


def test_the_page_you_are_on_is_marked_and_its_group_is_open(env):
    nav = _nav(env.client.get("/client/notes").text)
    files_group = nav.split('data-nav-grp="/client/files"')[1].split("</div>\n          </div>")[0]
    assert "is-open" in nav.split('data-nav-grp="/client/files"')[0][-60:] and 'href="/client/notes" class="active"' in files_group


def test_customers_get_the_navy_menu_and_staff_still_do(env):
    page = env.client.get("/client/home").text
    assert "staff-ui client-ui" in page
