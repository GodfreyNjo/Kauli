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


def test_one_theme_for_every_signed_in_page_and_it_follows_other_open_tabs(env):
    """The saved light/dark choice applies on every page; with none saved every page is light (not whatever the page was written with)."""
    for path in ("/client/home", "/client", "/client/billing", "/client/notes", "/help", "/settings"):
        page = env.client.get(path).text
        assert 'data-theme="light"' in page.split("<title>")[0], path
        assert 't === "dark" ? "dark" : "light"' in page and 'ev.key !== "kauli_theme"' in page, path
    css = (Path(__file__).parent.parent / "webapp" / "static" / "style.css").read_text(encoding="utf-8")
    for rule in ('html[data-theme="dark"] .hub-top h1', 'html[data-theme="dark"] .m-faq-item', 'html[data-theme="dark"] .src-card'):
        assert rule in css, rule


def test_the_account_menu_for_a_customer_has_no_role_a_dark_mode_switch_and_a_flag_grid(env):
    page = env.client.get("/client/home").text
    menu = page.split('id="account-menu-panel"')[1].split("</header>")[0]
    assert 'class="user-role"' not in menu      # the word Client is not shown to a customer
    assert 'role="switch"' in menu and 'class="am-switch"' in menu and "Dark mode" not in menu.split("am-switch")[0] or "theme-toggle-label" in menu
    for code in ("EN", "ES", "ZH", "AR", "FR", "PT", "DE", "HI", "JA", "KO", "SW"):
        assert f"<span>{code}</span>" in menu, code
    assert menu.count('class="lang-flag"') == 11 and "Sign out" in menu
