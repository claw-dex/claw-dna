"""Tab: Email — IMAP integration via KeePass credentials."""

import streamlit as st

KEEPASS_ENTRY_TITLE = "Email IMAP"
KEEPASS_GROUP = "Email"
DEFAULT_IMAP_HOST = "imap.gmail.com"


def render():
    import json
    import time

    from app.data.write import run_script

    st.subheader("Email (IMAP)")

    # ── Check if credentials exist in KeePass (cached 60s) ──────
    cred_cache_ts = st.session_state.get("imap_cred_check_ts", 0)
    if time.time() - cred_cache_ts > 60 or "imap_cred_data" not in st.session_state:
        cred_result = run_script("keepass.py", ["--json", "get", KEEPASS_ENTRY_TITLE])
        if cred_result.get("ok") and cred_result.get("exit_code") == 0:
            try:
                st.session_state["imap_cred_data"] = json.loads(
                    cred_result.get("stdout", "{}")
                )
            except json.JSONDecodeError:
                st.session_state["imap_cred_data"] = None
        else:
            st.session_state["imap_cred_data"] = None
        st.session_state["imap_cred_check_ts"] = time.time()

    cred_data = st.session_state.get("imap_cred_data")
    has_cred = cred_data is not None

    if not has_cred:
        # ── No credentials — show setup form ─────────────────────
        st.caption(
            "Enter your email credentials to enable IMAP access. "
            "For Gmail, use an App Password (https://myaccount.google.com/apppasswords)."
        )
        input_server = st.text_input(
            "IMAP server",
            key="imap_input_server",
            placeholder="imap.gmail.com",
        )
        input_email = st.text_input(
            "Email address",
            key="imap_input_email",
            placeholder="you@example.com",
        )
        input_password = st.text_input(
            "Password",
            key="imap_input_password",
            type="password",
        )
        if st.button("Save Credentials", key="imap_save_cred"):
            if not input_email or not input_password or not input_server:
                st.error("IMAP server, email, and password are all required.")
            else:
                result = run_script(
                    "keepass.py",
                    [
                        "--json",
                        "store",
                        "--title",
                        KEEPASS_ENTRY_TITLE,
                        "--username",
                        input_email.strip(),
                        "--password",
                        input_password.strip(),
                        "--url",
                        input_server.strip(),
                        "--group",
                        KEEPASS_GROUP,
                    ],
                )
                if result.get("ok") and result.get("exit_code") == 0:
                    st.success("Credentials saved to KeePass.")
                    # Clear caches so they re-verify
                    st.session_state.pop("imap_auth", None)
                    st.session_state.pop("imap_auth_ts", None)
                    st.session_state.pop("imap_cred_data", None)
                    st.session_state.pop("imap_cred_check_ts", None)
                    st.rerun()
                else:
                    st.error(
                        f"Failed to save: {result.get('stderr', result.get('error', 'Unknown error'))}"
                    )
    else:
        # ── Credentials exist — verify auth (cached 600s) ────────
        user_email = cred_data.get("username", "")
        now = time.time()
        cache_ts = st.session_state.get("imap_auth_ts", 0)

        if now - cache_ts > 600:
            with st.spinner("Verifying IMAP connection..."):
                auth_result = run_script("email_imap.py", ["auth"])
                if auth_result.get("ok") and auth_result.get("exit_code") == 0:
                    try:
                        auth_data = json.loads(auth_result.get("stdout", "{}"))
                        st.session_state["imap_auth"] = auth_data
                    except json.JSONDecodeError:
                        st.session_state["imap_auth"] = None
                else:
                    st.session_state["imap_auth"] = None
                st.session_state["imap_auth_ts"] = now

        auth_info = st.session_state.get("imap_auth")
        token_valid = False

        if (
            auth_info
            and isinstance(auth_info, dict)
            and auth_info.get("status") == "ok"
        ):
            token_valid = True
            st.success(f"Connected — {auth_info.get('email', user_email)}")
        elif auth_info and auth_info.get("error"):
            st.warning(f"Authentication failed: {auth_info['error']}")
        else:
            st.info("Credentials present. Could not verify connection.")

        if st.button("Remove Credentials", key="imap_remove_cred"):
            del_result = run_script(
                "keepass.py", ["--json", "delete", KEEPASS_ENTRY_TITLE]
            )
            if del_result.get("ok") and del_result.get("exit_code") == 0:
                st.session_state.pop("imap_auth", None)
                st.session_state.pop("imap_auth_ts", None)
                st.session_state.pop("imap_cred_data", None)
                st.session_state.pop("imap_cred_check_ts", None)
                for k in list(st.session_state.keys()):
                    if k.startswith("imap_gmail_cache"):
                        st.session_state.pop(k, None)
                st.success("Credentials removed.")
                st.rerun()
            else:
                st.error(
                    f"Failed to remove: {del_result.get('stderr', 'Unknown error')}"
                )

        # ── Inbox (only when auth is valid) ──────────────────────
        if token_valid:
            st.subheader("Inbox")
            is_gmail = "gmail" in (cred_data.get("url", "") or "").lower()

            col_filter, col_archive, col_refresh = st.columns([2, 2, 1])
            with col_filter:
                status_filter = st.selectbox(
                    "Status",
                    ["Unread", "Read", "All"],
                    key="imap_gmail_status",
                )
            with col_archive:
                if is_gmail:
                    hide_archived = st.checkbox(
                        "Hide archived",
                        value=True,
                        key="imap_gmail_hide_archived",
                    )
                else:
                    hide_archived = True  # non-Gmail: always INBOX
            with col_refresh:
                st.write("")  # vertical spacer
                refresh = st.button("Refresh", key="imap_gmail_refresh")

            # Map UI filters to email_imap.py args
            if hide_archived:
                mailbox = "INBOX"
            else:
                mailbox = "[Gmail]/All Mail"

            filter_map = {"Unread": "unseen", "Read": "seen", "All": "all"}
            imap_filter = filter_map[status_filter]

            cache_key = f"imap_gmail_cache_{mailbox}_{imap_filter}"
            cache_ts_key = f"{cache_key}_ts"

            if refresh:
                for k in list(st.session_state.keys()):
                    if k.startswith("imap_gmail_cache") or k.startswith(
                        "imap_read_cache_"
                    ):
                        st.session_state.pop(k, None)
                for k in list(st.session_state.keys()):
                    if k.startswith("imap_email_select_"):
                        st.session_state.pop(k, None)

            gmail_now = time.time()
            if gmail_now - st.session_state.get(cache_ts_key, 0) > 60:
                fetch_result = run_script(
                    "email_imap.py",
                    [
                        "fetch",
                        "--mailbox",
                        mailbox,
                        "--filter",
                        imap_filter,
                        "--max",
                        "100",
                    ],
                )
                if fetch_result.get("ok") and fetch_result.get("exit_code") == 0:
                    stdout = fetch_result.get("stdout", "").strip()
                    if stdout:
                        try:
                            st.session_state[cache_key] = json.loads(stdout)
                        except json.JSONDecodeError:
                            st.session_state[cache_key] = {"messages": []}
                    else:
                        st.session_state[cache_key] = {"messages": []}
                else:
                    stderr = fetch_result.get("stderr", "").strip()
                    # Try parsing JSON error from stdout
                    try:
                        err_data = json.loads(fetch_result.get("stdout", "{}"))
                        st.session_state[cache_key] = {
                            "error": err_data.get("error", stderr or "Fetch failed")
                        }
                    except json.JSONDecodeError:
                        st.session_state[cache_key] = {
                            "error": stderr or "Fetch failed"
                        }
                st.session_state[cache_ts_key] = gmail_now

            gmail_data = st.session_state.get(cache_key, {})

            if "error" in gmail_data:
                st.error(f"Email fetch failed: {gmail_data['error']}")
            else:
                messages = gmail_data.get("messages", [])
                if not messages:
                    st.caption("No emails found.")
                else:
                    import pandas as pd

                    uid_to_msg = {m["uid"]: m for m in messages if m.get("uid")}

                    rows = []
                    for msg in messages:
                        rows.append(
                            {
                                "Subject": msg.get("subject", "(no subject)"),
                                "From": msg.get("from", "unknown"),
                                "Date": msg.get("date", ""),
                                "_uid": msg.get("uid", ""),
                            }
                        )
                    df = pd.DataFrame(rows)

                    df["_date_parsed"] = pd.to_datetime(
                        df["Date"], errors="coerce", utc=True
                    )
                    df = df.sort_values("_date_parsed", ascending=False)
                    sorted_uids = df["_uid"].tolist()
                    df = df.drop(columns=["_date_parsed", "_uid"]).reset_index(
                        drop=True
                    )
                    sorted_messages = [
                        uid_to_msg[u] for u in sorted_uids if u in uid_to_msg
                    ]

                    total = len(sorted_messages)

                    # ── Pagination controls ──────────────────────
                    psize_col, spacer_col, nav_col = st.columns([2, 4, 3])
                    with psize_col:
                        page_size = st.selectbox(
                            "Per page",
                            [10, 20, 50, 100],
                            index=1,
                            key="imap_page_size",
                        )
                    page_size = int(page_size)
                    total_pages = max(1, (total + page_size - 1) // page_size)

                    # Reset page if it's out of range (e.g. after filter change)
                    if st.session_state.get("imap_page", 1) > total_pages:
                        st.session_state["imap_page"] = 1
                    current_page = st.session_state.get("imap_page", 1)

                    with nav_col:
                        st.write("")  # spacer
                        prev_col, page_col, next_col = st.columns([1, 2, 1])
                        with prev_col:
                            if st.button(
                                "◀", key="imap_prev_page", disabled=current_page <= 1
                            ):
                                st.session_state["imap_page"] = current_page - 1
                                st.session_state.pop("imap_selected_uid", None)
                                st.rerun()
                        with page_col:
                            st.caption(f"Page {current_page} / {total_pages}")
                        with next_col:
                            if st.button(
                                "▶",
                                key="imap_next_page",
                                disabled=current_page >= total_pages,
                            ):
                                st.session_state["imap_page"] = current_page + 1
                                st.session_state.pop("imap_selected_uid", None)
                                st.rerun()

                    start = (current_page - 1) * page_size
                    end = min(start + page_size, total)
                    page_messages = sorted_messages[start:end]

                    st.caption(f"Showing {start + 1}–{end} of {total} emails")

                    # ── Email table with inline Read buttons ─────
                    hdr = st.columns([4, 3, 2, 1])
                    hdr[0].markdown("**Subject**")
                    hdr[1].markdown("**From**")
                    hdr[2].markdown("**Date**")
                    hdr[3].markdown("**&nbsp;**", unsafe_allow_html=True)
                    st.divider()

                    for msg in page_messages:
                        uid = msg.get("uid", "")
                        row = st.columns([4, 3, 2, 1])
                        subj = msg.get("subject", "(no subject)")
                        sender = msg.get("from", "unknown")
                        date_str = msg.get("date", "")
                        # Truncate long values for display
                        row[0].write(subj[:70] + ("…" if len(subj) > 70 else ""))
                        row[1].caption(sender[:60] + ("…" if len(sender) > 60 else ""))
                        row[2].caption(date_str[:16] if date_str else "")
                        if row[3].button("Read", key=f"imap_read_btn_{uid}"):
                            st.session_state["imap_selected_uid"] = uid
                            st.session_state["imap_selected_mailbox"] = mailbox

                    # ── Email detail viewer ──────────────────────
                    selected_uid = st.session_state.get("imap_selected_uid")
                    selected_mailbox = st.session_state.get(
                        "imap_selected_mailbox", mailbox
                    )
                    if selected_uid:
                        read_cache_key = f"imap_read_cache_{selected_uid}"
                        if read_cache_key not in st.session_state:
                            with st.spinner("Loading email..."):
                                read_result = run_script(
                                    "email_imap.py",
                                    [
                                        "read",
                                        "--mailbox",
                                        selected_mailbox,
                                        "--uid",
                                        selected_uid,
                                    ],
                                )
                                if (
                                    read_result.get("ok")
                                    and read_result.get("exit_code") == 0
                                ):
                                    try:
                                        st.session_state[read_cache_key] = json.loads(
                                            read_result.get("stdout", "{}")
                                        )
                                    except json.JSONDecodeError:
                                        st.session_state[read_cache_key] = {
                                            "error": "Invalid response"
                                        }
                                else:
                                    st.session_state[read_cache_key] = {
                                        "error": read_result.get(
                                            "stderr", "Read failed"
                                        )
                                    }

                        email_data = st.session_state.get(read_cache_key, {})

                        st.divider()
                        close_col, _ = st.columns([1, 8])
                        if close_col.button("✕ Close", key="imap_close_email"):
                            st.session_state.pop("imap_selected_uid", None)
                            st.session_state.pop("imap_selected_mailbox", None)
                            st.rerun()

                        if "error" in email_data:
                            st.error(f"Could not load email: {email_data['error']}")
                        else:
                            st.write(f"**Subject:** {email_data.get('subject', '')}")
                            st.write(f"**From:** {email_data.get('from', '')}")
                            st.write(f"**Date:** {email_data.get('date', '')}")
                            body = email_data.get("body_text", "") or "(no content)"
                            st.code(body, language=None)
