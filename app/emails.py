"""Tab: Email — Google Workspace Gmail integration."""

import streamlit as st

GWS_CRED_PATH = "/home/agent/.config/gws/credentials.json"
GWS_REQUIRED_KEYS = {"client_id", "client_secret", "refresh_token", "type"}


def render():
    import json
    import os
    import subprocess
    import time
    from pathlib import Path

    # ── Google Workspace section ──────────────────────────────────
    st.subheader("Google Workspace")
    cred_path = Path(GWS_CRED_PATH)

    if not cred_path.exists():
        st.caption("Upload your Google OAuth credentials file to enable Gmail and other Google Workspace features.")
        uploaded = st.file_uploader(
            "Upload credentials.json",
            type=["json"],
            key="gws_upload_cred",
        )
        if uploaded is not None:
            data = None
            try:
                raw = uploaded.getvalue().decode("utf-8")
                data = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                st.error("Invalid JSON file. Please upload a valid credentials.json.")

            if data is not None:
                missing = GWS_REQUIRED_KEYS - set(data.keys())
                if missing:
                    st.error(
                        f"Missing required fields: {', '.join(sorted(missing))}. "
                        "Expected keys: client_id, client_secret, refresh_token, type."
                    )
                elif data.get("type") != "authorized_user":
                    st.error(
                        f"Invalid credential type: '{data.get('type')}'. "
                        "Expected 'authorized_user'."
                    )
                else:
                    try:
                        os.makedirs(cred_path.parent, exist_ok=True)
                        cred_path.write_text(raw, encoding="utf-8")
                        st.success("Google Workspace credentials saved.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Failed to save credentials: {exc}")
    else:
        # Credentials file exists — verify auth status (cached 600s)
        now = time.time()
        cache_ts = st.session_state.get("gws_auth_ts", 0)
        if now - cache_ts > 600:
            try:
                result = subprocess.run(
                    ["gws", "auth", "status", "--format", "json"],
                    capture_output=True, text=True, timeout=15,
                )
                if result.returncode == 0:
                    auth_data = json.loads(result.stdout)
                    st.session_state["gws_auth"] = auth_data
                else:
                    st.session_state["gws_auth"] = None
            except Exception:
                st.session_state["gws_auth"] = None
            st.session_state["gws_auth_ts"] = now

        auth_info = st.session_state.get("gws_auth")
        token_valid = False

        if auth_info and isinstance(auth_info, dict):
            token_valid = auth_info.get("token_valid", False)
            user_email = auth_info.get("user", "")
            if token_valid:
                st.success(f"Google Workspace connected — {user_email}")
            else:
                st.warning("Google Workspace token is expired or invalid. Remove and re-upload credentials.")
        else:
            st.info("Credentials file present. Could not verify token status.")

        if st.button("Remove Credentials", key="gws_remove_cred"):
            try:
                cred_path.unlink(missing_ok=True)
                st.session_state.pop("gws_auth", None)
                st.session_state.pop("gws_auth_ts", None)
                # Clear all gmail caches
                for k in list(st.session_state.keys()):
                    if k.startswith("gws_gmail_cache"):
                        st.session_state.pop(k, None)
                st.success("Credentials removed.")
                st.rerun()
            except Exception as exc:
                st.error(f"Failed to remove credentials: {exc}")

        # ── Gmail Inbox (only when token is valid) ─────────────────
        if token_valid:
            st.subheader("Gmail")

            # Filter controls
            col_filter, col_archive, col_refresh = st.columns([2, 2, 1])
            with col_filter:
                status_filter = st.selectbox(
                    "Status", ["Unread", "Read", "All"],
                    key="gws_gmail_status",
                )
            with col_archive:
                hide_archived = st.checkbox(
                    "Hide archived", value=True, key="gws_gmail_hide_archived",
                )
            with col_refresh:
                st.write("")  # vertical spacer to align button
                refresh = st.button("Refresh", key="gws_gmail_refresh")

            # Build Gmail search query
            query_parts = []
            if status_filter == "Unread":
                query_parts.append("is:unread")
            elif status_filter == "Read":
                query_parts.append("is:read")
            if hide_archived:
                query_parts.append("in:inbox")
            query_str = " ".join(query_parts)

            # Cache key includes query so filter changes trigger a fresh fetch
            cache_key = f"gws_gmail_cache_{query_str}"
            cache_ts_key = f"{cache_key}_ts"

            if refresh:
                st.session_state.pop(cache_key, None)
                st.session_state.pop(cache_ts_key, None)

            gmail_now = time.time()
            if gmail_now - st.session_state.get(cache_ts_key, 0) > 60:
                cmd = ["gws", "gmail", "+triage", "--format", "json", "--max", "20"]
                if query_str:
                    cmd += ["--query", query_str]
                try:
                    result = subprocess.run(
                        cmd, capture_output=True, text=True, timeout=30,
                    )
                    if result.returncode == 0:
                        # Handle empty response (no emails) gracefully
                        stdout = result.stdout.strip()
                        if stdout:
                            st.session_state[cache_key] = json.loads(stdout)
                        else:
                            # No emails found - return empty messages list
                            st.session_state[cache_key] = {"messages": []}
                    else:
                        st.session_state[cache_key] = {
                            "error": result.stderr.strip() or "Command failed"
                        }
                except json.JSONDecodeError:
                    # Invalid JSON response - treat as no emails
                    st.session_state[cache_key] = {"messages": []}
                except Exception as exc:
                    st.session_state[cache_key] = {"error": str(exc)}
                st.session_state[cache_ts_key] = gmail_now

            gmail_data = st.session_state.get(cache_key, {})

            if "error" in gmail_data:
                st.error(f"Gmail fetch failed: {gmail_data['error']}")
            else:
                messages = gmail_data.get("messages", [])
                if not messages:
                    st.caption("No emails found.")
                else:
                    import pandas as pd

                    rows = []
                    for msg in messages:
                        rows.append({
                            "Subject": msg.get("subject", "(no subject)"),
                            "From": msg.get("from", "unknown"),
                            "Date": msg.get("date", ""),
                        })
                    df = pd.DataFrame(rows)

                    # Parse dates for proper sorting (most recent first)
                    df["_date_parsed"] = pd.to_datetime(df["Date"], errors="coerce")
                    df = df.sort_values("_date_parsed", ascending=False)
                    df = df.drop(columns=["_date_parsed"]).reset_index(drop=True)

                    st.caption(f"Showing {len(df)} emails")
                    st.dataframe(
                        df,
                        use_container_width=True,
                        height=min(400, 35 * (len(df) + 1)),
                        hide_index=True,
                    )
