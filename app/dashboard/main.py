"""CSM dashboard entrypoint. Run: python -m streamlit run app/dashboard/main.py

The API token stays in server-side session state and is never rendered into the page. The
organisation comes from the token; there is no organisation selector.
"""

import os
from datetime import UTC, datetime

import httpx
import streamlit as st

from app.dashboard import screens
from app.dashboard.api_client import ApiClient, ApiError

DEFAULT_API_URL = "http://127.0.0.1:8000"


def _sidebar() -> tuple[str, str]:
    st.sidebar.title("Customer health")
    st.sidebar.caption("Simulated delivery only. No real email is sent.")
    base_url = os.environ.get("DASHBOARD_API_URL", DEFAULT_API_URL)
    typed = st.sidebar.text_input(
        "API token",
        type="password",
        help="Identifies your organisation. Blank uses DASHBOARD_API_TOKEN on the server.",
    )
    if st.sidebar.button("Reload data"):
        st.session_state["snapshot"] = None
    token = typed.strip() or os.environ.get("DASHBOARD_API_TOKEN", "").strip()
    return base_url, token


def _client(base_url: str, token: str) -> ApiClient:
    key = (base_url, token)
    if st.session_state.get("client_key") != key:
        previous: ApiClient | None = st.session_state.get("client")
        if previous is not None:
            previous.close()
        st.session_state["client"] = ApiClient(base_url, token)
        st.session_state["client_key"] = key
        st.session_state["snapshot"] = None
        st.session_state["attempts"] = {}
    client: ApiClient = st.session_state["client"]
    return client


def _snapshot(client: ApiClient) -> screens.Snapshot | None:
    snapshot: screens.Snapshot | None = st.session_state.get("snapshot")
    if snapshot is None:
        try:
            snapshot = screens.load_snapshot(client)
        except ApiError as error:
            st.error(f"The API refused the request ({error.status}): {error.detail}")
            return None
        except httpx.TransportError:
            st.error("The API is unreachable. Check that it is running, then reload.")
            return None
        st.session_state["snapshot"] = snapshot
    return snapshot


def main() -> None:
    st.set_page_config(page_title="Customer health", layout="wide")
    base_url, token = _sidebar()
    screen = st.sidebar.radio("Screen", screens.SCREENS, key="screen")
    screens.render_header(screen)
    screens.show_notice()
    if not token:
        st.info("Enter an API token in the sidebar to load your organisation's accounts.")
        return
    client = _client(base_url, token)
    snapshot = _snapshot(client)
    if snapshot is None:
        return
    now = datetime.now(UTC)
    if screen == screens.PORTFOLIO:
        screens.render_portfolio(snapshot, now)
    elif screen == screens.ACCOUNTS:
        screens.render_accounts(snapshot, now)
    elif screen == screens.DETAIL:
        screens.render_detail(client, snapshot, now)
    elif screen == screens.ACTIONS:
        screens.render_actions(snapshot)
    else:
        screens.render_data_quality(client)


main()
